"""Storage migration tests for the permanent Devil's Bargain tier."""

import json
import sqlite3
import unittest

from wechat_receiver.games.schema import GAME_SCHEMA, initialize_game_schema


_MAX_TIER_DEFINITION = """        devil_max_contract_tier INTEGER NOT NULL DEFAULT 0
            CHECK(typeof(devil_max_contract_tier) = 'integer' AND devil_max_contract_tier >= 0),
"""


class DevilMaxContractTierMigrationTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(':memory:')

    def tearDown(self):
        self.db.close()

    def legacy_schema(self):
        assert _MAX_TIER_DEFINITION in GAME_SCHEMA
        self.db.executescript(GAME_SCHEMA.replace(_MAX_TIER_DEFINITION, ''))

    def player(self, account='account', group='group', player='alice'):
        return self.db.execute('''SELECT devil_contract_tier, devil_max_contract_tier,
                spirit_stones, cultivation, devil_total_borrowed FROM game_players
                WHERE account_id=? AND group_id=? AND player_id=?''',
                (account, group, player)).fetchone()

    def add_player(self, account='account', group='group', player='alice', *,
                   tier=0, stones=321, cultivation=123, borrowed=987):
        self.db.execute('''INSERT INTO game_players
            (account_id, group_id, player_id, dao_name, dao_name_key,
             devil_contract_tier, spirit_stones, cultivation, devil_total_borrowed)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)''',
            (account, group, player, '道友', player, tier, stones, cultivation, borrowed))

    def add_sign(self, event, account='account', group='group', player='alice', *, tier=0):
        resource = json.dumps({'message_fingerprint': event,
                               'changes': {'devil_contract_tier': tier}})
        self.db.execute('''INSERT INTO game_actions
            (event_key, account_id, group_id, player_id, action_kind, resource_json)
            VALUES (?, ?, ?, ?, 'devil_sign', ?)''',
            (event, account, group, player, resource))

    def test_new_database_defaults_to_zero_and_enforces_nonnegative_integer(self):
        initialize_game_schema(self.db)
        self.add_player()
        self.assertEqual((0, 0, 321, 123, 987), self.player())
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.execute('UPDATE game_players SET devil_max_contract_tier=-1')
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.execute("UPDATE game_players SET devil_max_contract_tier='bad'")

    def test_legacy_current_tier_sets_known_historical_floor(self):
        self.legacy_schema()
        self.add_player(tier=3)
        initialize_game_schema(self.db)
        self.assertEqual((3, 3, 321, 123, 987), self.player())

    def test_cleared_contract_recovers_highest_successful_sign_in_exact_scope(self):
        self.legacy_schema()
        self.add_player()
        self.add_player(player='bob')
        self.add_player(group='other')
        self.add_player(account='other')
        self.add_sign('alice-low', tier=2)
        self.add_sign('alice-high', tier=4)
        self.add_sign('bob', player='bob', tier=5)
        self.add_sign('other-group', group='other', tier=3)
        self.add_sign('other-account', account='other', tier=1)
        initialize_game_schema(self.db)
        self.assertEqual((0, 4, 321, 123, 987), self.player())
        self.assertEqual(5, self.player(player='bob')[1])
        self.assertEqual(3, self.player(group='other')[1])
        self.assertEqual(1, self.player(account='other')[1])

    def test_invalid_sign_values_and_unrelated_actions_do_not_set_history(self):
        self.legacy_schema()
        self.add_player()
        for event, tier in [('negative', -1), ('boolean', True), ('fraction', 2.5),
                            ('string', '4'), ('overflow', 2**63)]:
            self.add_sign(event, tier=tier)
        self.db.execute('''INSERT INTO game_actions
            (event_key, account_id, group_id, player_id, action_kind, resource_json)
            VALUES ('other-kind', 'account', 'group', 'alice', 'devil_status', ?)''',
            (json.dumps({'changes': {'devil_contract_tier': 5}}),))
        initialize_game_schema(self.db)
        self.assertEqual((0, 0, 321, 123, 987), self.player())

    def test_reinitialization_preserves_greater_history_and_assets(self):
        self.legacy_schema()
        self.add_player(tier=2)
        self.add_sign('older-high', tier=4)
        initialize_game_schema(self.db)
        self.assertEqual((2, 4, 321, 123, 987), self.player())
        self.db.execute('UPDATE game_players SET devil_max_contract_tier=5')
        initialize_game_schema(self.db)
        self.assertEqual((2, 5, 321, 123, 987), self.player())


if __name__ == '__main__':
    unittest.main()
