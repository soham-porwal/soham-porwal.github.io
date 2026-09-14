"""Failure-oriented contract and CLI tests; all fixtures are invented."""
import copy
from decimal import Decimal, localcontext
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

import normalize as n


def row(instrument="demo-us-1", quantity="2", price="12.5"):
    symbol, mic, currency = n.INSTRUMENTS[instrument]
    return dict(instrument=instrument, symbol=symbol, mic=mic, currency=currency,
                quantity=quantity, price=price)


def snapshot(rows=None, sid="s1", stamp="2026-09-10T10:00:00Z", account="a1"):
    return dict(version=1, broker="alpha", account=account, snapshot_id=sid, as_of=stamp,
                positions=[row()] if rows is None else rows)


def feed(pipeline, payload, source="test.json"):
    pipeline.ingest(json.dumps(payload).encode(), source)


class ContractTests(unittest.TestCase):
    def reject(self, payload, code):
        p = n.Pipeline()
        feed(p, payload)
        self.assertEqual(p.events[-1]["code"], code)
        self.assertEqual(p.report()["accounts"], [])
        self.assertEqual(p.ledger, {})

    def test_null_and_zero_are_distinct_even_at_zero_quantity(self):
        p = n.Pipeline()
        feed(p, snapshot([row(price="0"), row("demo-halt", "0", None)]))
        account = p.report()["accounts"][0]
        positions = {r["instrument_id"]: r for r in account["positions"]}
        self.assertEqual(positions["demo-us-1"]["market_value"], "0")
        self.assertIsNone(positions["demo-halt"]["market_value"])
        self.assertEqual(account["valuations"], [dict(currency="USD", known_market_value="0",
                                                     missing_price_count=1, complete=False)])

    def test_missing_price_is_contract_violation(self):
        payload = snapshot()
        del payload["positions"][0]["price"]
        self.reject(payload, "FIELDS_MISMATCH")

    def test_decimal_grammar_rejects_ambiguous_and_nonfinite_inputs(self):
        for field in ("quantity", "price"):
            for value in (True, False, 12, 0.1, "NaN", "Infinity", "-1", "1e3", "1,000",
                          " 1", "", "01", "+1", "1.", "0.0000001", "1000000000000"):
                with self.subTest(field=field, value=value):
                    payload = snapshot()
                    payload["positions"][0][field] = value
                    self.reject(payload, "INVALID_DECIMAL")
        self.reject(snapshot([row(quantity=None)]), "INVALID_DECIMAL")

    def test_identity_has_no_ticker_fallback_or_currency_coercion(self):
        for field, value, code in (("instrument", "missing", "UNKNOWN_INSTRUMENT"),
                                   ("instrument", [], "UNKNOWN_INSTRUMENT"),
                                   ("symbol", "dup", "INSTRUMENT_IDENTITY_MISMATCH"),
                                   ("mic", "XTSE", "INSTRUMENT_IDENTITY_MISMATCH"),
                                   ("currency", "CAD", "INSTRUMENT_IDENTITY_MISMATCH"),
                                   ("currency", None, "INSTRUMENT_IDENTITY_MISMATCH")):
            with self.subTest(field=field, value=value):
                payload = snapshot()
                payload["positions"][0][field] = value
                self.reject(payload, code)

    def test_same_ticker_different_currency_kept_separate(self):
        p = n.Pipeline()
        feed(p, snapshot([row(), row("demo-ca-1", "3", "10")]))
        account = p.report()["accounts"][0]
        self.assertEqual(len(account["positions"]), 2)
        self.assertEqual({v["currency"]: v["known_market_value"] for v in account["valuations"]},
                         {"CAD": "30", "USD": "25"})

    def test_duplicate_rows_are_not_lots(self):
        for price in ("12.5", "99"):
            self.reject(snapshot([row(), row(price=price)]), "DUPLICATE_POSITION")

    def test_strict_envelope_and_timestamp(self):
        for field, value, code in (("broker", [], "UNKNOWN_BROKER"),
                ("version", True, "UNSUPPORTED_VERSION"), ("version", 2, "UNSUPPORTED_VERSION"),
                ("account", 12, "INVALID_ID"), ("snapshot_id", " ", "INVALID_ID"),
                ("positions", {}, "INVALID_ROWS"), ("positions", [None], "OBJECT_REQUIRED")):
            payload = snapshot()
            payload[field] = value
            self.reject(payload, code)
        for value in (None, "2026-02-30T10:00:00Z", "2026-09-10", "2026-09-10T10:00:00+00:00",
                      "2026-09-10T10:00:00.000Z", "2026-09-10T10:00:60Z"):
            self.reject(snapshot(stamp=value), "INVALID_TIMESTAMP")
        payload = snapshot()
        payload["ignored"] = "schema drift"
        self.reject(payload, "FIELDS_MISMATCH")

    def test_beta_adapter_and_broker_scoped_accounts(self):
        p = n.Pipeline()
        alpha = snapshot()
        beta = {k: v for k, v in alpha.items() if k != "positions"}
        beta.update(broker="beta", holdings=[dict(security="demo-us-1", ticker="DUP", venue="XNYS",
                                                ccy="USD", units="2", mark="12.5")])
        feed(p, alpha)
        feed(p, beta)
        accounts = p.report()["accounts"]
        self.assertEqual(len(accounts), 2)
        self.assertEqual(accounts[0]["positions"], accounts[1]["positions"])
        self.assertEqual(accounts[0]["valuations"], accounts[1]["valuations"])

    def test_exact_product_and_sum_outside_default_precision(self):
        maximum = "999999999999.999999"
        p = n.Pipeline()
        with localcontext() as ctx:
            ctx.prec = 6  # hostile ambient context must not affect arithmetic
            feed(p, snapshot([row(quantity=maximum, price=maximum), row("demo-halt", maximum, maximum)]))
            total = p.report()["accounts"][0]["valuations"][0]["known_market_value"]
        # Independent integer-scaled oracle: each decimal has six fractional digits.
        scaled = 999999999999999999 ** 2 * 2
        digits = str(scaled)
        self.assertEqual(total, digits[:-12] + "." + digits[-12:])

    def test_strict_json_rejects_hidden_keys_nonfinite_and_malformed(self):
        for data, code in ((b'{"broker":"alpha","broker":"beta"}', "DUPLICATE_JSON_KEY"),
                           (b'{"a":NaN}', "NONFINITE_JSON"), (b'{"a":Infinity}', "NONFINITE_JSON"),
                           (b'{', "INVALID_JSON"), (b'\xff', "INVALID_JSON"),
                           (b'[]', "OBJECT_REQUIRED"), (b'null', "OBJECT_REQUIRED")):
            p = n.Pipeline()
            p.ingest(data, "bad.json")
            self.assertEqual(p.events[0]["code"], code)
            self.assertEqual(p.report()["counts"]["rejected"], 1)

    def test_resource_bounds(self):
        p = n.Pipeline()
        p.ingest(b" " * (n.MAX_BYTES + 1), "big.json")
        self.assertEqual(p.events[0]["code"], "INPUT_TOO_LARGE")
        self.reject(snapshot([row()] * (n.MAX_ROWS + 1)), "INVALID_ROWS")

    def test_oversized_integer_rejects_and_processing_continues(self):
        p = n.Pipeline()
        feed(p, snapshot())
        p.ingest(b'{"version":' + b'9' * 5000 + b'}', "huge-integer.json")
        feed(p, snapshot(account="a2"))
        self.assertEqual(p.events[1]["code"], "INVALID_JSON")
        self.assertEqual(p.report()["counts"], dict(accepted=2, replay=0, rejected=1))

    def test_arithmetic_does_not_inherit_exponent_bounds(self):
        p = n.Pipeline()
        with localcontext() as ctx:
            ctx.Emax = 0
            feed(p, snapshot())
            total = p.report()["accounts"][0]["valuations"][0]["known_market_value"]
        self.assertEqual(total, "25")


