from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

from wechat_receiver.outbox import Outbox
from wechat_receiver.send_models import SendTextCommand
from wechat_receiver.send_service import (SenderConfig, SendClient, EndpointWatcher,
                                         discover_endpoint, run_sender)
from wechat_receiver.store import Store


def startup(session='session', pid=None):
    start = {'kind': 'observer_start', 'session_id': session, 'target_version': '4.1.13.12'}
    if pid is not None:
        start['pid'] = pid
    return ''.join(json.dumps(r) + '\n' for r in (
        start,
        {'kind': 'command_pipe_ready', 'session_id': session, 'pipe': '\\\\.\\pipe\\wechatbot-' + session}))


class SendServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.log = self.root / 'observer.jsonl'
        self.log.write_text(startup(), encoding='utf-8')
        self.config = SenderConfig(self.root / 'messages.sqlite3', self.log, 'account', frozenset({'friend'}), .1, .05)

    def tearDown(self):
        self.temp.cleanup()

    def test_discovery_partial_start_and_incremental_new_session(self):
        watcher = EndpointWatcher(self.log)
        self.assertEqual(watcher.current().session_id, 'session')
        with self.log.open('a', encoding='utf-8') as f:
            f.write('{"kind":"observer_start"')
        self.assertEqual(watcher.current().session_id, 'session')
        self.assertEqual(discover_endpoint(self.log).session_id, 'session')
        with self.log.open('a', encoding='utf-8') as f:
            f.write(',"session_id":"new","target_version":"4.1.13.12"}\n')
        with self.assertRaises(RuntimeError):
            watcher.current()
        with self.log.open('a', encoding='utf-8') as f:
            f.write(json.dumps({'kind': 'command_pipe_ready', 'session_id': 'new', 'pipe': r'\\.\pipe\wechatbot-new'}) + '\n')
        self.assertEqual(watcher.current().session_id, 'new')

    def test_discovery_binds_pid_from_latest_observer_start(self):
        self.log.write_text(startup(pid=2468), encoding='utf-8')
        endpoint = discover_endpoint(self.log)
        self.assertEqual(endpoint.pid, 2468)
        self.assertEqual(EndpointWatcher(self.log).current().pid, 2468)

    def test_incremental_session_replaces_pid_and_invalid_pid_clears_it(self):
        watcher = EndpointWatcher(self.log)
        self.assertIsNone(watcher.current().pid)
        with self.log.open('a', encoding='utf-8') as f:
            f.write(startup('new', 2468))
        endpoint = watcher.current()
        self.assertEqual((endpoint.session_id, endpoint.pid), ('new', 2468))
        with self.log.open('a', encoding='utf-8') as f:
            f.write(startup('zero', 0))
        endpoint = watcher.current()
        self.assertEqual((endpoint.session_id, endpoint.pid), ('zero', None))
        with self.log.open('a', encoding='utf-8') as f:
            f.write(startup('invalid', True))
        endpoint = watcher.current()
        self.assertEqual((endpoint.session_id, endpoint.pid), ('invalid', None))
        with self.log.open('a', encoding='utf-8') as f:
            f.write(startup('missing'))
        endpoint = watcher.current()
        self.assertEqual((endpoint.session_id, endpoint.pid), ('missing', None))

    def test_worker_serial_progress_idempotency_and_restart_facts(self):
        client = SendClient(self.config)
        a = client.enqueue('friend', '中文🙂', request_id='a')
        self.assertEqual(client.enqueue('friend', '中文🙂', request_id='a')['created_at'], a['created_at'])
        client.enqueue('friend', 'second', request_id='b', origin='game')
        store = Store(self.config.database_path)
        queue = Outbox(store.db)
        now = datetime.now(timezone.utc)
        queue.enqueue(SendTextCommand('old', 'account', 'old-session', 'friend', 'stale', now, now+timedelta(minutes=1)))
        queue.enqueue(SendTextCommand('bad', 'account', 'session', 'not-allowed', 'bad', now-timedelta(seconds=1), now+timedelta(minutes=1)))
        store.close()
        stop = threading.Event()
        sent = []
        class Transport:
            def __init__(self, *args, **kwargs): pass
            def exchange(self, request):
                if request['op'] == 'hello_media':
                    return {'op':'error','protocol_version':1,'request_id':request['request_id'],
                        'error_code':'unsupported_operation','error_detail':'legacy endpoint'}
                if request['op'] == 'hello':
                    return {'op':'hello','protocol_version':1,'request_id':request['request_id'],
                        'observer_session_id':'session','target_version':'4.1.13.12','mode':'send_enabled',
                        'account_id':'account','account_verified':True,'send_text':True,'max_text_bytes':16384}
                sent.append(request['request_id'])
                return {'op':'send_result','protocol_version':1,'request_id':request['request_id'],
                    'attempt_id':request['attempt_id'],'observer_session_id':'session',
                    'status':'accepted','error_code':None,'error_detail':None}
        def report(value):
            if len(sent) == 2: stop.set()
        stats = run_sender(self.config, duration=2, stop=stop, transport_factory=Transport, on_report=report)
        self.assertEqual(sent, ['a','b'])
        self.assertEqual(stats['attempted'], 2)
        store = Store(self.config.database_path)
        try:
            queue = Outbox(store.db)
            self.assertEqual(queue.get('old')['status'], 'cancelled')
            self.assertEqual(queue.get('bad')['status'], 'rejected')
            self.assertEqual(queue.get('bad')['attempts'], [])
            self.assertEqual(queue.recover_inflight(), 0)
            self.assertEqual(queue.get('a')['status'], 'accepted')
            self.assertEqual(len(queue.get('a')['attempts']), 1)
        finally:
            store.close()

    def test_concurrent_producers_share_one_business_request(self):
        Store(self.config.database_path).close()
        barrier = threading.Barrier(2)
        endpoint = discover_endpoint(self.log)
        def discover(_):
            barrier.wait(timeout=3)
            return endpoint
        with patch('wechat_receiver.send_service.discover_endpoint', side_effect=discover):
            with ThreadPoolExecutor(max_workers=2) as pool:
                futures = [pool.submit(SendClient(self.config).enqueue, 'friend', 'one', request_id='same') for _ in range(2)]
                rows = [future.result(timeout=5) for future in futures]
        self.assertEqual(rows[0]['command_json'], rows[1]['command_json'])


if __name__ == '__main__':
    unittest.main()
