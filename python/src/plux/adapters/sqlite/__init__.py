"""SQLite adapter with explicit thread-owned transaction scopes."""
from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from pathlib import Path
from typing import Any, Iterable, Sequence

from plux.api.errors import ConfigurationError, InvalidScope
from plux.api.models import Migration

_BLOCKED_ACTIONS = frozenset(
    getattr(sqlite3, name) for name in (
        "SQLITE_TRANSACTION", "SQLITE_SAVEPOINT", "SQLITE_ATTACH", "SQLITE_DETACH",
        "SQLITE_PRAGMA", "SQLITE_CREATE_TABLE", "SQLITE_DROP_TABLE", "SQLITE_ALTER_TABLE",
        "SQLITE_CREATE_INDEX", "SQLITE_DROP_INDEX", "SQLITE_CREATE_TRIGGER",
        "SQLITE_DROP_TRIGGER", "SQLITE_CREATE_VIEW", "SQLITE_DROP_VIEW",
    ) if hasattr(sqlite3, name)
)


class _Results:
    """Keep a live cursor usable without exposing its connection or commit method."""
    def __init__(self, cursor: sqlite3.Cursor, scope: SqliteUnitOfWork):
        self._cursor = cursor
        self._scope = scope

    def fetchone(self):
        self._scope._check()
        return self._cursor.fetchone()

    def fetchall(self):
        self._scope._check()
        return self._cursor.fetchall()

    def fetchmany(self, size: int = 1):
        self._scope._check()
        return self._cursor.fetchmany(size)

    @property
    def rowcount(self) -> int:
        self._scope._check()
        return self._cursor.rowcount

    @property
    def lastrowid(self) -> int | None:
        self._scope._check()
        return self._cursor.lastrowid

    def __iter__(self):
        while (row := self.fetchone()) is not None:
            yield row


class SqliteUnitOfWork:
    def __init__(self, database: SqliteDatabase):
        self._database = database
        self.domain = database.domain
        self.active = False
        self._thread_id: int | None = None
        self._connection: sqlite3.Connection | None = None

    def _check(self) -> None:
        if not self.active or self._thread_id != threading.get_ident() or self._database._closed:
            raise InvalidScope("transaction is inactive or belongs to another thread")
        if getattr(self._database._local, "active", None) is not self:
            raise InvalidScope("transaction no longer owns its connection")

    def __enter__(self) -> SqliteUnitOfWork:
        if self.active or self._database._closed or getattr(self._database._local, "active", None) is not None:
            raise InvalidScope("nested, reused, or closed transaction")
        connection = self._database._connection()
        self._database._control.active = True
        try:
            connection.execute("BEGIN IMMEDIATE")
        finally:
            self._database._control.active = False
        self._connection = connection
        self._thread_id = threading.get_ident()
        self._database._local.active = self
        self.active = True
        return self

    def execute(self, sql: str, parameters: Sequence[Any] = ()) -> _Results:
        self._check()
        return _Results(self._connection.execute(sql, parameters), self)

    def executemany(self, sql: str, parameters: Iterable[Sequence[Any]]) -> _Results:
        self._check()
        return _Results(self._connection.executemany(sql, parameters), self)

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self._check()
        self._database._control.active = True
        try:
            if exc_type is None:
                try:
                    self._connection.commit()
                except BaseException:
                    self._connection.rollback()
                    raise
            else:
                self._connection.rollback()
        finally:
            self._database._control.active = False
            self.active = False
            self._database._local.active = None


class SqliteDatabase:
    def __init__(self, path: str | Path, domain: str = "main"):
        self.path = Path(path).resolve()
        self.domain = domain
        self.capabilities = frozenset({"transactions", "conditional_updates", "json", "migrations"})
        self._local = threading.local()
        self._control = threading.local()
        self._connections: dict[threading.Thread, sqlite3.Connection] = {}
        self._lock = threading.RLock()
        self._closed = False
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def _authorizer(self, action, arg1, arg2, dbname, source):
        if action in _BLOCKED_ACTIONS and not getattr(self._control, "active", False):
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK

    def _connection(self) -> sqlite3.Connection:
        if self._closed:
            raise InvalidScope("database is closed")
        thread_id = threading.current_thread()
        with self._lock:
            if thread_id not in self._connections:
                connection = sqlite3.connect(self.path, isolation_level=None, timeout=30, check_same_thread=False)
                connection.row_factory = sqlite3.Row
                connection.execute("PRAGMA foreign_keys=ON")
                connection.execute("PRAGMA busy_timeout=30000")
                connection.set_authorizer(self._authorizer)
                self._connections[thread_id] = connection
            return self._connections[thread_id]

    def transaction(self) -> SqliteUnitOfWork:
        return SqliteUnitOfWork(self)

    def migrate(self, namespace: str, migrations: tuple[Migration, ...]) -> None:
        if not namespace or not migrations:
            if not namespace:
                raise ConfigurationError("migration namespace is required")
            return
        if self._closed or getattr(self._local, "active", None) is not None:
            raise InvalidScope("migration requires an idle database")
        versions = [migration.version for migration in migrations]
        if versions != list(range(1, len(versions) + 1)):
            raise ConfigurationError("migration versions must start at 1 and be contiguous")
        # A dedicated connection keeps migration DDL outside plugin transaction handles.
        with self._lock:
            connection = sqlite3.connect(self.path, isolation_level=None, timeout=30, check_same_thread=False)
            try:
                connection.execute("PRAGMA busy_timeout=30000")
                connection.execute("BEGIN IMMEDIATE")
                connection.execute("CREATE TABLE IF NOT EXISTS plux_migrations (namespace TEXT NOT NULL, version INTEGER NOT NULL, digest TEXT NOT NULL, PRIMARY KEY(namespace, version))")
                applied = {row[0]: row[1] for row in connection.execute(
                    "SELECT version, digest FROM plux_migrations WHERE namespace=?", (namespace,))}
                forbidden = frozenset(getattr(sqlite3, name) for name in (
                    "SQLITE_TRANSACTION", "SQLITE_SAVEPOINT", "SQLITE_ATTACH", "SQLITE_DETACH",
                    "SQLITE_PRAGMA") if hasattr(sqlite3, name))
                connection.set_authorizer(lambda action, a, b, db, source:
                                          sqlite3.SQLITE_DENY if action in forbidden else sqlite3.SQLITE_OK)
                if any(version > versions[-1] for version in applied):
                    raise ConfigurationError("database has a newer migration version")
                for migration in migrations:
                    digest = hashlib.sha256(json.dumps(migration.statements, ensure_ascii=False).encode()).hexdigest()
                    if migration.version in applied:
                        if applied[migration.version] != digest:
                            raise ConfigurationError("applied migration content changed")
                        continue
                    for statement in migration.statements:
                        connection.execute(statement)
                    connection.execute("INSERT INTO plux_migrations VALUES (?, ?, ?)",
                                       (namespace, migration.version, digest))
                connection.set_authorizer(None)
                connection.commit()
            except BaseException:
                connection.set_authorizer(None)
                connection.rollback()
                raise
            finally:
                connection.close()

    def close(self) -> None:
        with self._lock:
            if any(connection.in_transaction for connection in self._connections.values()):
                raise InvalidScope("cannot close database with an active transaction")
            self._closed = True
            for connection in self._connections.values():
                connection.close()
            self._connections.clear()






