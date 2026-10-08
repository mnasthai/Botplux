"""Bounded staging and immutable published assets."""
from __future__ import annotations

import hashlib
import os
import shutil
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from plux.api.errors import InvalidScope, ResourceMissing, ResourceNotReady
from plux.api.models import AssetRef


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class AssetStore:
    def __init__(self, database, asset_root: str | Path, staging_root: str | Path,
                 namespace: str, maintenance_lock):
        self._database = database
        self._asset_root = Path(asset_root).resolve()
        self._staging_root = Path(staging_root).resolve()
        self._namespace = namespace
        self._lock = maintenance_lock
        self._asset_root.mkdir(parents=True, exist_ok=True)
        self._staging_root.mkdir(parents=True, exist_ok=True)

    def stage(self, source: Any, *, kind: str = "file", max_bytes: int = 20 * 1024 * 1024) -> str:
        if getattr(self._database._local, "active", None) is not None:
            raise InvalidScope("asset staging must run outside a write transaction")
        if not kind or max_bytes < 1:
            raise ValueError("kind and positive max_bytes are required")
        staging_id = uuid.uuid4().hex
        target = self._staging_root / staging_id
        digest = hashlib.sha256()
        length = 0
        own_stream = isinstance(source, (str, Path))
        stream = Path(source).open("rb") if own_stream else source
        try:
            with target.open("xb") as output:
                if isinstance(source, (bytes, bytearray, memoryview)):
                    chunks = (bytes(source),)
                else:
                    chunks = iter(lambda: stream.read(1024 * 1024), b"")
                for chunk in chunks:
                    if not isinstance(chunk, bytes):
                        raise TypeError("asset source must yield bytes")
                    length += len(chunk)
                    if length > max_bytes:
                        raise ValueError("asset exceeds max_bytes")
                    digest.update(chunk)
                    output.write(chunk)
                output.flush()
                os.fsync(output.fileno())
            with self._database.transaction() as uow:
                uow.execute("INSERT INTO plux_staging(id,owner,kind,sha256,size,created_at,published_asset_id) "
                            "VALUES(?,?,?,?,?,?,NULL)",
                            (staging_id, self._namespace, kind, digest.hexdigest(), length, _now()))
            return staging_id
        except BaseException:
            target.unlink(missing_ok=True)
            raise
        finally:
            if own_stream:
                stream.close()

    def publish(self, staging_id: str) -> AssetRef:
        if getattr(self._database._local, "active", None) is not None:
            raise InvalidScope("asset publication must run outside a write transaction")
        if not staging_id or not staging_id.isalnum():
            raise ValueError("invalid staging ID")
        with self._lock:
            with self._database.transaction() as uow:
                row = uow.execute("SELECT * FROM plux_staging WHERE id=? AND owner=?",
                                  (staging_id, self._namespace)).fetchone()
            if row is None:
                raise ResourceMissing("staging item missing")
            asset_id = row["published_asset_id"] or staging_id
            ref = AssetRef(asset_id, "1", row["kind"], row["sha256"])
            if row["published_asset_id"]:
                return ref
            source = self._staging_root / staging_id
            target = self._asset_root / asset_id[:2] / asset_id
            target.parent.mkdir(parents=True, exist_ok=True)
            if source.exists():
                if source.stat().st_size != row["size"] or _hash_file(source) != row["sha256"]:
                    raise ResourceNotReady("staging content changed")
                os.replace(source, target)
            elif not target.exists():
                raise ResourceNotReady("staging content missing")
            if target.stat().st_size != row["size"] or _hash_file(target) != row["sha256"]:
                raise ResourceNotReady("published content changed")
            # The deterministic target makes interruption before this short DB commit retryable.
            with self._database.transaction() as uow:
                current = uow.execute("SELECT published_asset_id FROM plux_staging WHERE id=? AND owner=?",
                                      (staging_id, self._namespace)).fetchone()
                if current is None:
                    raise ResourceMissing("staging item missing")
                if current["published_asset_id"]:
                    return ref
                uow.execute("INSERT INTO plux_assets(id,owner,version,kind,sha256,size,path,created_at) "
                            "VALUES(?,?,?,?,?,?,?,?)",
                            (asset_id, self._namespace, "1", row["kind"], row["sha256"],
                             row["size"], str(target.relative_to(self._asset_root)), _now()))
                uow.execute("UPDATE plux_staging SET published_asset_id=? WHERE id=?",
                            (asset_id, staging_id))
            return ref
    def _row(self, ref: AssetRef, uow):
        row = uow.execute("SELECT * FROM plux_assets WHERE id=? AND version=? AND kind=? AND sha256=?",
                          (ref.asset_id, ref.version, ref.kind, ref.sha256)).fetchone()
        if row is None:
            raise ResourceMissing("asset reference missing")
        return row

    def resolve(self, ref: AssetRef) -> Path:
        with self._database.transaction() as uow:
            row = self._row(ref, uow)
            if row["owner"] != self._namespace and self._namespace != "platform":
                raise InvalidScope("asset belongs to another namespace")
            path = self._asset_root / row["path"]
            size, digest = row["size"], row["sha256"]
        if not path.is_file() or path.stat().st_size != size or _hash_file(path) != digest:
            raise ResourceNotReady("asset content unavailable or changed")
        return path

    def retain(self, ref: AssetRef, reference: str, uow) -> None:
        # A platform message/outbox can retain an asset owned by a plugin.
        self._check_scope(uow, reference)
        self._row(ref, uow)
        uow.execute("INSERT OR IGNORE INTO plux_asset_refs(asset_id,reference) VALUES(?,?)",
                    (ref.asset_id, f"{self._namespace}:{reference}"))

    def release(self, ref: AssetRef, reference: str, uow) -> None:
        self._check_scope(uow, reference)
        self._row(ref, uow)
        uow.execute("DELETE FROM plux_asset_refs WHERE asset_id=? AND reference=?",
                    (ref.asset_id, f"{self._namespace}:{reference}"))

    def _check_scope(self, uow, reference: str) -> None:
        if not getattr(uow, "active", False) or uow.domain != self._database.domain or getattr(uow, "_database", None) is not self._database:
            raise InvalidScope("asset reference requires active transaction in its database domain")
        if not reference:
            raise ValueError("asset reference key is required")


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()






