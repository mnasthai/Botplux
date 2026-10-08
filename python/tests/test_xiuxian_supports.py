"""Focused tests for the transactional spectator-support ledger."""
from __future__ import annotations

from dataclasses import asdict, replace
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from wechat_receiver.games.config import DEFAULT_GAME_CONFIG
from wechat_receiver.games.supports import SupportError, SupportLedger
from wechat_receiver.plugin_context import plugin_context
from wechat_receiver.store import Store


class XiuxianSupportTests(unittest.TestCase):
    account = 'wxid_bot'
    group = '22913213991@chatroom'
    other_group = '998877@chatroom'
    now = datetime(2026, 9, 18, 12, tzinfo=timezone.utc)

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temporary.name) / 'messages.sqlite3')
        self.config = replace(DEFAULT_GAME_CONFIG, duel_enabled=True)
        for player, name in (('alice', '青玄'), ('bob', '白墨'), ('s1', '甲一'),
                             ('s2', '乙二'), ('s3', '丙三'), ('s4', '丁四')):
            self.seed_player(self.group, player, name)
        for player, name in (('alice2', '青云'), ('bob2', '墨山'), ('s1', '异乡甲')):
            self.seed_player(self.other_group, player, name)
        self.seed_duel('DPRIMARY', self.group, 'alice', 'bob')
        self.seed_duel('DOTHER', self.other_group, 'alice2', 'bob2')
        self.store.db.commit()

    def tearDown(self):
        self.store.close()
        self.temporary.cleanup()

    def seed_player(self, group, player, name, stones=100):
        self.store.db.execute("""INSERT INTO game_players
            (account_id,group_id,player_id,dao_name,dao_name_key,spirit_stones,joined_at)
            VALUES(?,?,?,?,?,?,?)""",
            (self.account, group, player, name, name.casefold(), stones,
             self.now.isoformat().replace('+00:00', 'Z')))

    def seed_duel(self, duel_id, group, challenger, challenged):
        rules = json.dumps(asdict(self.config), ensure_ascii=True, separators=(',', ':'))
        self.store.db.execute("""INSERT INTO game_duels
            (duel_id,account_id,group_id,challenger_id,challenged_id,state,
             rules_version,rules_json,created_at)
            VALUES(?,?,?,?,?,'supporting',?,?,?)""",
            (duel_id, self.account, group, challenger, challenged,
             self.config.rules_version, rules, self.now.isoformat().replace('+00:00', 'Z')))

    def duel(self, duel_id='DPRIMARY'):
        return self.store.db.execute('SELECT * FROM game_duels WHERE duel_id=?', (duel_id,)).fetchone()

    def transact(self, operation, group=None):
        self.store.db.execute('BEGIN IMMEDIATE')
        try:
            with plugin_context(
                    self.store.db, account_id=self.account, plugin_name='xiuxian',
                    connection_id='support-test', message=None, now=self.now.timestamp(),
                    started_at=self.now.timestamp() - 1, previous_poll_at=self.now.timestamp(),
                    game_config=self.config, allowed_targets=frozenset({self.group, self.other_group}),
                    runtime_issue=None) as context:
                result = operation(SupportLedger(context, group or self.group))
            self.store.db.commit()
            return result
        except BaseException:
            self.store.db.rollback()
            raise

    def register(self, supporter, target, amount, *, duel_id='DPRIMARY', group=None):
        duel = self.duel(duel_id)
        return self.transact(
            lambda ledger: ledger.register(
                duel, supporter, target, amount, self.now.isoformat().replace('+00:00', 'Z')),
            group=group,
        )

    def balance(self, player, group=None):
        return self.store.db.execute("""SELECT spirit_stones FROM game_players
            WHERE account_id=? AND group_id=? AND player_id=?""",
            (self.account, group or self.group, player)).fetchone()[0]

    def test_equal_pools_pay_winner_supporter_and_conserve_total(self):
        self.register('s1', 'alice', 50)
        self.register('s2', 'bob', 50)
        totals = self.transact(lambda ledger: ledger.totals(self.duel()))
        self.assertEqual({'alice': 50, 'bob': 50}, totals)

        rows = self.transact(lambda ledger: ledger.settle(self.duel(), winner='alice'))
        self.assertEqual([('s1', 100, 'paid'), ('s2', 0, 'paid')],
                         [(row['supporter_id'], row['payout'], row['settlement_state']) for row in rows])
        self.assertEqual((150, 50), (self.balance('s1'), self.balance('s2')))
        self.assertEqual(200, self.balance('s1') + self.balance('s2'))

    def test_proportional_30_70_against_100_uses_integer_payouts(self):
        self.register('s1', 'alice', 30)
        self.register('s2', 'alice', 70)
        self.register('s3', 'bob', 100)
        rows = self.transact(lambda ledger: ledger.settle(self.duel(), winner='alice'))
        self.assertEqual([60, 140, 0], [row['payout'] for row in rows])
        self.assertEqual((130, 170, 0),
                         (self.balance('s1'), self.balance('s2'), self.balance('s3')))

    def test_equal_remainder_awards_tail_unit_by_registration_order(self):
        first = self.register('s1', 'alice', 10)
        second = self.register('s2', 'alice', 10)
        self.assertLess(first['support_order'], second['support_order'])
        self.register('s3', 'bob', 11)
        rows = self.transact(lambda ledger: ledger.settle(self.duel(), winner='alice'))
        self.assertEqual([16, 15, 0], [row['payout'] for row in rows])

    def test_one_sided_pool_refunds_and_repeat_settlement_does_not_pay_again(self):
        self.register('s1', 'alice', 50)
        first = self.transact(lambda ledger: ledger.settle(self.duel(), winner='alice'))
        balance = self.balance('s1')
        second = self.transact(lambda ledger: ledger.settle(self.duel(), winner='bob'))
        self.assertEqual((50, 'refunded'), (first[0]['payout'], first[0]['settlement_state']))
        self.assertEqual((50, 'refunded'), (second[0]['payout'], second[0]['settlement_state']))
        self.assertEqual(100, balance)
        self.assertEqual(balance, self.balance('s1'))

    def test_winner_none_refunds_both_sides_and_is_idempotent(self):
        self.register('s1', 'alice', 40)
        self.register('s2', 'bob', 60)
        first = self.transact(lambda ledger: ledger.settle(self.duel()))
        second = self.transact(lambda ledger: ledger.settle(self.duel(), winner='alice'))
        self.assertEqual([(40, 'refunded'), (60, 'refunded')],
                         [(row['payout'], row['settlement_state']) for row in first])
        self.assertEqual([(40, 'refunded'), (60, 'refunded')],
                         [(row['payout'], row['settlement_state']) for row in second])
        self.assertEqual((100, 100), (self.balance('s1'), self.balance('s2')))

    def test_registration_rejects_invalid_or_unsafe_requests_before_writing(self):
        invalid = (
            ('alice', 'bob', 10, '参战者不能'),
            ('ghost', 'alice', 10, '请先发送'),
            ('s1', 'ghost', 10, '只能支持'),
            ('s1', 'alice', -1, '10～100'),
            ('s1', 'alice', 9, '10～100'),
            ('s1', 'alice', 101, '10～100'),
            ('s1', 'alice', True, '十进制整数'),
            ('s1', 'alice', 10.0, '十进制整数'),
        )
        for supporter, target, amount, message in invalid:
            with self.subTest(supporter=supporter, target=target, amount=amount):
                with self.assertRaisesRegex(SupportError, message):
                    self.register(supporter, target, amount)
        self.assertEqual(0, self.store.db.execute('SELECT count(*) FROM game_supports').fetchone()[0])
        self.assertEqual(100, self.balance('s1'))

        self.store.db.execute("UPDATE game_players SET spirit_stones=5 WHERE group_id=? AND player_id='s4'",
                              (self.group,))
        self.store.db.commit()
        with self.assertRaisesRegex(SupportError, '灵石不足'):
            self.register('s4', 'alice', 10)
        self.assertEqual(5, self.balance('s4'))

        self.register('s1', 'alice', 10)
        with self.assertRaisesRegex(SupportError, '只能支持一次'):
            self.register('s1', 'bob', 20)
        self.assertEqual(90, self.balance('s1'))

    def test_database_failure_rolls_back_all_settlement_writes(self):
        self.register('s1', 'alice', 50)
        self.register('s2', 'bob', 50)
        self.store.db.execute("""CREATE TRIGGER fail_support_settlement
            BEFORE UPDATE OF settlement_state ON game_supports
            BEGIN SELECT RAISE(ABORT,'injected settlement failure'); END""")
        self.store.db.commit()
        with self.assertRaises(sqlite3.IntegrityError):
            self.transact(lambda ledger: ledger.settle(self.duel(), winner='alice'))
        self.assertEqual((50, 50), (self.balance('s1'), self.balance('s2')))
        rows = self.store.db.execute("""SELECT settlement_state,payout FROM game_supports
            ORDER BY rowid""").fetchall()
        self.assertEqual([('pending', None), ('pending', None)], [tuple(row) for row in rows])

    def test_mixed_settlement_state_is_reported_without_second_payment(self):
        self.register('s1', 'alice', 50)
        self.register('s2', 'bob', 50)
        self.store.db.execute("""UPDATE game_supports SET settlement_state='paid',payout=100
            WHERE supporter_id='s1'""")
        self.store.db.commit()
        with self.assertLogs(level='WARNING'):
            with self.assertRaisesRegex(SupportError, '结算状态异常'):
                self.transact(lambda ledger: ledger.settle(self.duel(), winner='alice'))
        self.assertEqual((50, 50), (self.balance('s1'), self.balance('s2')))

    def test_group_scope_keeps_rows_and_totals_isolated(self):
        self.register('s1', 'alice', 20)
        self.register('s1', 'alice2', 30, duel_id='DOTHER', group=self.other_group)
        primary = self.transact(lambda ledger: ledger.rows(self.duel()), group=self.group)
        other = self.transact(lambda ledger: ledger.rows(self.duel('DOTHER')), group=self.other_group)
        self.assertEqual([20], [row['amount'] for row in primary])
        self.assertEqual([30], [row['amount'] for row in other])
        self.assertEqual({'alice': 20, 'bob': 0},
                         self.transact(lambda ledger: ledger.totals(self.duel()), group=self.group))
        self.assertEqual({'alice2': 30, 'bob2': 0},
                         self.transact(lambda ledger: ledger.totals(self.duel('DOTHER')),
                                       group=self.other_group))
        with self.assertRaisesRegex(SupportError, '不属于当前群'):
            self.transact(lambda ledger: ledger.rows(self.duel('DOTHER')), group=self.group)


if __name__ == '__main__':
    unittest.main()
