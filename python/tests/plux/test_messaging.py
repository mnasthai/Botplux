from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from plux.adapters.sqlite import SqliteDatabase
from plux.adapters.wechat_observer import ObserverAdapter, parse_message
from plux.api.models import AssetRef, ConnectionSnapshot, HistoryQuery, MemberRef, MessageRef, ReplyIntent, TextMessage, UnknownMessage
from plux.messaging import MessageStore
from plux.adapters.wechat_observer.transport import TransportError


def event(kind="item", **values):
    base = {"schema_version": 2, "session_id": "s1", "kind": kind}
    base.update(values)
    return (json.dumps(base, ensure_ascii=False) + "\r\n").encode("utf-8")


def text_record(seq=1, content="hello"):
    return event(seq=seq, observed_unix_ms=1_700_000_000_000, msg_type=1,
                 **{"from": "friend", "to": "self", "content": content,
                    "msg_source": "<msgsource><atuserlist>self</atuserlist></msgsource>",
                    "from_read": {"status": "ok"}, "to_read": {"status": "ok"},
                    "content_read": {"status": "ok"}, "source_read": {"status": "ok"},
                    "raw_fields": {"9": "1700000000", "12": "123456"}})


class Assets:
    def retain(self, ref, reference, uow):
        pass


PNG_BYTES = (b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06"
             b"\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00\x00\x00\x02\x00\x01")
SILK_BYTES = b"#!SILK_V3\x02\x00\xff\xff"


class FakeAdapter(ObserverAdapter):
    def __init__(self):
        super().__init__(expected_account="self", expected_session="s1")
        self.mode = "read_only"
        self.capabilities = frozenset()
        self.responses = []
        self.sent = []

    def probe(self):
        can = self.mode == "send_enabled"
        self._snapshot = ConnectionSnapshot(
            account="self", native_session=self.expected_session,
            sampled_at=datetime.now(timezone.utc), source="test",
            phase="ready" if can else "read_only",
            capabilities=self.capabilities, can_send=can)
        return self._snapshot

    def exchange(self, request):
        self.sent.append(request)
        if self.responses:
            response = self.responses.pop(0)
            if isinstance(response, Exception):
                raise response
            return response
        return {"op": "send_result", "protocol_version": 1,
                "request_id": request["request_id"], "attempt_id": request["attempt_id"],
                "observer_session_id": request["observer_session_id"],
                "status": "accepted", "error_code": None, "error_detail": None}


class MessagingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = SqliteDatabase(Path(self.temp.name) / "messages.sqlite3")
        self.adapter = FakeAdapter()
        self.store = MessageStore(self.db, self.adapter, Assets(), "self",
                                  {"friend", "room@chatroom"},
                                  media_root=self.temp.name).initialize()

    def tearDown(self):
        self.db.close()
        self.temp.cleanup()

    def ingest(self, raw):
        with self.db.transaction() as uow:
            row = uow.execute("SELECT offset FROM plux_sources WHERE source='log'").fetchone()
            offset = row[0] if row else 0
        return self.store.ingest(raw, "log", offset, offset + len(raw))

    def test_atomic_ingest_inbox_history_and_control(self):
        self.assertIsNone(self.ingest(event("command_pipe_error", win32_error=5)))
        message = self.ingest(text_record())
        self.assertIsInstance(message, TextMessage)
        self.assertEqual(message.quality.history_status, "unknown")
        self.assertEqual(message.quality.mentions_status, "explicit_self")
        self.assertEqual([m.event_key for m in self.store.pending_inputs()], ["s1:1"])
        history = self.store.for_plugin("tool").history(HistoryQuery("self", "friend"))
        self.assertEqual(history.items[0].text, "hello")
        with self.db.transaction() as uow:
            self.store.complete_input("s1:1", uow)
        self.assertEqual(self.store.pending_inputs(), ())
        self.ingest(text_record())
        with self.db.transaction() as uow:
            self.assertEqual(uow.execute("SELECT COUNT(*) FROM plux_raw_events").fetchone()[0], 3)
            self.assertEqual(uow.execute("SELECT COUNT(*) FROM plux_messages").fetchone()[0], 1)

    def test_truncated_content_remains_unknown_in_durable_inbox(self):
        raw = event(seq=2, observed_unix_ms=1_700_000_000_000, msg_type=1,
                    **{"from": "friend", "to": "self", "content": "partial",
                       "from_read": {"status": "ok"}, "to_read": {"status": "ok"},
                       "content_read": {"status": "truncated"}})
        message = self.ingest(raw)
        self.assertIsInstance(message, UnknownMessage)
        self.assertEqual(message.quality.status, "uncertain")
        self.assertEqual(self.store.pending_inputs(), (message,))
        # Runtime sees the explicit quality marker and declines command routing.

    def test_reply_transaction_rollback_and_capability_gate(self):
        self.ingest(text_record())
        service = self.store.for_plugin("tool")
        intent = ReplyIntent("reply-1", "self", "friend", "s1", text="response")
        with self.assertRaises(RuntimeError):
            with self.db.transaction() as uow:
                self.store.complete_input("s1:1", uow)
                service.enqueue(intent, uow)
                raise RuntimeError("business rollback")
        self.assertEqual(len(self.store.pending_inputs()), 1)
        with self.db.transaction() as uow:
            request = service.enqueue(intent, uow)
        report = self.store.dispatch_once(request.request_id)
        self.assertEqual(report.status, "blocked")
        self.assertEqual(service.receipt(request.request_id).attempts, ())
        self.adapter.mode = "send_enabled"
        self.adapter.capabilities = frozenset({"send_text"})
        report = self.store.dispatch_once(request.request_id)
        self.assertEqual(report.status, "accepted")
        self.assertEqual(len(service.receipt(request.request_id).attempts), 1)
        self.assertEqual(self.adapter.sent[0]["origin"], "game")

    def test_unknown_response_is_never_replayed(self):
        service = self.store.for_plugin("tool")
        with self.db.transaction() as uow:
            request = service.enqueue(ReplyIntent("reply-2", "self", "friend", "s1", text="x"), uow)
        self.adapter.mode = "send_enabled"
        self.adapter.capabilities = frozenset({"send_text"})
        self.adapter.responses.append(TransportError("timeout", "lost result", stage="response", may_have_written=True))
        self.assertEqual(self.store.dispatch_once(request.request_id).status, "unknown")
        self.assertEqual(self.store.dispatch_once(request.request_id).status, "idle")
        self.assertEqual(len(self.adapter.sent), 1)
        self.assertEqual(self.store.recover(), 0)

    def test_stale_session_and_member_directory(self):
        service = self.store.for_plugin("tool")
        with self.db.transaction() as uow:
            request = service.enqueue(ReplyIntent("reply-3", "self", "friend", "s1", text="x"), uow)
        self.adapter.expected_session = "s2"
        self.adapter.mode = "send_enabled"
        self.adapter.capabilities = frozenset({"send_text"})
        self.assertEqual(self.store.dispatch_once(request.request_id).status, "blocked")
        self.assertEqual(service.receipt(request.request_id).attempts, ())
        ref = MemberRef("self", "friend", "room@chatroom")
        self.store.upsert_member(ref, display_name="Friend", sampled_at=datetime.now(timezone.utc), source="test")
        self.assertEqual(service.member(ref).display_name, "Friend")

    def test_disabled_adapter_has_no_sender(self):
        adapter = ObserverAdapter(enabled=False)
        self.assertFalse(adapter.probe().can_send)
        self.assertEqual(adapter.connection().phase, "disconnected")


    def test_poll_log_preserves_unknown_history_and_local_ingestion_status(self):
        log = Path(self.temp.name) / "observer.jsonl"
        log.write_bytes(text_record())
        self.assertEqual(self.store.poll_log(log), 1)
        first = self.store.for_plugin("tool").get("s1:1")
        self.assertEqual(first.quality.history_status, "unknown")
        self.assertEqual(first.quality.ingestion_status, "backlog")
        with log.open("ab") as stream:
            stream.write(text_record(2, "live"))
        self.assertEqual(self.store.poll_log(log), 1)
        second = self.store.for_plugin("tool").get("s1:2")
        self.assertEqual(second.quality.history_status, "unknown")
        self.assertEqual(second.quality.ingestion_status, "new")
        self.assertEqual(self.store.poll_log(log), 0)

    def test_media_event_associates_by_session_and_candidate_message_id(self):
        image = event(seq=10, observed_unix_ms=1_700_000_000_000, msg_type=3,
                      **{"from": "friend", "to": "self", "content": "<img/>",
                         "from_read": {"status": "ok"}, "to_read": {"status": "ok"},
                         "content_read": {"status": "ok"}, "raw_fields": {"12": "998"}})
        message = self.ingest(image)
        self.assertEqual(message.media_key, "s1:msgid:998")
        self.ingest(event("media_asset", seq=11, message_id="998", sha256="a" * 64,
                          asset_name="asset.png", byte_length=10, status="available"))
        media = self.store.for_plugin("tool").media(message.media_key)
        self.assertEqual(media["asset_name"], "asset.png")

    def test_quote_and_mention_require_capability_then_preserve_wire_fields(self):
        self.ingest(text_record())
        service = self.store.for_plugin("tool")
        mention = MemberRef("self", "friend", "room@chatroom")
        # Group quote needs a group-origin message; private quote exercises the quote path.
        with self.db.transaction() as uow:
            request = service.enqueue(ReplyIntent(
                "quoted", "self", "friend", "s1", text="reply",
                quote=MessageRef("s1:1")), uow)
        self.adapter.mode = "send_enabled"
        self.adapter.capabilities = frozenset({"send_text"})
        self.assertEqual(self.store.dispatch_once(request.request_id).error_code,
                         "native_quote_sender_unavailable")
        self.assertEqual(service.receipt(request.request_id).attempts, ())
        self.adapter.capabilities = frozenset({"send_text", "send_quote"})
        self.assertEqual(self.store.dispatch_once(request.request_id).status, "accepted")
        payload = self.adapter.sent[-1]
        self.assertEqual(payload["op"], "send_rich_text")
        self.assertEqual(payload["quote_message_id"], "123456")
        self.assertEqual(payload["quote_text"], "hello")
        self.assertNotIn("quote", payload)

    def test_image_and_voice_payloads_are_bound_to_content(self):
        class FileAssets(Assets):
            def __init__(self, path):
                self.path = path
            def resolve(self, ref):
                return self.path
        asset_path = Path(self.temp.name) / "sample.bin"
        self.store.assets = FileAssets(asset_path)
        self.adapter.mode = "send_enabled"
        self.adapter.capabilities = frozenset({"send_image", "send_voice"})
        service = self.store.for_plugin("tool")
        for kind, duration, content in (("image", 0, PNG_BYTES), ("voice", 2000, SILK_BYTES)):
            asset_path.write_bytes(content)
            digest = hashlib.sha256(content).hexdigest()
            with self.db.transaction() as uow:
                request = service.enqueue(ReplyIntent(
                    kind, "self", "friend", "s1",
                    asset=AssetRef(kind, "1", kind, digest), duration_ms=duration), uow)
            self.assertEqual(self.store.dispatch_once(request.request_id).status, "accepted")
            payload = self.adapter.sent[-1]
            self.assertEqual(payload["op"], "send_media")
            self.assertEqual(payload["media_kind"], kind)
            self.assertEqual(payload["media_sha256"], digest)
            self.assertEqual(payload["duration_ms"], duration)
            # The payload must point at the flat <sha256>.<ext> native layout.
            media = Path(payload["media_path"])
            self.assertEqual(media.parent, Path(self.temp.name).resolve())
            self.assertEqual(media.stem, digest)
            self.assertEqual(media.suffix, ".png" if kind == "image" else ".silk")

    def test_late_old_session_evidence_does_not_change_request_state(self):
        service = self.store.for_plugin("tool")
        with self.db.transaction() as uow:
            request = service.enqueue(ReplyIntent(
                "evidence", "self", "friend", "s1", text="x"), uow)
        self.ingest(event("native_send_request", session_id="old-session", seq=33,
                          request_id=request.request_id, status="accepted"))
        receipt = service.receipt(request.request_id)
        self.assertEqual(receipt.status, "queued")
        self.assertEqual(receipt.evidence[0]["session_id"], "old-session")


if __name__ == "__main__":
    unittest.main()