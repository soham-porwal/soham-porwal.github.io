"""Synthetic focused checks. Scratch files live below this directory only."""
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

import core


HERE = Path(__file__).resolve().parent
ENGINE = HERE / "engine"
ACCOUNT = "SYNTHETIC-PRIVATE-CANARY-7391"
META = dict(broker="alpaca", account=ACCOUNT, as_of="2026-09-01T20:00:00Z",
            currency="USD", total_scope="holdings")
ROW = dict(asset_id="904837e3-3b76-47ec-b432-046db621571b", symbol="AAPL",
           asset_class="us_equity", qty="2", side="long", market_value="400",
           cost_basis="300", current_price="200", account_id=ACCOUNT)
RAW = json.dumps([ROW]).encode()
CSV = ("Account Number,Account Name,Symbol,Description,Quantity,Last Price,Current Value,"
       "Cost Basis Total,Type\n" + ACCOUNT + ",Synthetic,VTI,Sample,2,100,200,150,Cash\n").encode()
AUDIT_FIELDS = set("request_id input_sha256 output_sha256 adapter adapter_version policy_hash "
                   "started_at completed_at outcome reason_codes duration_ms cleanup_result".split())


class LifecycleTests(unittest.TestCase):
    def setUp(self):
        self.scratch = tempfile.TemporaryDirectory(prefix="test-", dir=HERE)
        self.root = Path(self.scratch.name)
        self.temp = self.root / "requests"
        self.temp.mkdir()
        self.audit = self.root / "audit.jsonl"
        self.addCleanup(self.scratch.cleanup)

    def call(self, raw=RAW, metadata=None, **kwargs):
        options = dict(engine_root=ENGINE, audit_path=self.audit, temp_root=self.temp)
        options.update(kwargs)
        result = core.process_upload(raw, META.copy() if metadata is None else metadata, **options)
        self.assertEqual(result["cleanup_result"], "OK")
        self.assertEqual(list(self.temp.iterdir()), [])
        json.dumps(result, allow_nan=False)
        if result["outcome"] != "accepted":
            self.assertIsNone(result["normalization"])
            self.assertIsNone(result["output_sha256"])
        return result

    def records(self):
        return [json.loads(line) for line in self.audit.read_text().splitlines()]

    def fault_worker(self, setup):
        path = self.root / "fault_worker.py"
        path.write_text("import sys\nsys.dont_write_bytecode = True\n"
                        f"sys.path.insert(0, {str(HERE)!r})\n"
                        f"sys.path.insert(0, {str(ENGINE)!r})\n"
                        + setup + "\nimport worker\nraise SystemExit(worker.main())\n")
        return mock.patch.object(core, "WORKER", path)

    def test_alpaca_acceptance_determinism_and_exact_audit(self):
        first, second = self.call(), self.call()
        self.assertEqual(first["outcome"], "accepted")
        self.assertNotEqual(first["request_id"], second["request_id"])
        self.assertEqual(first["normalization"], second["normalization"])
        self.assertEqual(first["output_sha256"], second["output_sha256"])
        portfolio = first["normalization"]
        self.assertEqual(portfolio["account"], ACCOUNT)
        self.assertEqual(portfolio["as_of"], META["as_of"])
        self.assertEqual(portfolio["positions"][0]["quantity"], "2")
        self.assertEqual(portfolio["reconciliation"], "NOT_PROVIDED")
        # No inferred quote time or fabricated complete valuation.
        self.assertFalse(portfolio["valuation_complete"])
        self.assertIsNone(portfolio["positions"][0]["price_at"])
        canonical = json.dumps(portfolio, sort_keys=True, separators=(",", ":"),
                               ensure_ascii=False, allow_nan=False).encode()
        self.assertEqual(first["output_sha256"], hashlib.sha256(canonical).hexdigest())
        records = self.records()
        self.assertEqual(len(records), 2)
        for record, result in zip(records, (first, second)):
            self.assertEqual(set(record), AUDIT_FIELDS)
            self.assertEqual(record["request_id"], result["request_id"])
            self.assertEqual(record["input_sha256"], hashlib.sha256(RAW).hexdigest())
            self.assertEqual(record["output_sha256"], result["output_sha256"])
            self.assertEqual(record["outcome"], "accepted")
            self.assertEqual(len(record["adapter_version"]), 64)
            self.assertEqual(len(record["policy_hash"]), 64)
        audit = self.audit.read_text()
        for private in (ACCOUNT, "AAPL", "asset_id", "positions", "request.sqlite", str(self.root)):
            self.assertNotIn(private, audit)

    def test_fidelity_and_both_total_scopes(self):
        for scope in ("holdings", "holdings_plus_cash"):
            r = self.call(CSV, {**META, "broker": "fidelity_csv", "total_scope": scope})
            self.assertEqual(r["outcome"], "accepted")
            self.assertEqual(r["normalization"]["reconciliation_scope"], scope)

    def test_plaid_decimal_tokens_and_reconciliation(self):
        payload = dict(accounts=[dict(account_id=ACCOUNT)], securities=[dict(
            security_id="synthetic-plaid-aapl", ticker_symbol="AAPL", type="equity",
            iso_currency_code="USD")], holdings=[dict(account_id=ACCOUNT,
            security_id="synthetic-plaid-aapl", quantity=0.12345678, institution_price=100,
            institution_value=12.345678, cost_basis=10.01, iso_currency_code="USD",
            institution_price_datetime=META["as_of"])])
        r = self.call(json.dumps(payload).encode(), {**META, "broker": "plaid"})
        self.assertEqual(r["outcome"], "accepted")
        self.assertEqual(r["normalization"]["positions"][0]["quantity"], "0.12345678")
        self.assertEqual(r["normalization"]["known_holdings_value"], "12.345678")
        payload["holdings"][0]["institution_value"] = 1000
        r = self.call(json.dumps(payload).encode(), {**META, "broker": "plaid"})
        self.assertEqual(r["outcome"], "quarantined")
        self.assertEqual(r["reason_codes"], ["POSITION_VALUE_MISMATCH"])

    def test_invalid_request_matrix(self):
        inputs = [(None, META), ("[]", META), (b"", META), (b"x" * 2_000_001, META),
                  (RAW, []), (RAW, {}), (RAW, {**META, "extra": "x"}),
                  (RAW, {**META, "account": 1}), (RAW, {**META, "account": ""}),
                  (RAW, {**META, "currency": None}), (RAW, {**META, "broker": []}),
                  (RAW, {**META, "as_of": "2026-02-30T00:00:00Z"}),
                  (RAW, {**META, "as_of": "2026-09-01T20:00:00+00:00"}),
                  (RAW, {**META, "total_scope": "holdings_and_cash"}),
                  (b"{", META), (b'{"a":1,"a":2}', META), (b"[NaN]", META),
                  (b"\xff", META), (b'"unterminated', {**META, "broker": "fidelity_csv"})]
        for raw, metadata in inputs:
            with self.subTest(index=inputs.index((raw, metadata))):
                self.assertEqual(self.call(raw, metadata)["outcome"], "invalid_request")
        self.assertEqual(len(self.records()), len(inputs))

    def test_maximum_upload_boundary(self):
        exact = RAW + b" " * (2_000_000 - len(RAW))
        # Raw whitespace stays intact; wrapper overhead can trip the engine's own
        # capture limit, which is a domain rejection rather than upload validation.
        r = self.call(exact)
        self.assertEqual(r["outcome"], "quarantined")
        self.assertEqual(r["reason_codes"], ["INPUT_TOO_LARGE"])

    def test_unsupported_broker_never_leaks_value(self):
        r = self.call(metadata={**META, "broker": "PRIVATE-UNSUPPORTED-CANARY"})
        self.assertEqual(r["outcome"], "quarantined")
        self.assertEqual(r["reason_codes"], ["UNRECOGNIZED_BROKER_REVIEW_REQUIRED"])
        self.assertIsNone(self.records()[0]["adapter"])
        self.assertNotIn("PRIVATE-UNSUPPORTED-CANARY", self.audit.read_text())

    def test_domain_quarantine_and_state_isolation(self):
        unknown = json.dumps([{**ROW, "asset_id": "unknown-canary"}]).encode()
        r = self.call(unknown)
        self.assertEqual(r["outcome"], "quarantined")
        self.assertEqual(r["reason_codes"], ["UNRESOLVED_IDENTITY"])
        self.assertEqual(self.call()["outcome"], "accepted")
        # Older time and changed same-account state would be rejected by a reused DB.
        r = self.call(json.dumps([{**ROW, "qty": "3"}]).encode(),
                      {**META, "as_of": "2025-01-01T00:00:00Z"})
        self.assertEqual(r["outcome"], "accepted")
        self.assertEqual(r["normalization"]["positions"][0]["quantity"], "3")
        r = self.call(metadata={**META, "account": "mismatch"})
        self.assertEqual(r["reason_codes"], ["ACCOUNT_MISMATCH"])

    def test_import_failure_and_empty_success(self):
        r = self.call(engine_root=self.root / "missing-engine")
        self.assertEqual(r["outcome"], "operational_failure")
        r = self.call(b"[]")
        self.assertEqual(r["outcome"], "operational_failure")
        self.assertEqual(r["reason_codes"], ["EMPTY_NORMALIZATION"])

    def test_sqlite_failure(self):
        with self.fault_worker("import sqlite3\ndef fail(*a, **k):\n"
                               "    raise sqlite3.OperationalError('PRIVATE-EXCEPTION-CANARY')\n"
                               "sqlite3.connect = fail"):
            r = self.call()
        self.assertEqual(r["outcome"], "operational_failure")
        self.assertNotIn("PRIVATE-EXCEPTION-CANARY", self.audit.read_text())

    def test_deadline_interrupts_sqlite_setup(self):
        with self.fault_worker("import sqlite3, time\n"
                               "original = sqlite3.connect\n"
                               "def blocked(*a, **k):\n"
                               "    time.sleep(5)\n    return original(*a, **k)\n"
                               "sqlite3.connect = blocked"):
            started = time.monotonic()
            r = self.call(timeout_seconds=0.5)
            elapsed = time.monotonic() - started
        self.assertEqual(r["outcome"], "operational_failure")
        self.assertEqual(r["reason_codes"], ["TIMEOUT"])
        self.assertEqual(self.records()[0]["reason_codes"], ["TIMEOUT"])
        self.assertLess(elapsed, 2)

    def test_engine_dependency_bytes_and_cache_are_unchanged(self):
        names = ("broker_engine.py", "broker_adapters.py", "durable_store.py",
                 "broker_cli.py", "raw_intake.py", "reference.json")
        def snapshot():
            paths = [ENGINE / name for name in names]
            paths.extend((ENGINE / "__pycache__").glob("*"))
            return {str(path.relative_to(ENGINE)): hashlib.sha256(path.read_bytes()).hexdigest()
                    for path in paths if path.is_file()}
        before = snapshot()
        self.assertEqual(self.call()["outcome"], "accepted")
        self.assertEqual(snapshot(), before)

    def test_store_admission_rejection_preserves_code(self):
        with self.fault_worker("import durable_store\nfrom broker_engine import Rejected\n"
                "Original = durable_store.Store\n"
                "class RejectingStore(Original):\n"
                "    def __init__(self, path, normalizer, policy_hash):\n"
                "        def reject(raw):\n"
                "            raise Rejected('STALE_SNAPSHOT')\n"
                "        super().__init__(path, reject, policy_hash)\n"
                "durable_store.Store = RejectingStore"):
            r = self.call()
        self.assertEqual(r["outcome"], "quarantined")
        self.assertEqual(r["reason_codes"], ["STALE_SNAPSHOT"])

    def test_verify_failure(self):
        with self.fault_worker("import durable_store\n"
                "def fail(self):\n    raise durable_store.InvariantError('PRIVATE-CANARY')\n"
                "durable_store.Store.verify = fail"):
            r = self.call()
        self.assertEqual(r["outcome"], "operational_failure")

    def test_deadline_kills_work_and_cleans_partial_files(self):
        with self.fault_worker("import time\nfrom pathlib import Path\n"
                "Path('partial-upload').write_text('PRIVATE-CANARY')\n"
                "time.sleep(5)\nPath('late-marker').write_text('should never happen')"):
            started = time.monotonic()
            r = self.call(timeout_seconds=0.25)
            elapsed = time.monotonic() - started
        self.assertEqual(r["outcome"], "operational_failure")
        self.assertEqual(r["reason_codes"], ["TIMEOUT"])
        self.assertEqual(self.records()[0]["reason_codes"], ["TIMEOUT"])
        self.assertLess(elapsed, 2)

    def test_deadline_before_worker_start_has_same_timeout_contract(self):
        with mock.patch.object(core.subprocess, "Popen") as launch:
            result = self.call(timeout_seconds=1e-12)
        launch.assert_not_called()
        self.assertEqual(result["outcome"], "operational_failure")
        self.assertEqual(result["reason_codes"], ["TIMEOUT"])
        self.assertEqual(self.records()[0]["reason_codes"], ["TIMEOUT"])

    def test_corrupt_worker_protocol(self):
        with self.fault_worker("print('{}')\nraise SystemExit(0)"):
            r = self.call()
        self.assertEqual(r["outcome"], "operational_failure")

    def test_audit_unavailable_cannot_accept(self):
        r = self.call(audit_path=self.root)
        self.assertEqual(r["outcome"], "operational_failure")
        self.assertIn("AUDIT_FAILURE", r["reason_codes"])

    def test_partial_audit_write_rolls_back_and_preserves_prior_records(self):
        self.call()
        before = self.audit.read_bytes()
        real_write = core.os.write
        def partial(fd, body):
            real_write(fd, body[:12])
            raise OSError("PRIVATE-CANARY")
        with mock.patch.object(core.os, "write", side_effect=partial):
            r = self.call()
        self.assertEqual(r["outcome"], "operational_failure")
        self.assertIn("AUDIT_FAILURE", r["reason_codes"])
        self.assertEqual(self.audit.read_bytes(), before)

    def test_cleanup_failure_is_visible_in_response_and_audit(self):
        with mock.patch.object(core, "_cleanup", return_value=False):
            r = core.process_upload(RAW, META, engine_root=ENGINE,
                                    audit_path=self.audit, temp_root=self.temp)
        self.assertEqual(r["outcome"], "operational_failure")
        self.assertEqual(r["cleanup_result"], "FAIL")
        self.assertIsNone(r["normalization"])
        self.assertEqual(self.records()[0]["cleanup_result"], "FAIL")
        # Test owns this scratch directory and cleans its intentional fault residue.

    def test_concurrent_threads_have_intact_private_audit_and_isolated_results(self):
        def invoke(index):
            return core.process_upload(RAW if index % 2 else b"{", META,
                engine_root=ENGINE, audit_path=self.audit, temp_root=self.temp)
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(invoke, range(24)))
        self.assertEqual(list(self.temp.iterdir()), [])
        records = self.records()
        self.assertEqual(len(records), 24)
        self.assertEqual(len({r["request_id"] for r in records}), 24)
        self.assertEqual({r["request_id"] for r in records}, {r["request_id"] for r in results})
        self.assertEqual(sum(r["outcome"] == "accepted" for r in results), 12)
        for record in records:
            self.assertEqual(set(record), AUDIT_FIELDS)
            self.assertEqual(record["cleanup_result"], "OK")
        self.assertNotIn(ACCOUNT, self.audit.read_text())

    def test_six_accepted_requests_clean_before_each_return(self):
        local = threading.local()
        real_mkdtemp = core.tempfile.mkdtemp
        real_popen = core.subprocess.Popen

        def remember_workspace(*args, **kwargs):
            local.workspace = Path(real_mkdtemp(*args, **kwargs))
            return str(local.workspace)

        def remember_process(*args, **kwargs):
            local.process = real_popen(*args, **kwargs)
            return local.process

        def invoke(index):
            barrier.wait(timeout=10)
            account = f"{ACCOUNT}-{index}"
            raw = json.dumps([{**ROW, "account_id": account, "qty": str(index + 1)}]).encode()
            result = core.process_upload(raw, {**META, "account": account},
                engine_root=ENGINE, audit_path=self.audit, temp_root=self.temp)
            # Inspect immediately in the calling thread, before the future completes
            # or executor shutdown can mask deferred cleanup/handle release.
            self.assertNotIn(local.workspace.name, [p.name for p in self.temp.iterdir()])
            self.assertFalse(local.workspace.exists())
            self.assertEqual(result["outcome"], "accepted")
            self.assertEqual(result["cleanup_result"], "OK")
            self.assertEqual(result["normalization"]["account"], account)
            self.assertEqual(result["normalization"]["positions"][0]["quantity"], str(index + 1))
            self.assertIsNotNone(local.process.returncode)
            self.assertTrue(local.process.stdin.closed)
            self.assertTrue(local.process.stdout.closed)
            return result

        with mock.patch.object(core.tempfile, "mkdtemp", side_effect=remember_workspace), \
                mock.patch.object(core.subprocess, "Popen", side_effect=remember_process), \
                ThreadPoolExecutor(max_workers=6) as pool:
            for batch in range(5):
                barrier = threading.Barrier(6)
                results = list(pool.map(invoke, range(batch * 6, batch * 6 + 6)))
                self.assertEqual(list(self.temp.iterdir()), [])
                records = {r["request_id"]: r for r in self.records()}
                for result in results:
                    record = records[result["request_id"]]
                    self.assertEqual(record["outcome"], "accepted")
                    self.assertEqual(record["cleanup_result"], "OK")
                    self.assertEqual(record["output_sha256"], result["output_sha256"])
        self.assertEqual(len(self.records()), 30)

    def test_cleanup_retries_when_path_probe_misses_visible_directory(self):
        workspace = self.temp / "request-pending"
        workspace.mkdir()
        real_rmtree = core.shutil.rmtree
        attempts = []

        def delayed_remove(path):
            attempts.append(path)
            if len(attempts) == 1:
                # Model Windows delete-pending visibility: a direct path probe
                # says absent while its parent still enumerates the entry.
                return
            real_rmtree(path)

        with mock.patch.object(core.shutil, "rmtree", side_effect=delayed_remove), \
                mock.patch.object(core.os.path, "lexists", return_value=False):
            self.assertTrue(core._cleanup(workspace))
        self.assertEqual(list(self.temp.iterdir()), [])
        self.assertGreaterEqual(len(attempts), 2)

    def test_cleanup_never_accepts_persistent_parent_entry(self):
        with mock.patch.object(core.shutil, "rmtree"), \
                mock.patch.object(core.os.path, "lexists", return_value=False), \
                mock.patch.object(core, "FINALIZE_SECONDS", 0.05):
            result = core.process_upload(RAW, META, engine_root=ENGINE,
                audit_path=self.audit, temp_root=self.temp)
        self.assertEqual(result["outcome"], "operational_failure")
        self.assertEqual(result["cleanup_result"], "FAIL")
        self.assertIn("CLEANUP_FAILURE", result["reason_codes"])
        self.assertIsNone(result["normalization"])
        self.assertIsNone(result["output_sha256"])
        self.assertEqual(self.records()[0]["cleanup_result"], "FAIL")

    def test_cross_process_audit_appends(self):
        runner = self.root / "caller.py"
        runner.write_text("import sys\nsys.dont_write_bytecode = True\n"
            f"sys.path.insert(0, {str(HERE)!r})\nimport core\nfrom pathlib import Path\n"
            f"r = core.process_upload(b'', {META!r}, engine_root=Path({str(ENGINE)!r}), "
            f"audit_path=Path({str(self.audit)!r}), temp_root=Path({str(self.temp)!r}))\n"
            "raise SystemExit(0 if r['outcome'] == 'invalid_request' else 1)\n")
        processes = [subprocess.Popen([sys.executable, "-I", "-B", str(runner)],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)) for _ in range(12)]
        for process in processes:
            self.assertEqual(process.wait(timeout=10), 0)
        self.assertEqual(len(self.records()), 12)


if __name__ == "__main__":
    unittest.main(verbosity=2)
