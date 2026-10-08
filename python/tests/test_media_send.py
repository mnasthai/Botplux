from __future__ import annotations

from datetime import datetime, timedelta, timezone
import io
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
import wave

from wechat_receiver.media_spool import stage_media
from wechat_receiver.outbox import Outbox
from wechat_receiver.send_models import SendMediaCommand, SendTextCommand
from wechat_receiver.send_service import SendClient, SenderConfig
from wechat_receiver.sender import Sender
from wechat_receiver.store import Store
from wechat_receiver.transport import TransportError


NOW = datetime(2026, 9, 22, 4, tzinfo=timezone.utc)


def png_bytes() -> bytes:
    return (b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\rIHDR" + b"\x00" * 13 +
            b"\x00" * 4 + b"\x00\x00\x00\x00IEND\xaeB`\x82")


def wav_bytes(duration_ms: int) -> bytes:
    output = io.BytesIO()
    with wave.open(output, "wb") as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(16_000)
        stream.writeframes(b"\x00\x00" * (16_000 * duration_ms // 1000))
    return output.getvalue()


def media_command(staged, request_id: str = "media") -> SendMediaCommand:
    return SendMediaCommand(
        request_id, "account", "session", "friend", staged.media_kind,
        str(staged.path), staged.sha256, staged.media_bytes, staged.duration_ms,
        NOW, NOW + timedelta(minutes=5))


def hello_media(request: dict, *, image: bool, voice: bool, text: bool = True) -> dict:
    return {
        "op": "hello_media", "protocol_version": 1, "request_id": request["request_id"],
        "observer_session_id": "session", "target_version": "4.1.13.12",
        "mode": "send_enabled", "account_id": "account", "account_verified": True,
        "send_text": text, "max_text_bytes": 16384, "send_image": image,
        "send_voice": voice,
    }


class Transport:
    def __init__(self, *, image: bool, voice: bool, fail_after_write: bool = False):
        self.image = image
        self.voice = voice
        self.fail_after_write = fail_after_write
        self.calls: list[dict] = []

    def exchange(self, request: dict) -> dict:
        self.calls.append(request)
        if request["op"] == "hello_media":
            return hello_media(request, image=self.image, voice=self.voice)
        if self.fail_after_write:
            raise TransportError("pipe_disconnected", "lost", stage="read", may_have_written=True)
        return {
            "op": "send_result", "protocol_version": 1, "request_id": request["request_id"],
            "attempt_id": request["attempt_id"], "observer_session_id": "session",
            "status": "accepted", "error_code": None, "error_detail": None,
        }


class MediaSpoolTests(unittest.TestCase):
    def test_image_and_voice_validation_and_content_addressed_reuse(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            database = root / "messages.sqlite3"
            image = root / "source.any"
            image.write_bytes(png_bytes())
            first = stage_media(image, "image", database)
            second = stage_media(image, "image", database)
            self.assertEqual(first, second)
            self.assertEqual(first.path.parent, root / "media" / "outbound")
            self.assertEqual(first.path.name, first.sha256 + ".png")
            self.assertEqual(first.duration_ms, 0)

            voice = root / "voice.data"
            voice.write_bytes(wav_bytes(60))
            staged_voice = stage_media(voice, "voice", database)
            self.assertEqual(staged_voice.duration_ms, 60)
            self.assertEqual(staged_voice.path.suffix, ".silk")

            bad = root / "bad.silk"
            bad.write_bytes(b"\x02#!SILK_V3\x05\x00abc")
            with self.assertRaises(ValueError):
                stage_media(bad, "voice", database)
            truncated_png = root / "bad.png"
            truncated_png.write_bytes(b"\x89PNG\r\n\x1a\n")
            with self.assertRaisesRegex(ValueError, "IHDR/IEND"):
                stage_media(truncated_png, "image", database)


class MediaOutboxTests(unittest.TestCase):
    def test_union_storage_migrates_old_schema_and_keeps_text_canonical(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            db = sqlite3.connect(root / "messages.sqlite3")
            db.execute("""CREATE TABLE outbox(
                request_id TEXT PRIMARY KEY,command_json TEXT NOT NULL,fingerprint TEXT NOT NULL,
                expected_account_id TEXT NOT NULL,observer_session_id TEXT NOT NULL,target_id TEXT NOT NULL,
                text TEXT NOT NULL,created_at TEXT NOT NULL,expires_at TEXT NOT NULL,source_event_key TEXT,
                origin TEXT NOT NULL,protocol_version INTEGER NOT NULL,status TEXT NOT NULL,
                enqueued_at TEXT NOT NULL,updated_at TEXT NOT NULL,claimed_at TEXT,active_attempt_id TEXT,
                extra_json TEXT NOT NULL DEFAULT '{}')""")
            queue = Outbox(db)
            columns = {row[1] for row in db.execute("PRAGMA table_info(outbox)")}
            self.assertTrue({"command_kind", "payload_json"} <= columns)
            text = SendTextCommand("text", "account", "session", "friend", "body", NOW,
                                   NOW + timedelta(minutes=5))
            text_row = queue.enqueue(text, now=NOW)
            self.assertEqual(json.loads(text_row["command_json"]), text.to_dict())
            self.assertEqual(text_row["command_kind"], "text")

            source = root / "image"
            source.write_bytes(png_bytes())
            command = media_command(stage_media(source, "image", root / "messages.sqlite3"))
            row = queue.enqueue(command, now=NOW)
            self.assertEqual(row["text"], "")
            self.assertEqual(row["command_kind"], "image")
            self.assertEqual(json.loads(row["command_json"]), command.to_dict())
            with self.assertRaisesRegex(ValueError, "conflicts"):
                queue.enqueue(SendMediaCommand(
                    "media", "account", "session", "another", command.media_kind,
                    command.media_path, command.media_sha256, command.media_bytes,
                    command.duration_ms, command.created_at, command.expires_at))
            db.close()


class MediaClientAndSenderTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.database = self.root / "messages.sqlite3"
        self.log = self.root / "observer.jsonl"
        self.log.write_text(
            json.dumps({"kind": "observer_start", "session_id": "session",
                        "target_version": "4.1.13.12"}) + "\n" +
            json.dumps({"kind": "command_pipe_ready", "session_id": "session",
                        "pipe": r"\\.\pipe\wechatbot-session"}) + "\n", encoding="utf-8")
        self.config = SenderConfig(
            self.database, self.log, "account", frozenset({"friend"}))

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_client_restart_idempotence_uses_staged_bytes(self) -> None:
        source = self.root / "photo.bin"
        source.write_bytes(png_bytes())
        first = SendClient(self.config).enqueue_image("friend", source, request_id="stable")
        second = SendClient(self.config).enqueue_image("friend", source, request_id="stable")
        self.assertEqual(first["created_at"], second["created_at"])
        with self.assertRaisesRegex(ValueError, "command kind"):
            SendClient(self.config).enqueue("friend", "", request_id="stable")
        store = Store(self.database)
        try:
            row = Outbox(store.db).get("stable")
            payload = json.loads(row["payload_json"])
            self.assertEqual(Path(payload["media_path"]).read_bytes(), png_bytes())
            self.assertEqual(row["command_kind"], "image")
        finally:
            store.close()

        voice = self.root / "voice.wav"
        voice.write_bytes(wav_bytes(40))
        queued_voice = SendClient(self.config).enqueue_voice(
            "friend", voice, request_id="stable-voice")
        voice_payload = json.loads(queued_voice["payload_json"])
        self.assertEqual(queued_voice["command_kind"], "voice")
        self.assertEqual(voice_payload["duration_ms"], 40)
        self.assertEqual(Path(voice_payload["media_path"]).suffix, ".silk")

    def test_legacy_hello_fallback_disables_media_capabilities(self) -> None:
        class LegacyTransport:
            def __init__(self):
                self.calls = []

            def exchange(inner_self, request):
                inner_self.calls.append(request)
                if request["op"] == "hello_media":
                    return {"op": "error", "protocol_version": 1,
                            "request_id": request["request_id"],
                            "error_code": "unsupported_operation",
                            "error_detail": "legacy endpoint"}
                return {"op": "hello", "protocol_version": 1,
                        "request_id": request["request_id"],
                        "observer_session_id": "session", "target_version": "4.1.13.12",
                        "mode": "send_enabled", "account_id": "account",
                        "account_verified": True, "send_text": True,
                        "max_text_bytes": 16384}

        db = sqlite3.connect(":memory:")
        try:
            transport = LegacyTransport()
            capabilities = Sender(Outbox(db), transport, expected_account_id="account",
                observer_session_id="session", allowed_targets={"friend"}).probe()
            self.assertFalse(capabilities.send_image)
            self.assertFalse(capabilities.send_voice)
            self.assertEqual([call["op"] for call in transport.calls],
                             ["hello_media", "hello"])
        finally:
            db.close()

    def test_worker_skips_unsupported_media_and_dispatches_text(self) -> None:
        source = self.root / "photo"
        source.write_bytes(png_bytes())
        staged = stage_media(source, "image", self.database)
        db = sqlite3.connect(self.database)
        queue = Outbox(db)
        queue.enqueue(media_command(staged), now=NOW)
        queue.enqueue(SendTextCommand(
            "text", "account", "session", "friend", "body",
            NOW + timedelta(seconds=1), NOW + timedelta(minutes=5)), now=NOW)
        transport = Transport(image=False, voice=False)
        report = Sender(queue, transport, expected_account_id="account",
            observer_session_id="session", allowed_targets={"friend"},
            database_path=self.database).dispatch_once(now=NOW + timedelta(seconds=2))
        self.assertEqual((report.request_id, report.status), ("text", "accepted"))
        self.assertEqual(queue.get("media")["status"], "queued")
        self.assertEqual(queue.get("media")["attempts"], [])
        self.assertEqual([call["op"] for call in transport.calls], ["hello_media", "send_text"])
        db.close()

    def test_media_unknown_is_terminal_and_wire_is_flat(self) -> None:
        source = self.root / "voice"
        source.write_bytes(wav_bytes(20))
        staged = stage_media(source, "voice", self.database)
        db = sqlite3.connect(self.database)
        queue = Outbox(db)
        queue.enqueue(media_command(staged), now=NOW)
        transport = Transport(image=True, voice=True, fail_after_write=True)
        sender = Sender(queue, transport, expected_account_id="account",
            observer_session_id="session", allowed_targets={"friend"},
            database_path=self.database)
        try:
            first = sender.dispatch_once(now=NOW)
            self.assertEqual(first.status, "unknown")
            wire = transport.calls[-1]
            self.assertEqual(wire["op"], "send_media")
            self.assertEqual(wire["media_kind"], "voice")
            self.assertNotIn("text", wire)
            self.assertNotIn("command_kind", wire)
            second = sender.dispatch_once(now=NOW)
            self.assertEqual(second.status, "idle")
            self.assertEqual(len(queue.get("media")["attempts"]), 1)
        finally:
            db.close()


if __name__ == "__main__":
    unittest.main()
