import json
import os
from pathlib import Path
import tempfile
import subprocess
import sys
import time
import unittest
from unittest.mock import patch

from wechat_receiver.config import Config
from wechat_receiver.service import DatabaseLock, Receiver
from wechat_receiver.store import Store, status
from wechat_receiver.tail import Framer


def event(seq=1, content="你好😀", **changes):
    record = dict(kind="item", schema_version=2, session_id="test-session", seq=seq,
                  msg_type=1, content=content, **{"from": "member", "to": "self"},
                  content_read={"status": "ok"}, from_read={"status": "ok"}, to_read={"status": "ok"})
    record.update(changes)
    return (json.dumps(record, ensure_ascii=False) + "\r\n").encode("utf-8")


class ReceiverTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.log = self.root / "中文 messages.jsonl"
        self.log.write_bytes(b"")
        self.path = self.root / "messages.sqlite3"
        self.store = Store(self.path)
        self.config = Config(self.log, self.path, self_id="self", max_line_bytes=1024)
        self.receiver = Receiver(self.config, self.store)

    def tearDown(self):
        self.receiver.close()
        self.store.close()
        self.temp.cleanup()

    def restart(self):
        self.receiver.close()
        self.receiver = Receiver(self.config, self.store)

    def count(self, table="raw_events"):
        return self.store.db.execute(f"SELECT count(*) FROM {table}").fetchone()[0]

    def append(self, raw):
        with self.log.open("ab") as stream:
            stream.write(raw)

    def test_partial_utf8_and_restart_checkpoint(self):
        raw = event()
        cut = raw.index("😀".encode()) + 2
        self.append(raw[:cut])
        self.assertEqual(self.receiver.poll(), 0)
        self.assertEqual(self.receiver.runtime_issue, "receiver_log_incomplete_line")
        self.assertEqual(self.store.latest_source(self.log)["offset"], 0)
        self.restart()
        self.append(raw[cut:])
        self.assertEqual(self.receiver.poll(), 1)
        self.restart()
        self.assertEqual(self.receiver.replay(), 0)
        self.assertEqual(self.count("messages"), 1)
        model = json.loads(self.store.db.execute("SELECT model_json FROM messages").fetchone()[0])
        self.assertEqual(model["content"], "你好😀")
        self.assertEqual(model["history_state"], "backlog")

    def test_bounded_poll_reports_unread_log_bytes_until_drained(self):
        raw = event(content="x" * 800)
        self.append(raw * 400)
        first = self.receiver.poll_batch()
        self.assertGreater(first.records, 0)
        self.assertTrue(self.receiver.runtime_issue.startswith("receiver_log_unread:"))
        while self.receiver.runtime_issue:
            self.receiver.poll_batch()
        self.assertEqual(self.count(), 400)

    def test_command_pipe_control_events_are_not_messages(self):
        for kind in ("command_pipe_ready", "command_pipe_error"):
            self.append((json.dumps({"kind": kind, "session_id": "test-session",
                                     "mode": "read_only", "send_text": False}) + "\n").encode())
        self.assertEqual(self.receiver.replay(), 2)
        self.assertEqual(self.count("messages"), 0)
        self.assertEqual(self.count("issues"), 1)
        self.assertEqual(self.store.db.execute("SELECT parse_status FROM raw_events").fetchall()[0][0], "ok")

    def test_submit_trace_is_raw_only_and_disabled_is_diagnostic(self):
        for kind in ("send_submit_trace_enabled", "send_submit_enter", "send_submit_return", "send_submit_hook_disabled"):
            self.append((json.dumps({"kind": kind, "session_id": "test-session", "schema_version": 2,
                                     "source": "send_submit", "content": "SEND-SUBMIT-001 你好",
                                     "completion_callbacks": [{"read_status": "empty", "payload": "0x0"}]})
                         + "\n").encode("utf-8"))
        self.assertEqual(self.receiver.replay(), 4)
        self.assertEqual(self.count("raw_events"), 4)
        self.assertEqual(self.count("messages"), 0)
        self.assertEqual(self.count("issues"), 1)

    def test_native_send_diagnostics_are_not_delivered_messages(self):
        for kind in ("native_sender_ready", "native_sender_disabled", "native_send_result"):
            self.append((json.dumps({"kind": kind, "session_id": "test-session", "schema_version": 2,
                                    "request_id": "one", "status": "accepted", "reason": "test"})
                         + "\n").encode("utf-8"))
        self.assertEqual(self.receiver.replay(), 3)
        self.assertEqual(self.count("messages"), 0)
        self.assertEqual(self.count("issues"), 1)
        self.assertEqual(len(status(self.path)["latest_session_controls"]), 3)

    def test_bad_unknown_oversized_and_next_record(self):
        self.append(b"broken\n" + b'{"kind":"future"}\n' + b"x" * 3000)
        self.receiver.poll()
        self.assertEqual(self.count(), 2)
        self.assertLessEqual(len(self.receiver.framer.buffer), 1024)
        self.append(b"\n" + event())
        self.receiver.poll()
        self.assertEqual(self.count(), 4)
        self.assertEqual(self.count("messages"), 1)
        self.assertEqual(self.count("issues"), 3)

    def test_send_context_diagnostics_are_raw_only(self):
        for seq, kind in enumerate(("send_context_trace_enabled", "send_context_enter",
                                    "send_context_return"), start=1):
            self.append(event(seq, kind=kind, source="send_context_constructor", call_id=12,
                              call_stack={"scope": "weixin_rva", "rvas": ["0x176b7bf", None]}))
        self.append(event(4, kind="outbound_item"))
        self.assertEqual(self.receiver.replay(), 4)
        self.assertEqual(self.count("messages"), 1)
        self.assertEqual(self.count("issues"), 0)
        records = [json.loads(r[0]) for r in self.store.db.execute(
            "SELECT canonical_json FROM raw_events WHERE kind LIKE 'send_context_%' ORDER BY id")]
        self.assertEqual(len(records), 3)
        self.assertEqual(records[1]["content"], "你好😀")
        self.assertEqual(records[1]["call_stack"]["rvas"], ["0x176b7bf", None])
        self.store.renormalize("self")
        self.assertEqual(self.count("messages"), 1)
        controls = status(self.path)["latest_session_controls"]
        self.assertEqual([r["kind"] for r in controls], ["send_context_trace_enabled"])

    def test_send_context_hook_disabled_is_reported(self):
        self.append(event(kind="send_context_hook_disabled", reason="entry_signature_mismatch"))
        self.receiver.replay()
        self.assertEqual(self.count("messages"), 0)
        self.assertEqual(self.store.db.execute("SELECT code FROM issues").fetchone()[0],
                         "send_context_hook_disabled")
        controls = status(self.path)["latest_session_controls"]
        self.assertEqual(controls[0]["reason"], "entry_signature_mismatch")

    def test_transaction_failure_replay(self):
        self.append(event())
        with patch("wechat_receiver.store.normalize", side_effect=RuntimeError("injected failure")):
            with self.assertRaises(RuntimeError):
                self.receiver.poll()
        self.assertEqual(self.count(), 0)
        self.assertEqual(self.store.latest_source(self.log)["offset"], 0)
        self.restart()
        self.assertEqual(self.receiver.poll(), 1)

    def test_identity_duplicate_and_conflict(self):
        self.append(event() + event() + event(content="different"))
        self.receiver.replay()
        self.assertEqual(self.count(), 3)
        self.assertEqual(self.count("messages"), 2)
        codes = {r[0] for r in self.store.db.execute("SELECT code FROM issues")}
        self.assertEqual(codes, {"duplicate_event", "event_identity_conflict"})

    def test_truncate_and_regrow_anchor(self):
        self.append(event(content="old"))
        self.receiver.poll()
        self.log.write_bytes(event(2, content="new"))
        self.receiver.poll()
        self.assertEqual(self.count("messages"), 2)
        self.assertEqual(self.store.latest_source(self.log)["generation"], 2)

    def test_rotation_drains_old_and_quarantines_half_line(self):
        self.append(event())
        self.receiver.poll()
        old = self.root / "old.jsonl"
        self.log.rename(old)
        with old.open("ab") as stream:
            stream.write(event(2) + b"partial")
        self.log.write_bytes(event(3))
        self.assertEqual(self.receiver.poll(), 1)
        self.receiver.poll()
        self.receiver.rotation_since -= 2
        self.receiver.poll()
        self.assertEqual(self.receiver.poll(), 1)
        self.assertEqual(self.count("messages"), 3)
        self.assertIn("incomplete_rotated_line", [r[0] for r in self.store.db.execute("SELECT code FROM issues")])

    def test_legacy_session_restored(self):
        first = json.loads(event())
        del first["session_id"]
        self.append(b'{"kind":"observer_start"}\n' + json.dumps(first).encode() + b"\n")
        self.receiver.poll()
        session = self.store.latest_source(self.log)["session_id"]
        self.restart()
        first["seq"] = 2
        self.append(json.dumps(first).encode() + b"\n")
        self.receiver.poll()
        self.assertEqual(self.store.latest_source(self.log)["session_id"], session)
        self.assertTrue(session.startswith("legacy:"))

    def test_single_writer_lock_and_readonly_status(self):
        with DatabaseLock(self.path):
            with self.assertRaises(RuntimeError):
                with DatabaseLock(self.path):
                    pass
        self.assertEqual(status(self.path)["raw_events"], 0)
        with self.assertRaises(FileNotFoundError):
            status(self.root / "absent.sqlite3")

    def test_framer_only_splits_lf(self):
        f = Framer(0, 100)
        self.assertEqual(list(f.feed(b"a\rb\x0bc")), [])
        line, = list(f.feed(b"\n"))
        self.assertEqual(line.raw, b"a\rb\x0bc\n")

    def test_surrogate_and_unsupported_schema_are_quarantined(self):
        self.append(b'{"kind":"item","content":"\\ud800"}\n' + event(schema_version=99) + event(2))
        self.receiver.replay()
        self.assertEqual(self.count(), 3)
        self.assertEqual(self.count("messages"), 1)
        self.assertEqual(self.count("issues"), 2)

    def test_restart_after_replacement_creates_new_generation(self):
        self.append(event())
        self.receiver.replay()
        self.receiver.close()
        self.log.rename(self.root / "old.jsonl")
        self.log.write_bytes(event(2))
        self.restart()
        self.assertEqual(self.receiver.replay(), 1)
        self.assertEqual(self.store.latest_source(self.log)["generation"], 2)

    def test_renormalize_updates_interpretation_not_raw_or_checkpoint(self):
        self.append(event())
        self.receiver.replay()
        before = self.store.latest_source(self.log)["offset"]
        self.assertEqual(self.store.renormalize(""), 1)
        self.assertEqual(self.store.db.execute("SELECT direction FROM messages").fetchone()[0], "unknown")
        self.store.renormalize("self")
        self.assertEqual(self.store.db.execute("SELECT direction FROM messages").fetchone()[0], "incoming")
        self.assertEqual(self.count(), 1)
        self.assertEqual(self.store.latest_source(self.log)["offset"], before)

    def test_cli_follows_new_appends(self):
        config = self.root / "config.toml"
        config.write_text("log_path = '中文 messages.jsonl'\ndatabase_path = 'messages.sqlite3'\npoll_interval = 0.05\n", encoding="utf-8")
        process = subprocess.Popen([sys.executable, "-X", "utf8", "-m", "wechat_receiver", "follow",
            "--config", str(config), "--duration", "1.5"], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        try:
            deadline = time.monotonic() + 3
            while not self.store.latest_source(self.log) and process.poll() is None and time.monotonic() < deadline:
                time.sleep(0.02)
            if process.poll() is not None:
                output, errors = process.communicate()
                self.fail(f"Receiver exited early ({process.returncode}): {errors.decode(errors='replace')}")
            self.assertIsNotNone(self.store.latest_source(self.log))
            self.append(event())
            output, errors = process.communicate(timeout=5)
            self.assertEqual(process.returncode, 0, errors.decode(errors="replace"))
            self.assertEqual(self.count("messages"), 1)
            model = json.loads(self.store.db.execute("SELECT model_json FROM messages").fetchone()[0])
            self.assertEqual(model["history_state"], "unknown")
        finally:
            if process.poll() is None:
                process.kill()
            process.communicate()

    def test_schema_one_upgrade_preserves_messages_and_checkpoint(self):
        self.append(event())
        self.receiver.replay()
        offset = self.store.latest_source(self.log)["offset"]
        self.receiver.close()
        self.store.db.executescript("DROP TABLE send_evidence; DROP TABLE send_attempts; DROP TABLE outbox; PRAGMA user_version=1;")
        self.store.close()
        self.store = Store(self.path)
        self.receiver = Receiver(self.config, self.store)
        self.assertEqual(self.store.db.execute("PRAGMA user_version").fetchone()[0], 3)
        self.assertEqual(self.count("messages"), 1)
        self.assertEqual(self.store.latest_source(self.log)["offset"], offset)
        self.assertEqual(self.count("outbox"), 0)


if __name__ == "__main__":
    unittest.main()