class StateTests(unittest.TestCase):
    def test_old_replay_and_changed_replay_do_not_roll_back(self):
        p = n.Pipeline()
        old = snapshot()
        feed(p, old)
        feed(p, snapshot([row(quantity="3")], "s2", "2026-09-10T11:00:00Z"))
        feed(p, old)
        old["positions"][0]["quantity"] = "4"
        feed(p, old)
        self.assertEqual([e["status"] for e in p.events], ["ACCEPTED", "ACCEPTED", "REPLAY", "REJECTED"])
        self.assertEqual(p.events[-1]["code"], "REPLAY_CONFLICT")
        self.assertEqual(p.report()["accounts"][0]["positions"][0]["quantity"], "3")

    def test_semantic_replay_ignores_row_order_decimal_scale_and_whitespace(self):
        p = n.Pipeline()
        payload = snapshot([row(), row("demo-halt", "1", None)])
        feed(p, payload)
        payload["positions"].reverse()
        payload["positions"][1]["quantity"] = "2.000"
        p.ingest(json.dumps(payload, indent=4).encode(), "copy.json")
        self.assertEqual(p.events[-1]["status"], "REPLAY")
        self.assertNotEqual(p.events[0]["raw_sha256"], p.events[1]["raw_sha256"])
        self.assertEqual(p.events[0]["canonical_sha256"], p.events[1]["canonical_sha256"])

    def test_bad_final_row_rolls_back_entire_snapshot_and_can_be_corrected(self):
        p = n.Pipeline()
        feed(p, snapshot())
        state, ledger = copy.deepcopy(p.current), dict(p.ledger)
        payload = snapshot([row(quantity="99"), row("demo-halt", price="broken")], "s2", "2026-09-10T11:00:00Z")
        feed(p, payload)
        self.assertEqual(p.current, state)
        self.assertEqual(p.ledger, ledger)
        payload["positions"][1]["price"] = None
        feed(p, payload)
        self.assertEqual(p.events[-1]["status"], "ACCEPTED")
        self.assertEqual(p.report()["accounts"][0]["snapshot_id"], "s2")

    def test_empty_snapshot_clears_and_stale_does_not_resurrect(self):
        p = n.Pipeline()
        feed(p, snapshot())
        feed(p, snapshot([], "clear", "2026-09-10T11:00:00Z"))
        feed(p, snapshot(sid="stale"))
        result = p.report()
        self.assertEqual(result["accounts"][0]["positions"], [])
        self.assertEqual(result["accounts"][0]["valuations"], [])
        self.assertTrue(result["accounts"][0]["empty_snapshot"])
        self.assertEqual(result["row_accounting"]["superseded_rows"], 1)
        self.assertEqual(p.events[-1]["code"], "STALE_OR_EQUAL_TIMESTAMP")

    def test_equal_time_new_id_rejected_and_account_clocks_independent(self):
        p = n.Pipeline()
        feed(p, snapshot())
        feed(p, snapshot(sid="other"))
        feed(p, snapshot(account="a2", stamp="2026-09-09T10:00:00Z"))
        self.assertEqual([e["status"] for e in p.events], ["ACCEPTED", "REJECTED", "ACCEPTED"])

    def test_accounting_mixed_inputs_and_repeatable_report(self):
        p = n.Pipeline()
        feed(p, snapshot())
        feed(p, snapshot())
        feed(p, snapshot([row(price="bad")], sid="bad"))
        p.ingest(b"{", "unreadable.json")
        result = p.report()
        self.assertEqual(result, p.report())
        self.assertEqual(result["counts"], dict(accepted=1, replay=1, rejected=2))
        self.assertEqual(result["row_accounting"], dict(recognized_input_rows=3, accepted_rows=1,
            replayed_rows=1, rejected_rows=1, unknown_row_count_inputs=1, active_rows=1, superseded_rows=0))

    def test_empty_run_and_empty_snapshot_are_suspect(self):
        p = n.Pipeline()
        self.assertEqual(p.report()["status"], "SUSPECT")
        feed(p, snapshot([]))
        self.assertEqual(p.report()["status"], "SUSPECT")

    def test_verify_detects_mutated_state_and_ledger(self):
        p = n.Pipeline()
        feed(p, snapshot())
        p.current[("alpha", "a1")]["positions"][0]["quantity"] = "999"
        with self.assertRaisesRegex(n.ContractError, "STATE_HASH_FAILURE"):
            p.report()
        p.ledger.clear()
        with self.assertRaisesRegex(n.ContractError, "LEDGER_FAILURE"):
            p.report()

    def test_report_is_detached_from_internal_state(self):
        p = n.Pipeline()
        feed(p, snapshot())
        expected = p.report()
        detached = p.report()
        detached["accounts"][0]["positions"][0]["quantity"] = "999"
        detached["events"][0]["status"] = "REJECTED"
        self.assertEqual(p.report(), expected)


