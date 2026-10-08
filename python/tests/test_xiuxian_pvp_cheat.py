"""Focused checks for realm HP and administrator-only PvP GM modes."""

import json
import random
import sqlite3
import unittest
from types import SimpleNamespace

from wechat_receiver.games.commands import Command, parse_command
from wechat_receiver.games.config import GameConfig
from wechat_receiver.games.pvp import (
    apply_cheat_mode,
    calculate_fighter_stats,
    roll_attack,
    simulate_duel_round,
)
from wechat_receiver.games.schema import initialize_game_schema
from wechat_receiver.games.service import handle_command
from wechat_receiver.models import Message


def message(content, sender):
    return Message(
        session_id='s', event_key=f's:{sender}', seq=1, call_id=1,
        source='receive_batch', event_kind='item', observed_at_ms=1,
        message_type=1, message_kind='text', app_message_type=None,
        content=content, raw_content=content, conversation_id='room@chatroom',
        sender_id=sender, direction='incoming', message_time_candidate=None,
        message_id_candidate=None, mentioned_ids=(), mention_state='none',
        history_state='live_candidate',
    )


class PvpCheatTests(unittest.TestCase):
    def test_realm_hp_grows_even_when_breakthrough_consumes_cultivation(self):
        bases = [calculate_fighter_stats({'realm': realm, 'cultivation': 0}, [])['hp']
                 for realm in ('qi', 'foundation', 'core', 'nascent')]
        self.assertEqual(bases, [100, 200, 400, 730])
        for old_realm, threshold, new_realm in (
            ('qi', 100, 'foundation'),
            ('foundation', 250, 'core'),
            ('core', 500, 'nascent'),
        ):
            before = calculate_fighter_stats({'realm': old_realm, 'cultivation': threshold}, [])
            after = calculate_fighter_stats({'realm': new_realm, 'cultivation': 0}, [])
            self.assertGreater(after['hp'], before['hp'])
            self.assertEqual(after['max_hp'], after['hp'])

    def test_cheat_modes_change_combat_and_do_not_mutate_source(self):
        base = calculate_fighter_stats({'dao_name': '修士', 'realm': 'qi', 'cultivation': 0}, [])
        lucky = apply_cheat_mode(base, 'lucky', 19)
        self.assertEqual(roll_attack(lucky, base, random.Random(1))[1], 19)
        self.assertNotIn('fixed_dice', base)
        self.assertEqual(apply_cheat_mode(base, 'lucky', None)['fixed_dice'], 20)
        self.assertGreater(apply_cheat_mode(base, 'one_hit')['attack'], 900000)
        for mode in ('god', 'immortal'):
            protected = apply_cheat_mode(base, mode)
            attacker = apply_cheat_mode(base, 'one_hit')
            simulate_duel_round(attacker, protected, 1, random.Random(2))
            self.assertGreaterEqual(protected['hp'], 1)
            self.assertTrue(protected['gold_shield'])
        with self.assertRaises(ValueError):
            apply_cheat_mode(base, 'lucky', 21)

    def test_only_admin_participant_can_activate_mode(self):
        db = sqlite3.connect(':memory:')
        db.row_factory = sqlite3.Row
        try:
            initialize_game_schema(db)
            for player_id in ('admin', 'member'):
                db.execute(
                    "INSERT INTO game_players (account_id,group_id,player_id,dao_name,dao_name_key) "
                    "VALUES ('a','room@chatroom',?,?,?)",
                    (player_id, player_id, player_id),
                )
            fighters = {}
            for key, player_id in (('fighter1', 'admin'), ('fighter2', 'member')):
                fighters[key] = calculate_fighter_stats(
                    {'dao_name': player_id, 'realm': 'qi', 'cultivation': 0}, []
                ) | {'player_id': player_id}
            db.execute(
                "INSERT INTO game_pvp_duels "
                "(duel_id,account_id,group_id,challenger_id,challenged_id,wager,state,escrowed,"
                "current_round,round_state_json,round_deadline_at,created_at) "
                "VALUES ('VTEST','a','room@chatroom','admin','member',10,'fighting',1,1,?,?,?)",
                (json.dumps(fighters), '2026-09-28T13:01:00Z', '2026-09-28T13:00:00Z'),
            )

            def context(sender, is_admin):
                msg = message('#决斗开挂 lucky 18', sender)
                return SimpleNamespace(
                    store=db, account_id='a', conversation_id='room@chatroom',
                    user_id=sender, event_key=f'event:{sender}', message=msg,
                    now=1790600400, allowed_targets={'room@chatroom'},
                    game_config=GameConfig(), is_admin=is_admin,
                )

            command = parse_command(message('#决斗开挂 lucky 18', 'admin'))
            self.assertEqual(command.kind, 'pvp_cheat')
            denied = handle_command(command, context('member', False))
            self.assertIn('仅管理员', denied)
            original = json.loads(db.execute(
                "SELECT round_state_json FROM game_pvp_duels WHERE duel_id='VTEST'"
            ).fetchone()[0])
            self.assertNotIn('is_cheat', original['fighter2'])

            accepted = handle_command(command, context('admin', True))
            self.assertIn('已启用', accepted)
            saved = json.loads(db.execute(
                "SELECT round_state_json FROM game_pvp_duels WHERE duel_id='VTEST'"
            ).fetchone()[0])
            self.assertEqual(saved['fighter1']['fixed_dice'], 18)
            self.assertNotIn('is_cheat', saved['fighter2'])
        finally:
            db.close()

    def test_invitation_cheat_applies_before_first_round(self):
        db = sqlite3.connect(':memory:')
        db.row_factory = sqlite3.Row
        try:
            initialize_game_schema(db)
            for player_id in ('admin', 'member'):
                db.execute(
                    "INSERT INTO game_players "
                    "(account_id,group_id,player_id,dao_name,dao_name_key,cultivation,spirit_stones) "
                    "VALUES ('a','room@chatroom',?,?,?,50,500)",
                    (player_id, player_id, player_id),
                )
            db.execute(
                "INSERT INTO game_pvp_duels "
                "(duel_id,account_id,group_id,challenger_id,challenged_id,wager,state,created_at) "
                "VALUES ('VTEST','a','room@chatroom','admin','member',10,'inviting',?)",
                ('2026-09-28T13:00:00Z',),
            )

            def context(sender, content, is_admin):
                return SimpleNamespace(
                    store=db, account_id='a', conversation_id='room@chatroom',
                    user_id=sender, event_key=f'event:{sender}:{content}',
                    message=message(content, sender), now=1790600400,
                    allowed_targets={'room@chatroom'}, game_config=GameConfig(),
                    is_admin=is_admin,
                )

            cheat = parse_command(message('#决斗开挂 lucky', 'admin'))
            self.assertIn('开战时生效', handle_command(
                cheat, context('admin', '#决斗开挂 lucky', True)))
            handle_command(Command('accept_pvp_duel'),
                           context('member', '#接受决斗', False), rng=random.Random(3))
            row = db.execute(
                "SELECT state,round_state_json FROM game_pvp_duels WHERE duel_id='VTEST'"
            ).fetchone()
            self.assertEqual(row['state'], 'fighting')
            self.assertEqual(json.loads(row['round_state_json'])['fighter1']['fixed_dice'], 20)
        finally:
            db.close()


if __name__ == '__main__':
    unittest.main()
