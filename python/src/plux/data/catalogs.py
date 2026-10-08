"""Validated, immutable, versioned content snapshots."""
from __future__ import annotations

import hashlib
import json
import tomllib
from collections.abc import Mapping
from pathlib import Path
from types import MappingProxyType
from typing import Any, Callable

from plux.api.errors import ConfigurationError, ConflictError, ResourceMissing
from plux.api.models import CatalogRef, CatalogSnapshot


def _freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        if not all(isinstance(key, str) for key in value):
            raise ConfigurationError("catalog keys must be strings")
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    if value is None or isinstance(value, (str, bool, int, float)):
        return value
    raise ConfigurationError(f"unsupported catalog value: {type(value).__name__}")


def _plain(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_plain(item) for item in value]
    return value


def _merge(default: Any, override: Any) -> Any:
    if isinstance(default, Mapping) and isinstance(override, Mapping):
        result = dict(default)
        for key, value in override.items():
            result[key] = _merge(result[key], value) if key in result else value
        return result
    return override


def _read(path: Path) -> Any:
    try:
        if path.suffix.lower() == ".toml":
            with path.open("rb") as stream:
                return tomllib.load(stream)
        if path.suffix.lower() == ".json":
            return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ConfigurationError(f"invalid catalog source {path}: {exc}") from exc
    raise ConfigurationError("catalog source must be JSON or TOML")


def validate_reference(ref: CatalogRef, uow) -> None:
    if ref.name is not None:
        rows = uow.execute("SELECT name FROM plux_catalogs WHERE namespace=? AND version=? AND name=?",
                           (ref.namespace, ref.version, ref.name)).fetchall()
    else:
        rows = uow.execute("SELECT name FROM plux_catalogs WHERE namespace=? AND version=? LIMIT 2",
                           (ref.namespace, ref.version)).fetchall()
    if not rows:
        raise ResourceMissing("catalog reference has not been loaded")
    if len(rows) != 1:
        raise ConflictError("catalog reference needs its name to identify a unique version")


class CatalogStore:
    def __init__(self, database, namespace: str):
        self._database = database
        self._namespace = namespace
        self._cache: dict[tuple[str, str], CatalogSnapshot] = {}
        self._latest: dict[str, str] = {}

    def load(self, name: str, *, version: str, default: Mapping[str, Any] | Path,
             override: Path | None = None, validator: Callable[[Any], Any] | None = None,
             format_version: int = 1) -> CatalogSnapshot:
        if not name or not version or format_version < 1:
            raise ConfigurationError("catalog name, version and positive format version are required")
        sources = []
        if isinstance(default, Mapping):
            data = _plain(default)
            sources.append("inline-default")
        else:
            path = Path(default).resolve()
            data = _read(path)
            sources.append(str(path))
        if override is not None:
            path = Path(override).resolve()
            data = _merge(data, _read(path))
            sources.append(str(path))
        # Fully validate before changing either memory or persisted latest pointer.
        if validator is not None:
            validated = validator(data)
            if validated is not None:
                data = validated
        frozen = _freeze(data)
        canonical = json.dumps(_plain(frozen), ensure_ascii=False, sort_keys=True,
                               separators=(",", ":"), allow_nan=False)
        digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        snapshot = CatalogSnapshot(CatalogRef(self._namespace, version, name), format_version,
                                   digest, frozen, tuple(sources))
        key = (name, version)
        with self._database.transaction() as uow:
            row = uow.execute("SELECT digest, format_version FROM plux_catalogs WHERE namespace=? AND name=? AND version=?",
                              (self._namespace, name, version)).fetchone()
            if row is not None and (row["digest"] != digest or row["format_version"] != format_version):
                raise ConflictError("catalog version already has different content")
            if row is None:
                uow.execute("INSERT INTO plux_catalogs VALUES (?, ?, ?, ?, ?, ?, ?)",
                            (self._namespace, name, version, format_version, digest, canonical,
                             json.dumps(sources, ensure_ascii=False)))
            uow.execute("INSERT INTO plux_catalog_latest(namespace,name,version) VALUES(?,?,?) "
                        "ON CONFLICT(namespace,name) DO UPDATE SET version=excluded.version",
                        (self._namespace, name, version))
        self._cache[key] = snapshot
        self._latest[name] = version
        return snapshot

    def get(self, name: str, version: str | None = None) -> CatalogSnapshot:
        if version is None and name in self._latest:
            version = self._latest[name]
        if version is None:
            with self._database.transaction() as uow:
                row = uow.execute("SELECT version FROM plux_catalog_latest WHERE namespace=? AND name=?",
                                  (self._namespace, name)).fetchone()
            if row is None:
                raise ResourceMissing("catalog not loaded")
            version = row["version"]
            self._latest[name] = version
        key = (name, version)
        if key not in self._cache:
            with self._database.transaction() as uow:
                row = uow.execute("SELECT format_version,digest,data_json,sources_json FROM plux_catalogs "
                                  "WHERE namespace=? AND name=? AND version=?",
                                  (self._namespace, name, version)).fetchone()
            if row is None:
                raise ResourceMissing("catalog version missing")
            self._cache[key] = CatalogSnapshot(CatalogRef(self._namespace, version, name),
                                               row["format_version"], row["digest"],
                                               _freeze(json.loads(row["data_json"])),
                                               tuple(json.loads(row["sources_json"])))
        return self._cache[key]



