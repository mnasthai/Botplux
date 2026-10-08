"""Platform data composition, bounded maintenance, and consistent backups."""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from plux.api.errors import ConfigurationError, InvalidScope, ResourceNotReady
from plux.api.services import DataServices
from plux.api.models import Migration
from plux.data.assets import AssetStore, _hash_file
from plux.data.catalogs import CatalogStore
from plux.data.snapshots import SnapshotStore

_SCHEMA = (
    "CREATE TABLE IF NOT EXISTS plux_staging(id TEXT PRIMARY KEY, owner TEXT NOT NULL, kind TEXT NOT NULL, sha256 TEXT NOT NULL, size INTEGER NOT NULL, created_at TEXT NOT NULL, published_asset_id TEXT)",
    "CREATE INDEX IF NOT EXISTS plux_staging_created ON plux_staging(created_at)",
    "CREATE TABLE IF NOT EXISTS plux_assets(id TEXT PRIMARY KEY, owner TEXT NOT NULL, version TEXT NOT NULL, kind TEXT NOT NULL, sha256 TEXT NOT NULL, size INTEGER NOT NULL, path TEXT NOT NULL, created_at TEXT NOT NULL)",
    "CREATE INDEX IF NOT EXISTS plux_assets_created ON plux_assets(created_at)",
    "CREATE TABLE IF NOT EXISTS plux_asset_refs(asset_id TEXT NOT NULL REFERENCES plux_assets(id), reference TEXT NOT NULL, PRIMARY KEY(asset_id,reference))",
    "CREATE TABLE IF NOT EXISTS plux_catalogs(namespace TEXT NOT NULL, name TEXT NOT NULL, version TEXT NOT NULL, format_version INTEGER NOT NULL, digest TEXT NOT NULL, data_json TEXT NOT NULL, sources_json TEXT NOT NULL, PRIMARY KEY(namespace,name,version))",
    "CREATE TABLE IF NOT EXISTS plux_catalog_latest(namespace TEXT NOT NULL, name TEXT NOT NULL, version TEXT NOT NULL, PRIMARY KEY(namespace,name), FOREIGN KEY(namespace,name,version) REFERENCES plux_catalogs(namespace,name,version))",
    "CREATE TABLE IF NOT EXISTS plux_snapshots(namespace TEXT NOT NULL, key TEXT NOT NULL, structure_version INTEGER NOT NULL, revision INTEGER NOT NULL, data_json TEXT NOT NULL, deadline TEXT, catalog_json TEXT, assets_json TEXT NOT NULL, durability TEXT NOT NULL, PRIMARY KEY(namespace,key))",
    "CREATE INDEX IF NOT EXISTS plux_snapshots_deadline ON plux_snapshots(deadline)",
)


