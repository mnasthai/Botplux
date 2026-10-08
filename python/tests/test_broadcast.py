"""Contract tests for the administrator-only group broadcast command."""

from __future__ import annotations

import json
from pathlib import Path
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from wechat_receiver.bot_service import BotConfig, load_bot_config
from wechat_receiver.models import Message
from wechat_receiver.outbox import Outbox
from wechat_receiver.plugins import GroupBroadcast, LoadedPlugin, Reply, load_plugins
from wechat_receiver.reply_router import ReplyRouter
from wechat_receiver.send_service import SenderConfig
from wechat_receiver.store import Store


class BroadcastTests(unittest.TestCase):
    """Use an isolated outbox; none of these tests can reach the native sender."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.database = self.root / "messages.sqlite3"
        self.log = self.root / "observer.jsonl"
        self.groups = tuple(f"room{number}@chatroom" for number in range(1, 5))
        self.admin = "admin_private"
        self.member = "member_private"
        self.account = "bot_private"
        self.now = time.time()
        self.store = Store(self.database)
        sender = SenderConfig(
            self.database, self.log, self.account,
            frozenset((*self.groups, self.admin, self.member)),
        )
        self.config = BotConfig(
            sender, self.plugin_directory(), ("broadcast",), admin_ids=(self.admin,),
        )
        self.plugin = load_plugins(self.config.plugin_directory, ("broadcast",))[0]
        self.router = ReplyRouter(self.store, self.config, [self.plugin], started_at=self.now - 1)

    def tearDown(self) -> None:
        self.store.close()
        self.temporary.cleanup()

    @staticmethod
    def plugin_directory() -> Path:
        return Path(__file__).resolve().parents[2] / "plugins"

    def message(self, content: str, *, sender_id: str | None = None,
                conversation_id: str | None = None, event_key: str = "session:1") -> Message:
        sender_id = self.admin if sender_id is None else sender_id
        conversation_id = sender_id if conversation_id is None else conversation_id
        return Message(
            session_id="session", event_key=event_key, seq=1, call_id=1,
            source="receive_batch", event_kind="item", observed_at_ms=int(self.now * 1000),
            message_type=1, message_kind="text", app_message_type=None, content=content,
            raw_content=content, conversation_id=conversation_id, sender_id=sender_id,
            direction="incoming", message_time_candidate=int(self.now),
            message_id_candidate=None, mentioned_ids=(), mention_state="none",
            history_state="live_candidate",
        )

    def queued(self) -> list[dict]:
        request_ids = [row[0] for row in self.store.db.execute(
            "SELECT request_id FROM outbox ORDER BY target_id"
        )]
        return [json.loads(Outbox(self.store.db).get(request_id)["command_json"])
                for request_id in request_ids]

    def test_admin_broadcasts_exact_multiline_text_to_every_group_and_receives_receipt(self) -> None:
        text = "今晚更新说明\n第二行保留原样 🙂"
        self.assertEqual(5, self.router.handle(self.message(f"#广播  {text}"), "session", now=self.now))

        commands = self.queued()
        group_commands = [command for command in commands if command["target_id"].endswith("@chatroom")]
        receipt = [command for command in commands if command["target_id"] == self.admin]
        self.assertEqual(set(self.groups), {command["target_id"] for command in group_commands})
        self.assertEqual([text] * 4, [command["text"] for command in group_commands])
        self.assertEqual(1, len(receipt))
        self.assertIn("已加入发送队列", receipt[0]["text"])
        self.assertEqual("manual", {command["origin"] for command in commands}.pop())
        self.assertTrue(all(command["target_id"] != self.member for command in commands))

    def test_non_admin_and_group_message_cannot_broadcast(self) -> None:
        command = "#广播 confidential"
        self.assertEqual(1, self.router.handle(self.message(command, sender_id=self.member), "session", now=self.now))
        denied = self.queued()
        self.assertEqual([self.member], [item["target_id"] for item in denied])
        self.assertTrue("权限" in denied[0]["text"] or "管理员" in denied[0]["text"])

        self.assertEqual(0, self.router.handle(
            self.message(command, conversation_id=self.groups[0], event_key="session:2"), "session", now=self.now
        ))
        self.assertEqual(1, len(self.queued()))

    def test_non_command_text_and_admin_command_in_group_are_ignored(self) -> None:
        for key, content in (("session:1", "请#广播 消息"), ("session:2", "#广播测试"),
                             ("session:3", "#广播 test")):
            conversation = self.groups[0] if key == "session:3" else self.admin
            self.assertEqual(0, self.router.handle(
                self.message(content, conversation_id=conversation, event_key=key), "session", now=self.now
            ))
        self.assertEqual(0, len(self.queued()))

    def test_help_empty_body_group_list_and_no_groups_stay_private(self) -> None:
        self.assertEqual(1, self.router.handle(self.message("#广播帮助"), "session", now=self.now))
        self.assertEqual(1, self.router.handle(self.message("#广播", event_key="session:2"), "session", now=self.now))
        self.assertEqual(1, self.router.handle(self.message("#广播群列表", event_key="session:3"), "session", now=self.now))
        commands = self.queued()
        self.assertEqual([self.admin] * 3, [command["target_id"] for command in commands])
        self.assertTrue(all(command["text"].strip() for command in commands))

        no_groups = BotConfig(
            SenderConfig(self.database, self.log, self.account, frozenset({self.admin})),
            self.plugin_directory(), ("broadcast",), admin_ids=(self.admin,),
        )
        router = ReplyRouter(self.store, no_groups, [self.plugin], started_at=self.now - 1)
        self.assertEqual(1, router.handle(self.message("#广播 hello", event_key="session:4"), "session", now=self.now))
        last = next(command for command in self.queued() if command["source_event_key"] == "session:4")
        self.assertEqual(self.admin, last["target_id"])
        self.assertTrue("群" in last["text"] and ("无" in last["text"] or "没有" in last["text"]))

    def test_same_event_is_idempotent_and_lifecycle_does_not_broadcast(self) -> None:
        for name in ('on_before_messages', 'on_poll', 'on_shutdown'):
            self.assertIsNone(getattr(self.plugin, name))
        event = self.message("#广播 again")
        self.assertEqual(5, self.router.handle(event, "session", now=self.now))
        self.assertEqual(0, self.router.handle(event, "session", now=self.now))
        self.assertEqual(0, self.router.start("session", now=self.now))
        # Automatic status broadcasts are paused, including repeated callbacks.
        self.assertEqual(0, self.router.before_messages("session", now=self.now))
        self.assertEqual(0, self.router.tick("session", now=self.now))
        self.assertEqual(0, self.router.tick("session", now=self.now))
        self.assertEqual(0, self.router.shutdown("session", now=self.now))
        self.assertEqual(0, self.router.shutdown("session", now=self.now))
        commands = self.queued()
        self.assertEqual(5, len(commands))
        self.assertFalse(any(command.get("command_kind") == "image" for command in commands))

    def test_second_enqueue_failure_rolls_back_all_broadcast_commands_and_retry_succeeds_once(self) -> None:
        event = self.message("#广播 retry")
        enqueue = self.router.outbox.enqueue_in_transaction
        calls = 0

        def fail_second(command):
            nonlocal calls
            calls += 1
            enqueue(command)
            if calls == 2:
                raise RuntimeError("second enqueue failed")

        with patch.object(self.router.outbox, "enqueue_in_transaction", side_effect=fail_second):
            with self.assertRaisesRegex(RuntimeError, "second enqueue failed"):
                self.router.handle(event, "session", now=self.now)
        self.assertEqual(0, len(self.queued()))
        self.assertEqual(0, self.store.db.execute("SELECT count(*) FROM plugin_runs").fetchone()[0])
        self.assertEqual(5, self.router.handle(event, "session", now=self.now))
        self.assertEqual(5, len(self.queued()))

    def test_router_rejects_forged_broadcast_from_non_admin_and_preserves_reply_isolation(self) -> None:
        forged = LoadedPlugin(
            "forged", None, parse_command=lambda _message: "go",
            handle_command=lambda _command, _context: GroupBroadcast("forged"),
        )
        router = ReplyRouter(self.store, self.config, [forged], started_at=self.now - 1)
        with self.assertLogs(level="ERROR"):
            self.assertEqual(0, router.handle(self.message("anything", sender_id=self.member), "session", now=self.now))
        self.assertEqual(0, len(self.queued()))

        cross_conversation = LoadedPlugin(
            "cross", None, parse_command=lambda _message: "go",
            handle_command=lambda _command, _context: Reply("reply", target_id=self.groups[0]),
        )
        cross_router = ReplyRouter(self.store, self.config, [cross_conversation], started_at=self.now - 1)
        with self.assertLogs(level="ERROR"):
            self.assertEqual(0, cross_router.handle(
                self.message("anything", sender_id=self.member, conversation_id=self.admin, event_key="session:9"),
                "session", now=self.now,
            ))
        self.assertEqual(0, len(self.queued()))

    def test_group_broadcast_text_validation_and_config_admin_validation(self) -> None:
        with self.assertRaisesRegex(ValueError, "empty"):
            GroupBroadcast(" \n\t")
        with self.assertRaisesRegex(ValueError, "16384"):
            GroupBroadcast("🙂" * 4097)

        default = BotConfig(
            SenderConfig(self.database, self.log, self.account, frozenset({self.admin})),
            self.plugin_directory(), ("broadcast",),
        )
        self.assertEqual((), default.admin_ids)
        with self.assertRaises(ValueError):
            BotConfig(default.sender, default.plugin_directory, default.enabled_plugins,
                      admin_ids=(self.admin, self.admin))
        with self.assertRaises(ValueError):
            BotConfig(default.sender, default.plugin_directory, default.enabled_plugins,
                      admin_ids=(self.groups[0],))
        with self.assertRaises(ValueError):
            BotConfig(default.sender, default.plugin_directory, default.enabled_plugins,
                      admin_ids=(self.account,))

    def test_load_bot_config_reads_admin_ids_and_rejects_non_private_or_non_whitelisted_admin(self) -> None:
        (self.root / "sender.toml").write_text(
            "account_id='bot_private'\nallowed_targets=['admin_private','room1@chatroom']\n"
            "database_path='messages.sqlite3'\nlog_path='observer.jsonl'\n", encoding="utf-8",
        )
        config_path = self.root / "bot.toml"
        base = "sender_config='sender.toml'\nplugin_directory='.'\nenabled_plugins=['broadcast']\n"
        config_path.write_text(base + "admin_ids=['admin_private']\n", encoding="utf-8")
        self.assertEqual(("admin_private",), load_bot_config(config_path).admin_ids)
        for invalid in ("admin_ids=['room1@chatroom']\n", "admin_ids=['unknown_private']\n"):
            config_path.write_text(base + invalid, encoding="utf-8")
            with self.assertRaises(ValueError):
                load_bot_config(config_path)

    def test_admin_manual_broadcast_online_and_offline(self) -> None:
        for index, text in enumerate(("#广播上线", "#广播下线"), start=1):
            with self.subTest(command=text):
                self.assertEqual(5, self.router.handle(
                    self.message(text, event_key=f"session:{index}"), "session", now=self.now + index))
        commands = self.queued()
        image_cmds = [c for c in commands if c.get("command_kind") == "image"]
        self.assertEqual(8, len(image_cmds))
        self.assertEqual(set(self.groups), {c["target_id"] for c in image_cmds})
        receipt = [c for c in commands if c.get("command_kind") != "image"]
        self.assertEqual(2, len(receipt))
        self.assertTrue(all("图片广播已加入发送队列" in c["text"] for c in receipt))


if __name__ == "__main__":
    unittest.main()
