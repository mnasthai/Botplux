"""Prop use must survive capture replays and roll back with its reply."""
from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from wechat_receiver.games import service
from wechat_receiver.games.commands import parse_command
from wechat_receiver.games.config import DEFAULT_GAME_CONFIG
from wechat_receiver.models import Message
from wechat_receiver.plugins import LoadedPlugin
from wechat_receiver.reply_router import ReplyRouter
from wechat_receiver.store import Store


class FixedRng:
    def randrange(self, stop: int) -> int:
        return 0

    def randint(self, start: int, stop: int) -> int:
        return start

    def choice(self, values):
        return values[0]


class XiuxianPropReplayTests(unittest.TestCase):
    account = "wxid_bot"
    group = "22913213991@chatroom"
    session = "test_session"
    now = datetime(2026, 9, 23, 10, tzinfo=timezone.utc).timestamp()

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temporary.name) / "messages.sqlite3")
        config = SimpleNamespace(
            sender=SimpleNamespace(account_id=self.account, allowed_targets=frozenset({self.group})),
            enabled_plugins=("xiuxian",), reply_ttl_seconds=60, max_message_age_seconds=120,
            game_config=replace(DEFAULT_GAME_CONFIG, duel_enabled=True),
        )
        rng = FixedRng()
        plugin = LoadedPlugin(
            "xiuxian", None, parse_command=parse_command,
            handle_command=lambda command, context: service.handle_command(command, context, rng=rng),
        )
        self.router = ReplyRouter(self.store, config, [plugin], started_at=self.now - 1)
        self.router.start(self.session, now=self.now)
        self.sequence = 0

    def tearDown(self) -> None:
        self.store.close()
        self.temporary.cleanup()

    def message(self, body: str, player: str = "alice", *, mentioned_ids=(),
                message_id: str | None = None) -> Message:
        self.sequence += 1
        seq = self.sequence
        return Message(
            session_id=self.session, event_key=f"prop:{seq}", seq=seq, call_id=seq,
            source="receive_batch", event_kind="item", observed_at_ms=int(self.now * 1000),
            message_type=1, message_kind="text", app_message_type=None,
            content=body, raw_content=body, conversation_id=self.group, sender_id=player,
            direction="incoming", message_time_candidate=int(self.now),
            message_id_candidate=message_id or str(10_000 + seq),
            mentioned_ids=mentioned_ids,
            mention_state="explicit_other" if mentioned_ids else "none",
            history_state="live_candidate",
        )

    def handle(self, message: Message) -> int:
        return self.router.handle(message, self.session, now=self.now)

    def send(self, body: str, player: str = "alice", **kwargs) -> int:
        return self.handle(self.message(body, player, **kwargs))

    def register(self, player: str, dao_name: str) -> None:
        self.assertEqual(1, self.send(f"#修仙 {dao_name}", player))
        self.store.db.execute(
            "UPDATE game_players SET spirit_stones=2000 WHERE account_id=? AND group_id=? AND player_id=?",
            (self.account, self.group, player),
        )
        self.store.db.commit()

    def buy(self, prop_name: str, player: str = "alice", count: int = 1) -> None:
        for _ in range(count):
            self.assertEqual(1, self.send(f"#购买 {prop_name}", player))
            self.assertIn(prop_name, self.last_reply())

    def last_reply(self) -> str:
        row = self.store.db.execute("SELECT text FROM outbox ORDER BY rowid DESC LIMIT 1").fetchone()
        return row[0] if row else ""

    def prop_ids(self, player: str) -> list[str]:
        rows = self.store.db.execute(
            "SELECT prop_id FROM game_player_props WHERE account_id=? AND group_id=? AND player_id=? ORDER BY prop_id",
            (self.account, self.group, player),
        ).fetchall()
        return [row[0] for row in rows]

    def debuff_ids(self, player: str) -> list[str]:
        rows = self.store.db.execute(
            "SELECT debuff_id FROM game_player_debuffs WHERE account_id=? AND group_id=? AND target_player_id=? ORDER BY debuff_id",
            (self.account, self.group, player),
        ).fetchall()
        return [row[0] for row in rows]

    def use_actions(self, player: str) -> list:
        return self.store.db.execute(
            "SELECT * FROM game_actions WHERE account_id=? AND group_id=? AND player_id=? "
            "AND action_kind='use_prop' ORDER BY created_at, event_key",
            (self.account, self.group, player),
        ).fetchall()

    def cross_capture(self, original: Message) -> Message:
        self.sequence += 1
        return replace(original, event_key=f"prop:recapture:{self.sequence}",
                       seq=self.sequence, call_id=self.sequence)

    def test_self_cleansing_records_use_and_deduplicates_both_replays(self) -> None:
        self.register("alice", "青玄")
        self.buy("清心净衣符", count=2)
        before = self.prop_ids("alice")
        self.assertEqual(2, len(before))

        use = self.message("#使用 清心净衣符", message_id="90001")
        self.assertEqual(1, self.handle(use))
        self.assertIn("使用成功", self.last_reply())
        remaining = self.prop_ids("alice")
        self.assertEqual(1, len(remaining))
        actions = self.use_actions("alice")
        self.assertEqual(1, len(actions))
        self.assertEqual(use.event_key, actions[0]["event_key"])
        self.assertEqual("90001", actions[0]["message_id"])

        self.assertEqual(0, self.handle(use))  # Same capture event.
        self.assertEqual(0, self.handle(self.cross_capture(use)))  # New capture, same WeChat message.
        self.assertEqual(remaining, self.prop_ids("alice"))
        self.assertEqual(1, len(self.use_actions("alice")))

    def test_curse_replay_after_target_cleanses_does_not_cast_again(self) -> None:
        self.register("alice", "青玄")
        self.register("bob", "百里")
        self.buy("扰心符", count=2)
        use = self.message("#使用 扰心符 @百里", mentioned_ids=("bob",), message_id="90002")
        self.assertEqual(1, self.handle(use))
        self.assertIn("暗算得手", self.last_reply())
        self.assertEqual(1, len(self.debuff_ids("bob")))
        remaining = self.prop_ids("alice")
        self.assertEqual(1, len(remaining))
        self.assertEqual(1, len(self.use_actions("alice")))

        self.buy("清心净衣符", "bob")
        self.assertEqual(1, self.send("#使用 清心净衣符", "bob"))
        self.assertEqual([], self.debuff_ids("bob"))

        self.assertEqual(0, self.handle(self.cross_capture(use)))
        self.assertEqual(remaining, self.prop_ids("alice"))
        self.assertEqual([], self.debuff_ids("bob"))
        self.assertEqual(1, len(self.use_actions("alice")))

    def test_wuxing_flag_blocks_curse_but_consumption_is_recorded_once(self) -> None:
        self.register("alice", "青玄")
        self.register("bob", "百里")
        self.buy("断脉散", count=2)
        self.store.db.execute(
            "INSERT INTO game_items (item_id,account_id,group_id,template_id,rarity,owner_player_id,state) "
            "VALUES ('TEST_FLAG',?,?, 'wuxing_qi','treasure','bob','held')",
            (self.account, self.group),
        )
        self.store.db.commit()

        use = self.message("#使用 断脉散 @百里", mentioned_ids=("bob",), message_id="90003")
        self.assertEqual(1, self.handle(use))
        self.assertIn("五行辟易", self.last_reply())
        remaining = self.prop_ids("alice")
        self.assertEqual(1, len(remaining))
        self.assertEqual([], self.debuff_ids("bob"))
        self.assertEqual(1, len(self.use_actions("alice")))

        self.assertEqual(0, self.handle(self.cross_capture(use)))
        self.assertEqual(remaining, self.prop_ids("alice"))
        self.assertEqual([], self.debuff_ids("bob"))
        self.assertEqual(1, len(self.use_actions("alice")))

    def test_outbox_failure_rolls_back_prop_debuff_and_action_then_retries(self) -> None:
        self.register("alice", "青玄")
        self.register("bob", "百里")
        self.buy("断脉散")
        before_props = self.prop_ids("alice")
        before_outbox = self.store.db.execute("SELECT count(*) FROM outbox").fetchone()[0]
        use = self.message("#使用 断脉散 @百里", mentioned_ids=("bob",), message_id="90004")

        with patch.object(self.router.outbox, "enqueue_in_transaction",
                          side_effect=RuntimeError("queue down")):
            with self.assertRaisesRegex(RuntimeError, "queue down"):
                self.handle(use)
        self.assertEqual(before_props, self.prop_ids("alice"))
        self.assertEqual([], self.debuff_ids("bob"))
        self.assertEqual([], self.use_actions("alice"))
        self.assertEqual(before_outbox, self.store.db.execute("SELECT count(*) FROM outbox").fetchone()[0])

        self.assertEqual(1, self.handle(use))
        self.assertEqual([], self.prop_ids("alice"))
        self.assertEqual(1, len(self.debuff_ids("bob")))
        self.assertEqual(1, len(self.use_actions("alice")))
        self.assertEqual(0, self.handle(self.cross_capture(use)))
        self.assertEqual(1, len(self.debuff_ids("bob")))


if __name__ == "__main__":
    unittest.main()
