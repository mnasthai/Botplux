"""Persisted replies and one-at-a-time native delivery."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import threading
import uuid
from typing import Any

from plux.api.models import (AssetRef, Attempt, Migration, Receipt, ReplyIntent, RequestRef)
from plux.adapters.wechat_observer.transport import (TransportError, encode_message)
from plux.adapters.wechat_observer import TARGET_VERSION, native_media_path

_TARGET = re.compile(r"[A-Za-z0-9_-]{1,128}(?:@chatroom)?\Z", re.ASCII)
_LOCK = threading.Lock()
# The native host answers `op:"error"` only while it is still rejecting the
# request, before any send is dispatched, so these codes prove that nothing was
# attempted and the delivery is a definite rejection. Every gate, account,
# target, media, mention, quote, rate and busy failure is reported as
# `op:"send_result"` instead and keeps its own rejected/unknown status. A code
# outside this set stays uncertain: claiming "not sent" wrongly would invite a
# duplicate reply.
_PREEXECUTION = {"invalid_request", "invalid_frame", "invalid_utf8", "invalid_json",
                 "unsupported_operation", "unsupported_protocol"}


def valid_target(value: object) -> bool:
    if not isinstance(value, str) or _TARGET.fullmatch(value) is None:
        return False
    return "@" not in value or value.endswith("@chatroom")


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _timestamp(value: datetime) -> str:
    if value.tzinfo is None:
        raise ValueError("timezone-aware timestamp required")
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def initialize_delivery(database: Any) -> None:
    statements = (
        """CREATE TABLE IF NOT EXISTS plux_replies (
            request_id TEXT PRIMARY KEY, plugin_id TEXT NOT NULL, reply_key TEXT NOT NULL,
            account TEXT NOT NULL, native_session TEXT NOT NULL, target TEXT NOT NULL,
            kind TEXT NOT NULL, logical_json TEXT NOT NULL, logical_fingerprint TEXT NOT NULL,
            native_json TEXT, native_fingerprint TEXT, status TEXT NOT NULL,
            created_at TEXT NOT NULL, expires_at TEXT NOT NULL, updated_at TEXT NOT NULL,
            error_code TEXT, UNIQUE(plugin_id,reply_key))""",
        "CREATE INDEX IF NOT EXISTS plux_reply_queue ON plux_replies(status,created_at)",
        """CREATE TABLE IF NOT EXISTS plux_delivery_attempts (
            attempt_id TEXT PRIMARY KEY, request_id TEXT NOT NULL,
            status TEXT NOT NULL, started_at TEXT NOT NULL, finished_at TEXT,
            error_code TEXT, error_detail TEXT)""",
        "CREATE INDEX IF NOT EXISTS plux_attempt_request ON plux_delivery_attempts(request_id)",
        """CREATE TABLE IF NOT EXISTS plux_delivery_evidence (
            id INTEGER PRIMARY KEY, request_id TEXT NOT NULL, session TEXT NOT NULL,
            raw_event_id INTEGER UNIQUE, payload_json TEXT NOT NULL, observed_at TEXT NOT NULL)""",
    )
    database.migrate("plux.delivery", (Migration(1, statements),))


def _quote_payload(store: Any, event_key: str, conversation: str, uow: Any) -> dict[str, Any]:
    row = uow.execute("""SELECT m.model_json,r.record_json FROM plux_messages m
        JOIN plux_raw_events r ON r.id=m.raw_event_id WHERE m.event_key=?""",
        (event_key,)).fetchone()
    if row is None:
        raise ValueError("quote message not found")
    envelope, raw = json.loads(row[0]), json.loads(row[1])
    if envelope["type"] != "TextMessage":
        raise ValueError("quote requires a text message")
    model = envelope["value"]
    identity = model["identity"]
    if identity["account"] != store.account or identity["conversation"] != conversation or identity["direction"] != "inbound":
        raise ValueError("quote identity is not a matching incoming message")
    content_read = raw.get("content_read")
    if raw.get("msg_type") != 1 or not isinstance(content_read, dict) or content_read.get("status") != "ok":
        raise ValueError("quote requires complete plain text")
    fields = raw.get("raw_fields")
    if not isinstance(fields, dict):
        raise ValueError("quote metadata unavailable")
    message_id = fields.get("12")
    timestamp = fields.get("9")
    if not isinstance(message_id, str) or not message_id.isascii() or not message_id.isdecimal() or not 1 <= int(message_id) < 2**64:
        raise ValueError("quote message ID unavailable")
    if not isinstance(timestamp, str) or not timestamp.isascii() or not timestamp.isdecimal() or not 0 <= int(timestamp) < 2**32:
        raise ValueError("quote timestamp unavailable")
    sender = identity["actor"]
    if not valid_target(sender) or sender.endswith("@chatroom"):
        raise ValueError("quote sender unavailable")
    text = model.get("text")
    if not isinstance(text, str) or not text or len(text.encode("utf-8")) > 16384:
        raise ValueError("quote text unavailable")
    source = raw.get("msg_source", "")
    if not isinstance(source, str) or len(source.encode("utf-8")) > 8192:
        raise ValueError("quote source invalid")
    return dict(quote_message_id=str(int(message_id)), quote_from_id=conversation if conversation.endswith("@chatroom") else sender,
                quote_to_id=store.account, quote_sender_id=sender,
                quote_conversation_id=conversation, quote_text=text,
                quote_timestamp=int(timestamp), quote_msg_source=source, quote_message_type=1)


def enqueue(store: Any, plugin_id: str, intent: ReplyIntent, uow: Any, *, origin: str = "game") -> RequestRef:
    if origin not in {"game", "manual"}:
        raise ValueError("unsupported native origin")
    if not uow.active or uow.domain != store.database.domain or getattr(uow, "_database", None) is not store.database:
        raise ValueError("reply requires an active transaction in the message database domain")
    if not isinstance(intent, ReplyIntent) or not intent.reply_key or not plugin_id:
        raise ValueError("valid reply intent required")
    if intent.account != store.account or intent.conversation not in store.allowed_targets:
        raise ValueError("reply account or target is not allowed")
    if not intent.native_session:
        raise ValueError("reply requires an explicit native session")
    if type(intent.ttl_seconds) is not int or not 1 <= intent.ttl_seconds <= 600:
        raise ValueError("reply TTL must be between 1 and 600 seconds")
    if (intent.text is None) == (intent.asset is None):
        raise ValueError("reply needs exactly one text or asset")
    if intent.text is not None and (not intent.text or len(intent.text.encode("utf-8")) > 16384):
        raise ValueError("invalid reply text")
    if len(intent.mentions) > 16 or len({m.member_id for m in intent.mentions}) != len(intent.mentions):
        raise ValueError("invalid mention list")
    if intent.mentions and not intent.conversation.endswith("@chatroom"):
        raise ValueError("mentions require a group")
    for mention in intent.mentions:
        if mention.account != store.account or mention.conversation != intent.conversation or not valid_target(mention.member_id) or mention.member_id.endswith("@chatroom"):
            raise ValueError("invalid mention identity")
    if intent.asset is not None and (intent.mentions or intent.quote):
        raise ValueError("media reply cannot carry mentions or quote")
    if intent.quote is not None and intent.quote.event_key == intent.reply_key:
        raise ValueError("quote must reference a captured message")
    quote = _quote_payload(store, intent.quote.event_key, intent.conversation, uow) if intent.quote else None
    kind = "text" if intent.text is not None else intent.asset.kind
    if kind not in {"text", "image", "voice"}:
        raise ValueError("only text, image and voice replies are supported")
    if kind == "voice" and (type(intent.duration_ms) is not int or not 0 < intent.duration_ms <= 60000 or intent.duration_ms % 20):
        raise ValueError("voice duration must be 20 ms aligned")
    if kind != "voice" and intent.duration_ms != 0:
        raise ValueError("duration is only valid for voice")
    payload = dict(reply_key=intent.reply_key, account=intent.account, target=intent.conversation,
                   native_session=intent.native_session, text=intent.text,
                   asset=asdict(intent.asset) if intent.asset else None,
                   mentions=[m.member_id for m in intent.mentions], quote=quote,
                   source_event_key=intent.source_event_key, duration_ms=intent.duration_ms,
                   kind=kind, ttl_seconds=intent.ttl_seconds, origin=origin)
    logical = _json(payload)
    fingerprint = _digest(logical)
    existing = uow.execute("SELECT request_id,logical_fingerprint FROM plux_replies WHERE plugin_id=? AND reply_key=?",
                           (plugin_id, intent.reply_key)).fetchone()
    if existing:
        if existing[1] != fingerprint:
            raise ValueError("reply key conflicts with another logical payload")
        return RequestRef(existing[0])
    request_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"plux:{plugin_id}:{intent.reply_key}"))
    now = datetime.now(timezone.utc)
    created, expires = _timestamp(now), _timestamp(now + timedelta(seconds=intent.ttl_seconds))
    uow.execute("""INSERT INTO plux_replies
        (request_id,plugin_id,reply_key,account,native_session,target,kind,
         logical_json,logical_fingerprint,status,created_at,expires_at,updated_at)
         VALUES(?,?,?,?,?,?,?,?,?,'queued',?,?,?)""",
        (request_id, plugin_id, intent.reply_key, store.account, intent.native_session,
         intent.conversation, kind, logical, fingerprint, created, expires, created))
    if intent.asset is not None:
        store.assets.retain(intent.asset, f"plux:reply:{request_id}", uow)
    return RequestRef(request_id)


def receipt(store: Any, request_id: str, uow: Any | None = None) -> Receipt:
    if uow is None:
        with store.database.transaction() as own:
            return receipt(store, request_id, own)
    if not uow.active or getattr(uow, "_database", None) is not store.database:
        raise ValueError("receipt query requires this message database transaction")
    row = uow.execute("""SELECT status,updated_at,error_code FROM plux_replies
        WHERE request_id=?""", (request_id,)).fetchone()
    if row is None:
        raise KeyError(request_id)
    attempts = uow.execute("""SELECT attempt_id,status,started_at,finished_at,error_code
        FROM plux_delivery_attempts WHERE request_id=? ORDER BY started_at,attempt_id""",
        (request_id,)).fetchall()
    evidence = uow.execute("""SELECT payload_json FROM plux_delivery_evidence
        WHERE request_id=? ORDER BY id""", (request_id,)).fetchall()
    return Receipt(request_id, row[0], tuple(Attempt(
        item[0], item[1], datetime.fromisoformat(item[2].replace("Z", "+00:00")),
        datetime.fromisoformat(item[3].replace("Z", "+00:00")) if item[3] else None,
        item[4]) for item in attempts),
        tuple(json.loads(item[0]) for item in evidence),
        datetime.fromisoformat(row[1].replace("Z", "+00:00")), row[2])


@dataclass(frozen=True)
class DispatchReport:
    status: str
    request_id: str | None = None
    attempt_id: str | None = None
    error_code: str | None = None


class SenderLock:
    """A dedicated process lock, independent of receiver and database locks."""
    def __init__(self, database: Any):
        path = getattr(database, "path", None)
        if path is None or str(path) == ":memory:":
            raise ValueError("a file-backed database is required for native sender locking")
        self.path = Path(path).with_suffix(Path(path).suffix + ".sender.lock")
        self.stream = None

    def __enter__(self):
        _LOCK.acquire()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.stream = self.path.open("a+b")
        self.stream.seek(0, os.SEEK_END)
        if not self.stream.tell():
            self.stream.write(b"\0")
            self.stream.flush()
        self.stream.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.stream.fileno(), msvcrt.LK_LOCK, 1)
            else:
                import fcntl
                fcntl.flock(self.stream, fcntl.LOCK_EX)
        except BaseException:
            self.stream.close()
            _LOCK.release()
            raise
        return self

    def __exit__(self, *_):
        try:
            self.stream.close()
        finally:
            _LOCK.release()


def _native_payload(store: Any, row: Any, logical: dict[str, Any], attempt_id: str) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "protocol_version": 1, "request_id": row[0], "attempt_id": attempt_id,
        "expected_account_id": row[1], "observer_session_id": row[2],
        "target_id": row[3], "created_at": row[5], "expires_at": row[6],
        "source_event_key": logical["source_event_key"], "origin": logical["origin"]}
    if row[4] == "text":
        payload["text"] = logical["text"]
        mentions = logical["mentions"]
        if mentions:
            payload["at_user_list"] = ",".join(mentions)
        if logical["quote"]:
            payload.update(logical["quote"])
        payload["op"] = "send_rich_text" if mentions or logical["quote"] else "send_text"
    else:
        asset = AssetRef(**logical["asset"])
        path = Path(store.assets.resolve(asset)).resolve(strict=True)
        if not path.is_file():
            raise ValueError("media asset is not a file")
        size = path.stat().st_size
        limit = 20 * 1024 * 1024 if row[4] == "image" else 1024 * 1024
        if not 0 < size <= limit:
            raise ValueError("media asset size invalid")
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            while block := stream.read(1024 * 1024):
                digest.update(block)
        if digest.hexdigest() != asset.sha256:
            raise ValueError("media asset digest mismatch")
        if store.media_root is None:
            raise ValueError("a native media root is required for media replies")
        # The native host reads only `<media_root>/<sha256>.<ext>`, so the
        # published asset layout is exported before the payload is fixed.
        export = native_media_path(store.media_root, path, row[4], asset.sha256)
        payload.update(op="send_media", media_kind=row[4], media_path=str(export),
                       media_sha256=asset.sha256, media_bytes=size,
                       duration_ms=logical["duration_ms"])
    encode_message(payload)
    return payload


def _prepared_payload(store: Any, row: Any, logical: dict[str, Any], attempt_id: str) -> dict[str, Any]:
    if row[8] is None:
        return _native_payload(store, row, logical, attempt_id)
    payload = json.loads(row[8])
    if not isinstance(payload, dict) or _digest(_json(payload)) != row[9]:
        raise ValueError("prepared native fingerprint mismatch")
    required = {"protocol_version": 1, "request_id": row[0], "expected_account_id": row[1],
                "observer_session_id": row[2], "target_id": row[3],
                "created_at": row[5], "expires_at": row[6]}
    if any(payload.get(key) != value for key, value in required.items()) or not isinstance(payload.get("attempt_id"), str) or not payload["attempt_id"]:
        raise ValueError("prepared native identity mismatch")
    if row[4] == "text":
        expected_op = "send_rich_text" if logical["mentions"] or logical["quote"] else "send_text"
        if payload.get("op") != expected_op or payload.get("text") != logical["text"]:
            raise ValueError("prepared native text mismatch")
    else:
        asset = AssetRef(**logical["asset"])
        path = Path(payload["media_path"])
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            while block := stream.read(1024 * 1024):
                digest.update(block)
        if (payload.get("op") != "send_media" or payload.get("media_kind") != row[4]
                or payload.get("media_sha256") != asset.sha256
                or path.stat().st_size != payload.get("media_bytes") or digest.hexdigest() != asset.sha256):
            raise ValueError("prepared media changed")
    encode_message(payload)
    return payload


def _capability_error(snapshot: Any, logical: dict[str, Any], kind: str) -> str | None:
    caps = snapshot.capabilities
    if not snapshot.can_send:
        return "native_sender_unavailable"
    if kind == "text":
        if "send_text" not in caps:
            return "native_text_sender_unavailable"
        if logical["target"].endswith("@chatroom") and "send_group_text" not in caps:
            return "native_group_sender_unavailable"
        if logical["mentions"] and "send_mention" not in caps:
            return "native_mention_sender_unavailable"
        if logical["quote"] and "send_quote" not in caps:
            return "native_quote_sender_unavailable"
    elif f"send_{kind}" not in caps:
        return f"native_{kind}_sender_unavailable"
    return None


def _recover_locked(store: Any) -> int:
    now = _timestamp(datetime.now(timezone.utc))
    with store.database.transaction() as uow:
        rows = uow.execute("SELECT request_id FROM plux_replies WHERE status='dispatching'").fetchall()
        for row in rows:
            uow.execute("""UPDATE plux_replies SET status='unknown',error_code='interrupted_dispatch',
                updated_at=? WHERE request_id=?""", (now, row[0]))
            uow.execute("""UPDATE plux_delivery_attempts SET status='unknown',error_code='interrupted_dispatch',
                finished_at=? WHERE request_id=? AND status='dispatching'""", (now, row[0]))
        prepared = uow.execute("""SELECT r.request_id FROM plux_replies r
            WHERE r.status='queued' AND EXISTS(
                SELECT 1 FROM plux_delivery_attempts a WHERE a.request_id=r.request_id)""").fetchall()
        for row in prepared:
            uow.execute("""UPDATE plux_replies SET status='unknown',error_code='attempt_already_exists',
                updated_at=? WHERE request_id=?""", (now, row[0]))
    return len(rows) + len(prepared)


def recover(store: Any) -> int:
    with SenderLock(store.database):
        return _recover_locked(store)


def dispatch_once(store: Any, request_id: str | None = None) -> DispatchReport:
    with SenderLock(store.database):
        _recover_locked(store)
        now = _timestamp(datetime.now(timezone.utc))
        with store.database.transaction() as uow:
            uow.execute("""UPDATE plux_replies SET status='expired',error_code='expired',
                updated_at=? WHERE status='queued' AND expires_at<=?""", (now, now))
            where = " AND request_id=?" if request_id else ""
            args = (now, request_id) if request_id else (now,)
            candidates = uow.execute("""SELECT request_id,account,native_session,target,kind,
                created_at,expires_at,logical_json,native_json,native_fingerprint FROM plux_replies
                WHERE status='queued' AND expires_at>?""" + where +
                " ORDER BY created_at,request_id", args).fetchall()
        if not candidates:
            return DispatchReport("idle", request_id, error_code="not_queued" if request_id else None)
        try:
            snapshot = store.adapter.probe()
        except (TransportError, ValueError) as exc:
            return DispatchReport("blocked", candidates[0][0], error_code=getattr(exc, "error_code", "probe_failed"))
        for row in candidates:
            logical = json.loads(row[7])
            if (row[1] != store.account or row[3] not in store.allowed_targets
                    or row[2] != snapshot.native_session or snapshot.account != store.account):
                if request_id:
                    return DispatchReport("blocked", row[0], error_code="identity_or_target_mismatch")
                continue
            capability = _capability_error(snapshot, logical, row[4])
            if capability:
                if request_id:
                    return DispatchReport("blocked", row[0], error_code=capability)
                continue
            attempt_id = str(uuid.uuid4())
            try:
                payload = _prepared_payload(store, row, logical, attempt_id)
                attempt_id = payload["attempt_id"]
            except (ValueError, OSError, TypeError) as exc:
                with store.database.transaction() as uow:
                    uow.execute("""UPDATE plux_replies SET status='rejected',
                        error_code='invalid_native_payload',updated_at=?
                        WHERE request_id=? AND status='queued'""", (now, row[0]))
                return DispatchReport("rejected", row[0], error_code="invalid_native_payload")
            native = _json(payload)
            fingerprint = _digest(native)
            with store.database.transaction() as uow:
                changed = uow.execute("""UPDATE plux_replies SET native_json=?,native_fingerprint=?,
                    status='dispatching',updated_at=? WHERE request_id=? AND status='queued'""",
                    (native, fingerprint, now, row[0]))
                if changed.rowcount != 1:
                    return DispatchReport("idle", row[0], error_code="queue_changed")
                uow.execute("""INSERT INTO plux_delivery_attempts
                    (attempt_id,request_id,status,started_at) VALUES(?,?,'dispatching',?)""",
                    (attempt_id, row[0], now))
            try:
                response = store.adapter.exchange(payload)
            except TransportError as exc:
                status = "unknown" if exc.may_have_written else "rejected"
                return _finish(store, row[0], attempt_id, status, exc.error_code, str(exc))
            except BaseException as exc:
                return _finish(store, row[0], attempt_id, "unknown", "transport_failure", str(exc))
            status, code = "unknown", "invalid_response"
            valid_error = (isinstance(response, dict)
                and (response.get("error_code") is None or isinstance(response.get("error_code"), str))
                and (response.get("error_detail") is None or isinstance(response.get("error_detail"), str)))
            if valid_error and type(response.get("protocol_version")) is int and response["protocol_version"] == 1 and response.get("request_id") == row[0]:
                if response.get("op") == "send_result":
                    if (response.get("attempt_id") == attempt_id
                            and response.get("observer_session_id") == row[2]
                            and response.get("status") in {"accepted", "rejected", "unknown"}):
                        status = response["status"]
                        code = response.get("error_code")
                elif response.get("op") == "error":
                    code = response.get("error_code")
                    status = "rejected" if code in _PREEXECUTION else "unknown"
            return _finish(store, row[0], attempt_id, status, code, response.get("error_detail") if valid_error else None)
        return DispatchReport("blocked", candidates[0][0], error_code="no_eligible_request")


def _finish(store: Any, request_id: str, attempt_id: str, status: str,
            code: str | None, detail: str | None) -> DispatchReport:
    now = _timestamp(datetime.now(timezone.utc))
    with store.database.transaction() as uow:
        uow.execute("""UPDATE plux_replies SET status=?,error_code=?,updated_at=?
            WHERE request_id=? AND status='dispatching'""", (status, code, now, request_id))
        uow.execute("""UPDATE plux_delivery_attempts
            SET status=?,error_code=?,error_detail=?,finished_at=?
            WHERE attempt_id=? AND status='dispatching'""",
            (status, code, detail, now, attempt_id))
    return DispatchReport(status, request_id, attempt_id, code)