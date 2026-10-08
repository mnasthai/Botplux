from datetime import datetime, timedelta, timezone
import ctypes
import io
import json
import os
from pathlib import Path
import sqlite3
import struct
import tempfile
import unittest
from unittest.mock import patch
from contextlib import redirect_stdout

from wechat_receiver import cli
from wechat_receiver import transport as transport_module
from wechat_receiver.outbox import Outbox
from wechat_receiver.send_models import SendTextCommand
from wechat_receiver.sender import Sender, SenderLock
from wechat_receiver.transport import FrameError, NamedPipeTransport, TransportError, decode_frame, decode_payload, encode_message, validate_local_pipe_path


UTC = timezone.utc
NOW = datetime(2026, 9, 17, 12, tzinfo=UTC)


def command(request_id="r1", target="friend", text="你好\n😀"):
    return SendTextCommand(request_id, "account", "session", target, text, NOW, NOW + timedelta(minutes=5))


def hello(request):
    return {"op": "hello", "protocol_version": 1, "request_id": request["request_id"],
            "observer_session_id": "session", "target_version": "4.1.13.12", "mode": "send_enabled",
            "account_id": "account", "account_verified": True, "send_text": True, "max_text_bytes": 16384}


class FakeTransport:
    def __init__(self, handler=None):
        self.calls = []
        self.handler = handler

    def exchange(self, request):
        self.calls.append(request)
        if request["op"] == "hello_media":
            return {"op": "error", "protocol_version": 1, "request_id": request["request_id"],
                    "error_code": "unsupported_operation", "error_detail": "legacy endpoint"}
        if self.handler:
            return self.handler(request)
        if request["op"] == "hello":
            return hello(request)
        return {"op": "send_result", "protocol_version": 1, "request_id": request["request_id"],
                "attempt_id": request["attempt_id"], "observer_session_id": "session", "status": "submitted",
                "error_code": None, "error_detail": None}


class TransportCodecTests(unittest.TestCase):
    def test_utf8_newline_emoji_round_trip_and_exact_length(self):
        message = {"op": "x", "text": "你好\n😀"}
        frame = encode_message(message)
        self.assertEqual(struct.unpack("<I", frame[:4])[0], len(frame) - 4)
        self.assertEqual(decode_frame(frame), message)

    def test_frame_limits_invalid_json_and_duplicate_keys(self):
        with self.assertRaises(FrameError):
            encode_message({"x": "a" * 65536})
        with self.assertRaises(FrameError):
            decode_frame(struct.pack("<I", 65537) + b"x")
        with self.assertRaises(FrameError):
            decode_payload(b"{bad")
        with self.assertRaises(FrameError):
            decode_payload(b'{"a":1,"a":2}')
        with self.assertRaises(FrameError):
            decode_payload(b'{"text":"\\ud800"}')
        with self.assertRaises(FrameError):
            decode_payload(b'{"x":' + b'[' * 2000 + b'0' + b']' * 2000 + b'}')
        with self.assertRaises(FrameError):
            decode_payload(b'{"x":' + b'9' * 5000 + b'}')

    def test_pipe_must_be_local(self):
        self.assertEqual(validate_local_pipe_path(r"\\.\pipe\sender"), r"\\.\pipe\sender")
        for path in (r"\\server\pipe\sender", r"\\.\pipe\a\b", "sender"):
            with self.assertRaises(ValueError):
                validate_local_pipe_path(path)
        with self.assertRaises(ValueError):
            validate_local_pipe_path("\\\\.\\pipe\\bad\x00name")

    def test_response_setup_failure_after_write_is_uncertain(self):
        class Harness(NamedPipeTransport):
            def _open(self, deadline): return 1
            def _write_all(self, handle, frame, deadline): pass
            def _read_exact(self, handle, size, deadline):
                raise TransportError("event_failed", "event", stage="io", may_have_written=False)
            def _close_handle(self, handle): pass
        with self.assertRaises(TransportError) as result:
            Harness(r"\\.\pipe\test").exchange({"op": "hello"})
        self.assertTrue(result.exception.may_have_written)

    def test_elapsed_deadline_fails_before_starting_io(self):
        import time
        transport = NamedPipeTransport(r"\\.\pipe\test")
        with self.assertRaises(TransportError) as result:
            transport._io(None, bytearray(1), time.monotonic() - 1, write=True)
        self.assertFalse(result.exception.may_have_written)

    @unittest.skipUnless(os.name == "nt", "Windows OVERLAPPED cleanup")
    def test_keyboard_interrupt_cancels_and_reaps_pending_io(self):
        class Kernel:
            def __init__(self):
                self.cancelled = self.reaped = self.closed = 0
            def WriteFile(self, *args):
                ctypes.set_last_error(997)
                return False
            ReadFile = WriteFile
            def WaitForSingleObject(self, *args):
                raise KeyboardInterrupt
            def CancelIoEx(self, *args):
                self.cancelled += 1
                return True
            def GetOverlappedResult(self, *args):
                self.reaped += 1
                return False
            def CloseHandle(self, *args):
                self.closed += 1
                return True
        kernel = Kernel()
        overlapped = transport_module._OVERLAPPED(0, 0, 0, 0, 1)
        transport = NamedPipeTransport(r"\\.\pipe\test")
        transport._overlapped = lambda: (kernel, overlapped)
        import time
        with self.assertRaises(KeyboardInterrupt):
            transport._io(1, b"x", time.monotonic() + 1, write=True)
        self.assertEqual((kernel.cancelled, kernel.reaped, kernel.closed), (1, 1, 1))


class SenderTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        self.outbox = Outbox(self.db)

    def tearDown(self):
        self.db.close()

    def sender(self, transport, targets={"friend"}):
        return Sender(self.outbox, transport, expected_account_id="account", observer_session_id="session", allowed_targets=targets)

    def test_readonly_capability_never_sends_command(self):
        def handler(request):
            result = hello(request)
            result.update(mode="read_only", send_text=False, account_id=None, account_verified=False)
            return result
        transport = FakeTransport(handler)
        self.outbox.enqueue(command(), now=NOW)
        report = self.sender(transport).dispatch_once(now=NOW)
        self.assertEqual((report.status, report.error_code), ("blocked", "native_sender_unavailable"))
        self.assertEqual([x["op"] for x in transport.calls], ["hello_media", "hello"])
        self.assertEqual(self.outbox.get("r1")["status"], "queued")

    def test_corrupt_compact_command_is_rejected_before_any_transport(self):
        self.outbox.enqueue(command(), now=NOW)
        with self.db:
            self.db.execute("UPDATE outbox SET text='unexpected changed text' WHERE request_id='r1'")
        transport = FakeTransport()
        report = self.sender(transport).dispatch_once(now=NOW)
        self.assertEqual((report.status, report.error_code), ('rejected', 'invalid_queued_command'))
        self.assertEqual(transport.calls, [])
        self.assertEqual(self.db.execute('SELECT count(*) FROM send_attempts').fetchone()[0], 0)

    def test_protocol_boolean_is_rejected(self):
        def handler(request):
            result = hello(request); result["protocol_version"] = True; return result
        self.outbox.enqueue(command(), now=NOW)
        report = self.sender(FakeTransport(handler)).dispatch_once(now=NOW)
        self.assertEqual(report.error_code, "identity_mismatch")
        self.assertEqual(self.outbox.get("r1")["status"], "queued")

    def test_identity_mismatch_and_allowlist_block_before_claim(self):
        self.outbox.enqueue(command(), now=NOW)
        blocked_transport = FakeTransport()
        report = self.sender(blocked_transport, {"other"}).dispatch_once(now=NOW)
        self.assertEqual(report.error_code, "target_not_allowed")
        self.assertEqual(blocked_transport.calls, [])
        self.assertEqual(self.outbox.get("r1")["status"], "rejected")
        self.outbox.enqueue(command("r2"), now=NOW)
        def mismatch(request):
            result = hello(request); result["account_id"] = "other"; return result
        report = self.sender(FakeTransport(mismatch)).dispatch_once(now=NOW)
        self.assertEqual(report.error_code, "account_not_verified")
        self.assertEqual(self.outbox.get("r1")["attempts"], [])

    def test_attempt_is_durable_before_send_side_effect(self):
        self.outbox.enqueue(command(), now=NOW)
        transport = FakeTransport()
        def handler(request):
            if request["op"] == "hello": return hello(request)
            stored = self.outbox.get(request["request_id"])
            self.assertEqual(stored["status"], "dispatching")
            self.assertEqual(stored["active_attempt_id"], request["attempt_id"])
            return {"op": "send_result", "protocol_version": 1, "request_id": request["request_id"],
                    "attempt_id": request["attempt_id"], "observer_session_id": "session", "status": "submitted",
                    "error_code": None, "error_detail": None}
        transport.handler = handler
        report = self.sender(transport).dispatch_once(now=NOW)
        self.assertEqual(report.status, "submitted")

    def test_attempt_completion_uses_response_time_not_claim_time(self):
        self.outbox.enqueue(command(), now=NOW)
        with patch('wechat_receiver.sender.time.monotonic',
                   side_effect=(10.0, 10.0, 12.5)):
            report = self.sender(FakeTransport()).dispatch_once(now=NOW)
        self.assertEqual(report.status, "submitted")
        attempt = self.outbox.get("r1")["attempts"][0]
        started = datetime.fromisoformat(attempt["started_at"].replace("Z", "+00:00"))
        completed = datetime.fromisoformat(attempt["completed_at"].replace("Z", "+00:00"))
        self.assertEqual(NOW, started)
        self.assertEqual(NOW + timedelta(seconds=2.5), completed)

    def test_requested_command_is_selected_even_when_an_older_command_exists(self):
        self.outbox.enqueue(command("older"), now=NOW)
        self.outbox.enqueue(command("selected"), now=NOW)
        transport = FakeTransport()
        report = self.sender(transport).dispatch_once(request_id="selected", now=NOW)
        self.assertEqual((report.request_id, report.status), ("selected", "submitted"))
        self.assertEqual(self.outbox.get("older")["status"], "queued")
        sent = [item for item in transport.calls if item["op"] == "send_text"]
        self.assertEqual([item["request_id"] for item in sent], ["selected"])

    def test_expired_queue_is_updated_without_handshake(self):
        self.outbox.enqueue(command(), now=NOW)
        transport = FakeTransport()
        report = self.sender(transport).dispatch_once(now=NOW + timedelta(minutes=6))
        self.assertEqual(report.status, "idle")
        self.assertEqual(self.outbox.get("r1")["status"], "expired")
        self.assertEqual(transport.calls, [])

    def test_definite_prewrite_failure_is_rejected(self):
        def handler(request):
            if request["op"] == "hello": return hello(request)
            raise TransportError("invalid_request", "cannot encode", stage="encode", may_have_written=False)
        self.outbox.enqueue(command(), now=NOW)
        report = self.sender(FakeTransport(handler)).dispatch_once(now=NOW)
        self.assertEqual(report.status, "rejected")

    def test_unattributed_generic_error_after_write_is_unknown(self):
        def handler(request):
            if request["op"] == "hello": return hello(request)
            return {"op": "error", "protocol_version": 1, "request_id": None,
                    "error_code": "invalid_request", "error_detail": "bad"}
        self.outbox.enqueue(command(), now=NOW)
        report = self.sender(FakeTransport(handler)).dispatch_once(now=NOW)
        self.assertEqual(report.status, "unknown")

    def test_disconnect_timeout_and_invalid_identity_after_write_are_unknown(self):
        for kind in ("disconnect", "identity"):
            with self.subTest(kind=kind):
                db = sqlite3.connect(":memory:"); queue = Outbox(db); queue.enqueue(command(), now=NOW)
                def handler(request):
                    if request["op"] == "hello": return hello(request)
                    if kind == "disconnect":
                        raise TransportError("pipe_closed", "closed", stage="response", may_have_written=True)
                    result = {"op": "send_result", "protocol_version": 1, "request_id": "wrong",
                              "attempt_id": request["attempt_id"], "observer_session_id": "session", "status": "submitted",
                              "error_code": None, "error_detail": None}
                    return result
                report = Sender(queue, FakeTransport(handler), expected_account_id="account",
                                observer_session_id="session", allowed_targets={"friend"}).dispatch_once(now=NOW)
                self.assertEqual(report.status, "unknown")
                self.assertEqual(queue.get("r1")["status"], "unknown")
                db.close()

    def test_duplicate_dispatch_and_recovery_do_not_resend(self):
        self.outbox.enqueue(command(), now=NOW)
        transport = FakeTransport()
        first = self.sender(transport).dispatch_once(now=NOW)
        second = self.sender(transport).dispatch_once(now=NOW)
        self.assertEqual((first.status, second.status), ("submitted", "idle"))
        self.assertEqual([x["op"] for x in transport.calls].count("send_text"), 1)
        self.outbox.enqueue(command("r2"), now=NOW)
        self.outbox.claim_next("account", "session", now=NOW)
        self.assertEqual(self.outbox.recover_inflight(now=NOW), 1)  # only the interrupted dispatching r2
        self.assertEqual(self.outbox.get("r1")["status"], "submitted")
        calls = len(transport.calls)
        self.assertEqual(self.sender(transport).dispatch_once(now=NOW).status, "idle")
        self.assertEqual(len(transport.calls), calls)


