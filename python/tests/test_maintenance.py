from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from wechat_receiver.maintenance import compact_storage, inspect_storage
from wechat_receiver.outbox import Outbox
from wechat_receiver.send_models import SendTextCommand
from wechat_receiver.sender import SenderLock
from wechat_receiver.service import DatabaseLock
from wechat_receiver.store import Store


NOW = datetime(2026, 9, 18, 3, tzinfo=timezone.utc)


def command(request_id: str) -> SendTextCommand:
    return SendTextCommand(
        request_id=request_id,
        expected_account_id="account",
        observer_session_id="session",
        target_id="friend",
        text="保留正文🙂",
        created_at=NOW,
        expires_at=NOW + timedelta(minutes=5),
    )


class MaintenanceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.path = Path(self.temporary.name) / "receiver.sqlite3"
        self.store = Store(self.path)
        self.outbox = Outbox(self.store.db)
        value = command("request")
        self.outbox.enqueue(value, now=NOW)
        claim = self.outbox.claim_next("account", "session", request_id="request", now=NOW)
        self.outbox.record_result("request", claim["attempt_id"], "accepted", now=NOW)
        self.outbox.add_evidence("request", "native_send_result", {"local_uuid": "uuid"},
                                 attempt_id=claim["attempt_id"], observed_at=NOW)
        with self.store.db:
            self.store.db.execute(
                "UPDATE outbox SET command_json=?,fingerprint=? WHERE request_id=?",
                (value.fingerprint(), value.fingerprint(), value.request_id),
            )
            self.store.db.execute("""CREATE TABLE plugin_runs (
                account_id TEXT NOT NULL,event_key TEXT NOT NULL,plugin TEXT NOT NULL,
                session_id TEXT NOT NULL,status TEXT NOT NULL,commands_json TEXT NOT NULL,
                error TEXT,PRIMARY KEY(account_id,event_key,plugin))""")
            ready = json.dumps([{"text": "必须保留的恢复意图"}], ensure_ascii=False)
            terminal = json.dumps([{"text": "已入outbox的旧副本"}], ensure_ascii=False)
            self.store.db.executemany("INSERT INTO plugin_runs VALUES(?,?,?,?,?,?,?)", [
                ("account", "event-ready", "p", "session", "ready", ready, None),
                ("account", "event-ignored", "p", "session", "ignored", terminal, None),
            ])
            source = self.store.db.execute("""INSERT INTO sources(path,identity,generation)
                VALUES(?,?,?)""", (str((self.path.parent / "log.jsonl").resolve()), "id", 1)).lastrowid
            self.store.db.execute("""INSERT INTO raw_events
                (source_id,start_offset,end_offset,raw,parse_status,session_id,kind,canonical_json)
                VALUES(?,?,?,?,?,?,?,?)""",
                (source, 0, 2, b"{}", "ok", "session", "observer_start", "{}"))

    def tearDown(self) -> None:
        self.store.close()
        self.temporary.cleanup()

    def test_inspect_is_read_only_and_reports_legacy_payload(self) -> None:
        before_rows = [tuple(row) for row in self.store.db.execute(
            "SELECT request_id,command_json,fingerprint,text,status FROM outbox")]
        before_plugins = [tuple(row) for row in self.store.db.execute(
            "SELECT event_key,status,commands_json FROM plugin_runs ORDER BY event_key")]
        report = inspect_storage(self.path)
        self.assertEqual(1, report["outbox"]["rows"])
        self.assertEqual(1, report["outbox"]["legacy_compactable_rows"])
        self.assertEqual(2, report["plugin_runs"]["rows"])
        self.assertEqual(1, report["plugin_runs"]["compactable_rows"])
        self.assertGreater(report["logical_payload_bytes"], 0)
        self.assertEqual(before_rows, [tuple(row) for row in self.store.db.execute(
            "SELECT request_id,command_json,fingerprint,text,status FROM outbox")])
        self.assertEqual(before_plugins, [tuple(row) for row in self.store.db.execute(
            "SELECT event_key,status,commands_json FROM plugin_runs ORDER BY event_key")])

    def test_compaction_preserves_ready_requests_evidence_events_and_is_idempotent(self) -> None:
        attempt_before = [tuple(row) for row in self.store.db.execute("SELECT * FROM send_attempts")]
        evidence_before = [tuple(row) for row in self.store.db.execute("SELECT * FROM send_evidence")]
        raw_before = [tuple(row) for row in self.store.db.execute("SELECT * FROM raw_events")]
        ready_before = self.store.db.execute(
            "SELECT commands_json FROM plugin_runs WHERE status='ready'").fetchone()[0]
        self.store.close()

        result = compact_storage(self.path)
        self.assertEqual(1, result["outbox_rows_compacted"])
        self.assertEqual(1, result["plugin_runs_rows_compacted"])
        self.assertEqual(2, result["modified_rows"])
        self.assertGreater(result["logical_payload_bytes_removed"], 0)

        self.store = Store(self.path)
        self.outbox = Outbox(self.store.db)
        public = self.outbox.get("request")
        self.assertEqual("保留正文🙂", public["text"])
        self.assertEqual("accepted", public["status"])
        self.assertEqual("", self.store.db.execute(
            "SELECT command_json FROM outbox WHERE request_id='request'").fetchone()[0])
        self.assertEqual(ready_before, self.store.db.execute(
            "SELECT commands_json FROM plugin_runs WHERE status='ready'").fetchone()[0])
        self.assertEqual("[]", self.store.db.execute(
            "SELECT commands_json FROM plugin_runs WHERE status='ignored'").fetchone()[0])
        self.assertEqual(attempt_before, [tuple(row) for row in self.store.db.execute("SELECT * FROM send_attempts")])
        self.assertEqual(evidence_before, [tuple(row) for row in self.store.db.execute("SELECT * FROM send_evidence")])
        self.assertEqual(raw_before, [tuple(row) for row in self.store.db.execute("SELECT * FROM raw_events")])
        self.store.close()
        second = compact_storage(self.path)
        self.assertEqual(0, second["modified_rows"])
        self.store = Store(self.path)
        self.outbox = Outbox(self.store.db)

    def test_missing_database_and_lock_contention_fail_without_changes(self) -> None:
        missing = self.path.parent / "missing.sqlite3"
        with self.assertRaises(FileNotFoundError):
            inspect_storage(missing)
        self.assertFalse(missing.exists())
        with self.assertRaises(FileNotFoundError):
            compact_storage(missing)
        self.assertFalse(missing.exists())

        self.store.close()
        with DatabaseLock(self.path):
            with self.assertRaisesRegex(RuntimeError, "Another receiver"):
                compact_storage(self.path)
        with SenderLock(self.path):
            with self.assertRaisesRegex(RuntimeError, "Another sender"):
                compact_storage(self.path)
        self.store = Store(self.path)
        self.outbox = Outbox(self.store.db)

    def test_database_without_plugin_runs_is_supported(self) -> None:
        other = self.path.parent / "without-plugins.sqlite3"
        store = Store(other)
        try:
            Outbox(store.db).enqueue(command("other"), now=NOW)
        finally:
            store.close()
        preview = inspect_storage(other)
        self.assertEqual({"rows": 0, "commands_json_bytes": 0, "compactable_rows": 0},
                         preview["plugin_runs"])
        result = compact_storage(other)
        self.assertEqual(0, result["plugin_runs_rows_compacted"])

    def test_read_only_inspection_supports_outbox_without_media_payload_column(self) -> None:
        legacy = self.path.parent / "legacy-outbox.sqlite3"
        db = sqlite3.connect(legacy)
        try:
            db.execute("""CREATE TABLE outbox(
                command_json TEXT NOT NULL,
                fingerprint TEXT NOT NULL,
                text TEXT NOT NULL
            )""")
            db.execute("INSERT INTO outbox VALUES(?,?,?)", ("legacy", "fingerprint", "body"))
            db.commit()
        finally:
            db.close()
        report = inspect_storage(legacy)
        self.assertEqual(report["outbox"]["payload_json_bytes"], 0)
        self.assertEqual(report["logical_payload_bytes"],
                         len(b"legacy") + len(b"fingerprint") + len(b"body"))


if __name__ == "__main__":
    unittest.main()
