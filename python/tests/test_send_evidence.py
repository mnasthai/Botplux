from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from wechat_receiver.outbox import Outbox
from wechat_receiver.send_evidence import EvidenceCollector
from wechat_receiver.send_models import SendTextCommand


UTC = timezone.utc
START = datetime(2026, 9, 18, 1, tzinfo=UTC)
MS = int(START.timestamp() * 1000)


def command(request_id: str, *, target="friend", text="hello"):
    return SendTextCommand(request_id=request_id, expected_account_id="account",
        observer_session_id="session", target_id=target, text=text, created_at=START,
        expires_at=START + timedelta(minutes=5), origin="manual")


def line(**values) -> bytes:
    return (json.dumps(values, ensure_ascii=False, separators=(",", ":")) + "\n").encode()


class SendEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.log = root / "observer.jsonl"
        self.log.write_bytes(b"")
        self.db = sqlite3.connect(root / "receiver.sqlite3")
        self.outbox = Outbox(self.db)

    def tearDown(self):
        self.db.close()
        self.temp.cleanup()

    def claim(self, request_id: str, **changes):
        self.outbox.enqueue(command(request_id, **changes), now=START)
        return self.outbox.claim_next("account", "session", request_id=request_id, now=START)

    def append(self, data: bytes):
        with self.log.open("ab") as stream:
            stream.write(data)

    def test_partial_out_of_order_uuid_and_restart_are_idempotent(self):
        claim = self.claim("r1")
        attempt = claim["attempt_id"]
        context = line(kind="send_context_enter", session_id="session", seq=7,
                       observed_unix_ms=MS + 10, local_uuid="uuid-1")
        native = line(kind="native_send_result", session_id="session", request_id="r1",
                      attempt_id=attempt, status="accepted", observed_unix_ms=MS + 20,
                      local_uuid="uuid-1", start_entered=True, start_returned=True,
                      cleanup_complete=True)
        split = len(native) // 2
        self.append(context + native[:split])
        collector = EvidenceCollector(self.outbox, self.log)
        first = collector.poll()
        self.assertEqual(first["evidence_added"], 0)
        self.assertEqual(first["pending"], 1)

        self.append(native[split:])
        second = collector.poll()
        self.assertEqual(second["evidence_added"], 2)
        stored = self.outbox.get("r1")["evidence"]
        self.assertEqual([item["evidence_type"] for item in stored],
                         ["native_send_result", "send_context_enter"])
        self.assertTrue(all(item["attempt_id"] == attempt for item in stored))

        restarted = EvidenceCollector(self.outbox, self.log)
        third = restarted.poll()
        self.assertEqual(third["evidence_added"], 0)
        self.assertEqual(len(self.outbox.get("r1")["evidence"]), 2)

    def test_legacy_native_requires_one_active_attempt_and_same_session(self):
        claim = self.claim("legacy")
        self.append(line(kind="native_send_result", session_id="other", request_id="legacy",
                         status="accepted", observed_unix_ms=MS, local_uuid="uuid-x"))
        collector = EvidenceCollector(self.outbox, self.log)
        result = collector.poll()
        self.assertEqual(result["evidence_added"], 0)
        self.assertEqual(self.outbox.get("legacy")["evidence"], [])

        self.append(line(kind="native_send_result", session_id="session", request_id="legacy",
                         status="accepted", observed_unix_ms=MS + 1, local_uuid="uuid-x"))
        result = collector.poll()
        self.assertEqual(result["evidence_added"], 1)
        self.assertEqual(self.outbox.get("legacy")["evidence"][0]["attempt_id"], claim["attempt_id"])

    def test_ambiguous_outbound_is_never_bound(self):
        first = self.claim("r1", target="same", text="same text")
        second = self.claim("r2", target="same", text="same text")
        records = b"".join([
            line(kind="native_send_result", session_id="session", request_id="r1",
                 attempt_id=first["attempt_id"], status="accepted", observed_unix_ms=MS,
                 local_uuid="uuid-1"),
            line(kind="native_send_result", session_id="session", request_id="r2",
                 attempt_id=second["attempt_id"], status="accepted", observed_unix_ms=MS + 2,
                 local_uuid="uuid-2"),
            line(kind="outbound_item", session_id="session", seq=99,
                 observed_unix_ms=MS + 3, to="same", content="same text"),
        ])
        self.append(records)
        result = EvidenceCollector(self.outbox, self.log).poll()
        self.assertEqual(result["evidence_added"], 2)
        evidence = self.outbox.get("r1")["evidence"] + self.outbox.get("r2")["evidence"]
        self.assertFalse(any(item["evidence_type"] == "outbound_observed_candidate" for item in evidence))
        self.assertEqual(result["pending"], 1)

    def test_missing_invalid_and_truncated_file_do_not_stall(self):
        collector = EvidenceCollector(self.outbox, self.log)
        self.log.unlink()
        self.assertEqual(collector.poll()["missing"], 1)
        self.log.write_bytes(b"not-json\n")
        first = collector.poll()
        self.assertEqual(first["skipped"], 1)
        self.log.write_bytes(line(kind="outbound_item", session_id="session", seq=1,
                                  observed_unix_ms=MS, to="nobody", content="none"))
        second = collector.poll()
        self.assertEqual(second["resets"], 1)
        self.assertEqual(second["records"], 1)


if __name__ == "__main__":
    unittest.main()