class SenderCliTests(unittest.TestCase):
    def test_queued_status_cancel_and_separate_lock(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); database = root / "messages.sqlite3"; text_file = root / "text.txt"
            text_file.write_text("中文\n😀", encoding="utf-8")
            common = ["--database", str(database), "--account-id", "account", "--observer-session-id", "session"]
            output = io.StringIO()
            with redirect_stdout(output):
                code = cli.main(["send-text", *common, "--target", "friend", "--text-file", str(text_file), "--request-id", "cli-r1"])
            self.assertEqual(code, 0)
            self.assertFalse(json.loads(output.getvalue())["dispatch_attempted"])
            output = io.StringIO()
            with redirect_stdout(output):
                self.assertEqual(cli.main(["send-text", *common, "--target", "friend", "--text-file", str(text_file),
                                           "--request-id", "cli-r1"]), 0)
            db = sqlite3.connect(database)
            try:
                self.assertEqual(len(Outbox(db).list()), 1)
            finally:
                db.close()
            with SenderLock(database):
                with self.assertRaises(RuntimeError):
                    with SenderLock(database): pass
            with SenderLock(database):
                pass  # stale lock file is harmless; the OS lock was released
            output = io.StringIO()
            with redirect_stdout(output):
                self.assertEqual(cli.main(["send-status", "--database", str(database), "--request-id", "cli-r1"]), 0)
            self.assertEqual(json.loads(output.getvalue())["text"], "中文\n😀")
            output = io.StringIO()
            with redirect_stdout(output):
                self.assertEqual(cli.main(["send-cancel", "--database", str(database), "--request-id", "cli-r1"]), 0)
            self.assertEqual(json.loads(output.getvalue())["status"], "cancelled")


if __name__ == "__main__":
    unittest.main()
