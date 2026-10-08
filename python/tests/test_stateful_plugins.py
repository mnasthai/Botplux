"""Contract tests for transactional command plugins and their lifecycle hooks."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import time
import unittest
from unittest.mock import patch

from wechat_receiver.models import Message
from wechat_receiver.outbox import Outbox
from wechat_receiver.plugins import LoadedPlugin, Reply, load_plugins, reply_request_id
from wechat_receiver.reply_router import ReplyRouter
from wechat_receiver.send_models import SendTextCommand
from wechat_receiver.store import Store


class StatefulPluginTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.database = Path(self.temporary.name) / "messages.sqlite3"
        self.store = Store(self.database)
        self.store.db.execute("CREATE TABLE wallet (player_id TEXT PRIMARY KEY, balance INTEGER NOT NULL)")
        self.store.db.execute("INSERT INTO wallet(player_id,balance) VALUES('member', 10)")
        self.store.db.commit()
        self.now = time.time()
        self.group = "123456@chatroom"
        self.config = SimpleNamespace(
            sender=SimpleNamespace(
                account_id="wxid_bot", allowed_targets=frozenset({self.group}),
            ),
            enabled_plugins=(),
            reply_ttl_seconds=60,
            max_message_age_seconds=120,
            game_config={"phase": 1},
        )

    def tearDown(self) -> None:
        self.store.close()
        self.temporary.cleanup()

    def message(self, event_key: str = "session:1") -> Message:
        return Message(
            session_id="session", event_key=event_key, seq=1, call_id=1,
            source="receive_batch", event_kind="item", observed_at_ms=int(self.now * 1000),
            message_type=1, message_kind="text", app_message_type=None, content="/cultivate",
            raw_content="/cultivate", conversation_id=self.group, sender_id="member",
            direction="incoming", message_time_candidate=int(self.now), message_id_candidate=None,
            mentioned_ids=(), mention_state="none", history_state="live_candidate",
        )

    def router(self, *plugins: LoadedPlugin) -> ReplyRouter:
        self.config.enabled_plugins = tuple(plugin.name for plugin in plugins)
        return ReplyRouter(self.store, self.config, list(plugins), started_at=self.now - 1)

    def balance(self) -> int:
        return self.store.db.execute("SELECT balance FROM wallet WHERE player_id='member'").fetchone()[0]

    def status(self, plugin: str, event_key: str = "session:1") -> str:
        return self.store.db.execute(
            "SELECT status FROM plugin_runs WHERE account_id=? AND event_key=? AND plugin=?",
            ("wxid_bot", event_key, plugin),
        ).fetchone()[0]

    def test_stateful_command_commits_wallet_marker_and_mentioned_reply_once(self) -> None:
        def parse(message):
            return "cultivate" if message.content == "/cultivate" else None

        escaped_contexts = []

        def handle(command, context):
            self.assertEqual("cultivate", command)
            self.assertEqual("wxid_bot", context.account_id)
            self.assertEqual("session", context.connection_id)
            self.assertEqual(self.message(), context.message)
            self.assertEqual(self.group, context.conversation_id)
            self.assertEqual("member", context.user_id)
            self.assertEqual("session:1", context.event_key)
            self.assertEqual({"phase": 1}, context.game_config)
            self.assertEqual(frozenset({self.group}), context.allowed_targets)
            context.store.execute("UPDATE wallet SET balance=balance+5 WHERE player_id=?", (context.user_id,))
            escaped_contexts.append(context)
            return Reply("灵气增长", mention_ids=(context.user_id,), expires_in=15)

        plugin = LoadedPlugin("cultivate", None, parse_command=parse, handle_command=handle)
        router = self.router(plugin)

        self.assertEqual(1, router.handle(self.message(), "session", now=self.now))
        self.assertEqual(15, self.balance())
        self.assertEqual("queued", self.status("cultivate"))
        request_id = self.store.db.execute("SELECT request_id FROM outbox").fetchone()[0]
        command = json.loads(Outbox(self.store.db).get(request_id)["command_json"])
        self.assertEqual(self.group, command["target_id"])
        self.assertEqual("灵气增长", command["text"])
        self.assertEqual("member", command["at_user_list"])
        self.assertEqual("game", command["origin"])
        created = datetime.fromisoformat(command["created_at"].replace("Z", "+00:00"))
        expires = datetime.fromisoformat(command["expires_at"].replace("Z", "+00:00"))
        self.assertEqual(15, (expires - created).total_seconds())
        with self.assertRaisesRegex(RuntimeError, "only valid during its transaction"):
            escaped_contexts[0].store.execute("SELECT 1")

        self.assertEqual(0, router.handle(self.message(), "session", now=self.now))
        self.assertEqual(15, self.balance())
        self.assertEqual(1, self.store.db.execute("SELECT count(*) FROM outbox").fetchone()[0])

    def test_stateful_failures_roll_back_changes_and_do_not_block_other_plugins(self) -> None:
        def parse(_message):
            return "go"

        def crashing(_command, context):
            context.store.execute("UPDATE wallet SET balance=balance+100 WHERE player_id='member'")
            raise RuntimeError("boom")

        def invalid_reply(_command, context):
            context.store.execute("UPDATE wallet SET balance=balance+20 WHERE player_id='member'")
            return [Reply("first"), Reply("bad\x00reply")]

        def succeeds(_command, context):
            context.store.execute("UPDATE wallet SET balance=balance+1 WHERE player_id='member'")
            return "done"

        broken = LoadedPlugin("crashing", None, parse_command=parse, handle_command=crashing)
        invalid = LoadedPlugin("invalid", None, parse_command=parse, handle_command=invalid_reply)
        good = LoadedPlugin("good", None, parse_command=parse, handle_command=succeeds)
        router = self.router(broken, invalid, good)

        with self.assertLogs(level="ERROR"):
            self.assertEqual(1, router.handle(self.message(), "session", now=self.now))
        self.assertEqual(11, self.balance())
        self.assertEqual("failed", self.status("crashing"))
        self.assertEqual("failed", self.status("invalid"))
        self.assertEqual("queued", self.status("good"))
        self.assertEqual(("done",), tuple(self.store.db.execute("SELECT text FROM outbox").fetchone()))

    def test_context_cannot_commit_and_bad_reply_target_roll_back_the_transaction(self) -> None:
        def parse(_message):
            return "go"

        def attempts_commit(_command, context):
            context.store.execute("UPDATE wallet SET balance=balance+10 WHERE player_id='member'")
            context.store.execute("COMMIT")
            return "unreachable"

        def changes_target(_command, context):
            context.store.execute("UPDATE wallet SET balance=balance+20 WHERE player_id='member'")
            return Reply("wrong place", target_id="another@chatroom")

        commit = LoadedPlugin("commit", None, parse_command=parse, handle_command=attempts_commit)
        target = LoadedPlugin("target", None, parse_command=parse, handle_command=changes_target)
        router = self.router(commit, target)

        with self.assertLogs(level="ERROR"):
            self.assertEqual(0, router.handle(self.message(), "session", now=self.now))
        self.assertEqual(10, self.balance())
        self.assertEqual("failed", self.status("commit"))
        self.assertEqual("failed", self.status("target"))
        self.assertEqual(0, self.store.db.execute("SELECT count(*) FROM outbox").fetchone()[0])

    def test_enqueue_failure_rolls_back_state_and_a_retry_applies_it_once(self) -> None:
        def parse(_message):
            return "go"

        def handle(_command, context):
            context.store.execute("UPDATE wallet SET balance=balance+7 WHERE player_id='member'")
            return Reply("one"), Reply("two")

        plugin = LoadedPlugin("retry", None, parse_command=parse, handle_command=handle)
        router = self.router(plugin)
        enqueue = router.outbox.enqueue_in_transaction
        calls = 0

        def fail_second(command):
            nonlocal calls
            calls += 1
            enqueue(command)
            if calls == 2:
                raise RuntimeError("enqueue interrupted")

        with patch.object(router.outbox, "enqueue_in_transaction", side_effect=fail_second):
            with self.assertRaisesRegex(RuntimeError, "enqueue interrupted"):
                router.handle(self.message(), "session", now=self.now)
        self.assertEqual(10, self.balance())
        self.assertEqual(0, self.store.db.execute("SELECT count(*) FROM plugin_runs").fetchone()[0])
        self.assertEqual(0, self.store.db.execute("SELECT count(*) FROM outbox").fetchone()[0])

        self.assertEqual(2, router.handle(self.message(), "session", now=self.now))
        self.assertEqual(17, self.balance())
        self.assertEqual(2, self.store.db.execute("SELECT count(*) FROM outbox").fetchone()[0])

    def test_parser_and_legacy_handler_run_outside_write_transaction(self) -> None:
        seen: list[str] = []

        def parse(_message):
            self.assertFalse(self.store.db.in_transaction)
            seen.append("parse")
            return "go"

        def stateful(_command, context):
            self.assertTrue(self.store.db.in_transaction)
            seen.append("stateful")
            context.store.execute("UPDATE wallet SET balance=balance+1 WHERE player_id='member'")
            return None

        def legacy(_message):
            self.assertFalse(self.store.db.in_transaction)
            seen.append("legacy")
            return None

        router = self.router(
            LoadedPlugin("stateful", None, parse_command=parse, handle_command=stateful),
            LoadedPlugin("legacy", legacy),
        )
        self.assertEqual(0, router.handle(self.message(), "session", now=self.now))
        self.assertEqual(["parse", "stateful", "legacy"], seen)
        self.assertEqual(11, self.balance())

    def test_hooks_are_transactional_once_per_router_and_require_stable_whitelisted_replies(self) -> None:
        starts: list[str | None] = []
        polls: list[str | None] = []

        def on_start(context):
            self.assertTrue(self.store.db.in_transaction)
            starts.append(context.connection_id)
            context.store.execute("UPDATE wallet SET balance=balance+2 WHERE player_id='member'")
            return Reply("started", target_id=self.group, request_key="game-start")

        def on_poll(context):
            self.assertTrue(self.store.db.in_transaction)
            polls.append(context.connection_id)
            context.store.execute("UPDATE wallet SET balance=balance+1 WHERE player_id='member'")
            return Reply("tick", target_id=self.group, request_key="game-tick")

        plugin = LoadedPlugin("hooks", None, parse_command=lambda _message: None,
                              handle_command=lambda _command, _context: None,
                              on_start=on_start, on_poll=on_poll)
        router = self.router(plugin)
        self.assertEqual(1, router.start("session", now=self.now))
        self.assertEqual(0, router.start("session", now=self.now))
        self.assertEqual(1, router.tick("session", now=self.now))
        self.assertEqual(0, router.tick("session", now=self.now))
        self.assertEqual(["session"], starts)
        self.assertEqual(["session", "session"], polls)
        self.assertEqual(14, self.balance())
        self.assertEqual(2, self.store.db.execute("SELECT count(*) FROM outbox").fetchone()[0])

    def test_hook_reply_rejects_missing_connection_or_non_whitelisted_target(self) -> None:
        def start_reply(_context):
            return Reply("start", target_id=self.group, request_key="start-key")

        plugin = LoadedPlugin("no-session", None, parse_command=lambda _message: None,
                              handle_command=lambda _command, _context: None, on_start=start_reply)
        router = self.router(plugin)
        with self.assertRaises((ValueError, RuntimeError)):
            router.start(None, now=self.now)
        self.assertEqual(0, self.store.db.execute("SELECT count(*) FROM outbox").fetchone()[0])

        def bad_target(_context):
            return Reply("bad", target_id="other@chatroom", request_key="bad-key")

        rejected = LoadedPlugin("bad-target", None, parse_command=lambda _message: None,
                                handle_command=lambda _command, _context: None, on_start=bad_target)
        rejected_router = self.router(rejected)
        with self.assertRaises((ValueError, RuntimeError)):
            rejected_router.start("session", now=self.now)
        self.assertEqual(0, self.store.db.execute("SELECT count(*) FROM outbox").fetchone()[0])

    def test_hook_reusing_a_request_key_with_changed_content_rolls_back(self) -> None:
        poll_number = 0

        def on_poll(context):
            nonlocal poll_number
            poll_number += 1
            context.store.execute("UPDATE wallet SET balance=balance+3 WHERE player_id='member'")
            return Reply(f"poll {poll_number}", target_id=self.group, request_key="one-poll")

        plugin = LoadedPlugin("unstable-hook", None, parse_command=lambda _message: None,
                              handle_command=lambda _command, _context: None, on_poll=on_poll)
        router = self.router(plugin)
        self.assertEqual(1, router.tick("session", now=self.now))
        with self.assertRaises((ValueError, RuntimeError)):
            router.tick("session", now=self.now)
        self.assertEqual(13, self.balance())
        self.assertEqual(("poll 1",), tuple(self.store.db.execute("SELECT text FROM outbox").fetchone()))

    def test_context_reply_request_id_matches_the_queued_reply_and_can_be_saved_in_state(self) -> None:
        self.store.db.execute("CREATE TABLE planned_reply (request_id TEXT NOT NULL)")
        self.store.db.commit()
        key = "phase-prompt"

        def parse(_message):
            return "plan"

        def handle(_command, context):
            request_id = context.reply_request_id(key)
            self.assertEqual(reply_request_id("wxid_bot", "planner", key), request_id)
            context.store.execute("INSERT INTO planned_reply(request_id) VALUES(?)", (request_id,))
            return Reply("计划提示", request_key=key)

        router = self.router(LoadedPlugin("planner", None, parse_command=parse, handle_command=handle))
        self.assertEqual(1, router.handle(self.message(), "session", now=self.now))
        stored = self.store.db.execute("SELECT request_id FROM planned_reply").fetchone()[0]
        queued = self.store.db.execute("SELECT request_id FROM outbox").fetchone()[0]
        self.assertEqual(reply_request_id("wxid_bot", "planner", key), stored)
        self.assertEqual(stored, queued)

    def test_before_messages_observes_accepted_outbox_before_commands_and_tick_handles_timeout_afterward(self) -> None:
        self.store.db.execute("CREATE TABLE phase_state (state TEXT NOT NULL)")
        self.store.db.execute("INSERT INTO phase_state(state) VALUES('waiting_for_prompt')")
        self.store.db.commit()
        accepted_id = "accepted-prompt"
        created = datetime.fromtimestamp(self.now, timezone.utc)
        Outbox(self.store.db).enqueue(SendTextCommand(
            accepted_id, "wxid_bot", "session", self.group, "earlier prompt", created,
            created + timedelta(seconds=60), origin="game",
        ))
        self.store.db.execute("UPDATE outbox SET status='accepted' WHERE request_id=?", (accepted_id,))
        self.store.db.commit()
        order: list[str] = []

        def before(context):
            self.assertTrue(self.store.db.in_transaction)
            status = context.store.execute(
                "SELECT status FROM outbox WHERE request_id=?", (accepted_id,)
            ).fetchone()[0]
            self.assertEqual("accepted", status)
            context.store.execute("UPDATE phase_state SET state='prompt_accepted'")
            order.append("before")

        def parse(_message):
            return "act"

        def handle(_command, context):
            state = context.store.execute("SELECT state FROM phase_state").fetchone()[0]
            self.assertEqual("prompt_accepted", state)
            order.append("command")

        def on_poll(context):
            self.assertTrue(self.store.db.in_transaction)
            state = context.store.execute("SELECT state FROM phase_state").fetchone()[0]
            self.assertEqual("prompt_accepted", state)
            context.store.execute("UPDATE phase_state SET state='timeout_checked'")
            order.append("poll")

        plugin = LoadedPlugin("phase", None, parse_command=parse, handle_command=handle,
                              on_before_messages=before, on_poll=on_poll)
        router = self.router(plugin)
        self.assertEqual(0, router.before_messages("session", now=self.now))
        self.assertEqual(("prompt_accepted",), tuple(self.store.db.execute("SELECT state FROM phase_state").fetchone()))
        self.assertEqual(0, router.handle(self.message(), "session", now=self.now))
        self.assertEqual(("prompt_accepted",), tuple(self.store.db.execute("SELECT state FROM phase_state").fetchone()))
        self.assertEqual(0, router.tick("session", now=self.now + 1))
        self.assertEqual(["before", "command", "poll"], order)
        self.assertEqual(("timeout_checked",), tuple(self.store.db.execute("SELECT state FROM phase_state").fetchone()))

    def test_runtime_issue_reaches_hooks_and_same_batch_command_then_clears(self) -> None:
        seen: list[tuple[str, str | None]] = []

        def before(context):
            seen.append(("before", context.runtime_issue))

        def handle(_command, context):
            seen.append(("command", context.runtime_issue))

        def on_poll(context):
            seen.append(("poll", context.runtime_issue))

        plugin = LoadedPlugin("health", None, parse_command=lambda _message: "act",
                              handle_command=handle, on_before_messages=before,
                              on_poll=on_poll)
        router = self.router(plugin)
        router.before_messages("session", now=self.now,
                               runtime_issue="receiver_log_unread:12_bytes")
        router.handle(self.message(), "session", now=self.now)
        router.tick("session", now=self.now)
        router.before_messages("session", now=self.now + 1, runtime_issue=None)
        self.assertEqual([
            ("before", "receiver_log_unread:12_bytes"),
            ("command", "receiver_log_unread:12_bytes"),
            ("poll", "receiver_log_unread:12_bytes"),
            ("before", None),
        ], seen)

    def test_loader_accepts_complete_stateful_protocol_and_rejects_ambiguous_forms(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "stateful.py").write_text(
                "NAME = 'stateful'\n"
                "def parse_command(message): return 'go'\n"
                "def handle_command(command, context): return 'done'\n",
                encoding="utf-8",
            )
            loaded = load_plugins(root, ("stateful",))[0]
            self.assertTrue(callable(loaded.parse_command))
            self.assertTrue(callable(loaded.handle_command))
            self.assertEqual("go", loaded.parse_command(self.message()))

            (root / "half.py").write_text(
                "NAME = 'half'\ndef parse_command(message): return 'go'\n", encoding="utf-8"
            )
            with self.assertRaises((TypeError, ValueError)):
                load_plugins(root, ("half",))

            (root / "mixed.py").write_text(
                "NAME = 'mixed'\n"
                "def on_message(message): return None\n"
                "def parse_command(message): return 'go'\n"
                "def handle_command(command, context): return None\n",
                encoding="utf-8",
            )
            with self.assertRaises((TypeError, ValueError)):
                load_plugins(root, ("mixed",))


if __name__ == "__main__":
    unittest.main()
