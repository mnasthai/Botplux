from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest

from wechat_receiver.ai.memory import MemoryManager, MemoryTask, _transaction


SHANGHAI = timezone(timedelta(hours=8))
ACCOUNT = "wxid_bot"
GROUP = "room@chatroom"


def at(day: int, hour: int = 12) -> float:
    return datetime(2026, 1, day, hour, tzinfo=SHANGHAI).timestamp()


class MemoryTransactionTests(unittest.TestCase):
    def test_top_level_transaction_reserves_wal_writer_before_reading(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "memory.sqlite3"
            db = sqlite3.connect(path, timeout=1)
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("CREATE TABLE sample(id INTEGER PRIMARY KEY, value INTEGER NOT NULL)")
            db.execute("INSERT INTO sample VALUES(1,0)")
            db.commit()
            competing_result: dict[str, object] = {}

            def competing_writer() -> None:
                other = sqlite3.connect(path, timeout=0)
                try:
                    other.execute("BEGIN IMMEDIATE")
                    other.execute("UPDATE sample SET value=1 WHERE id=1")
                    other.commit()
                    competing_result["committed"] = True
                except sqlite3.OperationalError as error:
                    competing_result["code"] = error.sqlite_errorcode
                    competing_result["name"] = error.sqlite_errorname
                    other.rollback()
                finally:
                    other.close()

            try:
                with _transaction(db):
                    self.assertEqual(0, db.execute("SELECT value FROM sample WHERE id=1").fetchone()[0])
                    thread = threading.Thread(target=competing_writer)
                    thread.start()
                    thread.join(timeout=2)
                    self.assertFalse(thread.is_alive())
                    self.assertEqual("SQLITE_BUSY", competing_result.get("name"))
                    db.execute("UPDATE sample SET value=2 WHERE id=1")
                self.assertEqual(2, db.execute("SELECT value FROM sample WHERE id=1").fetchone()[0])

                # The top-level helper released its writer transaction.
                other = sqlite3.connect(path, timeout=1)
                try:
                    other.execute("BEGIN IMMEDIATE")
                    other.execute("UPDATE sample SET value=3 WHERE id=1")
                    other.commit()
                finally:
                    other.close()
                self.assertEqual(3, db.execute("SELECT value FROM sample WHERE id=1").fetchone()[0])
            finally:
                db.close()

    def test_nested_transaction_uses_savepoint_without_committing_outer_work(self) -> None:
        db = sqlite3.connect(":memory:")
        try:
            db.execute("CREATE TABLE sample(id INTEGER PRIMARY KEY, value TEXT NOT NULL)")
            db.execute("BEGIN IMMEDIATE")
            db.execute("INSERT INTO sample VALUES(1,'outer')")
            with self.assertRaisesRegex(RuntimeError, "inner failure"):
                with _transaction(db):
                    db.execute("INSERT INTO sample VALUES(2,'inner')")
                    raise RuntimeError("inner failure")
            self.assertTrue(db.in_transaction)
            self.assertEqual([(1, "outer")], db.execute("SELECT * FROM sample ORDER BY id").fetchall())

            with _transaction(db):
                db.execute("INSERT INTO sample VALUES(3,'nested success')")
            self.assertTrue(db.in_transaction)
            self.assertEqual(2, db.execute("SELECT COUNT(*) FROM sample").fetchone()[0])
            db.rollback()
            self.assertEqual([], db.execute("SELECT * FROM sample").fetchall())
        finally:
            db.close()


class MemoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.db = sqlite3.connect(":memory:")
        self.db.execute("""CREATE TABLE messages (
            raw_event_id INTEGER PRIMARY KEY, session_id TEXT NOT NULL,
            conversation_id TEXT, sender_id TEXT, direction TEXT NOT NULL,
            message_type INTEGER, observed_at_ms INTEGER,
            message_id_candidate TEXT, history_state TEXT NOT NULL,
            model_json TEXT NOT NULL)""")
        self.memory = MemoryManager(self.db, ACCOUNT, (GROUP,))
        self.memory.initialize(0)
        self.next_raw = 1

    def tearDown(self) -> None:
        self.db.close()

    def add(self, content: str, when: float, *, sender: str = "u1", nickname: str | None = "甲",
            group: str = GROUP, kind: str = "text", direction: str = "incoming",
            history: str = "live_candidate", message_id: str | None = None) -> int:
        raw_id = self.next_raw
        self.next_raw += 1
        observed = int(when * 1000)
        model = {
            "message_kind": kind,
            "event_kind": "item",
            "source": "receive_batch",
            "content": content,
            "observed_at_ms": observed,
            "sender_group_nickname": nickname,
        }
        self.db.execute(
            "INSERT INTO messages VALUES(?,?,?,?,?,?,?,?,?,?)",
            (raw_id, "s", group, sender, direction, 1, observed,
             str(raw_id) if message_id is None else message_id, history,
             json.dumps(model, ensure_ascii=False)),
        )
        self.db.commit()
        return raw_id

    def add_many(self, amount: int, when: float, *, senders: tuple[str, ...] = ("u1",)) -> None:
        for index in range(amount):
            sender = senders[index % len(senders)]
            nickname = {"u1": "甲", "u2": None}.get(sender, sender)
            self.add(f"普通聊天 {index}", when + index / 1000, sender=sender, nickname=nickname)

    def answer_semantic(self, task: MemoryTask, *, excluded: list[int] | None = None) -> str:
        excluded = excluded or []
        rows = self.db.execute(
            """SELECT raw_event_id,member_id FROM ai_memory_candidates
               WHERE account_id=? AND task_id=? ORDER BY raw_event_id""",
            (ACCOUNT, task.task_id),
        ).fetchall()
        retained = [(raw_id, member) for raw_id, member in rows if raw_id not in excluded]
        items = [] if not retained else [{
            "source_ids": [retained[0][0]], "member_id": retained[0][1], "text": "群友进行了普通聊天",
        }]
        return json.dumps({"excluded_ids": excluded, "summary_items": items}, ensure_ascii=False)

    @staticmethod
    def answer_compression(task: MemoryTask) -> str:
        payload = json.loads(task.prompt.split("来源：", 1)[1])
        for batch in payload:
            if batch["summary"]:
                source = batch["summary"][0]
                return json.dumps({"summary_items": [{
                    "source_ids": source["source_ids"], "member_id": source["member_id"],
                    "text": "压缩后的长期记忆",
                }]}, ensure_ascii=False)
        raise AssertionError("compression source unexpectedly empty")

    def test_100_and_101_boundaries_and_duplicate_completion(self) -> None:
        now = at(2)
        self.add_many(101, now)
        self.assertEqual(101, self.memory.ingest(now))
        task = self.memory.next_task(now)
        self.assertIsNotNone(task)
        self.assertEqual("semantic_batch", task.kind)
        self.memory.complete(task, self.answer_semantic(task), now)
        self.memory.complete(task, "not parsed on idempotent replay", now)

        status = self.memory.status()["groups"][0]
        self.assertEqual(100, status["batched_messages"])
        self.assertEqual(1, status["pending_candidates"])
        self.assertEqual(1, status["active_batches"])
        self.assertIsNone(self.memory.next_task(now))

    def test_manual_flush_persists_short_tail_and_leaves_new_messages_pending(self) -> None:
        now = at(2)
        self.add_many(3, now)
        cutoff = self.next_raw - 1
        self.memory.request_flush("manual-1", GROUP, cutoff, now)
        self.assertEqual(
            {"state": "pending", "cutoff_raw_id": cutoff},
            self.memory.flush_status("manual-1"),
        )
        self.add("命令之后的新消息", now + 1, sender="u2", nickname="乙")
        self.memory.ingest(now + 2)

        task = self.memory.next_task(now + 2)
        self.assertEqual("manual-1", task.manual_request_id)
        payload = json.loads(self.db.execute(
            "SELECT payload_json FROM ai_memory_tasks WHERE task_id=?", (task.task_id,),
        ).fetchone()[0])
        self.assertEqual([1, 2, 3], payload["raw_ids"])
        self.memory.complete(task, self.answer_semantic(task), now + 2)
        self.memory.complete(task, "duplicate completion is not parsed", now + 2)

        self.assertEqual(
            {"state": "completed", "cutoff_raw_id": cutoff},
            self.memory.flush_status("manual-1"),
        )
        status = self.memory.status()["groups"][0]
        self.assertEqual(3, status["batched_messages"])
        self.assertEqual(1, status["pending_candidates"])
        self.memory.request_flush("manual-1", GROUP, cutoff, now + 3)
        self.assertEqual(1, self.db.execute(
            "SELECT COUNT(*) FROM ai_memory_flushes WHERE account_id=? AND request_id=?",
            (ACCOUNT, "manual-1"),
        ).fetchone()[0])

    def test_manual_flush_rejects_unconfigured_group_and_conflicting_replay(self) -> None:
        now = at(2)
        with self.assertRaisesRegex(ValueError, "not enabled"):
            self.memory.request_flush("outside", "other@chatroom", 0, now)
        self.memory.request_flush("stable", GROUP, 0, now)
        with self.assertRaisesRegex(ValueError, "different flush boundary"):
            self.memory.request_flush("stable", GROUP, 1, now)

    def test_manual_flush_adopts_retrying_semantic_task_and_survives_restart(self) -> None:
        memory = MemoryManager(self.db, "retry", (GROUP,), batch_size=2)
        memory.initialize(0)
        now = at(2)
        self.add_many(2, now)
        memory.ingest(now)
        memory.request_flush("manual-retry", GROUP, self.next_raw - 1, now)
        task = memory.next_task(now)
        self.assertEqual("manual-retry", task.manual_request_id)
        memory.fail(task, "temporary", now)
        self.assertIsNone(memory.next_task(now + 59))

        restarted = MemoryManager(self.db, "retry", (GROUP,), batch_size=2)
        restarted.initialize(0)
        self.assertEqual("pending", restarted.flush_status("manual-retry")["state"])
        retry = restarted.next_task(now + 60)
        self.assertEqual(task.task_id, retry.task_id)
        self.assertEqual("manual-retry", retry.manual_request_id)
        rows = self.db.execute(
            """SELECT raw_event_id,member_id FROM ai_memory_candidates
               WHERE account_id='retry' AND task_id=? ORDER BY raw_event_id""",
            (retry.task_id,),
        ).fetchall()
        restarted.complete(retry, json.dumps({
            "excluded_ids": [rows[0][0]],
            "summary_items": [{"source_ids": [rows[1][0]], "member_id": rows[1][1],
                               "text": "保留的普通聊天"}],
        }, ensure_ascii=False), now + 60)
        self.assertEqual("completed", restarted.flush_status("manual-retry")["state"])
        self.assertEqual(1, restarted.status()["groups"][0]["batched_messages"])

    def test_manual_flush_requeues_running_task_that_crosses_cutoff(self) -> None:
        memory = MemoryManager(self.db, "cross-cutoff", (GROUP,), batch_size=4)
        memory.initialize(0)
        now = at(2)
        self.add_many(4, now)
        memory.ingest(now)
        running = memory.next_task(now)
        self.assertIsNone(running.manual_request_id)

        memory.request_flush("manual-cross", GROUP, 2, now)
        # The worker already received all four rows. A mixed summary must not
        # batch pre-cutoff rows without a usable summary or consume rows 3-4.
        memory.complete(running, json.dumps({
            "excluded_ids": [],
            "summary_items": [{"source_ids": [1, 3], "member_id": "u1",
                               "text": "边界前后的混合摘要"}],
        }, ensure_ascii=False), now)
        self.assertEqual(0, memory.status()["groups"][0]["batched_messages"])

        manual = memory.next_task(now)
        self.assertEqual("manual-cross", manual.manual_request_id)
        payload = json.loads(self.db.execute(
            "SELECT payload_json FROM ai_memory_tasks WHERE task_id=?", (manual.task_id,),
        ).fetchone()[0])
        self.assertEqual([1, 2], payload["raw_ids"])
        rows = self.db.execute(
            """SELECT raw_event_id,member_id FROM ai_memory_candidates
               WHERE account_id='cross-cutoff' AND task_id=? ORDER BY raw_event_id""",
            (manual.task_id,),
        ).fetchall()
        memory.complete(manual, json.dumps({
            "excluded_ids": [],
            "summary_items": [{"source_ids": [rows[0][0]], "member_id": rows[0][1],
                               "text": "边界内的普通聊天"}],
        }, ensure_ascii=False), now)
        self.assertEqual("completed", memory.flush_status("manual-cross")["state"])
        status = memory.status()["groups"][0]
        self.assertEqual(2, status["batched_messages"])
        self.assertEqual(2, status["pending_candidates"])

    def test_semantic_game_filter_keeps_real_video_game_and_refills_batch(self) -> None:
        memory = MemoryManager(self.db, ACCOUNT + "2", (GROUP,), batch_size=2)
        memory.initialize(0)
        now = at(2)
        robot_game = self.add("修仙机器人今天的战绩和灵石", now, sender="u1")
        real_game = self.add("今晚一起玩 Steam 上的电子游戏", now + 1, sender="u2", nickname=None)
        memory.ingest(now + 2)
        task = memory.next_task(now + 2)
        self.assertIn("保留普通人类聊天", task.prompt)
        rows = self.db.execute(
            "SELECT raw_event_id,member_id FROM ai_memory_candidates WHERE account_id=? AND task_id=?",
            (ACCOUNT + "2", task.task_id),
        ).fetchall()
        kept_member = next(member for raw_id, member in rows if raw_id == real_game)
        answer = {"excluded_ids": [robot_game],
                  "summary_items": [{"source_ids": [real_game], "member_id": kept_member,
                                     "text": "群友讨论 Steam 游戏"}]}
        memory.complete(task, json.dumps(answer, ensure_ascii=False), now + 2)
        self.assertEqual(1, memory.status()["groups"][0]["pending_candidates"])

        self.add("周末看电影", now + 3, sender="u1")
        memory.ingest(now + 4)
        task = memory.next_task(now + 4)
        rows = self.db.execute(
            "SELECT raw_event_id,member_id FROM ai_memory_candidates WHERE account_id=? AND task_id=? ORDER BY raw_event_id",
            (ACCOUNT + "2", task.task_id),
        ).fetchall()
        memory.complete(task, json.dumps({
            "excluded_ids": [],
            "summary_items": [{"source_ids": [rows[0][0]], "member_id": rows[0][1],
                               "text": "群友讨论游戏和周末安排"}],
        }, ensure_ascii=False), now + 4)
        self.assertEqual(2, memory.status()["groups"][0]["batched_messages"])

    def test_cross_day_tail_never_mixes_with_next_day(self) -> None:
        self.add_many(100, at(1))
        self.add_many(100, at(2))
        self.memory.ingest(at(2, 13))
        task = self.memory.next_task(at(2, 13))
        self.assertEqual("semantic_batch", task.kind)
        self.assertEqual("2026-01-01", task.day)
        payload = json.loads(self.db.execute(
            "SELECT payload_json FROM ai_memory_tasks WHERE task_id=?", (task.task_id,)
        ).fetchone()[0])
        self.assertEqual(100, len(payload["raw_ids"]))
        self.memory.complete(task, self.answer_semantic(task), at(2, 13))
        day2_pending = self.db.execute(
            """SELECT COUNT(*) FROM ai_memory_candidates WHERE account_id=?
               AND day='2026-01-02' AND disposition='pending'""", (ACCOUNT,)
        ).fetchone()[0]
        self.assertEqual(100, day2_pending)

    def test_closed_day_uses_remaining_candidates_before_making_short_tail(self) -> None:
        memory = MemoryManager(self.db, "tail", (GROUP,), batch_size=4)
        memory.initialize(0)
        self.add_many(6, at(1))
        memory.ingest(at(2))
        first = memory.next_task(at(2))
        first_rows = self.db.execute(
            "SELECT raw_event_id,member_id FROM ai_memory_candidates WHERE account_id='tail' AND task_id=?",
            (first.task_id,),
        ).fetchall()
        memory.complete(first, json.dumps({
            "excluded_ids": [first_rows[0][0]],
            "summary_items": [{"source_ids": [first_rows[1][0]], "member_id": first_rows[1][1],
                               "text": "保留的普通聊天"}],
        }, ensure_ascii=False), at(2))
        self.assertEqual(0, memory.status()["groups"][0]["batched_messages"])

        second = memory.next_task(at(2))
        second_rows = self.db.execute(
            "SELECT raw_event_id,member_id FROM ai_memory_candidates WHERE account_id='tail' AND task_id=?",
            (second.task_id,),
        ).fetchall()
        self.assertEqual(4, len(second_rows))
        memory.complete(second, json.dumps({
            "excluded_ids": [],
            "summary_items": [{"source_ids": [second_rows[0][0]], "member_id": second_rows[0][1],
                               "text": "已填满的有效批次"}],
        }, ensure_ascii=False), at(2))
        self.assertEqual(4, memory.status()["groups"][0]["batched_messages"])

    def test_600_messages_create_six_batches_then_compress_to_one(self) -> None:
        now = at(2)
        self.add_many(600, now)
        self.memory.ingest(now, limit=2000)
        for _ in range(6):
            task = self.memory.next_task(now)
            self.assertEqual("semantic_batch", task.kind)
            self.assertLessEqual(len(task.prompt.encode("utf-8")), 220 * 1024)
            rows = self.db.execute(
                """SELECT raw_event_id,member_id FROM ai_memory_candidates
                   WHERE account_id=? AND task_id=? ORDER BY raw_event_id LIMIT 24""",
                (ACCOUNT, task.task_id),
            ).fetchall()
            self.memory.complete(task, json.dumps({
                "excluded_ids": [],
                "summary_items": [
                    {"source_ids": [raw_id], "member_id": member, "text": "记" * 240}
                    for raw_id, member in rows
                ],
            }, ensure_ascii=False), now)
        states = dict(self.db.execute(
            "SELECT state,COUNT(*) FROM ai_memory_batches WHERE account_id=? GROUP BY state", (ACCOUNT,)
        ))
        self.assertEqual({"active": 5, "pending_compression": 1}, states)

        task = self.memory.next_task(now)
        self.assertEqual("compress", task.kind)
        self.assertLessEqual(len(task.prompt.encode("utf-8")), 220 * 1024)
        self.memory.complete(task, self.answer_compression(task), now)
        states = dict(self.db.execute(
            "SELECT state,COUNT(*) FROM ai_memory_batches WHERE account_id=? GROUP BY state", (ACCOUNT,)
        ))
        self.assertEqual(1, states["active"])
        self.assertEqual(6, states["replaced"])
        self.assertEqual(600, self.memory.status()["groups"][0]["batched_messages"])
        replaced = self.db.execute(
            "SELECT summary_json,source_task_id FROM ai_memory_batches WHERE account_id=? AND state='replaced'",
            (ACCOUNT,),
        ).fetchall()
        self.assertTrue(all(summary == "[]" for summary, _ in replaced))
        for _, source_task_id in replaced:
            prompt, answer = self.db.execute(
                "SELECT prompt,answer_json FROM ai_memory_tasks WHERE task_id=?", (source_task_id,)
            ).fetchone()
            self.assertIn("compacted", prompt)
            self.assertIsNone(answer)

    def test_compression_failure_keeps_active_and_pending_batches(self) -> None:
        memory = MemoryManager(self.db, "small", (GROUP,), batch_size=2, max_batches=1)
        memory.initialize(0)
        now = at(2)
        self.add_many(4, now)
        memory.ingest(now)
        for _ in range(2):
            task = memory.next_task(now)
            rows = self.db.execute(
                "SELECT raw_event_id,member_id FROM ai_memory_candidates WHERE account_id=? AND task_id=?",
                ("small", task.task_id),
            ).fetchall()
            memory.complete(task, json.dumps({
                "excluded_ids": [],
                "summary_items": [{"source_ids": [rows[0][0]], "member_id": rows[0][1],
                                   "text": "有效记忆"}],
            }, ensure_ascii=False), now)
        task = memory.next_task(now)
        self.assertEqual("compress", task.kind)
        memory.fail(task, "temporary", now)
        states = dict(self.db.execute(
            "SELECT state,COUNT(*) FROM ai_memory_batches WHERE account_id='small' GROUP BY state"
        ))
        self.assertEqual({"active": 1, "pending_compression": 1}, states)
        self.assertIsNone(memory.next_task(now + 59))
        self.assertEqual(task.task_id, memory.next_task(now + 60).task_id)

    def test_daily_report_uses_filtered_exact_tied_counts_and_nickname_fallback(self) -> None:
        memory = MemoryManager(self.db, "daily", (GROUP,), batch_size=6)
        memory.initialize(0)
        self.add("A", at(1), sender="u1", nickname="甲")
        self.add("B", at(1) + 1, sender="u2", nickname=None)
        self.add("C", at(1) + 2, sender="u1", nickname="甲")
        self.add("D", at(1) + 3, sender="u2", nickname=None)
        self.add("E", at(1) + 4, sender="u3", nickname=None)
        self.add("F", at(1) + 5, sender="u3", nickname=None)
        self.add("次日消息一", at(2), sender="u1", nickname="甲")
        self.add("次日消息二", at(2) + 1, sender="u1", nickname="甲")
        memory.ingest(at(2))
        task = memory.next_task(at(2))
        rows = self.db.execute(
            "SELECT raw_event_id,member_id FROM ai_memory_candidates WHERE account_id='daily' AND task_id=?", (task.task_id,)
        ).fetchall()
        memory.complete(task, json.dumps({
            "excluded_ids": [],
            "summary_items": [{"source_ids": [rows[0][0]], "member_id": rows[0][1],
                               "text": "当日聊天"}],
        }, ensure_ascii=False), at(2))
        task = memory.next_task(at(2))
        self.assertEqual("daily_report", task.kind)
        self.assertIn("不得输出或复述内部成员ID、群ID", task.prompt)
        self.assertIn("本机路径、密钥", task.prompt)
        self.assertIn("不要猜测所用模型或模型身份", task.prompt)
        self.assertNotIn(GROUP, task.prompt)
        self.assertNotIn('"member_id"', task.prompt)
        memory.complete(task, json.dumps({"topics": ["群友讨论了日常安排"]}, ensure_ascii=False), at(2))
        report = memory.pending_reports()[0]
        self.assertEqual(GROUP, report["target_id"])
        self.assertTrue(report["text"].startswith("【群聊小记】2026-01-01\n主要话题："))
        self.assertIn("水群王：甲、群友1、群友2（2 条）", report["text"])
        self.assertNotIn("u2", report["text"])
        self.assertNotIn("u3", report["text"])
        self.assertNotIn("wxid", report["text"])
        self.assertNotIn("@chatroom", report["text"])
        self.assertNotIn("#修仙", report["text"])
        with self.db:
            memory.mark_report_queued(report["report_id"], "request-1")
        self.assertEqual([], memory.pending_reports())

    def test_report_waits_until_source_cursor_is_caught_up(self) -> None:
        memory = MemoryManager(self.db, "caught-up", (GROUP,), batch_size=1)
        memory.initialize(0)
        self.add("第一条", at(1), sender="u1")
        self.add("第二条", at(1) + 1, sender="u2")
        memory.ingest(at(2), limit=1)
        task = memory.next_task(at(2))
        row = self.db.execute(
            "SELECT raw_event_id,member_id FROM ai_memory_candidates WHERE account_id='caught-up' AND task_id=?",
            (task.task_id,),
        ).fetchone()
        memory.complete(task, json.dumps({
            "excluded_ids": [],
            "summary_items": [{"source_ids": [row[0]], "member_id": row[1], "text": "第一条聊天"}],
        }, ensure_ascii=False), at(2))
        self.assertIsNone(memory.next_task(at(2)))
        self.assertEqual([], memory.pending_reports())

        memory.ingest(at(2))
        task = memory.next_task(at(2))
        row = self.db.execute(
            "SELECT raw_event_id,member_id FROM ai_memory_candidates WHERE account_id='caught-up' AND task_id=?",
            (task.task_id,),
        ).fetchone()
        memory.complete(task, json.dumps({
            "excluded_ids": [],
            "summary_items": [{"source_ids": [row[0]], "member_id": row[1], "text": "第二条聊天"}],
        }, ensure_ascii=False), at(2))
        self.assertEqual("daily_report", memory.next_task(at(2)).kind)

    def test_first_cursor_skips_history_and_restart_keeps_cursor(self) -> None:
        db = sqlite3.connect(":memory:")
        try:
            db.execute("""CREATE TABLE messages (
                raw_event_id INTEGER PRIMARY KEY, session_id TEXT NOT NULL,
                conversation_id TEXT, sender_id TEXT, direction TEXT NOT NULL,
                message_type INTEGER, observed_at_ms INTEGER,
                message_id_candidate TEXT, history_state TEXT NOT NULL, model_json TEXT NOT NULL)""")
            observed = int(at(1) * 1000)
            model = json.dumps({"message_kind": "text", "event_kind": "item", "source": "receive_batch",
                                "content": "old", "observed_at_ms": observed})
            db.execute("INSERT INTO messages VALUES(1,'s',?,'u','incoming',1,?,'1','live_candidate',?)",
                       (GROUP, observed, model))
            memory = MemoryManager(db, "restart", (GROUP,), batch_size=1)
            memory.initialize(0)
            self.assertEqual(0, memory.ingest(at(2)))
            db.execute("INSERT INTO messages VALUES(2,'s',?,'u','incoming',1,?,'2','live_candidate',?)",
                       (GROUP, observed + 1, model.replace('"old"', '"new"')))
            db.commit()
            self.assertEqual(1, memory.ingest(at(2)))
            restarted = MemoryManager(db, "restart", (GROUP,), batch_size=1)
            restarted.initialize(0)
            self.assertEqual(0, restarted.ingest(at(2)))
            self.assertEqual(2, restarted.status()["cursor_raw_event_id"])
        finally:
            db.close()

    def test_obvious_commands_bots_nontext_and_backlog_are_filtered_but_cursor_advances(self) -> None:
        now = at(2)
        self.add("#修仙", now)
        self.add("hello", now + 1, sender=ACCOUNT)
        self.add("image", now + 2, kind="image")
        self.add("old", now + 3, history="backlog")
        self.add("normal", now + 4, sender="u2")
        self.assertEqual(5, self.memory.ingest(now + 5))
        status = self.memory.status()
        self.assertEqual(5, status["cursor_raw_event_id"])
        self.assertEqual(1, status["groups"][0]["pending_candidates"])

    def test_duplicate_message_id_counts_once_and_context_uses_refreshed_group_nickname(self) -> None:
        memory = MemoryManager(self.db, "dedupe", (GROUP,), batch_size=1)
        memory.initialize(0)
        now = at(2)
        first = self.add("同一条微信消息", now, sender="u1", nickname="旧昵称", message_id="wechat-7")
        self.add("同一条微信消息", now + 1, sender="u1", nickname="新昵称", message_id="wechat-7")
        memory.ingest(now + 2)
        self.assertEqual(1, memory.status()["groups"][0]["pending_candidates"])
        task = memory.next_task(now + 2)
        memory.complete(task, json.dumps({
            "excluded_ids": [],
            "summary_items": [{"source_ids": [first], "member_id": "u1", "text": "普通聊天"}],
        }, ensure_ascii=False), now + 2)

        latest_raw = self.next_raw - 1
        latest_model = json.loads(self.db.execute(
            "SELECT model_json FROM messages WHERE raw_event_id=?", (latest_raw,)
        ).fetchone()[0])
        latest_model["sender_group_nickname"] = "当前昵称"
        self.db.execute("UPDATE messages SET model_json=? WHERE raw_event_id=?",
                        (json.dumps(latest_model, ensure_ascii=False), latest_raw))
        context = memory.context(GROUP)
        self.assertIn("u1=当前昵称", context)
        self.assertNotIn("旧昵称", context)

    def test_manual_summary_rejects_mixed_members_and_preserves_inputs(self) -> None:
        now = at(2)
        first = self.add("周末去公园", now, sender="u1")
        second = self.add("周末在家看书", now + 1, sender="u2")
        self.memory.ingest(now + 2)
        self.memory.request_flush("member-check", GROUP, second, now + 2)
        task = self.memory.next_task(now + 2)
        with self.assertRaisesRegex(ValueError, "member_id must match"):
            self.memory.complete(task, json.dumps({
                "excluded_ids": [],
                "summary_items": [{"source_ids": [first, second], "member_id": "u1",
                                   "text": "周末去公园和在家看书"}],
            }, ensure_ascii=False), now + 3)
        self.assertEqual(2, self.memory.status()["groups"][0]["pending_candidates"])
        self.assertEqual(0, self.db.execute("SELECT count(*) FROM ai_memory_batches").fetchone()[0])
        self.memory.complete(task, json.dumps({
            "excluded_ids": [],
            "summary_items": [
                {"source_ids": [first], "member_id": "u1", "text": "周末去公园"},
                {"source_ids": [second], "member_id": "u2", "text": "周末在家看书"},
            ],
        }, ensure_ascii=False), now + 4)
        self.assertEqual("completed", self.memory.flush_status("member-check")["state"])
        self.assertEqual(0, self.memory.status()["groups"][0]["pending_candidates"])

    def test_long_same_member_summary_keeps_source_limit_and_can_choose_representative_sources(self) -> None:
        now = at(2)
        self.add_many(21, now)
        self.memory.ingest(now + 1)
        self.memory.request_flush("source-limit", GROUP, self.next_raw - 1, now + 1)
        task = self.memory.next_task(now + 1)
        item = {"source_ids": list(range(1, 22)), "member_id": "u1", "text": "普通聊天"}
        with self.assertRaisesRegex(ValueError, "at most 20 IDs"):
            self.memory.complete(task, json.dumps({"excluded_ids": [], "summary_items": [item]}), now + 2)
        self.assertEqual(21, self.memory.status()["groups"][0]["pending_candidates"])
        item["source_ids"] = [1, 11, 21]
        self.memory.complete(task, json.dumps({"excluded_ids": [], "summary_items": [item]}), now + 3)
        self.assertEqual("completed", self.memory.flush_status("source-limit")["state"])
        self.assertEqual(21, self.memory.status()["groups"][0]["batched_messages"])

    def test_source_errors_are_distinct_and_repair_feedback_preserves_original_input(self) -> None:
        now = at(2)
        first = self.add("机器人游戏战绩", now, sender="u1")
        second = self.add("明天去公园", now + 1, sender="u1")
        self.memory.ingest(now + 2)
        self.memory.request_flush("repair-feedback", GROUP, second, now + 2)
        task = self.memory.next_task(now + 2)
        cases = [
            ([str(second)], [], "non-empty array of integers"),
            ([second, second], [], "duplicate IDs"),
            ([999999], [], "known input IDs"),
            ([first], [first], "excluded IDs"),
        ]
        for sources, excluded, reason in cases:
            with self.subTest(reason=reason):
                answer = json.dumps({"excluded_ids": excluded, "summary_items": [
                    {"source_ids": sources, "member_id": "u1", "text": "明天去公园"},
                ]}, ensure_ascii=False)
                with self.assertRaisesRegex(ValueError, reason) as captured:
                    self.memory.complete(task, answer, now + 3)
                retry_prompt = self.memory.validation_retry_prompt(task, answer, captured.exception)
                self.assertTrue(retry_prompt.startswith(task.prompt + "\n\n【记忆校验纠错】"))
                feedback = json.loads(retry_prompt.rsplit("\n", 1)[1])
                self.assertEqual(str(captured.exception), feedback["validation_error"])
                self.assertEqual(answer, feedback["rejected_response"])
                if excluded:
                    self.assertEqual([first], feedback["item_issues"][0]["excluded_source_ids"])
        self.assertEqual(2, self.memory.status()["groups"][0]["pending_candidates"])
        row = self.db.execute("SELECT prompt,answer_json,state FROM ai_memory_tasks WHERE task_id=?",
                              (task.task_id,)).fetchone()
        self.assertEqual((task.prompt, None, "running"), row)

    def test_repair_prompt_rejects_internal_failures_and_bounds_rejected_output(self) -> None:
        now = at(2)
        self.add("普通聊天", now)
        self.memory.ingest(now + 1)
        self.memory.request_flush("repair-budget", GROUP, self.next_raw - 1, now + 1)
        task = self.memory.next_task(now + 1)
        for error in (ValueError("semantic task candidates no longer match its input"),
                      ValueError("private untrusted error"), sqlite3.OperationalError("database busy")):
            self.assertIsNone(self.memory.validation_retry_prompt(task, "{}", error))
        retry_prompt = self.memory.validation_retry_prompt(
            task, "私" * 100000, ValueError("model answer must be one JSON object"),
        )
        self.assertLessEqual(len(retry_prompt.encode("utf-8")), 220 * 1024)
        feedback = json.loads(retry_prompt.rsplit("\n", 1)[1])
        self.assertNotIn("rejected_response", feedback)
        self.assertTrue(retry_prompt.startswith(task.prompt))

    def test_current_attribution_index_is_added_to_previously_queued_task(self) -> None:
        now = at(2)
        first = self.add("private-chat-one", now, sender="u1", nickname="private-nickname")
        second = self.add("private-chat-two", now + 1, sender="u2")
        third = self.add("private-chat-three", now + 2, sender="u1")
        self.memory.ingest(now + 3)
        self.memory.request_flush("legacy-task", GROUP, third, now + 3)
        task = self.memory.next_task(now + 3)
        self.db.execute("UPDATE ai_memory_tasks SET prompt='legacy prompt' WHERE task_id=?", (task.task_id,))
        self.db.commit()
        old_task = MemoryTask(task.task_id, task.kind, task.group_id, task.day,
                              "legacy prompt", task.manual_request_id)
        instructions = self.memory.instructions_for(old_task)
        source_index = json.loads(instructions.rsplit("\n", 1)[1])
        self.assertEqual({"u1": [first, third], "u2": [second]}, source_index)
        self.assertNotIn("private-chat", instructions)
        self.assertNotIn("private-nickname", instructions)
        self.assertEqual("legacy prompt", self.db.execute(
            "SELECT prompt FROM ai_memory_tasks WHERE task_id=?", (task.task_id,),
        ).fetchone()[0])

    def test_summary_cannot_cite_excluded_text_from_same_member(self) -> None:
        memory = MemoryManager(self.db, "provenance", (GROUP,), batch_size=2)
        memory.initialize(0)
        now = at(2)
        excluded = self.add("修仙机器人灵石战绩", now, sender="u1")
        retained = self.add("周末去公园", now + 1, sender="u1")
        memory.ingest(now + 2)
        task = memory.next_task(now + 2)
        with self.assertRaisesRegex(ValueError, "source_ids"):
            memory.complete(task, json.dumps({
                "excluded_ids": [excluded],
                "summary_items": [{"source_ids": [excluded], "member_id": "u1", "text": "修仙机器人灵石战绩"}],
            }, ensure_ascii=False), now + 2)
        memory.complete(task, json.dumps({
            "excluded_ids": [excluded],
            "summary_items": [{"source_ids": [retained], "member_id": "u1", "text": "周末计划去公园"}],
        }, ensure_ascii=False), now + 2)
        excluded_content = self.db.execute(
            "SELECT content FROM ai_memory_candidates WHERE account_id='provenance' AND raw_event_id=?",
            (excluded,),
        ).fetchone()[0]
        stored_prompt = self.db.execute(
            "SELECT prompt FROM ai_memory_tasks WHERE task_id=?", (task.task_id,)
        ).fetchone()[0]
        self.assertEqual("", excluded_content)
        self.assertNotIn("修仙机器人", stored_prompt)

    def test_prompt_budget_truncates_emoji_and_json_escapes_before_serializing(self) -> None:
        memory = MemoryManager(self.db, "budget", (GROUP,), batch_size=200)
        memory.initialize(0)
        now = at(2)
        content = ("😀\n\"\\" * 500) + "末尾"
        nickname = "昵称😀\n\"" * 100
        for index in range(200):
            self.add(content + str(index), now + index / 1000, sender="u1", nickname=nickname)
        memory.ingest(now + 1)
        task = memory.next_task(now + 1)
        self.assertEqual("semantic_batch", task.kind)
        self.assertLessEqual(len(task.prompt.encode("utf-8")), 220 * 1024)
        records = json.loads(task.prompt.split("输入：", 1)[1])
        self.assertEqual(200, len(records))
        self.assertTrue(all("…[截断]" in record["text"] for record in records))
        self.assertTrue(all(len(json.dumps(record["text"], ensure_ascii=False,
                                           separators=(",", ":")).encode("utf-8")) <= 600
                            for record in records))
        self.assertTrue(all(len(json.dumps(record["nickname"], ensure_ascii=False,
                                           separators=(",", ":")).encode("utf-8")) <= 120
                            for record in records))
        stored = self.db.execute(
            "SELECT content FROM ai_memory_candidates WHERE account_id='budget' ORDER BY raw_event_id LIMIT 1"
        ).fetchone()[0]
        self.assertIn("…[截断]", stored)
        self.assertLessEqual(len(json.dumps(stored, ensure_ascii=False,
                                            separators=(",", ":")).encode("utf-8")), 600)

    def test_daily_prompt_uniformly_samples_the_whole_day(self) -> None:
        memory = MemoryManager(self.db, "sample", (GROUP,), batch_size=200)
        memory.initialize(0)
        self.add_many(200, at(1))
        memory.ingest(at(2))
        task = memory.next_task(at(2))
        rows = self.db.execute(
            "SELECT raw_event_id,member_id FROM ai_memory_candidates WHERE account_id='sample' AND task_id=?",
            (task.task_id,),
        ).fetchall()
        memory.complete(task, json.dumps({
            "excluded_ids": [],
            "summary_items": [{"source_ids": [rows[0][0]], "member_id": rows[0][1],
                               "text": "全天普通聊天"}],
        }, ensure_ascii=False), at(2))
        report = memory.next_task(at(2))
        self.assertEqual("daily_report", report.kind)
        self.assertLessEqual(len(report.prompt.encode("utf-8")), 220 * 1024)
        evidence = json.loads(report.prompt.split("证据：", 1)[1])
        samples = evidence["day_samples"]
        self.assertEqual(80, len(samples))
        self.assertEqual(1, samples[0]["position"])
        self.assertEqual(200, samples[-1]["position"])
        self.assertTrue(any(90 <= item["position"] <= 110 for item in samples))
        self.assertEqual(200, evidence["validated_message_count"])


if __name__ == "__main__":
    unittest.main()
