from __future__ import annotations

from collections import Counter
from dataclasses import replace
from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile
from types import MappingProxyType, SimpleNamespace
import unittest

from wechat_receiver.games import service, duels
from wechat_receiver.games.catalog import CATALOG, RARITY_NAMES, REALM_NAMES, format_artifact_help
from wechat_receiver.games.artifact_effects import (daily_cultivation_rewards,
                                                     self_cultivation_success_percent)
from wechat_receiver.games.commands import Command, parse_command
from wechat_receiver.games.config import DEFAULT_GAME_CONFIG
from wechat_receiver.games.shop import find_shop_item, SHOP_ITEMS, PROP_TEMPLATES
from wechat_receiver.models import Message
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


class XiuxianPropsAndNewArtifactsTests(unittest.TestCase):
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

    def seed_player(self, player_id: str, dao_name: str, stones: int = 500, cultivation: int = 50, realm: str = "qi") -> None:
        self.send(f"#修仙 {dao_name}", player_id)
        self.store.db.execute(
            "UPDATE game_players SET spirit_stones=?, cultivation=?, realm=? WHERE player_id=?",
            (stones, cultivation, realm, player_id)
        )
        self.store.db.commit()

    def seed_artifact(self, player_id: str, template_id: str, suffix: str) -> None:
        template = CATALOG[template_id]
        self.store.db.execute(
            "INSERT INTO game_items (item_id, account_id, group_id, template_id, rarity, owner_player_id, state, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, 'held', '2026-09-23T10:00:00Z')",
            (f'FBAL{suffix}', self.account, self.group, template_id, template.rarity, player_id),
        )
        self.store.db.commit()

    def test_new_artifact_daily_and_cost_effects(self):
        self.seed_player('alice', '青玄', stones=1000, cultivation=300)
        for index, template_id in enumerate(('huoyun_pei', 'qinglian_deng', 'zijin_bo',
                                              'xuanyuan_jing', 'xunling_pan')):
            self.seed_artifact('alice', template_id, str(index))
        self.send('#修炼')
        self.assertIn('修为 +66', self.last_reply())
        self.assertIn('灵石 +120', self.last_reply())
        self.send('#修仙')
        self.assertIn('366 / 90', self.last_reply())
        self.assertIn('可探索（需 50 灵石）', self.last_reply())
        self.send('#突破')
        self.assertIn('修为 -90', self.last_reply())
        self.seed_player('bob', '青城', stones=500)
        self.seed_artifact('bob', 'xunling_pan', 'b')
        self.send('#秘境', 'bob')
        self.assertIn('灵石 -50', self.last_reply())

    def test_new_artifact_mining_and_self_cultivation(self):
        self.seed_player('alice', '青玄')
        for index, template_id in enumerate(('lingquan_ping', 'huixin_yu', 'hongmeng_zhu',
                                              'yufeng_shan', 'liuguang_suo')):
            self.seed_artifact('alice', template_id, str(index))
        self.send('#采矿')
        self.assertIn('灵泉瓶【采露】+5', self.last_reply())
        self.send('#自主修炼')
        self.assertIn('修为 +9', self.last_reply())
        self.send('#修仙')
        self.assertIn('1500 秒后可进行', self.last_reply())

    def test_wujie_tu_refunds_artifact_exploration(self):
        self.seed_player('alice', '青玄')
        self.seed_artifact('alice', 'wujie_tu', '0')
        self.send('#秘境')
        self.assertIn('无界图引路，返还 20 灵石', self.last_reply())
        self.assertIn('当前灵石：460', self.last_reply())

    def test_percent_bonuses_are_bounded_and_duplicates_do_not_stack(self):
        inventory = [{'template_id': template_id} for template_id in (
            'shanhe_tu', 'tongtian_bei', 'tongtian_bei', 'juqi_hulu', 'zijin_bo')]
        self.assertEqual((77, 150), daily_cultivation_rewards(50, 100, inventory)[:2])
        self.assertEqual(73, self_cultivation_success_percent(50, [
            {'template_id': 'xuantie_yin'}, {'template_id': 'huixin_yu'}, {'template_id': 'huixin_yu'}]))
        self.assertEqual(95, self_cultivation_success_percent(90, [{'template_id': 'huixin_yu'}]))

    def test_new_rare_artifacts_are_in_exploration_pool(self):
        for player_id, dao_name, roll, template_id in (
            ('alice', '青玄', 80, 'xuanyuan_jing'),
            ('bob', '青城', 95, 'hongmeng_zhu'),
        ):
            self.seed_player(player_id, dao_name)
            self.rng = FixedRng(roll)
            self.rng.choice = lambda pool, wanted=template_id: next(item for item in pool if item.id == wanted)
            self.send('#秘境', player_id)
            self.assertIn(CATALOG[template_id].name, self.last_reply())

    # 1. 验证新法宝模板与帮助信息
    def test_catalog_artifacts_and_help(self):
        self.assertIn("taomu_jian", CATALOG)
        self.assertIn("baiyu_banzhi", CATALOG)
        self.assertIn("jingtie_yin", CATALOG)
        self.assertIn("zijin_bo", CATALOG)
        self.assertIn("taixu_shenzhen", CATALOG)
        self.assertIn("wuxing_qi", CATALOG)

        self.assertEqual("", CATALOG["taomu_jian"].ability_name)
        self.assertEqual("", CATALOG["baiyu_banzhi"].ability_name)
        self.assertEqual("", CATALOG["jingtie_yin"].ability_name)
        self.assertEqual("", CATALOG["qingtong_ling"].ability_name)

        self.assertEqual("霜刃", CATALOG["qingshuang_jian"].ability_name)
        self.assertEqual("破妄", CATALOG["zhaoyao_jing"].ability_name)
        self.assertEqual("龙吟", CATALOG["longwen_gu"].ability_name)
        self.assertEqual("聚灵", CATALOG["zijin_bo"].ability_name)
        self.assertEqual("破气", CATALOG["taixu_shenzhen"].ability_name)
        self.assertEqual("辟易", CATALOG["wuxing_qi"].ability_name)

        help_text = format_artifact_help()
        self.assertIn("青霜剑【霜刃】", help_text)
        self.assertIn("照妖镜【破妄】", help_text)
        self.assertIn("龙纹鼓【龙吟】", help_text)
        self.assertIn("桃木剑：无随身神通；仍计入 #决斗 法宝攻击", help_text)

    # 2. 验证商店中包含道具并能成功购买
    def test_shop_and_buy_props(self):
        self.seed_player("alice", "青玄", stones=1000)
        self.send("#商店", "alice")
        reply = self.last_reply()
        self.assertIn("扰心符", reply)
        self.assertIn("断脉散", reply)
        self.assertIn("清心净衣符", reply)

        # 购买扰心符
        self.send("#购买 扰心符", "alice")
        reply = self.last_reply()
        self.assertIn("🛒【商店 · 购买成功】", reply)
        self.assertIn("扰心符", reply)
        self.assertIn("已放入百宝囊", reply)

        # 查看百宝囊
        self.send("#道具", "alice")
        reply = self.last_reply()
        self.assertIn("🎒【青玄 · 百宝囊】（1/3 格）", reply)
        self.assertIn("扰心符", reply)

    # 3. 验证百宝囊道具上限 3 格拦截
    def test_prop_inventory_limit(self):
        self.seed_player("alice", "青玄", stones=2000)
        self.send("#购买 扰心符", "alice")
        self.send("#购买 扰心符", "alice")
        self.send("#购买 清心净衣符", "alice")
        # 第 4 个应拦截
        self.send("#购买 散灵尘", "alice")
        reply = self.last_reply()
        self.assertIn("百宝囊已满", reply)

    # 4. 验证暗算道具使用及扰心符走火入魔
    def test_use_raoxin_fu_against_target(self):
        self.seed_player("alice", "青玄", stones=1000)
        self.seed_player("bob", "百里", stones=1000, cultivation=100)

        self.send("#购买 扰心符", "alice")
        # 对 bob 使用扰心符
        self.send("#使用 扰心符 @百里", "alice", mentioned_ids=("bob",), mention_state="explicit_other")
        reply = self.last_reply()
        self.assertIn("🩸【暗算得手 · 阴阳咒缚】", reply)
        self.assertIn("青玄", reply)
        self.assertIn("百里", reply)

        # bob 查看面板应显示异常状态
        self.send("#修仙", "bob")
        reply = self.last_reply()
        self.assertIn("受【扰心符】缠身中！", reply)

        # bob 尝试自主修炼，必定走火入魔
        self.send("#自主修炼", "bob")
        reply = self.last_reply()
        self.assertIn("【自主修炼 · 走火入魔】", reply)
        self.assertIn("扰心符", reply)

    # 5. 验证清心净衣符驱散负面诅咒
    def test_qingxin_fu_clears_debuffs(self):
        self.seed_player("alice", "青玄", stones=1000)
        self.seed_player("bob", "百里", stones=1000)

        self.send("#购买 扰心符", "alice")
        self.send("#使用 扰心符 @百里", "alice", mentioned_ids=("bob",), mention_state="explicit_other")

        # bob 购买清心净衣符并使用
        self.send("#购买 清心净衣符", "bob")
        self.send("#使用 清心净衣符", "bob")
        reply = self.last_reply()
        self.assertIn("浩然正气涤荡身心", reply)
        self.assertIn("已成功驱除体内附着的 1 种负面诅咒", reply)

        # 再次查看面板，异常状态消失
        self.send("#修仙", "bob")
        reply = self.last_reply()
        self.assertNotIn("异常状态", reply)

    # 6. 验证散灵尘阻碍境界突破
    def test_sanling_chen_blocks_breakthrough(self):
        self.seed_player("alice", "青玄", stones=1000)
        self.seed_player("bob", "百里", stones=1000, cultivation=100)

        self.send("#购买 散灵尘", "alice")
        self.send("#使用 散灵尘 @百里", "alice", mentioned_ids=("bob",), mention_state="explicit_other")

        # bob 尝试突破
        self.send("#突破", "bob")
        reply = self.last_reply()
        self.assertIn("周身被【散灵尘】死死封锁", reply)
        self.assertIn("无法突破境界", reply)

    def test_sanling_chen_expires_at_beijing_midnight(self):
        self.now = datetime(2026, 9, 23, 15, 59, 59, tzinfo=timezone.utc).timestamp()
        self.seed_player("alice", "青玄", stones=1000)
        self.seed_player("bob", "百里", stones=1000, cultivation=100)
        self.send("#购买 散灵尘", "alice")
        self.send("#使用 散灵尘 @百里", "alice", mentioned_ids=("bob",), mention_state="explicit_other")

        self.send("#突破", "bob")
        self.assertIn("周身被【散灵尘】死死封锁", self.last_reply())
        player = self.store.db.execute("SELECT * FROM game_players WHERE player_id='bob'").fetchone()
        self.assertEqual(("qi", 100, 1000), (player["realm"], player["cultivation"], player["spirit_stones"]))

        # 北京时间跨过零点，UTC 日期尚未改变。
        self.now += 1
        self.send("#突破", "bob")
        self.assertIn("【境界突破】", self.last_reply())
        player = self.store.db.execute("SELECT * FROM game_players WHERE player_id='bob'").fetchone()
        self.assertEqual(("foundation", 0, 900), (player["realm"], player["cultivation"], player["spirit_stones"]))
        self.assertEqual(0, self.store.db.execute(
            "SELECT count(*) FROM game_player_debuffs WHERE target_player_id='bob'"
        ).fetchone()[0])

    def test_sanling_chen_expiry_updates_status_and_preserves_other_debuffs(self):
        self.seed_player("alice", "青玄", stones=2000)
        self.seed_player("bob", "百里", stones=1000)
        self.send("#购买 扰心符", "alice")
        self.send("#使用 扰心符 @百里", "alice", mentioned_ids=("bob",), mention_state="explicit_other")
        self.send("#购买 窃灵蛊", "alice")
        self.send("#使用 窃灵蛊 @百里", "alice", mentioned_ids=("bob",), mention_state="explicit_other")

        for command in ("#修仙", "#道具"):
            with self.subTest(command=command):
                self.send("#购买 散灵尘", "alice")
                self.send("#使用 散灵尘 @百里", "alice", mentioned_ids=("bob",), mention_state="explicit_other")
                self.assertIn("【暗算得手 · 阴阳咒缚】", self.last_reply())
                self.now += 86400
                self.send(command, "bob")
                self.assertNotIn("散灵尘", self.last_reply())
                self.assertIn("扰心符", self.last_reply())
                self.assertIn("窃灵蛊", self.last_reply())
                kinds = {row["debuff_kind"] for row in self.store.db.execute(
                    "SELECT debuff_kind FROM game_player_debuffs WHERE target_player_id='bob'"
                ).fetchall()}
                self.assertEqual({"raoxin_fu", "qieling_gu"}, kinds)

    def test_sanling_chen_can_be_reapplied_after_expiry_without_target_command(self):
        self.seed_player("alice", "青玄", stones=1000)
        self.seed_player("bob", "百里", stones=1000, cultivation=100)
        self.send("#购买 散灵尘", "alice")
        self.send("#购买 散灵尘", "alice")
        self.send("#使用 散灵尘 @百里", "alice", mentioned_ids=("bob",), mention_state="explicit_other")
        original_id = self.store.db.execute(
            "SELECT debuff_id FROM game_player_debuffs WHERE target_player_id='bob'"
        ).fetchone()[0]

        self.send("#使用 散灵尘 @百里", "alice", mentioned_ids=("bob",), mention_state="explicit_other")
        self.assertIn("无法重复施加同种负面效果", self.last_reply())
        self.assertEqual(1, self.store.db.execute(
            "SELECT count(*) FROM game_player_props WHERE player_id='alice'"
        ).fetchone()[0])

        self.now += 86400
        self.send("#使用 散灵尘 @百里", "alice", mentioned_ids=("bob",), mention_state="explicit_other")
        self.assertIn("【暗算得手 · 阴阳咒缚】", self.last_reply())
        rows = self.store.db.execute(
            "SELECT debuff_id FROM game_player_debuffs WHERE target_player_id='bob'"
        ).fetchall()
        self.assertEqual(1, len(rows))
        self.assertNotEqual(original_id, rows[0]["debuff_id"])
        self.assertEqual(0, self.store.db.execute(
            "SELECT count(*) FROM game_player_props WHERE player_id='alice'"
        ).fetchone()[0])
        self.send("#突破", "bob")
        self.assertIn("周身被【散灵尘】死死封锁", self.last_reply())

    # 7. 验证窃灵蛊偷取修炼收益反哺施术者
    def test_qieling_gu_steals_cultivation(self):
        self.seed_player("alice", "青玄", stones=1000, cultivation=100)
        self.seed_player("bob", "百里", stones=1000, cultivation=100)

        self.send("#购买 窃灵蛊", "alice")
        self.send("#使用 窃灵蛊 @百里", "alice", mentioned_ids=("bob",), mention_state="explicit_other")

        # bob 进行每日修炼
        self.send("#修炼", "bob")
        reply = self.last_reply()
        self.assertIn("【窃灵蛊】破茧吸食", reply)
        self.assertIn("青玄", reply)

        # 验证 alice 获得反哺收益（30% 修为 = 15，30% 灵石 = 30）
        p_alice = self.store.db.execute("SELECT * FROM game_players WHERE player_id='alice'").fetchone()
        self.assertEqual(115, p_alice["cultivation"])
        # alice 初始 1000 - 100(购买窃灵蛊) + 30(偷取灵石) = 930
        self.assertEqual(930, p_alice["spirit_stones"])

    # 8. 验证清心净衣符购买概率机制（成功与失败）
    def test_qingxin_fu_chance_buy(self):
        self.seed_player("alice", "青玄", stones=1000)
        # 固定 roll = 60 (>= 50 失败)
        self.rng.rolls = [60]
        self.send("#购买 清心净衣符", "alice")
        reply = self.last_reply()
        self.assertIn("尝试求购护体灵符【清心净衣符】", reply)
        self.assertIn("符纸灵光微散，化为飞灰", reply)
        # 道具未增加
        self.assertEqual(0, self.store.db.execute("SELECT count(*) FROM game_player_props WHERE player_id='alice'").fetchone()[0])

        # 固定 roll = 20 (< 50 成功)
        self.rng.rolls = [20]
        self.send("#购买 清心净衣符", "alice")
        reply = self.last_reply()
        self.assertIn("灵光大炽，吉星高照！", reply)
        self.assertIn("购得秘传符箓【清心净衣符】", reply)
        # 道具增加了 1 个
        self.assertEqual(1, self.store.db.execute("SELECT count(*) FROM game_player_props WHERE player_id='alice'").fetchone()[0])

    # 9. 验证清心净衣符随身携带被动抵消他人暗算
    def test_qingxin_fu_passive_counter_curse(self):
        self.seed_player("alice", "青玄", stones=1000)
        self.seed_player("bob", "百里", stones=1000)

        # bob 求得一张清心净衣符放百宝囊中
        self.rng.rolls = [10]
        self.send("#购买 清心净衣符", "bob")
        self.assertEqual(1, self.store.db.execute("SELECT count(*) FROM game_player_props WHERE player_id='bob'").fetchone()[0])

        # alice 购买扰心符并暗算 bob
        self.send("#购买 扰心符", "alice")
        self.send("#使用 扰心符 @百里", "alice", mentioned_ids=("bob",), mention_state="explicit_other")
        reply = self.last_reply()
        self.assertIn("🛡️【浩然正气 · 替身化劫】", reply)
        self.assertIn("清心净衣符】无风自燃，化作浩然正气金光护体，瞬间抵消了本次暗算", reply)

        # bob 的清心净衣符被自动消耗
        self.assertEqual(0, self.store.db.execute("SELECT count(*) FROM game_player_props WHERE player_id='bob'").fetchone()[0])
        # alice 的扰心符也被消耗
        self.assertEqual(0, self.store.db.execute("SELECT count(*) FROM game_player_props WHERE player_id='alice'").fetchone()[0])
        # bob 身上无 debuff
        self.assertEqual(0, self.store.db.execute("SELECT count(*) FROM game_player_debuffs WHERE target_player_id='bob'").fetchone()[0])

    # 10. 验证照妖镜秘境探索保底灵石提升 25% 与高品质概率加成
    def test_zhaoyao_jing_explore_rewards(self):
        self.seed_player("alice", "青玄", stones=1000)
        # 发放照妖镜给 alice
        from wechat_receiver.games.catalog import CATALOG
        self.store.db.execute(
            "INSERT INTO game_items (item_id, account_id, group_id, template_id, rarity, owner_player_id, state, created_at) "
            "VALUES ('FTEST999', ?, ?, 'zhaoyao_jing', 'ancient', 'alice', 'held', '2026-09-23T10:00:00Z')",
            (self.account, self.group)
        )
        self.store.db.commit()

        # 探索秘境
        self.send("#秘境", "alice")
        reply = self.last_reply()
        self.assertIn("🪞【破妄】照妖镜辨析宝光，提升稀有法宝概率，额外获得 25 灵石！", reply)

    # 11. 验证不同负面道具效果可在同一人身上叠加，且各效果独立触发
    def test_different_negative_props_stack_on_same_player_and_execute_independently(self):
        self.seed_player("alice", "青玄", stones=5000, cultivation=200)
        self.seed_player("bob", "百里", stones=5000, cultivation=200)

        # alice 购买 3 种不同的负面道具
        self.send("#购买 扰心符", "alice")
        self.send("#购买 散灵尘", "alice")
        self.send("#购买 窃灵蛊", "alice")

        # 依次对 bob 施展 扰心符、散灵尘、窃灵蛊（不同负面效果叠加在 bob 身上）
        self.send("#使用 扰心符 @百里", "alice", mentioned_ids=("bob",), mention_state="explicit_other")
        self.assertIn("【暗算得手 · 阴阳咒缚】", self.last_reply())

        self.send("#使用 散灵尘 @百里", "alice", mentioned_ids=("bob",), mention_state="explicit_other")
        self.assertIn("【暗算得手 · 阴阳咒缚】", self.last_reply())

        self.send("#使用 窃灵蛊 @百里", "alice", mentioned_ids=("bob",), mention_state="explicit_other")
        self.assertIn("【暗算得手 · 阴阳咒缚】", self.last_reply())

        # 验证 bob 身上已叠加 3 种不同 debuff
        debuff_rows = self.store.db.execute(
            "SELECT debuff_kind FROM game_player_debuffs WHERE target_player_id='bob' ORDER BY created_at"
        ).fetchall()
        kinds = [r["debuff_kind"] for r in debuff_rows]
        self.assertEqual(["raoxin_fu", "sanling_chen", "qieling_gu"], kinds)

        # bob 查看人物面板，显示叠加的异常状态
        self.send("#修仙", "bob")
        reply = self.last_reply()
        self.assertIn("受【扰心符、散灵尘、窃灵蛊】缠身中！", reply)

        # bob 查看道具背包，显示 3 条独立诅咒记录
        self.send("#道具", "bob")
        reply = self.last_reply()
        self.assertIn("• 受【扰心符】缠身（施术者：青玄）", reply)
        self.assertIn("• 受【散灵尘】缠身（施术者：青玄）", reply)
        self.assertIn("• 受【窃灵蛊】缠身（施术者：青玄）", reply)

        # 验证各效果独立触发：
        # 1) 散灵尘独立生效：阻碍突破
        self.send("#突破", "bob")
        self.assertIn("周身被【散灵尘】死死封锁", self.last_reply())

        # 2) 窃灵蛊独立生效：每日修炼被偷取 30%，窃灵蛊被消耗，其余 debuff 仍在
        self.send("#修炼", "bob")
        self.assertIn("【窃灵蛊】破茧吸食", self.last_reply())
        remaining_debuffs = [r["debuff_kind"] for r in self.store.db.execute(
            "SELECT debuff_kind FROM game_player_debuffs WHERE target_player_id='bob'"
        ).fetchall()]
        self.assertNotIn("qieling_gu", remaining_debuffs)
        self.assertIn("raoxin_fu", remaining_debuffs)
        self.assertIn("sanling_chen", remaining_debuffs)

        # 3) 扰心符独立生效：自主修炼必定走火入魔，扰心符被消耗，散灵尘仍在
        self.send("#自主修炼", "bob")
        self.assertIn("【自主修炼 · 走火入魔】", self.last_reply())
        remaining_debuffs = [r["debuff_kind"] for r in self.store.db.execute(
            "SELECT debuff_kind FROM game_player_debuffs WHERE target_player_id='bob'"
        ).fetchall()]
        self.assertNotIn("raoxin_fu", remaining_debuffs)
        self.assertIn("sanling_chen", remaining_debuffs)

    # 12. 验证同种负面道具不可重复叠加，且不消耗道具
    def test_duplicate_same_prop_cannot_stack_and_does_not_consume_prop(self):
        self.seed_player("alice", "青玄", stones=2000)
        self.seed_player("bob", "百里", stones=2000)

        self.send("#购买 扰心符", "alice")
        self.send("#购买 扰心符", "alice")
        self.assertEqual(2, self.store.db.execute(
            "SELECT count(*) FROM game_player_props WHERE player_id='alice' AND template_id='raoxin_fu'"
        ).fetchone()[0])

        # 第一次施展扰心符成功
        self.send("#使用 扰心符 @百里", "alice", mentioned_ids=("bob",), mention_state="explicit_other")
        self.assertIn("【暗算得手 · 阴阳咒缚】", self.last_reply())
        self.assertEqual(1, self.store.db.execute(
            "SELECT count(*) FROM game_player_props WHERE player_id='alice' AND template_id='raoxin_fu'"
        ).fetchone()[0])

        # 第二次对同一目标施展同种扰心符：拦截且不扣除道具
        self.send("#使用 扰心符 @百里", "alice", mentioned_ids=("bob",), mention_state="explicit_other")
        reply = self.last_reply()
        self.assertIn("体内已有【扰心符】附着生效，无法重复施加同种负面效果！", reply)
        self.assertEqual(1, self.store.db.execute(
            "SELECT count(*) FROM game_player_props WHERE player_id='alice' AND template_id='raoxin_fu'"
        ).fetchone()[0])

        # bob 身上依然只有 1 个扰心符 debuff
        self.assertEqual(1, self.store.db.execute(
            "SELECT count(*) FROM game_player_debuffs WHERE target_player_id='bob'"
        ).fetchone()[0])

    # 13. 验证清心净衣符一次性驱除身上叠加的多种负面效果
    def test_qingxin_fu_clears_multiple_stacked_debuffs(self):
        self.seed_player("alice", "青玄", stones=3000)
        self.seed_player("bob", "百里", stones=3000)

        self.send("#购买 扰心符", "alice")
        self.send("#购买 散灵尘", "alice")
        self.send("#使用 扰心符 @百里", "alice", mentioned_ids=("bob",), mention_state="explicit_other")
        self.send("#使用 散灵尘 @百里", "alice", mentioned_ids=("bob",), mention_state="explicit_other")

        # bob 身上有 2 个 debuff
        self.assertEqual(2, self.store.db.execute(
            "SELECT count(*) FROM game_player_debuffs WHERE target_player_id='bob'"
        ).fetchone()[0])

        # bob 使用清心净衣符
        self.send("#购买 清心净衣符", "bob")
        self.send("#使用 清心净衣符", "bob")
        reply = self.last_reply()
        self.assertIn("已成功驱除体内附着的 2 种负面诅咒", reply)

        # bob 身上 debuff 全部清除
        self.assertEqual(0, self.store.db.execute(
            "SELECT count(*) FROM game_player_debuffs WHERE target_player_id='bob'"
        ).fetchone()[0])


if __name__ == "__main__":
    unittest.main()



