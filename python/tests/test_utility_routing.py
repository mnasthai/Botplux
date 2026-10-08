"""Exercise received mention metadata through actual plugins and the outbox."""
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest

from wechat_receiver.normalize import normalize
from wechat_receiver.plugins import load_plugins
from wechat_receiver.reply_router import ReplyRouter
from wechat_receiver.store import Store


class UtilityRoutingTests(unittest.TestCase):
    def test_real_receive_metadata_controls_calculator_and_replies_to_original_group(self):
        now = datetime(2026, 9, 18, 4, tzinfo=timezone.utc).timestamp()
        group = '123456@chatroom'
        config = SimpleNamespace(
            sender=SimpleNamespace(account_id='wxid_bot', allowed_targets=frozenset({group})),
            enabled_plugins=('help', 'calculator'), reply_ttl_seconds=60, max_message_age_seconds=120,
        )
        plugins = load_plugins(Path(__file__).resolve().parents[2] / 'plugins', config.enabled_plugins)
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / 'messages.sqlite3')
            try:
                router = ReplyRouter(store, config, plugins, started_at=now - 1)
                for seq, (text, members, expected) in enumerate((
                    ('#帮助', '', 1),
                    ('@机器人\u2005计算sqrt(9)+2^3', 'wxid_bot', 1),
                    ('@机器人\u2005计算 1+2', '', 0),
                    ('@其他成员\u2005计算 1+2', 'wxid_other', 0),
                ), 1):
                    raw = dict(kind='item', seq=seq, source='receive_batch', msg_type=1,
                               content='wxid_member:\n' + text,
                               msg_source='<msgsource>' + (f'<atuserlist>{members}</atuserlist>' if members else '') + '</msgsource>',
                               observed_unix_ms=int(now * 1000), raw_fields={'9': str(int(now))},
                               **{'from': group, 'to': 'wxid_bot'})
                    for field in ('from_read', 'to_read', 'content_read', 'source_read'):
                        raw[field] = {'status': 'ok'}
                    message = normalize(raw, session_id='s', self_id='wxid_bot', history_state='live_candidate')
                    self.assertEqual(expected, router.handle(message, 's', now=now))
                    self.assertEqual(0, router.handle(message, 's', now=now))
                rows = store.db.execute('SELECT target_id,text,command_kind,payload_json FROM outbox ORDER BY rowid').fetchall()
                self.assertEqual(2, len(rows))
                self.assertEqual(group, rows[0]['target_id'])
                self.assertEqual('image', rows[0]['command_kind'])
                self.assertIn('.png', rows[0]['payload_json'])
                self.assertEqual((group, '结果：11'), (rows[1]['target_id'], rows[1]['text']))
                self.assertEqual(0, store.db.execute('SELECT count(*) FROM send_attempts').fetchone()[0])
            finally:
                store.close()


if __name__ == '__main__':
    unittest.main()
