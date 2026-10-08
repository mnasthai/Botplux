"""Conservative JSONL conversion and a strict IRIS command endpoint."""
from __future__ import annotations

from datetime import datetime, timezone
from dataclasses import replace
from pathlib import Path
from typing import Any, Mapping
from xml.etree import ElementTree
import os
import re
import shutil
import uuid

from plux.api.models import (BaseMessage, ConnectionSnapshot, ContentQuality,
                             ImageMessage, MemberRef, MessageIdentity, TextMessage,
                             UnknownMessage, VoiceMessage)
from .transport import NamedPipeTransport, TransportError, validate_local_pipe_path

TARGET_VERSION = "4.1.13.12"
_GROUP_PREFIX = re.compile(r"^([A-Za-z0-9_-]{1,128}):\n")
_COMPLETE = {"ok", "empty"}
_KNOWN_CONTROLS = {"observer_start", "command_pipe_ready", "command_pipe_error",
                   "native_sender_disabled", "observer_disabled", "dropped"}
_PNG = b"\x89PNG\r\n\x1a\n"
_JPEG = b"\xff\xd8\xff"
_SILK = b"#!SILK_V3"


def native_media_extension(source: Path, kind: str) -> str:
    """Return the extension the native sender requires for this container.

    The backend accepts only `.png`/`.jpg`/`.jpeg` images and `.silk` voice, so
    an unsupported container is refused here rather than after a send attempt.
    """
    with Path(source).open("rb") as stream:
        head = stream.read(16)
    if kind == "voice":
        if head.startswith(b"\x02"):
            head = head[1:]
        if not head.startswith(_SILK):
            raise ValueError("voice media must be a SILK stream")
        return ".silk"
    if head.startswith(_PNG):
        return ".png"
    if head.startswith(_JPEG):
        return ".jpg"
    raise ValueError("image media must be PNG or JPEG")


def native_media_path(root: str | Path, source: str | Path, kind: str, sha256: str) -> Path:
    """Place a digest-verified copy at `<root>/<sha256><ext>`.

    The native host only reads a media file whose parent directory is exactly
    the media root and whose stem equals the payload digest, so the published
    asset layout cannot be handed over directly.
    """
    root = Path(root).resolve()
    source = Path(source).resolve(strict=True)
    target = root / f"{sha256}{native_media_extension(source, kind)}"
    root.mkdir(parents=True, exist_ok=True)
    if target.exists():
        if target.stat().st_size != source.stat().st_size:
            raise ValueError("native media export does not match its content address")
        return target
    temporary = target.with_name(target.name + ".part")
    try:
        os.link(source, target)
    except OSError:
        try:
            shutil.copyfile(source, temporary)
            os.replace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)
    return target


def _status(record: Mapping[str, Any], name: str, diagnostic: str) -> str:
    detail = record.get(diagnostic)
    if isinstance(detail, Mapping) and isinstance(detail.get("status"), str):
        return detail["status"]
    return "legacy_unknown_empty" if record.get(name) == "" else "legacy_unverified"


def _mentions(source: str | None, status: str, account: str, conversation: str) -> tuple[tuple[MemberRef, ...], str, str | None]:
    if status not in _COMPLETE or source is None:
        return (), "unknown", None
    if "<!doctype" in source.lower() or "<!entity" in source.lower():
        return (), "unknown", "unsafe_msg_source_xml"
    try:
        root = ElementTree.fromstring(source)
    except (ElementTree.ParseError, ValueError):
        return (), "unknown", "invalid_msg_source_xml"
    if root.tag != "msgsource":
        return (), "unknown", "unexpected_msg_source_root"
    fields = [node for node in root if node.tag == "atuserlist"]
    if len(fields) != 1 or not fields[0].text:
        return (), "unknown", None if not fields else "ambiguous_atuserlist"
    ids = [value.strip() for value in fields[0].text.split(",") if value.strip()]
    refs = tuple(MemberRef(account, value, conversation) for value in ids)
    if not refs:
        return (), "unknown", None
    return refs, "explicit_self" if account in ids else "explicit_other", None


