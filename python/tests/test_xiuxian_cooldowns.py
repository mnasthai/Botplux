"""Cross-feature cooldown regressions through the real transactional router."""
import unittest

import test_xiuxian_shop as shop_tests


class CultivationCooldownTests(unittest.TestCase):
    def setUp(self):
        self.game = shop_tests.XiuxianShopTests()
        self.game.setUp()
        self.addCleanup(self.game.tearDown)
        self.game.seed_player('alice', '青玄', stones=200)
        self.game.seed_player('bob', '百里', stones=200)

    def curse(self):
        game = self.game
        game.send('#购买 断脉散', 'bob')
        game.send('#使用 断脉散 @青玄\u2005', 'bob',
                  mentioned_ids=('alice',), mention_state='explicit_other')
        self.assertIn('暗算得手', game.last_reply())

    def test_liuguang_ready_does_not_charge_for_unneeded_pill(self):
        game = self.game
        game.seed_item('alice', 'F10000001', 'liuguang_suo')
        game.send('#自主修炼')
        game.now += 2400
        game.send('#修仙')
        self.assertIn('自主修炼：可进行', game.last_reply())
        game.send('#购买 洗髓丹')
        self.assertIn('并未处于冷却中', game.last_reply())
        self.assertEqual(200, game.player()['spirit_stones'])
        self.assertEqual(0, game.store.db.execute(
            "SELECT count(*) FROM game_shop_purchases WHERE item_id='xisui_dan'"
        ).fetchone()[0])
        game.send('#自主修炼')
        self.assertIn('修为 +5', game.last_reply())

    def test_duanmai_panel_and_pill_use_the_extended_cooldown(self):
        game = self.game
        game.send('#自主修炼')
        self.curse()
        game.now += 5400
        game.send('#修仙')
        self.assertIn('自主修炼：1800 秒后可进行', game.last_reply())
        game.send('#自主修炼')
        self.assertIn('尚需 1800 秒', game.last_reply())
        game.send('#购买 洗髓丹')
        self.assertIn('购买成功', game.last_reply())
        self.assertEqual(190, game.player()['spirit_stones'])
        game.send('#自主修炼')
        self.assertIn('修为 +5', game.last_reply())
        self.assertEqual(0, game.store.db.execute(
            "SELECT count(*) FROM game_player_debuffs WHERE target_player_id='alice'"
        ).fetchone()[0])

    def test_liuguang_and_duanmai_share_the_exact_expiry_boundary(self):
        game = self.game
        game.seed_item('alice', 'F10000001', 'liuguang_suo')
        game.send('#自主修炼')
        self.curse()
        game.now += 5399
        game.send('#修仙')
        self.assertIn('自主修炼：1 秒后可进行', game.last_reply())
        game.send('#自主修炼')
        self.assertIn('尚需 1 秒', game.last_reply())
        game.now += 1
        game.send('#购买 洗髓丹')
        self.assertIn('并未处于冷却中', game.last_reply())
        self.assertEqual(200, game.player()['spirit_stones'])
        game.send('#自主修炼')
        self.assertIn('修为 +5', game.last_reply())


if __name__ == '__main__':
    unittest.main()
