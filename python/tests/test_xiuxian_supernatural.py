"""Comprehensive tests for artifact supernatural abilities (法宝随身神通)."""
from __future__ import annotations

from dataclasses import asdict, replace
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest

from wechat_receiver.games import service
from wechat_receiver.games.catalog import CATALOG
from wechat_receiver.games.commands import parse_command
from wechat_receiver.games.config import DEFAULT_GAME_CONFIG
from wechat_receiver.models import Message
from wechat_receiver.outbox import Outbox
from wechat_receiver.plugins import LoadedPlugin
from wechat_receiver.reply_router import ReplyRouter
from wechat_receiver.store import Store


class ControllableRng:
    def __init__(self) -> None:
        self.randrange_results: list[int] = []
        self.randint_results: list[int] = []
        self.choice_index: int = 0

    def randrange(self, stop: int) -> int:
        if self.randrange_results:
            return self.randrange_results.pop(0)
        return 0

    def randint(self, a: int, b: int) -> int:
        if self.randint_results:
            return self.randint_results.pop(0)
        return a

    def choice(self, sequence):
        if not sequence:
            raise IndexError('Cannot choose from empty sequence')
        idx = self.choice_index % len(sequence)
        return sequence[idx]


class XiuxianSupernaturalTests(unittest.TestCase):
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
        self.rng = ControllableRng()
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

    def seed_player(self, player_id: str, name: str, stones: int = 100, cultivation: int = 0, realm: str = "qi") -> None:
        self.store.db.execute("""INSERT INTO game_players
            (account_id,group_id,player_id,dao_name,dao_name_key,spirit_stones,cultivation,realm,joined_at)
            VALUES(?,?,?,?,?,?,?,?,?)""",
            (self.account, self.group, player_id, name, name.casefold(), stones, cultivation, realm,
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

    # 1. 聚气葫芦【纳灵】：每日修炼额外 +5 修为 与 +10 灵石
    def test_juqi_hulu_cultivation(self) -> None:
        self.seed_player("alice", "青玄", stones=100, cultivation=0)
        self.seed_item("alice", "FJUQI", "juqi_hulu")
        self.send("#修炼", "alice")
        reply = self.last_reply()
        self.assertIn("修为 +52", reply)
        self.assertIn("灵石 +110", reply)
        self.assertIn("聚气葫芦【纳灵】", reply)
        alice = self.player("alice")
        self.assertEqual(52, alice["cultivation"])
        self.assertEqual(210, alice["spirit_stones"])

    # 2. 山河图【洞天福地】：每日修炼收益提升 20% (60 修为、120 灵石)
    def test_shanhe_tu_cultivation(self) -> None:
        self.seed_player("alice", "青玄", stones=100, cultivation=0)
        self.seed_item("alice", "FSHANHE", "shanhe_tu")
        self.send("#修炼", "alice")
        reply = self.last_reply()
        self.assertIn("修为 +60", reply)
        self.assertIn("灵石 +120", reply)
        self.assertIn("山河图【洞天福地】", reply)
        alice = self.player("alice")
        self.assertEqual(60, alice["cultivation"])
        self.assertEqual(220, alice["spirit_stones"])

    # 3. 山河图 + 聚气葫芦 叠加
    def test_shanhe_and_juqi_stack(self) -> None:
        self.seed_player("alice", "青玄", stones=100, cultivation=0)
        self.seed_item("alice", "FSHANHE", "shanhe_tu")
        self.seed_item("alice", "FJUQI", "juqi_hulu")
        self.send("#修炼", "alice")
        reply = self.last_reply()
        self.assertIn("修为 +62", reply)
        self.assertIn("灵石 +130", reply)
        self.assertIn("山河图【洞天福地】+20%", reply)
        self.assertIn("聚气葫芦【纳灵】+2修为/+10灵石", reply)

    # 4. 避尘珠【辟邪】：自主修炼失败走火入魔仅损失 1 修为
    def test_bichen_zhu_reduces_self_cultivate_loss(self) -> None:
        self.seed_player("alice", "青玄", stones=100, cultivation=10)
        self.seed_item("alice", "FBICHEN", "bichen_zhu")
        self.rng.randrange_results = [99]  # 触发失败 (>=50)
        self.send("#自主修炼", "alice")
        reply = self.last_reply()
        self.assertIn("走火入魔", reply)
        self.assertIn("修为 -1", reply)
        self.assertIn("避尘珠【辟邪】庇护心神，损失减半", reply)
        self.assertEqual(9, self.player("alice")["cultivation"])

    # 5. 玄铁印【沉心】：自主修炼成功率从 50% 提高至 65%
    def test_xuantie_yin_increases_success_rate(self) -> None:
        self.seed_player("alice", "青玄", stones=100, cultivation=10)
        self.seed_item("alice", "FXUANTIE", "xuantie_yin")
        # 60 在原设定中失败(>=50)，但有玄铁印则成功(<65)
        self.rng.randrange_results = [60]
        self.send("#自主修炼", "alice")
        reply = self.last_reply()
        self.assertIn("灵机乍现", reply)
        self.assertIn("玄铁印【沉心】稳固道基", reply)
        self.assertEqual(15, self.player("alice")["cultivation"])

    # 6. 流光梭【疾行】：自主修炼冷却时间缩短 30 分钟 (3600 -> 1800 秒)
    def test_liuguang_suo_shortens_cooldown(self) -> None:
        self.seed_player("alice", "青玄", stones=100, cultivation=10)
        self.seed_item("alice", "FLIUGUANG", "liuguang_suo")
        self.rng.randrange_results = [0]
        self.send("#自主修炼", "alice")
        # 10 秒后再次尝试，冷却提示约为 1790 秒（而非 3590 秒）
        self.now += 10
        self.send("#自主修炼", "alice")
        self.assertIn("尚需 1790 秒", self.last_reply())
        # 1800 秒后冷却完毕
        self.now += 1791
        self.rng.randrange_results = [0]
        self.send("#自主修炼", "alice")
        self.assertIn("灵机乍现", self.last_reply())

    # 7. 碧玉葫芦【回泉】：秘境探索抽到法器返还 30 灵石
    def test_biyu_hulu_refunds_on_artifact_explore(self) -> None:
        self.seed_player("alice", "青玄", stones=300, cultivation=10)
        self.seed_item("alice", "FBIYU", "biyu_hulu")
        self.rng.randrange_results = [0]  # 炼气期权重 roll 0 为 artifact
        self.send("#秘境", "alice")
        reply = self.last_reply()
        self.assertIn("【回泉】碧玉葫芦", reply)
        self.assertIn("返还 30 灵石", reply)
        # 探索原价 60，返还 30，净消耗 30 灵石：300 - 30 = 270
        self.assertEqual(270, self.player("alice")["spirit_stones"])

    # 8. 乾坤鼎【内藏乾坤】：储物袋上限由 6 拓展至 8 件（日常探宝、购买以及斗法联动放宽）
    def test_qiankun_ding_expands_inventory_to_8(self) -> None:
        self.seed_player("alice", "青玄", stones=1000, cultivation=10)
        self.seed_item("alice", "FDING", "qiankun_ding")
        # 填充到 6 件（乾坤鼎 + 5 件普通法宝）
        for i in range(1, 6):
            self.seed_item("alice", f"FITEM{i}", "qingfeng_jian")
        self.assertEqual(6, len(self.inventory("alice")))

        # 查看面板显示 6/8
        self.send("#修仙", "alice")
        self.assertIn("法宝：6/8", self.last_reply())

        # 查看法宝显示 6/8
        self.send("#法宝", "alice")
        self.assertIn("6/8", self.last_reply())

        # 探索秘境：达到 7 件
        self.rng.randrange_results = [0]
        self.send("#秘境", "alice")
        self.assertIn("发现【", self.last_reply())
        self.assertEqual(7, len(self.inventory("alice")))

        # 商店购买替身草人：成功放入，达到 8 件
        self.send("#购买 1", "alice")
        self.assertIn("购买成功", self.last_reply())
        self.assertEqual(8, len(self.inventory("alice")))

        # 满 8 件后再尝试购买，提示储物袋已满
        self.send("#购买 1", "alice")
        self.assertIn("储物袋已满", self.last_reply())

    # 8b. 乾坤鼎【内藏乾坤】：斗法参战上限联动放宽至 8 件
    def test_qiankun_ding_duel_inventory_limit(self) -> None:
        self.seed_player("alice", "青玄", stones=1000, cultivation=10)
        self.seed_item("alice", "FDING", "qiankun_ding")
        for i in range(1, 8):  # 乾坤鼎 + 7件 = 8件
            self.seed_item("alice", f"FITEM{i}", "qingfeng_jian")
        self.assertEqual(8, len(self.inventory("alice")))

        self.seed_player("bob", "白墨", stones=1000, cultivation=10)
        self.seed_item("bob", "FBOB1", "qingfeng_jian")

        # bob 挑战持有 8 件法宝的 alice：由于 alice 有乾坤鼎，挑战成立！
        self.assertEqual(1, self.send("#斗法 @青玄", "bob", mentioned_ids=("alice",), mention_state="explicit_other"))
        self.assertIn("发起斗法", self.last_reply())

    # 9. 覆海印【涌现】：#突破 灵石减少 10%
    def test_fuhai_yin_reduces_breakthrough_stones(self) -> None:
        # 炼气突破至筑基，原需 100 修为、100 灵石；覆海印减免 10% 后只需 90 灵石
        self.seed_player("alice", "青玄", stones=90, cultivation=100, realm="qi")
        self.seed_item("alice", "FFUHAI", "fuhai_yin")
        self.send("#突破", "alice")
        reply = self.last_reply()
        self.assertIn("破境成功，踏入【筑基】", reply)
        self.assertIn("覆海印【涌现】减免 10% 灵石", reply)
        alice = self.player("alice")
        self.assertEqual("foundation", alice["realm"])
        self.assertEqual(0, alice["spirit_stones"])

    # 10. 镇魂铃【定魄】：斗法被紫霄神雷劈中时，30% 免除 20 修为扣除
    def test_zhenhun_ling_immune_duel_cultivation_loss(self) -> None:
        self.seed_player("alice", "青玄", stones=300, cultivation=100)
        self.seed_item("alice", "FZHENHUN", "zhenhun_ling")
        self.seed_player("bob", "白墨", stones=300, cultivation=100)
        self.seed_item("bob", "FBOB", "qingfeng_jian")

        self.send("#斗法 @青玄", "bob", mentioned_ids=("alice",), mention_state="explicit_other")
        duel_id = self.store.db.execute("SELECT duel_id FROM game_duels WHERE state='inviting'").fetchone()["duel_id"]
        self.accept_latest_prompt()
        self.router.before_messages(self.session, now=self.now)

        self.send("#接受斗法", "alice")
        self.accept_latest_prompt()
        self.router.before_messages(self.session, now=self.now)

        for _ in range(8):
            self.now += 4
            self.router.tick(self.session, now=self.now)

        self.accept_latest_prompt()
        self.router.before_messages(self.session, now=self.now)

        # 设置紫霄神雷位置为 2，bob(turn 1) 未中雷，alice(turn 2) 中雷
        self.store.db.execute("UPDATE game_duels SET lightning_position=2, next_turn=1 WHERE duel_id=?", (duel_id,))
        self.store.db.commit()

        self.send("#引雷", "bob")
        self.accept_latest_prompt()
        self.router.before_messages(self.session, now=self.now)

        # 设置 rng 掷骰：镇魂铃 roll 10 (< 30) 成功免疫修为扣除
        self.rng.randrange_results = [10]
        self.send("#引雷", "alice")
        reply = self.last_reply()
        self.assertIn("【镇魂铃 · 定魄】神铃清响镇住神魂，青玄免除 20 点修为扣除！", reply)
        # alice 修为仍为 100
        self.assertEqual(100, self.player("alice")["cultivation"])

    # 11. 至宝 万魂幡【噬魂摄宝】：斗法获胜夺取败者法宝时，25% 概率额外从秘境卷出普通法器
    def test_wanhun_fan_extra_loot_on_duel_win(self) -> None:
        self.seed_player("bob", "白墨", stones=300, cultivation=100)
        self.seed_item("bob", "FWANHUN", "wanhun_fan")
        self.seed_player("alice", "青玄", stones=300, cultivation=100)
        self.seed_item("alice", "FSWORD", "qingfeng_jian")

        self.send("#斗法 @青玄", "bob", mentioned_ids=("alice",), mention_state="explicit_other")
        duel_id = self.store.db.execute("SELECT duel_id FROM game_duels WHERE state='inviting'").fetchone()["duel_id"]
        self.accept_latest_prompt()
        self.router.before_messages(self.session, now=self.now)

        self.send("#接受斗法", "alice")
        self.accept_latest_prompt()
        self.router.before_messages(self.session, now=self.now)

        for _ in range(8):
            self.now += 4
            self.router.tick(self.session, now=self.now)

        self.accept_latest_prompt()
        self.router.before_messages(self.session, now=self.now)

        # 设置紫霄神雷位置为 1，challenger(bob) 本场若是 turn 2 中雷则 bob 输，这里设 bob 为胜者：alice 引雷中雷
        # challenger bob 引雷先走，避开；alice 引雷中雷落败
        self.store.db.execute("UPDATE game_duels SET lightning_position=2, next_turn=1 WHERE duel_id=?", (duel_id,))
        self.store.db.commit()

        self.send("#引雷", "bob")
        self.accept_latest_prompt()
        self.router.before_messages(self.session, now=self.now)

        # alice 引雷中雷，bob 获胜。设置万魂幡 roll 10 (< 25) 触发噬魂摄宝
        self.rng.randrange_results = [10]
        self.send("#引雷", "alice")
        reply = self.last_reply()
        self.assertIn("【万魂幡 · 噬魂摄宝】阴风卷动，从秘境额外卷出普通法器", reply)
        # bob 原有 1 件(万魂幡)，赢得 alice 1 件(青锋剑)，万魂幡再卷出 1 件，共 3 件
        self.assertEqual(3, len(self.inventory("bob")))

    def support(self, player: str, target: str, amount: int) -> int:
        return self.send(f"#支持 @{target}\u2005 {amount}", player, mentioned_ids=(target,), mention_state="explicit_other")

    # 12. 青锋剑 / 松纹剑【剑意】：斗法围观支持胜利有 25% 概率获取两倍灵石
    def test_sword_doubles_support_payout(self) -> None:
        self.seed_player("bob", "白墨", stones=300)
        self.seed_item("bob", "FBOB", "qingfeng_jian")
        self.seed_player("alice", "青玄", stones=300)
        self.seed_item("alice", "FALICE", "qingfeng_jian")
        # 围观者 charlie 持有松纹剑
        self.seed_player("charlie", "长生", stones=300)
        self.seed_item("charlie", "FSONGWEN", "songwen_jian")
        # 围观者 dave 无剑
        self.seed_player("dave", "大卫", stones=300)

        self.send("#斗法 @青玄", "bob", mentioned_ids=("alice",), mention_state="explicit_other")
        duel_id = self.store.db.execute("SELECT duel_id FROM game_duels WHERE state='inviting'").fetchone()["duel_id"]
        self.accept_latest_prompt()
        self.router.before_messages(self.session, now=self.now)

        self.send("#接受斗法", "alice")
        self.accept_latest_prompt()
        self.router.before_messages(self.session, now=self.now)

        # 围观支持阶段：charlie 支持 bob 50 灵石，dave 支持 alice 50 灵石
        self.assertEqual(1, self.support("charlie", "bob", 50))
        self.assertEqual(1, self.support("dave", "alice", 50))

        for _ in range(8):
            self.now += 4
            self.router.tick(self.session, now=self.now)

        self.accept_latest_prompt()
        self.router.before_messages(self.session, now=self.now)

        # alice 中雷落败，bob 获胜
        self.store.db.execute("UPDATE game_duels SET lightning_position=2, next_turn=1 WHERE duel_id=?", (duel_id,))
        self.store.db.commit()

        self.send("#引雷", "bob")
        self.accept_latest_prompt()
        self.router.before_messages(self.session, now=self.now)

        # charlie 支持 bob 获胜。正常本金 50 + 赢 50 = 100 灵石。
        # charlie 持有松纹剑，roll 5 (< 25) 触发【剑意】，灵石翻倍至 200！
        self.rng.randrange_results = [5]
        self.send("#引雷", "alice")

        # charlie 初始 300 - 50 = 250，到账 200，最终应为 450 灵石
        self.assertEqual(450, self.player("charlie")["spirit_stones"])

    # 13. 发送 #法宝 F编号 展示神通说明
    def test_artifact_detail_displays_ability(self) -> None:
        self.seed_player("alice", "青玄", stones=300)
        self.seed_item("alice", "FJUQI01", "juqi_hulu")
        self.send("#法宝 FJUQI01", "alice")
        reply = self.last_reply()
        self.assertIn("✨ 神通【纳灵】：每日 #修炼 时，额外凝聚灵气，额外获得 +10 灵石 与 +2 修为。", reply)

    # 14. UI 卡片渲染测试：持有乾坤鼎与 8 件法宝正常渲染无异常
    def test_render_profile_card_with_qiankun_ding(self) -> None:
        from wechat_receiver.games.renderer import render_profile_card
        player = {"dao_name": "青玄", "realm": "foundation", "cultivation": 200, "spirit_stones": 500}
        # 8 件法宝
        inv = [{"item_id": "FDING", "template_id": "qiankun_ding", "rarity": "ancient", "name": "乾坤鼎"}]
        for i in range(1, 8):
            inv.append({"item_id": f"F{i}", "template_id": "qingfeng_jian", "rarity": "artifact", "name": "青锋剑"})
        daily_info = {"cultivated": True, "explored": False, "self_cultivate_remaining": 0, "duel_status": ""}

        # 渲染 8 件背包，应正常生成图片，且无超限报错
        image_path = render_profile_card(player, inv, daily_info, rules=asdict(DEFAULT_GAME_CONFIG))
        self.assertTrue(Path(image_path).exists())
        self.assertTrue(Path(image_path).stat().st_size > 1000)

    # 15. 法宝帮助指令：返回所有法宝神通效果，未注册玩家也能查看
    def test_artifact_help_returns_all_abilities(self) -> None:
        from wechat_receiver.games.catalog import CATALOG, format_artifact_help

        # 未注册玩家发送 #法宝帮助
        self.send("#法宝帮助", "stranger")
        reply = self.last_reply()
        self.assertIn("📖【大爱仙途 · 法宝神通全录】", reply)

        # 校验 CATALOG 中所有带 ability_name 的法宝均在帮助文本中
        for template in CATALOG.values():
            if template.ability_name:
                self.assertIn(template.name, reply, f"Artifact {template.name} missing from help")
                self.assertIn(template.ability_name, reply, f"Ability {template.ability_name} missing from help")

        # 各种别名指令均能正确触发
        for alias in ("#法宝 帮助", "#法宝效果", "#法宝 效果", "#法宝神通", "#法宝 help"):
            with self.subTest(alias=alias):
                self.send(alias, "stranger")
                self.assertEqual(reply, self.last_reply())


if __name__ == "__main__":
    unittest.main()
