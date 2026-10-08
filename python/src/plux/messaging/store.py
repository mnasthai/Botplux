"""Durable observer ingestion, message queries, and inbox state."""
from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any, Mapping

from plux.api.models import (BaseMessage, ContentQuality, HistoryQuery, ImageMessage, Migration,
                             MemberRef, MemberSnapshot, MessageIdentity, Page, TextMessage,
                             UnknownMessage, VoiceMessage)
from plux.adapters.wechat_observer import ObserverAdapter, parse_message

_MAX_LINE = 2 * 1024 * 1024
_CONTROL_ISSUES = {"hook_error", "observer_disabled", "send_hook_disabled",
                   "command_pipe_error", "native_sender_disabled", "dropped"}


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _decode(raw: bytes) -> tuple[dict[str, Any] | None, str]:
    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        value: dict[str, Any] = {}
        for key, item in items:
            if key in value:
                raise ValueError(f"duplicate key {key}")
            value[key] = item
        return value
    def reject_constant(value: str) -> None:
        raise ValueError(f"non-JSON constant {value}")
    try:
        record = json.loads(raw.decode("utf-8", "strict"), object_pairs_hook=pairs,
                            parse_constant=reject_constant)
        json.dumps(record, ensure_ascii=False, allow_nan=False).encode("utf-8", "strict")
    except (UnicodeError, ValueError, RecursionError, TypeError):
        return None, "invalid_json"
    if not isinstance(record, dict) or not isinstance(record.get("kind"), str):
        return None, "invalid_envelope"
    if type(record.get("schema_version")) is not int or record["schema_version"] != 2:
        return record, "unsupported_schema"
    for key in ("seq", "call_id", "observed_unix_ms", "msg_type"):
        if key in record and (type(record[key]) is not int or not 0 <= record[key] <= 2**63 - 1):
            return record, "invalid_envelope"
    return record, "ok"


def _model_data(message: BaseMessage) -> str:
    value = asdict(message)
    value["observed_at"] = message.observed_at.isoformat()
    return _json({"type": type(message).__name__, "value": value})


def _message(data: str) -> BaseMessage:
    encoded = json.loads(data)
    value = encoded["value"]
    identity = MessageIdentity(**value.pop("identity"))
    quality_data = value.pop("quality")
    quality_data["issues"] = tuple(quality_data["issues"])
    quality = ContentQuality(**quality_data)
    common = dict(value, identity=identity, quality=quality,
                  observed_at=datetime.fromisoformat(value["observed_at"]))
    kind = encoded["type"]
    if kind == "TextMessage":
        common["mentions"] = tuple(MemberRef(**item) for item in common["mentions"])
        quote = common.get("quote")
        if quote is not None:
            from plux.api.models import MessageRef
            common["quote"] = MessageRef(**quote)
        return TextMessage(**common)
    if kind in {"ImageMessage", "VoiceMessage"}:
        asset = common.get("asset")
        if asset is not None:
            from plux.api.models import AssetRef
            common["asset"] = AssetRef(**asset)
        return (ImageMessage if kind == "ImageMessage" else VoiceMessage)(**common)
    common["issues"] = tuple(common["issues"])
    return UnknownMessage(**common)


