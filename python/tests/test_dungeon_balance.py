"""Regression checks for the reduced class stats and finite healer sustain."""

import random
import unittest

from wechat_receiver.games.dungeon import combat, render
from wechat_receiver.games.dungeon.content import BOSSES, CLASSES, RULES


class DungeonBalanceTests(unittest.TestCase):
    def test_level_curve_accelerates_and_preserves_health_fraction(self):
        for class_id in CLASSES:
            member = combat.create_member('p', '青玄', class_id)
            member['hp'] *= .4
            levels = [combat.scale_member(member, level) for level in range(1, 10)]
            for current in levels:
                self.assertAlmostEqual(current['hp'] / current['max_hp'], .4, places=6)
            for field in ('max_hp', 'atk'):
                gains = [b[field] - a[field] for a, b in zip(levels, levels[1:])]
                self.assertGreater(gains[-1], gains[0])
                self.assertTrue(all(gain > 0 for gain in gains))

    def test_basic_only_healer_does_not_stall_the_boss_roster(self):
        # Full-health, max-level, no equipment/roots. This deliberately ignores
        # timing and cooldown skills: the old basic-heal loop must not suffice.
        for boss_id in BOSSES:
            with self.subTest(boss=boss_id):
                member = combat.scale_member(combat.create_member('p', '丹', 'C04'), 9)
                rng = random.Random(7)
                battle = combat.start_battle([member], boss_id, night=3, rng=rng)
                while battle['outcome'] == 'ongoing' and battle['round'] <= 180:
                    battle, _ = combat.resolve_round(battle, {'p': 'attack'}, rng)
                self.assertEqual(battle['outcome'], 'defeat')

    def test_daytime_healer_and_water_elite_finish_within_auto_limit(self):
        member = combat.scale_member(combat.create_member('p', '丹', 'C04'), 6)
        battle, _ = combat.auto_battle([member], 'M07', day=2, rng=random.Random(7))
        self.assertIn(battle['outcome'], ('victory', 'defeat'))
        self.assertLessEqual(battle['round'], 200)

    def test_battle_prompt_has_one_real_token_and_only_exposed_part(self):
        rng = random.Random(7)
        member = combat.create_member('p', '青玄', 'C01')
        battle = combat.start_battle([member], 'M04', night=2, rng=rng)
        state = {'code': 'ABCD', 'token': 'ABCD.9', 'battle': battle, 'actions': {}}
        text = render.battle_prompt(state)
        self.assertEqual(text.count('#副本行动'), 1)
        self.assertIn('#副本行动 ABCD.9 行动', text)
        self.assertNotIn('部位 雷角', text)
        for command in ('闪避 前段', '闪避 后段', '回气'):
            self.assertIn(command, text)
        battle, _ = combat.resolve_round(battle, {'p': 'defend'}, rng)
        state['battle'] = battle
        text = render.battle_prompt(state)
        self.assertIn('部位 雷角', text)
        for answer in ('应该', '推荐', '7.20', '前段命中', '后段命中'):
            self.assertNotIn(answer, text)

    def test_exploration_and_help_describe_current_progression(self):
        member = combat.create_member('p', '青玄', 'C01')
        state = {'phase': 'explore', 'code': 'ABCD', 'token': 'ABCD.2',
                 'day': 2, 'night': 2, 'node': 6, 'experience': 400,
                 'members': [member], 'fighters': [combat.scale_member(member, 6)],
                 'choices': ['N01', 'N05', 'N04']}
        text = render.choices(state)
        self.assertIn('探索 6/6', text)
        self.assertIn('经验 400', text)
        for amount in (100, 250, 20):
            self.assertIn(f'+{amount} 经验', text)
        help_text = render.help_text()
        self.assertIn('两昼三夜', help_text)
        self.assertIn('第二夜胜利后直接挑战第三夜妖王', help_text)
        self.assertIn('#副本行动 ABCD.2 灵药', text)
        self.assertIn('【出击前准备】', help_text)
        self.assertIn('【副本中】', help_text)
        self.assertIn('玩法机制', help_text)
        self.assertIn(f"每天可出击 {RULES['rewarded_runs_per_day']} 次", help_text)
        self.assertNotIn('第一夜胜利后 +1', help_text)
        self.assertNotIn('灵泉恢复 40%', help_text)
        self.assertNotIn('精英胜利 +', help_text)
        self.assertNotIn('最高 Lv.', help_text)
        self.assertIn('攻击 75', render.class_detail('C04'))
        self.assertIn('自身攻击的25%', render.class_detail('C04'))


if __name__ == '__main__':
    unittest.main()
