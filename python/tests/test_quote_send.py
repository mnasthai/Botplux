from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from wechat_receiver.backend import MessageEvent, WeChatBackend
from wechat_receiver.models import Message
from wechat_receiver.outbox import Outbox
from wechat_receiver.quote import QuoteReference
from wechat_receiver.send_models import SendTextCommand
from wechat_receiver.send_service import SenderConfig
from wechat_receiver.sender import HelloCapabilities, Sender


NOW = datetime(2026, 9, 18, 6, tzinfo=timezone.utc)
GROUP = "22913213991@chatroom"
MESSAGE_ID = "18446744073709551614"


def quote(*, conversation_id: str = GROUP) -> QuoteReference:
    return QuoteReference(
        message_id=MESSAGE_ID,
        from_id=conversation_id,
        to_id="wxid_self",
        sender_id="wxid_member",
        conversation_id=conversation_id,
        text="被引用正文🙂",
        timestamp=1_789_642_395,
        msg_source="<msgsource />",
    )


def command(request_id: str = "quote", *, with_mention: bool = False) -> SendTextCommand:
    return SendTextCommand(
        request_id, "account", "session", GROUP, "回复正文", NOW, NOW + timedelta(minutes=5),
        at_user_list="wxid_member,778" if with_mention else "", quote=quote(),
    )


def message(*, session: str = "session", message_id: str | None = MESSAGE_ID,
            content_status: str = "ok") -> Message:
    return Message(
        session_id=session,
        event_key=f"{session}:1",
        seq=1,
        call_id=1,
        source="receive_batch",
        event_kind="item",
        observed_at_ms=1_789_642_395_000,
        message_type=1,
        message_kind="text",
        app_message_type=None,
        content="被引用正文🙂",
        raw_content="wxid_member:\n被引用正文🙂",
        conversation_id=GROUP,
        sender_id="wxid_member",
        direction="incoming",
        message_time_candidate=1_789_642_395,
        message_id_candidate=message_id,
        mentioned_ids=(),
        mention_state="none",
        history_state="live_candidate",
        read_status={"content": content_status},
    )


def hello(request: dict, *, quote_capability: bool | None) -> dict:
    response = {
        "op": "hello",
        "protocol_version": 1,
        "request_id": request["request_id"],
        "observer_session_id": "session",
        "target_version": "4.1.13.12",
        "mode": "send_enabled",
        "account_id": "account",
        "account_verified": True,
        "send_text": True,
        "max_text_bytes": 16384,
        "send_group_text": True,
        "send_mention": True,
    }
    if quote_capability is not None:
        response["send_quote"] = quote_capability
    return response


class Transport:
    def __init__(self, quote_capability: bool | None):
        self.quote_capability = quote_capability
        self.calls: list[dict] = []

    def exchange(self, request: dict) -> dict:
        self.calls.append(request)
        if request["op"] == "hello_media":
            return {"op": "error", "protocol_version": 1, "request_id": request["request_id"],
                    "error_code": "unsupported_operation", "error_detail": "legacy endpoint"}
        if request["op"] == "hello":
            return hello(request, quote_capability=self.quote_capability)
        return {
            "op": "send_result",
            "protocol_version": 1,
            "request_id": request["request_id"],
            "attempt_id": request["attempt_id"],
            "observer_session_id": "session",
            "status": "accepted",
            "error_code": None,
            "error_detail": None,
        }