def _verify_backup_database(path: Path, manifest_assets: list[dict[str, Any]] | None = None,
                            manifest_staging: list[dict[str, Any]] | None = None) -> None:
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    try:
        if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise ResourceNotReady("backup database integrity check failed")
        if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
            raise ResourceNotReady("backup database foreign key check failed")
        for row in connection.execute("SELECT digest,data_json,format_version FROM plux_catalogs"):
            if row["format_version"] < 1 or hashlib.sha256(row["data_json"].encode("utf-8")).hexdigest() != row["digest"]:
                raise ResourceNotReady("backup catalog digest mismatch")
        assets = {row["id"]: row for row in connection.execute(
            "SELECT id,owner,version,kind,sha256,size,path FROM plux_assets")}
        for row in assets.values():
            rel = Path(row["path"])
            if rel.is_absolute() or ".." in rel.parts:
                raise ResourceNotReady("backup asset path is invalid")
        if manifest_assets is not None:
            listed = {item["id"]: item for item in manifest_assets}
            if len(listed) != len(manifest_assets) or set(listed) != set(assets):
                raise ResourceNotReady("backup manifest does not match asset metadata")
            for asset_id, row in assets.items():
                if any(listed[asset_id][field] != row[field] for field in ("path", "sha256", "size")):
                    raise ResourceNotReady("backup manifest asset metadata mismatch")
        pending = {row["id"]: row for row in connection.execute(
            "SELECT id,sha256,size FROM plux_staging WHERE published_asset_id IS NULL")}
        if manifest_staging is not None:
            listed_staging = {item["id"]: item for item in manifest_staging}
            if len(listed_staging) != len(manifest_staging) or set(listed_staging) != set(pending):
                raise ResourceNotReady("backup manifest does not match staging metadata")
            for staging_id, row in pending.items():
                if any(listed_staging[staging_id][field] != row[field] for field in ("sha256", "size")):
                    raise ResourceNotReady("backup staging metadata mismatch")
        for row in connection.execute("SELECT asset_id FROM plux_asset_refs"):
            if row["asset_id"] not in assets:
                raise ResourceNotReady("backup contains dangling asset reference")
        catalog_versions = {(row[0], row[1], row[2]) for row in connection.execute(
            "SELECT namespace,version,name FROM plux_catalogs")}
        for row in connection.execute("SELECT catalog_json,assets_json FROM plux_snapshots"):
            if row["catalog_json"]:
                reference = json.loads(row["catalog_json"])
                namespace, version = reference[:2]
                name = reference[2] if len(reference) > 2 else None
                matching = [item for item in catalog_versions if item[:2] == (namespace, version)
                            and (name is None or item[2] == name)]
                if len(matching) != 1:
                    raise ResourceNotReady("backup contains a missing or ambiguous catalog version")
            for ref in json.loads(row["assets_json"]):
                asset = assets.get(ref["asset_id"])
                if asset is None or any(ref[field] != asset[field] for field in ("version", "kind", "sha256")):
                    raise ResourceNotReady("backup contains missing snapshot asset")
    finally:
        connection.close()

class _DatabaseView:
    def __init__(self, database, namespace: str):
        self._database = database
        self._namespace = namespace
        self.domain = database.domain
        self.capabilities = database.capabilities

    def transaction(self):
        return self._database.transaction()

    def migrate(self, namespace, migrations):
        if namespace != self._namespace:
            raise InvalidScope("migration namespace does not match data service owner")
        return self._database.migrate(namespace, migrations)


class MaintenanceStore:
    def __init__(self, manager: DataManager, namespace: str):
        self._manager = manager
        self._namespace = namespace

    def inspect(self) -> dict[str, Any]:
        with self._manager.database.transaction() as uow:
            owner_filter = "" if self._namespace == "platform" else " WHERE owner=?"
            params = () if self._namespace == "platform" else (self._namespace,)
            assets = uow.execute("SELECT COUNT(*) AS count, COALESCE(SUM(size),0) AS bytes FROM plux_assets" + owner_filter, params).fetchone()
            staged = uow.execute("SELECT COUNT(*) AS count, COALESCE(SUM(size),0) AS bytes FROM plux_staging" + owner_filter, params).fetchone()
            if self._namespace == "platform":
                retained = uow.execute("SELECT COUNT(*) AS count FROM plux_asset_refs").fetchone()["count"]
            else:
                retained = uow.execute("SELECT COUNT(*) AS count FROM plux_asset_refs r JOIN plux_assets a ON a.id=r.asset_id WHERE a.owner=?", params).fetchone()["count"]
            snapshots = uow.execute("SELECT COUNT(*) AS count FROM plux_snapshots WHERE namespace=?", (self._namespace,)).fetchone()["count"]
        return {"assets": assets["count"], "asset_bytes": assets["bytes"],
                "staged": staged["count"], "staged_bytes": staged["bytes"],
                "references": retained, "snapshots": snapshots}

    def collect(self, *, limit: int = 100, older_than: datetime | None = None) -> dict[str, Any]:
        if limit < 1:
            raise ValueError("limit must be positive")
        cutoff = older_than or (datetime.now(timezone.utc) - timedelta(days=1))
        if cutoff.tzinfo is None or cutoff.utcoffset() is None:
            raise ValueError("older_than must include timezone")
        cutoff_iso = cutoff.astimezone(timezone.utc).isoformat()
        stage_paths: list[Path] = []
        asset_paths: list[Path] = []
        with self._manager._maintenance_lock:
            with self._manager.database.transaction() as uow:
                owner_clause = "" if self._namespace == "platform" else " AND owner=?"
                params = (cutoff_iso,) if self._namespace == "platform" else (cutoff_iso, self._namespace)
                rows = uow.execute("SELECT id FROM plux_staging WHERE created_at<?" + owner_clause + " ORDER BY created_at,id LIMIT ?", (*params, limit)).fetchall()
                for row in rows:
                    stage_paths.append(self._manager.staging_root / row["id"])
                    uow.execute("DELETE FROM plux_staging WHERE id=?", (row["id"],))
                remaining = limit - len(stage_paths)
                if remaining:
                    where = "" if self._namespace == "platform" else " AND a.owner=?"
                    rows = uow.execute("SELECT a.id,a.path FROM plux_assets a WHERE a.created_at<?" + where +
                                       " AND NOT EXISTS(SELECT 1 FROM plux_asset_refs r WHERE r.asset_id=a.id) "
                                       "ORDER BY a.created_at,a.id LIMIT ?", (*params, remaining)).fetchall()
                    for row in rows:
                        asset_paths.append(self._manager.asset_root / row["path"])
                        uow.execute("DELETE FROM plux_assets WHERE id=?", (row["id"],))
            # Metadata commits first: a failed commit must never leave a referenced file missing.
            failed = 0
            for path in stage_paths + asset_paths:
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    failed += 1
        return {"staged_removed": len(stage_paths), "assets_removed": len(asset_paths),
                "files_pending_removal": failed}


