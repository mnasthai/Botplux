from __future__ import annotations

from contextlib import redirect_stderr
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "tools" / "Inspect-MediaSamples.py"
SPEC = importlib.util.spec_from_file_location("inspect_media_samples_tool", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
TOOL = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(TOOL)


def buffer(data: bytes, *, status: str = "ok", declared: int | None = None,
           original: int | None = None, captured: int | None = None,
           encoded: str | None = None) -> dict:
    size = len(data) if captured is None else captured
    return {
        "status": status,
        "declared_bytes": len(data) if declared is None else declared,
        "original_bytes": len(data) if original is None else original,
        "captured_bytes": size,
        "hex": data.hex() if encoded is None else encoded,
    }


def missing() -> dict:
    return {
        "status": "missing",
        "declared_bytes": None,
        "original_bytes": None,
        "captured_bytes": 0,
        "hex": "",
    }


def sample(session: str, seq: int, source_seq: int, msg_type: int,
           field8: dict, field14: dict) -> dict:
    return {
        "kind": "media_receive_sample",
        "schema_version": 2,
        "session_id": session,
        "seq": seq,
        "call_id": seq + 1000,
        "observed_unix_ms": 1_800_000_000_000 + seq,
        "source_event_seq": source_seq,
        "msg_type": msg_type,
        "snapshot_limit_bytes": 8192,
        "field8": field8,
        "field14": field14,
    }


def append_read(status: str, original: int | None, captured: int) -> dict:
    return {
        "status": status,
        "original_bytes": original,
        "captured_bytes": captured,
    }


def send_sample(session: str, seq: int, call_id: int, source_type: int) -> dict:
    media_kind = "image" if source_type == 3 else "voice"
    result = {
        "kind": "send_submit_enter",
        "schema_version": 2,
        "source": "send_submit",
        "session_id": session,
        "seq": seq,
        "call_id": call_id,
        "tid": 123,
        "observed_unix_ms": 1_800_000_000_000 + seq,
        "tick": 500 + seq,
        "source_type": source_type,
        "source_subtype": 0,
        "source_vtable_rva": "0x8af1234" if source_type == 3 else "0x8af5678",
        "media_kind": media_kind,
        "target": "wxid-sensitive-target",
        "target_read": append_read("ok", 21, 21),
        "local_uuid": "sensitive-local-uuid",
        "local_uuid_read": append_read("ok", 20, 20),
        "source_object_address": "0xdeadbeef",
        "source_control_address": "0xcafebabe",
        "completion_readable": True,
        "completion_callbacks": [
            {"read_status": "ok", "payload": "0x111", "vtable": "0x222", "vtable_rva": "0x333"},
            {"read_status": "empty", "payload": "0x0", "vtable": None, "vtable_rva": None},
            {"read_status": "unreadable", "payload": None, "vtable": None, "vtable_rva": None},
        ],
        "root_address": "0x100",
        "executor_raw": "0x200",
        "executor_control": "0x300",
        "executor_inner": "0x400",
        "executor_flags": 1,
        "executor_bit0": True,
    }
    if source_type == 3:
        result.update(
            image_path=r"C:\sensitive\photo.jpg",
            image_path_read=append_read("ok", 44, 44),
            image_data_layout=append_read("ok", 2048, 0),
            image_width=640,
            image_height=480,
        )
    else:
        prefix = b"\x02#!SILK_V3voice-prefix"
        result.update(
            voice_prefix_hex=prefix.hex(),
            voice_data_read=append_read("ok", len(prefix), len(prefix)),
            source_voicelength=2345,
            source_voiceformat=4,
        )
    return result


def resource_base(session: str, kind: str, seq: int, msg_type: int) -> dict:
    return {
        "kind": kind,
        "schema_version": 2,
        "session_id": session,
        "seq": seq,
        "observed_unix_ms": 1_800_000_100_000 + seq,
        "msg_type": msg_type,
        "message_id": str(9_000_000 + seq),
        "from": "wxid-sensitive-from",
        "to": "sensitive-room@chatroom",
        "sender": "wxid-sensitive-sender",
    }


class MediaSampleInspectorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.log = self.root / "observer.jsonl"

    def tearDown(self) -> None:
        self.temp.cleanup()

    def write(self, records: list[object]) -> None:
        self.log.write_text(
            "".join(json.dumps(record, ensure_ascii=False) + "\n" for record in records),
            encoding="utf-8",
        )

    def test_latest_session_magic_correlation_and_sensitive_fields_are_not_reported(self) -> None:
        png = b"\x89PNG\r\n\x1a\nrest"
        silk = b"\x02#!SILK_V3payload"
        records = [
            {"kind": "observer_start", "session_id": "old-session"},
            sample("old-session", 2, 1, 3, buffer(b"\xff\xd8\xffold"), missing()),
            {"kind": "observer_start", "session_id": "new-session"},
            {
                "kind": "item", "session_id": "new-session", "seq": 10, "msg_type": 3,
                "content": "<msg aeskey='do-not-print' cdnurl='secret' />",
                "from": "wxid_private", "to": "room@chatroom", "message_id": "secret-id",
            },
            sample(
                "new-session", 11, 10, 3, buffer(png),
                buffer(b"\xff\xd8\xff", status="truncated", declared=20, original=20),
            ),
            {
                "kind": "item", "session_id": "new-session", "seq": 20, "msg_type": 34,
                "content": "raw voice XML must stay private", "aeskey": "voice-secret",
            },
            sample("new-session", 21, 20, 34, missing(), buffer(silk)),
            {"kind": "media_receive_sample", "schema_version": 99,
             "session_id": "new-session", "seq": 22, "source_event_seq": 20,
             "observed_unix_ms": 1, "msg_type": 34, "snapshot_limit_bytes": 8192,
             "field8": missing(), "field14": missing()},
        ]
        self.write(records)

        result = TOOL.inspect_media_samples(self.log)

        self.assertEqual("latest_observer_start", result["session"]["selection"])
        self.assertEqual(hashlib.sha256(b"new-session").hexdigest()[:16],
                         result["session"]["hash"])
        self.assertEqual(2, result["sample_count"])
        self.assertEqual({"unsupported_schema": 1}, result["rejected_samples"])
        self.assertEqual("PNG", result["samples"][0]["buffers"]["field8"]["magic"])
        self.assertTrue(result["samples"][0]["buffers"]["field8"]["buffer_complete"])
        self.assertFalse(result["samples"][0]["buffers"]["field14"]["buffer_complete"])
        self.assertEqual("SILK", result["samples"][1]["buffers"]["field14"]["magic"])
        self.assertTrue(all(item["item_msg_type_match"] for item in result["samples"]))
        rendered = json.dumps(result, ensure_ascii=False)
        for secret in ("new-session", "do-not-print", "cdnurl", "wxid_private",
                       "room@chatroom", "secret-id", "voice-secret", "raw voice XML"):
            self.assertNotIn(secret, rendered)
        self.assertIn("does not establish that a complete media file", result["completeness_note"])

    def test_exports_only_complete_buffers_is_idempotent_and_never_overwrites_conflict(self) -> None:
        session = "export-session"
        unknown = b"opaque-but-complete"
        amr = b"#!AMR\nvoice"
        no_declared = buffer(unknown)
        no_declared["declared_bytes"] = None
        invalid_hex = buffer(b"xx", encoded="zzzz")
        length_mismatch = buffer(b"abc", declared=4)
        records = [
            {"kind": "observer_start", "session_id": session},
            {"kind": "item", "session_id": session, "seq": 1, "msg_type": 3,
             "content": "private image XML"},
            sample(session, 10, 1, 3, no_declared, invalid_hex),
            {"kind": "item", "session_id": session, "seq": 2, "msg_type": 34,
             "content": "private voice XML"},
            sample(session, 20, 2, 34, length_mismatch, buffer(amr)),
        ]
        self.write(records)
        output = self.root / "exports"
        output.mkdir()
        tag = hashlib.sha256(session.encode()).hexdigest()[:16]
        conflict = output / f"media-{tag}-20-field14.amr"
        conflict.write_bytes(b"existing-different-data")

        first = TOOL.inspect_media_samples(self.log, session=session, output_dir=output)
        second = TOOL.inspect_media_samples(self.log, session=session, output_dir=output)

        image = first["samples"][0]["buffers"]
        voice = first["samples"][1]["buffers"]
        self.assertEqual("written", image["field8"]["export"]["status"])
        self.assertTrue(image["field8"]["buffer_complete"])
        self.assertIsNone(image["field8"]["declared_bytes"])
        self.assertIsNone(image["field14"]["export"])
        self.assertIsNone(voice["field8"]["export"])
        self.assertFalse(voice["field8"]["buffer_complete"])
        self.assertIn("declared_length_mismatch", voice["field8"]["errors"])
        self.assertEqual("conflict", voice["field14"]["export"]["status"])
        self.assertEqual(b"existing-different-data", conflict.read_bytes())
        self.assertEqual("reused", second["samples"][0]["buffers"]["field8"]["export"]["status"])
        exported = output / image["field8"]["export"]["file"]
        self.assertEqual(unknown, exported.read_bytes())
        self.assertEqual(2, len(list(output.iterdir())))

    def test_cli_requires_log_and_rejects_invalid_hex_without_export(self) -> None:
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as missing_log:
            TOOL.main([])
        self.assertEqual(2, missing_log.exception.code)

        session = "invalid-hex-session"
        self.write([
            {"kind": "observer_start", "session_id": session},
            sample(session, 3, 2, 3, buffer(b"a", encoded="0g"), missing()),
        ])
        output = self.root / "exports"
        result = TOOL.inspect_media_samples(self.log, output_dir=output)
        detail = result["samples"][0]["buffers"]["field8"]
        self.assertFalse(detail["hex_valid"])
        self.assertIn("invalid_hex", detail["errors"])
        self.assertEqual([], list(output.iterdir()))

    def test_export_open_failure_does_not_delete_file_created_by_a_racer(self) -> None:
        output = self.root / "exports"
        output.mkdir()
        destination = output / "media-race.bin"
        raced_content = b"created-by-another-writer"
        real_open = Path.open

        def fail_after_racer(path: Path, mode: str, *args, **kwargs):
            self.assertEqual((path, mode), (destination, "xb"))
            with real_open(path, "wb") as stream:
                stream.write(raced_content)
            raise PermissionError("simulated exclusive-open failure")

        with patch.object(Path, "open", autospec=True, side_effect=fail_after_racer):
            with self.assertRaises(PermissionError):
                TOOL._export_buffer(output, destination.name, b"our-data")

        self.assertEqual(raced_content, destination.read_bytes())

    def test_send_submit_media_is_bounded_correlated_redacted_and_never_exported(self) -> None:
        session = "send-sensitive-session"
        image = send_sample(session, 30, 9_000_001, 3)
        voice = send_sample(session, 40, 9_000_002, 34)
        records = [
            {"kind": "observer_start", "session_id": session},
            image,
            {
                "kind": "send_submit_return", "schema_version": 2, "source": "send_submit",
                "session_id": session, "seq": 31, "call_id": 9_000_001,
                "normal_return": True, "return_tick": 900,
            },
            voice,
            {
                "kind": "send_submit_return", "schema_version": 2, "source": "send_submit",
                "session_id": session, "seq": 41, "call_id": 9_000_002,
                "normal_return": True, "return_tick": 901,
            },
        ]
        # The observer budget is four samples independently for image and voice.
        for offset in range(4):
            records.append(send_sample(session, 50 + offset, 9_100_000 + offset, 3))
        self.write(records)
        output = self.root / "exports"

        result = TOOL.inspect_media_samples(self.log, output_dir=output)

        sends = result["send_submit"]
        self.assertEqual(5, sends["sample_count"])
        self.assertEqual({"image_budget_exceeded": 1}, sends["rejected_samples"])
        image_result, voice_result = sends["samples"][:2]
        self.assertEqual((3, "image", "0x8af1234", 640, 480),
                         (image_result["source_type"], image_result["media_kind"],
                          image_result["source_vtable_rva"], image_result["width"],
                          image_result["height"]))
        self.assertEqual("ok", image_result["image_path_read"]["status"])
        self.assertEqual((2048, 0, "unknown"),
                         (image_result["binary"]["original_bytes"],
                          image_result["binary"]["captured_bytes"],
                          image_result["binary"]["magic"]))
        self.assertEqual((34, "voice", 2345, 4, "SILK"),
                         (voice_result["source_type"], voice_result["media_kind"],
                          voice_result["voicelength"], voice_result["voiceformat"],
                          voice_result["binary"]["magic"]))
        self.assertTrue(voice_result["binary"]["prefix_valid"])
        self.assertEqual(1, image_result["normal_return_matches"])
        self.assertEqual(1, voice_result["normal_return_matches"])
        self.assertEqual("readable", image_result["completion"]["state"])
        self.assertEqual("readable", image_result["executor"]["state"])
        self.assertFalse(image_result["completion"]["submission_proven"])
        self.assertFalse(image_result["executor"]["submission_proven"])
        self.assertEqual("ok", image_result["target_read"]["status"])
        self.assertEqual("ok", image_result["local_uuid_read"]["status"])
        self.assertEqual([], list(output.iterdir()))

        rendered = json.dumps(sends, ensure_ascii=False)
        for secret in (
            session, r"C:\sensitive\photo.jpg", "wxid-sensitive-target",
            "sensitive-local-uuid", "0xdeadbeef", "0xcafebabe",
            image["completion_callbacks"][0]["payload"],
            voice["voice_prefix_hex"], "9000001", "9000002",
        ):
            self.assertNotIn(secret, rendered)
        self.assertIn("do not establish submission or delivery", sends["evidence_note"])

    def test_resource_events_follow_selected_session_and_expose_only_allowlisted_metadata(self) -> None:
        old_asset = resource_base("old-resource-session", "media_asset", 1, 34) | {
            "status": "available",
            "sha256": "a" * 64,
            "byte_length": 123,
            "asset_name": "a" * 64 + ".silk",
        }
        session = "new-resource-session"
        digest = "b" * 64
        asset = resource_base(session, "media_asset", 10, 34) | {
            "status": "available",
            "sha256": digest,
            "byte_length": 6694,
            "asset_name": digest + ".silk",
        }
        image_digest = "d" * 64
        image_asset = resource_base(session, "media_asset", 14, 3) | {
            "status": "available",
            "sha256": image_digest,
            "byte_length": 12_345,
            "asset_name": image_digest + ".jpg",
            "image_variant": "original",
            "resource_kind": 3,
            "resource_path_offset": 0x68,
            "width": 1600,
            "height": 1200,
        }
        encoded_digest = "e" * 64
        encoded_asset = resource_base(session, "media_encoded_asset", 15, 3) | {
            "status": "encoded",
            "sha256": encoded_digest,
            "byte_length": 20 * 1024 * 1024,
            "asset_name": encoded_digest + ".wxgf",
            "image_variant": "thumbnail",
            "resource_kind": 1,
            "resource_path_offset": 0x68,
            "width": 320,
            "height": 240,
        }
        candidate = resource_base(session, "media_image_candidate", 11, 3) | {
            "status": "pending",
            "resource_kind": 2,
            "resource_path_offset": 0x88,
            "capture_status": "stable_file_read",
            "file_size": 4096,
            "win32_error": 0,
            "byte_length": 4096,
            "plaintext_format": "png",
            "source_path": r"C:\private\sensitive-image.png",
            "sha256": "c" * 64,
            "prefix_hex": "89504e470d0a1a0a",
            "asset_published": True,
        }
        asset_error = resource_base(session, "media_asset_error", 12, 34) | {
            "status": "pending",
            "reason": "voice_format_or_framing_invalid",
        }
        records = [
            {"kind": "observer_start", "session_id": "old-resource-session"},
            old_asset,
            {"kind": "observer_start", "session_id": session},
            {
                "kind": "media_receive_enabled", "session_id": session,
                "voice": True, "image_candidates": False, "max_pending": 16,
                "voice_limit_bytes": 1024 * 1024,
                "image_limit_bytes": 32 * 1024 * 1024,
                "source_path": r"C:\must-not-leak",
            },
            {"kind": "media_receive_disabled", "session_id": session,
             "reason": "entry_signature_mismatch"},
            asset,
            image_asset,
            encoded_asset,
            candidate,
            asset_error,
            {"kind": "media_asset_dropped", "session_id": session, "total": 7},
            # Native status records intentionally do not carry schema_version.
            {"kind": "media_asset_error", "session_id": session,
             "reason": "writer_thread_failed"},
            # A schema-bearing asset with a non-native schema is counted but rejected.
            asset | {"seq": 13, "schema_version": 3},
            image_asset | {"seq": 16, "width": 16_385},
        ]
        self.write(records)

        result = TOOL.inspect_media_samples(self.log)

        resources = result["resource_events"]
        image_without_dimensions = {
            key: value for key, value in image_asset.items() if key not in ("width", "height")
        }
        optional_dimensions, reason = TOOL._validate_resource_event(image_without_dimensions)
        self.assertIsNone(reason)
        self.assertNotIn("width", optional_dimensions)
        too_large_encoded, reason = TOOL._validate_resource_event(
            encoded_asset | {"byte_length": 20 * 1024 * 1024 + 1}
        )
        self.assertIsNone(too_large_encoded)
        self.assertEqual("invalid_encoded_asset", reason)
        self.assertEqual("latest_observer_start", result["session"]["selection"])
        self.assertEqual(9, resources["event_count"])
        self.assertEqual({"invalid_media_asset": 2}, resources["rejected_events"])
        self.assertEqual(
            {
                "media_asset": 2,
                "media_asset_dropped": 1,
                "media_asset_error": 2,
                "media_encoded_asset": 1,
                "media_image_candidate": 1,
                "media_receive_disabled": 1,
                "media_receive_enabled": 1,
            },
            resources["summary"]["kinds"],
        )
        self.assertEqual(20_994_655, resources["summary"]["byte_length_total"])
        self.assertEqual(
            (True, False, 16),
            (resources["events"][0]["voice"],
             resources["events"][0]["image_candidates"],
             resources["events"][0]["max_pending"]),
        )
        self.assertEqual(
            (34, "voice", 6694),
            (resources["events"][2]["msg_type"],
             resources["events"][2]["media_kind"],
             resources["events"][2]["byte_length"]),
        )
        self.assertEqual(
            ("image", "jpg", "original", 3, 0x68, 1600, 1200),
            (resources["events"][3]["media_kind"],
             resources["events"][3]["plaintext_format"],
             resources["events"][3]["image_variant"],
             resources["events"][3]["resource_kind"],
             resources["events"][3]["resource_path_offset"],
             resources["events"][3]["width"], resources["events"][3]["height"]),
        )
        self.assertEqual(
            ("encoded", "wxgf", 20 * 1024 * 1024, "thumbnail", 320, 240),
            (resources["events"][4]["status"], resources["events"][4]["encoded_format"],
             resources["events"][4]["byte_length"], resources["events"][4]["image_variant"],
             resources["events"][4]["width"], resources["events"][4]["height"]),
        )
        self.assertEqual(
            (2, 0x88, "stable_file_read", "png", 4096, True),
            (resources["events"][5]["resource_kind"],
             resources["events"][5]["resource_path_offset"],
             resources["events"][5]["capture_status"],
             resources["events"][5]["plaintext_format"],
             resources["events"][5]["byte_length"],
             resources["events"][5]["asset_published"]),
        )
        rendered = json.dumps(resources, ensure_ascii=False)
        for secret in (
            "old-resource-session", session, "wxid-sensitive-from",
            "sensitive-room@chatroom", "wxid-sensitive-sender",
            r"C:\private\sensitive-image.png", r"C:\must-not-leak",
            digest, digest + ".silk", "c" * 64, "89504e470d0a1a0a",
            image_digest, image_digest + ".jpg", encoded_digest,
            encoded_digest + ".wxgf",
            asset["message_id"], candidate["message_id"],
        ):
            self.assertNotIn(secret, rendered)

    def test_resource_event_limit_accepts_boundary_and_rejects_one_more(self) -> None:
        session = "resource-limit-session"
        records = [{"kind": "observer_start", "session_id": session}]
        records.extend(
            {"kind": "media_asset_dropped", "session_id": session, "total": index + 1}
            for index in range(TOOL.MAX_RESOURCE_EVENTS)
        )
        self.write(records)
        result = TOOL.inspect_media_samples(self.log)
        self.assertEqual(TOOL.MAX_RESOURCE_EVENTS,
                         result["resource_events"]["event_count"])

        records.append({
            "kind": "media_asset_dropped", "session_id": session,
            "total": TOOL.MAX_RESOURCE_EVENTS + 1,
        })
        self.write(records)
        with self.assertRaisesRegex(TOOL.InspectionError, "resource event limit"):
            TOOL.inspect_media_samples(self.log)


if __name__ == "__main__":
    unittest.main()
