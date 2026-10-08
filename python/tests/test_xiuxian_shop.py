"""Tests for the xiuxian shop system (替身草人, 凝气丹, 洗髓丹, 寻宝令)."""
from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest

from wechat_receiver.games import service
from wechat_receiver.games.catalog import CATALOG
from wechat_receiver.games.commands import Command, parse_command
from wechat_receiver.games.config import DEFAULT_GAME_CONFIG
from wechat_receiver.models import Message
from wechat_receiver.outbox import Outbox
from wechat_receiver.plugins import LoadedPlugin
from wechat_receiver.reply_router import ReplyRouter
from wechat_receiver.store import Store


class FixedRng:
    def __init__(self, *rolls: int) -> None:
        self.rolls = list(rolls or (0,))

    def randrange(self, stop: int) -> int:
        return self.rolls.pop(0) if self.rolls else 0

    def randint(self, a: int, b: int) -> int:
        return a

    def choice(self, sequence):
        return sequence[0]


class XiuxianShopTests(unittest.TestCase):
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
        from wechat_receiver.games import duels
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
        row = self.store.db.execute("SELECT text FROM outbox ORDER BY rowid DESC LIMIT 1").fetchone()
        return row[0] if row else ""

    def player(self, player_id: str = "alice"):
        return self.store.db.execute(
            "SELECT * FROM game_players WHERE account_id=? AND group_id=? AND player_id=?",
            (self.account, self.group, player_id),
        ).fetchone()

    def inventory(self, player_id: str = "alice"):
        return self.store.db.execute(
            "SELECT * FROM game_items WHERE account_id=? AND group_id=? AND owner_player_id=? AND state='held'",
            (self.account, self.group, player_id),
        ).fetchall()

    def seed_player(self, player_id: str, name: str, stones: int = 100, cultivation: int = 0) -> None:
        self.store.db.execute("""INSERT INTO game_players
            (account_id,group_id,player_id,dao_name,dao_name_key,spirit_stones,cultivation,joined_at)
            VALUES(?,?,?,?,?,?,?,?)""",
            (self.account, self.group, player_id, name, name.casefold(), stones, cultivation,
             datetime.fromtimestamp(self.now, timezone.utc).isoformat()))
        self.store.db.commit()

    def seed_item(self, player_id: str, item_id: str, template_id: str = "qingfeng_jian") -> None:
        template = CATALOG[template_id]
        self.store.db.execute("""INSERT INTO game_items
            (item_id,account_id,group_id,template_id,rarity,owner_player_id,state)
            VALUES(?,?,?,?,?,?,'held')""",
            (item_id, self.account, self.group, template.id, template.rarity, player_id))
        self.store.db.commit()

    def accept_latest_prompt(self, *, completed_at: float | None = None) -> None:
        duel = self.store.db.execute(
            "SELECT * FROM game_duels WHERE account_id=? AND group_id=? ORDER BY created_at DESC,duel_id DESC LIMIT 1",
            (self.account, self.group)).fetchone()
        self.assertIsNotNone(duel)
        request_id = duel["prompt_request_id"]
        self.assertIsNotNone(request_id)
        outbox = Outbox(self.store.db)
        at = self.now if completed_at is None else completed_at
        claim = outbox.claim_next(self.account, self.session, now=datetime.fromtimestamp(self.now, timezone.utc),
                                  request_id=request_id)
        self.assertIsNotNone(claim)
        outbox.record_result(claim["request_id"], claim["attempt_id"], "accepted",
                             now=datetime.fromtimestamp(at, timezone.utc))

    def test_command_parsing(self) -> None:
        m = lambda text: Message(
            session_id="s", event_key="k", seq=1, call_id=1, source="batch", event_kind="item",
            observed_at_ms=1, message_type=1, message_kind="text", app_message_type=None, content=text,
            raw_content=text, conversation_id="room@chatroom", sender_id="user", direction="incoming",
            message_time_candidate=None, message_id_candidate=None, mentioned_ids=(),
            mention_state="none", history_state="live_candidate",
        )
        self.assertEqual(Command("shop"), parse_command(m("#商店")))
        self.assertEqual(Command("shop"), parse_command(m("#修仙商店")))
        self.assertEqual(Command("shop_help"), parse_command(m("#商店帮助")))
        self.assertEqual(Command("shop_help"), parse_command(m("#商店 帮助")))
        self.assertEqual(Command("buy", "1"), parse_command(m("#购买 1")))
        self.assertEqual(Command("buy", "替身草人"), parse_command(m("#购买 替身草人")))
        self.assertEqual(Command("buy", "凝气丹"), parse_command(m("#购买 凝气丹")))
        self.assertEqual("usage", parse_command(m("#购买")).kind)

    def test_unregistered_player_access_denied(self) -> None:
        self.assertEqual(1, self.send("#商店", "stranger"))
        self.assertIn("你还没有创建修士", self.last_reply())

        self.assertEqual(1, self.send("#购买 1", "stranger"))
        self.assertIn("你还没有创建修士", self.last_reply())

    def test_shop_display_and_help(self) -> None:
        self.seed_player("alice", "青玄", stones=300)
        self.assertEqual(1, self.send("#商店"))
        reply = self.last_reply()
        self.assertIn("仙家商店", reply)
        self.assertIn("青玄", reply)
        self.assertIn("300", reply)
        self.assertIn("替身草人", reply)
        self.assertIn("凝气丹", reply)
        self.assertIn("洗髓丹", reply)
        self.assertIn("寻宝令", reply)

        self.assertEqual(1, self.send("#商店帮助"))
        self.assertIn("【商店指引】", self.last_reply())

        self.assertEqual(1, self.send("#修仙帮助"))
        self.assertIn("#商店帮助", self.last_reply())
        self.assertIn("#道具帮助", self.last_reply())
        self.assertNotIn("#购买", self.last_reply())

    def test_buy_invalid_item_and_insufficient_stones(self) -> None:
        self.seed_player("alice", "青玄", stones=30)
        self.assertEqual(1, self.send("#购买 诛仙剑"))
        self.assertIn("暂无此物品", self.last_reply())

        # 凝气丹售价 150 灵石，当前只有 30
        self.assertEqual(1, self.send("#购买 凝气丹"))
        self.assertIn("灵石不足", self.last_reply())
        self.assertEqual(30, self.player("alice")["spirit_stones"])

    def test_buy_ningqi_dan_increases_cultivation_and_respects_daily_limit(self) -> None:
        self.seed_player("alice", "青玄", stones=1000, cultivation=10)
        # 第一次购买：消耗 150 灵石，增加 20 修为
        self.assertEqual(1, self.send("#购买 凝气丹"))
        self.assertIn("购买成功", self.last_reply())
        self.assertIn("修为 +20", self.last_reply())
        player = self.player("alice")
        self.assertEqual(850, player["spirit_stones"])
        self.assertEqual(30, player["cultivation"])

        # 第二次购买：通过编号 2 购买（不设个人限购，可继续购买）
        self.assertEqual(1, self.send("#购买 2"))
        self.assertIn("购买成功", self.last_reply())
        player = self.player("alice")
        self.assertEqual(700, player["spirit_stones"])
        self.assertEqual(50, player["cultivation"])

        # 购买第 3, 4, 5 次，消耗完群每日库存（共 5 枚）
        for _ in range(3):
            self.assertEqual(1, self.send("#购买 2"))
        player = self.player("alice")
        self.assertEqual(250, player["spirit_stones"])
        self.assertEqual(110, player["cultivation"])

        # 第六次购买：群每日库存已售罄 (5/5)
        self.assertEqual(1, self.send("#购买 凝气丹"))
        self.assertIn("库存已售罄", self.last_reply())
        player = self.player("alice")
        self.assertEqual(250, player["spirit_stones"])
        self.assertEqual(110, player["cultivation"])

    def test_buy_xisui_dan_clears_cooldown_and_rejects_unnecessary_buy(self) -> None:
        self.seed_player("alice", "青玄", stones=200)
        # 冷却未激活时购买被拦截，不浪费灵石
        self.assertEqual(1, self.send("#购买 洗髓丹"))
        self.assertIn("并未处于冷却中", self.last_reply())
        self.assertEqual(200, self.player("alice")["spirit_stones"])

        # 自主修炼一次，进入 1 小时冷却
        self.rng.rolls = [10]  # 触发成功
        self.assertEqual(1, self.send("#自主修炼"))
        self.assertIn("自主修炼", self.last_reply())

        # 冷却中无法再次自主修炼
        self.assertEqual(1, self.send("#自主修炼"))
        self.assertIn("尚需", self.last_reply())

        # 购买洗髓丹重置冷却（售价 10 灵石）
        self.assertEqual(1, self.send("#购买 洗髓丹"))
        self.assertIn("购买成功", self.last_reply())
        self.assertIn("自主修炼冷却已重置", self.last_reply())
        self.assertEqual(190, self.player("alice")["spirit_stones"])

        # 立刻可以再次自主修炼
        self.rng.rolls = [10]
        self.assertEqual(1, self.send("#自主修炼"))
        self.assertIn("自主修炼", self.last_reply())

        # 不设个人限购：再次冷却后，可再次购买洗髓丹重置！
        self.assertEqual(1, self.send("#购买 洗髓丹"))
        self.assertIn("购买成功", self.last_reply())
        self.assertEqual(180, self.player("alice")["spirit_stones"])

    def test_buy_xunbao_ling_clears_explored_flag(self) -> None:
        self.seed_player("alice", "青玄", stones=600)
        # 尚未探索过秘境时尝试购买被拦截
        self.assertEqual(1, self.send("#购买 寻宝令"))
        self.assertIn("尚未探索过秘境", self.last_reply())
        self.assertEqual(600, self.player("alice")["spirit_stones"])

        # 探索秘境消耗 60 灵石
        self.assertEqual(1, self.send("#秘境"))
        self.assertIn("发现", self.last_reply())
        self.assertEqual(540, self.player("alice")["spirit_stones"])

        # 再次探索提示今日已探索过
        self.assertEqual(1, self.send("#秘境"))
        self.assertIn("今日已经探索过秘境", self.last_reply())

        # 购买寻宝令（100 灵石）
        self.assertEqual(1, self.send("#购买 4"))
        self.assertIn("今日秘境探索资格已刷新", self.last_reply())
        self.assertEqual(440, self.player("alice")["spirit_stones"])

        # 再次探索秘境成功！
        self.assertEqual(1, self.send("#秘境"))
        self.assertIn("发现", self.last_reply())
        self.assertEqual(380, self.player("alice")["spirit_stones"])

        # 不设个人限购：同一个人当天可以再次购买寻宝令并探索！
        self.assertEqual(1, self.send("#购买 寻宝令"))
        self.assertIn("今日秘境探索资格已刷新", self.last_reply())
        self.assertEqual(280, self.player("alice")["spirit_stones"])

    def test_buy_tishen_caoren_and_offer(self) -> None:
        self.seed_player("alice", "青玄", stones=200)
        # 购买替身草人（88 灵石）
        self.assertEqual(1, self.send("#购买 替身草人"))
        self.assertIn("购得护身法器【替身草人】", self.last_reply())
        self.assertEqual(112, self.player("alice")["spirit_stones"])
        inv = self.inventory("alice")
        self.assertEqual(1, len(inv))
        straw_id = inv[0]["item_id"]
        self.assertEqual("tishen_caoren", inv[0]["template_id"])

        # 献宝替身草人可得 20 灵石
        self.assertEqual(1, self.send(f"#献宝 {straw_id}"))
        self.assertIn("灵石 +20", self.last_reply())
        self.assertEqual(132, self.player("alice")["spirit_stones"])
        self.assertEqual(0, len(self.inventory("alice")))

    def test_buy_tishen_caoren_bag_full_rejected(self) -> None:
        self.seed_player("alice", "青玄", stones=300)
        for i in range(DEFAULT_GAME_CONFIG.inventory_limit):
            self.seed_item("alice", f"F0000000{i}")
        self.assertEqual(6, len(self.inventory("alice")))

        self.assertEqual(1, self.send("#购买 替身草人"))
        self.assertIn("储物袋已满", self.last_reply())
        self.assertEqual(300, self.player("alice")["spirit_stones"])

    def test_tishen_caoren_protects_in_duel(self) -> None:
        # alice 持有真实法宝 qingfeng_jian 与 替身草人 tishen_caoren
        self.seed_player("alice", "青玄", stones=300)
        self.seed_item("alice", "F_REAL_SWORD", "qingfeng_jian")
        self.seed_item("alice", "F_STRAW_DUMMY", "tishen_caoren")

        # bob 持有普通法宝
        self.seed_player("bob", "白墨", stones=300)
        self.seed_item("bob", "F_BOB_ITEM", "qingfeng_jian")

        # bob 发起斗法挑战 alice
        self.assertEqual(1, self.send("#斗法 @青玄", "bob", mentioned_ids=("alice",), mention_state="explicit_other"))
        duel = self.store.db.execute("SELECT * FROM game_duels WHERE state='inviting'").fetchone()
        self.assertIsNotNone(duel)
        duel_id = duel["duel_id"]

        self.accept_latest_prompt()
        self.router.before_messages(self.session, now=self.now)

        # alice 接受斗法（预选 loot 时，应当优先选择替身草人）
        self.assertEqual(1, self.send("#接受斗法", "alice"))
        duel = self.store.db.execute("SELECT * FROM game_duels WHERE duel_id=?", (duel_id,)).fetchone()
        snapshot = json.loads(duel["rules_json"])
        self.assertEqual("F_STRAW_DUMMY", snapshot["loot_by_player"]["alice"])

        self.accept_latest_prompt()
        self.router.before_messages(self.session, now=self.now)
        for _ in range(8):
            self.now += 4
            self.router.tick(self.session, now=self.now)

        # 进入 playing 阶段
        duel = self.store.db.execute("SELECT * FROM game_duels WHERE duel_id=?", (duel_id,)).fetchone()
        self.assertEqual("playing", duel["state"])
        self.accept_latest_prompt()
        self.router.before_messages(self.session, now=self.now)

        # 设置紫霄神雷位置为 2，challenger(bob) 先引雷，落空；轮到 alice 引雷时中雷落败
        self.store.db.execute("UPDATE game_duels SET lightning_position=2, next_turn=1 WHERE duel_id=?", (duel_id,))
        self.store.db.commit()

        # bob 引雷 (turn 1) - 未中雷
        self.assertEqual(1, self.send("#引雷", "bob"))
        self.accept_latest_prompt()
        self.router.before_messages(self.session, now=self.now)

        # alice 引雷 (turn 2) - 中雷落败！
        self.assertEqual(1, self.send("#引雷", "alice"))
        duel = self.store.db.execute("SELECT * FROM game_duels WHERE duel_id=?", (duel_id,)).fetchone()
        self.assertEqual("settled", duel["state"])
        self.assertEqual("alice", duel["loser_player_id"])
        self.assertEqual("bob", duel["winner_player_id"])
        self.assertEqual("F_STRAW_DUMMY", duel["loot_item_id"])

        # 检查战报文案
        reply = self.last_reply()
        self.assertIn("【替身草人】替主挡灾应声碎裂", reply)

        # 检查法宝归属：alice 仍持有真实的青风剑，替身草人直接消失，胜者 bob 无法夺取
        alice_inv = [i["item_id"] for i in self.inventory("alice")]
        self.assertIn("F_REAL_SWORD", alice_inv)
        self.assertNotIn("F_STRAW_DUMMY", alice_inv)
        bob_inv = [i["item_id"] for i in self.inventory("bob")]
        self.assertNotIn("F_STRAW_DUMMY", bob_inv)

    def test_tishen_caoren_can_be_offered(self) -> None:
        # 持有者依然可以使用 #献宝 换取灵石
        self.seed_player("alice", "青玄", stones=300)
        self.seed_item("alice", "FSTRAWDUMMY", "tishen_caoren")
        self.assertEqual(1, self.send("#献宝 FSTRAWDUMMY", "alice"))
        self.assertIn("献出【替身草人】FSTRAWDUMMY", self.last_reply())
        self.assertIn("灵石 +20", self.last_reply())
        self.assertEqual(320, self.player("alice")["spirit_stones"])
        self.assertNotIn("FSTRAWDUMMY", [i["item_id"] for i in self.inventory("alice")])

    def test_explore_never_drops_tishen_caoren(self) -> None:
        self.seed_player("alice", "青玄", stones=10000)
        dropped_templates = set()
        for i in range(30):
            # 每次清空探索标记以便多次测试
            self.store.db.execute("UPDATE game_players SET last_explored_on=NULL WHERE player_id='alice'")
            self.store.db.commit()
            self.rng.rolls = [0]  # artifact roll
            self.send("#秘境", "alice")
            inv = self.inventory("alice")
            if inv:
                dropped_templates.add(inv[-1]["template_id"])
                # 整理背包避免上限
                self.store.db.execute("DELETE FROM game_items WHERE item_id=?", (inv[-1]["item_id"],))
                self.store.db.commit()
        self.assertNotIn("tishen_caoren", dropped_templates)


if __name__ == "__main__":
    unittest.main()
