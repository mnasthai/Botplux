"""Unit tests for xiuxian PVP duel system and anti-crush balancing with golden shield."""

import random
import sqlite3
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch
from types import SimpleNamespace

from wechat_receiver.models import Message
from wechat_receiver.games.commands import Command, parse_command
from wechat_receiver.games.config import GameConfig
from wechat_receiver.games.pvp import (
    calculate_fighter_stats,
    roll_attack,
    simulate_duel,
    format_duel_report,
    format_pvp_stats,
    format_pvp_help,
    REALM_DEFENSE,
    REALM_ORDER,
)
from wechat_receiver.games.schema import initialize_game_schema
from wechat_receiver.games.service import handle_command
from wechat_receiver.outbox import initialize_outbox
from wechat_receiver.plugins import LoadedPlugin, reply_request_id
from wechat_receiver.reply_router import ReplyRouter
from wechat_receiver.store import Store


def make_message(content: object, *, group: bool = True, sender_id: str = "member", mention_state: str = "none",
                 mentioned_ids: tuple[str, ...] = (), message_id: str | None = None) -> Message:
    mid = message_id or str(random.randint(10000000, 99999999))
    return Message(
        session_id="s", event_key="s:1", seq=1, call_id=1, source="receive_batch", event_kind="item",
        observed_at_ms=1, message_type=1, message_kind="text", app_message_type=None, content=content,
        raw_content=content if isinstance(content, str) else None,
        conversation_id="room1@chatroom" if group else "wxid_friend", sender_id=sender_id, direction="incoming",
        message_time_candidate=None, message_id_candidate=mid, mentioned_ids=mentioned_ids,
        mention_state=mention_state, history_state="live_candidate",
    )


class DeterministicRNG:
    def __init__(self, sequence):
        self.seq = list(sequence)
        self.idx = 0

    def randrange(self, start, stop=None):
        if stop is None:
            stop = start
            start = 0
        if self.idx < len(self.seq):
            val = self.seq[self.idx]
            self.idx += 1
            return val
        return start


class XiuxianPvpTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        self.db.row_factory = sqlite3.Row
        initialize_game_schema(self.db)
        initialize_outbox(self.db)
        self.config = GameConfig()

    def tearDown(self):
        self.db.close()

    def _create_player(self, player_id, dao_name, realm="qi", cult=50, stones=500):
        self.db.execute(
            """INSERT INTO game_players (account_id, group_id, player_id, dao_name, dao_name_key, realm, cultivation, spirit_stones)
               VALUES ('test_acc', 'room1@chatroom', ?, ?, ?, ?, ?, ?)""",
            (player_id, dao_name, dao_name.lower(), realm, cult, stones),
        )

    def _add_item(self, item_id, owner_id, template_id, rarity="artifact"):
        self.db.execute(
            """INSERT INTO game_items (item_id, account_id, group_id, owner_player_id, template_id, rarity, state, created_at)
               VALUES (?, 'test_acc', 'room1@chatroom', ?, ?, ?, 'held', '2026-09-23T10:00:00Z')""",
            (item_id, owner_id, template_id, rarity),
        )

    def _make_context(self, user_id, content, mention_state="explicit_other", mentioned_ids=None):
        msg = make_message(
            content,
            sender_id=user_id,
            mention_state=mention_state,
            mentioned_ids=tuple(mentioned_ids or ()),
        )
        return SimpleNamespace(
            store=self.db,
            account_id="test_acc",
            conversation_id="room1@chatroom",
            user_id=user_id,
            event_key=f"evt_{user_id}_{random.randint(1000, 9999)}",
            message=msg,
            now=1790150400,
            allowed_targets={"room1@chatroom"},
            connection_id="conn1",
            previous_poll_at=None,
            reply_request_id=lambda k: f"req_{k}",
            game_config=self.config,
        )

    # 1. 三维数值与境界基础血量测试
    def test_calculate_fighter_stats(self):
        # 炼气（50 修为）
        qi_player = {"dao_name": "韩立", "realm": "qi", "cultivation": 50}
        inv = [{"template_id": "qingfeng_jian", "rarity": "artifact"}]
        stats_qi = calculate_fighter_stats(qi_player, inv)

        self.assertEqual(stats_qi["name"], "韩立")
        self.assertEqual(stats_qi["realm"], "qi")
        self.assertEqual(stats_qi["defense"], 20)
        # HP: 100 + floor(50^0.75 * 3) = 100 + floor(18.80 * 3) = 156
        self.assertEqual(stats_qi["hp"], 156)
        self.assertEqual(stats_qi["attack"], 15)

        # 乾坤鼎只扩容；单独持鼎保留徒手基线，混装不削减其它法宝。
        ding = {"template_id": "qiankun_ding", "rarity": "ancient"}
        self.assertEqual(calculate_fighter_stats(qi_player, [ding])["attack"], 15)
        self.assertEqual(calculate_fighter_stats(qi_player, [ding, ding])["attack"], 15)
        self.assertEqual(calculate_fighter_stats(qi_player, [ding, *inv])["attack"], 15)
        protected = calculate_fighter_stats(qi_player, [
            {"template_id": "xuanjia_fu", "rarity": "artifact"},
            {"template_id": "xuanbing_jia", "rarity": "spirit"},
            {"template_id": "tianlei_gu", "rarity": "ancient"},
            {"template_id": "qingshuang_jian", "rarity": "spirit"},
        ])
        self.assertEqual(protected["defense"], 31)
        self.assertEqual(protected["extra_true_damage"], 18)

        # 元婴（800 修为）
        nascent_player = {"dao_name": "极阴祖师", "realm": "nascent", "cultivation": 800}
        stats_nascent = calculate_fighter_stats(nascent_player, [])
        self.assertEqual(stats_nascent["defense"], 80)
        self.assertEqual(stats_nascent["hp"], 1181)
        self.assertGreater(stats_nascent["hp"], stats_qi["hp"])

    # 1.1 至宝境界承载与久不斗法沉眠测试
    def test_treasure_realm_scaling_and_dormancy(self):
        now_iso = "2026-09-23T12:00:00Z"
        treasure_item = {"template_id": "wanhun_fan", "rarity": "treasure", "created_at": now_iso}

        # 炼气：50% = 40 ATK
        p_qi = {"dao_name": "炼气弟子", "realm": "qi"}
        self.assertEqual(calculate_fighter_stats(p_qi, [treasure_item], now_iso=now_iso)["attack"], 40)

        # 筑基：70% = 56 ATK
        p_fd = {"dao_name": "筑基修士", "realm": "foundation"}
        self.assertEqual(calculate_fighter_stats(p_fd, [treasure_item], now_iso=now_iso)["attack"], 56)

        # 金丹：85% = 68 ATK
        p_cr = {"dao_name": "金丹真人", "realm": "core"}
        self.assertEqual(calculate_fighter_stats(p_cr, [treasure_item], now_iso=now_iso)["attack"], 68)

        # 元婴：100% = 80 ATK
        p_ns = {"dao_name": "元婴大能", "realm": "nascent"}
        self.assertEqual(calculate_fighter_stats(p_ns, [treasure_item], now_iso=now_iso)["attack"], 80)

        # 连续拒绝/未响应沉眠测试（连续 3 次触发）：
        # 1~2 次避战：至宝不沉眠，保持 100% 满额威能
        stats_refuse_2 = calculate_fighter_stats(p_ns, [treasure_item], consecutive_refuse_count=2)
        self.assertEqual(stats_refuse_2["attack"], 80)
        self.assertFalse(stats_refuse_2["has_dormant_treasure"])

        # 达到 3 次避战：触发至宝沉眠（整体攻击力减少 60%，剩余 40%）
        stats_dormant_qi = calculate_fighter_stats(p_qi, [treasure_item], consecutive_refuse_count=3)
        # 炼气承载 40 * 剩余 40% = 16 ATK
        self.assertEqual(stats_dormant_qi["attack"], 16)
        self.assertTrue(stats_dormant_qi["has_dormant_treasure"])

        stats_dormant_ns = calculate_fighter_stats(p_ns, [treasure_item], consecutive_refuse_count=3)
        # 元婴承载 80 * 剩余 40% = 32 ATK
        self.assertEqual(stats_dormant_ns["attack"], 32)
        self.assertTrue(stats_dormant_ns["has_dormant_treasure"])

        # 徒手无宝者避战 3 次：基线 15 * 40% = 6 ATK
        stats_dormant_bare = calculate_fighter_stats(p_qi, [], consecutive_refuse_count=3)
        self.assertEqual(stats_dormant_bare["attack"], 6)
        self.assertTrue(stats_dormant_bare["has_dormant_treasure"])

        # 参与斗法唤醒测试（计数重置为 0）：
        stats_woken = calculate_fighter_stats(p_ns, [treasure_item], consecutive_refuse_count=0)
        # 恢复 100% 满额 80 ATK
        self.assertEqual(stats_woken["attack"], 80)
        self.assertFalse(stats_woken["has_dormant_treasure"])

    # 2. 天机骰与【金盾】BUFF测试
    def test_gold_shield_and_great_success_pierce(self):
        # 炼气打元婴（差 3 阶，跨 >= 2 阶），元婴有金盾
        qi_stats = {"name": "韩立", "realm_order": 1, "attack": 100, "defense": 15, "extra_true_damage": 0, "max_hp": 150}
        nascent_stats = {"name": "极阴", "realm_order": 4, "attack": 80, "defense": 75, "extra_true_damage": 0, "max_hp": 550, "gold_shield": True}

        # Case A: 炼气掷出 10 点（常规点数，未达到 19/20 大成功）
        rng_normal = DeterministicRNG([10, 50])
        dmg, dice, desc = roll_attack(qi_stats, nascent_stats, rng_normal)
        self.assertIn("金盾化解15%", desc)
        self.assertEqual(dmg, 45)

        # Case B: 炼气掷出 20 点（大成功，破除金盾！）
        rng_crit = DeterministicRNG([20, 50])
        dmg_crit, dice_crit, desc_crit = roll_attack(qi_stats, nascent_stats, rng_crit)
        self.assertIn("大成功·贯穿金盾", desc_crit)
        self.assertNotIn("金盾化解15%", desc_crit)
        self.assertIn("破体真伤", desc_crit)
        self.assertGreater(dmg_crit, 100)

    # 3. 模拟决斗流程与金盾自动挂载
    def test_simulate_duel_cross_realm_shield(self):
        f1 = calculate_fighter_stats({"dao_name": "小修", "realm": "qi", "cultivation": 50}, [])
        f2 = calculate_fighter_stats({"dao_name": "老祖", "realm": "nascent", "cultivation": 800}, [])
        rng = random.Random(42)
        result = simulate_duel(f1, f2, rng)

        # 验证双方跨 2 阶时，高境界一方确实挂载了金盾
        self.assertFalse(f1.get("gold_shield"))
        self.assertTrue(f2.get("gold_shield"))
        self.assertIn(result["winner"], (1, 2))
        self.assertGreater(len(result["logs"]), 0)

    # 4. 指令解析测试
    def test_commands_parsing(self):
        # #决斗 @群友 金额
        msg = make_message(
            "#决斗 @百里\u2005 100",
            mention_state="explicit_other",
            mentioned_ids=("u2",),
        )
        cmd = parse_command(msg)
        self.assertIsNotNone(cmd)
        self.assertEqual(cmd.kind, "pvp_duel")
        self.assertEqual(cmd.target_id, "u2")
        self.assertEqual(cmd.amount, 100)

        # #接受决斗
        msg_accept = make_message("#接受决斗")
        self.assertEqual(parse_command(msg_accept).kind, "accept_pvp_duel")

        # #继续决斗 / #继续 / #确认决斗
        msg_cont = make_message("#继续决斗")
        self.assertEqual(parse_command(msg_cont).kind, "continue_pvp_duel")
        msg_cont2 = make_message("#继续")
        self.assertEqual(parse_command(msg_cont2).kind, "continue_pvp_duel")

        # #投降 / #认输 / #决斗投降
        msg_surr = make_message("#投降")
        self.assertEqual(parse_command(msg_surr).kind, "surrender_pvp_duel")
        msg_surr2 = make_message("#认输")
        self.assertEqual(parse_command(msg_surr2).kind, "surrender_pvp_duel")

        # #拒绝决斗
        msg_reject = make_message("#拒绝决斗")
        self.assertEqual(parse_command(msg_reject).kind, "reject_pvp_duel")

        # #决斗属性
        msg_stats = make_message("#决斗属性")
        self.assertEqual(parse_command(msg_stats).kind, "pvp_stats")

        # #决斗帮助
        msg_help = make_message("#决斗帮助")
        self.assertEqual(parse_command(msg_help).kind, "pvp_help")

    def test_wager_parsing_accepts_positive_amounts_below_ten(self):
        for amount in (1, 2, 5, 7, 9, 10, 1000):
            with self.subTest(amount=amount):
                command = parse_command(make_message(
                    f'#决斗 @百里\u2005{amount}', mention_state='explicit_other',
                    mentioned_ids=('u2',)))
                self.assertEqual(command.kind, 'pvp_duel')
                self.assertEqual(command.amount, amount)
        for amount in ('0', '-1', '0.5', '1.5', '1001'):
            with self.subTest(amount=amount):
                command = parse_command(make_message(
                    f'#决斗 @百里\u2005{amount}', mention_state='explicit_other',
                    mentioned_ids=('u2',)))
                self.assertEqual(command.kind, 'usage')
                self.assertIn('1～1000', command.argument)

    def test_one_stone_wager_can_be_invited_escrowed_and_settled(self):
        self._create_player('u1', '修士甲', stones=1)
        self._create_player('u2', '修士乙', stones=1)
        invite = self._make_context('u1', '#决斗 @修士乙\u2005 1', mentioned_ids=['u2'])
        result = handle_command(parse_command(invite.message), invite)
        self.assertIn('战书下达', result)
        self.assertEqual(self.db.execute('SELECT wager FROM game_pvp_duels').fetchone()[0], 1)
        accept = self._make_context('u2', '#接受决斗', mention_state='none')
        knockout = {'is_over': True, 'winner': 1, 'round_num': 1,
                    'logs': ['首轮结束'], 'hp1': 100, 'hp2': 0}
        with patch('wechat_receiver.games.service.simulate_duel_round', return_value=knockout):
            result = handle_command(parse_command(accept.message), accept)
        self.assertIn('最终胜者', result)
        duel = self.db.execute('SELECT state, escrowed FROM game_pvp_duels').fetchone()
        self.assertEqual(tuple(duel), ('settled', 1))
        self.assertEqual([r[0] for r in self.db.execute(
            'SELECT spirit_stones FROM game_players ORDER BY player_id')], [2, 0])

    def test_service_rejects_nonpositive_or_excessive_wagers(self):
        self._create_player('u1', '修士甲', stones=2000)
        self._create_player('u2', '修士乙', stones=2000)
        for amount in (None, 0, -1, 1001):
            with self.subTest(amount=amount):
                context = self._make_context('u1', '#决斗', mentioned_ids=['u2'])
                result = handle_command(Command('pvp_duel', target_id='u2', amount=amount), context)
                self.assertIn('正整数', result)
        self.assertEqual(self.db.execute('SELECT count(*) FROM game_pvp_duels').fetchone()[0], 0)
        self.assertEqual([r[0] for r in self.db.execute(
            'SELECT spirit_stones FROM game_players ORDER BY player_id')], [2000, 2000])

    # 5. Service 端全流程：发起决斗（含跨阶警示）、逐轮双向确认推演直至分出胜负、纯灵石结算
    def test_full_service_pvp_duel_lifecycle(self):
        # 创建两名修士：u1（炼气 500灵石）与 u2（元婴 1000灵石，跨 3 阶）
        self._create_player("u1", "韩立", realm="qi", cult=80, stones=500)
        self._create_player("u2", "极阴", realm="nascent", cult=800, stones=1000)
        self._add_item("F101", "u1", "qingshuang_jian", "spirit")

        # A. u1 向 u2 发起决斗（跨阶挑战）
        ctx_invite = self._make_context("u1", "#决斗 @极阴\u2005 200", mentioned_ids=["u2"])
        cmd_invite = parse_command(ctx_invite.message)
        reply_invite = handle_command(cmd_invite, ctx_invite)

        self.assertIn("战书下达", reply_invite)
        self.assertIn("越级挑战 · 逆境伐仙", reply_invite)
        self.assertIn("金盾", reply_invite)
        self.assertIn("200 灵石", reply_invite)

        # B. u2 查看三维属性
        ctx_stats = self._make_context("u2", "#决斗属性", mention_state="none")
        reply_stats = handle_command(parse_command(ctx_stats.message), ctx_stats)
        self.assertIn("决斗三维属性", reply_stats)
        self.assertIn("极阴", reply_stats)

        # C. u2 接受决斗，打响第 1 轮
        ctx_accept = self._make_context("u2", "#接受决斗", mention_state="none")
        reply_accept = handle_command(parse_command(ctx_accept.message), ctx_accept)

        self.assertIn("第 1 轮交锋", reply_accept)
        self.assertIn("气血余量", reply_accept)
        self.assertIn("请双方在 60 秒内发送【#继续决斗】", reply_accept)

        # D. 双方逐轮发送 #继续决斗，直至决出胜负
        reply_final = None
        for _ in range(25):
            ctx_c1 = self._make_context("u1", "#继续决斗", mention_state="none")
            r1 = handle_command(parse_command(ctx_c1.message), ctx_c1)
            self.assertIn("已确认继续决斗", r1)

            ctx_c2 = self._make_context("u2", "#继续", mention_state="none")
            r2 = handle_command(parse_command(ctx_c2.message), ctx_c2)
            if "最终胜者" in r2:
                reply_final = r2
                break

        self.assertIsNotNone(reply_final)
        self.assertIn("生死擂台 · 仙道决斗", reply_final)
        self.assertIn("仙道切磋，双方道基真实修为未损，储物袋法宝完好无缺！", reply_final)

        # 校验数据库结算结果：
        # 1. 灵石严格变动 ±200，总量守恒（500 + 1000 = 1500）
        p1 = self.db.execute("SELECT * FROM game_players WHERE player_id='u1'").fetchone()
        p2 = self.db.execute("SELECT * FROM game_players WHERE player_id='u2'").fetchone()
        self.assertEqual(p1["spirit_stones"] + p2["spirit_stones"], 1500)
        self.assertTrue((p1["spirit_stones"] == 700 and p2["spirit_stones"] == 800) or
                        (p1["spirit_stones"] == 300 and p2["spirit_stones"] == 1200))

        # 2. 真实修为未受任何伤害
        self.assertEqual(p1["cultivation"], 80)
        self.assertEqual(p2["cultivation"], 800)

        # 3. 法宝背包未受任何影响
        item = self.db.execute("SELECT * FROM game_items WHERE item_id='F101'").fetchone()
        self.assertEqual(item["owner_player_id"], "u1")
        self.assertEqual(item["state"], "held")

    # 6. Service 端拒绝决斗与避战累计
    def test_service_pvp_duel_reject(self):
        self._create_player("u1", "修士甲", stones=200)
        self._create_player("u2", "修士乙", stones=200)

        # 第 1 次发起与拒绝
        ctx_invite = self._make_context("u1", "#决斗 @修士乙\u2005 50", mentioned_ids=["u2"])
        handle_command(parse_command(ctx_invite.message), ctx_invite)

        ctx_reject = self._make_context("u2", "#拒绝决斗", mention_state="none")
        reply_reject = handle_command(parse_command(ctx_reject.message), ctx_reject)

        self.assertIn("拒绝了【修士甲】的决斗邀请", reply_reject)
        self.assertIn("连续拒绝/未响应 1/3 次", reply_reject)

        # 校验数据库：u2 避战计数为 1
        p2 = self.db.execute("SELECT consecutive_refuse_duel_count FROM game_players WHERE player_id='u2'").fetchone()
        self.assertEqual(p2["consecutive_refuse_duel_count"], 1)

        # 模拟再连续拒绝 2 次（共 3 次）
        for _ in range(2):
            ci = self._make_context("u1", "#决斗 @修士乙\u2005 50", mentioned_ids=["u2"])
            handle_command(parse_command(ci.message), ci)
            cr = self._make_context("u2", "#拒绝决斗", mention_state="none")
            reply_reject = handle_command(parse_command(cr.message), cr)

        self.assertIn("已连续 3 次拒绝或未响应", reply_reject)
        self.assertIn("战意溃散自晦", reply_reject)
        self.assertIn("整体攻击力减少 60%", reply_reject)

        p2_3 = self.db.execute("SELECT consecutive_refuse_duel_count FROM game_players WHERE player_id='u2'").fetchone()
        self.assertEqual(p2_3["consecutive_refuse_duel_count"], 3)

        # 灵石无损失
        p1 = self.db.execute("SELECT spirit_stones FROM game_players WHERE player_id='u1'").fetchone()
        self.assertEqual(p1[0], 200)

    # 7. 决斗超时自动累计避战与应战清零唤醒
    def test_service_pvp_duel_timeout_and_reset(self):
        self._create_player("u1", "剑修", stones=300)
        self._create_player("u2", "佛修", stones=300)

        # A. u1 发起决斗，时间戳设为 100 秒前（已超时）
        ctx_invite = self._make_context("u1", "#决斗 @佛修\u2005 50", mentioned_ids=["u2"])
        handle_command(parse_command(ctx_invite.message), ctx_invite)

        # 修改创建时间为 100 秒前
        self.db.execute("UPDATE game_pvp_duels SET created_at='2026-09-23T00:00:00Z'")

        # u2 此时尝试应战，已超时撤销并计入避战
        ctx_accept = self._make_context("u2", "#接受决斗", mention_state="none")
        reply_accept = handle_command(parse_command(ctx_accept.message), ctx_accept)
        self.assertIn("决斗邀请已超时", reply_accept)

        p2 = self.db.execute("SELECT consecutive_refuse_duel_count FROM game_players WHERE player_id='u2'").fetchone()
        self.assertEqual(p2["consecutive_refuse_duel_count"], 1)

        # B. 再次发起决斗并成功应战，验证参与决斗无法重置避战计数（规则：参与决斗不行）
        ctx_invite2 = self._make_context("u1", "#决斗 @佛修\u2005 50", mentioned_ids=["u2"])
        handle_command(parse_command(ctx_invite2.message), ctx_invite2)

        ctx_accept2 = self._make_context("u2", "#接受决斗", mention_state="none")
        reply_fight = handle_command(parse_command(ctx_accept2.message), ctx_accept2)
        self.assertIn("第 1 轮交锋", reply_fight)

        p2_after = self.db.execute("SELECT consecutive_refuse_duel_count FROM game_players WHERE player_id='u2'").fetchone()
        self.assertEqual(p2_after["consecutive_refuse_duel_count"], 1)

    # 7.1 专项测试：触发灵性沉眠后只有参与斗法才能移除，参与决斗不行
    def test_dormant_treasure_can_only_be_awakened_by_doufa_not_pvp(self):
        # 创建持有至宝的修士 u1（炼气，满额至宝威能 40）与对手 u2
        self._create_player("u1", "韩立", realm="qi", stones=500)
        self._create_player("u2", "对手", realm="qi", stones=500)
        self._add_item("T001", "u1", "wanhun_fan", "treasure")

        # 模拟 u1 累计避战 3 次，触发至宝沉眠
        self.db.execute("UPDATE game_players SET consecutive_refuse_duel_count=3 WHERE player_id='u1'")

        # 1. 查看决斗属性：确认至宝已陷入沉眠（削弱 60% 剩 40% = 16 ATK），且提示唯有参与斗法唤醒
        ctx_stats = self._make_context("u1", "#决斗属性", mention_state="none")
        r_stats = handle_command(parse_command(ctx_stats.message), ctx_stats)
        self.assertIn("至宝自晦", r_stats)
        self.assertIn("唯有参与一次【#斗法】方可唤醒满额神威（参与决斗无法移除沉眠）", r_stats)
        self.assertIn("攻击（ATK）：16", r_stats)

        # 2. u1 参与一场【#决斗】，并在交锋中打出结果
        ctx_inv = self._make_context("u2", "#决斗 @韩立\u2005 50", mentioned_ids=["u1"])
        handle_command(parse_command(ctx_inv.message), ctx_inv)

        ctx_acc = self._make_context("u1", "#接受决斗", mention_state="none")
        r_acc = handle_command(parse_command(ctx_acc.message), ctx_acc)
        self.assertIn("第 1 轮交锋", r_acc)

        # 决斗进行中投降结算
        ctx_surr = self._make_context("u2", "#投降", mention_state="none")
        handle_command(parse_command(ctx_surr.message), ctx_surr)

        # 校验：参与决斗后，u1 的避战计数依旧为 3，未被决斗清零！
        p1_after_pvp = self.db.execute("SELECT consecutive_refuse_duel_count FROM game_players WHERE player_id='u1'").fetchone()
        self.assertEqual(p1_after_pvp["consecutive_refuse_duel_count"], 3)

        # 至宝仍然处于沉眠状态（16 攻击）
        p1_row = self.db.execute("SELECT * FROM game_players WHERE player_id='u1'").fetchone()
        inv1 = self.db.execute("SELECT * FROM game_items WHERE owner_player_id='u1' AND state='held'").fetchall()
        stats_after_pvp = calculate_fighter_stats(p1_row, inv1)
        self.assertEqual(stats_after_pvp["attack"], 16)
        self.assertTrue(stats_after_pvp["has_dormant_treasure"])

        # 3. 参与【#斗法】淬炼：模拟斗法完成并结算
        # 在 duels 结算机制中，正常开打完成后会将参战双方的 consecutive_refuse_duel_count 置为 0
        self.db.execute("UPDATE game_players SET consecutive_refuse_duel_count = 0 WHERE player_id='u1'")

        # 校验：参与斗法后，避战计数归零，至宝成功唤醒，满额攻击恢复为 40！
        p1_after_doufa = self.db.execute("SELECT * FROM game_players WHERE player_id='u1'").fetchone()
        stats_after_doufa = calculate_fighter_stats(p1_after_doufa, inv1)
        self.assertEqual(p1_after_doufa["consecutive_refuse_duel_count"], 0)
        self.assertEqual(stats_after_doufa["attack"], 40)
        self.assertFalse(stats_after_doufa["has_dormant_treasure"])

    # 8. 逐轮交互测试：单方确认等待、重复确认提示、双方确认打响第 2 轮
    def test_pvp_round_by_round_flow(self):
        self._create_player("u1", "剑修", stones=500)
        self._create_player("u2", "法修", stones=500)

        ctx_invite = self._make_context("u1", "#决斗 @法修\u2005 100", mentioned_ids=["u2"])
        handle_command(parse_command(ctx_invite.message), ctx_invite)

        ctx_accept = self._make_context("u2", "#接受决斗", mention_state="none")
        r_round1 = handle_command(parse_command(ctx_accept.message), ctx_accept)
        self.assertIn("第 1 轮交锋", r_round1)
        self.assertIn("@剑修", str(r_round1))
        self.assertIn("@法修", str(r_round1))
        self.assertEqual(getattr(r_round1, 'mention_ids', ()), ('u1', 'u2'))

        # u1 首次确认
        ctx_c1 = self._make_context("u1", "#继续决斗", mention_state="none")
        r_c1 = handle_command(parse_command(ctx_c1.message), ctx_c1)
        self.assertIn("已确认继续决斗", r_c1)
        self.assertIn("正在等待【法修】确认", r_c1)
        self.assertIn("@法修", str(r_c1))
        self.assertEqual(getattr(r_c1, 'mention_ids', ()), ('u2',))

        # u1 重复确认
        ctx_c1_repeat = self._make_context("u1", "#继续", mention_state="none")
        r_c1_rep = handle_command(parse_command(ctx_c1_repeat.message), ctx_c1_repeat)
        self.assertIn("你已确认继续决斗，正在等待【法修】确认", r_c1_rep)

        # 非参战玩家尝试插手
        self._create_player("u3", "路人", stones=100)
        ctx_c3 = self._make_context("u3", "#继续决斗", mention_state="none")
        r_c3 = handle_command(parse_command(ctx_c3.message), ctx_c3)
        self.assertIn("当前没有正在进行中的决斗交锋", r_c3)

        # u2 确认继续 -> 双方确认完毕，打响第 2 轮交锋
        ctx_c2 = self._make_context("u2", "#继续", mention_state="none")
        r_round2 = handle_command(parse_command(ctx_c2.message), ctx_c2)
        self.assertTrue("第 2 轮交锋" in r_round2 or "最终胜者" in r_round2)
        if "第 2 轮交锋" in r_round2:
            self.assertIn("@剑修", str(r_round2))
            self.assertIn("@法修", str(r_round2))
            self.assertEqual(getattr(r_round2, 'mention_ids', ()), ('u1', 'u2'))

    # 9. 主动投降结算测试：扣除 80% 押注交给获胜方，保全 20%
    def test_pvp_surrender_penalty_80_percent(self):
        self._create_player("u1", "修士甲", stones=500)
        self._create_player("u2", "修士乙", stones=500)

        # 押注 100 灵石
        ctx_invite = self._make_context("u1", "#决斗 @修士乙\u2005 100", mentioned_ids=["u2"])
        handle_command(parse_command(ctx_invite.message), ctx_invite)

        ctx_accept = self._make_context("u2", "#接受决斗", mention_state="none")
        handle_command(parse_command(ctx_accept.message), ctx_accept)

        # u1 主动认输投降
        ctx_surr = self._make_context("u1", "#投降", mention_state="none")
        reply_surr = handle_command(parse_command(ctx_surr.message), ctx_surr)

        self.assertIn("认输投降", reply_surr)
        self.assertIn("扣除 -80 灵石（保全 20 灵石）", reply_surr)
        self.assertIn("斩获 +80 灵石", reply_surr)

        # 校验灵石划转：
        # u1: 500 - 80 = 420
        # u2: 500 + 80 = 580
        # 总量严格守恒 1000
        p1 = self.db.execute("SELECT spirit_stones FROM game_players WHERE player_id='u1'").fetchone()
        p2 = self.db.execute("SELECT spirit_stones FROM game_players WHERE player_id='u2'").fetchone()
        self.assertEqual(p1["spirit_stones"], 420)
        self.assertEqual(p2["spirit_stones"], 580)
        self.assertEqual(p1["spirit_stones"] + p2["spirit_stones"], 1000)

        # 校验数据库决斗记录
        duel = self.db.execute("SELECT * FROM game_pvp_duels WHERE challenger_id='u1'").fetchone()
        self.assertEqual(duel["state"], "settled")
        self.assertEqual(duel["winner_id"], "u2")
        self.assertEqual(duel["surrendered_id"], "u1")

    # 10. 一方确认另一方超时未确认测试：判定未确认方投降，扣除 80% 押注给已确认方
    def test_pvp_unconfirmed_timeout_as_surrender(self):
        self._create_player("u1", "修士甲", stones=500)
        self._create_player("u2", "修士乙", stones=500)

        ctx_invite = self._make_context("u1", "#决斗 @修士乙\u2005 100", mentioned_ids=["u2"])
        handle_command(parse_command(ctx_invite.message), ctx_invite)

        ctx_accept = self._make_context("u2", "#接受决斗", mention_state="none")
        handle_command(parse_command(ctx_accept.message), ctx_accept)

        # u1 确认继续
        ctx_c1 = self._make_context("u1", "#继续决斗", mention_state="none")
        handle_command(parse_command(ctx_c1.message), ctx_c1)

        # 模拟超时（60 秒过去）：修改 round_deadline_at
        self.db.execute("UPDATE game_pvp_duels SET round_deadline_at='2026-09-23T00:00:00Z'")

        # 清理超时对局
        from wechat_receiver.games.repository import GameRepository
        repo = GameRepository(self.db, "test_acc", "room1@chatroom", "u1")
        cleaned = repo.clean_expired_pvp_duels("2026-09-23T12:00:00Z")
        self.assertEqual(len(cleaned), 1)

        # 校验：u2 视为超时弃权，扣除 80 灵石，u1 获得 80 灵石
        p1 = self.db.execute("SELECT spirit_stones FROM game_players WHERE player_id='u1'").fetchone()
        p2 = self.db.execute("SELECT spirit_stones FROM game_players WHERE player_id='u2'").fetchone()
        self.assertEqual(p1["spirit_stones"], 580)
        self.assertEqual(p2["spirit_stones"], 420)

        duel = self.db.execute("SELECT * FROM game_pvp_duels WHERE challenger_id='u1'").fetchone()
        self.assertEqual(duel["state"], "settled")
        self.assertEqual(duel["winner_id"], "u1")
        self.assertEqual(duel["surrendered_id"], "u2")

    # 11. 双方均超时未确认测试：擂台撤销，双方押注原路退还
    def test_pvp_timeout_both_unconfirmed(self):
        self._create_player("u1", "修士甲", stones=500)
        self._create_player("u2", "修士乙", stones=500)

        ctx_invite = self._make_context("u1", "#决斗 @修士乙\u2005 100", mentioned_ids=["u2"])
        handle_command(parse_command(ctx_invite.message), ctx_invite)

        ctx_accept = self._make_context("u2", "#接受决斗", mention_state="none")
        handle_command(parse_command(ctx_accept.message), ctx_accept)

        # 双方均未确认，且超时
        self.db.execute("UPDATE game_pvp_duels SET round_deadline_at='2026-09-23T00:00:00Z'")

        from wechat_receiver.games.repository import GameRepository
        repo = GameRepository(self.db, "test_acc", "room1@chatroom", "u1")
        cleaned = repo.clean_expired_pvp_duels("2026-09-23T12:00:00Z")
        self.assertEqual(len(cleaned), 1)

        # 灵石完全不变
        p1 = self.db.execute("SELECT spirit_stones FROM game_players WHERE player_id='u1'").fetchone()
        p2 = self.db.execute("SELECT spirit_stones FROM game_players WHERE player_id='u2'").fetchone()
        self.assertEqual(p1["spirit_stones"], 500)
        self.assertEqual(p2["spirit_stones"], 500)

        duel = self.db.execute("SELECT * FROM game_pvp_duels WHERE challenger_id='u1'").fetchone()
        self.assertEqual(duel["state"], "cancelled")


    # 12. 决斗邀请超时 60 秒后台轮询主动撤销并广播测试
    def test_pvp_invitation_timeout_poll(self):
        from wechat_receiver.games.duels import on_poll

        self._create_player("u1", "挑战者甲", stones=500)
        self._create_player("u2", "受邀者乙", stones=500)

        ctx_invite = self._make_context("u1", "#决斗 @受邀者乙\u2005 100", mentioned_ids=["u2"])
        handle_command(parse_command(ctx_invite.message), ctx_invite)

        # 模拟 70 秒过去
        self.db.execute("UPDATE game_pvp_duels SET created_at='2026-09-23T00:00:00Z'")

        # 构造轮询 context，now 时间设为 2026-09-23T00:01:10Z
        ctx_poll = SimpleNamespace(
            store=self.db,
            account_id="test_acc",
            now=1790121670.0, # 对应 00:01:10
            connection_id="conn1",
            reply_request_id=lambda k: f"req_{k}",
            game_config=self.config,
        )

        replies = on_poll(ctx_poll)
        self.assertTrue(len(replies) >= 1)
        found_timeout_reply = False
        for rep in replies:
            if "邀请超时" in rep.text:
                found_timeout_reply = True
                self.assertIn("受邀者乙", rep.text)
                self.assertIn("超过 60 秒未应答", rep.text)
                self.assertEqual(rep.target_id, "room1@chatroom")
                self.assertEqual(rep.mention_ids, ("u2", "u1"))
        self.assertTrue(found_timeout_reply)

        # 检查数据库状态
        duel = self.db.execute("SELECT * FROM game_pvp_duels WHERE challenger_id='u1'").fetchone()
        self.assertEqual(duel["state"], "cancelled")

        p2 = self.db.execute("SELECT consecutive_refuse_duel_count FROM game_players WHERE player_id='u2'").fetchone()
        self.assertEqual(p2["consecutive_refuse_duel_count"], 1)

    # 13. 决斗邀请超时累计 3 次触发至宝沉眠测试
    def test_pvp_invitation_timeout_triggers_dormant(self):
        from wechat_receiver.games.duels import on_poll

        self._create_player("u1", "挑战者甲", stones=500)
        self._create_player("u2", "受邀者乙", stones=500)
        # 预设已经避战 2 次
        self.db.execute("UPDATE game_players SET consecutive_refuse_duel_count=2 WHERE player_id='u2'")

        ctx_invite = self._make_context("u1", "#决斗 @受邀者乙\u2005 100", mentioned_ids=["u2"])
        handle_command(parse_command(ctx_invite.message), ctx_invite)

        self.db.execute("UPDATE game_pvp_duels SET created_at='2026-09-23T00:00:00Z'")

        ctx_poll = SimpleNamespace(
            store=self.db,
            account_id="test_acc",
            now=1790121670.0,
            connection_id="conn1",
            reply_request_id=lambda k: f"req_{k}",
            game_config=self.config,
        )

        replies = on_poll(ctx_poll)
        found = False
        for rep in replies:
            if "整体攻击力减少 60%" in rep.text:
                found = True
                self.assertIn("连续 3 次拒绝或未响应", rep.text)
                self.assertIn("唯有参与一次【#斗法】方可唤醒", rep.text)
        self.assertTrue(found)

        p2 = self.db.execute("SELECT consecutive_refuse_duel_count FROM game_players WHERE player_id='u2'").fetchone()
        self.assertEqual(p2["consecutive_refuse_duel_count"], 3)

    # 14. 发起者主动撤销决斗（#取消决斗 / #撤销决斗）测试
    def test_pvp_cancel_duel(self):
        self._create_player("u1", "修士甲", stones=500)
        self._create_player("u2", "修士乙", stones=500)

        # 无邀请时取消
        ctx_cancel_none = self._make_context("u1", "#取消决斗", mention_state="none")
        res_none = handle_command(parse_command(ctx_cancel_none.message), ctx_cancel_none)
        self.assertIn("你当前没有正在等待应战的决斗邀请", res_none)

        # 发起决斗
        ctx_invite = self._make_context("u1", "#决斗 @修士乙\u2005 100", mentioned_ids=["u2"])
        handle_command(parse_command(ctx_invite.message), ctx_invite)

        # 发起者撤销邀请
        ctx_cancel = self._make_context("u1", "#取消决斗", mention_state="none")
        res_cancel = handle_command(parse_command(ctx_cancel.message), ctx_cancel)
        self.assertIn("撤销了向【修士乙】发起的仙道决斗邀请", res_cancel)

        # 校验数据库已 cancelled
        duel = self.db.execute("SELECT * FROM game_pvp_duels WHERE challenger_id='u1'").fetchone()
        self.assertEqual(duel["state"], "cancelled")

        # 撤销后可以立即重新发起
        ctx_invite2 = self._make_context("u1", "#决斗 @修士乙\u2005 100", mentioned_ids=["u2"])
        res2 = handle_command(parse_command(ctx_invite2.message), ctx_invite2)
        self.assertIn("战书下达", res2)

    # 15. 查看决斗状态（#决斗状态）测试
    def test_pvp_duel_status(self):
        self._create_player("u1", "修士甲", stones=500)
        self._create_player("u2", "修士乙", stones=500)

        # 状态：暂无决斗
        ctx_st1 = self._make_context("u1", "#决斗状态", mention_state="none")
        res1 = handle_command(parse_command(ctx_st1.message), ctx_st1)
        self.assertIn("暂无进行中或等待应战", res1)

        # 发起邀请后查看状态
        ctx_invite = self._make_context("u1", "#决斗 @修士乙\u2005 100", mentioned_ids=["u2"])
        handle_command(parse_command(ctx_invite.message), ctx_invite)

        ctx_st2 = self._make_context("u1", "#决斗状态", mention_state="none")
        res2 = handle_command(parse_command(ctx_st2.message), ctx_st2)
        self.assertIn("等待应战中", res2)
        self.assertIn("剩余应答时间", res2)
        self.assertIn("【修士甲】", res2)
        self.assertIn("【修士乙】", res2)

        # 接受决斗后交锋中查看状态
        ctx_accept = self._make_context("u2", "#接受决斗", mention_state="none")
        handle_command(parse_command(ctx_accept.message), ctx_accept)

        ctx_st3 = self._make_context("u1", "#决斗状态", mention_state="none")
        res3 = handle_command(parse_command(ctx_st3.message), ctx_st3)
        self.assertIn("第 1 轮交锋中", res3)
        self.assertIn("剩余确认时间", res3)

    # 16. 邀请超时后受邀者拒绝决斗测试
    def test_pvp_reject_after_timeout(self):
        self._create_player("u1", "修士甲", stones=500)
        self._create_player("u2", "修士乙", stones=500)

        ctx_invite = self._make_context("u1", "#决斗 @修士乙\u2005 100", mentioned_ids=["u2"])
        handle_command(parse_command(ctx_invite.message), ctx_invite)

        # 模拟 70 秒超时
        self.db.execute("UPDATE game_pvp_duels SET created_at='2026-09-23T00:00:00Z'")

        ctx_reject = self._make_context("u2", "#拒绝决斗", mention_state="none")
        # now 设置为 2026-09-23T00:01:10Z
        ctx_reject.now = 1790121670.0
        res = handle_command(parse_command(ctx_reject.message), ctx_reject)
        self.assertIn("该决斗邀请已超时", res)

    def test_wager_is_held_while_other_spending_continues(self):
        self._create_player('u1', '修士甲', stones=200)
        self._create_player('u2', '修士乙', stones=200)
        invite = self._make_context('u1', '#决斗 @修士乙\u2005 100', mentioned_ids=['u2'])
        handle_command(parse_command(invite.message), invite)
        accept = self._make_context('u2', '#接受决斗', mention_state='none')
        handle_command(parse_command(accept.message), accept)
        duel = self.db.execute("SELECT * FROM game_pvp_duels").fetchone()
        self.assertEqual((duel['state'], duel['escrowed']), ('fighting', 1))
        self.assertEqual(self.db.execute("SELECT spirit_stones FROM game_players WHERE player_id='u1'").fetchone()[0], 100)

        costly = self._make_context('u1', '#购买 凝气丹', mention_state='none')
        result = handle_command(parse_command(costly.message), costly)
        self.assertIn('不足', result)
        cheap = self._make_context('u1', '#购买 扰心符', mention_state='none')
        result = handle_command(parse_command(cheap.message), cheap)
        self.assertIn('购买成功', result)
        self.assertEqual(self.db.execute("SELECT spirit_stones FROM game_players WHERE player_id='u1'").fetchone()[0], 85)

        surrender = self._make_context('u1', '#投降', mention_state='none')
        handle_command(parse_command(surrender.message), surrender)
        balances = [row[0] for row in self.db.execute("SELECT spirit_stones FROM game_players ORDER BY player_id")]
        self.assertEqual(balances, [105, 280])

    def test_escrow_settlement_and_timeout_are_idempotent(self):
        from wechat_receiver.games.repository import GameRepository
        self._create_player('u1', '修士甲', stones=200)
        self._create_player('u2', '修士乙', stones=200)
        repo = GameRepository(self.db, 'test_acc', 'room1@chatroom', 'u1')
        duel_id = repo.create_pvp_duel('u1', 'u2', 100, '2026-09-23T00:00:00Z')
        repo.reserve_pvp_wagers(duel_id)
        self.assertTrue(repo.finish_pvp_duel(duel_id, 'settled', '2026-09-23T00:00:01Z', winner_id='u1'))
        self.assertFalse(repo.finish_pvp_duel(duel_id, 'settled', '2026-09-23T00:00:02Z', winner_id='u1'))
        self.assertEqual([r[0] for r in self.db.execute('SELECT spirit_stones FROM game_players ORDER BY player_id')], [300, 100])

        duel_id = repo.create_pvp_duel('u1', 'u2', 50, '2026-09-23T00:01:00Z')
        repo.reserve_pvp_wagers(duel_id)
        repo.update_pvp_duel_round_state(duel_id, 1, '{"p1_confirmed":true,"p2_confirmed":false}',
                                         '2026-09-23T00:01:10Z')
        self.assertEqual(len(repo.clean_expired_pvp_duels('2026-09-23T00:02:00Z')), 1)
        self.assertEqual(repo.clean_expired_pvp_duels('2026-09-23T00:02:00Z'), [])
        self.assertEqual([r[0] for r in self.db.execute('SELECT spirit_stones FROM game_players ORDER BY player_id')], [340, 60])

    def test_first_round_knockout_releases_escrow_once(self):
        self._create_player('u1', '修士甲', stones=200)
        self._create_player('u2', '修士乙', stones=200)
        invite = self._make_context('u1', '#决斗 @修士乙\u2005 100', mentioned_ids=['u2'])
        handle_command(parse_command(invite.message), invite)
        accept = self._make_context('u2', '#接受决斗', mention_state='none')
        knockout = {'is_over': True, 'winner': 1, 'round_num': 1,
                    'logs': ['首轮结束'], 'hp1': 100, 'hp2': 0}
        with patch('wechat_receiver.games.service.simulate_duel_round', return_value=knockout):
            result = handle_command(parse_command(accept.message), accept)
        self.assertIn('最终胜者', result)
        duel = self.db.execute('SELECT * FROM game_pvp_duels').fetchone()
        self.assertEqual((duel['state'], duel['escrowed']), ('settled', 1))
        self.assertEqual([r[0] for r in self.db.execute('SELECT spirit_stones FROM game_players ORDER BY player_id')], [300, 100])

    def test_command_cleanup_keeps_timeout_notice_for_poll(self):
        from wechat_receiver.games.duels import poll_pvp_duels
        self._create_player('u1', '修士甲', stones=200)
        self._create_player('u2', '修士乙', stones=200)
        invite = self._make_context('u1', '#决斗 @修士乙\u2005 100', mentioned_ids=['u2'])
        handle_command(parse_command(invite.message), invite)
        self.db.execute("UPDATE game_pvp_duels SET created_at='2026-09-23T00:00:00Z'")
        status = self._make_context('u1', '#决斗状态', mention_state='none')
        handle_command(parse_command(status.message), status)
        duel = self.db.execute('SELECT * FROM game_pvp_duels').fetchone()
        self.assertEqual(duel['state'], 'cancelled')
        self.assertIsNotNone(duel['timeout_notice_json'])
        poll = SimpleNamespace(store=self.db, account_id='test_acc', now=1790121670.0,
                               connection_id='conn1', reply_request_id=lambda key: 'req_' + key)
        replies = poll_pvp_duels(poll)
        self.assertEqual(len(replies), 1)
        self.assertIn('邀请超时', replies[0].text)

    def test_old_unreserved_fighting_duel_is_cancelled_without_refund(self):
        self._create_player('u1', '修士甲', stones=200)
        self._create_player('u2', '修士乙', stones=200)
        self.db.execute('DROP TABLE game_pvp_duels')
        self.db.execute("""CREATE TABLE game_pvp_duels (
            duel_id TEXT PRIMARY KEY, account_id TEXT, group_id TEXT,
            challenger_id TEXT, challenged_id TEXT, wager INTEGER,
            state TEXT CHECK(state IN ('inviting','fighting','settled','rejected','cancelled')),
            current_round INTEGER, round_state_json TEXT, round_deadline_at TEXT,
            winner_id TEXT, surrendered_id TEXT, created_at TEXT, settled_at TEXT)""")
        self.db.execute("""INSERT INTO game_pvp_duels
            (duel_id,account_id,group_id,challenger_id,challenged_id,wager,state,created_at)
            VALUES ('VOLD','test_acc','room1@chatroom','u1','u2',100,'fighting','2026-09-23T00:00:00Z')""")
        initialize_game_schema(self.db)
        duel = self.db.execute("SELECT * FROM game_pvp_duels WHERE duel_id='VOLD'").fetchone()
        self.assertEqual((duel['state'], duel['escrowed']), ('cancelled', 0))
        self.assertIsNotNone(duel['timeout_notice_json'])
        self.assertEqual([r[0] for r in self.db.execute('SELECT spirit_stones FROM game_players ORDER BY player_id')], [200, 200])

    def test_cross_mode_invitations_and_acceptance_are_mutually_exclusive(self):
        from wechat_receiver.games import duels
        from wechat_receiver.games.commands import Command
        for player_id, name in [('u1', '修士甲'), ('u2', '修士乙'),
                                ('u3', '修士丙'), ('u4', '修士丁')]:
            self._create_player(player_id, name, stones=200)
        self.db.execute("""INSERT INTO game_duels
            (duel_id,account_id,group_id,challenger_id,challenged_id,state,rules_version,rules_json)
            VALUES ('D1','test_acc','room1@chatroom','u1','u2','supporting',1,'{}')""")
        blocked = self._make_context('u1', '#决斗 @修士乙\u2005 100', mentioned_ids=['u2'])
        self.assertIn('已有斗法', handle_command(parse_command(blocked.message), blocked))
        independent = self._make_context('u3', '#决斗 @修士丁\u2005 100', mentioned_ids=['u4'])
        self.assertIn('战书下达', handle_command(parse_command(independent.message), independent))
        self.db.execute("UPDATE game_duels SET state='cancelled' WHERE duel_id='D1'")
        duel_game = duels._Duel(self._make_context('u3', '#斗法 @修士丁', mentioned_ids=['u4']))
        self.assertIn('已有仙道决斗', duel_game.challenge(Command('challenge', target_id='u4')))

        self.db.execute("""INSERT INTO game_duels
            (duel_id,account_id,group_id,challenger_id,challenged_id,state,rules_version,rules_json)
            VALUES ('D2','test_acc','room1@chatroom','u3','u4','supporting',1,'{}')""")
        accept = self._make_context('u4', '#接受决斗', mention_state='none')
        self.assertIn('已有斗法', handle_command(parse_command(accept.message), accept))
        self.assertEqual(self.db.execute("SELECT state FROM game_pvp_duels WHERE challenger_id='u3'").fetchone()[0], 'inviting')
        self.db.execute("""UPDATE game_duels SET state='inviting',
            phase_opened_at='1970-01-01T00:00:00Z', phase_deadline_at='1970-01-01T00:00:10Z'
            WHERE duel_id='D2'""")
        traditional = duels._Duel(self._make_context('u4', '#接受斗法', mention_state='none'))
        row = self.db.execute("SELECT * FROM game_duels WHERE duel_id='D2'").fetchone()
        self.assertIn('已有仙道决斗', traditional.act(Command('accept'), row))

    def test_router_queues_notice_after_command_timeout_once_across_restart(self):
        from wechat_receiver.games import duels
        account, group, now = 'test_acc', 'room1@chatroom', 1790150400.0
        config = SimpleNamespace(
            sender=SimpleNamespace(account_id=account, allowed_targets=frozenset({group})),
            enabled_plugins=('xiuxian',), reply_ttl_seconds=60, max_message_age_seconds=120,
            game_config=self.config,
        )
        plugin = LoadedPlugin('xiuxian', None, parse_command=parse_command,
                              handle_command=handle_command, on_start=duels.on_start,
                              on_before_messages=duels.on_before_messages, on_poll=duels.on_poll)
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'pvp.sqlite3'
            store = Store(path)
            try:
                router = ReplyRouter(store, config, [plugin], started_at=now - 1)
                router.start('pvp-session', now=now)
                for player_id, name in [('u1', '修士甲'), ('u2', '修士乙')]:
                    store.db.execute("""INSERT INTO game_players
                        (account_id,group_id,player_id,dao_name,dao_name_key,cultivation,spirit_stones)
                        VALUES (?,?,?,?,?,50,200)""",
                        (account, group, player_id, name, name,))
                store.db.commit()

                def message(content, sender, number, mentions=()):
                    return replace(make_message(content, sender_id=sender,
                                                mention_state='explicit_other' if mentions else 'none',
                                                mentioned_ids=mentions),
                                   session_id='pvp-session', event_key=f'pvp-session:{number}',
                                   seq=number, call_id=number, observed_at_ms=int(now * 1000),
                                   message_time_candidate=int(now), message_id_candidate=str(90000 + number))

                router.handle(message('#决斗 @修士乙\u2005 100', 'u1', 1, ('u2',)), 'pvp-session', now=now)
                duel = store.db.execute('SELECT duel_id FROM game_pvp_duels').fetchone()
                self.assertIsNotNone(duel)
                store.db.execute("UPDATE game_pvp_duels SET created_at='2026-09-23T00:00:00Z'")
                store.db.commit()
                router.handle(message('#决斗状态', 'u1', 2), 'pvp-session', now=now)
                self.assertEqual(store.db.execute('SELECT state FROM game_pvp_duels').fetchone()[0], 'cancelled')

                key = f"pvp_duel:{duel['duel_id']}:invite_timeout"
                request_id = reply_request_id(account, 'xiuxian', key)
                self.assertIsNone(store.db.execute('SELECT 1 FROM outbox WHERE request_id=?', (request_id,)).fetchone())
                router.tick('pvp-session', now=now + 1)
                self.assertIsNotNone(store.db.execute('SELECT 1 FROM outbox WHERE request_id=?', (request_id,)).fetchone())
                self.assertEqual(store.db.execute('SELECT consecutive_refuse_duel_count FROM game_players WHERE player_id=\'u2\'').fetchone()[0], 1)
            finally:
                store.close()
            reopened = Store(path)
            try:
                restarted = ReplyRouter(reopened, config, [plugin], started_at=now + 1)
                restarted.start('new-session', now=now + 2)
                restarted.tick('new-session', now=now + 2)
                self.assertEqual(reopened.db.execute('SELECT count(*) FROM outbox WHERE request_id=?', (request_id,)).fetchone()[0], 1)
                self.assertEqual(reopened.db.execute('SELECT consecutive_refuse_duel_count FROM game_players WHERE player_id=\'u2\'').fetchone()[0], 1)
                for number in range(5):
                    reopened.db.execute("""INSERT INTO game_pvp_duels
                        (duel_id,account_id,group_id,challenger_id,challenged_id,wager,state,created_at)
                        VALUES (?,?,?,'u1','u2',100,'inviting','2026-09-23T00:00:00Z')""",
                        (f'VBACK{number}', account, group))
                reopened.db.commit()
                backlog_ids = [reply_request_id(account, 'xiuxian', f'pvp_duel:VBACK{number}:invite_timeout')
                               for number in range(5)]
                restarted.tick('new-session', now=now + 3)
                queued = reopened.db.execute(
                    'SELECT count(*) FROM outbox WHERE request_id IN (?,?,?,?,?)', backlog_ids).fetchone()[0]
                self.assertEqual(queued, 3)
                restarted.tick('new-session', now=now + 4)
                queued = reopened.db.execute(
                    'SELECT count(*) FROM outbox WHERE request_id IN (?,?,?,?,?)', backlog_ids).fetchone()[0]
                self.assertEqual(queued, 5)
            finally:
                reopened.close()

    # 20. 斗法拒绝与超时未应答测试：验证 sqlite3.Row 兼容性与避战计数增加
    def test_doufa_declined_and_timeout_sqlite_row_compatibility(self):
        from wechat_receiver.games.duels import _Duel
        self._create_player("u1", "清风", stones=500)
        self._create_player("u2", "明月", stones=500)
        self.db.execute("UPDATE game_players SET consecutive_refuse_duel_count=2 WHERE player_id='u2'")

        # 构造一条处于 cancelled 状态且被邀请方为 u2 的真实 sqlite3.Row 对局
        self.db.execute("""INSERT INTO game_duels
            (duel_id, account_id, group_id, challenger_id, challenged_id, state, final_reason, rules_version, rules_json, created_at)
            VALUES ('D_TEST_ROW', 'test_acc', 'room1@chatroom', 'u1', 'u2', 'cancelled', 'declined', 1, '{}', '2026-09-23T00:00:00Z')""")
        row = self.db.execute("SELECT * FROM game_duels WHERE duel_id='D_TEST_ROW'").fetchone()

        # 验证 row 为真实的 sqlite3.Row 类型
        self.assertIsInstance(row, sqlite3.Row)

        game = _Duel(SimpleNamespace(store=self.db, account_id='test_acc', now=1790150400), 'room1@chatroom')
        # 验证调用 result_text 时不会因为 sqlite3.Row 没有 get 属性抛出 AttributeError，并包含避战进度提示
        reply_text = game.result_text(row)
        self.assertIn("受邀者拒绝了本场斗法", reply_text)
        self.assertIn("连续拒绝/未响应 2/3 次", reply_text)

        # 同样验证 invitation_timeout 达到 3 次触发至宝沉眠
        self.db.execute("UPDATE game_duels SET final_reason='invitation_timeout' WHERE duel_id='D_TEST_ROW'")
        self.db.execute("UPDATE game_players SET consecutive_refuse_duel_count=3 WHERE player_id='u2'")
        row2 = self.db.execute("SELECT * FROM game_duels WHERE duel_id='D_TEST_ROW'").fetchone()
        reply_text2 = game.result_text(row2)
        self.assertIn("邀请已到期", reply_text2)
        self.assertIn("至宝自晦", reply_text2)
        self.assertIn("整体攻击力减少 60%", reply_text2)


if __name__ == "__main__":
    unittest.main()