class QuoteReferenceTests(unittest.TestCase):
    def test_validation_dict_restore_and_target_binding(self) -> None:
        value = quote()
        self.assertEqual(QuoteReference.from_dict(value.to_dict()), value)
        self.assertEqual(SendTextCommand(**command().to_dict()).quote, value)
        with self.assertRaisesRegex(ValueError, "documented fields"):
            QuoteReference.from_dict(value.to_dict() | {"future": True})
        for bad_id in ("", "0", "01", str(1 << 64), "-1", "1.0"):
            with self.subTest(message_id=bad_id), self.assertRaises(ValueError):
                replace(value, message_id=bad_id)
        with self.assertRaises(ValueError):
            replace(value, timestamp=1 << 32)
        with self.assertRaises(ValueError):
            replace(value, sender_id=GROUP)
        with self.assertRaises(ValueError):
            replace(value, text="x" * (16 * 1024 + 1))
        with self.assertRaises(ValueError):
            replace(value, msg_source="x" * (8 * 1024 + 1))
        with self.assertRaisesRegex(ValueError, "match target_id"):
            SendTextCommand("bad-target", "account", "session", "other", "reply", NOW,
                            NOW + timedelta(minutes=5), quote=value)

    def test_from_message_rejects_missing_id_truncation_and_incomplete_identity(self) -> None:
        value = QuoteReference.from_message(message(), self_account_id="wxid_self")
        self.assertEqual((value.from_id, value.to_id, value.sender_id, value.conversation_id),
                         (GROUP, "wxid_self", "wxid_member", GROUP))
        with self.assertRaisesRegex(ValueError, "message ID"):
            QuoteReference.from_message(message(message_id=None), self_account_id="wxid_self")
        with self.assertRaisesRegex(ValueError, "complete incoming"):
            QuoteReference.from_message(message(content_status="truncated"),
                                        self_account_id="wxid_self")
        with self.assertRaisesRegex(ValueError, "complete incoming"):
            QuoteReference.from_message(replace(message(), direction="self_sync"),
                                        self_account_id="wxid_self")


class QuoteStorageAndDispatchTests(unittest.TestCase):
    def setUp(self) -> None:
        self.db = sqlite3.connect(":memory:")
        self.queue = Outbox(self.db)

    def tearDown(self) -> None:
        self.db.close()

    def sender(self, transport: Transport) -> Sender:
        return Sender(self.queue, transport, expected_account_id="account",
                      observer_session_id="session", allowed_targets={GROUP, "friend"})

    def test_quote_metadata_is_hashed_and_tampering_is_rejected(self) -> None:
        stored = self.queue.enqueue(command(), now=NOW)
        metadata = json.loads(stored["extra_json"])
        self.assertEqual(metadata, {"quote": quote().to_dict()})
        self.assertEqual(json.loads(stored["command_json"])["quote"], quote().to_dict())
        metadata["quote"]["text"] = "tampered"
        with self.db:
            self.db.execute("UPDATE outbox SET extra_json=? WHERE request_id='quote'",
                            (json.dumps(metadata, ensure_ascii=True, sort_keys=True,
                                        separators=(",", ":")),))
        with self.assertRaisesRegex(ValueError, "inconsistent"):
            self.queue.get("quote")

    def test_old_hello_keeps_quote_queued_and_sends_later_plain(self) -> None:
        self.queue.enqueue(command("a-quote"), now=NOW)
        plain = SendTextCommand("b-plain", "account", "session", "friend", "plain", NOW,
                                NOW + timedelta(minutes=5))
        self.queue.enqueue(plain, now=NOW)
        transport = Transport(quote_capability=None)
        report = self.sender(transport).dispatch_once(now=NOW)
        self.assertEqual((report.request_id, report.status), ("b-plain", "accepted"))
        self.assertEqual(self.queue.get("a-quote")["status"], "queued")
        self.assertEqual(self.queue.get("a-quote")["attempts"], [])
        blocked = self.sender(transport).dispatch_once(request_id="a-quote", now=NOW)
        self.assertEqual((blocked.status, blocked.error_code),
                         ("blocked", "native_quote_sender_unavailable"))
        self.assertEqual(self.queue.get("a-quote")["attempts"], [])

    def test_new_hello_flattens_quote_and_mention_without_losing_uint64(self) -> None:
        value = command(with_mention=True)
        self.queue.enqueue(value, now=NOW)
        transport = Transport(quote_capability=True)
        self.assertEqual(self.sender(transport).dispatch_once(now=NOW).status, "accepted")
        wire = transport.calls[-1]
        base_keys = set(value.to_dict()) - {"quote"}
        quote_keys = {f"quote_{key}" for key in quote().to_dict()}
        self.assertEqual(set(wire), base_keys | quote_keys | {"op", "attempt_id"})
        self.assertEqual(wire["op"], "send_rich_text")
        self.assertNotIn("quote", wire)
        self.assertEqual(wire["at_user_list"], "wxid_member,778")
        self.assertEqual(wire["quote_message_id"], MESSAGE_ID)
        self.assertIsInstance(wire["quote_message_id"], str)
        self.assertEqual(wire["quote_timestamp"], 1_789_642_395)
        self.assertIs(type(wire["quote_timestamp"]), int)
        self.assertEqual(wire["quote_message_type"], 1)
        self.assertIs(type(wire["quote_message_type"]), int)

    def test_high_escape_rich_frame_is_rejected_before_claim(self) -> None:
        high_escape_quote = replace(
            quote(), text="\n" * (16 * 1024), msg_source="\\" * (8 * 1024))
        value = SendTextCommand(
            "oversized-frame", "account", "session", GROUP, "\\" * (16 * 1024), NOW,
            NOW + timedelta(minutes=5), quote=high_escape_quote)
        self.queue.enqueue(value, now=NOW)
        transport = Transport(quote_capability=True)

        report = self.sender(transport).dispatch_once(now=NOW)

        self.assertEqual((report.status, report.error_code),
                         ("rejected", "frame_too_large"))
        stored = self.queue.get(value.request_id)
        self.assertEqual(stored["status"], "rejected")
        self.assertEqual(stored["attempts"], [])
        self.assertEqual([call["op"] for call in transport.calls], ["hello_media", "hello"])


