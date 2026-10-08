"""Immutable public values. Timestamps use timezone-aware UTC."""
from __future__ import annotations
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Mapping
from pathlib import Path

def utc_now() -> datetime:
    return datetime.now(timezone.utc)

def require_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamp must include a timezone")
    return value.astimezone(timezone.utc)

@dataclass(frozen=True)
class AssetRef:
    asset_id: str
    version: str
    kind: str
    sha256: str

@dataclass(frozen=True)
class CatalogRef:
    namespace: str
    version: str
    name: str | None = None

@dataclass(frozen=True)
class MemberRef:
    account: str
    member_id: str
    conversation: str | None = None

@dataclass(frozen=True)
class MessageRef:
    event_key: str
    message_id: str | None = None

@dataclass(frozen=True)
class MessageIdentity:
    account: str
    conversation: str
    actor: str | None
    native_session: str | None = None
    direction: str = "inbound"

@dataclass(frozen=True)
class ContentQuality:
    status: str = "ok"
    mentions_status: str = "unknown"
    history_status: str = "unknown"
    issues: tuple[str, ...] = ()
    ingestion_status: str = "unknown"

@dataclass(frozen=True, kw_only=True)
class BaseMessage:
    event_key: str
    identity: MessageIdentity
    observed_at: datetime
    quality: ContentQuality = field(default_factory=ContentQuality)

    def __post_init__(self) -> None:
        object.__setattr__(self, "observed_at", require_utc(self.observed_at))

@dataclass(frozen=True, kw_only=True)
class TextMessage(BaseMessage):
    text: str
    mentions: tuple[MemberRef, ...] = ()
    quote: MessageRef | None = None

@dataclass(frozen=True, kw_only=True)
class ImageMessage(BaseMessage):
    asset: AssetRef | None = None
    media_key: str | None = None
    width: int | None = None
    height: int | None = None

@dataclass(frozen=True, kw_only=True)
class VoiceMessage(BaseMessage):
    asset: AssetRef | None = None
    media_key: str | None = None
    duration_ms: int | None = None
    transcription: str | None = None

@dataclass(frozen=True, kw_only=True)
class UnknownMessage(BaseMessage):
    raw_type: str
    raw_ref: str
    issues: tuple[str, ...] = ()

@dataclass(frozen=True)
class ReplyIntent:
    reply_key: str
    account: str
    conversation: str
    native_session: str | None
    text: str | None = None
    asset: AssetRef | None = None
    mentions: tuple[MemberRef, ...] = ()
    quote: MessageRef | None = None
    ttl_seconds: int = 300
    source_event_key: str | None = None
    duration_ms: int = 0

@dataclass(frozen=True)
class ConnectionSnapshot:
    account: str | None = None
    native_session: str | None = None
    sampled_at: datetime = field(default_factory=utc_now)
    source: str = "unavailable"
    phase: str = "disconnected"
    capabilities: frozenset[str] = frozenset()
    can_send: bool = False
    generation: int = 0
    issues: tuple[str, ...] = ()

@dataclass(frozen=True)
class RequestRef:
    request_id: str

@dataclass(frozen=True)
class Attempt:
    attempt_id: str
    status: str
    started_at: datetime
    finished_at: datetime | None = None
    error_code: str | None = None

@dataclass(frozen=True)
class Receipt:
    request_id: str
    status: str
    attempts: tuple[Attempt, ...] = ()
    evidence: tuple[Mapping[str, Any], ...] = ()
    updated_at: datetime = field(default_factory=utc_now)
    error_code: str | None = None

@dataclass(frozen=True)
class HistoryQuery:
    account: str
    conversation: str
    limit: int = 50
    cursor: str | None = None

@dataclass(frozen=True)
class Page:
    items: tuple[Any, ...]
    next_cursor: str | None = None

@dataclass(frozen=True)
class MemberSnapshot:
    ref: MemberRef
    display_name: str | None = None
    sampled_at: datetime | None = None
    source: str = "missing"

@dataclass(frozen=True)
class Migration:
    version: int
    statements: tuple[str, ...]

@dataclass(frozen=True)
class CatalogSnapshot:
    ref: CatalogRef
    format_version: int
    digest: str
    data: Any
    sources: tuple[str, ...] = ()

@dataclass(frozen=True)
class StateSnapshot:
    key: str
    structure_version: int
    revision: int
    data: Any
    deadline: datetime | None = None
    catalog: CatalogRef | None = None
    assets: tuple[AssetRef, ...] = ()
    durability: str = "persistent"

@dataclass(frozen=True)
class TaskIntent:
    task_key: str
    task_type: str
    payload: Any
    not_before: datetime | None = None
    deadline: datetime | None = None
    catalog: CatalogRef | None = None
    assets: tuple[AssetRef, ...] = ()

@dataclass(frozen=True)
class Outcome:
    status: str = "success"
    result: Any = None
    replies: tuple[ReplyIntent, ...] = ()
    tasks: tuple[TaskIntent, ...] = ()
    error_code: str | None = None

    @classmethod
    def success(cls, result: Any = None, *, replies: tuple[ReplyIntent, ...] = (),
                tasks: tuple[TaskIntent, ...] = ()) -> Outcome:
        return cls("success", result, replies, tasks)

    @classmethod
    def rejected(cls, error_code: str, *, replies: tuple[ReplyIntent, ...] = ()) -> Outcome:
        return cls("rejected", replies=replies, error_code=error_code)

    @classmethod
    def noop(cls) -> Outcome:
        return cls("noop")

@dataclass(frozen=True)
class CatalogSpec:
    name: str
    version: str
    default: Mapping[str, Any] | Path
    format_version: int = 1
    validator: Callable[[Any], Any] | None = None
    override: Path | None = None

@dataclass(frozen=True)
class PluginManifest:
    plugin_id: str
    version: str = "0.1.0"
    api: str = "0.1"
    dependencies: tuple[str, ...] = ()
    capabilities: frozenset[str] = frozenset()
    namespace: str | None = None
    database_domain: str = "main"
    migrations: tuple[Migration, ...] = ()
    config_validator: Callable[[Mapping[str, Any]], Any] | None = None
    catalogs: tuple[CatalogSpec, ...] = ()
    resources: tuple[Path, ...] = ()

@dataclass(frozen=True)
class CommandSpec:
    handler_id: str
    trigger: str
    handler: Callable[[str, Any], Outcome]
    mode: str = "stateless"
    priority: int = 0
    database_domain: str = "main"

@dataclass(frozen=True)
class EventSpec:
    handler_id: str
    handler: Callable[[BaseMessage, Any], Outcome]
    message_type: type[BaseMessage] = BaseMessage
    predicate: Callable[[BaseMessage], bool] | None = None
    mode: str = "atomic"
    serial_key: str | None = None
    database_domain: str = "main"
    requires_command_policy: bool = False

@dataclass(frozen=True)
class ScheduleSpec:
    schedule_id: str
    task_type: str
    interval_seconds: int
    payload: Any = None
    timezone: str = "UTC"
    missed_policy: str = "coalesce"

@dataclass(frozen=True)
class TaskSpec:
    task_type: str
    work: Callable[[Any, Any], Any]
    commit: Callable[[Any, Any], Outcome]
    prepare: Callable[[Any, Any], Any] | None = None
    recover: Callable[[Any, Any], Any] | None = None
    idempotent: bool = False
    max_attempts: int = 1
    timeout_seconds: float = 60.0
    concurrency_key: str | None = None
    input_version: int = 1
    result_version: int = 1
