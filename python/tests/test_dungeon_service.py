"""Dungeon transaction and reward invariants, independent of message transport."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
import random
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from wechat_receiver.games.dungeon import repository, service
from wechat_receiver.games.schema import initialize_game_schema


class DungeonServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / 'game.sqlite3'
        self.db = sqlite3.connect(self.path)
        self.db.row_factory = sqlite3.Row
        initialize_game_schema(self.db)
        repository.initialize_temp(self.db)
        self.db.execute('CREATE TABLE outbox(request_id TEXT PRIMARY KEY)')
        self.db.commit()
        self.now = datetime(2026, 9, 26, 5, tzinfo=timezone.utc).timestamp()
        self.seq = 0
        self.rng = random.Random(7)
        for player, name in [('alice', '青玄'), ('bob', '墨羽')]:
            self.db.execute('''INSERT INTO game_players
                (account_id,group_id,player_id,dao_name,dao_name_key)
                VALUES('bot','room@chatroom',?,?,?)''', (player, name, player))
        self.db.commit()

    def tearDown(self):
        self.db.close()
        self.temp.cleanup()

    def context(self, player='alice', text='', *, event=None, message_id=None,
                account='bot', group='room@chatroom', connection='live'):
        self.seq += 1
        msg = SimpleNamespace(content=text, message_id_candidate=message_id or str(self.seq + 9000))
        return SimpleNamespace(account_id=account, conversation_id=group, user_id=player,
            event_key=event or f'event:{self.seq}', message=msg, now=self.now,
            store=self.db, connection_id=connection,
            game_config=SimpleNamespace(dungeon_idle_timeout_seconds=600,
                dungeon_round_timeout_seconds=90),
            allowed_targets=frozenset({group}),
            reply_request_id=lambda key: 'reply:' + key)

    def send(self, kind, arg='', player='alice', **context_args):
        text = kind + ' ' + arg
        ctx = self.context(player, text, **context_args)
        self.db.execute('BEGIN IMMEDIATE')
        try:
            result = service.handle_command(SimpleNamespace(kind=kind, argument=arg),
                                            ctx, rng=self.rng)
            self.db.commit()
            return result
        except BaseException:
            self.db.rollback()
            raise

    def current(self, player='alice'):
        run = repository.active_run(self.db, 'bot', 'room@chatroom', player)
        return run, repository.load_state(self.db, run['run_id']) if run else None

    def start(self, players=('alice',)):
        self.send('dungeon_create')
        run, _ = self.current()
        for player in players[1:]:
            self.send('dungeon_join', run['code'], player)
        for player in players:
            self.send('dungeon_ready', player=player)
        self.send('dungeon_start')
        return self.current()[0]

    def test_cancel_before_departure_and_daily_count_only_at_start(self):
        self.send('dungeon_create')
        run, _ = self.current()
        self.assertEqual(0, repository.daily_departures(self.db, 'bot', 'room@chatroom',
                                                         'alice', service._today(self.now)))
        self.send('dungeon_leave')
        self.assertIsNone(self.current()[0])
        self.assertEqual('finished', self.db.execute('SELECT state FROM dungeon_runs WHERE run_id=?',
                                                     (run['run_id'],)).fetchone()[0])
        self.assertEqual(0, repository.daily_departures(self.db, 'bot', 'room@chatroom',
                                                         'alice', service._today(self.now)))
        self.start()
        self.assertEqual(1, repository.daily_departures(self.db, 'bot', 'room@chatroom',
                                                         'alice', service._today(self.now)))

    def test_same_message_id_different_capture_is_not_applied_twice(self):
        self.send('dungeon_create')
        self.send('dungeon_ready', event='capture:a', message_id='912345')
        count = self.db.execute('SELECT COUNT(*) FROM dungeon_actions').fetchone()[0]
        result = self.send('dungeon_ready', event='capture:b', message_id='912345')
        self.assertIsNone(result)
        self.assertEqual(count, self.db.execute('SELECT COUNT(*) FROM dungeon_actions').fetchone()[0])

    def test_class_details_are_read_only_and_available_during_a_run(self):
        self.send('dungeon_class', '体修')
        overview = self.send('dungeon_class')
        self.assertIn('#职业 详情 剑修', overview)
        self.assertNotIn('CD2完整回合', overview)
        detail = self.send('dungeon_class', '详情 丹修')
        self.assertIn('回元术', detail)
        self.assertIn('济世丹华', detail)
        self.assertEqual(detail, self.send('dungeon_class', '详情 c04'))
        self.assertIn('未开放的职业', self.send('dungeon_class', '详情 C99'))
        self.send('dungeon_create')
        for phase in ('gathering', 'explore'):
            if phase == 'explore':
                self.send('dungeon_ready')
                self.send('dungeon_start')
            before = self.current()[1]
            self.now += 10
            self.assertEqual(detail, self.send('dungeon_class', '详情 丹修'))
            self.assertEqual(before, self.current()[1])
            self.assertIn('不能更换', self.send('dungeon_class', '丹修'))
        self.assertEqual('C02', repository.profile(
            self.db, 'bot', 'room@chatroom', 'alice')['class_id'])

    def test_same_message_id_in_other_group_is_independent(self):
        self.db.execute('''INSERT INTO game_players
            (account_id,group_id,player_id,dao_name,dao_name_key)
            VALUES('bot','second@chatroom','alice','玄初','alice')''')
        self.db.commit()
        self.send('dungeon_roots', event='room:event', message_id='998877')
        other = self.send('dungeon_roots', group='second@chatroom',
                          event='other:event', message_id='998877')
        self.assertIn('职业', other)
        self.assertEqual(2, self.db.execute('''SELECT COUNT(*) FROM dungeon_actions
            WHERE message_id='998877' ''').fetchone()[0])

    def test_invalid_and_partial_action_do_not_extend_idle_time(self):
        run = self.start(('alice', 'bob'))
        state, progress, deadline = self.current()[1]
        # Enter a synthetic night state so the test isolates the multiplayer timer.
        state['phase'] = 'battle'
        state['battle'] = service.combat.start_battle(state['fighters'], 'M01', rng=self.rng)
        state['actions'] = {}
        self.db.execute('BEGIN IMMEDIATE')
        repository.save_state(self.db, run['run_id'], state, progress, self.now + 90)
        self.db.commit()
        self.now += 500
        self.send('dungeon_act', state['token'] + ' 防御')
        self.assertEqual(progress, self.current()[1][1])
        self.send('dungeon_status')
        self.assertEqual(progress, self.current()[1][1])
        self.now += 101
        self.db.execute('BEGIN IMMEDIATE')
        notices = service.on_poll(self.context(text='', connection=None))
        self.db.commit()
        self.assertEqual([], notices)
        self.assertIsNone(self.current()[0])
        self.assertEqual('finished', self.db.execute('SELECT state FROM dungeon_runs WHERE run_id=?',
                                                     (run['run_id'],)).fetchone()[0])

    def test_reward_once_on_restart_and_starter_claim_remains_available(self):
        run = self.start()
        self.db.execute('UPDATE dungeon_runs SET highest_night=2 WHERE run_id=?', (run['run_id'],))
        self.db.commit()
        self.db.close()
        self.db = sqlite3.connect(self.path)
        self.db.row_factory = sqlite3.Row
        self.db.execute('BEGIN IMMEDIATE')
        service.on_start(self.context(text='', connection=None))
        self.db.commit()
        profile = repository.profile(self.db, 'bot', 'room@chatroom', 'alice')
        self.assertEqual((1, 2), (profile['dust'], profile['highest_night']))
        self.assertEqual((20, 250), tuple(self.db.execute('''SELECT cultivation,spirit_stones
            FROM game_players WHERE account_id='bot' AND group_id='room@chatroom'
            AND player_id='alice' ''').fetchone()))
        self.db.commit()
        self.db.execute('BEGIN IMMEDIATE')
        service.on_start(self.context(text='', connection=None))
        self.db.commit()
        self.assertEqual(1, repository.profile(self.db, 'bot', 'room@chatroom', 'alice')['dust'])
        self.db.commit()
        self.assertIn('获得', self.send('dungeon_root_claim', 'R01'))
        self.assertIn('已领取', self.send('dungeon_root_claim', 'R13'))

    def test_temp_state_and_economy_rollback_together(self):
        self.start()
        run, loaded = self.current()
        state, progress, deadline = loaded
        original = state['version']
        self.db.execute('BEGIN IMMEDIATE')
        state['version'] = 999
        repository.save_state(self.db, run['run_id'], state, self.now + 1)
        self.db.execute('UPDATE dungeon_profiles SET dust=8 WHERE account_id=? AND group_id=? AND player_id=?',
                        ('bot', 'room@chatroom', 'alice'))
        self.db.rollback()
        self.assertEqual(original, self.current()[1][0]['version'])
        self.assertEqual(0, repository.profile(self.db, 'bot', 'room@chatroom', 'alice')['dust'])

    def test_daily_departure_limit_and_beijing_date(self):
        first_day = service._today(self.now)
        for _ in range(5):
            self.start()
            self.send('dungeon_leave')
        self.assertEqual(5, repository.daily_departures(self.db, 'bot', 'room@chatroom',
                                                         'alice', first_day))
        self.send('dungeon_create')
        self.send('dungeon_ready')
        denied = self.send('dungeon_start')
        self.assertIn('次数已用完', denied)
        self.assertEqual('gathering', self.current()[0]['state'])
        self.send('dungeon_leave')
        self.now += 86400
        self.start()
        self.assertEqual(1, repository.daily_departures(self.db, 'bot', 'room@chatroom',
                                                         'alice', service._today(self.now)))

    def test_practice_creation_is_rejected(self):
        result = self.send('dungeon_create', '练习')
        self.assertIn('不再提供练习', result)
        self.assertIsNone(self.current()[0])

    def test_clear_root_roll_is_independent_and_duplicate_becomes_dust(self):
        class Rolls:
            def __init__(self, *values):
                self.values = list(values)

            def random(self):
                return self.values.pop(0)

            def choice(self, values):
                return list(values)[0]

        first = self.start(('alice', 'bob'))
        state = self.current()[1][0]
        state['highest_night'] = 3
        self.db.execute('BEGIN IMMEDIATE')
        text = service._finish(self.context(text=''), first, state, '通关', Rolls(.1, .9),
                               victory=True)
        self.db.commit()
        self.assertIn('本次未掉落灵根', text)
        self.assertEqual({'R01'}, {r['root_id'] for r in repository.roots(
            self.db, 'bot', 'room@chatroom', 'alice')})
        self.assertEqual([], repository.roots(self.db, 'bot', 'room@chatroom', 'bob'))
        second = self.start(('alice', 'bob'))
        state = self.current()[1][0]
        state['highest_night'] = 3
        self.db.execute('BEGIN IMMEDIATE')
        text = service._finish(self.context(text=''), second, state, '通关', Rolls(.1, .1),
                               victory=True)
        self.db.commit()
        self.assertIn('重复断岳灵根已转2灵尘', text)
        self.assertEqual((4, 2), (
            repository.profile(self.db, 'bot', 'room@chatroom', 'alice')['dust'],
            repository.profile(self.db, 'bot', 'room@chatroom', 'bob')['dust']))
        self.db.commit()
        self.db.execute('BEGIN IMMEDIATE')
        duplicate = service._finish(self.context(text=''), second, state, '通关', Rolls(.1, .1),
                                    victory=True)
        self.db.commit()
        self.assertIsNone(duplicate)
        self.assertEqual(4, repository.profile(self.db, 'bot', 'room@chatroom', 'alice')['dust'])

    def test_rewards_match_help_and_credit_only_the_highest_cleared_night(self):
        help_text = self.send('dungeon_help')
        expected = ((0, 0, 0), (10, 50, 0), (20, 150, 1), (30, 250, 1))
        labels = ('尚未通过第一夜', '第一夜', '第二夜', '第三夜')
        for highest, (cult, stones, dust) in enumerate(expected):
            with self.subTest(highest=highest):
                self.assertIn(f'{labels[highest]}：{cult} 修为、{stones} 灵石、{dust} 灵尘', help_text)
                run = self.start()
                state = self.current()[1][0]
                state['highest_night'] = highest
                before = tuple(self.db.execute('''SELECT cultivation,spirit_stones
                    FROM game_players WHERE player_id='alice' ''').fetchone())
                before_dust = self.db.execute('''SELECT dust FROM dungeon_profiles
                    WHERE account_id='bot' AND group_id='room@chatroom' AND player_id='alice' ''').fetchone()[0]
                self.db.execute('BEGIN IMMEDIATE')
                service._finish(self.context(), run, state, '结算', random.Random(0),
                                victory=highest == 3)
                self.db.commit()
                after = tuple(self.db.execute('''SELECT cultivation,spirit_stones
                    FROM game_players WHERE player_id='alice' ''').fetchone())
                after_dust = self.db.execute('''SELECT dust FROM dungeon_profiles
                    WHERE account_id='bot' AND group_id='room@chatroom' AND player_id='alice' ''').fetchone()[0]
                self.assertEqual((cult, stones, dust),
                                 (after[0] - before[0], after[1] - before[1], after_dust - before_dust))
                ledger = self.db.execute('''SELECT cultivation_awarded,stones_awarded,dust_awarded
                    FROM dungeon_run_members WHERE run_id=? AND player_id='alice' ''',
                    (run['run_id'],)).fetchone()
                self.assertEqual((cult, stones, dust), tuple(ledger))
        self.assertIn('不逐夜累加', help_text)
        self.assertIn('每人独立以 50% 概率', help_text)

    def test_round_timeout_requires_real_action_and_scope_isolated(self):
        run = self.start(('alice', 'bob'))
        state, progress, _ = self.current()[1]
        state['phase'] = 'battle'
        state['battle'] = service.combat.start_battle(state['fighters'], 'M01', rng=self.rng)
        state['actions'] = {}
        self.db.execute('BEGIN IMMEDIATE')
        repository.save_state(self.db, run['run_id'], state, progress, self.now + 90)
        self.db.commit()
        self.assertIsNone(repository.active_run(self.db, 'bot', 'other@chatroom', 'alice'))
        self.assertIsNone(repository.active_run(self.db, 'other_bot', 'room@chatroom', 'alice'))
        self.now += 91
        self.db.execute('BEGIN IMMEDIATE')
        service.on_poll(self.context(text='', connection=None))
        self.db.commit()
        self.assertEqual(state['token'], self.current()[1][0]['token'])
        self.assertEqual(progress, self.current()[1][1])
        self.send('dungeon_act', state['token'] + ' 防御')
        self.assertEqual(progress, self.current()[1][1])
        self.db.execute('BEGIN IMMEDIATE')
        service.on_poll(self.context(text='', connection=None))
        self.db.commit()
        later, changed_progress, _ = self.current()[1]
        self.assertNotEqual(state['token'], later['token'])
        self.assertEqual(self.now, changed_progress)

    def test_full_spring_can_be_skipped_and_advance(self):
        run = self.start()
        state, progress, _ = self.current()[1]
        state['fighters'][0]['hp'] = state['fighters'][0]['max_hp']
        state['fighters'][0]['potions'] = 3
        service._loot_options(state, 'spring', self.rng)
        self.db.execute('BEGIN IMMEDIATE')
        repository.save_state(self.db, run['run_id'], state, progress)
        self.db.commit()
        result = self.send('dungeon_loot', state['token'] + ' 3')
        self.assertIn('放弃', result)
        self.assertEqual('explore', self.current()[1][0]['phase'])

    def test_daytime_potion_is_instant_self_only_and_replay_safe(self):
        run = self.start(('alice', 'bob'))
        for phase in ('explore', 'loot'):
            with self.subTest(phase=phase):
                state, progress, _ = self.current()[1]
                if phase == 'loot':
                    service._loot_options(state, 'spring', self.rng)
                actor = state['fighters'][1]
                actor['hp'] = actor['max_hp'] * .1
                actor['potions'] = 3
                actor['fortunes'] = ['F05', 'F07']
                self.db.execute('BEGIN IMMEDIATE')
                repository.save_state(self.db, run['run_id'], state, progress)
                self.db.commit()
                token = state['token']
                for argument in ('OLD.1 灵药', token + ' 灵药 1',
                                 token + ' 灵药 2 多余', token + ' 进攻'):
                    self.assertIn('⚠️', self.send('dungeon_act', argument, 'bob'))
                    self.assertEqual(state, self.current()[1][0])
                message_id = '777001' if phase == 'explore' else '777002'
                self.assertIn('恢复', self.send('dungeon_act', token + ' 灵药', 'bob',
                                               message_id=message_id))
                after, after_progress, _ = self.current()[1]
                self.assertAlmostEqual(actor['max_hp'] * .4, after['fighters'][1]['hp'])
                self.assertEqual(2, after['fighters'][1]['potions'])
                self.assertEqual(state['fighters'][0], after['fighters'][0])
                for field in ('phase', 'token', 'choices', 'node', 'completed_nodes', 'experience'):
                    self.assertEqual(state[field], after[field])
                if phase == 'loot':
                    self.assertEqual(state['loot_taken'], after['loot_taken'])
                self.assertEqual(progress, after_progress)
                self.assertIsNone(self.send('dungeon_act', token + ' 灵药', 'bob',
                                            message_id=message_id))
                self.assertEqual(after, self.current()[1][0])

    def test_daytime_potion_rejects_unusable_cases_and_caps_health(self):
        run = self.start()
        for fraction, potions in ((1, 2), (.5, 0), (0, 2), (.9, 2)):
            with self.subTest(fraction=fraction, potions=potions):
                state, progress, _ = self.current()[1]
                actor = state['fighters'][0]
                actor['hp'], actor['potions'] = actor['max_hp'] * fraction, potions
                self.db.execute('BEGIN IMMEDIATE')
                repository.save_state(self.db, run['run_id'], state, progress)
                self.db.commit()
                text = self.send('dungeon_act', state['token'] + ' 灵药')
                current = self.current()[1][0]
                if fraction != .9:
                    self.assertIn('⚠️', text)
                    self.assertEqual(state, current)
                else:
                    self.assertEqual(actor['max_hp'], current['fighters'][0]['hp'])
                    self.assertEqual(1, current['fighters'][0]['potions'])

    def test_spring_heals_forty_percent_and_caps_at_full(self):
        run = self.start(('alice', 'bob'))
        state, progress, _ = self.current()[1]
        for actor, fraction in zip(state['fighters'], (.1, .9)):
            actor['hp'] = actor['max_hp'] * fraction
        service._loot_options(state, 'spring', self.rng)
        self.db.execute('BEGIN IMMEDIATE')
        repository.save_state(self.db, run['run_id'], state, progress)
        self.db.commit()
        self.assertIn('40%', self.send('dungeon_status'))
        self.send('dungeon_loot', state['token'] + ' 1')
        after = self.current()[1][0]
        self.assertAlmostEqual(state['fighters'][0]['max_hp'] * .5, after['fighters'][0]['hp'])
        self.send('dungeon_loot', state['token'] + ' 1', 'bob')
        self.assertEqual(state['fighters'][1]['max_hp'], self.current()[1][0]['fighters'][1]['hp'])
        # The fallback healing reward at other nodes retains its original potency.
        actor = state['fighters'][0]
        service._apply_loot(actor, 'S_HEAL', node_kind='camp')
        self.assertAlmostEqual(actor['max_hp'] * .35, actor['hp'])

    def test_night_rewards_add_one_then_two_potions_before_next_stage(self):
        run = self.start(('alice', 'bob'))
        for night, extra in ((1, 1), (2, 2)):
            with self.subTest(night=night):
                state, progress, _ = self.current()[1]
                state['day'], state['night'], state['highest_night'] = night, night, night - 1
                state['fighters'][0]['potions'] = 0
                state['fighters'][1]['potions'] = 2
                service._battle_start(state, 'M01', night, self.rng)
                state['battle']['enemy']['hp'] = 1
                self.db.execute('BEGIN IMMEDIATE')
                repository.save_state(self.db, run['run_id'], state, progress, self.now + 90)
                self.db.commit()
                self.send('dungeon_act', state['token'] + ' 进攻')
                text = self.send('dungeon_act', state['token'] + ' 进攻', 'bob')
                self.assertIn(f'灵药补充 {extra} 瓶', text)
                after = self.current()[1][0]
                self.assertEqual([extra, 3], [m['potions'] for m in after['fighters']])
                self.assertEqual('explore' if night == 1 else 'battle', after['phase'])
                if night == 2:
                    self.assertEqual(3, after['battle']['night'])
                    self.assertEqual([2, 3], [m['potions'] for m in after['battle']['members']])
                self.assertIn('阶段已变化', self.send('dungeon_act', state['token'] + ' 进攻'))
                self.assertEqual(after, self.current()[1][0])

    def test_two_rescuers_cannot_target_same_member_in_one_round(self):
        self.db.execute('''INSERT INTO game_players
            (account_id,group_id,player_id,dao_name,dao_name_key)
            VALUES('bot','room@chatroom','charlie','无尘','charlie')''')
        self.db.commit()
        run = self.start(('alice', 'bob', 'charlie'))
        state, progress, _ = self.current()[1]
        state['phase'] = 'battle'
        state['battle'] = service.combat.start_battle(state['fighters'], 'M01', rng=self.rng)
        state['battle']['members'][1]['hp'] = 0
        state['actions'] = {}
        self.db.execute('BEGIN IMMEDIATE')
        repository.save_state(self.db, run['run_id'], state, progress, self.now + 90)
        self.db.commit()
        self.assertIn('等待', self.send('dungeon_act', state['token'] + ' 救援 2'))
        result = self.send('dungeon_act', state['token'] + ' 救援 2', 'charlie')
        self.assertIn('已有队友', result)
        self.assertEqual({'alice'}, set(self.current()[1][0]['actions']))

    def test_event_cost_is_paid_only_when_accepting_and_active_exit_settles(self):
        run = self.start()
        state, progress, _ = self.current()[1]
        state['choices'] = ['N06', 'N01', 'N02']
        hp = state['fighters'][0]['hp']
        self.db.execute('BEGIN IMMEDIATE')
        repository.save_state(self.db, run['run_id'], state, progress)
        self.db.commit()
        self.send('dungeon_choose', state['token'] + ' 1')
        loot, _, _ = self.current()[1]
        self.assertEqual(hp, loot['fighters'][0]['hp'])
        self.assertEqual('SKIP', loot['loot_choices']['alice'][2])
        self.send('dungeon_loot', loot['token'] + ' 3')
        self.assertEqual(hp, self.current()[1][0]['fighters'][0]['hp'])
        state, progress, _ = self.current()[1]
        state['highest_night'] = 1
        self.db.execute('BEGIN IMMEDIATE')
        repository.save_state(self.db, run['run_id'], state, progress)
        self.db.commit()
        self.assertIn('队长主动退出', self.send('dungeon_leave'))
        self.assertIsNone(self.current()[0])
        old = self.db.execute("SELECT cultivation FROM game_players WHERE player_id='alice'").fetchone()[0]
        self.assertEqual(10, old)
        self.send('dungeon_leave')
        self.assertEqual(old, self.db.execute("SELECT cultivation FROM game_players WHERE player_id='alice'").fetchone()[0])

    def test_daytime_auto_battle_receives_current_day(self):
        run = self.start()
        state, progress, _ = self.current()[1]
        state['day'] = 2
        state['choices'] = ['N01', 'N02', 'N04']
        self.db.execute('BEGIN IMMEDIATE')
        repository.save_state(self.db, run['run_id'], state, progress)
        self.db.commit()
        result = {'members': state['fighters'], 'outcome': 'victory'}
        with patch.object(service.combat, 'auto_battle', return_value=(result, ['胜利'])) as automatic:
            self.send('dungeon_choose', state['token'] + ' 1')
        self.assertEqual(2, automatic.call_args.kwargs['day'])

    def test_explore_options_always_offer_combat_without_forcing_elite(self):
        kinds_seen = set()
        for seed in range(30):
            state = {'phase': 'explore', 'version': 0, 'code': 'TEST'}
            service._explore_options(state, random.Random(seed))
            choices = state['choices']
            self.assertEqual(3, len(set(choices)))
            self.assertTrue({'N01', 'N05'} & set(choices))
            kinds_seen.add('N05' in choices)
        self.assertEqual({False, True}, kinds_seen)

    def test_experience_is_awarded_only_after_actual_node_result(self):
        run = self.start()
        state, progress, _ = self.current()[1]
        state['choices'] = ['N01', 'N04', 'N02']
        self.db.execute('BEGIN IMMEDIATE')
        repository.save_state(self.db, run['run_id'], state, progress)
        self.db.commit()
        initial_hp = state['fighters'][0]['hp']

        def win(members, enemy_id, **kwargs):
            self.assertEqual(0, members[0]['experience'])
            self.assertEqual(1, members[0]['level'])
            return {'members': members, 'outcome': 'victory'}, ['胜利']

        with patch.object(service.combat, 'auto_battle', side_effect=win):
            self.send('dungeon_choose', state['token'] + ' 1')
        loot = self.current()[1][0]
        self.assertEqual((100, 0, 3), (loot['experience'], loot['completed_nodes'],
                                        loot['fighters'][0]['level']))
        self.assertAlmostEqual(initial_hp / state['fighters'][0]['max_hp'],
                               loot['fighters'][0]['hp'] / loot['fighters'][0]['max_hp'])
        self.send('dungeon_loot', loot['token'] + ' 1')
        current = self.current()[1][0]
        self.assertEqual((100, 1), (current['experience'], current['completed_nodes']))
        self.assertIn('阶段已变化', self.send('dungeon_choose', state['token'] + ' 1'))

    def test_failed_daytime_battle_does_not_award_experience(self):
        run = self.start()
        state, progress, _ = self.current()[1]
        state['choices'] = ['N05', 'N04', 'N02']
        self.db.execute('BEGIN IMMEDIATE')
        repository.save_state(self.db, run['run_id'], state, progress)
        self.db.commit()
        outcome = {'members': state['fighters'], 'outcome': 'defeat'}
        with patch.object(service.combat, 'auto_battle', return_value=(outcome, ['失败'])), \
                patch.object(service, '_award_experience') as award:
            self.send('dungeon_choose', state['token'] + ' 1')
        award.assert_not_called()
        self.assertIsNone(self.current()[0])
        self.assertEqual('finished', self.db.execute(
            'SELECT state FROM dungeon_runs WHERE run_id=?', (run['run_id'],)).fetchone()[0])

    def test_elite_victory_awards_250_experience_once(self):
        run = self.start()
        state, progress, _ = self.current()[1]
        state['choices'] = ['N05', 'N04', 'N02']
        self.db.execute('BEGIN IMMEDIATE')
        repository.save_state(self.db, run['run_id'], state, progress)
        self.db.commit()
        outcome = {'members': state['fighters'], 'outcome': 'victory'}
        with patch.object(service.combat, 'auto_battle', return_value=(outcome, ['胜利'])):
            self.send('dungeon_choose', state['token'] + ' 1')
        loot = self.current()[1][0]
        self.assertEqual((250, 4), (loot['experience'], loot['fighters'][0]['level']))
        self.send('dungeon_loot', loot['token'] + ' 1')
        self.assertEqual(250, self.current()[1][0]['experience'])
        self.assertIn('阶段已变化', self.send('dungeon_choose', state['token'] + ' 1'))
        self.assertEqual(250, self.current()[1][0]['experience'])

    def test_noncombat_experience_waits_until_all_loot_taken(self):
        run = self.start(('alice', 'bob'))
        state, progress, _ = self.current()[1]
        state['choices'] = ['N04', 'N01', 'N02']
        self.db.execute('BEGIN IMMEDIATE')
        repository.save_state(self.db, run['run_id'], state, progress)
        self.db.commit()
        self.send('dungeon_choose', state['token'] + ' 1')
        loot = self.current()[1][0]
        self.assertEqual((0, 0), (loot['experience'], loot['completed_nodes']))
        self.send('dungeon_loot', loot['token'] + ' 3')
        pending = self.current()[1][0]
        self.assertEqual((0, 0), (pending['experience'], pending['completed_nodes']))
        self.send('dungeon_loot', loot['token'] + ' 3', player='bob')
        current = self.current()[1][0]
        self.assertEqual((20, 1), (current['experience'], current['completed_nodes']))
        self.assertEqual([20, 20], [fighter['experience'] for fighter in current['fighters']])

    def test_second_night_victory_starts_boss_without_third_day(self):
        run = self.start()
        state, progress, _ = self.current()[1]
        state['day'], state['night'], state['highest_night'] = 2, 2, 1
        state['battle'] = {'night': 2, 'members': state['fighters']}
        self.db.execute('BEGIN IMMEDIATE')
        text = service._after_victory(self.context(), run, state, self.rng)
        self.db.commit()
        current = self.current()[1][0]
        self.assertIn('第 2 夜已通过', text)
        self.assertEqual((2, 3, 'battle', 2), (current['day'], current['night'],
                                               current['phase'], current['highest_night']))
        self.assertEqual((3, state['boss_id']), (current['battle']['night'],
                                                current['battle']['enemy']['id']))

    def test_sixth_day_node_starts_matching_night(self):
        run = self.start()
        state, _, _ = self.current()[1]
        state['node'] = 5
        self.db.execute('BEGIN IMMEDIATE')
        service._after_node(self.context(), run, state, self.rng)
        self.db.commit()
        current = self.current()[1][0]
        self.assertEqual((6, 1, 'explore'), (current['node'], current['completed_nodes'],
                                              current['phase']))
        self.db.execute('BEGIN IMMEDIATE')
        service._after_node(self.context(), run, current, self.rng)
        self.db.commit()
        battle = self.current()[1][0]
        self.assertEqual((1, 2, 'battle'), (battle['battle']['night'],
                                            battle['completed_nodes'], battle['phase']))
        self.assertEqual(1, battle['day'])

    def test_dodge_reserves_stance_only_for_accepted_action(self):
        run = self.start(('alice', 'bob'))
        state, progress, _ = self.current()[1]
        state['phase'] = 'battle'
        state['battle'] = service.combat.start_battle(state['fighters'], 'M01', night=1,
                                                       rng=self.rng)
        state['actions'] = {}
        self.db.execute('BEGIN IMMEDIATE')
        repository.save_state(self.db, run['run_id'], state, progress)
        self.db.commit()
        token = state['token']
        for argument in (token + ' 闪避', token + ' 闪避 前段 本体',
                         'OLD.1 闪避 前段', token + ' 回气 本体'):
            self.assertIn('⚠️', self.send('dungeon_act', argument))
            current = self.current()[1][0]
            self.assertEqual(3, current['battle']['members'][0]['stance'])
            self.assertEqual({}, current['actions'])
        self.assertIn('已提交', self.send('dungeon_act', token + ' 闪避 前段'))
        current = self.current()[1][0]
        self.assertEqual(2, current['battle']['members'][0]['stance'])
        self.assertEqual({'type': 'dodge', 'segment': 'front'}, current['actions']['alice'])
        self.assertIn('已提交', self.send('dungeon_act', token + ' 闪避 前段'))
        self.assertEqual(2, self.current()[1][0]['battle']['members'][0]['stance'])


if __name__ == '__main__':
    unittest.main()
