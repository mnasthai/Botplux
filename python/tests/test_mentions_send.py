from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from wechat_receiver.backend import WeChatBackend
from wechat_receiver.outbox import Outbox
from wechat_receiver.send_models import SendTextCommand
from wechat_receiver.send_service import SendClient, SenderConfig
from wechat_receiver.sender import HelloCapabilities, Sender, SenderError


NOW = datetime(2026, 9, 18, 5, tzinfo=timezone.utc)
GROUP = "22913213991@chatroom"


def command(request_id: str = "mention", *, mentions: str = "member_1,778") -> SendTextCommand:
    return SendTextCommand(request_id, "account", "session", GROUP, "@成员 正文", NOW,
                           NOW + timedelta(minutes=5), at_user_list=mentions)


def hello(request: dict, *, group: bool | None = True,
          mention: bool | None = True) -> dict:
    result = {
        "op": "hello",
        "protocol_version": 1,
        "request_id": request["request_id"],
        "observer_session_id": "session",
        "target_version": "4.1.13.12",
        "mode": "send_enabled",
        "account_id": "account",
        "account_verified": True,
        "send_text": True,
        "max_text_bytes": 16384,
    }
    if group is not None:
        result["send_group_text"] = group
    if mention is not None:
        result["send_mention"] = mention
    return result


class Transport:
    def __init__(self, *, group: bool | None = True, mention: bool | None = True):
        self.group = group
        self.mention = mention
        self.calls: list[dict] = []

    def exchange(self, request: dict) -> dict:
        self.calls.append(request)
        if request["op"] == "hello_media":
            return {"op": "error", "protocol_version": 1, "request_id": request["request_id"],
                    "error_code": "unsupported_operation", "error_detail": "legacy endpoint"}
        if request["op"] == "hello":
            return hello(request, group=self.group, mention=self.mention)
        return {
            "op": "send_result",
            "protocol_version": 1,
            "request_id": request["request_id"],
            "attempt_id": request["attempt_id"],
            "observer_session_id": "session",
            "status": "accepted",
            "error_code": None,
            "error_detail": None,
        }


class MentionCommandTests(unittest.TestCase):
    def test_plain_fingerprint_stays_legacy_and_csv_is_canonical(self) -> None:
        plain = SendTextCommand("plain", "account", "session", "friend", "text", NOW,
                                NOW + timedelta(minutes=5))
        self.assertNotIn("at_user_list", plain.to_dict())
        expected = json.dumps(plain.to_dict(), ensure_ascii=True, sort_keys=True,
                              separators=(",", ":"))
        self.assertEqual(expected, plain.fingerprint())
        self.assertEqual(command().to_dict()["at_user_list"], "member_1,778")

    def test_csv_rejects_noncanonical_reserved_and_nonprivate_ids(self) -> None:
        invalid = (
            "member_1,", ",member_1", "member_1,member_1", "member 1", "filehelper",
            "notify@all", GROUP, "x" * 129, ",".join(f"u{i}" for i in range(17)),
        )
        for value in invalid:
            with self.subTest(value=value), self.assertRaises(ValueError):
                command(mentions=value)
        with self.assertRaises(ValueError):
            SendTextCommand("private", "account", "session", "friend", "text", NOW,
                            NOW + timedelta(minutes=5), at_user_list="member_1")


class MentionStorageTests(unittest.TestCase):
    def test_old_schema_migrates_in_place_and_old_hash_remains_valid(self) -> None:
        db = sqlite3.connect(":memory:")
        db.execute("""CREATE TABLE outbox(
            request_id TEXT PRIMARY KEY,command_json TEXT NOT NULL,fingerprint TEXT NOT NULL,
            expected_account_id TEXT NOT NULL,observer_session_id TEXT NOT NULL,target_id TEXT NOT NULL,
            text TEXT NOT NULL,created_at TEXT NOT NULL,expires_at TEXT NOT NULL,source_event_key TEXT,
            origin TEXT NOT NULL,protocol_version INTEGER NOT NULL,status TEXT NOT NULL,
            enqueued_at TEXT NOT NULL,updated_at TEXT NOT NULL,claimed_at TEXT,active_attempt_id TEXT)""")
        plain = SendTextCommand("legacy", "account", "session", "friend", "old", NOW,
                                NOW + timedelta(minutes=5))
        payload = plain.to_dict()
        canonical = plain.fingerprint()
        db.execute("INSERT INTO outbox VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (
            "legacy", "", hashlib.sha256(canonical.encode()).hexdigest(), "account", "session",
            "friend", "old", payload["created_at"], payload["expires_at"], None, "manual", 1,
            "queued", payload["created_at"], payload["created_at"], None, None))
        queue = Outbox(db)
        self.assertEqual(queue.get("legacy")["command_json"], canonical)
        self.assertEqual(db.execute("SELECT extra_json FROM outbox").fetchone()[0], "{}")
        db.close()

    def test_metadata_is_hashed_and_idempotency_detects_conflicts(self) -> None:
        db = sqlite3.connect(":memory:")
        queue = Outbox(db)
        stored = queue.enqueue(command(), now=NOW)
        self.assertEqual(json.loads(stored["extra_json"]), {"at_user_list": "member_1,778"})
        self.assertEqual(json.loads(stored["command_json"])["at_user_list"], "member_1,778")
        with self.assertRaisesRegex(ValueError, "conflicts"):
            queue.enqueue(command(mentions="member_2"), now=NOW)
        with db:
            db.execute("UPDATE outbox SET extra_json=? WHERE request_id=?",
                       ('{"future":"value"}', "mention"))
        with self.assertRaisesRegex(ValueError, "metadata"):
            queue.get("mention")
        db.close()


