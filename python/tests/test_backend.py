import asyncio
from dataclasses import replace
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from wechat_receiver import MessageEvent, WeChatBackend
from wechat_receiver.normalize import normalize
from wechat_receiver.send_service import SenderConfig
from wechat_receiver.sender import HelloCapabilities, SenderLock


class BackendTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.log = self.root / 'events.jsonl'
        self.log.write_text(''.join(json.dumps(r)+'\n' for r in (
            dict(kind='observer_start',session_id='s',target_version='4.1.13.12'),
            dict(kind='command_pipe_ready',session_id='s',pipe=r'\\.\pipe\wechatbot-s'))), encoding='utf-8')
        self.config = SenderConfig(self.root/'messages.sqlite3', self.log, 'self', frozenset({'friend'}), poll_interval=.05)

    def tearDown(self):
        self.temp.cleanup()

    def message(self):
        return normalize(dict(kind='item',seq=3,msg_type=1,content='hi',to='self',
            **{'from':'friend'},from_read={'status':'ok'},to_read={'status':'ok'},content_read={'status':'ok'}),
            session_id='s',self_id='self')

    def test_subscribers_receive_committed_normalized_object_and_can_unsubscribe(self):
        with WeChatBackend(self.config) as backend:
            events = []
            order = []
            def record(event):
                self.assertGreater(backend.store.db.execute('SELECT count(*) FROM messages').fetchone()[0], 0)
                order.append('message')
                events.append(event)
            unsubscribe = backend.subscribe(record)
            backend.poll()
            raw = dict(kind='item',seq=3,msg_type=1,session_id='s',content='hi',to='self',
                **{'from':'friend'},from_read={'status':'ok'},to_read={'status':'ok'},content_read={'status':'ok'})
            with self.log.open('a',encoding='utf-8') as stream:
                stream.write(json.dumps(raw)+'\n')
            def before_dispatch():
                self.assertEqual(1, backend.store.db.execute('SELECT count(*) FROM messages').fetchone()[0])
                self.assertEqual('s', backend.connection_id)
                order.append('before')
            with patch('wechat_receiver.store.normalize', wraps=normalize) as normalizer:
                backend.poll(before_dispatch=before_dispatch)
                self.assertEqual(normalizer.call_count, 1)
            self.assertEqual(['before', 'message'], order)
            self.assertEqual(len(events), 1)
            event = events[0]
            self.assertEqual((event.event_id,event.connection_id,event.conversation_id,event.user_id,event.text),
                             ('s:3','s','friend','friend','hi'))
            self.assertFalse(backend.connection_state.account_verified)
            self.assertTrue(backend.capabilities.receive_group)
            self.assertTrue(backend.capabilities.send_group_text)
            unsubscribe()
            raw['seq'] = 4
            with self.log.open('a',encoding='utf-8') as stream:
                stream.write(json.dumps(raw)+'\n')
            backend.poll()
            self.assertEqual(len(events), 1)

    def test_received_media_metadata_survives_commit_and_diagnostics_are_not_messages(self):
        with WeChatBackend(self.config) as backend:
            events = []
            backend.subscribe(events.append)
            backend.poll()
            records = []
            for seq, message_type, payload in (
                (10, 3, '<msg><img length="123" cdnmidwidth="640" /></msg>'),
                (11, 34, '<msg><voicemsg voicelength="2000" voiceformat="4" /></msg>'),
            ):
                records.append(dict(kind='item', seq=seq, session_id='s', msg_type=message_type,
                                    content='member:\n' + payload, to='self', **{'from': 'room@chatroom'},
                                    from_read={'status': 'ok'}, to_read={'status': 'ok'},
                                    content_read={'status': 'ok'}))
            records.append(dict(kind='media_receive_sample', schema_version=2, seq=12, session_id='s',
                                source_event_seq=11, msg_type=34, field8={'status': 'missing'}))
            with self.log.open('a', encoding='utf-8') as stream:
                stream.writelines(json.dumps(record) + '\n' for record in records)
            backend.poll()
            self.assertEqual(['image', 'voice'], [event.kind for event in events])
            self.assertEqual(['member', 'member'], [event.user_id for event in events])
            self.assertTrue(all(event.text is None for event in events))
            self.assertEqual(123, events[0].media.image.byte_length_candidate)
            self.assertEqual(2000, events[1].media.voice.duration_ms_candidate)
            self.assertEqual('not_acquired', events[1].media.acquisition_state)
            rows = backend.store.db.execute('SELECT model_json FROM messages ORDER BY raw_event_id').fetchall()
            self.assertEqual(2, len(rows))
            self.assertEqual('voice', json.loads(rows[1][0])['message_kind'])

    def test_async_enqueue_status_and_reply_connection_binding_without_native_io(self):
        backend = WeChatBackend(self.config)
        event = MessageEvent('self', self.message())
        receipt = asyncio.run(backend.reply(event, 'hello', request_id='stable-reply'))
        self.assertEqual(receipt.status, 'queued')
        self.assertEqual(backend.get_send_status(receipt.request_id), receipt)
        self.assertEqual(asyncio.run(backend.reply(event, 'hello', request_id='stable-reply')), receipt)
        old = MessageEvent('self', replace(event.capture, session_id='old'))
        with self.assertRaises(ValueError):
            asyncio.run(backend.reply(old, 'hello', request_id='new-reply'))
        with self.assertRaises(ValueError):
            asyncio.run(backend.reply(replace(event, account_id='other'), 'hello'))
        with self.assertRaises(ValueError):
            asyncio.run(backend.send_text('room@chatroom', 'hello'))

    def test_sender_lock_failure_does_not_deliver_plugin_events(self):
        self.config.database_path.parent.mkdir(parents=True, exist_ok=True)
        with SenderLock(self.config.database_path), WeChatBackend(self.config) as backend:
            with patch.object(backend, 'poll', side_effect=AssertionError('must wait for sender lock')):
                with self.assertRaisesRegex(RuntimeError, 'Sender worker stopped'):
                    backend.run(duration=.2)
            self.assertEqual(backend.connection_state.status, 'stopped')
            self.assertFalse(backend.connection_state.send_ready)

    def test_group_connection_readiness_requires_verified_current_capability(self):
        backend = WeChatBackend(self.config)
        capability = HelloCapabilities('s', '4.1.13.12', 'send_enabled', 'self', True, True, 16384)
        backend._on_state(None, capability, None)
        self.assertTrue(backend.connection_state.send_ready)
        self.assertFalse(backend.connection_state.send_group_ready)
        backend._on_state(None, replace(capability, send_group_text=True), None)
        self.assertTrue(backend.connection_state.send_group_ready)
        for unavailable in (replace(capability, send_group_text=True, account_id='other'),
                            replace(capability, send_group_text=True, send_text=False), None):
            backend._on_state(None, unavailable, 'waiting')
            self.assertFalse(backend.connection_state.send_group_ready)

    def test_run_exposes_read_error_to_lifecycle_before_stopping(self):
        stop = threading.Event()
        issues = []

        def sender(_config, *, stop, on_started, **_kwargs):
            on_started()
            stop.wait(1)
            return {}

        with WeChatBackend(self.config) as backend:
            def lifecycle():
                issues.append(backend.runtime_issue)
                if backend.runtime_issue and backend.runtime_issue.startswith('receiver_read_error:'):
                    stop.set()

            with patch('wechat_receiver.backend.run_sender', side_effect=sender), \
                    patch.object(backend._receiver, 'poll_batch', side_effect=OSError('read failed')):
                backend.run(stop=stop, on_poll=lifecycle)
        self.assertTrue(issues[0].startswith('receiver_read_error:OSError: read failed'))
        self.assertEqual('backend_stopping', issues[-1])

    def test_unexpected_sender_exit_reaches_final_lifecycle(self):
        issues = []

        def sender(_config, *, on_started, **_kwargs):
            on_started()
            return {}

        with WeChatBackend(self.config) as backend:
            with patch('wechat_receiver.backend.run_sender', side_effect=sender):
                backend.run(duration=1, on_poll=lambda: issues.append(backend.runtime_issue))
        self.assertEqual(['sender_worker_exited'], issues)

    def test_sender_failure_during_poll_reaches_before_dispatch_and_poll_hook(self):
        polling = threading.Event()
        stop = threading.Event()
        before_issues = []
        poll_issues = []

        def sender(_config, *, on_started, **_kwargs):
            on_started()
            polling.wait(1)
            raise RuntimeError('sender failed during receive poll')

        with WeChatBackend(self.config) as backend:
            def receive(*, before_dispatch):
                polling.set()
                self.assertTrue(stop.wait(1))
                before_dispatch()
                return 0

            with patch('wechat_receiver.backend.run_sender', side_effect=sender), \
                    patch.object(backend, 'poll', side_effect=receive):
                with self.assertRaisesRegex(RuntimeError, 'Sender worker stopped'):
                    backend.run(duration=1, stop=stop,
                                on_before_dispatch=lambda: before_issues.append(backend.runtime_issue),
                                on_poll=lambda: poll_issues.append(backend.runtime_issue))
        self.assertEqual(['sender_worker_error'], before_issues)
        self.assertGreaterEqual(len(poll_issues), 1)
        self.assertEqual('sender_worker_error', poll_issues[0])

    def test_line_appended_during_handler_is_dispatched_before_poll_hook(self):
        stop = threading.Event()
        order = []
        deadline = time.time() + 1

        def sender(_config, *, stop, on_started, **_kwargs):
            on_started()
            stop.wait(1)
            return {}

        def raw(seq, observed):
            return dict(kind='item', seq=seq, msg_type=1, session_id='s', content='hi',
                        observed_unix_ms=int(observed * 1000), to='self', **{'from': 'friend'},
                        from_read={'status': 'ok'}, to_read={'status': 'ok'},
                        content_read={'status': 'ok'})

        with WeChatBackend(self.config) as backend:
            backend.poll()  # Establish the live tail after startup records.
            with self.log.open('a', encoding='utf-8') as stream:
                stream.write(json.dumps(raw(3, deadline - .2)) + '\n')

            def handle(event):
                order.append(f'handle:{event.capture.seq}')
                if event.capture.seq == 3:
                    with self.log.open('a', encoding='utf-8') as stream:
                        stream.write(json.dumps(raw(4, deadline - .1)) + '\n')

            def polled():
                order.append('poll')
                self.assertIn('handle:4', order)
                stop.set()

            backend.subscribe(handle)
            with patch('wechat_receiver.backend.run_sender', side_effect=sender):
                backend.run(duration=1, stop=stop,
                            on_before_dispatch=lambda: order.append('before'), on_poll=polled)
        self.assertEqual(['before', 'handle:3', 'before', 'handle:4', 'poll'], order[:5])


if __name__ == '__main__':
    unittest.main()
