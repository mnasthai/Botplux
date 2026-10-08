"""Comprehensive tests for the Devil's Bargain (九幽魔契) system."""
from __future__ import annotations

from dataclasses import asdict, replace
from datetime import datetime, timezone
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest

from wechat_receiver.games import duels, service
from wechat_receiver.games.commands import Command, parse_command
from wechat_receiver.games.config import DEFAULT_GAME_CONFIG
from wechat_receiver.models import Message
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
        return sequence[self.choice_index % len(sequence)]


class XiuxianDevilContractTests(unittest.TestCase):
    account = "wxid_bot"
    group = "22913213991@chatroom"
    session = "test_session"
    base = datetime(2026, 9, 23, 10, 0, tzinfo=timezone.utc)

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.store = Store(Path(self.temporary.name) / "messages.sqlite3")
        self.now = self.base.timestamp()
        game_config = replace(DEFAULT_GAME_CONFIG, duel_enabled=True)
        self.config = SimpleNamespace(
            sender=SimpleNamespace(account_id=self.account, allowed_targets=frozenset({self.group})),
            enabled_plugins=("xiuxian",), reply_ttl_seconds=60, max_message_age_seconds=120,
            chatrooms={self.group: SimpleNamespace(rules=SimpleNamespace(admin_ids=(), mention_all_level=0))},
            game_config=game_config,
        )
        self.rng = ControllableRng()
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
        try:
            self.temporary.cleanup()
        except PermissionError:
            pass

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

    def seed_player(self, player_id: str, name: str, stones: int = 100, cultivation: int = 0, realm: str = "qi",
                    tier: int = 0, settled_on: str = "2026-09-23", signed_on: str | None = None) -> None:
        self.store.db.execute(
            """INSERT INTO game_players (account_id, group_id, player_id, dao_name, dao_name_key,
                                         spirit_stones, cultivation, realm, devil_contract_tier,
                                         devil_last_settled_on, devil_signed_on, joined_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (self.account, self.group, player_id, name, name.casefold(), stones, cultivation, realm, tier,
             settled_on, signed_on, datetime.fromtimestamp(self.now, timezone.utc).isoformat())
        )
        self.store.db.commit()

    # 1. 契约指令解析测试
    def test_devil_contract_commands_parsing(self) -> None:
        def m(text: str) -> Message:
            return self.message(text)

        cases = {
            "#魔契": Command("devil_status"),
            "#魔鬼交易": Command("devil_status"),
            "#魔契帮助": Command("devil_help"),
            "#魔鬼交易帮助": Command("devil_help"),
            "#魔契 帮助": Command("devil_help"),
            "#魔鬼交易 帮助": Command("devil_help"),
            "#签订魔契": Command("devil_sign"),
            "#签订契约": Command("devil_sign"),
            "#深化魔契": Command("devil_sign"),
            "#魔契 签订": Command("devil_sign"),
            "#魔契 深化": Command("devil_sign"),
        }
        for cmd, expected in cases.items():
            with self.subTest(cmd=cmd):
                self.assertEqual(expected, parse_command(m(cmd)))

        # Usage boundaries
        self.assertEqual(Command("usage", "#签订魔契"), parse_command(m("#签订魔契 多余")))
        self.assertEqual(Command("usage", "#魔契帮助"), parse_command(m("#魔契帮助 123")))

    # 2. 魔契帮助指令测试
    def test_devil_help_text(self) -> None:
        self.send("#魔契帮助", "stranger")
        reply = self.last_reply()
        self.assertIn("📖【大爱仙途 · 与魔鬼的契约】", reply)
        self.assertIn("与魔鬼立约，各取所需，花费修为，获得大量财富！", reply)
        self.assertIn("• 花费修为与魔鬼签订契约可以获得灵石，如果修为不足则无法签订契约。", reply)
        self.assertIn("• 契约共可深化 5 层，每次深化均可获得更加丰厚的灵石奖励。", reply)
        self.assertIn("• 最高境界，不可签订魔契。", reply)
        self.assertIn("永久记录历史最高签订层数", reply)
        self.assertIn("历史最高层的下一层", reply)
        self.assertNotIn("第 1 层", reply)
        self.assertNotIn("每日反噬", reply)
        self.assertNotIn("境界跌落", reply)

    # 3. 未签约状态查看
    def test_devil_status_initial(self) -> None:
        self.seed_player("alice", "青玄", stones=100)
        self.send("#魔契", "alice")
        reply = self.last_reply()
        self.assertIn("道心清明（未签魔契）", reply)
        self.assertIn("首次签约（第 1 层）：花费自身修为，立即获得 💎 +300 灵石", reply)
        self.assertNotIn("次日起每日强扣", reply)

    # 4. 签订与逐步深化魔契至第 5 层
    def test_sign_and_deepen_contract(self) -> None:
        # alice 初始修为 50（< 80 突破线），签约第 1 层当场扣除 10%*1 突破修为（10 点），剩余 40 点
        self.seed_player("alice", "青玄", stones=100, cultivation=50)

        # 签订第 1 层
        self.send("#签订魔契", "alice")
        reply = self.last_reply()
        self.assertIn("🩸【契约签订】", reply)
        self.assertIn("💎 灵石 +300", reply)
        self.assertIn("魔契等级：第 1 层", reply)
        self.assertIn("签订契约需要扣除修为，修为 -10（剩余：40）", reply)
        self.assertIn("魔鬼不求回报，心满意足的离开了\n\n吗？\n「拿起刀的，终死于刀下。」", reply)
        p = self.player("alice")
        self.assertEqual(400, p["spirit_stones"])
        self.assertEqual(40, p["cultivation"])
        self.assertEqual(1, p["devil_contract_tier"])
        self.assertEqual(300, p["devil_total_borrowed"])
        self.assertEqual("2026-09-23", p["devil_signed_on"])

        # 查看个人面板包含魔契状态
        self.send("#修仙", "alice")
        self.assertIn("👹 魔契状态：第 1 层（每日 -10 修为）", self.last_reply())

        # 深化第 2 层（当前 40 修为，扣 10%*2=20，剩余 20）
        self.send("#深化魔契", "alice")
        self.assertIn("魔契等级：第 2 层 / 共 5 层", self.last_reply())
        self.assertIn("签订契约需要扣除修为，修为 -20（剩余：20）", self.last_reply())
        self.assertIn("魔鬼不求回报，心满意足的离开了\n\n吗？\n「拿起刀的，终死于刀下。」", self.last_reply())
        p = self.player("alice")
        self.assertEqual(400 + 600, p["spirit_stones"])
        self.assertEqual(20, p["cultivation"])
        self.assertEqual(2, p["devil_contract_tier"])
        self.assertEqual(300 + 600, p["devil_total_borrowed"])

        # 尝试在修为不足时深化第 3 层（当前 20 修为，需 30）
        self.send("#深化魔契", "alice")
        self.assertEqual("修为过于孱弱，魔鬼不屑地离开了，本次未能立下契约", self.last_reply())
        self.assertEqual(2, self.player("alice")["devil_contract_tier"])

        # 闭关获得修为（提升至 60），深化第 3 层（扣 30，剩余 30）
        self.store.db.execute("UPDATE game_players SET cultivation=60 WHERE player_id='alice'")
        self.store.db.commit()
        self.send("#深化魔契", "alice")
        self.assertEqual(3, self.player("alice")["devil_contract_tier"])
        self.assertEqual(30, self.player("alice")["cultivation"])

        # 修为提升至 70（< 80 突破线），深化第 4 层（扣 40，剩余 30）
        self.store.db.execute("UPDATE game_players SET cultivation=70 WHERE player_id='alice'")
        self.store.db.commit()
        self.send("#深化魔契", "alice")
        self.assertEqual(4, self.player("alice")["devil_contract_tier"])
        self.assertEqual(30, self.player("alice")["cultivation"])

        # 修为提升至 70（< 80 突破线），深化第 5 层（扣 50，剩余 20）
        self.store.db.execute("UPDATE game_players SET cultivation=70 WHERE player_id='alice'")
        self.store.db.commit()
        self.send("#深化魔契", "alice")
        self.assertEqual(5, self.player("alice")["devil_contract_tier"])
        self.assertEqual(20, self.player("alice")["cultivation"])

        # 尝试再次深化第 6 层（已被阻止）
        self.send("#深化魔契", "alice")
        self.assertIn("魔契已深达极渊第 5 层", self.last_reply())
        self.assertEqual(5, self.player("alice")["devil_contract_tier"])

    # 5. 元婴期修士不可签订魔契
    def test_nascent_cannot_sign(self) -> None:
        self.seed_player("alice", "青玄老祖", stones=100, cultivation=500, realm="nascent")
        self.send("#签订魔契", "alice")
        reply = self.last_reply()
        self.assertIn("元婴大能道心圆满，心魔不侵", reply)
        self.assertEqual(0, self.player("alice")["devil_contract_tier"])

    # 6. 修炼修为扣除：每次 #修炼 结算一次，平常跨天交互不会被动扣除
    def test_daily_cultivation_decay(self) -> None:
        # 第 2 层按当前配置扣除修为；跨天查看面板不触发结算。
        loss = self.config.game_config.devil_daily_losses[1]
        gain = self.config.game_config.cultivation_reward - loss
        self.seed_player("alice", "青玄", stones=100, cultivation=200, tier=2, settled_on="2026-09-23")

        # 时间推进 1 天（24 小时后），平常发送 #修仙 查看面板：不会被动扣除修为！
        self.now += 86400
        self.send("#修仙", "alice")
        reply = self.last_reply()
        self.assertNotIn("心魔反噬", reply)
        p = self.player("alice")
        self.assertEqual(200, p["cultivation"])

        # 只有每日修炼触发收益和心魔扣除。
        self.send("#修炼", "alice")
        reply_cult = self.last_reply()
        self.assertIn(f"👹【心魔反噬】魔契第 2 层发难，抽吸 -{loss} 修为！", reply_cult)
        self.assertIn(f"净增修为：+{gain}", reply_cult)
        p = self.player("alice")
        self.assertEqual(200 + gain, p["cultivation"])
        self.assertEqual(200, p["spirit_stones"])
        self.assertEqual("2026-09-24", p["devil_last_settled_on"])

        # 同一天内再次发送 #修炼，提示今日已修炼过
        self.send("#修炼", "alice")
        self.assertIn("今日已经修炼过了", self.last_reply())
        self.assertEqual(200 + gain, self.player("alice")["cultivation"])

        # 时间再推进 1 天（24 小时后）
        self.now += 86400
        self.send("#修炼", "alice")
        reply2 = self.last_reply()
        self.assertIn(f"👹【心魔反噬】魔契第 2 层发难，抽吸 -{loss} 修为！", reply2)
        self.assertEqual(200 + gain * 2, self.player("alice")["cultivation"])

    # 7. 境界跌落：金丹 ➔ 筑基（#修炼 时修为扣减至 <= 0 触发道基崩塌）
    def test_realm_demotion_core_to_foundation(self) -> None:
        # bob 金丹期，修为 5，第 4 层魔契（抽吸 75 点，5 + 50 - 75 = -20 <= 0）
        self.seed_player("bob", "百里", stones=100, cultivation=5, realm="core", tier=4, settled_on="2026-09-23")

        self.send("#修炼", "bob")
        reply = self.last_reply()
        self.assertIn("💥【道基崩塌 · 心魔反噬】", reply)
        self.assertIn("境界跌落至【筑基】，修为保留为跌落境界的一半（125/250）！", reply)

        p = self.player("bob")
        self.assertEqual("foundation", p["realm"])
        self.assertEqual(125, p["cultivation"])
        self.assertEqual(200, p["spirit_stones"])
        # 魔契依然有效
        self.assertEqual(4, p["devil_contract_tier"])

    # 8. 境界跌落：筑基 ➔ 炼气，魔契保留，重新突破到筑基后心魔消除
    def test_realm_demotion_foundation_to_qi(self) -> None:
        # charlie 筑基期，修为 5，第 4 层魔契（抽吸 75 点，5 + 50 - 75 = -20 <= 0）
        self.seed_player("charlie", "长生", stones=100, cultivation=5, realm="foundation", tier=4, settled_on="2026-09-23")

        self.send("#修炼", "charlie")
        reply = self.last_reply()
        self.assertIn("💥【道基崩塌 · 心魔反噬】", reply)
        self.assertIn("境界跌落至【炼气】，修为保留为跌落境界的一半（50/100）！", reply)

        p = self.player("charlie")
        self.assertEqual("qi", p["realm"])
        self.assertEqual(50, p["cultivation"])
        self.assertEqual(4, p["devil_contract_tier"])

        # 跌落后，玩家下一次突破变为从【炼气】突破回【筑基】
        # 补满修为和灵石后突破：
        self.store.db.execute("UPDATE game_players SET cultivation=100, spirit_stones=100 WHERE player_id='charlie'")
        self.store.db.commit()
        self.send("#突破", "charlie")
        self.assertIn("踏入【筑基】", self.last_reply())
        self.assertIn("⚡【天雷涤魂】破境雷劫九天降临，涤尽心魔！魔契已彻底破除！", self.last_reply())
        self.assertEqual(0, self.player("charlie")["devil_contract_tier"])

    # 9. 炼气期修为降至 0：触发【道心彻底崩溃】，移除所有修为、灵石、法宝，随后移除心魔
    def test_dao_heart_collapse_at_qi_removes_everything_and_devil(self) -> None:
        # alice 处于炼气期，持有灵石 500，持有 2 件法宝，修为 5，第 4 层魔契（抽吸 75 点，5 + 50 - 75 = -20 <= 0）
        self.seed_player("alice", "青玄", stones=500, cultivation=5, realm="qi", tier=4, settled_on="2026-09-23")
        # 放入法宝
        self.store.db.execute(
            """INSERT INTO game_items (item_id, account_id, group_id, template_id, rarity, owner_player_id, state)
               VALUES ('FTEST01', ?, ?, 'qingfeng_jian', 'artifact', 'alice', 'held'),
                      ('FTEST02', ?, ?, 'bichen_zhu', 'artifact', 'alice', 'held')""",
            (self.account, self.group, self.account, self.group)
        )
        self.store.db.commit()

        self.send("#修炼", "alice")
        reply = self.last_reply()
        self.assertIn("💥💥💥【道心彻底崩溃】", reply)
        self.assertIn("修为散尽归零，灵石全失归零，所有法宝尽数离体破散！", reply)
        self.assertIn("魔契已彻底解除", reply)

        p = self.player("alice")
        self.assertEqual("qi", p["realm"])
        self.assertEqual(0, p["cultivation"])
        self.assertEqual(0, p["spirit_stones"])
        self.assertEqual(0, p["devil_contract_tier"])
        self.assertIsNone(p["devil_last_settled_on"])

        # 验证所有法宝均已离体（不再是 held 状态）
        items = self.store.db.execute("SELECT * FROM game_items WHERE owner_player_id='alice' AND state='held'").fetchall()
        self.assertEqual(0, len(items))

    # 9.1 自主修炼走火入魔：50% 概率触发心魔加剧反噬（损失放大为 (1 + tier) 倍）
    def test_self_cultivate_failure_devil_flare(self) -> None:
        # alice 签了第 2 层魔契（成数 = 2，倍率 3 倍），修为 100
        self.seed_player("alice", "青玄", stones=100, cultivation=100, tier=2, settled_on="2026-09-23")

        # Case 1: 走火入魔失败，且 50% 概率触发心魔加剧（第 1 个随机数 99 导致自主修炼失败，第 2 个随机数 20 触发心魔加剧 < 50）
        self.rng.randrange_results = [99, 20]
        self.send("#自主修炼", "alice")
        reply1 = self.last_reply()
        self.assertIn("走火入魔", reply1)
        self.assertIn("👹【心魔引动】魔契第 2 层（2成）借机发难，反噬加剧为 3 倍！", reply1)
        self.assertIn("📉 修为 -6", reply1)  # 2 * 3 = 6
        self.assertEqual(94, self.player("alice")["cultivation"])

        # 时间推进冷却后再次自主修炼
        self.now += 3600
        # Case 2: 走火入魔失败，但 50% 概率未触发心魔加剧（第 1 个随机数 99 失败，第 2 个随机数 70 未触发 >= 50）
        self.rng.randrange_results = [99, 70]
        self.send("#自主修炼", "alice")
        reply2 = self.last_reply()
        self.assertIn("走火入魔", reply2)
        self.assertNotIn("【心魔引动】", reply2)
        self.assertIn("📉 修为 -2", reply2)  # 原原本本的 2 点
        self.assertEqual(92, self.player("alice")["cultivation"])

    # 9.2 自主修炼走火入魔且修为归零：金丹期触发【道基崩塌】跌落至筑基
    def test_self_cultivate_failure_causes_realm_demotion_when_cultivation_depleted(self) -> None:
        self.seed_player("bob", "百里", stones=300, cultivation=2, realm="core", tier=2, settled_on="2026-09-23")
        # 走火入魔失败，且触发心魔引动（损失 2 * 3 = 6，2 - 6 = -4 <= 0）
        self.rng.randrange_results = [99, 20]
        self.send("#自主修炼", "bob")
        reply = self.last_reply()
        self.assertIn("走火入魔", reply)
        self.assertIn("💥【道基崩塌 · 心魔反噬】", reply)
        self.assertIn("境界跌落至【筑基】", reply)
        self.assertIn("修为保留为跌落境界的一半（125/250）", reply)

        p = self.player("bob")
        self.assertEqual("foundation", p["realm"])
        self.assertEqual(125, p["cultivation"])
        self.assertEqual(300, p["spirit_stones"])
        self.assertEqual(2, p["devil_contract_tier"])

    # 9.3 自主修炼走火入魔且修为归零：炼气期触发【道心彻底崩溃】，清除法宝、灵石并解除魔契
    def test_self_cultivate_failure_causes_dao_heart_collapse_at_qi_realm(self) -> None:
        self.seed_player("alice", "青玄", stones=500, cultivation=1, realm="qi", tier=1, settled_on="2026-09-23")
        self.store.db.execute(
            """INSERT INTO game_items (item_id, account_id, group_id, template_id, rarity, owner_player_id, state)
               VALUES ('FTEST01', ?, ?, 'qingfeng_jian', 'artifact', 'alice', 'held')""",
            (self.account, self.group)
        )
        self.store.db.commit()

        # 走火入魔失败，未触发心魔引动（损失 2，1 - 2 = -1 <= 0）
        self.rng.randrange_results = [99, 80]
        self.send("#自主修炼", "alice")
        reply = self.last_reply()
        self.assertIn("走火入魔", reply)
        self.assertIn("💥💥💥【道心彻底崩溃】", reply)
        self.assertIn("修为散尽归零，灵石全失归零，所有法宝尽数离体破散！", reply)
        self.assertIn("魔契已彻底解除", reply)

        p = self.player("alice")
        self.assertEqual("qi", p["realm"])
        self.assertEqual(0, p["cultivation"])
        self.assertEqual(0, p["spirit_stones"])
        self.assertEqual(0, p["devil_contract_tier"])

        items = self.store.db.execute("SELECT * FROM game_items WHERE owner_player_id='alice' AND state='held'").fetchall()
        self.assertEqual(0, len(items))

    # 9.4 扰心符必定走火入魔导致修为归零时，同样触发道心彻底崩溃
    def test_self_cultivate_failure_with_raoxin_fu_causes_collapse(self) -> None:
        self.seed_player("alice", "青玄", stones=200, cultivation=1, realm="qi", tier=1, settled_on="2026-09-23")
        self.seed_player("bob", "百里", stones=100, cultivation=50, realm="qi")
        self.store.db.execute(
            """INSERT INTO game_player_debuffs (debuff_id, account_id, group_id, target_player_id, debuff_kind, caster_player_id, created_at)
               VALUES ('DEB01', ?, ?, 'alice', 'raoxin_fu', 'bob', '2026-09-23T10:00:00Z')""",
            (self.account, self.group)
        )
        self.store.db.commit()

        # 扰心符爆发，走火入魔（损失 2，1 - 2 = -1 <= 0）
        self.rng.randrange_results = [80]
        self.send("#自主修炼", "alice")
        reply = self.last_reply()
        self.assertIn("【自主修炼 · 走火入魔】", reply)
        self.assertIn("扰心符", reply)
        self.assertIn("💥💥💥【道心彻底崩溃】", reply)
        self.assertEqual(0, self.player("alice")["cultivation"])
        self.assertEqual(0, self.player("alice")["spirit_stones"])
        self.assertEqual(0, self.player("alice")["devil_contract_tier"])

    # 10. 突破大境界涤除心魔，彻底解除魔契
    def test_breakthrough_clears_contract(self) -> None:
        # alice 炼气期，修为 120，灵石 200，持有第 2 层魔契，签约于昨日（2026-09-22）
        self.seed_player("alice", "青玄", stones=200, cultivation=120, realm="qi", tier=2,
                         settled_on="2026-09-23", signed_on="2026-09-22")

        self.send("#突破", "alice")
        reply = self.last_reply()
        self.assertIn("踏入【筑基】", reply)
        self.assertIn("⚡【天雷涤魂】破境雷劫九天降临，涤尽心魔！魔契已彻底破除！", reply)

        p = self.player("alice")
        self.assertEqual("foundation", p["realm"])
        self.assertEqual(0, p["devil_contract_tier"])
        self.assertEqual(2, p["devil_max_contract_tier"])
        self.assertIsNone(p["devil_last_settled_on"])
        self.assertIsNone(p["devil_signed_on"])

        # 隔天再次交互，无心魔反噬
        self.now += 86400
        self.send("#修炼", "alice")
        self.assertNotIn("【九幽噬魂】", self.last_reply())

    def test_resigning_after_breakthrough_starts_above_historical_peak(self) -> None:
        self.seed_player('alice', '青玄', stones=1000, cultivation=50)
        for tier in (1, 2):
            self.send('#签订魔契')
            self.assertEqual(tier, self.player()['devil_max_contract_tier'])
        self.store.db.execute("UPDATE game_players SET cultivation=120 WHERE player_id='alice'")
        self.store.db.commit()
        self.now += 86400
        self.send('#突破')
        self.assertEqual((0, 2), (self.player()['devil_contract_tier'],
                                  self.player()['devil_max_contract_tier']))
        self.store.db.execute("UPDATE game_players SET cultivation=100 WHERE player_id='alice'")
        self.store.db.commit()
        before = dict(self.player())
        self.send('#魔契')
        self.assertIn('历史最高签订：第 2 层', self.last_reply())
        self.assertIn('再次签约（第 3 层）', self.last_reply())
        self.assertEqual(before, dict(self.player()))
        self.send('#签订魔契')
        after = self.player()
        self.assertEqual((3, 3), (after['devil_contract_tier'], after['devil_max_contract_tier']))
        self.assertEqual(25, after['cultivation'])  # 筑基门槛 250 × 新层数 3 × 10%。
        self.assertEqual(before['spirit_stones'] + 1200, after['spirit_stones'])
        self.assertIn('魔契等级：第 3 层', self.last_reply())

    def test_collapse_preserves_peak_and_resigning_uses_next_tier(self) -> None:
        self.seed_player('alice', '青玄', stones=500, cultivation=5, tier=4)
        self.send('#修炼')
        self.assertEqual((0, 4), (self.player()['devil_contract_tier'],
                                  self.player()['devil_max_contract_tier']))
        self.store.db.execute("UPDATE game_players SET cultivation=70 WHERE player_id='alice'")
        self.store.db.commit()
        self.send('#签订魔契')
        self.assertEqual((5, 5, 20, 4800), tuple(self.player()[key] for key in
            ('devil_contract_tier', 'devil_max_contract_tier', 'cultivation', 'spirit_stones')))

    def test_cleared_maximum_tier_cannot_sign_again(self) -> None:
        self.seed_player('alice', '青玄', stones=500, cultivation=70)
        self.store.db.execute("UPDATE game_players SET devil_max_contract_tier=5 WHERE player_id='alice'")
        self.store.db.commit()
        before = dict(self.player())
        self.send('#魔契')
        self.assertIn('不可再次签订或深化', self.last_reply())
        self.assertNotIn('首次签约', self.last_reply())
        for command in ('#签订魔契', '#深化魔契'):
            self.send(command)
            self.assertIn('历史签订层数已达上限', self.last_reply())
            self.assertEqual(before, dict(self.player()))
        self.assertEqual(0, self.store.db.execute('SELECT count(*) FROM game_actions').fetchone()[0])

    def test_failed_resign_does_not_consume_the_next_tier(self) -> None:
        self.seed_player('alice', '青玄', stones=100, cultivation=49)
        self.store.db.execute("UPDATE game_players SET devil_max_contract_tier=4 WHERE player_id='alice'")
        self.store.db.commit()
        before = dict(self.player())
        self.send('#签订魔契')
        self.assertIn('修为过于孱弱', self.last_reply())
        self.assertEqual(before, dict(self.player()))
        self.store.db.execute("UPDATE game_players SET cultivation=70, spirit_stones=? WHERE player_id='alice'",
                              (2**63 - 1,))
        self.store.db.commit()
        before = dict(self.player())
        self.send('#签订魔契')
        self.assertIn('灵石已达存储上限', self.last_reply())
        self.assertEqual(before, dict(self.player()))

    def test_replayed_sign_does_not_advance_historical_peak_twice(self) -> None:
        self.seed_player('alice', '青玄', stones=100, cultivation=70)
        message = self.message('#签订魔契')
        self.router.handle(message, self.session, now=self.now)
        before = dict(self.player())
        self.assertEqual(1, before['devil_max_contract_tier'])
        self.router.handle(message, self.session, now=self.now)
        self.assertEqual(before, dict(self.player()))
        self.assertEqual(1, self.store.db.execute('SELECT count(*) FROM game_actions').fetchone()[0])

    # 11. 视觉卡片绘制带有魔契标签测试
    def test_render_profile_card_with_devil_contract(self) -> None:
        from wechat_receiver.games.renderer import render_profile_card

        player = {
            "dao_name": "青玄", "realm": "foundation", "cultivation": 200,
            "spirit_stones": 500, "devil_contract_tier": 2
        }
        daily_info = {"cultivated": True, "explored": False, "self_cultivate_remaining": 0, "duel_status": ""}
        image_path = render_profile_card(player, [], daily_info, rules=asdict(DEFAULT_GAME_CONFIG))
        self.assertTrue(Path(image_path).exists())
        self.assertTrue(Path(image_path).stat().st_size > 1000)

    # 12. 方案二：临近突破门槛（>= 80%）拒贷拦截测试
    def test_scheme2_rejection_at_80_percent_threshold(self) -> None:
        # alice 处于炼气期（突破需要 100 修为），当前修为 80（刚好达到 80%）
        self.seed_player("alice", "青玄", stones=100, cultivation=80, realm="qi")
        self.send("#签订魔契", "alice")
        reply = self.last_reply()
        self.assertIn("魔鬼冷笑一声隐入虚空，本次拒绝签订魔契。", reply)
        self.assertIn("⚠️ 已达突破门槛的 80%（≥80）", reply)
        # 验证玩家未受影响
        p = self.player("alice")
        self.assertEqual(0, p["devil_contract_tier"])
        self.assertEqual(80, p["cultivation"])
        self.assertEqual(100, p["spirit_stones"])

    # 13. 方案一：签约即时扣除 10%*层数 境界突破修为与包装文案测试
    def test_scheme1_instant_cultivation_cost_and_dao_knife_text(self) -> None:
        # bob 筑基期（突破需要 250 修为，第 1 层扣除 10%*1 = 25），当前修为 100（< 200 门槛）
        self.seed_player("bob", "百里", stones=100, cultivation=100, realm="foundation")
        self.send("#签订魔契", "bob")
        reply = self.last_reply()
        self.assertIn("🩸【契约签订】", reply)
        self.assertIn("签订契约需要扣除修为，修为 -25（剩余：75）", reply)
        self.assertIn("魔鬼不求回报，心满意足的离开了\n\n吗？\n「拿起刀的，终死于刀下。」", reply)
        p = self.player("bob")
        self.assertEqual(75, p["cultivation"])
        self.assertEqual(1, p["devil_contract_tier"])
        self.assertEqual("2026-09-23", p["devil_signed_on"])

    # 14. 方案三：签约当日心魔狂暴锁关，次日方可破境除魔测试
    def test_scheme3_breakthrough_locked_on_signing_day_and_unlocked_next_day(self) -> None:
        # charlie 炼气期，初始 50 修为，100 灵石，今日签订第 1 层魔契
        self.seed_player("charlie", "长生", stones=100, cultivation=50, realm="qi")
        self.send("#签订魔契", "charlie")
        # 当日补满突破所需修为 100、灵石 100
        self.store.db.execute("UPDATE game_players SET cultivation=100, spirit_stones=100 WHERE player_id='charlie'")
        self.store.db.commit()

        # 当日尝试突破，触发方案三拦截
        self.send("#突破", "charlie")
        self.assertEqual("👹 心魔正在狂暴肆虐，周天阻塞，道心未稳，今日无法突破境界", self.last_reply())
        p = self.player("charlie")
        self.assertEqual("qi", p["realm"])
        self.assertEqual(1, p["devil_contract_tier"])

        # 时间推进到次日（经历心魔反噬 -10，修为变为 90，日常修炼补满至 140）
        self.now += 86400
        self.send("#修炼", "charlie")
        self.send("#突破", "charlie")
        self.assertIn("踏入【筑基】", self.last_reply())
        self.assertIn("⚡【天雷涤魂】破境雷劫九天降临，涤尽心魔！魔契已彻底破除！", self.last_reply())
        p = self.player("charlie")
        self.assertEqual("foundation", p["realm"])
        self.assertEqual(0, p["devil_contract_tier"])
        self.assertIsNone(p["devil_signed_on"])

    # 15. 修为不足以支付签订所需修为时，魔鬼拒签测试
    def test_devil_sign_rejected_when_cultivation_insufficient(self) -> None:
        # david 处于炼气期（突破需 100 修为，第 1 层签约需要扣除 10%*1 = 10），当前修为仅有 5 点
        self.seed_player("david", "道一", stones=100, cultivation=5, realm="qi")
        self.send("#签订魔契", "david")
        reply = self.last_reply()
        self.assertEqual("修为过于孱弱，魔鬼不屑地离开了，本次未能立下契约", reply)
        # 验证玩家未被扣减修为与灵石，契约层级依然为 0
        p = self.player("david")
        self.assertEqual(0, p["devil_contract_tier"])
        self.assertEqual(5, p["cultivation"])
        self.assertEqual(100, p["spirit_stones"])
        self.assertIsNone(p["devil_signed_on"])


if __name__ == "__main__":
    unittest.main()
