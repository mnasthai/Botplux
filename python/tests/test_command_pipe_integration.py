"""Real Windows IPC against an ordinary C++ test process, never WeChat.

Set WECHATBOT_TEST_PIPE_HOST to the built ObserverReaderTests.exe to enable.
"""
import ctypes
from ctypes import wintypes
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import sqlite3
import struct
import subprocess
import time
import unittest
import uuid

from wechat_receiver.outbox import Outbox
from wechat_receiver.send_models import SendMediaCommand, SendTextCommand
from wechat_receiver.sender import Sender, _outbound
from wechat_receiver.transport import NamedPipeTransport, TransportError, encode_message, decode_payload


HOST = os.environ.get("WECHATBOT_TEST_PIPE_HOST")


@unittest.skipUnless(os.name == "nt" and HOST, "set WECHATBOT_TEST_PIPE_HOST for real Windows IPC")
class CommandPipeIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not Path(HOST).is_file():
            raise RuntimeError(f"C++ test host does not exist: {HOST}")
        cls.pipe = rf"\\.\pipe\wechatbot-integration-{uuid.uuid4()}"
        cls.host = subprocess.Popen([HOST, "--serve-command-pipe", cls.pipe],
                                    creationflags=subprocess.CREATE_NO_WINDOW)
        cls.transport = NamedPipeTransport(cls.pipe, timeout=2)
        until = time.monotonic() + 3
        while True:
            try:
                cls.transport.exchange(cls.hello())
                break
            except TransportError:
                if time.monotonic() >= until or cls.host.poll() is not None:
                    cls.host.terminate()
                    cls.host.wait(timeout=3)
                    raise
                time.sleep(0.02)

    @classmethod
    def tearDownClass(cls):
        if cls.host.poll() is None:
            cls.host.terminate()
        cls.host.wait(timeout=3)

    @staticmethod
    def hello():
        return {"op": "hello", "protocol_version": 1, "request_id": str(uuid.uuid4())}

    @staticmethod
    def close(handle):
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
        kernel32.CloseHandle.restype = wintypes.BOOL
        kernel32.CloseHandle(handle)

    def test_hello_and_explicit_native_rejection(self):
        request = self.hello() | {"request_id": "中文😀"}
        response = self.transport.exchange(request)
        self.assertEqual(response["request_id"], request["request_id"])
        self.assertEqual(response["observer_session_id"], "test-session")
        self.assertEqual(response["mode"], "read_only")
        self.assertIs(response["send_text"], False)
        self.assertIs(response["send_group_text"], False)
        self.assertIs(response["account_verified"], False)
        now = datetime.now(timezone.utc)
        command = SendTextCommand("integration", "account", "test-session", "friend", "中文\n😀",
                                  now, now + timedelta(minutes=1))
        result = self.transport.exchange(command.to_dict() | {"op": "send_text", "attempt_id": "attempt"})
        self.assertEqual((result["status"], result["error_code"]), ("rejected", "native_sender_unavailable"))
        result = self.transport.exchange(command.to_dict() | {"op": "send_text", "attempt_id": "attempt",
                                                               "observer_session_id": "old-session"})
        self.assertEqual(result["error_code"], "session_mismatch")

    def test_python_dispatch_preserves_queue_without_attempt(self):
        db = sqlite3.connect(":memory:")
        try:
            queue = Outbox(db)
            now = datetime.now(timezone.utc)
            queue.enqueue(SendTextCommand("queued", "account", "test-session", "friend", "local only",
                                           now, now + timedelta(minutes=1)))
            report = Sender(queue, self.transport, expected_account_id="account",
                            observer_session_id="test-session", allowed_targets={"friend"}).dispatch_once()
            self.assertEqual((report.status, report.error_code), ("blocked", "native_sender_unavailable"))
            row = queue.get("queued")
            self.assertEqual(row["status"], "queued")
            self.assertEqual(row["attempts"], [])
        finally:
            db.close()

    def test_media_handshake_and_command_cross_language_contract(self):
        response = self.transport.exchange(self.hello() | {'op': 'hello_media'})
        self.assertEqual(response['op'], 'hello_media')
        self.assertIs(response['send_image'], False)
        self.assertIs(response['send_voice'], False)
        now = datetime.now(timezone.utc)
        for kind, duration in (('image', 0), ('voice', 2000)):
            command = SendMediaCommand(
                'integration-' + kind, 'account', 'test-session', 'friend', kind,
                str(Path('fixture.' + ('png' if kind == 'image' else 'silk')).resolve()),
                'a' * 64, 100, duration, now, now + timedelta(minutes=1))
            response = self.transport.exchange(_outbound(command, 'media-attempt'))
            self.assertEqual((response['status'], response['error_code']),
                             ('rejected', 'native_sender_unavailable'))

    def test_fragmented_request_and_invalid_frame_recovery(self):
        request = self.hello()
        frame = encode_message(request)
        deadline = time.monotonic() + 2
        handle = self.transport._open(deadline)
        try:
            # Split both the prefix and a body: the C++ server must accumulate.
            for part in (frame[:1], frame[1:3], frame[3:9], frame[9:]):
                self.transport._write_all(handle, part, deadline)
            prefix = self.transport._read_exact(handle, 4, deadline)
            body = self.transport._read_exact(handle, struct.unpack("<I", prefix)[0], deadline)
            self.assertEqual(decode_payload(body)["request_id"], request["request_id"])
        finally:
            self.close(handle)
        for length in (0, 65537):
            deadline = time.monotonic() + 2
            handle = self.transport._open(deadline)
            try:
                self.transport._write_all(handle, struct.pack("<I", length), deadline)
                prefix = self.transport._read_exact(handle, 4, deadline)
                body = self.transport._read_exact(handle, struct.unpack("<I", prefix)[0], deadline)
                self.assertEqual(json.loads(body)["error_code"], "invalid_frame")
            finally:
                self.close(handle)
        self.assertEqual(self.transport.exchange(self.hello())["op"], "hello")

    def test_partial_frame_timeout_cancels_io_and_next_client_works(self):
        deadline = time.monotonic() + 2
        handle = self.transport._open(deadline)
        try:
            self.transport._write_all(handle, struct.pack("<I", 100) + b"{", deadline)
            with self.assertRaises(TransportError) as result:
                self.transport._read_exact(handle, 4, time.monotonic() + 0.05)
            self.assertTrue(result.exception.may_have_written)
            self.assertEqual(result.exception.error_code, "timeout")
        finally:
            self.close(handle)
        self.assertEqual(self.transport.exchange(self.hello())["op"], "hello")


if __name__ == "__main__":
    unittest.main()