class DataManager:
    def __init__(self, database, asset_root: str | Path, staging_root: str | Path):
        self.database = database
        self.asset_root = Path(asset_root).resolve()
        self.staging_root = Path(staging_root).resolve()
        self._maintenance_lock = threading.RLock()
        self._initialized = False
        self._views: dict[str, DataServices] = {}

    def initialize(self) -> DataManager:
        self.asset_root.mkdir(parents=True, exist_ok=True)
        self.staging_root.mkdir(parents=True, exist_ok=True)
        self.database.migrate('plux.data', (Migration(1, _SCHEMA),))
        self._initialized = True
        return self

    def for_plugin(self, namespace: str) -> DataServices:
        if not self._initialized:
            raise InvalidScope("data manager must be initialized")
        if not namespace or ":" in namespace or "/" in namespace or "\\" in namespace:
            raise ConfigurationError("invalid namespace")
        if namespace not in self._views:
            assets = AssetStore(self.database, self.asset_root, self.staging_root,
                                namespace, self._maintenance_lock)
            self._views[namespace] = DataServices(
                _DatabaseView(self.database, namespace), CatalogStore(self.database, namespace),
                assets, SnapshotStore(self.database, namespace, assets),
                MaintenanceStore(self, namespace))
        return self._views[namespace]

    def backup(self, destination: str | Path) -> Path:
        """Create a verified, self-contained DB and asset backup in a new directory."""
        if getattr(self.database._local, "active", None) is not None:
            raise InvalidScope("backup must run outside a write transaction")
        destination = Path(destination).resolve()
        if destination.exists():
            raise FileExistsError(destination)
        temporary = destination.with_name(destination.name + ".tmp-" + os.urandom(4).hex())
        with self._maintenance_lock:
            temporary.mkdir(parents=True)
            try:
                backup_db = temporary / "data.sqlite3"
                source = sqlite3.connect(self.database.path, timeout=30)
                target = sqlite3.connect(backup_db)
                try:
                    source.backup(target)
                finally:
                    target.close()
                    source.close()
                _verify_backup_database(backup_db)
                listing = []
                snapshot = sqlite3.connect(backup_db)
                try:
                    for asset_id, path, digest, size in snapshot.execute("SELECT id,path,sha256,size FROM plux_assets"):
                        source_path = self.asset_root / path
                        if not source_path.is_file() or source_path.stat().st_size != size or _hash_file(source_path) != digest:
                            raise ResourceNotReady(f"asset {asset_id} missing or changed during backup")
                        target_path = temporary / "assets" / path
                        target_path.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copyfile(source_path, target_path)
                        if _hash_file(target_path) != digest:
                            raise ResourceNotReady("backup asset copy failed verification")
                        listing.append({"id": asset_id, "path": path, "sha256": digest, "size": size})
                    staged_listing = []
                    for staging_id, digest, size in snapshot.execute(
                            "SELECT id,sha256,size FROM plux_staging WHERE published_asset_id IS NULL"):
                        source_path = self.staging_root / staging_id
                        if not source_path.is_file() or source_path.stat().st_size != size or _hash_file(source_path) != digest:
                            raise ResourceNotReady(f"staging item {staging_id} missing or changed during backup")
                        target_path = temporary / "staging" / staging_id
                        target_path.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copyfile(source_path, target_path)
                        if _hash_file(target_path) != digest:
                            raise ResourceNotReady("backup staging copy failed verification")
                        staged_listing.append({"id": staging_id, "sha256": digest, "size": size})
                finally:
                    snapshot.close()
                manifest = {"format": 1, "database_sha256": _hash_file(backup_db),
                            "assets": listing, "staging": staged_listing}
                (temporary / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False,
                                                                      sort_keys=True), encoding="utf-8")
                temporary.rename(destination)
            except BaseException:
                shutil.rmtree(temporary, ignore_errors=True)
                raise
        return destination

    @staticmethod
    def restore_backup(source: str | Path, database_path: str | Path, asset_root: str | Path,
                       staging_root: str | Path | None = None) -> None:
        """Validate and restore to previously absent paths; never overwrite live data."""
        source = Path(source).resolve()
        database_path = Path(database_path).resolve()
        asset_root = Path(asset_root).resolve()
        staging_root = (Path(staging_root) if staging_root is not None else
                        asset_root.with_name(asset_root.name + "-staging")).resolve()
        if len({database_path, asset_root, staging_root}) != 3:
            raise ValueError("restore destinations must be distinct")
        if any(path.exists() for path in (database_path, asset_root, staging_root)):
            raise FileExistsError("restore destinations must be absent")
        manifest = json.loads((source / "manifest.json").read_text(encoding="utf-8"))
        backup_db = source / "data.sqlite3"
        if manifest.get("format") != 1 or _hash_file(backup_db) != manifest.get("database_sha256"):
            raise ResourceNotReady("backup database checksum mismatch")
        _verify_backup_database(backup_db, manifest.get("assets", []), manifest.get("staging", []))
        checked_assets = []
        for item in manifest["assets"]:
            rel = Path(item["path"])
            if rel.is_absolute() or ".." in rel.parts:
                raise ResourceNotReady("invalid asset path in backup")
            path = source / "assets" / rel
            if not path.is_file() or path.stat().st_size != item["size"] or _hash_file(path) != item["sha256"]:
                raise ResourceNotReady("backup asset mismatch")
            checked_assets.append((path, rel))
        checked_staging = []
        for item in manifest["staging"]:
            staging_id = item["id"]
            if not staging_id.isalnum():
                raise ResourceNotReady("invalid staging ID in backup")
            path = source / "staging" / staging_id
            if not path.is_file() or path.stat().st_size != item["size"] or _hash_file(path) != item["sha256"]:
                raise ResourceNotReady("backup staging mismatch")
            checked_staging.append((path, staging_id))
        database_path.parent.mkdir(parents=True, exist_ok=True)
        temporary_db = database_path.with_name(database_path.name + ".restore-tmp")
        temporary_assets = asset_root.with_name(asset_root.name + ".restore-tmp")
        temporary_staging = staging_root.with_name(staging_root.name + ".restore-tmp")
        if any(path.exists() for path in (temporary_db, temporary_assets, temporary_staging)):
            raise FileExistsError("restore temporary destination exists")
        try:
            shutil.copyfile(backup_db, temporary_db)
            temporary_assets.mkdir(parents=True)
            temporary_staging.mkdir(parents=True)
            for path, rel in checked_assets:
                target = temporary_assets / rel
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(path, target)
            for path, staging_id in checked_staging:
                shutil.copyfile(path, temporary_staging / staging_id)
            if _hash_file(temporary_db) != manifest["database_sha256"]:
                raise ResourceNotReady("restored database copy changed")
            temporary_assets.rename(asset_root)
            temporary_staging.rename(staging_root)
            temporary_db.rename(database_path)
        except BaseException:
            temporary_db.unlink(missing_ok=True)
            for path in (temporary_assets, temporary_staging):
                if path.exists():
                    shutil.rmtree(path)
            if not database_path.exists():
                for path in (asset_root, staging_root):
                    if path.exists():
                        shutil.rmtree(path)
            raise

