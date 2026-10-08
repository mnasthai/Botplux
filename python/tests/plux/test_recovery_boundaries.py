"""Concrete restart and protocol boundary regressions discovered during integration."""
from __future__ import annotations
import ast
from dataclasses import replace
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import tempfile
import tomllib
import unittest

from plux.adapters.sqlite import SqliteDatabase
from plux.adapters.wechat_observer import ObserverAdapter
from plux.api import AssetRef, ConfigurationError, ReplyIntent
from plux.messaging import MessageStore
from plux.messaging.delivery import _digest, _json, _native_payload
from plux.runtime.lifecycle import LifecycleSupervisor
from test_messaging import PNG_BYTES, Assets, FakeAdapter, event, text_record


def _examples_root(project: Path) -> Path:
    """The examples package is either nested in or a sibling of the framework project."""
    for candidate in (project / "plugins" / "examples", project.parent / "plugins" / "examples"):
        if (candidate / "pyproject.toml").is_file():
            return candidate
    raise FileNotFoundError(f"example plugins are missing next to {project}")

class RecoveryBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.database = SqliteDatabase(self.root / "main.sqlite3")
        self.adapter = FakeAdapter()
        self.messages = MessageStore(self.database, self.adapter, Assets(),
                                     "self", {"friend", "room@chatroom"},
                                     media_root=self.root).initialize()
    def tearDown(self):
        self.database.close()
        self.directory.cleanup()
    def ingest(self, raw):
        with self.database.transaction() as uow:
            row = uow.execute("SELECT offset FROM plux_sources WHERE source='log'").fetchone()
        offset = row[0] if row else 0
        return self.messages.ingest(raw, "log", offset, offset + len(raw))

    def test_restart_recovers_discovery_behind_log_checkpoint(self):
        self.ingest(event("observer_start", target_version="4.1.13.12"))
        pipe = r"\\.\pipe\plux-fixture-s1"
        self.ingest(event("command_pipe_ready", pipe=pipe, protocol_version=1))
        adapter = ObserverAdapter(expected_account="self")
        restored = MessageStore(self.database, adapter, Assets(), "self", {"friend"}).initialize()
        self.assertEqual(restored.adapter.pipe, pipe)
        self.assertEqual(restored.adapter.connection().native_session, "s1")
        self.assertEqual(restored.adapter.connection().phase, "discovered")
        self.assertFalse(restored.adapter.connection().can_send)

    def test_late_start_of_retired_session_does_not_replace_new_session(self):
        adapter = ObserverAdapter(expected_account="self")
        for session in ("old", "new", "old"):
            adapter.observe_control({"kind": "observer_start", "session_id": session})
        self.assertEqual(adapter.connection().native_session, "new")
        self.assertEqual(adapter.connection().generation, 2)

    def test_message_services_reject_another_database_with_same_domain(self):
        other = SqliteDatabase(self.root / "other.sqlite3")
        try:
            with other.transaction() as uow:
                with self.assertRaises(ValueError):
                    self.messages.for_plugin("tool").enqueue(
                        ReplyIntent("scope", "self", "friend", "s1", text="x"), uow)
                with self.assertRaises(ValueError):
                    self.messages.complete_input("s1:1", uow)
        finally:
            other.close()

    def test_invalid_unicode_and_unbounded_sequence_are_quarantined(self):
        raw = b'{"kind":"item","schema_version":2,"session_id":"s1","seq":1,"content":"\\ud800"}\n'
        self.assertIsNone(self.ingest(raw))
        self.assertIsNone(self.ingest(event(seq=2**100)))
        with self.database.transaction() as uow:
            self.assertEqual(uow.execute("SELECT COUNT(*) FROM plux_issues").fetchone()[0], 2)
            self.assertEqual(uow.execute("SELECT COUNT(*) FROM plux_raw_events").fetchone()[0], 2)
        self.assertIsNotNone(self.ingest(text_record()))

    def test_prepared_media_keeps_native_path_and_fingerprint(self):
        class FileAssets(Assets):
            def __init__(self, path):
                self.path = path
            def resolve(self, ref):
                return self.path
        original = self.root / "original.bin"
        relocated = self.root / "relocated.bin"
        content = PNG_BYTES
        original.write_bytes(content)
        relocated.write_bytes(content)
        self.messages.assets = FileAssets(original)
        ref = AssetRef("asset", "1", "image", hashlib.sha256(content).hexdigest())
        service = self.messages.for_plugin("tool")
        with self.database.transaction() as uow:
            request = service.enqueue(ReplyIntent("prepared", "self", "friend", "s1", asset=ref), uow)
            row = uow.execute("""SELECT request_id,account,native_session,target,kind,
                created_at,expires_at,logical_json,native_json,native_fingerprint
                FROM plux_replies WHERE request_id=?""", (request.request_id,)).fetchone()
        payload = _native_payload(self.messages, row, json.loads(row[7]), "fixed-attempt")
        native = _json(payload)
        fingerprint = _digest(native)
        with self.database.transaction() as uow:
            uow.execute("UPDATE plux_replies SET native_json=?,native_fingerprint=? WHERE request_id=?",
                        (native, fingerprint, request.request_id))
        self.messages.assets = FileAssets(relocated)
        self.adapter.mode = "send_enabled"
        self.adapter.capabilities = frozenset({"send_image"})
        self.assertEqual(self.messages.dispatch_once(request.request_id).status, "accepted")
        # The native sender reads a flat <sha256>.<ext> copy, not the asset path.
        expected = (self.root / (hashlib.sha256(content).hexdigest() + ".png")).resolve()
        self.assertEqual(self.adapter.sent[0]["media_path"], str(expected))
        self.assertEqual(self.adapter.sent[0]["attempt_id"], "fixed-attempt")
        with self.database.transaction() as uow:
            self.assertEqual(uow.execute("SELECT native_fingerprint FROM plux_replies").fetchone()[0], fingerprint)

    def test_boolean_protocol_version_does_not_become_accepted(self):
        service = self.messages.for_plugin("tool")
        with self.database.transaction() as uow:
            request = service.enqueue(ReplyIntent("malformed", "self", "friend", "s1", text="x"), uow)
        self.adapter.mode = "send_enabled"
        self.adapter.capabilities = frozenset({"send_text"})
        def response(payload):
            return {"op": "send_result", "protocol_version": True, "request_id": payload["request_id"],
                    "attempt_id": payload["attempt_id"], "observer_session_id": "s1", "status": "accepted"}
        self.adapter.exchange = response
        self.assertEqual(self.messages.dispatch_once(request.request_id).status, "unknown")
        self.assertEqual(service.receipt(request.request_id).status, "unknown")

    def test_resource_close_failure_preserves_remaining_resources(self):
        lifecycle = LifecycleSupervisor()
        calls = []
        failed = [True]
        def close():
            if failed[0]:
                failed[0] = False
                raise RuntimeError("resource is still in use")
            calls.append("database")
        lifecycle.add_resource("lock", lambda: calls.append("lock"))
        lifecycle.add_resource("database", close)
        report = lifecycle.stop()
        self.assertFalse(report.resources_closed)
        self.assertEqual(calls, [])
        self.assertIn("resource:database", report.active)
        report = lifecycle.stop()
        self.assertTrue(report.resources_closed)
        self.assertEqual(calls, ["database", "lock"])