def parse_message(record: Mapping[str, Any], *, account: str, session: str,
                  raw_ref: str, history_status: str = "unknown",
                  ingestion_status: str = "unknown") -> BaseMessage | None:
    """Convert only a message event. Unknown and damaged content stays explicit."""
    if not isinstance(record, Mapping) or record.get("kind") not in {"item", "outbound_item"}:
        return None
    issues: list[str] = []
    seq = record.get("seq")
    if type(seq) is not int or seq < 0:
        issues.append("missing_or_invalid_seq")
        event_key = raw_ref
    else:
        event_key = f"{session}:{seq}"
    ms = record.get("observed_unix_ms")
    if type(ms) is int and 0 <= ms <= 253402300799999:
        observed = datetime.fromtimestamp(ms / 1000, timezone.utc)
    else:
        observed = datetime.now(timezone.utc)
        issues.append("missing_observed_time")
    read = {key: _status(record, key, diagnostic) for key, diagnostic in
            (("from", "from_read"), ("to", "to_read"), ("content", "content_read"),
             ("msg_source", "source_read"))}
    for key, status in read.items():
        if status not in _COMPLETE:
            issues.append(f"{key}_{status}")
    raw_content = record.get("content")
    content = raw_content if isinstance(raw_content, str) and read["content"] in _COMPLETE else None
    from_id = record.get("from") if isinstance(record.get("from"), str) and read["from"] in _COMPLETE else None
    to_id = record.get("to") if isinstance(record.get("to"), str) and read["to"] in _COMPLETE else None
    outbound = record.get("kind") == "outbound_item"
    conversation = ""
    actor = None
    direction = "outbound_request" if outbound else "unknown"
    if outbound:
        conversation = to_id or ""
        actor = account
    elif from_id and from_id.endswith("@chatroom"):
        conversation = from_id
        if content is not None:
            match = _GROUP_PREFIX.match(content)
            if match:
                actor, content = match.group(1), content[match.end():]
            else:
                issues.append("group_sender_prefix_missing")
        if actor and to_id == account:
            direction = "self_sync" if actor == account else "inbound"
    elif from_id:
        actor = from_id
        if from_id == account:
            direction = "self_sync"
            conversation = to_id or ""
        elif to_id == account:
            direction = "inbound"
            conversation = from_id
    if record.get("vtable_match") is False:
        issues.append("object_validation_failed")
        direction = "unknown"
        content = None
    mentions, mentions_status, mention_issue = _mentions(
        record.get("msg_source") if isinstance(record.get("msg_source"), str) else None,
        read["msg_source"], account, conversation)
    if mention_issue:
        issues.append(mention_issue)
    if history_status not in {"unknown", "realtime", "backlog"}:
        history_status = "unknown"
    quality = ContentQuality(
        status="ok" if content is not None and not issues else "uncertain",
        mentions_status=mentions_status, history_status=history_status,
        issues=tuple(issues), ingestion_status=ingestion_status)
    identity = MessageIdentity(account, conversation, actor, session, direction)
    common = dict(event_key=event_key, identity=identity, observed_at=observed, quality=quality)
    msg_type = record.get("msg_type")
    if type(msg_type) is not int:
        msg_type = None
    if msg_type == 1 and content is not None:
        return TextMessage(**common, text=content, mentions=mentions)
    media_key = record.get("media_key")
    if not isinstance(media_key, str):
        fields = record.get("raw_fields")
        candidate = fields.get("12") if isinstance(fields, Mapping) else None
        media_key = f"{session}:msgid:{candidate}" if isinstance(candidate, str) and candidate else None
    if msg_type == 3:
        return ImageMessage(**common, media_key=media_key)
    if msg_type == 34:
        return VoiceMessage(**common, media_key=media_key)
    if content is None:
        issues.append("content_unavailable")
    return UnknownMessage(**common, raw_type=str(msg_type), raw_ref=raw_ref, issues=tuple(issues))


