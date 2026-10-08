"""Visible group mentions for durable xiuxian duel prompts."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest

from wechat_receiver.games import duels
from wechat_receiver.games.commands import parse_command
from wechat_receiver.games.config import DEFAULT_GAME_CONFIG
from wechat_receiver.member_profiles import GroupMemberDirectory, GroupMemberProfile
from wechat_receiver.models import Message
from wechat_receiver.outbox import Outbox
from wechat_receiver.plugins import LoadedPlugin
from wechat_receiver.reply_router import ReplyRouter
from wechat_receiver.store import Store


class _FixedRng:
    def randint(self, start, end):
        self.assertEqual((1, 6), (start, end))
        return 6

    def choice(self, items):
        return tuple(items)[0]

    def assertEqual(self, expected, actual):
        if expected != actual:
            raise AssertionError((expected, actual))


class XiuxianDuelMentionTests(unittest.TestCase):
    account = 'wxid_bot'
    group = '22913213991@chatroom'
    base = datetime(2026, 9, 18, 12, tzinfo=timezone.utc)

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temporary.name) / 'messages.sqlite3')
        self.now = self.base.timestamp()
        self.session = 'duel-mentions'
        self.sequence = 0
        config = SimpleNamespace(
            sender=SimpleNamespace(account_id=self.account, allowed_targets=frozenset({self.group})),
            enabled_plugins=('xiuxian',), reply_ttl_seconds=60, max_message_age_seconds=120,
            game_config=replace(DEFAULT_GAME_CONFIG, duel_enabled=True),
        )
        plugin = LoadedPlugin(
            'xiuxian', None, parse_command=parse_command,
            handle_command=lambda command, context: duels.handle_command(command, context, rng=_FixedRng()),
            on_start=duels.on_start, on_before_messages=duels.on_before_messages, on_poll=duels.on_poll,
        )
        self.router = ReplyRouter(self.store, config, [plugin], started_at=self.now - 1)
        self.router.start(self.session, now=self.now)
        self.seed_player('alice', '青玄', '阿璃')
        self.seed_player('bob', '白墨', '明远')
        self.store.db.commit()

    def tearDown(self):
        self.store.close()
        self.temporary.cleanup()

    def seed_player(self, player, dao_name, group_nickname):
        self.store.db.execute("""INSERT INTO game_players
            (account_id,group_id,player_id,dao_name,dao_name_key,spirit_stones,joined_at)
            VALUES(?,?,?,?,?,?,?)""", (self.account, self.group, player, dao_name, dao_name.casefold(), 100,
                                           self.base.isoformat()))
        self.store.db.execute("""INSERT INTO game_items
            (item_id,account_id,group_id,template_id,rarity,owner_player_id,state)
            VALUES(?,?,?,?,?,?,'held')""",
            ({'alice': 'F00000001', 'bob': 'F00000002'}[player], self.account, self.group,
             'qingfeng_jian', 'artifact', player))
        GroupMemberDirectory(self.store.db).save(GroupMemberProfile(
            self.account, self.group, player, group_nickname, int(self.now * 1000)))

    def message(self, text, player, *, target=None):
        self.sequence += 1
        return Message(
            session_id=self.session, event_key=f'{self.session}:{self.sequence}', seq=self.sequence,
            call_id=self.sequence, source='receive_batch', event_kind='item',
            observed_at_ms=int(self.now * 1000), message_type=1, message_kind='text',
            app_message_type=None, content=text, raw_content=text, conversation_id=self.group,
            sender_id=player, direction='incoming', message_time_candidate=int(self.now),
            message_id_candidate=str(90_000 + self.sequence),
            mentioned_ids=(target,) if target else (),
            mention_state='explicit_other' if target else 'none', history_state='live_candidate')

    def send(self, text, player, *, target=None):
        return self.router.handle(self.message(text, player, target=target), self.session, now=self.now)

    def duel(self):
        return self.store.db.execute('SELECT * FROM game_duels ORDER BY created_at DESC,duel_id DESC LIMIT 1').fetchone()

    def latest_prompt(self):
        row = self.store.db.execute('SELECT text,extra_json FROM outbox ORDER BY rowid DESC LIMIT 1').fetchone()
        return row['text'], json.loads(row['extra_json']).get('at_user_list')

    def accept_latest_prompt(self):
        request_id = self.duel()['prompt_request_id']
        claim = Outbox(self.store.db).claim_next(
            self.account, self.session, now=datetime.fromtimestamp(self.now, timezone.utc), request_id=request_id)
        self.assertIsNotNone(claim)
        Outbox(self.store.db).record_result(claim['request_id'], claim['attempt_id'], 'accepted',
                                            now=datetime.fromtimestamp(self.now, timezone.utc))

    def open_first_turn(self):
        self.assertEqual(1, self.send('#斗法 @明远\u2005', 'alice', target='bob'))
        text, mentioned = self.latest_prompt()
        self.assertEqual('bob', mentioned)
        self.assertEqual(1, text.count('@明远'))
        self.assertNotIn('@白墨', text)
        self.accept_latest_prompt()
        self.assertEqual(1, self.send('#接受斗法', 'bob'))
        text, mentioned = self.latest_prompt()
        self.assertEqual('alice,bob', mentioned)
        self.assertEqual(1, text.count('@阿璃'))
        self.assertEqual(1, text.count('@明远'))
        self.accept_latest_prompt()
        self.router.before_messages(self.session, now=self.now)
        for _ in range(8):
            self.now += 4
            self.router.tick(self.session, now=self.now)
        text, mentioned = self.latest_prompt()
        self.assertEqual('alice', mentioned)
        self.assertEqual(1, text.count('@阿璃'))
        self.assertNotIn('@青玄', text)
        self.accept_latest_prompt()
        self.router.before_messages(self.session, now=self.now)

    def test_each_turn_names_the_stably_mentioned_next_player(self):
        self.open_first_turn()
        self.assertEqual(1, self.send('#引雷', 'alice'))
        text, mentioned = self.latest_prompt()
        self.assertEqual('bob', mentioned)
        self.assertEqual(1, text.count('@明远'))
        self.assertNotIn('@白墨', text)
        self.accept_latest_prompt()
        self.router.before_messages(self.session, now=self.now)
        self.assertEqual(1, self.send('#引雷', 'bob'))
        text, mentioned = self.latest_prompt()
        self.assertEqual('alice', mentioned)
        self.assertEqual(1, text.count('@阿璃'))
        self.assertNotIn('@青玄', text)


if __name__ == '__main__':
    unittest.main()
