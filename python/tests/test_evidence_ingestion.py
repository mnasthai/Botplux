from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from wechat_receiver.ingestion import IngestedEvent
from wechat_receiver.outbox import Outbox
from wechat_receiver.send_evidence import EvidenceCollector
from wechat_receiver.send_models import SendTextCommand
from wechat_receiver.store import Store


UTC = timezone.utc
NOW = datetime(2026, 9, 18, 2, tzinfo=UTC)
OBSERVED_MS = int(NOW.timestamp() * 1000)


class CommittedEvidenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.log = self.root / "observer.jsonl"
        self.store = Store(self.root / "receiver.sqlite3")
        self.outbox = Outbox(self.store.db)
        self.source = self.store.new_source(self.log, "test-file")
        self.offset = 0

    def tearDown(self) -> None:
        self.store.close()
        self.temporary.cleanup()

    def claim(self, request_id: str) -> dict:
        self.outbox.enqueue(SendTextCommand(
            request_id=request_id,
            expected_account_id="account",
            observer_session_id="session",
            target_id="friend",
            text="hello",
            created_at=NOW,
            expires_at=NOW + timedelta(minutes=5),
        ), now=NOW)
        return self.outbox.claim_next("account", "session", request_id=request_id, now=NOW)

    def insert(self, record: dict, *, canonical: str | None = None,
               duplicate_of: int | None = None) -> int:
        self.offset += 1
        encoded = canonical if canonical is not None else json.dumps(record, ensure_ascii=True)
        cursor = self.store.db.execute("""INSERT INTO raw_events
            (source_id,start_offset,end_offset,raw,parse_status,session_id,kind,seq,
             event_key,canonical_json,duplicate_of)
            VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
            (self.source["id"], self.offset - 1, self.offset, b"{}\n", "ok", "session",
             record.get("kind"), record.get("seq"), None, encoded, duplicate_of))
        return cursor.lastrowid

    @staticmethod
    def native(request_id: str, attempt_id: str, *, include_session: bool = True) -> dict:
        record = {
            "kind": "native_send_result",
            "request_id": request_id,
            "attempt_id": attempt_id,
            "status": "accepted",
            "observed_unix_ms": OBSERVED_MS,
            "local_uuid": f"uuid-{request_id}",
        }
        if include_session:
            record["session_id"] = "session"
        return record

    def test_direct_committed_event_uses_record_without_reading_log_or_canonical(self) -> None:
        attempt = self.claim("direct")["attempt_id"]
        record = self.native("direct", attempt)
        raw_id = self.insert(record, canonical="not valid stored JSON")
        self.store.db.commit()
        event = IngestedEvent(record, "session", None, raw_id)

        collector = EvidenceCollector(self.outbox, self.log)
        with patch("wechat_receiver.send_evidence.open_shared",
                   side_effect=AssertionError("committed ingestion must not tail the log")):
            counters = collector.consume_committed((event,))
        self.assertEqual(1, counters["records"])
        self.assertEqual(1, counters["evidence_added"])
        self.assertEqual(0, counters["recovered"])
        self.assertEqual(1, len(self.outbox.get("direct")["evidence"]))

    def test_crash_gap_is_recovered_in_bounded_order_without_parsing_chat_rows(self) -> None:
        attempt = self.claim("gap")["attempt_id"]
        with self.store.db:
            for index in range(512):
                self.insert({"kind": "item", "seq": index}, canonical="broken chat JSON")
            record = self.native("gap", attempt, include_session=False)
            evidence_id = self.insert(record)

        collector = EvidenceCollector(self.outbox, self.log)
        first = collector.consume_committed(())
        self.assertEqual(0, first["recovered"])
        self.assertEqual([], self.outbox.get("gap")["evidence"])
        progress = self.store.db.execute(
            "SELECT raw_event_id FROM evidence_collector_ingest_progress WHERE path=?",
            (str(self.log.resolve()),)).fetchone()[0]
        self.assertLess(progress, evidence_id)

        second = collector.consume_committed(())
        self.assertEqual(1, second["recovered"])
        self.assertEqual(1, second["evidence_added"])
        payload = json.loads(self.outbox.get("gap")["evidence"][0]["payload_json"])
        self.assertEqual("session", payload["session_id"])

    def test_restart_keeps_cursor_and_evidence_idempotent(self) -> None:
        attempt = self.claim("restart")["attempt_id"]
        record = self.native("restart", attempt)
        raw_id = self.insert(record)
        self.store.db.commit()
        event = IngestedEvent(record, "session", None, raw_id)

        first = EvidenceCollector(self.outbox, self.log).consume_committed((event,))
        self.assertEqual(1, first["evidence_added"])
        restarted = EvidenceCollector(self.outbox, self.log)
        second = restarted.consume_committed((event,))
        self.assertEqual(0, second["records"])
        self.assertEqual(0, second["evidence_added"])
        self.assertEqual(1, len(self.outbox.get("restart")["evidence"]))


if __name__ == "__main__":
    unittest.main()
