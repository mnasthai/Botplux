"""Durable lifecycle tests for the background AI worker.

All providers here are local fakes.  The tests cover database state and the
outbox boundary without contacting DeepSeek, Codex, or WeChat.
"""

from __future__ import annotations

import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from types import SimpleNamespace

from wechat_receiver.ai.commands import day_at
from wechat_receiver.ai.config import AIConfig
from wechat_receiver.ai.codex_provider import CodexError
from wechat_receiver.ai.profiles import DEFAULT_PROFILE, PROFILES
from wechat_receiver.ai.deepseek_provider import DeepSeekError
from wechat_receiver.ai.schema import initialize_schema
from wechat_receiver.ai.worker import AIEngine, QA_INSTRUCTIONS, transaction, _log_worker_error
from wechat_receiver.games.schema import initialize_game_schema
from wechat_receiver.store import Store


class FakeProvider:
    def __init__(self, answer: str = "answer", *, configured: bool = True, error: Exception | None = None) -> None:
        self.answer, self.is_configured, self.error = answer, configured, error
        self.calls: list[tuple[str, dict]] = []

    def configured(self) -> bool:
        return self.is_configured

    def complete(self, prompt: str, **kwargs) -> str:
        self.calls.append((prompt, kwargs))
        if self.error:
            raise self.error
        return self.answer


class FakeCodex(FakeProvider):
    def __init__(self, answer: str = "answer", *, error: Exception | None = None) -> None:
        super().__init__(answer, error=error)
        self.checks = 0
        self.profile = DEFAULT_PROFILE
        self.call_profiles = []

    def select_profile(self, profile: str) -> None:
        if profile not in PROFILES:
            raise ValueError("invalid test profile")
        self.profile = profile

    def complete(self, prompt: str, **kwargs) -> str:
        self.call_profiles.append(self.profile)
        return super().complete(prompt, **kwargs)

    def check_available(self) -> None:
        self.checks += 1
        check_error = getattr(self, "check_error", None)
        if check_error:
            raise check_error


class FakeConnectionState:
    def __init__(self, *, ready: bool, account: str, connection_id: str = "session") -> None:
        self.send_ready = ready
        self.account_verified = ready
        self.account_id = account
        self.connection_id = connection_id if ready else None
        self.send_group_ready = ready