class MentionDispatchTests(unittest.TestCase):
    def setUp(self) -> None:
        self.db = sqlite3.connect(":memory:")
        self.queue = Outbox(self.db)

    def tearDown(self) -> None:
        self.db.close()

    def sender(self, transport: Transport) -> Sender:
        return Sender(self.queue, transport, expected_account_id="account",
                      observer_session_id="session", allowed_targets={GROUP, "friend"})

    def test_old_hello_leaves_mention_queued_and_sends_later_plain_text(self) -> None:
        self.queue.enqueue(command("a-rich"), now=NOW)
        plain = SendTextCommand("b-plain", "account", "session", "friend", "plain", NOW,
                                NOW + timedelta(minutes=5))
        self.queue.enqueue(plain, now=NOW)
        transport = Transport(group=None, mention=None)
        report = self.sender(transport).dispatch_once(now=NOW)
        self.assertEqual((report.request_id, report.status), ("b-plain", "accepted"))
        self.assertEqual(self.queue.get("a-rich")["status"], "queued")
        self.assertEqual(self.queue.get("a-rich")["attempts"], [])
        self.assertEqual([call["op"] for call in transport.calls],
                         ["hello_media", "hello", "send_text"])
        blocked = self.sender(transport).dispatch_once(request_id="a-rich", now=NOW)
        self.assertEqual((blocked.status, blocked.error_code),
                         ("blocked", "native_mention_sender_unavailable"))
        self.assertEqual(self.queue.get("a-rich")["attempts"], [])

    def test_new_hello_sends_rich_wire_command_with_csv_unchanged(self) -> None:
        self.queue.enqueue(command(), now=NOW)
        transport = Transport()
        report = self.sender(transport).dispatch_once(now=NOW)
        self.assertEqual(report.status, "accepted")
        wire = transport.calls[-1]
        self.assertEqual(wire["op"], "send_rich_text")
        self.assertEqual(wire["at_user_list"], "member_1,778")
        self.assertEqual(wire["target_id"], GROUP)

    def test_corrupt_metadata_is_rejected_before_transport_or_attempt(self) -> None:
        for corrupt in ('[]', '{"at_user_list":"member_1","at_user_list":"778"}'):
            with self.subTest(corrupt=corrupt):
                request_id = "bad-" + str(len(corrupt))
                self.queue.enqueue(command(request_id), now=NOW)
                with self.db:
                    self.db.execute("UPDATE outbox SET extra_json=? WHERE request_id=?",
                                    (corrupt, request_id))
                transport = Transport()
                report = self.sender(transport).dispatch_once(request_id=request_id, now=NOW)
                self.assertEqual((report.status, report.error_code),
                                 ("rejected", "invalid_queued_command"))
                self.assertEqual(transport.calls, [])
        self.assertEqual(self.db.execute("SELECT count(*) FROM send_attempts").fetchone()[0], 0)

    def test_hello_accepts_each_known_optional_combination_and_rejects_unknown(self) -> None:
        for group, mention in ((None, None), (True, None), (None, True), (True, True)):
            with self.subTest(group=group, mention=mention):
                capabilities = self.sender(Transport(group=group, mention=mention)).probe()
                self.assertEqual(capabilities.send_group_text, bool(group))
                self.assertEqual(capabilities.send_mention, bool(mention))

        class Unknown(Transport):
            def exchange(self, request: dict) -> dict:
                result = hello(request)
                result["future"] = True
                return result

        with self.assertRaises(SenderError):
            self.sender(Unknown()).probe()


class MentionPublicApiTests(unittest.TestCase):
    def test_sdk_enqueues_only_and_request_id_includes_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            log = root / "observer.jsonl"
            log.write_text(
                json.dumps({"kind": "observer_start", "session_id": "session",
                            "target_version": "4.1.13.12"}) + "\n" +
                json.dumps({"kind": "command_pipe_ready", "session_id": "session",
                            "pipe": r"\\.\pipe\wechatbot-session"}) + "\n", encoding="utf-8")
            config = SenderConfig(root / "messages.sqlite3", log, "account", frozenset({GROUP}))
            backend = WeChatBackend(config)
            receipt = asyncio.run(backend.send_text(
                GROUP, "body", request_id="sdk", mention_ids=("member_1", "778")))
            self.assertEqual(receipt.status, "queued")
            self.assertTrue(backend.capabilities.send_mention)
            capability = HelloCapabilities("session", "4.1.13.12", "send_enabled", "account",
                                           True, True, 16384, True, True)
            endpoint = type("Endpoint", (), {"session_id": "session"})()
            backend._on_state(endpoint, capability, None)
            self.assertTrue(backend.connection_state.send_mention_ready)
            db = sqlite3.connect(config.database_path)
            try:
                row = Outbox(db).get("sdk")
                self.assertEqual(json.loads(row["extra_json"])["at_user_list"], "member_1,778")
            finally:
                db.close()
            with self.assertRaisesRegex(ValueError, "existing content"):
                SendClient(config).enqueue(GROUP, "body", request_id="sdk",
                                           mention_ids=("member_2",))


if __name__ == "__main__":
    unittest.main()
