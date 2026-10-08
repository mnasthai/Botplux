"""Public dungeon command boundaries and operator configuration."""
from dataclasses import replace
from pathlib import Path
import tempfile
import unittest

from wechat_receiver.games.commands import Command, parse_command
from wechat_receiver.games.config import GameConfig, load_game_config
from test_xiuxian_commands import message


class DungeonCommandTests(unittest.TestCase):
    def test_group_commands_keep_decision_tokens(self):
        cases = {
            '#副本': Command('dungeon_help'),
            '#副本帮助': Command('dungeon_help'),
            '#副本状态': Command('dungeon_status'),
            '#副本创建': Command('dungeon_create'),
            '#副本加入 A7C2': Command('dungeon_join', 'A7C2'),
            '#副本准备': Command('dungeon_ready'),
            '#副本出发': Command('dungeon_start'),
            '#副本退出': Command('dungeon_leave'),
            '#副本取消': Command('dungeon_leave'),
            '#职业': Command('dungeon_class'),
            '#职业 丹修': Command('dungeon_class', '丹修'),
            '#职业 详情 丹修': Command('dungeon_class', '详情 丹修'),
            '#职业 详情 c04': Command('dungeon_class', '详情 c04'),
            '#灵根': Command('dungeon_roots'),
            '#灵根 R01': Command('dungeon_roots', 'R01'),
            '#灵根领取 R01': Command('dungeon_root_claim', 'R01'),
            '#灵根装配 1 断岳灵根': Command('dungeon_root_equip', '1 断岳灵根'),
            '#灵根卸下 2': Command('dungeon_root_remove', '2'),
            '#灵根兑换 R30': Command('dungeon_root_exchange', 'R30'),
            '#副本选择 A7C2.1 2': Command('dungeon_choose', 'A7C2.1 2'),
            '#副本选宝 A7C2.2 1': Command('dungeon_loot', 'A7C2.2 1'),
            '#副本行动 A7C2.3 防御': Command('dungeon_act', 'A7C2.3 防御'),
            '#副本行动 A7C2.3 灵药': Command('dungeon_act', 'A7C2.3 灵药'),
            '#副本行动 A7C2.3 闪避 前段': Command('dungeon_act', 'A7C2.3 闪避 前段'),
            '#副本行动 A7C2.3 闪避 后段': Command('dungeon_act', 'A7C2.3 闪避 后段'),
            '#副本行动 A7C2.3 回气': Command('dungeon_act', 'A7C2.3 回气'),
            '#副本行动 A7C2.3 救援 2': Command('dungeon_act', 'A7C2.3 救援 2'),
            '#副本行动 A7C2.3 普攻 2 毒囊': Command('dungeon_act', 'A7C2.3 普攻 2 毒囊'),
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                self.assertEqual(expected, parse_command(message(text)))
                self.assertIsNone(parse_command(message(text, group=False)))
        self.assertEqual(Command('dungeon_help'), parse_command(
            message('@机器人\u2005 #副本', mention_state='explicit_self')))

    def test_incomplete_and_oversized_inputs_do_not_become_decisions(self):
        for text in (
            '#副本选择 2', '#副本选宝 1', '#副本行动 防御',
            '#副本行动 A7C2.3 防御 多 余 参数', '#副本选择 A7C2.3 4',
            '#副本选择 ' + '9' * 5000 + ' 1', '#灵根装配 0 R01',
            '#灵根卸下 4', '#灵根领取', '#副本创建 随意', '#副本创建 练习', '#副本出发 多余',
            '#职业 详情', '#职业 丹修 多余', '#职业 详情 丹修 多余',
        ):
            with self.subTest(text=text[:70]):
                self.assertEqual('usage', parse_command(message(text)).kind)
        for text in ('#副本状态abc', '#副本行动A7C2.3 防御', '聊天 #副本', '#职业剑修'):
            self.assertIsNone(parse_command(message(text)))

    def test_idle_and_round_limits_load_without_changing_existing_config(self):
        self.assertEqual(600, GameConfig().dungeon_idle_timeout_seconds)
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'game.toml'
            path.write_text('[game]\ndungeon_idle_timeout_seconds=300\n'
                            'dungeon_round_timeout_seconds=60\n', encoding='utf-8')
            config = load_game_config(path)
        self.assertEqual((300, 60), (config.dungeon_idle_timeout_seconds,
                                    config.dungeon_round_timeout_seconds))
        for value in (0, -1, True, 1.5):
            with self.assertRaises(ValueError):
                replace(config, dungeon_idle_timeout_seconds=value)


if __name__ == '__main__':
    unittest.main()