class MessageStore:
    """One SQLite transaction commits raw bytes, message, inbox, and checkpoint."""
    def __init__(self, database: Any, adapter: ObserverAdapter, assets: Any,
                 account: str, allowed_targets: set[str] | frozenset[str],
                 *, media_root: str | Path | None = None):
        if adapter.enabled and (not account or adapter.expected_account != account):
            raise ValueError("configured account must match observer adapter")
        if adapter.enabled and not allowed_targets:
            raise ValueError("an explicit non-empty target allowlist is required")
        from .delivery import valid_target
        if any(not valid_target(target) for target in allowed_targets):
            raise ValueError("invalid allowed target")
        self.database = database
        self.adapter = adapter
        self.assets = assets
        self.account = account
        self.allowed_targets = frozenset(allowed_targets)
        # The native sender reads media only from this directory, so media
        # replies are refused when it is unknown.
        self.media_root = Path(media_root).resolve() if media_root is not None else None
        self._poll_backlog_end: dict[str, tuple[str, int]] = {}
        self._unread_sources: dict[str, bool] = {}

    def initialize(self) -> MessageStore:
        statements = (
            """CREATE TABLE IF NOT EXISTS plux_sources (
                source TEXT PRIMARY KEY, identity TEXT NOT NULL, generation INTEGER NOT NULL,
                offset INTEGER NOT NULL DEFAULT 0, session TEXT NOT NULL DEFAULT '')""",
            """CREATE TABLE IF NOT EXISTS plux_raw_events (
                id INTEGER PRIMARY KEY, source TEXT NOT NULL, generation INTEGER NOT NULL,
                start_offset INTEGER NOT NULL, end_offset INTEGER NOT NULL, raw BLOB NOT NULL,
                parse_status TEXT NOT NULL, session TEXT NOT NULL, kind TEXT, seq INTEGER,
                event_key TEXT, record_json TEXT, duplicate_of INTEGER,
                UNIQUE(source,generation,start_offset))""",
            "CREATE INDEX IF NOT EXISTS plux_raw_event_key ON plux_raw_events(event_key)",
            """CREATE TABLE IF NOT EXISTS plux_messages (
                event_key TEXT PRIMARY KEY, raw_event_id INTEGER NOT NULL UNIQUE,
                account TEXT NOT NULL, conversation TEXT NOT NULL, actor TEXT,
                direction TEXT NOT NULL, observed_at TEXT NOT NULL, model_json TEXT NOT NULL)""",
            "CREATE INDEX IF NOT EXISTS plux_message_history ON plux_messages(account,conversation,observed_at,event_key)",
            """CREATE TABLE IF NOT EXISTS plux_inbox (
                event_key TEXT PRIMARY KEY, state TEXT NOT NULL DEFAULT 'pending',
                completed_at TEXT)""",
            """CREATE TABLE IF NOT EXISTS plux_issues (
                id INTEGER PRIMARY KEY, raw_event_id INTEGER, code TEXT NOT NULL, detail TEXT)""",
            """CREATE TABLE IF NOT EXISTS plux_members (
                account TEXT NOT NULL, conversation TEXT NOT NULL, member_id TEXT NOT NULL,
                display_name TEXT, sampled_at TEXT NOT NULL, source TEXT NOT NULL,
                PRIMARY KEY(account,conversation,member_id))""",
            """CREATE TABLE IF NOT EXISTS plux_media (
                media_key TEXT PRIMARY KEY, event_key TEXT, asset_json TEXT,
                status TEXT NOT NULL, source TEXT NOT NULL, updated_at TEXT NOT NULL)""",
        )
        self.database.migrate("plux.messaging", (Migration(1, statements),))
        from .delivery import initialize_delivery
        initialize_delivery(self.database)
        self.restore_connection()
        return self

    def restore_connection(self) -> None:
        """Restore the latest persisted endpoint when the log checkpoint skips discovery."""
        if not self.adapter.enabled or self.adapter.pipe is not None:
            return
        controls = {"observer_start", "command_pipe_ready", "command_pipe_error",
                    "native_sender_disabled", "observer_disabled"}
        with self.database.transaction() as uow:
            rows = uow.execute("""SELECT record_json FROM plux_raw_events
                WHERE parse_status='ok' AND kind IN
                ('observer_start','command_pipe_ready','command_pipe_error',
                 'native_sender_disabled','observer_disabled') ORDER BY id""").fetchall()
        for row in rows:
            record = json.loads(row[0])
            if record.get("kind") in controls:
                self.adapter.observe_control(record)

    def enqueue_manual(self, intent, uow):
        from .delivery import enqueue
        return enqueue(self, "__manual__", intent, uow, origin="manual")

    def dispatch_once(self, request_id: str | None = None):
        from .delivery import dispatch_once
        return dispatch_once(self, request_id)

    def recover(self) -> int:
        from .delivery import recover
        return recover(self)

    def for_plugin(self, plugin_id: str) -> MessageServices:
        if not plugin_id:
            raise ValueError("plugin_id required")
        return MessageServices(self, plugin_id)

    def _source(self, uow: Any, source: str, identity: str | None = None) -> tuple[int, str]:
        row = uow.execute("SELECT identity,generation,offset,session FROM plux_sources WHERE source=?",
                          (source,)).fetchone()
        if row is None:
            uow.execute("INSERT INTO plux_sources(source,identity,generation) VALUES(?,?,1)",
                        (source, identity or source))
            return 0, ""
        if identity is not None and row[0] != identity:
            uow.execute("""UPDATE plux_sources SET identity=?,generation=generation+1,
                           offset=0,session='' WHERE source=?""", (identity, source))
            return 0, ""
        return row[2], row[3]

    def ingest(self, raw: bytes, source: str, offset: int, next_offset: int,
               *, history_status: str = "unknown", identity: str | None = None,
               oversized: bool = False, ingestion_status: str = "unknown") -> BaseMessage | None:
        if not isinstance(raw, bytes) or not isinstance(source, str) or not source:
            raise ValueError("raw bytes and source required")
        if offset < 0 or next_offset <= offset or (
                next_offset - offset < len(raw) if oversized else next_offset - offset != len(raw)):
            raise ValueError("invalid contiguous byte offsets")
        if oversized or len(raw) > _MAX_LINE:
            record, status = None, "oversized_line"
        else:
            record, status = _decode(raw.rstrip(b"\r\n"))
        message: BaseMessage | None = None
        with self.database.transaction() as uow:
            expected, session = self._source(uow, source, identity)
            if expected != offset:
                raise ValueError("source offset differs from committed checkpoint")
            row = uow.execute("SELECT generation FROM plux_sources WHERE source=?", (source,)).fetchone()
            generation = row[0]
            valid = record if status == "ok" else None
            if valid:
                explicit = valid.get("session_id")
                if isinstance(explicit, str) and explicit:
                    session = explicit
                elif valid["kind"] == "observer_start":
                    session = f"legacy:{source}:{generation}:{offset}"
            if not session:
                session = f"unscoped:{source}:{generation}"
            seq = valid.get("seq") if valid and type(valid.get("seq")) is int else None
            event_key = f"{session}:{seq}" if seq is not None else None
            canonical = _json(record) if record is not None else None
            previous = uow.execute("""SELECT id,record_json FROM plux_raw_events
                WHERE event_key=? ORDER BY id LIMIT 1""", (event_key,)).fetchone() if event_key else None
            duplicate = previous[0] if previous and previous[1] == canonical else None
            cursor = uow.execute("""INSERT INTO plux_raw_events
                (source,generation,start_offset,end_offset,raw,parse_status,session,kind,seq,event_key,record_json,duplicate_of)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                (source, generation, offset, next_offset, raw, status, session,
                 record.get("kind") if record else None, seq, event_key, canonical, duplicate))
            raw_id = cursor.lastrowid
            if status != "ok":
                uow.execute("INSERT INTO plux_issues(raw_event_id,code,detail) VALUES(?,?,?)",
                            (raw_id, status, f"{offset}..{next_offset}"))
            if previous:
                uow.execute("INSERT INTO plux_issues(raw_event_id,code,detail) VALUES(?,?,?)",
                            (raw_id, "duplicate_event" if duplicate else "event_identity_conflict", str(previous[0])))
            if valid and not previous:
                message = parse_message(valid, account=self.account, session=session,
                                        raw_ref=f"raw:{raw_id}", history_status=history_status,
                                        ingestion_status=ingestion_status)
                if message is not None:
                    uow.execute("""INSERT INTO plux_messages
                        (event_key,raw_event_id,account,conversation,actor,direction,observed_at,model_json)
                        VALUES(?,?,?,?,?,?,?,?)""",
                        (message.event_key, raw_id, self.account, message.identity.conversation,
                         message.identity.actor, message.identity.direction,
                         message.observed_at.isoformat(), _model_data(message)))
                    if (message.identity.direction == "inbound"
                            and message.identity.conversation in self.allowed_targets
                            and message.identity.actor):
                        uow.execute("INSERT INTO plux_inbox(event_key) VALUES(?)", (message.event_key,))
                if valid["kind"] in _CONTROL_ISSUES:
                    uow.execute("INSERT INTO plux_issues(raw_event_id,code,detail) VALUES(?,?,?)",
                                (raw_id, valid["kind"], canonical))
                self._ingest_media(valid, raw_id, message, session, uow)
                self._ingest_evidence(valid, raw_id, session, uow)
            updated = uow.execute("""UPDATE plux_sources SET offset=?,session=?
                WHERE source=? AND generation=? AND offset=?""",
                (next_offset, session, source, generation, expected))
            if updated.rowcount != 1:
                raise RuntimeError("concurrent source checkpoint change")
        if valid:
            self.adapter.observe_control(valid)
        return message

    def _ingest_media(self, record: Mapping[str, Any], raw_id: int,
                      message: BaseMessage | None, session: str, uow: Any) -> None:
        if record.get("kind") not in {"media_asset", "media_encoded_asset"}:
            return
        candidate = record.get("message_id")
        key = record.get("media_key") or record.get("asset_id") or (
            f"{session}:msgid:{candidate}" if isinstance(candidate, str) and candidate else None)
        if not isinstance(key, str) or not key:
            return
        now = datetime.now(timezone.utc).isoformat()
        uow.execute("""INSERT INTO plux_media(media_key,event_key,asset_json,status,source,updated_at)
            VALUES(?,?,?,?,?,?) ON CONFLICT(media_key) DO UPDATE SET
            event_key=COALESCE(excluded.event_key,plux_media.event_key),
            asset_json=excluded.asset_json,status=excluded.status,updated_at=excluded.updated_at""",
            (key, record.get("event_key") if isinstance(record.get("event_key"), str) else
             (message.event_key if message else None), _json(record),
             record.get("status", "observed"), record["kind"], now))

    def _ingest_evidence(self, record: Mapping[str, Any], raw_id: int,
                         session: str, uow: Any) -> None:
        if record.get("kind") not in {"native_send_request", "native_send_result"}:
            return
        request_id = record.get("request_id")
        if isinstance(request_id, str):
            uow.execute("""INSERT OR IGNORE INTO plux_delivery_evidence
                (request_id,session,raw_event_id,payload_json,observed_at)
                VALUES(?,?,?,?,?)""",
                (request_id, session, raw_id, _json(record), datetime.now(timezone.utc).isoformat()))

    def poll_log(self, path: str | Path, limit: int = 100) -> int:
        path = Path(path).resolve()
        stat = path.stat()
        identity = f"{stat.st_dev}:{stat.st_ino}"
        source = str(path)
        with self.database.transaction() as uow:
            offset, _ = self._source(uow, source, identity)
            if stat.st_size < offset:
                uow.execute("""UPDATE plux_sources SET generation=generation+1,offset=0,session=''
                               WHERE source=?""", (source,))
                offset = 0
        previous_boundary = self._poll_backlog_end.get(source)
        if previous_boundary is None or previous_boundary[0] != identity or stat.st_size < previous_boundary[1]:
            self._poll_backlog_end[source] = (identity, stat.st_size)
        backlog_end = self._poll_backlog_end[source][1]
        count = 0
        with path.open("rb") as stream:
            stream.seek(offset)
            while count < limit:
                start = stream.tell()
                line = stream.readline(_MAX_LINE + 1)
                if not line:
                    break
                oversized = len(line) > _MAX_LINE
                if oversized and not line.endswith(b"\n"):
                    while True:
                        chunk = stream.readline(_MAX_LINE + 1)
                        if not chunk or chunk.endswith(b"\n"):
                            break
                    if not chunk:
                        break
                elif not line.endswith(b"\n"):
                    break
                end = stream.tell()
                self.ingest(line[:_MAX_LINE] if oversized else line,
                            source, start, end, identity=identity, oversized=oversized,
                            ingestion_status="backlog" if start < backlog_end else "new")
                count += 1
            self._unread_sources[source] = end < stat.st_size if count else offset < stat.st_size
        return count

    def pending_inputs(self, limit: int = 100) -> tuple[BaseMessage, ...]:
        if limit < 1:
            return ()
        with self.database.transaction() as uow:
            rows = uow.execute("""SELECT m.model_json FROM plux_inbox i
                JOIN plux_messages m ON m.event_key=i.event_key
                WHERE i.state='pending' ORDER BY m.observed_at,m.event_key LIMIT ?""",
                (limit,)).fetchall()
        return tuple(_message(row[0]) for row in rows)

    def complete_input(self, event_key: str, uow: Any) -> None:
        if not uow.active or uow.domain != self.database.domain or getattr(uow, "_database", None) is not self.database:
            raise ValueError("active message database transaction required")
        result = uow.execute("""UPDATE plux_inbox SET state='complete',completed_at=?
            WHERE event_key=? AND state='pending'""",
            (datetime.now(timezone.utc).isoformat(), event_key))
        if result.rowcount == 0:
            row = uow.execute("SELECT state FROM plux_inbox WHERE event_key=?", (event_key,)).fetchone()
            if row is None:
                raise KeyError(event_key)

    def upsert_member(self, ref: MemberRef, *, display_name: str | None,
                      sampled_at: datetime, source: str, uow: Any | None = None) -> None:
        if ref.account != self.account or not source or sampled_at.tzinfo is None:
            raise ValueError("invalid member snapshot")
        if uow is None:
            with self.database.transaction() as own:
                self.upsert_member(ref, display_name=display_name, sampled_at=sampled_at,
                                   source=source, uow=own)
            return
        if not uow.active or getattr(uow, "_database", None) is not self.database:
            raise ValueError("member update requires this message database transaction")
        uow.execute("""INSERT INTO plux_members
            (account,conversation,member_id,display_name,sampled_at,source)
            VALUES(?,?,?,?,?,?) ON CONFLICT(account,conversation,member_id)
            DO UPDATE SET display_name=excluded.display_name,sampled_at=excluded.sampled_at,
                          source=excluded.source WHERE excluded.sampled_at>=plux_members.sampled_at""",
            (ref.account, ref.conversation or "", ref.member_id, display_name,
             sampled_at.astimezone(timezone.utc).isoformat(), source))


class MessageServices:
    def __init__(self, store: MessageStore, plugin_id: str):
        self.store, self.plugin_id = store, plugin_id

    def get(self, event_key: str, uow: Any | None = None) -> BaseMessage | None:
        if uow is None:
            with self.store.database.transaction() as own:
                return self.get(event_key, own)
        if not uow.active or getattr(uow, "_database", None) is not self.store.database:
            raise ValueError("message query requires this message database transaction")
        row = uow.execute("SELECT model_json FROM plux_messages WHERE event_key=?", (event_key,)).fetchone()
        return _message(row[0]) if row else None

    def _check_scope(self, uow: Any) -> None:
        if not uow.active or getattr(uow, "_database", None) is not self.store.database:
            raise ValueError("message query requires this message database transaction")

    def history(self, query: HistoryQuery, uow: Any | None = None) -> Page:
        if query.account != self.store.account or not 1 <= query.limit <= 200:
            raise ValueError("invalid history scope or limit")
        if uow is None:
            with self.store.database.transaction() as own:
                return self.history(query, own)
        self._check_scope(uow)
        if query.cursor is None:
            rows = uow.execute("""SELECT model_json,event_key FROM plux_messages
                WHERE account=? AND conversation=? ORDER BY observed_at DESC,event_key DESC LIMIT ?""",
                (query.account, query.conversation, query.limit + 1)).fetchall()
        else:
            cursor = uow.execute("""SELECT observed_at FROM plux_messages WHERE event_key=?
                AND account=? AND conversation=?""",
                (query.cursor, query.account, query.conversation)).fetchone()
            if cursor is None:
                raise ValueError("unknown history cursor")
            rows = uow.execute("""SELECT model_json,event_key FROM plux_messages
                WHERE account=? AND conversation=? AND
                (observed_at<? OR (observed_at=? AND event_key<?))
                ORDER BY observed_at DESC,event_key DESC LIMIT ?""",
                (query.account, query.conversation, cursor[0], cursor[0], query.cursor,
                 query.limit + 1)).fetchall()
        visible = rows[:query.limit]
        return Page(tuple(_message(row[0]) for row in visible),
                    visible[-1][1] if len(rows) > query.limit else None)

    def member(self, ref: MemberRef, uow: Any | None = None) -> MemberSnapshot:
        if ref.account != self.store.account:
            raise ValueError("account mismatch")
        if uow is None:
            with self.store.database.transaction() as own:
                return self.member(ref, own)
        self._check_scope(uow)
        row = uow.execute("""SELECT display_name,sampled_at,source FROM plux_members
            WHERE account=? AND conversation=? AND member_id=?""",
            (ref.account, ref.conversation or "", ref.member_id)).fetchone()
        return MemberSnapshot(ref) if row is None else MemberSnapshot(
            ref, row[0], datetime.fromisoformat(row[1]), row[2])

    def media(self, media_key: str, uow: Any | None = None) -> Mapping[str, Any] | None:
        if uow is None:
            with self.store.database.transaction() as own:
                return self.media(media_key, own)
        self._check_scope(uow)
        row = uow.execute("SELECT asset_json FROM plux_media WHERE media_key=?", (media_key,)).fetchone()
        return json.loads(row[0]) if row else None

    def input_status(self, uow: Any | None = None, *, exclude_event_key: str | None = None) -> Mapping[str, Any]:
        if uow is None:
            with self.store.database.transaction() as own:
                return self.input_status(own, exclude_event_key=exclude_event_key)
        self._check_scope(uow)
        row = uow.execute("""SELECT COUNT(*),MIN(m.observed_at) FROM plux_inbox i
            JOIN plux_messages m ON m.event_key=i.event_key
            WHERE i.state='pending' AND m.account=? AND (? IS NULL OR m.event_key<>?)""",
            (self.store.account, exclude_event_key, exclude_event_key)).fetchone()
        return {"pending_count": row[0], "oldest_observed_at": row[1],
                "source_unread": any(self.store._unread_sources.values())}

    def reply_reference(self, reply_key: str, uow: Any | None = None):
        from plux.api import RequestRef
        if uow is None:
            with self.store.database.transaction() as own:
                return self.reply_reference(reply_key, own)
        self._check_scope(uow)
        row = uow.execute("SELECT request_id FROM plux_replies WHERE plugin_id=? AND reply_key=?",
                          (self.plugin_id, reply_key)).fetchone()
        return RequestRef(row[0]) if row else None

    def connection(self):
        return self.store.adapter.connection()

    def enqueue(self, intent, uow):
        from .delivery import enqueue
        return enqueue(self.store, self.plugin_id, intent, uow)

    def cancel(self, request_id: str, uow: Any) -> bool:
        self._check_scope(uow)
        row = uow.execute("""SELECT logical_json FROM plux_replies
            WHERE request_id=? AND plugin_id=? AND status='queued'
            AND NOT EXISTS (SELECT 1 FROM plux_delivery_attempts a
                            WHERE a.request_id=plux_replies.request_id)""",
            (request_id, self.plugin_id)).fetchone()
        if row is None:
            return False
        uow.execute("UPDATE plux_replies SET status='cancelled',error_code='producer_cancelled',updated_at=? WHERE request_id=?",
                    (datetime.now(timezone.utc).isoformat(), request_id))
        asset = json.loads(row[0]).get("asset")
        if asset:
            from plux.api import AssetRef
            self.store.assets.release(AssetRef(**asset), f"plux:reply:{request_id}", uow)
        return True

    def receipt(self, request_id: str, uow: Any | None = None):
        from .delivery import receipt
        return receipt(self.store, request_id, uow)