from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
import sqlite3
from types import SimpleNamespace
import tempfile
import unittest

from wechat_receiver.backend_profile import CURRENT_PROFILE
from wechat_receiver.models import Message
from wechat_receiver.outbox import Outbox
from wechat_receiver.reply_router import ReplyRouter
from wechat_receiver.send_models import SendTextCommand
from wechat_receiver.send_service import SenderConfig
from wechat_receiver.sender import Sender, SenderError
from wechat_receiver.store import Store
from wechat_receiver.target import is_group_target, is_valid_target


NOW = datetime(2026, 9, 18, 4, tzinfo=timezone.utc)


def command(request_id: str, target: str) -> SendTextCommand:
    return SendTextCommand(request_id, "account", "session", target, "群聊正文🙂", NOW,
                           NOW + timedelta(minutes=5))


def hello(request: dict, *, group: bool | None) -> dict:
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
    }
    if group is not None:
        response["send_group_text"] = group
    return response


class Transport:
    def __init__(self, group: bool | None):
        self.group = group
        self.calls: list[dict] = []

    def exchange(self, request: dict) -> dict:
        self.calls.append(request)
        if request["op"] == "hello_media":
            return {"op": "error", "protocol_version": 1, "request_id": request["request_id"],
                    "error_code": "unsupported_operation", "error_detail": "legacy endpoint"}
        if request["op"] == "hello":
            return hello(request, group=self.group)
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


class GroupTargetTests(unittest.TestCase):
    def test_target_grammar_and_sender_config(self) -> None:
        valid = {
            "friend",
            "filehelper",
            "A_1-b",
            "123456@chatroom",
            "room_A-2@chatroom",
            "x" * 128,
            "9" * 128 + "@chatroom",
        }
        invalid = {
            "",
            " ",
            "a b",
            "a\n",
            "a;other",
            "foo@bar",
            "@chatroom",
            "a@chatroom@chatroom",
            "中文",
            "friend.",
            "x" * 129,
            "9" * 129 + "@chatroom",
        }
        self.assertTrue(all(is_valid_target(value) for value in valid))
        self.assertTrue(all(not is_valid_target(value) for value in invalid))
        self.assertTrue(is_group_target("123_room@chatroom"))
        self.assertFalse(is_group_target("friend"))
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = SenderConfig(root / "db.sqlite3", root / "log.jsonl", "account",
                                  frozenset({"friend", "123_room@chatroom"}))
            self.assertIn("123_room@chatroom", config.allowed_targets)
            for target in invalid:
                with self.subTest(target=target), self.assertRaises(ValueError):
                    SenderConfig(root / "db.sqlite3", root / "log.jsonl", "account",
                                 frozenset({target}))
        self.assertTrue(CURRENT_PROFILE.send_group_text)

    def test_old_hello_blocks_group_before_claim_but_does_not_block_private(self) -> None:
        db = sqlite3.connect(":memory:")
        try:
            outbox = Outbox(db)
            outbox.enqueue(command("a-group", "123@chatroom"), now=NOW)
            outbox.enqueue(command("b-private", "friend"), now=NOW)
            transport = Transport(group=None)  # Legacy hello has no group field.
            sender = Sender(outbox, transport, expected_account_id="account",
                            observer_session_id="session",
                            allowed_targets={"123@chatroom", "friend"})
            report = sender.dispatch_once(now=NOW)
            self.assertEqual(("b-private", "accepted"), (report.request_id, report.status))
            self.assertEqual("queued", outbox.get("a-group")["status"])
            self.assertEqual([], outbox.get("a-group")["attempts"])
            sent = [call for call in transport.calls if call["op"] == "send_text"]
            self.assertEqual(["friend"], [call["target_id"] for call in sent])

            blocked = sender.dispatch_once(request_id="a-group", now=NOW)
            self.assertEqual(("blocked", "native_group_sender_unavailable"),
                             (blocked.status, blocked.error_code))
            self.assertEqual([], outbox.get("a-group")["attempts"])
        finally:
            db.close()

    def test_new_hello_sends_group_body_unchanged_and_unknown_fields_stay_strict(self) -> None:
        db = sqlite3.connect(":memory:")
        try:
            outbox = Outbox(db)
            value = command("group", "room_A-2@chatroom")
            outbox.enqueue(value, now=NOW)
            transport = Transport(group=True)
            report = Sender(outbox, transport, expected_account_id="account",
                            observer_session_id="session",
                            allowed_targets={value.target_id}).dispatch_once(now=NOW)
            self.assertEqual("accepted", report.status)
            sent = [call for call in transport.calls if call["op"] == "send_text"]
            self.assertEqual(1, len(sent))
            self.assertEqual(value.target_id, sent[0]["target_id"])
            self.assertEqual(value.text, sent[0]["text"])

            def unexpected(request):
                response = hello(request, group=True)
                response["future_capability"] = True
                return response

            class Unexpected:
                exchange = staticmethod(unexpected)

            with self.assertRaisesRegex(SenderError, "unexpected fields"):
                Sender(outbox, Unexpected(), expected_account_id="account",
                       observer_session_id="session", allowed_targets={value.target_id}).probe()
        finally:
            db.close()

    def test_group_message_reply_keeps_original_group_target(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = Store(root / "receiver.sqlite3")
            try:
                group = "123_room@chatroom"
                sender_config = SimpleNamespace(
                    account_id="self",
                    allowed_targets=frozenset({group}),
                )
                config = SimpleNamespace(
                    sender=sender_config,
                    reply_ttl_seconds=60,
                    max_message_age_seconds=120,
                    enabled_plugins=("reply",),
                )

                class Plugin:
                    name = "reply"

                    @staticmethod
                    def reply(message):
                        return ("原群回复🙂",)

                now = NOW.timestamp()
                message = Message(
                    session_id="session", event_key="session:1", seq=1, call_id=1,
                    source="receive_batch", event_kind="item",
                    observed_at_ms=int(now * 1000), message_type=1, message_kind="text",
                    app_message_type=None, content="我喜欢你", raw_content="member:\n我喜欢你",
                    conversation_id=group, sender_id="member", direction="incoming",
                    message_time_candidate=int(now), message_id_candidate=None,
                    mentioned_ids=(), mention_state="none", history_state="live_candidate",
                )
                router = ReplyRouter(store, config, [Plugin()], started_at=now - 1)
                self.assertEqual(1, router.handle(message, "session", now=now))
                row = store.db.execute("SELECT target_id,text FROM outbox").fetchone()
                self.assertEqual((group, "原群回复🙂"), tuple(row))
            finally:
                store.close()


if __name__ == "__main__":
    unittest.main()
