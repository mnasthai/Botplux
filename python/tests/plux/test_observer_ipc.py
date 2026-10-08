"""Cross-language IRIS adapter test against the ordinary C++ command-pipe host.

The host is a read-only test process. This never attaches to or sends through WeChat.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import os
import subprocess
import time
import unittest
import uuid

from plux.adapters.wechat_observer import ObserverAdapter
from plux.adapters.wechat_observer.transport import TransportError


HOST = Path(os.environ.get("WECHATBOT_TEST_PIPE_HOST",
            r"<C++ 仓库>\build\observer-cmake\Release\bin\ObserverReaderTests.exe"))


@unittest.skipUnless(os.name == "nt" and HOST.is_file(), "ordinary C++ test host unavailable")
class ObserverIpcTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.pipe = rf"\\.\pipe\plux-adapter-test-{uuid.uuid4()}"
        cls.host = subprocess.Popen([str(HOST), "--serve-command-pipe", cls.pipe],
                                    creationflags=subprocess.CREATE_NO_WINDOW)
        cls.adapter = ObserverAdapter(cls.pipe, expected_account="self", expected_session="test-session")
        deadline = time.monotonic() + 4
        while True:
            try:
                cls.adapter.probe()
                break
            except (TransportError, ValueError):
                if time.monotonic() >= deadline or cls.host.poll() is not None:
                    cls.host.terminate()
                    cls.host.wait(timeout=3)
                    raise
                time.sleep(0.025)

    @classmethod
    def tearDownClass(cls):
        if cls.host.poll() is None:
            cls.host.terminate()
        cls.host.wait(timeout=3)

    def test_handshake_is_read_only_and_identity_is_explicit(self):
        snapshot = self.adapter.connection()
        self.assertEqual(snapshot.native_session, "test-session")
        self.assertEqual(snapshot.phase, "read_only")
        self.assertFalse(snapshot.can_send)
        self.assertEqual(snapshot.account, None)
        self.assertEqual(snapshot.issues, ("account_not_verified",))

    def test_command_is_rejected_by_ordinary_native_host(self):
        now = datetime.now(timezone.utc)
        result = self.adapter.exchange({
            "op": "send_text", "protocol_version": 1, "request_id": "plux-test-request",
            "attempt_id": "plux-test-attempt", "expected_account_id": "self",
            "observer_session_id": "test-session", "target_id": "friend",
            "text": "中文\n😀", "created_at": now.isoformat().replace("+00:00", "Z"),
            "expires_at": now.replace(year=now.year + 1).isoformat().replace("+00:00", "Z"),
            "source_event_key": None, "origin": "manual"})
        self.assertEqual(result["status"], "rejected")
        self.assertEqual(result["error_code"], "native_sender_unavailable")


if __name__ == "__main__":
    unittest.main()