class CLITests(unittest.TestCase):
    def run_cli(self, *args):
        return subprocess.run([sys.executable, "-B", "normalize.py", *map(str, args)],
                              capture_output=True, text=True, timeout=10)

    def test_cli_exit_status_audit_and_partial_report(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as temp:
            temp = Path(temp)
            good, bad, log = temp / "good.json", temp / "bad.json", temp / "audit.jsonl"
            good.write_text(json.dumps(snapshot()), encoding="utf-8")
            bad.write_text('{', encoding="utf-8")
            ok = self.run_cli(good, "--log", log)
            self.assertEqual(ok.returncode, 0, ok.stderr)
            mixed = self.run_cli(good, bad, "--log", log)
            self.assertEqual(mixed.returncode, 2, mixed.stderr)
            report = json.loads(mixed.stdout)
            self.assertEqual(report["counts"], dict(accepted=1, replay=0, rejected=1))
            entries = [json.loads(line) for line in log.read_text().splitlines()]
            self.assertEqual(len(entries), 2)
            self.assertEqual(entries[-1], report)

    def test_io_failure_and_unwritable_audit_never_emit_success(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as temp:
            temp = Path(temp)
            log = temp / "audit.jsonl"
            failed = self.run_cli(temp / "absent.json", "--log", log)
            self.assertEqual(failed.returncode, 1)
            self.assertEqual(failed.stdout, "")
            self.assertEqual(json.loads(log.read_text())["status"], "FAIL")
            good = temp / "good.json"
            good.write_text(json.dumps(snapshot()), encoding="utf-8")
            failed = self.run_cli(good, "--log", temp)
            self.assertEqual(failed.returncode, 1)
            self.assertIn("audit log unavailable", failed.stderr)
            self.assertEqual(failed.stdout, "")

    def test_audit_cannot_alias_input_directly_or_by_hard_link(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as temp:
            temp = Path(temp)
            source = temp / "source.json"
            original = json.dumps(snapshot()).encode()
            source.write_bytes(original)
            alias = temp / "alias.json"
            os.link(source, alias)
            for log in (source, alias):
                failed = self.run_cli(source, "--log", log)
                self.assertEqual(failed.returncode, 1)
                self.assertIn("aliases an input", failed.stderr)
                self.assertEqual(source.read_bytes(), original)
                self.assertEqual(failed.stdout, "")

    def test_oversized_file_is_explicitly_prefix_hashed(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as temp:
            temp = Path(temp)
            source = temp / "big.json"
            source.write_bytes(b" " * (n.MAX_BYTES + 20))
            result = self.run_cli(source, "--log", temp / "audit.jsonl")
            self.assertEqual(result.returncode, 2)
            event = json.loads(result.stdout)["events"][0]
            self.assertTrue(event["truncated"])
            self.assertIsNone(event["raw_sha256"])
            self.assertEqual(len(event["prefix_sha256"]), 64)
            self.assertEqual(event["bytes_read"], n.MAX_BYTES + 1)


if __name__ == "__main__":
    unittest.main()