class ObserverAdapter:
    """Local endpoint discovery and handshake; no implicit account trust."""
    def __init__(self, pipe: str | None = None, *, expected_account: str = "",
                 expected_session: str | None = None, timeout: float = 5.0,
                 enabled: bool = True):
        if enabled and not expected_account:
            raise ValueError("expected_account is required")
        self.enabled = enabled
        self.expected_account = expected_account
        self.expected_session = expected_session
        self.pipe = validate_local_pipe_path(pipe) if pipe is not None else None
        self.timeout = timeout
        self._generation = 0
        self._retired_sessions: set[str] = set()
        self._snapshot = ConnectionSnapshot(account=expected_account)
        self._transport = NamedPipeTransport(self.pipe, timeout=timeout) if self.pipe else None

    def observe_control(self, record: Mapping[str, Any]) -> None:
        if not self.enabled:
            return
        kind = record.get("kind")
        session = record.get("session_id")
        if kind not in _KNOWN_CONTROLS or not isinstance(session, str) or not session:
            return
        if kind == "observer_start":
            if session in self._retired_sessions:
                return
            if self.expected_session is None and session != self._snapshot.native_session:
                if self._snapshot.native_session:
                    self._retired_sessions.add(self._snapshot.native_session)
                self._generation += 1
                self.pipe = None
                self._transport = None
                self._snapshot = ConnectionSnapshot(account=self.expected_account, native_session=session,
                                                    source="observer_start", phase="disconnected",
                                                    generation=self._generation)
            return
        current = self.expected_session or self._snapshot.native_session
        if current and session != current:
            return
        if kind == "command_pipe_ready":
            pipe = record.get("pipe")
            if record.get("protocol_version") != 1 or not isinstance(pipe, str):
                return
            try:
                self.pipe = validate_local_pipe_path(pipe)
            except ValueError:
                return
            self._transport = NamedPipeTransport(self.pipe, timeout=self.timeout)
            self._snapshot = ConnectionSnapshot(account=self.expected_account, native_session=session,
                                                source="command_pipe_ready", phase="discovered",
                                                generation=self._generation)
        elif kind in {"command_pipe_error", "native_sender_disabled", "observer_disabled"}:
            self._snapshot = ConnectionSnapshot(account=self.expected_account, native_session=session,
                                                source=kind, phase="disconnected",
                                                generation=self._generation, issues=(kind,))

    def exchange(self, request: dict[str, Any]) -> dict[str, Any]:
        if not self.enabled or self._transport is None:
            raise TransportError("pipe_unavailable", "no command pipe has been discovered", stage="connect")
        return self._transport.exchange(request)

    def probe(self) -> ConnectionSnapshot:
        if not self.enabled:
            return self._snapshot
        try:
            return self._probe()
        except (TransportError, ValueError) as exc:
            self._snapshot = replace(self._snapshot, sampled_at=datetime.now(timezone.utc),
                                     phase="disconnected", can_send=False,
                                     issues=(getattr(exc, "error_code", "probe_failed"),))
            raise

    def _probe(self) -> ConnectionSnapshot:
        request_id = str(uuid.uuid4())
        response = self.exchange({"op": "hello_media", "protocol_version": 1, "request_id": request_id})
        media = True
        if not isinstance(response, dict):
            raise ValueError("native hello is not an object")
        if response.get("op") == "error":
            if response.get("error_code") != "unsupported_operation":
                raise ValueError(f"native hello failed: {response.get('error_code')}")
            media = False
            request_id = str(uuid.uuid4())
            response = self.exchange({"op": "hello", "protocol_version": 1, "request_id": request_id})
        if not isinstance(response, dict):
            raise ValueError("native hello is not an object")
        required = {"op", "protocol_version", "request_id", "observer_session_id",
                    "target_version", "mode", "account_id", "account_verified",
                    "send_text", "max_text_bytes"}
        optional = {"send_group_text", "send_mention", "send_quote"}
        if media:
            required |= {"send_image", "send_voice"}
        unknown = sorted(set(response) - required - optional)
        absent = sorted(required - set(response))
        if unknown or absent:
            raise ValueError(f"unexpected native hello fields: unknown={unknown} missing={absent}")
        if response["op"] != ("hello_media" if media else "hello") or type(response["protocol_version"]) is not int or response["protocol_version"] != 1 or response["request_id"] != request_id:
            raise ValueError("native hello identity mismatch")
        session = response["observer_session_id"]
        if not isinstance(session, str) or not session or (self.expected_session and session != self.expected_session):
            raise ValueError("native session mismatch")
        if self._snapshot.native_session and session != self._snapshot.native_session:
            raise ValueError("stale command pipe session")
        if response["target_version"] != TARGET_VERSION:
            raise ValueError("unsupported native target version")
        if response["account_id"] is not None and not isinstance(response["account_id"], str):
            raise ValueError("invalid native account identity")
        if response["mode"] not in {"read_only", "send_enabled"}:
            raise ValueError("invalid native mode")
        for field in {"account_verified", "send_text"} | optional | ({"send_image", "send_voice"} if media else set()):
            if field in response and type(response[field]) is not bool:
                raise ValueError(f"invalid native capability: {field}")
        limit = response["max_text_bytes"]
        if type(limit) is not int or not 1 <= limit <= 65536:
            raise ValueError("invalid native text limit")
        capabilities = frozenset(name for name in
                                 ("send_text", "send_group_text", "send_mention", "send_quote", "send_image", "send_voice")
                                 if response.get(name) is True)
        verified = response["account_verified"] and response["account_id"] == self.expected_account
        can_send = verified and response["mode"] == "send_enabled" and bool(capabilities)
        self._snapshot = ConnectionSnapshot(
            account=response["account_id"], native_session=session,
            sampled_at=datetime.now(timezone.utc), source="hello_media" if media else "hello",
            phase="ready" if can_send else "read_only", capabilities=capabilities,
            can_send=can_send, generation=self._generation,
            issues=() if verified else ("account_not_verified",))
        return self._snapshot

    def connection(self) -> ConnectionSnapshot:
        return self._snapshot