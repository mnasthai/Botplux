"""Typed ports; concrete implementations remain outside the public API."""
from __future__ import annotations
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from threading import Event
from typing import Any, Callable, Iterable, Mapping, Protocol, Sequence
from .models import (AssetRef, BaseMessage, CatalogSnapshot, ConnectionSnapshot,
                     HistoryQuery, MemberRef, MemberSnapshot, Migration, Page,
                     Receipt, ReplyIntent, RequestRef, StateSnapshot, TaskIntent)

class UnitOfWork(Protocol):
    domain: str
    active: bool
    def execute(self, sql: str, parameters: Sequence[Any] = ()) -> Any: ...
    def executemany(self, sql: str, parameters: Iterable[Sequence[Any]]) -> Any: ...
    def __enter__(self) -> UnitOfWork: ...
    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None: ...

class DatabaseServices(Protocol):
    domain: str
    capabilities: frozenset[str]
    def transaction(self) -> UnitOfWork: ...
    def migrate(self, namespace: str, migrations: tuple[Migration, ...]) -> None: ...

class CatalogServices(Protocol):
    def load(self, name: str, *, version: str, default: Mapping[str, Any] | Path,
             override: Path | None = None, validator: Callable[[Any], Any] | None = None,
             format_version: int = 1) -> CatalogSnapshot: ...
    def get(self, name: str, version: str | None = None) -> CatalogSnapshot: ...

class AssetServices(Protocol):
    def stage(self, source: Any, *, kind: str = "file", max_bytes: int = 20 * 1024 * 1024) -> str: ...
    def publish(self, staging_id: str) -> AssetRef: ...
    def resolve(self, ref: AssetRef) -> Path: ...
    def retain(self, ref: AssetRef, reference: str, uow: UnitOfWork) -> None: ...
    def release(self, ref: AssetRef, reference: str, uow: UnitOfWork) -> None: ...

class SnapshotServices(Protocol):
    def get(self, key: str, uow: UnitOfWork | None = None) -> StateSnapshot | None: ...
    def put(self, snapshot: StateSnapshot, *, expected_revision: int | None,
            uow: UnitOfWork) -> StateSnapshot: ...
    def delete(self, key: str, *, expected_revision: int, uow: UnitOfWork) -> None: ...

class MaintenanceServices(Protocol):
    def inspect(self) -> Mapping[str, Any]: ...
    def collect(self, *, limit: int = 100, older_than: datetime | None = None) -> Mapping[str, Any]: ...

@dataclass(frozen=True)
class DataServices:
    database: DatabaseServices
    catalogs: CatalogServices
    assets: AssetServices
    snapshots: SnapshotServices
    maintenance: MaintenanceServices
    def repository_factory(self, factory: Callable[[UnitOfWork], Any]) -> RepositoryFactory:
        return RepositoryFactory(factory, self.database.domain)

@dataclass(frozen=True)
class RepositoryFactory:
    factory: Callable[[UnitOfWork], Any]
    domain: str
    def bind(self, uow: UnitOfWork | None) -> Any:
        from .errors import InvalidScope
        if uow is None or not uow.active or uow.domain != self.domain:
            raise InvalidScope("repository requires an active transaction in its database domain")
        return self.factory(uow)

class MessageServices(Protocol):
    def get(self, event_key: str, uow: UnitOfWork | None = None) -> BaseMessage | None: ...
    def history(self, query: HistoryQuery, uow: UnitOfWork | None = None) -> Page: ...
    def member(self, ref: MemberRef, uow: UnitOfWork | None = None) -> MemberSnapshot: ...
    def receipt(self, request_id: str, uow: UnitOfWork | None = None) -> Receipt: ...
    def input_status(self, uow: UnitOfWork | None = None, *, exclude_event_key: str | None = None) -> Mapping[str, Any]: ...
    def reply_reference(self, reply_key: str, uow: UnitOfWork | None = None) -> RequestRef | None: ...
    def connection(self) -> ConnectionSnapshot: ...
    def media(self, media_key: str, uow: UnitOfWork | None = None) -> Mapping[str, Any] | None: ...
    def enqueue(self, intent: ReplyIntent, uow: UnitOfWork) -> RequestRef: ...
    def cancel(self, request_id: str, uow: UnitOfWork) -> bool: ...

class TaskServices(Protocol):
    def enqueue(self, intent: TaskIntent, uow: UnitOfWork) -> str: ...
    def get(self, task_key: str) -> Mapping[str, Any] | None: ...

class Clock(Protocol):
    def now(self) -> datetime: ...

class PluginLogger(Protocol):
    def info(self, message: str, *args: Any, **kwargs: Any) -> None: ...
    def warning(self, message: str, *args: Any, **kwargs: Any) -> None: ...
    def error(self, message: str, *args: Any, **kwargs: Any) -> None: ...

@dataclass(frozen=True)
class PluginServices:
    messages: MessageServices
    data: DataServices
    tasks: TaskServices
    clock: Clock
    logger: PluginLogger
    config: Any = None

@dataclass(frozen=True)
class PluginContext:
    plugin_id: str
    call_id: str
    event_key: str
    account: str | None
    conversation: str | None
    actor: str | None
    native_session: str | None
    observed_at: datetime
    permissions: frozenset[str] = frozenset()
    uow: UnitOfWork | None = None
    cancellation: Event = field(default_factory=Event, compare=False)
