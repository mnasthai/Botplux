from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from wechat_receiver.config import Config
from wechat_receiver.normalize import normalize
from wechat_receiver.service import Receiver
from wechat_receiver.store import Store


def item(seq: int = 1, content: str = "你好🙂") -> bytes:
    record = {
        "kind": "item",
        "schema_version": 2,
        "session_id": "session",
        "seq": seq,
        "msg_type": 1,
        "from": "friend",
        "to": "self",
        "content": content,
        "content_read": {"status": "ok"},
        "from_read": {"status": "ok"},
        "to_read": {"status": "ok"},
    }
    return (json.dumps(record, ensure_ascii=False) + "\n").encode("utf-8")


class IngestionBatchTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.log = self.root / "observer.jsonl"
        self.log.write_bytes(b"")
        self.database = self.root / "messages.sqlite3"
        self.store = Store(self.database)
        self.receiver = Receiver(Config(self.log, self.database, self_id="self"), self.store)

    def tearDown(self) -> None:
        self.receiver.close()
        self.store.close()
        self.temporary.cleanup()

    def append(self, raw: bytes) -> None:
        with self.log.open("ab") as stream:
            stream.write(raw)

    def test_poll_batch_returns_only_committed_valid_events(self) -> None:
        control = (json.dumps({
            "kind": "native_send_result",
            "schema_version": 2,
            "session_id": "session",
            "request_id": "request",
            "status": "accepted",
        }) + "\n").encode("utf-8")
        self.append(item() + b"not-json\n" + control)
        batch = self.receiver.poll_batch()
        self.assertEqual(3, batch.records)
        self.assertEqual(2, len(batch.events))
        message_event, control_event = batch.events
        self.assertEqual("item", message_event.record["kind"])
        self.assertEqual("session", message_event.session_id)
        self.assertIsNotNone(message_event.message)
        self.assertEqual("backlog", message_event.message.history_state)
        self.assertEqual("native_send_result", control_event.record["kind"])
        self.assertIsNone(control_event.message)
        stored_ids = {row[0] for row in self.store.db.execute("SELECT id FROM raw_events")}
        self.assertTrue({message_event.raw_event_id, control_event.raw_event_id} <= stored_ids)

    def test_message_is_normalized_once_and_duplicate_is_not_published(self) -> None:
        self.append(item())
        with patch("wechat_receiver.store.normalize", wraps=normalize) as normalize_once:
            first = self.receiver.poll_batch()
        self.assertEqual(1, normalize_once.call_count)
        self.assertEqual(1, len(first.events))

        self.append(item())
        with patch("wechat_receiver.store.normalize", wraps=normalize) as normalize_duplicate:
            duplicate = self.receiver.poll_batch()
        self.assertEqual(1, duplicate.records)
        self.assertEqual((), duplicate.events)
        self.assertEqual(0, normalize_duplicate.call_count)

    def test_failed_transaction_returns_no_batch_and_keeps_checkpoint(self) -> None:
        self.append(item())
        with patch("wechat_receiver.store.normalize", side_effect=RuntimeError("injected failure")):
            with self.assertRaisesRegex(RuntimeError, "injected failure"):
                self.receiver.poll_batch()
        self.assertEqual(0, self.store.db.execute("SELECT count(*) FROM raw_events").fetchone()[0])
        self.assertEqual(0, self.store.latest_source(self.log)["offset"])

        self.receiver.close()
        self.receiver = Receiver(Config(self.log, self.database, self_id="self"), self.store)
        recovered = self.receiver.poll_batch()
        self.assertEqual(1, recovered.records)
        self.assertEqual(1, len(recovered.events))


if __name__ == "__main__":
    unittest.main()
