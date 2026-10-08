from __future__ import annotations

import tempfile
from threading import Event
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from plux.adapters.sqlite import SqliteDatabase
from plux.api import (CommandSpec, ContentQuality, EventSpec, MessageIdentity,
                      Migration, Outcome, Plugin, PluginManifest, ReplyIntent, TaskIntent,
                      TaskSpec, TextMessage)
from plux.runtime.executor import Executor
from plux.runtime.loader import Loader
from plux.runtime.lifecycle import LifecycleSupervisor
from plux.runtime.registry import Registry
from plux.runtime.tasks import TaskManager


class _Messages:
    def __init__(self, database):
        self.database = database
        database.migrate("test.replies", (Migration(1, ("CREATE TABLE replies(reply_key TEXT PRIMARY KEY, text TEXT)",)),))

    def enqueue(self, intent, uow):
        uow.execute("INSERT OR IGNORE INTO replies VALUES(?,?)", (intent.reply_key, intent.text))


class BadPlugin(Plugin):
    manifest = PluginManifest("bad")
    def register(self, registry):
        self.services.messages.enqueue(None, None)


class GoodPlugin(Plugin):
    manifest = PluginManifest("gated")
    def register(self, registry):
        pass

class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.database = SqliteDatabase(Path(self.temp.name) / "runtime.db")
        self.registry = Registry()
        self.services = {}
        self.messages = _Messages(self.database)

    def tearDown(self):
        self.database.close()
        self.temp.cleanup()

    def service(self, plugin_id, tasks=None):
        value = SimpleNamespace(messages=self.messages,
                                data=SimpleNamespace(database=self.database),
                                tasks=tasks)
        self.services[plugin_id] = value
        return value

    def message(self, key="event-1"):
        return TextMessage(event_key=key, identity=MessageIdentity("bot", "room", "alice"),
                           observed_at=datetime.now(timezone.utc), text="/go",
                           quality=ContentQuality(history_status="realtime"))

    def test_atomic_rollback_retry_and_event_subscription(self):
        self.service("one")
        self.service("two")
        self.database.migrate("test.business", (Migration(1, ("CREATE TABLE business(value INTEGER)",)),))
        attempts = []
        def command(arg, call):
            call.uow.execute("INSERT INTO business VALUES(1)")
            attempts.append(1)
            if len(attempts) == 1:
                raise RuntimeError("fail once")
            return Outcome.success(replies=(ReplyIntent("reply", "bot", "room", None, text="ok"),))
        event_calls = []
        self.registry.for_plugin("one").command(CommandSpec("go", "/go", command, mode="atomic"))
        self.registry.for_plugin("two").event(EventSpec("observe", lambda msg, ctx: (event_calls.append(msg.event_key), Outcome.noop())[1]))
        executor = Executor(self.database, self.registry, self.services)
        executor.initialize()
        self.assertFalse(executor.process(self.message()))
        self.assertTrue(executor.process(self.message()))
        self.assertTrue(executor.process(self.message()))
        with self.database.transaction() as uow:
            self.assertEqual(uow.execute("SELECT COUNT(*) FROM business").fetchone()[0], 1)
            self.assertEqual(uow.execute("SELECT COUNT(*) FROM replies").fetchone()[0], 1)
        self.assertEqual(event_calls, ["event-1"])

    def test_nonidempotent_recovery_without_result_is_uncertain(self):
        self.service("one")
        calls = []
        self.registry.for_plugin("one").task(TaskSpec("external", work=lambda payload, ctx: calls.append(payload),
                                                      commit=lambda result, ctx: Outcome.noop(), idempotent=False))
        manager = TaskManager(self.database, self.registry, self.services)
        self.services["one"].tasks = manager.for_plugin("one")
        manager.initialize()
        with self.database.transaction() as uow:
            manager.for_plugin("one").enqueue(TaskIntent("job", "external", {"n": 1}), uow)
            uow.execute("UPDATE plux_tasks SET status='working',lease_until='2000-01-01T00:00:00+00:00' WHERE task_key='job'")
        manager.recover()
        self.assertEqual(manager.get("one", "job")["status"], "uncertain")
        self.assertFalse(manager.tick())
        self.assertEqual(calls, [])

    def test_saved_result_retries_commit_without_repeating_work(self):
        self.service("one")
        work_calls = []
        commit_calls = []
        def work(payload, ctx):
            self.assertIsNone(ctx.uow)
            work_calls.append(payload)
            return {"rendered": payload["value"]}
        def commit(result, ctx):
            commit_calls.append(result)
            if len(commit_calls) == 1:
                raise RuntimeError("temporary commit failure")
            return Outcome.noop()
        self.registry.for_plugin("one").task(TaskSpec("render", work=work, commit=commit,
                                                     idempotent=False, max_attempts=1))
        manager = TaskManager(self.database, self.registry, self.services)
        self.services["one"].tasks = manager.for_plugin("one")
        manager.initialize()
        with self.database.transaction() as uow:
            manager.for_plugin("one").enqueue(TaskIntent("job", "render", {"value": 7}), uow)
        self.assertTrue(manager.tick())
        self.assertEqual(manager.get("one", "job")["status"], "result_ready")
        self.assertTrue(manager.tick())
        self.assertEqual(manager.get("one", "job")["status"], "committed")
        self.assertEqual(work_calls, [{"value": 7}])
        self.assertEqual(commit_calls, [{"rendered": 7}, {"rendered": 7}])

    def test_recover_callback_must_return_verified_result(self):
        self.service("one")
        callbacks = []
        self.registry.for_plugin("one").task(TaskSpec("external", work=lambda payload, ctx: None,
                                                      commit=lambda result, ctx: Outcome.noop(),
                                                      recover=lambda payload, ctx: (callbacks.append(payload), {"found": True})[1]))
        manager = TaskManager(self.database, self.registry, self.services)
        self.services["one"].tasks = manager.for_plugin("one")
        manager.initialize()
        with self.database.transaction() as uow:
            manager.for_plugin("one").enqueue(TaskIntent("job", "external", {"key": "x"}), uow)
            uow.execute("UPDATE plux_tasks SET status='working',lease_until='2000-01-01T00:00:00+00:00' WHERE task_key='job'")
        self.assertEqual(manager.recover(), 1)
        self.assertEqual(manager.get("one", "job")["status"], "result_ready")
        self.assertEqual(callbacks, [{"key": "x"}])
        self.assertTrue(manager.tick())
        self.assertEqual(manager.get("one", "job")["status"], "committed")
    def test_disabled_plugin_tasks_are_preserved_without_progress(self):
        self.service("one")
        self.registry.for_plugin("one").task(TaskSpec("old", lambda payload, ctx: payload,
                                                     lambda result, ctx: Outcome.noop()))
        manager = TaskManager(self.database, self.registry, self.services)
        self.services["one"].tasks = manager.for_plugin("one")
        manager.initialize()
        with self.database.transaction() as uow:
            manager.for_plugin("one").enqueue(TaskIntent("job", "old", {"x": 1}), uow)
        disabled = TaskManager(self.database, Registry(), {})
        self.assertEqual(disabled.recover(), 0)
        self.assertFalse(disabled.tick())
        self.assertEqual(manager.get("one", "job")["status"], "pending")

    def test_shutdown_timeout_retains_resources(self):
        release = Event()
        closed = []
        supervisor = LifecycleSupervisor()
        supervisor.add_worker("blocked", lambda stopping: release.wait())
        supervisor.add_resource("database", lambda: closed.append(True))
        supervisor.start()
        first = supervisor.stop(0.01)
        self.assertFalse(first.stopped)
        self.assertFalse(first.resources_closed)
        release.set()
        second = supervisor.stop(1.0)
        self.assertTrue(second.stopped)
        self.assertEqual(closed, [True])
    def test_loader_blocks_constructor_io_until_activation(self):
        loader = Loader(self.registry, lambda manifest, config: self.service(manifest.plugin_id))
        with self.assertRaises(Exception):
            loader.load([{"entrypoint": f"{__name__}:BadPlugin"}])
        loader = Loader(Registry(), lambda manifest, config: self.service(manifest.plugin_id))
        loaded = loader.load([{"entrypoint": f"{__name__}:GoodPlugin"}])
        self.assertEqual(loaded[0].manifest.plugin_id, "gated")
        loader.activate()
