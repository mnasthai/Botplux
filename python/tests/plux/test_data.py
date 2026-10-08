import json
import sqlite3
import tempfile
import threading
from datetime import datetime, timedelta, timezone
import unittest
from pathlib import Path

from plux.adapters.sqlite import SqliteDatabase
from plux.api.errors import ConflictError, InvalidScope, ResourceNotReady
from plux.api.models import AssetRef, Migration, StateSnapshot
from plux.data import DataManager


class DataTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.db = SqliteDatabase(root / "main.sqlite3")
        self.manager = DataManager(self.db, root / "assets", root / "staging").initialize()
        self.data = self.manager.for_plugin("example")

    def tearDown(self):
        self.db.close()
        self.temp.cleanup()

    def test_transaction_rollback_scope_and_sql_boundary(self):
        self.db.migrate("example", (Migration(1, ("CREATE TABLE example(value INTEGER)",)),))
        with self.assertRaises(RuntimeError):
            with self.db.transaction() as uow:
                uow.execute("INSERT INTO example VALUES (1)")
                with self.assertRaises(sqlite3.DatabaseError):
                    uow.execute("COMMIT")
                with self.assertRaises(sqlite3.DatabaseError):
                    uow.execute("PRAGMA foreign_keys=OFF")
                raise RuntimeError("rollback")
        with self.db.transaction() as uow:
            self.assertEqual(uow.execute("SELECT COUNT(*) FROM example").fetchone()[0], 0)
            errors = []
            thread = threading.Thread(target=lambda: errors.append(self._off_thread(uow)))
            thread.start(); thread.join()
            self.assertIsInstance(errors[0], InvalidScope)
        with self.assertRaises(InvalidScope):
            uow.execute("SELECT 1")

    @staticmethod
    def _off_thread(uow):
        try:
            uow.execute("SELECT 1")
        except Exception as exc:
            return exc
        return None

    def test_migration_failure_keeps_version_and_schema(self):
        with self.assertRaises(sqlite3.DatabaseError):
            self.db.migrate("broken", (Migration(1, ("CREATE TABLE broken(x)", "INVALID SQL")),))
        with self.db.transaction() as uow:
            self.assertEqual(uow.execute("SELECT COUNT(*) FROM plux_migrations WHERE namespace='broken'").fetchone()[0], 0)
            self.assertEqual(uow.execute("SELECT COUNT(*) FROM sqlite_master WHERE name='broken'").fetchone()[0], 0)

    def test_migration_cannot_commit_its_own_partial_work(self):
        with self.assertRaises(sqlite3.DatabaseError):
            self.db.migrate("unsafe", (Migration(1, ("CREATE TABLE unsafe(x)", "COMMIT")),))
        with self.db.transaction() as uow:
            self.assertEqual(uow.execute("SELECT COUNT(*) FROM sqlite_master WHERE name='unsafe'").fetchone()[0], 0)
    def test_catalog_failed_refresh_and_historical_version(self):
        first = self.data.catalogs.load("rules", version="v1", default={"x": [1]}, validator=lambda d: d)
        with self.assertRaises(ValueError):
            self.data.catalogs.load("rules", version="v2", default={"x": [2]},
                                    validator=lambda d: (_ for _ in ()).throw(ValueError("bad")))
        self.assertEqual(self.data.catalogs.get("rules").digest, first.digest)
        with self.assertRaises(TypeError):
            first.data["x"] = (3,)
        second = self.data.catalogs.load("rules", version="v2", default={"x": [2]})
        self.assertNotEqual(first.digest, second.digest)
        self.assertEqual(self.data.catalogs.get("rules", "v1").data["x"], (1,))

    def test_asset_snapshot_references_and_collection(self):
        ref = self.data.assets.publish(self.data.assets.stage(b"asset", max_bytes=5))
        with self.db.transaction() as uow:
            current = self.data.snapshots.put(StateSnapshot("one", 1, 0, {"turn": 1}, assets=(ref,)),
                                              expected_revision=None, uow=uow)
        self.assertEqual(current.revision, 1)
        with self.db.transaction() as uow:
            with self.assertRaises(ConflictError):
                self.data.snapshots.put(StateSnapshot("one", 1, 0, {}), expected_revision=0, uow=uow)
        self.assertEqual(self.data.maintenance.inspect()["references"], 1)
        self.assertEqual(self.data.maintenance.collect()["assets_removed"], 0)
        with self.db.transaction() as uow:
            self.data.snapshots.delete("one", expected_revision=1, uow=uow)
        self.assertEqual(self.data.maintenance.collect(older_than=datetime.now(timezone.utc) + timedelta(days=1))["assets_removed"], 1)
        with self.assertRaises(Exception):
            self.data.assets.resolve(ref)

    def test_asset_io_rejected_inside_transaction_and_cross_owner_retention(self):
        ref = self.data.assets.publish(self.data.assets.stage(b"cross-owner"))
        platform = self.manager.for_plugin("platform")
        with self.db.transaction() as uow:
            with self.assertRaises(InvalidScope):
                self.data.assets.stage(b"forbidden")
            with self.assertRaises(InvalidScope):
                self.data.assets.publish("0123")
            platform.assets.retain(ref, "outbox:cross-owner", uow)
        self.assertEqual(self.data.maintenance.inspect()["references"], 1)
        self.assertEqual(platform.assets.resolve(ref).read_bytes(), b"cross-owner")
    def test_scoped_migration_and_cross_database_uow(self):
        with self.assertRaises(InvalidScope):
            self.data.database.migrate("other", (Migration(1, ("CREATE TABLE other(x)",)),))
        ref = self.data.assets.publish(self.data.assets.stage(b"belongs-to-main"))
        other = SqliteDatabase(Path(self.temp.name) / "other.sqlite3")
        try:
            with other.transaction() as foreign:
                with self.assertRaises(InvalidScope):
                    self.data.snapshots.put(StateSnapshot("foreign", 1, 0, {}),
                                            expected_revision=None, uow=foreign)
                with self.assertRaises(InvalidScope):
                    self.data.assets.retain(ref, "foreign", foreign)
        finally:
            other.close()

    def test_restore_rejects_tampered_asset(self):
        root = Path(self.temp.name)
        ref = self.data.assets.publish(self.data.assets.stage(b"original"))
        backup = self.manager.backup(root / "backup")
        (backup / "assets" / ref.asset_id[:2] / ref.asset_id).write_bytes(b"tampered")
        with self.assertRaises(ResourceNotReady):
            DataManager.restore_backup(backup, root / "new.sqlite3", root / "new-assets")
        self.assertFalse((root / "new.sqlite3").exists())
        self.assertFalse((root / "new-assets").exists())
    def test_empty_asset_backup_restores(self):
        root = Path(self.temp.name)
        backup = self.manager.backup(root / "empty-backup")
        DataManager.restore_backup(backup, root / "empty.sqlite3", root / "empty-assets")
        self.assertTrue((root / "empty.sqlite3").is_file())
        self.assertTrue((root / "empty-assets").is_dir())
    def test_pending_staging_restores_and_can_publish(self):
        root = Path(self.temp.name)
        staging_id = self.data.assets.stage(b"pending")
        backup = self.manager.backup(root / "pending-backup")
        DataManager.restore_backup(backup, root / "pending.sqlite3",
                                   root / "pending-assets", root / "pending-staging")
        restored_db = SqliteDatabase(root / "pending.sqlite3")
        restored = DataManager(restored_db, root / "pending-assets", root / "pending-staging").initialize()
        try:
            ref = restored.for_plugin("example").assets.publish(staging_id)
            self.assertEqual(restored.for_plugin("example").assets.resolve(ref).read_bytes(), b"pending")
        finally:
            restored_db.close()
    def test_backup_restore(self):
        root = Path(self.temp.name)
        ref = self.data.assets.publish(self.data.assets.stage(b"backup"))
        self.data.catalogs.load("rules", version="v1", default={"k": 1})
        with self.db.transaction() as uow:
            self.data.assets.retain(ref, "outbox:1", uow)
        backup = self.manager.backup(root / "backup")
        DataManager.restore_backup(backup, root / "restored.sqlite3", root / "restored-assets")
        restored_db = SqliteDatabase(root / "restored.sqlite3")
        restored = DataManager(restored_db, root / "restored-assets", root / "restored-staging").initialize()
        try:
            self.assertEqual(restored.for_plugin("example").assets.resolve(ref).read_bytes(), b"backup")
            self.assertEqual(restored.for_plugin("example").catalogs.get("rules", "v1").data["k"], 1)
        finally:
            restored_db.close()


if __name__ == "__main__":
    unittest.main()









