"""Conditional, persistent state snapshots."""
from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime
from typing import Any

from plux.api.errors import ConflictError, InvalidScope
from plux.api.models import AssetRef, CatalogRef, StateSnapshot, require_utc
from plux.data.catalogs import validate_reference


class SnapshotStore:
    def __init__(self, database, namespace: str, assets):
        self._database = database
        self._namespace = namespace
        self._assets = assets

    def _check(self, uow) -> None:
        if not getattr(uow, "active", False) or uow.domain != self._database.domain or getattr(uow, "_database", None) is not self._database:
            raise InvalidScope("snapshot requires active transaction in its database domain")

    def get(self, key: str, uow=None) -> StateSnapshot | None:
        if uow is None:
            with self._database.transaction() as scope:
                return self.get(key, scope)
        self._check(uow)
        row = uow.execute("SELECT * FROM plux_snapshots WHERE namespace=? AND key=?",
                          (self._namespace, key)).fetchone()
        if row is None:
            return None
        catalog = CatalogRef(*json.loads(row["catalog_json"])) if row["catalog_json"] else None
        assets = tuple(AssetRef(**item) for item in json.loads(row["assets_json"]))
        deadline = datetime.fromisoformat(row["deadline"]) if row["deadline"] else None
        return StateSnapshot(key, row["structure_version"], row["revision"],
                             json.loads(row["data_json"]), deadline, catalog, assets,
                             row["durability"])

    def put(self, snapshot: StateSnapshot, *, expected_revision: int | None, uow) -> StateSnapshot:
        self._check(uow)
        if not snapshot.key or snapshot.structure_version < 1 or snapshot.durability != "persistent":
            raise ValueError("snapshot needs key, positive structure version and persistent durability")
        deadline = require_utc(snapshot.deadline).isoformat() if snapshot.deadline else None
        data = json.dumps(snapshot.data, ensure_ascii=False, sort_keys=True, allow_nan=False)
        catalog = json.dumps((snapshot.catalog.namespace, snapshot.catalog.version, snapshot.catalog.name)) if snapshot.catalog else None
        if snapshot.catalog is not None:
            validate_reference(snapshot.catalog, uow)
        assets = json.dumps([vars(ref) for ref in snapshot.assets], sort_keys=True)
        old = self.get(snapshot.key, uow)
        if expected_revision is None:
            if old is not None:
                raise ConflictError("snapshot already exists")
            revision = 1
            uow.execute("INSERT INTO plux_snapshots VALUES(?,?,?,?,?,?,?,?,?)",
                        (self._namespace, snapshot.key, snapshot.structure_version, revision,
                         data, deadline, catalog, assets, snapshot.durability))
        else:
            if old is None or old.revision != expected_revision:
                raise ConflictError("snapshot revision changed")
            revision = old.revision + 1
            result = uow.execute("UPDATE plux_snapshots SET structure_version=?, revision=?, data_json=?, "
                                 "deadline=?, catalog_json=?, assets_json=?, durability=? "
                                 "WHERE namespace=? AND key=? AND revision=?",
                                 (snapshot.structure_version, revision, data, deadline, catalog,
                                  assets, snapshot.durability, self._namespace, snapshot.key,
                                  expected_revision))
            if result.rowcount != 1:
                raise ConflictError("snapshot revision changed")
        reference = f"snapshot:{self._namespace}:{snapshot.key}"
        old_assets = set(old.assets if old else ())
        new_assets = set(snapshot.assets)
        for ref in old_assets - new_assets:
            self._assets.release(ref, reference, uow)
        for ref in new_assets - old_assets:
            self._assets.retain(ref, reference, uow)
        return replace(snapshot, revision=revision)

    def delete(self, key: str, *, expected_revision: int, uow) -> None:
        self._check(uow)
        old = self.get(key, uow)
        if old is None or old.revision != expected_revision:
            raise ConflictError("snapshot revision changed")
        result = uow.execute("DELETE FROM plux_snapshots WHERE namespace=? AND key=? AND revision=?",
                             (self._namespace, key, expected_revision))
        if result.rowcount != 1:
            raise ConflictError("snapshot revision changed")
        reference = f"snapshot:{self._namespace}:{key}"
        for ref in old.assets:
            self._assets.release(ref, reference, uow)