class ContentAndRuntimeBoundaryTests(unittest.TestCase):
    def test_catalog_reference_names_a_unique_content_version(self):
        from plux.api import CatalogRef, ConflictError, StateSnapshot
        from plux.data import DataManager
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            database = SqliteDatabase(root / "state.sqlite3")
            try:
                data = DataManager(database, root / "assets", root / "staging").initialize().for_plugin("test")
                rules = data.catalogs.load("rules", version="1", default={"step": 1})
                data.catalogs.load("content", version="1", default={"items": []})
                with database.transaction() as uow:
                    with self.assertRaises(ConflictError):
                        data.snapshots.put(StateSnapshot("ambiguous", 1, 0, {}, catalog=CatalogRef("test", "1")),
                                           expected_revision=None, uow=uow)
                    saved = data.snapshots.put(StateSnapshot("named", 1, 0, {}, catalog=rules.ref),
                                               expected_revision=None, uow=uow)
                    self.assertEqual(saved.catalog.name, "rules")
                    # A preloaded current catalog can be used without a nested transaction.
                    self.assertIs(data.catalogs.get("rules"), rules)
                self.assertEqual(data.snapshots.get("named").catalog, rules.ref)
            finally:
                database.close()

    def test_predicate_failure_is_recorded_and_handler_id_is_unique(self):
        from types import SimpleNamespace
        from plux.api import CommandSpec, ConflictError, EventSpec, Outcome
        from plux.runtime.registry import Registry
        from plux.runtime.executor import Executor
        from plux.adapters.wechat_observer import parse_message
        with tempfile.TemporaryDirectory() as temp:
            database = SqliteDatabase(Path(temp) / "state.sqlite3")
            try:
                registry = Registry()
                def broken(message):
                    raise RuntimeError("invalid filter")
                registry.for_plugin("test").event(EventSpec("filter", lambda message, context: Outcome.noop(), predicate=broken))
                with self.assertRaises(ConflictError):
                    registry.for_plugin("test").command(CommandSpec("filter", "/filter", lambda arg, context: Outcome.noop()))
                service = SimpleNamespace(data=SimpleNamespace(database=database), messages=None, tasks=None)
                executor = Executor(database, registry, {"test": service})
                executor.initialize()
                message = parse_message(json.loads(text_record()), account="self", session="s1", raw_ref="raw:1")
                self.assertFalse(executor.process(message))
                with database.transaction() as uow:
                    self.assertEqual(uow.execute("SELECT status FROM plux_handler_runs").fetchone()[0], "failed")
            finally:
                database.close()

