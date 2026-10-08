from datetime import datetime, timedelta, timezone
import hashlib
import sqlite3
import tempfile
import threading
import unittest

from wechat_receiver.outbox import Outbox, initialize_outbox
from wechat_receiver.send_models import SendTextCommand


UTC = timezone.utc
START = datetime(2026, 9, 17, 1, tzinfo=UTC)


def command(request_id="r1", **changes):
    values = dict(request_id=request_id, expected_account_id="account", observer_session_id="session",
                  target_id="friend", text="你好\n😀", created_at=START, expires_at=START + timedelta(minutes=5),
                  source_event_key="session:1", origin="manual")
    values.update(changes)
    return SendTextCommand(**values)


class OutboxTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        self.outbox = Outbox(self.db)

    def tearDown(self):
        self.db.close()

    def test_command_validation(self):
        with self.assertRaises(ValueError):
            command(text="")
        with self.assertRaises(ValueError):
            command(expires_at=START)
        with self.assertRaises(ValueError):
            command(origin="script")
        with self.assertRaises(ValueError):
            command(protocol_version=2)
        with self.assertRaises(ValueError):
            command(target_id="friend\x00id")
        with self.assertRaises(ValueError):
            command(text="bad\ud800")
        self.assertTrue(command().fingerprint())

    def test_text_has_a_local_not_wechat_size_limit(self):
        with self.assertRaises(ValueError):
            command(text="x" * (16 * 1024 + 1))

    def test_enqueue_idempotency_and_conflict(self):
        first = self.outbox.enqueue(command(), now=START)
        second = self.outbox.enqueue(command(), now=START)
        self.assertEqual(first["request_id"], second["request_id"])
        with self.assertRaises(ValueError):
            self.outbox.enqueue(command(text="different"), now=START)

    def test_new_rows_store_text_once_and_rebuild_public_command_json(self):
        value = command(text="one physical body 你好😀")
        stored = self.outbox.enqueue(value, now=START)
        physical = self.db.execute(
            "SELECT command_json,fingerprint,text FROM outbox WHERE request_id=?",
            (value.request_id,)).fetchone()
        self.assertEqual(physical["command_json"], "")
        self.assertEqual(physical["text"], value.text)
        self.assertEqual(physical["fingerprint"],
                         hashlib.sha256(value.fingerprint().encode("utf-8")).hexdigest())
        self.assertEqual(stored["command_json"], value.fingerprint())
        self.assertEqual(self.outbox.list()[0]["command_json"], value.fingerprint())

    def test_enqueue_in_transaction_never_commits_and_rolls_back_atomically(self):
        with self.assertRaises(RuntimeError):
            self.outbox.enqueue_in_transaction(command("outside"), now=START)

        self.db.execute("BEGIN IMMEDIATE")
        row = self.outbox.enqueue_in_transaction(command("atomic"), now=START)
        self.assertEqual(row["request_id"], "atomic")
        self.assertTrue(self.db.in_transaction)
        with self.assertRaises(ValueError):
            self.outbox.enqueue_in_transaction(command("atomic", text="conflict"), now=START)
        self.assertTrue(self.db.in_transaction)
        self.db.rollback()
        with self.assertRaises(KeyError):
            self.outbox.get("atomic")

    def test_legacy_full_json_fingerprint_is_compatible_until_explicit_compaction(self):
        value = command("legacy")
        canonical = value.fingerprint()
        self.outbox.enqueue(value, now=START)
        with self.db:
            self.db.execute("UPDATE outbox SET command_json=?,fingerprint=? WHERE request_id=?",
                            (canonical, canonical, value.request_id))

        self.assertEqual(self.outbox.get(value.request_id)["command_json"], canonical)
        self.assertEqual(self.outbox.list()[0]["command_json"], canonical)
        self.outbox.enqueue(value, now=START)
        physical = self.db.execute(
            "SELECT command_json,fingerprint FROM outbox WHERE request_id=?",
            (value.request_id,)).fetchone()
        self.assertEqual((physical["command_json"], physical["fingerprint"]),
                         (canonical, canonical))
        with self.assertRaises(ValueError):
            self.outbox.enqueue(command("legacy", text="different"), now=START)

        self.assertEqual(self.outbox.compact_storage(), 1)
        physical = self.db.execute(
            "SELECT command_json,fingerprint FROM outbox WHERE request_id=?",
            (value.request_id,)).fetchone()
        self.assertEqual(physical["command_json"], "")
        self.assertEqual(physical["fingerprint"],
                         hashlib.sha256(canonical.encode("utf-8")).hexdigest())
        self.assertEqual(self.outbox.compact_storage(), 0)

    def test_inconsistent_legacy_json_is_not_healed_and_aborts_compaction(self):
        valid = command("a-valid")
        damaged = command("z-damaged")
        different_json = command("z-damaged", target_id="different").fingerprint()
        self.outbox.enqueue(valid, now=START)
        self.outbox.enqueue(damaged, now=START)
        with self.db:
            self.db.execute("UPDATE outbox SET command_json=?,fingerprint=? WHERE request_id=?",
                            (valid.fingerprint(), valid.fingerprint(), valid.request_id))
            self.db.execute("UPDATE outbox SET command_json=?,fingerprint=? WHERE request_id=?",
                            (different_json, different_json, damaged.request_id))

        with self.assertRaisesRegex(ValueError, "z-damaged"):
            self.outbox.get(damaged.request_id)
        with self.assertRaisesRegex(ValueError, "z-damaged"):
            self.outbox.compact_storage()
        rows = dict(self.db.execute(
            "SELECT request_id,command_json FROM outbox ORDER BY request_id").fetchall())
        self.assertEqual(rows[valid.request_id], valid.fingerprint())
        self.assertEqual(rows[damaged.request_id], different_json)

    def test_inconsistent_compact_digest_is_not_healed(self):
        value = command("damaged-digest")
        self.outbox.enqueue(value, now=START)
        with self.db:
            self.db.execute("UPDATE outbox SET fingerprint=? WHERE request_id=?",
                            ("0" * 64, value.request_id))

        with self.assertRaisesRegex(ValueError, "damaged-digest"):
            self.outbox.get(value.request_id)
        with self.assertRaisesRegex(ValueError, "damaged-digest"):
            self.outbox.compact_storage()
        stored = self.db.execute(
            "SELECT command_json,fingerprint FROM outbox WHERE request_id=?",
            (value.request_id,)).fetchone()
        self.assertEqual((stored["command_json"], stored["fingerprint"]),
                         ("", "0" * 64))

    def test_exact_claim_never_takes_another_queued_command(self):
        self.outbox.enqueue(command("older"), now=START)
        self.outbox.enqueue(command("selected"), now=START)
        self.assertIsNone(self.outbox.claim_next("account", "session", request_id="missing", now=START))
        selected = self.outbox.claim_next("account", "session", request_id="selected", now=START)
        self.assertEqual(selected["request_id"], "selected")
        self.assertEqual(self.outbox.get("older")["status"], "queued")
        self.assertEqual(self.outbox.get("older")["attempts"], [])

    def test_expiration_without_a_native_claim_preserves_inflight(self):
        self.outbox.enqueue(command("queued"), now=START)
        self.outbox.enqueue(command("inflight"), now=START)
        self.outbox.claim_next("account", "session", request_id="inflight", now=START)
        self.assertEqual(self.outbox.expire_queued(now=START + timedelta(minutes=5)), 1)
        self.assertEqual(self.outbox.get("queued")["status"], "expired")
        self.assertEqual(self.outbox.get("queued")["attempts"], [])
        self.assertEqual(self.outbox.get("inflight")["status"], "dispatching")

    def test_claim_result_and_evidence(self):
        self.outbox.enqueue(command(), now=START)
        self.assertIsNone(self.outbox.claim_next("other", "session", now=START))
        claim = self.outbox.claim_next("account", "session", now=START)
        self.assertEqual(claim["status"], "dispatching")
        attempt = claim["attempt_id"]
        self.outbox.record_result("r1", attempt, "accepted", now=START)
        self.outbox.record_result("r1", attempt, "submitted", now=START)
        evidence_id = self.outbox.add_evidence("r1", "outbound_observed_candidate", {"key": "x"}, attempt_id=attempt, observed_at=START)
        stored = self.outbox.get("r1")
        self.assertEqual(stored["status"], "submitted")
        self.assertEqual(stored["evidence"][0]["id"], evidence_id)
        with self.assertRaises(ValueError):
            self.outbox.record_result("r1", attempt, "accepted", now=START)
        with self.assertRaises(ValueError):
            self.outbox.add_evidence("r1", "invalid", {"number": float("nan")})

    def test_future_command_is_not_claimable_early(self):
        future = START + timedelta(minutes=1)
        self.outbox.enqueue(command(created_at=future, expires_at=future + timedelta(minutes=5)), now=START)
        self.assertIsNone(self.outbox.claim_next("account", "session", now=START))
        self.assertIsNotNone(self.outbox.claim_next("account", "session", now=future))

    def test_cancel_expiry_and_recovery_never_retry(self):
        self.outbox.enqueue(command("cancel"), now=START)
        self.assertTrue(self.outbox.cancel("cancel", now=START))
        self.assertFalse(self.outbox.cancel("cancel", now=START))
        self.outbox.enqueue(command("old", expires_at=START + timedelta(seconds=1)), now=START)
        self.assertIsNone(self.outbox.claim_next("account", "session", now=START + timedelta(seconds=1)))
        self.assertEqual(self.outbox.get("old")["status"], "expired")
        self.outbox.enqueue(command("inflight"), now=START)
        self.outbox.claim_next("account", "session", now=START)
        self.assertEqual(self.outbox.recover_inflight(now=START), 1)
        self.assertEqual(self.outbox.get("inflight")["status"], "unknown")
        self.assertIsNone(self.outbox.claim_next("account", "session", now=START))

    def test_two_connections_cannot_claim_same_command(self):
        with tempfile.TemporaryDirectory() as temp:
            path = temp + "/outbox.sqlite3"
            first_db = sqlite3.connect(path, timeout=3, check_same_thread=False)
            second_db = sqlite3.connect(path, timeout=3, check_same_thread=False)
            initialize_outbox(first_db)
            first, second = Outbox(first_db), Outbox(second_db)
            first.enqueue(command(), now=START)
            barrier = threading.Barrier(2)
            claims = []
            def claim(queue):
                barrier.wait()
                claims.append(queue.claim_next("account", "session", now=START))
            threads = [threading.Thread(target=claim, args=(queue,)) for queue in (first, second)]
            for thread in threads: thread.start()
            for thread in threads: thread.join()
            self.assertEqual(sum(item is not None for item in claims), 1)
            first_db.close(); second_db.close()

    def test_two_connections_enqueue_same_request_idempotently(self):
        with tempfile.TemporaryDirectory() as temp:
            path = temp + "/outbox.sqlite3"
            first_db = sqlite3.connect(path, timeout=3, check_same_thread=False)
            second_db = sqlite3.connect(path, timeout=3, check_same_thread=False)
            first, second = Outbox(first_db), Outbox(second_db)
            barrier = threading.Barrier(2)
            results, failures = [], []
            def enqueue(queue):
                try:
                    barrier.wait()
                    results.append(queue.enqueue(command(), now=START))
                except BaseException as exc:
                    failures.append(exc)
            threads = [threading.Thread(target=enqueue, args=(queue,)) for queue in (first, second)]
            for thread in threads: thread.start()
            for thread in threads: thread.join()
            self.assertEqual(failures, [])
            self.assertEqual(len(results), 2)
            self.assertEqual(len(first.list()), 1)
            first_db.close(); second_db.close()


if __name__ == "__main__":
    unittest.main()
