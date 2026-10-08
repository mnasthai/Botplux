from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3
import tempfile
import time
import unittest
from unittest.mock import patch

from wechat_receiver.bot_service import BotConfig, ReplyRouter, load_bot_config, run_bot
from wechat_receiver.config import Config
from wechat_receiver.normalize import normalize
from wechat_receiver.plugins import LoadedPlugin
from wechat_receiver.send_service import SenderConfig, run_sender
from wechat_receiver.send_models import SendTextCommand
from wechat_receiver.service import Receiver
from wechat_receiver.store import Store


class BotTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.log = self.root / 'observer.jsonl'
        startup = [dict(kind='observer_start', session_id='session', target_version='4.1.13.12'),
                   dict(kind='command_pipe_ready', session_id='session', pipe=r'\\.\pipe\wechatbot-session')]
        self.log.write_text(''.join(json.dumps(r) + '\n' for r in startup), encoding='utf-8')
        self.config = BotConfig(SenderConfig(self.root / 'messages.sqlite3', self.log,
            'self', frozenset({'friend'}), interval=.1, poll_interval=.05), self.root, ('affection',))
        self.store = Store(self.config.sender.database_path)
        self.receiver = Receiver(Config(self.log, self.config.sender.database_path, 'self'), self.store)
        self.now = time.time()
        self.calls = []
        def reply(message):
            self.calls.append(message.event_key)
            return '收到你的喜欢啦🙂' if message.content.strip() == '我喜欢你' else None
        self.plugin = LoadedPlugin('affection', reply)

    def tearDown(self):
        self.receiver.close()
        self.store.close()
        self.temp.cleanup()

    def record(self, seq=1, **updates):
        value = dict(kind='item', source='receive_batch', session_id='session', seq=seq,
            msg_type=1, observed_unix_ms=int(self.now * 1000), content='我喜欢你',
            to='self', from_read={'status':'ok'}, to_read={'status':'ok'},
            content_read={'status':'ok'}, raw_fields={'9':str(int(self.now))})
        value['from'] = 'friend'
        value.update(updates)
        return value

    def append(self, *records):
        with self.log.open('a', encoding='utf-8') as stream:
            for record in records:
                stream.write(json.dumps(record, ensure_ascii=False) + '\n')

    def test_live_trigger_ignores_backlog_and_duplicate_event(self):
        router = ReplyRouter(self.store, self.config, [self.plugin])
        self.append(self.record(1))
        self.receiver.poll()  # First open classifies existing file contents as backlog.
        self.assertEqual(router.poll('session'), 0)
        self.append(self.record(2), self.record(2), self.record(3, content='hello'))
        self.receiver.poll()
        self.assertEqual(router.poll('session'), 1)
        self.assertEqual(router.poll('session'), 0)
        self.assertEqual(self.calls, ['session:2', 'session:3'])
        row = self.store.db.execute('SELECT * FROM outbox').fetchone()
        self.assertEqual((row['text'], row['target_id'], row['source_event_key']),
                         ('收到你的喜欢啦🙂', 'friend', 'session:2'))

    def test_ineligible_messages_never_reach_plugin(self):
        router = ReplyRouter(self.store, self.config, [self.plugin], started_at=self.now)
        message = normalize(self.record(), session_id='session', self_id='self')
        self.assertTrue(router.eligible(message, 'session', self.now))
        cases = [dict(direction='self_sync'), dict(direction='outbound_request'),
            dict(conversation_id='stranger'), dict(conversation_id='room@chatroom'),
            dict(sender_id='self'), dict(history_state='backlog'), dict(session_id='old'),
            dict(content=None), dict(message_kind='image'), dict(event_key=None),
            dict(source='unknown'), dict(message_time_candidate=int(self.now)-200),
            dict(message_time_candidate=None), dict(observed_at_ms=int((self.now-200)*1000))]
        for changes in cases:
            with self.subTest(changes=changes):
                self.assertFalse(router.eligible(replace(message, **changes), 'session', self.now))

    def test_game_config_path_is_relative_and_optional(self):
        (self.root / 'sender.toml').write_text(
            "account_id='self'\nallowed_targets=['friend']\n"
            "database_path='messages.sqlite3'\nlog_path='observer.jsonl'\n", encoding='utf-8')
        path = self.root / 'bot.toml'
        base = "sender_config='sender.toml'\nplugin_directory='.'\nenabled_plugins=['affection']\n"
        path.write_text(base, encoding='utf-8')
        self.assertEqual(50, load_bot_config(path).game_config.cultivation_reward)
        (self.root / 'rules.toml').write_text('[game]\ncultivation_reward=75\n', encoding='utf-8')
        path.write_text(base + "game_config='rules.toml'\n", encoding='utf-8')
        self.assertEqual(75, load_bot_config(path).game_config.cultivation_reward)
        path.write_text(base + 'game_config=false\n', encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'game_config'):
            load_bot_config(path)

    def test_plugin_failure_does_not_block_other_plugin(self):
        def broken(_):
            raise ValueError('broken example')
        config = replace(self.config, enabled_plugins=('broken', 'affection'))
        router = ReplyRouter(self.store, config, [LoadedPlugin('broken', broken), self.plugin])
        self.receiver.poll()
        self.append(self.record())
        self.receiver.poll()
        with self.assertLogs(level='ERROR'):
            self.assertEqual(router.poll('session'), 1)
        states = dict(self.store.db.execute('SELECT plugin,status FROM plugin_runs').fetchall())
        self.assertEqual(states, {'broken':'failed', 'affection':'queued'})

    def test_partial_multi_reply_failure_rolls_back_marker_and_all_commands(self):
        plugin = LoadedPlugin('affection', lambda _: ['first', 'second'])
        router = ReplyRouter(self.store, self.config, [plugin])
        message = normalize(self.record(), session_id='session', self_id='self')
        real_enqueue = router.outbox.enqueue_in_transaction
        def crash(command):
            real_enqueue(command)
            if command.text == 'second':
                raise RuntimeError('simulated crash before commit')
        with patch.object(router.outbox, 'enqueue_in_transaction', side_effect=crash):
            with self.assertRaisesRegex(RuntimeError, 'simulated crash'):
                router.handle(message, 'session')
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM outbox').fetchone()[0], 0)
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM plugin_runs').fetchone()[0], 0)
        self.assertEqual(router.handle(message, 'session'), 2)
        restarted = ReplyRouter(self.store, self.config, [plugin])
        self.assertEqual(restarted.handle(message, 'session'), 0)
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM outbox').fetchone()[0], 2)
        self.assertEqual(self.store.db.execute('SELECT status FROM plugin_runs').fetchone()[0], 'queued')
        self.assertEqual(self.store.db.execute('SELECT commands_json FROM plugin_runs').fetchone()[0], '[]')

    def insert_legacy_ready(self):
        now = datetime.now(timezone.utc)
        command = SendTextCommand('old-reply', 'self', 'session', 'friend', 'old reply', now,
                                  now + timedelta(seconds=60), 'session:1')
        with self.store.db:
            self.store.db.execute('''INSERT INTO plugin_runs
                (account_id,event_key,plugin,session_id,status,commands_json) VALUES(?,?,?,?,?,?)''',
                ('self','session:1','affection','session','ready',json.dumps([command.to_dict()])))
        return command

    def test_legacy_ready_recovery_does_not_repeat_plugin_or_command(self):
        router = ReplyRouter(self.store, self.config, [self.plugin])
        command = self.insert_legacy_ready()
        router.outbox.enqueue(command)  # Old process crashed after its first enqueue.
        self.assertEqual(router.recover('session'), 1)
        self.assertEqual(router.recover('session'), 0)
        self.assertEqual(self.calls, [])
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM outbox').fetchone()[0], 1)
        self.assertEqual(self.store.db.execute('SELECT commands_json FROM plugin_runs').fetchone()[0], '[]')

    def test_pending_reply_is_not_rebound_to_new_session(self):
        router = ReplyRouter(self.store, self.config, [self.plugin])
        self.insert_legacy_ready()
        self.assertEqual(router.poll('different-session'), 0)
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM outbox').fetchone()[0], 0)
        self.assertEqual(self.store.db.execute('SELECT status FROM plugin_runs').fetchone()[0], 'cancelled')

    def test_corrupt_legacy_ready_is_isolated_and_identity_mismatch_rejected(self):
        router = ReplyRouter(self.store, self.config, [self.plugin])
        command = self.insert_legacy_ready()
        bad = command.to_dict() | {'expected_account_id':'other', 'request_id':'bad-command'}
        with self.store.db:
            self.store.db.execute('''INSERT INTO plugin_runs
                (account_id,event_key,plugin,session_id,status,commands_json) VALUES(?,?,?,?,?,?)''',
                ('self','session:2','affection','session','ready','{bad json'))
            self.store.db.execute('''INSERT INTO plugin_runs
                (account_id,event_key,plugin,session_id,status,commands_json) VALUES(?,?,?,?,?,?)''',
                ('self','session:3','affection','session','ready',json.dumps([bad])))
        with self.assertLogs(level='WARNING'):
            self.assertEqual(router.recover('session'), 1)
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM outbox').fetchone()[0], 1)
        states = dict(self.store.db.execute('SELECT event_key,status FROM plugin_runs'))
        self.assertEqual(states, {'session:1':'queued','session:2':'failed','session:3':'failed'})

    def test_bot_integrates_receiver_plugin_and_sender_using_fake_transport(self):
        (self.root / 'affection.py').write_text(
            "NAME='affection'\ndef on_message(message):\n    return '收到你的喜欢啦🙂'\n", encoding='utf-8')
        sent = []
        class Transport:
            def __init__(self, *args, **kwargs): pass
            def exchange(self, request):
                if request['op'] == 'hello_media':
                    return dict(op='error', protocol_version=1, request_id=request['request_id'],
                                error_code='unsupported_operation', error_detail='legacy endpoint')
                if request['op'] == 'hello':
                    return dict(op='hello', protocol_version=1, request_id=request['request_id'],
                        observer_session_id='session', target_version='4.1.13.12', mode='send_enabled',
                        account_id='self', account_verified=True, send_text=True, max_text_bytes=16384)
                sent.append(request)
                return dict(op='send_result', protocol_version=1, request_id=request['request_id'],
                    attempt_id=request['attempt_id'], observer_session_id='session',
                    status='accepted', error_code=None, error_detail=None)
        original_poll = Receiver.poll_batch
        first = True
        def receive(receiver):
            nonlocal first
            count = original_poll(receiver)
            if first:
                first = False
                self.append(self.record())
            return count
        def worker(config, **kwargs):
            kwargs['transport_factory'] = Transport
            return run_sender(config, **kwargs)
        with patch('wechat_receiver.backend.run_sender', side_effect=worker), patch.object(Receiver, 'poll_batch', receive), \
                patch.object(ReplyRouter, 'poll', side_effect=AssertionError('main path must not query messages again')):
            result = run_bot(self.config, duration=.6)
        self.assertEqual(result['queued_replies'], 1)
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0]['text'], '收到你的喜欢啦🙂')
        self.assertEqual(self.store.db.execute('SELECT status FROM outbox').fetchone()[0], 'accepted')

    def test_bot_runs_start_before_commands_and_poll_on_existing_loop(self):
        from types import SimpleNamespace

        self.store.db.execute('CREATE TABLE hook_order (label TEXT)')
        (self.root / 'stateful.py').write_text(
            "NAME='stateful'\n"
            "def parse_command(message): return 'go'\n"
            "def handle_command(command, context):\n"
            "    context.store.execute(\"INSERT INTO hook_order VALUES('command')\")\n"
            "    return 'done'\n"
            "def on_start(context):\n"
            "    assert context.connection_id is None\n"
            "    context.store.execute(\"INSERT INTO hook_order VALUES('start')\")\n"
            "def on_before_messages(context):\n"
            "    assert context.connection_id == 'session'\n"
            "    context.store.execute(\"INSERT INTO hook_order VALUES('before')\")\n"
            "def on_poll(context):\n"
            "    assert context.connection_id == 'session'\n"
            "    context.store.execute(\"INSERT INTO hook_order VALUES('poll')\")\n", encoding='utf-8')
        store = self.store
        message = normalize(self.record(), session_id='session', self_id='self')

        class Backend:
            connection_id = None

            def __init__(self, _config): self.store = store
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def subscribe(self, handle): self.handle = handle
            def run(self, *, duration, on_poll, on_before_dispatch):
                self.connection_id = 'session'
                on_before_dispatch()
                self.handle(SimpleNamespace(capture=message))
                on_poll()
                return {'received_records': 1}

        with patch('wechat_receiver.bot_service.WeChatBackend', Backend):
            result = run_bot(replace(self.config, enabled_plugins=('stateful',)), duration=1)
        self.assertEqual(1, result['queued_replies'])
        self.assertEqual(['start', 'before', 'command', 'poll'],
                         [r[0] for r in self.store.db.execute('SELECT label FROM hook_order')])

    def test_legacy_ai_schema_upgrades_before_restricted_hooks_and_survives_restart(self):
        from wechat_receiver.ai.commands import on_start
        from wechat_receiver.ai.config import AIConfig
        from wechat_receiver.ai.schema import SCHEMA

        old_schema = SCHEMA.replace(
            'daily_limit_exempt INTEGER NOT NULL DEFAULT 0 CHECK(daily_limit_exempt IN (0,1)), ', '')
        self.store.db.executescript(old_schema)
        with self.store.db:
            self.store.db.execute("""INSERT INTO ai_jobs(
                job_id,account_id,event_key,message_key,conversation_id,user_id,session_id,
                provider,question,is_admin,cost,created_at,created_day,expires_at,state)
                VALUES('legacy','self','event','message','friend','friend','session',
                       'deepseek','preserved',0,20,1,'2026-09-19',2,'queued')""")
        starts = []

        def restricted_start(context):
            on_start(context)
            row = context.store.execute(
                "SELECT question,cost,daily_limit_exempt FROM ai_jobs WHERE job_id='legacy'").fetchone()
            self.assertEqual(('preserved', 20, 0), tuple(row))
            with self.assertRaisesRegex(sqlite3.DatabaseError, 'not authorized'):
                context.store.execute('PRAGMA user_version=99')
            with self.assertRaises(sqlite3.IntegrityError):
                context.store.execute("UPDATE ai_jobs SET daily_limit_exempt=2 WHERE job_id='legacy'")
            starts.append('ready')

        plugin = LoadedPlugin('ai', None, parse_command=lambda _message: None,
                              handle_command=lambda _command, _context: None, on_start=restricted_start)
        store = self.store

        class Backend:
            def __init__(self, _config): self.store = store
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def subscribe(self, _handle): pass
            def run(self, **_kwargs): return {'received_records': 0}

        config = replace(self.config, enabled_plugins=('ai',), ai_config=AIConfig(enabled=False))
        with patch('wechat_receiver.bot_service.WeChatBackend', Backend), \
                patch('wechat_receiver.bot_service.load_plugins', return_value=[plugin]):
            self.assertEqual(0, run_bot(config)['queued_replies'])
            self.assertEqual(0, run_bot(config)['queued_replies'])
        self.assertEqual(['ready', 'ready'], starts)
        self.assertEqual(0, self.store.db.execute('SELECT count(*) FROM outbox').fetchone()[0])


if __name__ == '__main__':
    unittest.main()