class ArchitectureTests(unittest.TestCase):
    def test_api_and_plugins_keep_dependency_direction(self):
        project = Path(__file__).resolve().parents[2]
        src = project / "src" / "plux"
        for path in src.rglob("*.py"):
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"), str(path))):
                modules = ([alias.name for alias in node.names] if isinstance(node, ast.Import)
                           else [node.module or ""] if isinstance(node, ast.ImportFrom) else ())
                for module in modules:
                    self.assertFalse(module.startswith(("wechat_receiver", "plux_plugins")), str(path))
                    if "api" in path.relative_to(src).parts:
                        self.assertFalse(module.startswith(("plux.data", "plux.runtime", "plux.adapters", "plux.messaging")), str(path))
        plugins = _examples_root(project) / "src"
        for path in plugins.rglob("*.py"):
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"), str(path))):
                modules = ([alias.name for alias in node.names] if isinstance(node, ast.Import)
                           else [node.module or ""] if isinstance(node, ast.ImportFrom) else ())
                for module in modules:
                    if module.startswith("plux"):
                        self.assertEqual(module, "plux.api", str(path))

    def test_distributions_and_cli_are_declared(self):
        project = Path(__file__).resolve().parents[2]
        framework = tomllib.loads((project / "pyproject.toml").read_text(encoding="utf-8"))
        examples = tomllib.loads((_examples_root(project) / "pyproject.toml").read_text(encoding="utf-8"))
        self.assertEqual(framework["project"]["scripts"]["plux"], "plux.runtime.cli:main")
        self.assertEqual(framework["tool"]["setuptools"]["packages"]["find"]["include"], ["plux", "plux.*"])
        self.assertEqual(examples["project"]["dependencies"], ["plux-framework>=0.1,<0.2"])

if __name__ == "__main__":
    unittest.main()