class AIWorkerTests(unittest.TestCase):
    account = "wxid_bot"
    group = "worker-test@chatroom"
    other_group = "other-worker@chatroom"
    user = "wxid_member"
    now = 1_789_740_000.0

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temporary.name) / "messages.sqlite3")
        self.clock_now = self.now
        self.config = AIConfig(
            key_file=Path(self.temporary.name) / "not-read.key",
            memory_enabled=False,
            deepseek_daily_limit=10,
            codex_daily_limit=10,
            max_answer_chars=3000,
        )
        self.bot = SimpleNamespace(
            sender=SimpleNamespace(account_id=self.account, allowed_targets=frozenset({self.group, self.other_group})),
            ai_config=self.config,
        )
        with self.store.db:
            initialize_schema(self.store.db)
            initialize_game_schema(self.store.db)
            self.store.db.execute(
                """INSERT INTO game_players(account_id,group_id,player_id,dao_name,dao_name_key,spirit_stones)
                   VALUES(?,?,?,?,?,?)""",
                (self.account, self.group, self.user, "青玄", self.user, 100),
            )
        self.deepseek = FakeProvider()
        self.codex = FakeCodex()
        self.connection = FakeConnectionState(ready=True, account=self.account)
        self.engine = AIEngine(self.store.db, self.bot, self.deepseek, self.codex,
                               lambda: self.connection, clock=lambda: self.clock_now)

    def tearDown(self) -> None:
        self.store.close()
        self.temporary.cleanup()

    def add_job(self, *, job_id: str, provider: str = "deepseek", state: str = "queued",
                group: str | None = None, question: str = "current question", cost: int = 20,
                expires_at: float | None = None, answer: str | None = None, daily_limit_exempt: bool = False) -> None:
        group = self.group if group is None else group
        with self.store.db:
            self.store.db.execute("""INSERT INTO ai_jobs(
                job_id,account_id,event_key,message_key,conversation_id,user_id,nickname,session_id,provider,
                question,is_admin,daily_limit_exempt,cost,created_at,created_day,expires_at,state,answer)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (
                job_id, self.account, "event-" + job_id, "message-" + job_id, group, self.user, "成员",
                "old-session", provider, question, 0, int(daily_limit_exempt), cost, self.clock_now, day_at(self.clock_now),
                self.clock_now + 600 if expires_at is None else expires_at, state, answer,
            ))

    def job(self, job_id: str):
        return self.store.db.execute("SELECT * FROM ai_jobs WHERE job_id=?", (job_id,)).fetchone()

    def stones(self) -> int:
        return self.store.db.execute(
            "SELECT spirit_stones FROM game_players WHERE account_id=? AND group_id=? AND player_id=?",
            (self.account, self.group, self.user),
        ).fetchone()[0]

    def outbox_count(self) -> int:
        return self.store.db.execute("SELECT count(*) FROM outbox").fetchone()[0]

    def select_profile(self, profile: str) -> None:
        with self.store.db:
            self.store.db.execute("""INSERT INTO ai_model_settings(account_id,profile,updated_at,updated_by)
                VALUES(?,?,?,?) ON CONFLICT(account_id) DO UPDATE SET profile=excluded.profile""",
                (self.account, profile, self.clock_now, 'wxid_admin'))

    def test_default_and_runtime_switch_refresh_availability_without_restart(self) -> None:
        for index, profile in enumerate(('luna', 'sol', 'luna')):
            if index:
                self.select_profile(profile)
            self.add_job(job_id=f'switch-{index}', provider='codex', cost=0)
            self.assertTrue(self.engine.run_once())
            self.assertEqual('completed', self.job(f'switch-{index}')['state'])
            self.assertEqual(index + 1, self.codex.checks)
            self.assertIn(PROFILES[profile].model, self.engine.codex_status)
            self.assertIn(PROFILES[profile].reasoning_effort, self.engine.codex_status)
        self.assertEqual(['luna', 'sol', 'luna'], self.codex.call_profiles)

    def test_recreated_worker_restores_persisted_profile(self) -> None:
        self.select_profile('sol')
        provider = FakeCodex()
        engine = AIEngine(self.store.db, self.bot, self.deepseek, provider,
                          lambda: self.connection, clock=lambda: self.clock_now)
        self.add_job(job_id='restored-profile', provider='codex', cost=0)
        self.assertTrue(engine.run_once())
        self.assertEqual(['sol'], provider.call_profiles)

    def test_switch_during_answer_only_changes_the_next_task(self) -> None:
        original_complete = self.codex.complete

        def change_during_call(prompt, **kwargs):
            answer = original_complete(prompt, **kwargs)
            self.select_profile('sol')
            self.assertEqual('luna', self.codex.profile)
            return answer

        self.codex.complete = change_during_call
        self.add_job(job_id='active-profile', provider='codex', cost=0)
        self.assertTrue(self.engine.run_once())
        self.codex.complete = original_complete
        self.add_job(job_id='next-profile', provider='codex', cost=0)
        self.assertTrue(self.engine.run_once())
        self.assertEqual(['luna', 'sol'], self.codex.call_profiles)

    def test_memory_correction_keeps_profile_when_admin_switches_mid_task(self) -> None:
        self.select_profile('sol')
        self.add_memory_task(task_id='memory-profile', kind='daily_report',
                             answer='{"topics": "not a list"}')
        original_complete = self.codex.complete

        def change_during_call(prompt, **kwargs):
            answer = original_complete(prompt, **kwargs)
            self.select_profile('luna')
            return answer

        self.codex.complete = change_during_call
        with self.assertLogs(level='WARNING'):
            self.assertTrue(self.engine.run_once())
        self.assertEqual(['sol', 'sol'], self.codex.call_profiles)
        self.codex.complete = original_complete
        self.codex.answer = 'answer'
        self.add_job(job_id='after-memory-switch', provider='codex', cost=0)
        self.assertTrue(self.engine.run_once())
        self.assertEqual(['sol', 'sol', 'luna'], self.codex.call_profiles)

    def test_new_profile_unavailable_never_uses_previous_cached_success(self) -> None:
        self.add_job(job_id='available-profile', provider='codex', cost=0)
        self.assertTrue(self.engine.run_once())
        self.select_profile('sol')
        self.codex.check_error = CodexError('test model unavailable')
        self.add_job(job_id='unavailable-profile', provider='codex', cost=0)
        self.assertTrue(self.engine.run_once())
        self.assertEqual(('failed', 'unavailable'),
                         (self.job('unavailable-profile')['state'], self.job('unavailable-profile')['error_code']))
        self.assertEqual(['luna'], self.codex.call_profiles)
        self.assertEqual(2, self.codex.checks)

    def add_memory_task(self, *, task_id: str, kind: str, answer: str,
                        memory_daily_limit: int = 20,
                        memory_group_daily_limit: int = 10) -> None:
        self.config = AIConfig(
            key_file=Path(self.temporary.name) / "not-read.key",
            memory_enabled=True,
            deepseek_daily_limit=10,
            codex_daily_limit=10,
            memory_daily_limit=memory_daily_limit,
            memory_group_daily_limit=memory_group_daily_limit,
            max_answer_chars=3000,
        )
        self.bot.ai_config = self.config
        self.engine = AIEngine(self.store.db, self.bot, self.deepseek, self.codex,
                               lambda: self.connection, clock=lambda: self.clock_now)
        self.codex.answer = answer
        with self.store.db:
            self.store.db.execute("""INSERT INTO ai_memory_tasks(
                task_id,account_id,kind,group_id,day,prompt,payload_json,priority,state,
                available_at,created_at,updated_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""", (
                task_id, self.account, kind, self.group, day_at(self.clock_now),
                'private prompt must never appear in diagnostics', '{}', 0, 'pending',
                self.clock_now, self.clock_now, self.clock_now,
            ))

    def test_success_delivers_once_and_never_reinvokes_provider(self) -> None:
        self.add_job(job_id="success")
        self.assertTrue(self.engine.run_once())
        job = self.job("success")
        self.assertEqual(("completed", "answer", "ai-answer-success"),
                         (job["state"], job["answer"], job["reply_request_id"]))
        self.assertEqual(1, len(self.deepseek.calls))
        self.assertEqual(("current question", {"instructions": QA_INSTRUCTIONS}), self.deepseek.calls[0])
        self.assertEqual(1, self.outbox_count())
        self.assertFalse(self.engine.run_once())
        self.assertEqual(1, len(self.deepseek.calls))
        self.assertEqual(1, self.outbox_count())

    def test_answer_storage_and_delivery_hide_obvious_internal_text(self) -> None:
        self.deepseek.answer = (
            "公开产品 DeepSeek 可以讨论；deepseek-flash；gpt-5.6-sol；gpt-5.6-luna；sk-secret-token-12345678；"
            "C:\\Users\\name\\secret.txt；wxid_private；room@chatroom"
        )
        self.add_job(job_id="redacted")
        with self.store.db:
            self.store.db.execute("UPDATE ai_jobs SET nickname=NULL WHERE job_id='redacted'")
        self.assertTrue(self.engine.run_once())
        job = self.job("redacted")
        queued = self.store.db.execute("SELECT text FROM outbox WHERE request_id='ai-answer-redacted'").fetchone()[0]
        for private in ("deepseek-flash", "gpt-5.6-sol", "gpt-5.6-luna", "sk-secret-token-12345678", "C:\\Users\\name\\secret.txt",
                        "wxid_private", "room@chatroom"):
            self.assertNotIn(private, job["answer"])
            self.assertNotIn(private, queued)
        self.assertIn("公开产品 DeepSeek 可以讨论", job["answer"])
        self.assertTrue(queued.startswith("【道友】\n"))

    def test_every_outbox_text_redacts_bearer_json_token_and_windows_paths(self) -> None:
        text = (
            '日报：Bearer abcdefghijklmnop；"api_key": "quoted-secret"；'
            'refresh_token=refresh-secret；C:/Users/Name/My Folder/private.txt；'
            'C:\\Users\\Name\\My Folder\\private.txt'
        )
        with transaction(self.store.db):
            self.engine._enqueue("ai-report-redacted", self.group, text, "session", self.clock_now)
        queued = self.store.db.execute("SELECT text FROM outbox WHERE request_id='ai-report-redacted'").fetchone()[0]
        for private in ("abcdefghijklmnop", "quoted-secret", "refresh-secret",
                        "C:/Users/Name/My Folder/private.txt", "C:\\Users\\Name\\My Folder\\private.txt"):
            self.assertNotIn(private, queued)
        self.assertIn("日报：", queued)

    def test_unexpected_provider_exception_uses_internal_error_and_refunds_once(self) -> None:
        self.deepseek.error = RuntimeError("provider unavailable")
        self.add_job(job_id="provider-error")
        self.assertTrue(self.engine.run_once())
        job = self.job("provider-error")
        self.assertEqual(("failed", "internal_error", 1), (job["state"], job["error_code"], job["refunded"]))
        self.assertEqual(120, self.stones())
        self.assertEqual(1, len(self.deepseek.calls))

    def test_safe_deepseek_exception_code_is_persisted_without_exception_text(self) -> None:
        secret = "do-not-persist-this-response-or-key"
        error = DeepSeekError(secret)
        error.code = "deepseek_timeout"
        self.deepseek.error = error
        self.add_job(job_id="safe-code")
        with self.assertLogs(level="WARNING") as logs:
            self.assertTrue(self.engine.run_once())
        job = self.job("safe-code")
        self.assertEqual(("failed", "deepseek_timeout", 1),
                         (job["state"], job["error_code"], job["refunded"]))
        self.assertEqual(120, self.stones())
        self.assertEqual(1, len(self.deepseek.calls))
        persisted = " ".join(str(value) for value in job)
        self.assertNotIn(secret, persisted)
        self.assertNotIn(secret, "\n".join(logs.output))
        self.assertIn("job_id=safe-code provider=deepseek code=deepseek_timeout exception_type=DeepSeekError",
                      "\n".join(logs.output))
        with transaction(self.store.db):
            self.engine._fail(job, "internal_error", self.clock_now + 1)
        self.assertEqual("deepseek_timeout", self.job("safe-code")["error_code"])

    def test_worker_diagnostics_identify_sqlite_failure_without_sensitive_error_body(self) -> None:
        def failing_ingest(_now):
            self.store.db.execute('SELECT * FROM "private-chat-and-credential-name"')

        self.engine.memory = SimpleNamespace(ingest=failing_ingest)
        with self.assertRaises(sqlite3.OperationalError) as captured:
            self.engine.run_once()
        self.assertEqual('memory_ingest', self.engine.phase)
        self.assertIn('private-chat-and-credential-name', str(captured.exception))
        with self.assertLogs(level='ERROR') as logs:
            _log_worker_error('AI worker cycle failed', captured.exception, phase=self.engine.phase)
        diagnostic = '\n'.join(logs.output)
        self.assertIn('phase=memory_ingest', diagnostic)
        self.assertIn('sqlite=SQLITE_ERROR code=1', diagnostic)
        self.assertNotIn('private-chat-and-credential-name', diagnostic)

    def test_memory_complete_records_allowlisted_validation_reason_without_model_text(self) -> None:
        self.add_memory_task(task_id='memory-known-validation', kind='daily_report',
                             answer='{"topics": "not a list"}')
        with self.assertLogs(level='WARNING') as logs:
            self.assertTrue(self.engine.run_once())
        task = self.store.db.execute("""SELECT state,last_error FROM ai_memory_tasks
            WHERE task_id='memory-known-validation'""").fetchone()
        self.assertEqual(('pending', 'memory_report_topics'), tuple(task))
        self.assertEqual(2, len(self.codex.calls))
        self.assertEqual(2, self.store.db.execute(
            "SELECT count(*) FROM ai_calls WHERE purpose='memory'"
        ).fetchone()[0])
        diagnostic = '\n'.join(logs.output)
        self.assertIn('phase=memory_complete', diagnostic)
        self.assertIn('requesting one correction', diagnostic)
        self.assertIn('phase=memory_retry_complete', diagnostic)
        self.assertIn('reason=memory_report_topics', diagnostic)
        self.assertNotIn('private prompt must never appear in diagnostics', diagnostic)

    def test_memory_complete_unknown_value_error_uses_generic_reason_without_error_text(self) -> None:
        secret = 'unknown validation secret wxid_private@chatroom sk-memory-token'
        self.add_memory_task(task_id='memory-unknown-validation', kind='daily_report', answer='{}')
        original_complete = self.engine.memory.complete

        def failing_complete(task, answer, now):
            del task, answer, now
            raise ValueError(secret)

        self.engine.memory.complete = failing_complete
        try:
            with self.assertLogs(level='WARNING') as logs:
                self.assertTrue(self.engine.run_once())
        finally:
            self.engine.memory.complete = original_complete
        task = self.store.db.execute("""SELECT state,last_error FROM ai_memory_tasks
            WHERE task_id='memory-unknown-validation'""").fetchone()
        self.assertEqual(('pending', 'memory_validation_error'), tuple(task))
        self.assertEqual(1, len(self.codex.calls))
        diagnostic = '\n'.join(logs.output)
        self.assertIn('phase=memory_complete', diagnostic)
        self.assertIn('reason=memory_validation_error', diagnostic)
        self.assertNotIn(secret, diagnostic)

    def test_memory_validation_correction_stops_when_second_call_has_no_quota(self) -> None:
        self.add_memory_task(task_id='memory-correction-quota', kind='daily_report',
                             answer='{"topics": "not a list"}',
                             memory_daily_limit=1, memory_group_daily_limit=1)
        self.assertTrue(self.engine.run_once())
        task = self.store.db.execute("""SELECT state,attempts,last_error FROM ai_memory_tasks
            WHERE task_id='memory-correction-quota'""").fetchone()
        self.assertEqual(('pending', 1, 'memory_report_topics'), tuple(task))
        self.assertEqual(1, len(self.codex.calls))
        self.assertEqual(1, self.store.db.execute(
            "SELECT count(*) FROM ai_calls WHERE purpose='memory'"
        ).fetchone()[0])

    def test_rejects_untrusted_exception_code_and_classifies_codex_without_text(self) -> None:
        secret = "not-a-valid-code secret"
        error = DeepSeekError(secret)
        error.code = secret
        self.deepseek.error = error
        self.add_job(job_id="invalid-code")
        self.assertTrue(self.engine.run_once())
        self.assertEqual("provider_error", self.job("invalid-code")["error_code"])
        self.assertNotIn(secret, " ".join(str(value) for value in self.job("invalid-code")))

        self.codex.error = CodexError("codex secret output")
        self.add_job(job_id="codex-code", provider="codex", cost=0)
        self.assertTrue(self.engine.run_once())
        self.assertEqual("codex_error", self.job("codex-code")["error_code"])
        self.assertNotIn("codex secret output", " ".join(str(value) for value in self.job("codex-code")))
        self.engine.run_once()
        self.assertEqual(120, self.stones())
        self.assertEqual(1, len(self.deepseek.calls))

    def test_memory_provider_persists_allowlisted_codex_failure_code(self) -> None:
        self.add_memory_task(task_id='memory-codex-code', kind='daily_report', answer='unused')
        self.codex.error = CodexError(
            'provider detail must not be stored', code='codex_server_overloaded')
        with self.assertLogs(level='WARNING') as logs:
            self.assertTrue(self.engine.run_once())
        task = self.store.db.execute("""SELECT state,last_error FROM ai_memory_tasks
            WHERE task_id='memory-codex-code'""").fetchone()
        self.assertEqual(('pending', 'codex_server_overloaded'), tuple(task))
        diagnostic = '\n'.join(logs.output)
        self.assertIn('reason=codex_server_overloaded', diagnostic)
        self.assertNotIn('provider detail must not be stored', diagnostic)

    def test_recover_running_refunds_once_and_never_replays_uncertain_call(self) -> None:
        self.add_job(job_id="interrupted", state="running")
        self.engine.recover(self.clock_now)
        self.assertEqual(("failed", "interrupted", 1),
                         (self.job("interrupted")["state"], self.job("interrupted")["error_code"], self.job("interrupted")["refunded"]))
        self.assertEqual(120, self.stones())
        self.engine.recover(self.clock_now + 1)
        self.assertEqual(120, self.stones())
        self.assertEqual(0, len(self.deepseek.calls))

    def test_expired_job_is_refunded_without_a_provider_call(self) -> None:
        self.add_job(job_id="expired", expires_at=self.clock_now)
        self.assertFalse(self.engine.run_once())
        self.assertEqual(("failed", "expired", 1),
                         (self.job("expired")["state"], self.job("expired")["error_code"], self.job("expired")["refunded"]))
        self.assertEqual(120, self.stones())
        self.assertEqual(0, len(self.deepseek.calls))

    def test_actual_daily_quota_refunds_without_a_provider_call(self) -> None:
        self.config = AIConfig(key_file=Path(self.temporary.name) / "not-read.key", memory_enabled=False,
                               deepseek_daily_limit=1, codex_daily_limit=10)
        self.bot.ai_config = self.config
        self.engine.config = self.config
        with self.store.db:
            self.store.db.execute("INSERT INTO ai_calls VALUES(?,?,?,?,?,?,?)", (
                "used-call", self.account, "deepseek", "qa", self.group, day_at(self.clock_now), self.clock_now,
            ))
        self.add_job(job_id="quota")
        self.assertTrue(self.engine.run_once())
        self.assertEqual(("failed", "quota", 1),
                         (self.job("quota")["state"], self.job("quota")["error_code"], self.job("quota")["refunded"]))
        self.assertEqual(120, self.stones())
        self.assertEqual(0, len(self.deepseek.calls))

    def test_daily_limit_exempt_job_uses_separate_audit_and_skips_qa_limit(self) -> None:
        self.config = AIConfig(key_file=Path(self.temporary.name) / "not-read.key", memory_enabled=False,
                               deepseek_daily_limit=1, codex_daily_limit=10,
                               memory_daily_limit=1, memory_group_daily_limit=1)
        self.bot.ai_config = self.config
        self.engine.config = self.config
        with self.store.db:
            self.store.db.execute("INSERT INTO ai_calls VALUES(?,?,?,?,?,?,?)", (
                "ordinary-used", self.account, "deepseek", "qa", self.group, day_at(self.clock_now), self.clock_now,
            ))
            self.store.db.execute("INSERT INTO ai_calls VALUES(?,?,?,?,?,?,?)", (
                "memory-used", self.account, "codex", "memory", self.group, day_at(self.clock_now), self.clock_now,
            ))
        self.add_job(job_id="admin-audit", daily_limit_exempt=True)
        self.assertTrue(self.engine.run_once())
        self.assertEqual("completed", self.job("admin-audit")["state"])
        self.assertEqual(1, len(self.deepseek.calls))
        purposes = [row[0] for row in self.store.db.execute(
            "SELECT purpose FROM ai_calls WHERE account_id=? ORDER BY purpose", (self.account,)
        )]
        self.assertEqual(["admin_qa", "memory", "qa"], purposes)

    def test_schema_upgrade_adds_exemption_to_an_existing_jobs_table_without_losing_rows(self) -> None:
        path = Path(self.temporary.name) / "legacy-ai.sqlite3"
        legacy = sqlite3.connect(path)
        try:
            legacy.execute("""CREATE TABLE ai_jobs (
                job_id TEXT PRIMARY KEY, account_id TEXT NOT NULL, event_key TEXT NOT NULL,
                message_key TEXT NOT NULL, conversation_id TEXT NOT NULL, user_id TEXT NOT NULL,
                nickname TEXT, session_id TEXT NOT NULL, provider TEXT NOT NULL,
                question TEXT NOT NULL, is_admin INTEGER NOT NULL, cost INTEGER NOT NULL,
                created_at REAL NOT NULL, created_day TEXT NOT NULL, expires_at REAL NOT NULL,
                state TEXT NOT NULL, started_at REAL, finished_at REAL, answer TEXT, error_code TEXT,
                refunded INTEGER NOT NULL DEFAULT 0, reply_request_id TEXT,
                UNIQUE(account_id,event_key), UNIQUE(account_id,message_key))""")
            legacy.execute("""INSERT INTO ai_jobs(job_id,account_id,event_key,message_key,conversation_id,user_id,
                session_id,provider,question,is_admin,cost,created_at,created_day,expires_at,state)
                VALUES('legacy','account','event','message','group','member','session','deepseek','kept',0,20,1,'2026-09-19',2,'queued')""")
            legacy.commit()
            initialize_schema(legacy)
            initialize_schema(legacy)
            row = legacy.execute("SELECT question,cost,daily_limit_exempt FROM ai_jobs WHERE job_id='legacy'").fetchone()
            self.assertEqual(("kept", 20, 0), row)
        finally:
            legacy.close()

    def test_unready_connection_keeps_answer_until_one_later_delivery(self) -> None:
        self.connection = FakeConnectionState(ready=False, account=self.account)
        self.add_job(job_id="deferred")
        self.assertTrue(self.engine.run_once())
        self.assertEqual(("completed", None), (self.job("deferred")["state"], self.job("deferred")["reply_request_id"]))
        self.assertEqual(0, self.outbox_count())
        self.connection = FakeConnectionState(ready=True, account=self.account, connection_id="new-session")
        self.assertFalse(self.engine.run_once())
        self.assertEqual("ai-answer-deferred", self.job("deferred")["reply_request_id"])
        self.assertEqual(1, self.outbox_count())
        self.assertEqual(1, len(self.deepseek.calls))

    def test_deepseek_is_stateless_and_codex_context_stays_in_one_conversation(self) -> None:
        self.add_job(job_id="other-history", provider="codex", group=self.other_group,
                     question="other secret", cost=0, state="completed", answer="other answer")
        self.add_job(job_id="same-history", provider="codex", group=self.group,
                     question="same history", cost=0, state="completed", answer="same answer")
        self.add_job(job_id="deepseek", question="only this prompt")
        self.assertTrue(self.engine.run_once())
        self.assertEqual("only this prompt", self.deepseek.calls[0][0])

        self.add_job(job_id="codex", provider="codex", question="codex current")
        self.assertTrue(self.engine.run_once())
        prompt, kwargs = self.codex.calls[0]
        context = json.loads(prompt)
        self.assertEqual("codex current", context["question"])
        self.assertEqual(["same history"], [item["question"] for item in context["recent_questions"]])
        self.assertNotIn("other secret", prompt)
        self.assertEqual({"instructions": QA_INSTRUCTIONS, "admin_tools": False}, kwargs)

    def test_worker_suspends_memory_and_reports_when_tianji_disabled(self) -> None:
        from wechat_receiver.ai.settings import set_ai_feature
        with self.store.db:
            set_ai_feature(self.store.db, self.account, "tianji", False,
                           event_key="evt:tianji-off", message_key="msg:tianji-off",
                           updated_at=self.now, updated_by="admin")
        self.assertFalse(self.engine.run_once())
        self.assertEqual(0, len(self.codex.calls))

