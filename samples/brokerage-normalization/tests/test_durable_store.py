import concurrent.futures
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest

from durable_store import Store, InvariantError


ROOT = Path(__file__).resolve().parents[1]


class Rejection(ValueError):
    code = "BAD_INPUT"
    path = "$.positions"


def normalize(raw):
    try:
        value = json.loads(raw)
    except ValueError as error:
        raise Rejection("sensitive malformed value") from error
    if value.get("bad"):
        raise Rejection("sensitive malformed value")
    return value


def delivery(snapshot_id="s1", as_of="2026-09-10T10:00:00Z", positions=None):
    return json.dumps({"broker": "alpha", "account": "demo", "snapshot_id": snapshot_id,
        "as_of": as_of, "positions": [{"instrument": "SYN", "quantity": "2"}]
        if positions is None else positions}).encode()


class DurableStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="store-test-", dir=ROOT)
        self.addCleanup(self.temp.cleanup)
        self.db = Path(self.temp.name) / "state.sqlite"

    def store(self, **kwargs):
        store = Store(self.db, normalize, "policy-v1", **kwargs)
        self.addCleanup(store.close)
        return store

    def test_restart_replay_conflict_and_old_replay(self):
        first = self.store()
        first.ingest(delivery(), "original.json")
        first.ingest(delivery("s2", "2026-09-10T11:00:00Z"), "newer.json")
        first.close()
        restarted = self.store()
        self.assertEqual(restarted.ingest(delivery(), "copy.json")["status"], "REPLAY")
        conflict = restarted.ingest(delivery(positions=[]), "conflict.json")
        self.assertEqual((conflict["status"], conflict["code"]), ("QUARANTINED", "CONFLICTING_REPLAY"))
        self.assertEqual(restarted.report()["current"][0]["snapshot_id"], "s2")
        self.assertTrue(restarted.verify()["ok"])

    def test_canonical_key_order_replay_and_full_field_hash(self):
        store = self.store()
        raw = delivery()
        store.ingest(raw, "first")
        reordered = json.dumps(dict(reversed(list(json.loads(raw).items()))), indent=2).encode()
        self.assertEqual(store.ingest(reordered, "second")["status"], "REPLAY")
        modified = json.loads(raw)
        modified["new_field"] = "must count in hash"
        self.assertEqual(store.ingest(json.dumps(modified).encode(), "third")["code"], "CONFLICTING_REPLAY")

    def test_stale_equal_and_rejected_id_can_be_corrected(self):
        store = self.store()
        store.ingest(delivery(), "first")
        for when in ("2026-09-10T09:00:00Z", "2026-09-10T10:00:00Z"):
            self.assertEqual(store.ingest(delivery("s2", when), "old")["code"], "STALE_SNAPSHOT")
        self.assertEqual(store.ingest(delivery("s2", "2026-09-10T11:00:00Z"), "corrected")["status"], "ACCEPTED")
        self.assertEqual(store.report()["counts"]["accepted_snapshots"], 2)

    def test_empty_snapshot_clears_account_without_erasing_history(self):
        store = self.store()
        store.ingest(delivery(), "first")
        store.ingest(delivery("clear", "2026-09-10T11:00:00Z", []), "clear")
        report = store.report()
        self.assertEqual(report["current"][0]["positions"], [])
        self.assertEqual(report["counts"]["accepted_snapshots"], 2)
        self.assertEqual(report["counts"]["current_accounts"], 1)
        self.assertTrue(store.verify()["ok"])

    def test_fault_rolls_back_ledger_current_and_audit(self):
        store = self.store()
        store.ingest(delivery(), "first")
        before = store.report()
        def fail(point):
            self.assertEqual(point, "before_commit")
            raise OSError("simulated sensitive write failure")
        store.fault_hook = fail
        with self.assertRaises(OSError):
            store.ingest(delivery("s2", "2026-09-10T11:00:00Z"), "second")
        self.assertEqual(store.report(), before)
        errors = store.error_path.read_text()
        self.assertIn('"exception_type": "OSError"', errors)
        self.assertNotIn("sensitive", errors)
        store.close()
        reopened = self.store()
        self.assertEqual(reopened.report(), before)
        self.assertEqual(reopened.ingest(delivery("s2", "2026-09-10T11:00:00Z"), "retry")["status"], "ACCEPTED")

    def test_malformed_quarantine_excludes_payload(self):
        store = self.store()
        raw = b'{"bad":true,"secret":"NEVER_STORE_THIS"}'
        event = store.ingest(raw, "local-input.json")
        self.assertEqual(event["status"], "QUARANTINED")
        report = store.report()
        self.assertEqual(report["current"], [])
        queue = report["quarantine_queue"]
        self.assertEqual(queue[0]["sha256"], hashlib.sha256(raw).hexdigest())
        self.assertEqual(set(queue[0]), {"attempt_id", "sha256", "source", "code", "path"})
        self.assertNotIn(b"NEVER_STORE_THIS", self.db.read_bytes())
        self.assertNotIn("secret", json.dumps(report))
        self.assertTrue(store.verify()["ok"])

    def test_operational_normalizer_failure_is_loud_and_does_not_quarantine(self):
        store = self.store()
        def failure(raw):
            raise RuntimeError("secret")
        store.normalizer = failure
        with self.assertRaises(RuntimeError):
            store.ingest(delivery(), "input")
        self.assertEqual(store.report()["counts"]["attempts"], 0)
        self.assertNotIn("secret", store.error_path.read_text())

    def test_corrupt_sqlite_fails_without_reset(self):
        self.db.write_bytes(b"not a database; preserve me")
        before = self.db.read_bytes()
        with self.assertRaises(sqlite3.DatabaseError):
            self.store()
        self.assertEqual(self.db.read_bytes(), before)
        self.assertTrue(self.db.with_suffix(".sqlite.errors.jsonl").exists())

    def test_policy_mismatch_fails_without_changing_state(self):
        store = self.store()
        store.ingest(delivery(), "first")
        before = store.report()
        store.close()
        with self.assertRaises(InvariantError):
            Store(self.db, normalize, "policy-v2")
        self.assertEqual(self.store().report(), before)

    def test_concurrent_connections_accept_once(self):
        initial = self.store()
        initial.close()
        barrier = threading.Barrier(2)
        def worker(index):
            with Store(self.db, normalize, "policy-v1") as store:
                barrier.wait(timeout=5)
                return store.ingest(delivery(), str(index))["status"]
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(worker, range(2)))
        self.assertEqual(sorted(results), ["ACCEPTED", "REPLAY"])
        store = self.store()
        self.assertEqual(store.report()["counts"]["accepted_snapshots"], 1)
        self.assertEqual(store.verify()["attempts"], 2)

    def test_verify_detects_current_tampering(self):
        store = self.store()
        store.ingest(delivery(), "first")
        store.connection.execute("UPDATE current SET fingerprint='tampered'")
        with self.assertRaises(InvariantError):
            store.verify()


if __name__ == "__main__":
    unittest.main()
