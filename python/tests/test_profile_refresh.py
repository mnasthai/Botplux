import json
from pathlib import Path
import tempfile
import unittest

from wechat_receiver.config import Config
from wechat_receiver.member_profiles import GroupMemberProfile
from wechat_receiver.native_memory import NativeMemoryError
from wechat_receiver.service import Receiver
from wechat_receiver.store import Store


GROUP = 'room@chatroom'
SELF = 'self'


def controls():
    return [
        {'kind': 'observer_start', 'session_id': 'session', 'target_version': '4.1.13.12', 'pid': 123},
        {'kind': 'command_pipe_ready', 'session_id': 'session', 'pipe': r'\\.\pipe\wechatbot-session'},
    ]


def group_item(seq, member='member', body='body'):
    return dict(kind='item', schema_version=2, session_id='session', seq=seq, msg_type=1,
                **{'from': GROUP}, to=SELF, content=f'{member}:\n{body}',
                from_read={'status': 'ok'}, to_read={'status': 'ok'}, content_read={'status': 'ok'})


def private_item(seq, body='private'):
    return dict(kind='item', schema_version=2, session_id='session', seq=seq, msg_type=1,
                **{'from': 'friend'}, to=SELF, content=body,
                from_read={'status': 'ok'}, to_read={'status': 'ok'}, content_read={'status': 'ok'})


class ProfileRefreshTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.log = self.root / 'observer.jsonl'
        self.log.write_text(''.join(json.dumps(record) + '\n' for record in controls()), encoding='utf-8')
        self.store = Store(self.root / 'messages.sqlite3')
        self.receiver = Receiver(Config(self.log, self.root / 'messages.sqlite3', self_id=SELF), self.store)

    def tearDown(self):
        self.receiver.close()
        self.store.close()
        self.temp.cleanup()

    def append(self, *records):
        with self.log.open('a', encoding='utf-8') as stream:
            for record in records:
                stream.write(json.dumps(record) + '\n')

    def model(self, raw_event_id):
        value = self.store.db.execute('SELECT model_json FROM messages WHERE raw_event_id=?',
                                      (raw_event_id,)).fetchone()[0]
        return json.loads(value)

    @staticmethod
    def profile(nickname, *, member='member', account=SELF, group=GROUP, observed=100):
        return GroupMemberProfile(account, group, member, nickname, observed)

    def test_new_group_messages_refresh_each_batch_and_preserve_old_label(self):
        calls = []

        def fetch(pid, account, group, member):
            calls.append((pid, account, group, member))
            return self.profile('First', member=member, observed=100)

        self.receiver._profiles.fetch = fetch
        self.receiver.poll_batch()  # Commit controls and establish the live tail.
        self.append(group_item(1, body='first body'))
        first = self.receiver.poll_batch()
        message = first.events[-1].message
        self.assertEqual('First', message.sender_group_nickname)
        self.assertFalse(message.sender_group_nickname_backfilled)
        self.assertEqual('First', self.model(first.events[-1].raw_event_id)['sender_group_nickname'])

        self.receiver._profiles.fetch = lambda pid, account, group, member: self.profile(
            'Second', member=member, observed=200)
        self.append(group_item(2, body='second body'))
        second = self.receiver.poll_batch()
        self.assertEqual('Second', second.events[-1].message.sender_group_nickname)
        self.assertEqual('First', self.model(first.events[-1].raw_event_id)['sender_group_nickname'])
        self.assertEqual((123, SELF, GROUP, 'member'), calls[0])

    def test_unknown_errors_scope_rejection_and_private_messages_do_not_create_nickname(self):
        calls = []

        def fetch(pid, account, group, member):
            calls.append(member)
            if member == 'unknown':
                return None
            if member == 'unreadable':
                raise NativeMemoryError('unreadable cache')
            return self.profile('wrong scope', member=member, account='other')

        self.receiver._profiles.fetch = fetch
        self.receiver.poll_batch()
        self.append(group_item(1, 'unknown'), group_item(2, 'unreadable'), group_item(3, 'wrongscope'))
        batch = self.receiver.poll_batch()
        self.assertEqual(['unknown', 'unreadable', 'wrongscope'], calls)
        self.assertTrue(all(event.message.sender_group_nickname is None for event in batch.events if event.message))
        self.assertTrue(all(self.model(event.raw_event_id)['sender_group_nickname'] is None
                            for event in batch.events if event.message))

        self.append(private_item(4))
        private = self.receiver.poll_batch()
        self.assertEqual('friend', private.events[-1].message.sender_id)
        self.assertEqual(['unknown', 'unreadable', 'wrongscope'], calls)

    def test_backfill_marks_only_missing_history_preserves_body_and_survives_renormalize(self):
        self.receiver._profiles.fetch = lambda *args: None
        self.append(group_item(1, body='historical body'))
        initial = self.receiver.poll_batch()
        raw_event_id = initial.events[-1].raw_event_id
        self.assertEqual('backlog', initial.events[-1].message.history_state)
        self.assertIsNone(self.model(raw_event_id)['sender_group_nickname'])

        self.receiver._profiles.fetch = lambda pid, account, group, member: self.profile(
            'Backfilled', member=member, observed=300)
        report = self.receiver._profiles.backfill()
        self.assertEqual({'member_pairs': 1, 'resolved_pairs': 1, 'unknown_pairs': 0, 'updated_messages': 1}, report)
        enriched = self.model(raw_event_id)
        self.assertEqual(('historical body', 'Backfilled', True),
                         (enriched['content'], enriched['sender_group_nickname'],
                          enriched['sender_group_nickname_backfilled']))
        self.assertEqual(0, self.receiver._profiles.backfill()['updated_messages'])

        self.assertEqual(1, self.store.renormalize(SELF))
        retained = self.model(raw_event_id)
        self.assertEqual(('historical body', 'Backfilled', True),
                         (retained['content'], retained['sender_group_nickname'],
                          retained['sender_group_nickname_backfilled']))


if __name__ == '__main__':
    unittest.main()
