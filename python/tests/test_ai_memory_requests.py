"""Offline integration coverage for the administrator memory-flush command."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace

from wechat_receiver.ai.config import AIConfig
from wechat_receiver.ai.profiles import DEFAULT_PROFILE, PROFILES
from wechat_receiver.ai.worker import AIEngine
from wechat_receiver.models import Message
from wechat_receiver.plugins import load_plugins
from wechat_receiver.reply_router import ReplyRouter
from wechat_receiver.store import Store
from wechat_receiver.tail import Line


class FakeDeepSeek:
    def configured(self) -> bool:
        return True


class FakeCodex:
    def __init__(self, *, fail_once: bool = False, scripted_answers=()) -> None:
        self.fail_once = fail_once
        self.scripted_answers = list(scripted_answers)
        self.calls: list[tuple[str, dict]] = []
        self.profile = DEFAULT_PROFILE

    def select_profile(self, profile: str) -> None:
        if profile not in PROFILES:
            raise ValueError("invalid test profile")
        self.profile = profile

    def check_available(self) -> None:
        pass

    def complete(self, prompt: str, **kwargs) -> str:
        self.calls.append((prompt, kwargs))
        if self.fail_once:
            self.fail_once = False
            raise RuntimeError("local fake failure")
        if self.scripted_answers:
            answer = self.scripted_answers.pop(0)
            return answer(prompt) if callable(answer) else answer
        original_prompt = prompt.split("\n\n【记忆校验纠错】", 1)[0]
        payload = json.loads(original_prompt.rsplit("输入：", 1)[1])
        raw_ids = [item["raw_id"] for item in payload]
        return json.dumps({
            "excluded_ids": raw_ids[1:],
            "summary_items": [{"source_ids": raw_ids[:1], "member_id": "wxid_member",
                               "text": "群友讨论周末安排"}],
        }, ensure_ascii=False)


class ReadyConnection:
    send_ready = True
    account_verified = True
    send_group_ready = True

    def __init__(self, account: str) -> None:
        self.account_id = account
        self.connection_id = "memory-test-session"


class MemoryRequestTests(unittest.TestCase):
    account = "wxid_bot"
    group = "memory-request@chatroom"
    other_group = "other-memory-request@chatroom"
    admin = "wxid_admin"
    member = "wxid_member"
    now = datetime(2026, 9, 19, 12, tzinfo=timezone.utc).timestamp()

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temporary.name) / "messages.sqlite3")
        self.clock_now = self.now
        self.config = AIConfig(
            key_file=Path(self.temporary.name) / "not-read.key",
            memory_enabled=True,
            memory_batch_size=100,
            memory_daily_limit=1,
            memory_group_daily_limit=1,
        )
        self.bot = SimpleNamespace(
            sender=SimpleNamespace(account_id=self.account,
                                   allowed_targets=frozenset({self.group, self.other_group, self.admin})),
            admin_ids=(self.admin,),
            ai_config=self.config,
        )
        router_config = SimpleNamespace(
            sender=self.bot.sender, enabled_plugins=("ai",), reply_ttl_seconds=60,
            max_message_age_seconds=120, admin_ids=(self.admin,), ai_config=self.config,
        )
        plugin_root = Path(__file__).resolve().parents[2] / "plugins"
        self.router = ReplyRouter(self.store, router_config, load_plugins(plugin_root, ("ai",)),
                                  started_at=self.now - 1)
        self.deepseek = FakeDeepSeek()
        self.codex = FakeCodex()
        self.connection = ReadyConnection(self.account)
        self.engine = AIEngine(self.store.db, self.bot, self.deepseek, self.codex,
                               lambda: self.connection, clock=lambda: self.clock_now)
        self.source = self.store.new_source(Path(self.temporary.name) / "observer.jsonl", "memory-test")
        self.sequence = 0

    def tearDown(self) -> None:
        self.store.close()
        self.temporary.cleanup()

    def command(self, text: str, *, sender: str | None = None, conversation: str | None = None,
                event_key: str | None = None, message_id: str | None = None) -> Message:
        self.sequence += 1
        number = self.sequence
        return Message(
            session_id="memory-test", event_key=event_key or f"memory-test:{number}", seq=number,
            call_id=number, source="receive_batch", event_kind="item",
            observed_at_ms=int(self.clock_now * 1000), message_type=1, message_kind="text",
            app_message_type=None, content=text, raw_content=text,
            conversation_id=conversation or self.group, sender_id=sender or self.member,
            direction="incoming", message_time_candidate=int(self.clock_now),
            message_id_candidate=message_id or f"message-{number}", mentioned_ids=(),
            mention_state="none", history_state="live_candidate",
        )

    def send(self, text: str, **kwargs) -> int:
        return self.router.handle(self.command(text, **kwargs), "memory-test", now=self.clock_now)

    def requests(self):
        return self.store.db.execute("SELECT * FROM ai_memory_requests ORDER BY created_at,request_id").fetchall()

    def add_natural_messages(self, count: int, *, group: str | None = None,
                             member: str | None = None) -> None:
        group = group or self.group
        member = member or self.member
        offset = int(self.source["offset"])
        lines = []
        for index in range(count):
            self.sequence += 1
            record = {
                "kind": "item", "schema_version": 2, "source": "receive_batch",
                "session_id": "memory-test", "seq": 10_000 + self.sequence,
                "call_id": self.sequence, "observed_unix_ms": int(self.clock_now * 1000) + index,
                "msg_type": 1, "from": group, "to": self.account,
                "content": f"{member}:\n普通聊天 {self.sequence}",
                "from_read": {"status": "ok"}, "to_read": {"status": "ok"},
                "content_read": {"status": "ok"},
            }
            raw = (json.dumps(record, ensure_ascii=False) + "\n").encode("utf-8")
            lines.append(Line(offset, offset + len(raw), raw))
            offset += len(raw)
        self.store.ingest_batch(self.source["id"], lines, b"memory-anchor", self_id=self.account)
        self.source = self.store.source(self.source["id"])

    def test_member_is_rejected_without_a_request_or_charge(self) -> None:
        self.assertEqual(1, self.send("#整理记忆"))
        self.assertEqual([], self.requests())
        self.assertIn("仅限管理员", self.store.db.execute(
            "SELECT text FROM outbox ORDER BY rowid DESC LIMIT 1").fetchone()[0])
        self.assertEqual(0, self.store.db.execute("SELECT count(*) FROM ai_jobs").fetchone()[0])

    def test_admin_group_scope_private_scope_and_pending_overlap_are_idempotent(self) -> None:
        self.add_natural_messages(2)
        self.assertEqual(1, self.send("#整理记忆", sender=self.admin, message_id="group-command"))
        first = self.requests()[0]
        self.assertEqual([self.group], json.loads(first["target_groups_json"]))
        self.assertEqual(2, first["cutoff_raw_id"])
        self.assertEqual(0, self.send("#整理记忆", sender=self.admin, event_key="replay",
                                      message_id="group-command"))
        self.assertEqual(1, len(self.requests()))

        self.assertEqual(1, self.send("#整理记忆", sender=self.admin, conversation=self.admin))
        self.assertEqual(1, len(self.requests()))
        self.assertIn("已有记忆整理任务", self.store.db.execute(
            "SELECT text FROM outbox ORDER BY rowid DESC LIMIT 1").fetchone()[0])

        with self.store.db:
            self.store.db.execute("UPDATE ai_memory_requests SET state='completed'")
        self.clock_now += 1
        self.assertEqual(1, self.send("#整理记忆", sender=self.admin, conversation=self.admin))
        targets = json.loads(self.requests()[-1]["target_groups_json"])
        self.assertEqual(sorted([self.group, self.other_group]), targets)

    def test_memory_disabled_rejects_admin_without_creating_a_request(self) -> None:
        self.config = AIConfig(key_file=Path(self.temporary.name) / "not-read.key", memory_enabled=False)
        self.router.config.ai_config = self.config
        self.assertEqual(1, self.send("#整理记忆", sender=self.admin))
        self.assertEqual([], self.requests())
        self.assertIn("暂未启用", self.store.db.execute(
            "SELECT text FROM outbox ORDER BY rowid DESC LIMIT 1").fetchone()[0])

    def test_manual_request_flushes_under_100_messages_with_admin_quota_and_single_notice(self) -> None:
        self.add_natural_messages(3)
        self.assertEqual(1, self.send("#整理记忆", sender=self.admin))
        with self.store.db:
            self.store.db.execute(
                "INSERT INTO ai_calls VALUES(?,?,?,?,?,?,?)",
                ("regular-memory-used", self.account, "codex", "memory", self.group,
                 "2026-09-19", self.clock_now),
            )
        self.assertTrue(self.engine.run_once())
        request = self.requests()[0]
        self.assertEqual("completed", request["state"])
        self.assertEqual(1, len(self.codex.calls))
        self.assertEqual(False, self.codex.calls[0][1]["admin_tools"])
        self.assertEqual("admin_memory", self.store.db.execute(
            "SELECT purpose FROM ai_calls WHERE purpose='admin_memory'").fetchone()[0])
        self.assertEqual(100, self.engine.memory.batch_size)
        self.assertEqual(1, self.store.db.execute(
            "SELECT count(*) FROM outbox WHERE request_id=?", (request["reply_request_id"],)).fetchone()[0])
        self.assertFalse(self.engine.run_once())
        self.assertEqual(1, len(self.codex.calls))

    def test_mixed_author_answer_is_corrected_once_and_both_calls_are_audited(self) -> None:
        other_member = "wxid_other_member"

        def records(prompt: str):
            original = prompt.split("\n\n【记忆校验纠错】", 1)[0]
            return json.loads(original.rsplit("输入：", 1)[1])

        def mixed_answer(prompt: str) -> str:
            items = records(prompt)
            return json.dumps({
                "excluded_ids": [],
                "summary_items": [{
                    "source_ids": [item["raw_id"] for item in items],
                    "member_id": items[0]["member_id"],
                    "text": "两位群友讨论各自的周末安排",
                }],
            }, ensure_ascii=False)

        def corrected_answer(prompt: str) -> str:
            items = records(prompt)
            return json.dumps({
                "excluded_ids": [],
                "summary_items": [{
                    "source_ids": [item["raw_id"]],
                    "member_id": item["member_id"],
                    "text": f"{index + 1}号群友讨论周末安排",
                } for index, item in enumerate(items)],
            }, ensure_ascii=False)

        self.codex = FakeCodex(scripted_answers=(mixed_answer, corrected_answer))
        self.engine.codex = self.codex
        self.add_natural_messages(1)
        self.add_natural_messages(1, member=other_member)
        self.assertEqual(1, self.send("#整理记忆", sender=self.admin))
        self.assertTrue(self.engine.run_once())
        self.assertEqual("completed", self.requests()[0]["state"])
        self.assertEqual(2, len(self.codex.calls))
        self.assertIn("【记忆校验纠错】", self.codex.calls[1][0])
        self.assertEqual(self.codex.calls[0][1], self.codex.calls[1][1])
        self.assertEqual(False, self.codex.calls[1][1]["admin_tools"])
        self.assertEqual(2, self.store.db.execute(
            "SELECT count(*) FROM ai_calls WHERE purpose='admin_memory'"
        ).fetchone()[0])

    def test_provider_failure_retries_after_sixty_seconds_and_keeps_later_messages_pending(self) -> None:
        self.codex = FakeCodex(fail_once=True)
        self.engine.codex = self.codex
        self.add_natural_messages(3)
        self.assertEqual(1, self.send("#整理记忆", sender=self.admin))
        self.assertTrue(self.engine.run_once())
        self.assertEqual("pending", self.requests()[0]["state"])
        request = self.requests()[0]
        self.assertIsNone(request["reply_request_id"])
        progress_id = "ai-memory-progress-" + request["request_id"]
        progress = self.store.db.execute(
            "SELECT text FROM outbox WHERE request_id=?", (progress_id,)
        ).fetchone()
        self.assertIsNotNone(progress)
        self.assertIn("相关消息均已保留", progress[0])
        self.assertIn("预计下次尝试时间", progress[0])
        self.assertEqual(3, self.store.db.execute(
            "SELECT count(*) FROM ai_memory_candidates WHERE account_id=?", (self.account,)).fetchone()[0])

        self.add_natural_messages(1)
        self.clock_now += 60
        self.assertTrue(self.engine.run_once())
        self.assertEqual("completed", self.requests()[0]["state"])
        result_id = "ai-memory-result-" + request["request_id"]
        self.assertEqual(result_id, self.requests()[0]["reply_request_id"])
        self.assertEqual(1, self.store.db.execute(
            "SELECT count(*) FROM outbox WHERE request_id=?", (progress_id,)
        ).fetchone()[0])
        self.assertEqual(1, self.store.db.execute(
            "SELECT count(*) FROM outbox WHERE request_id=?", (result_id,)
        ).fetchone()[0])
        self.assertEqual(2, len(self.codex.calls))
        self.assertEqual(1, self.store.db.execute(
            "SELECT count(*) FROM ai_memory_candidates WHERE account_id=? AND disposition='pending'",
            (self.account,)).fetchone()[0])

    def test_existing_failed_manual_task_gets_one_recovery_progress_notice(self) -> None:
        self.add_natural_messages(2)
        self.assertEqual(1, self.send("#整理记忆", sender=self.admin))
        self.engine.memory.ingest(self.clock_now)
        self.engine._sync_memory_requests(self.clock_now)
        task = self.store.db.execute("""SELECT task_id FROM ai_memory_tasks
            WHERE account_id=? AND json_extract(payload_json,'$.manual_request_id') IS NOT NULL""",
            (self.account,)).fetchone()
        self.assertIsNotNone(task)
        with self.store.db:
            self.store.db.execute("""UPDATE ai_memory_tasks
                SET attempts=5,last_error='memory_summary_sources',available_at=?
                WHERE task_id=?""", (self.clock_now + 300, task["task_id"]))

        self.assertFalse(self.engine.run_once())
        request = self.requests()[0]
        progress_id = "ai-memory-progress-" + request["request_id"]
        self.assertIsNone(request["reply_request_id"])
        self.assertEqual(1, self.store.db.execute(
            "SELECT count(*) FROM outbox WHERE request_id=?", (progress_id,)
        ).fetchone()[0])
        self.assertIn("相关消息均已保留", self.store.db.execute(
            "SELECT text FROM outbox WHERE request_id=?", (progress_id,)
        ).fetchone()[0])

        self.clock_now += 1
        self.assertFalse(self.engine.run_once())
        self.assertEqual(1, self.store.db.execute(
            "SELECT count(*) FROM outbox WHERE request_id=?", (progress_id,)
        ).fetchone()[0])
        self.assertEqual(0, len(self.codex.calls))


if __name__ == "__main__":
    unittest.main()
