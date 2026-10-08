from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest

from wechat_receiver.games import service, duels
from wechat_receiver.games.commands import Command, parse_command
from wechat_receiver.games.config import DEFAULT_GAME_CONFIG
from wechat_receiver.models import Message
from wechat_receiver.plugins import LoadedPlugin
from wechat_receiver.reply_router import ReplyRouter
from wechat_receiver.store import Store


class FixedRng:
    def __init__(self, *rolls: int) -> None:
        self.rolls = list(rolls)

    def randrange(self, stop: int) -> int:
        return self.rolls.pop(0) if self.rolls else 0

    def randint(self, a: int, b: int) -> int:
        return a

    def choice(self, sequence):
        return sequence[0]


class XiuxianMineTests(unittest.TestCase):
    account = "wxid_bot"
    group = "22913213991@chatroom"
    session = "test_session"
    base = datetime(2026, 9, 23, 10, 0, tzinfo=timezone.utc)

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temporary.name) / "messages.sqlite3")
        self.now = self.base.timestamp()
        game_config = replace(DEFAULT_GAME_CONFIG, duel_enabled=True)
        self.config = SimpleNamespace(
            sender=SimpleNamespace(account_id=self.account, allowed_targets=frozenset({self.group})),
            enabled_plugins=("xiuxian",), reply_ttl_seconds=60, max_message_age_seconds=120,
            game_config=game_config,
        )
        self.rng = FixedRng()
        plugin = LoadedPlugin(
            "xiuxian", None, parse_command=parse_command,
            handle_command=lambda command, context: service.handle_command(command, context, rng=self.rng),
            on_start=duels.on_start, on_before_messages=duels.on_before_messages, on_poll=duels.on_poll,
        )
        self.router = ReplyRouter(self.store, self.config, [plugin], started_at=self.now - 1)
        self.router.start(self.session, now=self.now)
        self.sequence = 0

    def tearDown(self) -> None:
        self.store.close()
        self.temporary.cleanup()

    def message(self, text: str, player: str = "alice", *, mentioned_ids=(), mention_state="none") -> Message:
        self.sequence += 1
        return Message(
            session_id=self.session, event_key=f"test:{self.sequence}", seq=self.sequence, call_id=self.sequence,
            source="receive_batch", event_kind="item", observed_at_ms=int(self.now * 1000),
            message_type=1, message_kind="text", app_message_type=None, content=text, raw_content=text,
            conversation_id=self.group, sender_id=player, direction="incoming",
            message_time_candidate=int(self.now), message_id_candidate=str(10_000 + self.sequence),
            mentioned_ids=mentioned_ids, mention_state=mention_state, history_state="live_candidate",
        )

    def send(self, text: str, player: str = "alice", **kwargs) -> int:
        return self.router.handle(self.message(text, player, **kwargs), self.session, now=self.now)

    def last_reply(self) -> str:
        row = self.store.db.execute(
            "SELECT text FROM outbox ORDER BY rowid DESC LIMIT 1"
        ).fetchone()
        return row[0] if row else ""

    def seed_player(self, player_id: str, dao_name: str, stones: int = 100, realm: str = "qi") -> None:
        self.send(f"#修仙 {dao_name}", player_id)
        self.store.db.execute(
            "UPDATE game_players SET spirit_stones=?, realm=? WHERE player_id=?",
            (stones, realm, player_id)
        )
        self.store.db.commit()

    # 1. 指令解析：#采灵、#开采、#采矿
    def test_mine_command_parsing(self) -> None:
        msg1 = self.message("#采灵")
        cmd1 = parse_command(msg1)
        self.assertIsNotNone(cmd1)
        self.assertEqual("mine", cmd1.kind)

        msg2 = self.message("#开采")
        cmd2 = parse_command(msg2)
        self.assertIsNotNone(cmd2)
        self.assertEqual("mine", cmd2.kind)

        msg3 = self.message("#采矿")
        cmd3 = parse_command(msg3)
        self.assertIsNotNone(cmd3)
        self.assertEqual("mine", cmd3.kind)

    # 2. 正常采矿获得灵石与个人面板冷却状态
    def test_mine_success_and_profile_status(self) -> None:
        self.seed_player("alice", "青玄", stones=100, realm="qi")

        # 检查初始面板采矿可进行
        self.send("#修仙", "alice")
        self.assertIn("⛏️ 灵矿采矿：可开采", self.last_reply())

        # rng 控制：
        # low=10, high=15, randrange(6) -> 取 2 (产出 10+2=12)
        # crit randrange(100) -> 取 50 (未暴击)
        self.rng.rolls = [2, 50]
        self.send("#采矿", "alice")
        reply = self.last_reply()
        self.assertIn("⛏️【灵矿采矿】", reply)
        self.assertIn("青玄深入后山灵矿，运转灵力凿采灵石！", reply)
        self.assertIn("💎 获得灵石：+12", reply)
        self.assertIn("当前灵石：112", reply)

        # 再次查看面板，应显示冷却中
        self.send("#修仙", "alice")
        self.assertIn("⏳ 灵矿采矿：3600 秒后可开采", self.last_reply())

    # 3. 冷却时间拦截与跨小时重新开采
    def test_mine_cooldown_and_resume(self) -> None:
        self.seed_player("alice", "青玄", stones=100, realm="qi")
        self.rng.rolls = [0, 50]
        self.send("#采矿", "alice")

        # 立即再次开采，应被冷却拦截
        self.send("#采矿", "alice")
        reply = self.last_reply()
        self.assertIn("⏳ 灵矿灵气枯竭，尚需 3600 秒后灵气方能再次凝聚", reply)

        # 时间推进 30 分钟（1800 秒）
        self.now += 1800
        self.send("#采矿", "alice")
        self.assertIn("尚需 1800 秒后灵气方能再次凝聚", self.last_reply())

        # 时间推进至 3601 秒，冷却结束可再次开采
        self.now += 1801
        self.rng.rolls = [3, 50]
        self.send("#采矿", "alice")
        self.assertIn("💎 获得灵石：+13", self.last_reply())

    # 4. 灵髓暴击彩蛋（额外 +15 灵石）
    def test_mine_crit_egg(self) -> None:
        self.seed_player("alice", "青玄", stones=100, realm="qi")

        # rng 控制：base roll = 5 (10+5=15), crit roll = 5 (< 10 暴击)
        self.rng.rolls = [5, 5]
        self.send("#采矿", "alice")
        reply = self.last_reply()
        self.assertIn("⛏️【灵矿采矿 · 灵髓乍现】", reply)
        self.assertIn("竟挖出一块【极品灵髓】！", reply)
        self.assertIn("💎 获得灵石：+30（含灵髓奖励 +15）", reply)
        self.assertIn("当前灵石：130", reply)

    # 5. 境界进阶产出提升
    def test_mine_realm_tiers(self) -> None:
        # 筑基期：15 ~ 20 (base=15+0=15)
        self.seed_player("bob", "百里", stones=100, realm="foundation")
        self.rng.rolls = [0, 50]
        self.send("#开采", "bob")
        self.assertIn("💎 获得灵石：+15", self.last_reply())

        # 金丹期：20 ~ 25 (base=20+2=22)
        self.seed_player("charlie", "长生", stones=100, realm="core")
        self.rng.rolls = [2, 50]
        self.send("#采矿", "charlie")
        self.assertIn("💎 获得灵石：+22", self.last_reply())


if __name__ == "__main__":
    unittest.main()
