"""Integration contracts through the public API and independent plugin package."""
from __future__ import annotations
from dataclasses import replace
from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile
import unittest

from plux.api import (CommandSpec, ConfigurationError, Migration, Outcome, Plugin,
                      PluginManifest, ReplyIntent)
from plux.runtime.bootstrap import Application, validate_plugins
from plux.runtime.cli import main
from plux.runtime.config import (ObserverConfig, PluginEntry, PolicyConfig,
                                 RuntimeConfig, RuntimePaths, load_config)
from plux_plugins.examples import calculate

class BrokenReplyPlugin(Plugin):
    manifest = PluginManifest("broken", migrations=(Migration(1,
        ("CREATE TABLE broken_state(value INTEGER)",)),))
    def register(self, registry):
        registry.command(CommandSpec("fail", "/fail", self.fail, mode="atomic"))
    def fail(self, argument, context):
        context.uow.execute("INSERT INTO broken_state(value) VALUES (1)")
        return Outcome.success(replies=(ReplyIntent(
            context.event_key, context.account, context.conversation, "s1",
            text="rollback", ttl_seconds=601),))

def configuration(root: Path, *, examples=True, entries=None, enabled=True):
    paths = RuntimePaths(root, root / "db.sqlite3", root / "observer-4.1.13.12.jsonl",
                         root / "inbound", root / "outbound", root / "staging", root / "cache")
    if entries is None:
        entries = tuple(PluginEntry(f"plux_plugins.examples:{name}") for name in
                        ("HelpPlugin", "CalculatorPlugin", "CounterPlugin")) if examples else ()
    return RuntimeConfig(root / "plux.toml", paths,
                         PolicyConfig("bot", ("friend",), allow_unknown_history=True),
                         ObserverConfig(enabled=enabled), entries)

def ingest(application, command, *, seq=1, source="fixture", offset=0):
    raw = json.dumps({
        "kind": "item", "schema_version": 2, "session_id": "s1", "seq": seq,
        "call_id": seq, "index": 0, "observed_unix_ms": int(datetime.now(timezone.utc).timestamp()*1000),
        "from": "friend", "to": "bot", "content": command, "msg_type": 1,
        "content_read": {"status": "ok"}, "from_read": {"status": "ok"},
        "to_read": {"status": "ok"}, "source_read": {"status": "ok"},
        "msg_source": "<msgsource></msgsource>",
    }, ensure_ascii=False).encode("utf-8") + b"\n"
    return application.messages.ingest(raw, source, offset, offset + len(raw))

class ApplicationTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.apps = []

    def tearDown(self):
        for app in reversed(self.apps):
            app.stop()
        self.directory.cleanup()

    def app(self, config):
        app = Application(config).start(background=False)
        self.apps.append(app)
        return app

    def test_no_plugins_no_native_starts_and_stops(self):
        app = self.app(configuration(self.root, examples=False, enabled=False))
        self.assertEqual(app.step()["processed"], 0)
        self.assertFalse(app.observer.connection().can_send)
        report = app.stop()
        self.assertTrue(report.stopped)
        self.assertTrue(report.resources_closed)

    def test_atomic_reply_crash_gap_and_restart_are_idempotent(self):
        config = configuration(self.root)
        app = self.app(config)
        message = ingest(app, "/count")
        self.assertIsNotNone(message)
        self.assertTrue(app.executor.process(message))  # crash before inbox acknowledgement
        self.assertEqual(len(app.messages.pending_inputs()), 1)
        app.stop()
        restarted = self.app(config)
        self.assertEqual(restarted.step()["processed"], 1)
        with restarted.database.transaction() as uow:
            self.assertEqual(uow.execute("SELECT value FROM example_counter").fetchone()[0], 1)
            self.assertEqual(uow.execute("SELECT COUNT(*) FROM plux_replies").fetchone()[0], 1)
        self.assertEqual(restarted.messages.pending_inputs(), ())

    def test_background_counter_result_and_reply_commit_once(self):
        app = self.app(configuration(self.root))
        ingest(app, "/count-later")
        report = app.step()
        self.assertTrue(report["task_worked"])
        self.assertFalse(app.tasks.tick())
        with app.database.transaction() as uow:
            self.assertEqual(uow.execute("SELECT value FROM example_counter").fetchone()[0], 1)
            self.assertEqual(uow.execute("SELECT status FROM plux_tasks").fetchone()[0], "committed")
            self.assertEqual(uow.execute("SELECT COUNT(*) FROM plux_replies").fetchone()[0], 1)

    def test_reply_validation_failure_rolls_back_business_effect(self):
        entry = PluginEntry(f"{__name__}:BrokenReplyPlugin")
        app = self.app(configuration(self.root, entries=(entry,)))
        message = ingest(app, "/fail")
        self.assertFalse(app.executor.process(message, app.messages))
        with app.database.transaction() as uow:
            self.assertEqual(uow.execute("SELECT COUNT(*) FROM broken_state").fetchone()[0], 0)
            self.assertEqual(uow.execute("SELECT COUNT(*) FROM plux_replies").fetchone()[0], 0)
            self.assertEqual(uow.execute("SELECT status FROM plux_handler_runs").fetchone()[0], "failed")

    def test_disabled_plugin_preserves_pending_tasks_without_running(self):
        config = configuration(self.root)
        app = self.app(config)
        message = ingest(app, "/count-later")
        self.assertTrue(app.executor.process(message, app.messages))
        app.stop()
        empty = self.app(replace(config, plugins=()))
        self.assertFalse(empty.tasks.tick())
        with empty.database.transaction() as uow:
            self.assertEqual(uow.execute("SELECT status FROM plux_tasks").fetchone()[0], "pending")

    def test_background_worker_starts_after_application_becomes_running(self):
        app = Application(configuration(self.root, examples=False, enabled=False)).start(background=True)
        self.apps.append(app)
        app.cancellation.wait(0.05)
        self.assertEqual(app.health()["errors"], ())
        self.assertTrue(app.health()["workers"]["runtime"])
        self.assertTrue(app.stop().stopped)

    def test_process_lock_prevents_two_runtime_owners(self):
        config = configuration(self.root, examples=False, enabled=False)
        self.app(config)
        with self.assertRaises(ConfigurationError):
            Application(config).prepare()

    def test_metadata_check_creates_no_database(self):
        config = configuration(self.root)
        loaded = validate_plugins(config)
        self.assertEqual(len(loaded), 3)
        self.assertFalse(config.paths.database.exists())

    def test_calculator_rejects_code_and_unbounded_arithmetic(self):
        self.assertEqual(calculate("1 + 2 * 3"), 7)
        for expression in ("__import__('os')", "2**1000000000", "[1]*10000", "1/0", "1e309"):
            with self.assertRaises((ValueError, SyntaxError, ArithmeticError)):
                calculate(expression)

class ConfigurationTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
    def tearDown(self):
        self.directory.cleanup()
    def write(self, text):
        path = self.root / "配置.toml"
        path.write_text(text, encoding="utf-8")
        return path

    def test_paths_follow_configuration_and_env_does_not_mutate(self):
        path = self.write("[paths]\nruntime_root='local-data'\ndatabase='db/状态.sqlite3'\n")
        config = load_config(path)
        self.assertEqual(config.paths.database, self.root / "db" / "状态.sqlite3")
        self.assertEqual(config.paths.runtime_root, self.root / "local-data")
        self.assertEqual(config.native_environment()["WECHATBOT_NATIVE_SEND"], "0")
        self.assertFalse(config.paths.database.exists())

    def test_configuration_rejects_ambiguous_paths_and_typo(self):
        texts = ("[paths]\nruntime_root='C:relative'\n",
                 "[paths]\ndatabase='nul'\n",
                 "[runtime]\nbatch_size=true\n",
                 "[observer]\nsend_enabled=true\n",
                 "[policy]\nallowed_targets=['friend','friend']\n",
                 "[observer]\ntarget_version='unknown'\n",
                 "[paths]\ndatabse='oops'\n",
                 "[paths]\noutbound='same'\nstaging='same'\n")
        for text in texts:
            with self.subTest(text=text), self.assertRaises(ConfigurationError):
                load_config(self.write(text))

    def test_custom_observer_log_is_rejected_for_native_export(self):
        config = load_config(self.write("[paths]\nobserver_log='custom.jsonl'\n"))
        with self.assertRaises(ConfigurationError):
            config.native_environment()

if __name__ == "__main__":
    unittest.main()
