"""Dungeon isolation and lifecycle through the installed plugin entry point."""
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest

from wechat_receiver.games.config import GameConfig
from wechat_receiver.games.dungeon.repository import active_run
from wechat_receiver.models import Message
from wechat_receiver.plugins import load_plugins
from wechat_receiver.reply_router import ReplyRouter
from wechat_receiver.store import Store


class DungeonIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temporary.name) / 'game.sqlite3')
        self.now = 1790424000.0
        self.sequence = 0
        self.groups = [f'room{i}@chatroom' for i in range(4)]
        self.config = SimpleNamespace(
            sender=SimpleNamespace(account_id='bot', allowed_targets=frozenset(self.groups)),
            enabled_plugins=('xiuxian',), reply_ttl_seconds=60,
            max_message_age_seconds=120, game_config=GameConfig(duel_enabled=True),
        )
        plugins_path = Path(__file__).resolve().parents[2] / 'plugins'
        self.plugins = load_plugins(plugins_path, ('xiuxian',))
        self.router = ReplyRouter(self.store, self.config, self.plugins, started_at=self.now - 1)

    def tearDown(self):
        self.store.close()
        self.temporary.cleanup()

    def send(self, text, player='alice', group=None, target=None):
        self.sequence += 1
        message = Message(
            session_id='s', event_key=f's:{self.sequence}', seq=self.sequence,
            call_id=self.sequence, source='receive_batch', event_kind='item',
            observed_at_ms=int(self.now * 1000), message_type=1, message_kind='text',
            app_message_type=None, content=text, raw_content=text,
            conversation_id=group or self.groups[0], sender_id=player,
            direction='incoming', message_time_candidate=int(self.now),
            message_id_candidate=str(10000 + self.sequence),
            mentioned_ids=(target,) if target else (),
            mention_state='explicit_other' if target else 'none', history_state='live_candidate',
        )
        count = self.router.handle(message, 's', now=self.now)
        failed = self.store.db.execute(
            "SELECT error FROM plugin_runs WHERE event_key=? AND status='failed'",
            (message.event_key,)).fetchone()
        self.assertIsNone(failed, failed['error'] if failed else '')
        return count

    def register(self, player='alice', name='青玄', group=None):
        self.send('#修仙 ' + name, player, group)
        self.send('#修炼', player, group)

    def run_for(self, player='alice', group=None):
        return active_run(self.store.db, 'bot', group or self.groups[0], player)

    def test_dungeon_blocks_both_pvp_invitations_in_both_directions(self):
        self.register()
        self.register('bob', '白墨')
        self.send('#副本创建')
        for player, target, name in (('alice', 'bob', '白墨'), ('bob', 'alice', '青玄')):
            self.send(f'#决斗 @{name}\u2005 10', player, target=target)
            self.send(f'#斗法 @{name}\u2005', player, target=target)
        self.assertEqual(0, self.store.db.execute('SELECT COUNT(*) FROM game_pvp_duels').fetchone()[0])
        self.assertEqual(0, self.store.db.execute('SELECT COUNT(*) FROM game_duels').fetchone()[0])
        self.assertIsNotNone(self.run_for())
        self.send('#副本退出')
        self.send('#决斗 @白墨\u2005 10', target='bob')
        self.assertEqual(1, self.store.db.execute('SELECT COUNT(*) FROM game_pvp_duels').fetchone()[0])
        self.send('#副本创建', 'bob')
        self.assertIsNone(self.run_for('bob'))

    def test_lifecycle_batches_four_expirations_without_losing_notices(self):
        for group in self.groups:
            self.register(group=group)
            self.send('#副本创建', group=group)
            self.send('#副本准备', group=group)
            self.send('#副本出发', group=group)
            self.assertEqual('active', self.run_for(group=group)['state'])
        before = self.store.db.execute('SELECT COUNT(*) FROM outbox').fetchone()[0]
        self.now += 601
        self.assertLessEqual(self.router.tick('s', now=self.now), 3)
        self.assertLessEqual(self.router.tick('s', now=self.now + 1), 3)
        self.assertEqual(before + 4, self.store.db.execute('SELECT COUNT(*) FROM outbox').fetchone()[0])
        for group in self.groups:
            self.assertIsNone(self.run_for(group=group))
        self.assertEqual(0, self.router.tick('s', now=self.now + 2))

    def test_new_router_ends_run_without_restoring_progress(self):
        self.register()
        self.send('#副本创建')
        self.send('#副本准备')
        self.send('#副本出发')
        run_id = self.run_for()['run_id']
        self.router = ReplyRouter(self.store, self.config, self.plugins, started_at=self.now)
        self.router.start(None, now=self.now)
        self.assertIsNone(self.run_for())
        row = self.store.db.execute('SELECT * FROM dungeon_runs WHERE run_id=?', (run_id,)).fetchone()
        self.assertEqual('finished', row['state'])
        self.assertEqual(1, self.router.tick('s', now=self.now + 1))
        self.assertEqual(0, self.router.tick('s', now=self.now + 2))


if __name__ == '__main__':
    unittest.main()