class QuotePublicReplyTests(unittest.TestCase):
    def test_reply_quote_binds_target_and_current_observer_session(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            log = root / "observer.jsonl"
            log.write_text(
                json.dumps({"kind": "observer_start", "session_id": "session",
                            "target_version": "4.1.13.12"}) + "\n" +
                json.dumps({"kind": "command_pipe_ready", "session_id": "session",
                            "pipe": r"\\.\pipe\wechatbot-session"}) + "\n", encoding="utf-8")
            config = SenderConfig(root / "messages.sqlite3", log, "wxid_self", frozenset({GROUP}))
            backend = WeChatBackend(config)
            event = MessageEvent("wxid_self", message())
            receipt = asyncio.run(backend.reply(event, "引用回复", quote=True, request_id="reply"))
            self.assertEqual(receipt.status, "queued")
            changed_event = MessageEvent(
                "wxid_self", replace(event.capture, content="另一条被引用正文"))
            with self.assertRaisesRegex(ValueError, "existing content"):
                asyncio.run(backend.reply(
                    changed_event, "引用回复", quote=True, request_id="reply"))
            db = sqlite3.connect(config.database_path)
            try:
                row = Outbox(db).get("reply")
                self.assertEqual(json.loads(row["command_json"])["quote"]["message_id"], MESSAGE_ID)
            finally:
                db.close()
            old_event = MessageEvent("wxid_self", message(session="old-session"))
            with self.assertRaisesRegex(ValueError, "inactive observer session"):
                asyncio.run(backend.reply(old_event, "old", quote=True, request_id="old"))

            capability = HelloCapabilities("session", "4.1.13.12", "send_enabled", "wxid_self",
                                           True, True, 16384, True, True, True)
            endpoint = type("Endpoint", (), {"session_id": "session"})()
            backend._on_state(endpoint, capability, None)
            self.assertTrue(backend.connection_state.send_quote_ready)


if __name__ == "__main__":
    unittest.main()
