"""Raw-export intake: the wrapper supplies the envelope and decides nothing about the data.

Every test here exists to prove one of two things: the wrapper refuses to invent a field it
was not given, or a wrapped export is judged exactly as a hand-authored capture would be.
"""
import contextlib
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

import broker_cli
from raw_intake import IntakeError, PAYLOAD_KIND, snapshot_id, wrap, wrap_file

ARTIFACT = Path(__file__).resolve().parent.parent

# The single-account subset already pinned as captures/04-fidelity-derived.json, used here as a
# stand-in for a clean export a reader could plausibly download.
CLEAN_CSV = (
    "Account Number,Account Name,Symbol,Description,Quantity,Last Price,Current Value,"
    "Cost Basis Total,Type\r\n"
    "Z12345678,Individual Brokerage,VTI,VANGUARD TOTAL STOCK MARKET ETF,420,$305.12,"
    '"$128,150.40","$95,000.00",Cash\r\n'
    "Z12345678,Individual Brokerage,SPAXX**,FIDELITY GOVERNMENT MONEY MARKET,5200,$1.00,"
    '"$5,200.00",--,Cash\r\n'
)
ACCOUNT = "Z12345678"
AS_OF = "2026-07-02T20:00:00Z"


class WrapperRefusals(unittest.TestCase):
    """The wrapper's only job is to refuse when it was not told enough."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="raw-intake-")
        self.dir = Path(self.temp.name)
        self.addCleanup(self.temp.cleanup)

    def test_unknown_broker_is_refused_and_never_sniffed_from_content(self):
        with self.assertRaises(IntakeError) as caught:
            wrap(CLEAN_CSV.encode(), "schwab", ACCOUNT, AS_OF)
        self.assertIn("unknown broker", str(caught.exception))

    def test_profile_without_a_declared_payload_kind_is_refused(self):
        # A new adapter must state how its raw bytes travel; the wrapper assumes nothing.
        self.assertEqual(set(PAYLOAD_KIND), {"alpaca", "plaid", "fidelity_csv"})
        with mock.patch.dict("raw_intake.PROFILES", {"newbroker": lambda *a: None}):
            with self.assertRaises(IntakeError) as caught:
                wrap(b"{}", "newbroker", ACCOUNT, AS_OF)
        self.assertIn("declares no raw payload kind", str(caught.exception))

    def test_as_of_is_required_and_never_defaults_to_the_clock(self):
        for bad in ("", "today", "2026-07-02", "2026-07-02T20:00:00+00:00"):
            with self.assertRaises(IntakeError, msg=bad):
                wrap(CLEAN_CSV.encode(), "fidelity_csv", ACCOUNT, bad)

    def test_missing_account_is_refused(self):
        with self.assertRaises(IntakeError):
            wrap(CLEAN_CSV.encode(), "fidelity_csv", "", AS_OF)

    def test_unknown_total_scope_is_refused(self):
        with self.assertRaises(IntakeError):
            wrap(CLEAN_CSV.encode(), "fidelity_csv", ACCOUNT, AS_OF, total_scope="everything")

    def test_empty_export_is_not_an_empty_account(self):
        empty = self.dir / "empty.csv"
        empty.write_bytes(b"")
        with self.assertRaises(IntakeError) as caught:
            wrap_file(empty, "fidelity_csv", ACCOUNT, AS_OF)
        self.assertIn("not an empty account", str(caught.exception))

    def test_oversize_export_writes_no_partial_capture(self):
        big = self.dir / "big.csv"
        big.write_bytes(b"x" * 400)
        with self.assertRaises(IntakeError):
            wrap_file(big, "fidelity_csv", ACCOUNT, AS_OF, max_bytes=200)
        self.assertEqual(sorted(p.name for p in self.dir.iterdir()), ["big.csv"])

    def test_strict_json_rules_still_apply_to_json_exports(self):
        with self.assertRaises(Exception):
            wrap(b'{"a": 1, "a": 2}', "alpaca", ACCOUNT, AS_OF)

    def test_non_utf8_text_export_is_refused(self):
        with self.assertRaises(IntakeError) as caught:
            wrap(b"\xff\xfeAccount Number", "fidelity_csv", ACCOUNT, AS_OF)
        self.assertIn("not UTF-8", str(caught.exception))


class WrapperOutput(unittest.TestCase):
    def test_same_bytes_wrap_to_the_same_id_so_a_rewrap_replays(self):
        first = wrap(CLEAN_CSV.encode(), "fidelity_csv", ACCOUNT, AS_OF)
        second = wrap(CLEAN_CSV.encode(), "fidelity_csv", ACCOUNT, AS_OF)
        self.assertEqual(first, second)
        self.assertEqual(first["snapshot_id"], snapshot_id(CLEAN_CSV.encode()))
        changed = wrap(CLEAN_CSV.replace("420", "421").encode(), "fidelity_csv", ACCOUNT, AS_OF)
        self.assertNotEqual(first["snapshot_id"], changed["snapshot_id"])

    def test_envelope_invents_no_quote_times_and_carries_the_given_observation_time(self):
        capture = wrap(CLEAN_CSV.encode(), "fidelity_csv", ACCOUNT, AS_OF)
        self.assertEqual(capture["as_of"], AS_OF)
        self.assertEqual(capture["context"]["price_timestamps"], {})
        self.assertIsNone(capture["context"]["reported_total"])
        self.assertEqual(capture["schema"], "capture/v1")
        self.assertEqual(len(capture["pages"]), 1)
        self.assertIsNone(capture["pages"][0]["next_cursor"])


class CommandLine(unittest.TestCase):
    """End to end, through the same store and engine a hand-authored capture uses."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="raw-cli-")
        self.dir = Path(self.temp.name)
        self.addCleanup(self.temp.cleanup)
        self.export = self.dir / "export.csv"
        self.export.write_text(CLEAN_CSV, encoding="utf-8", newline="")

    def run_cli(self, *argv):
        """Run the command in-process, keeping its report JSON out of the test output."""
        self.stdout = io.StringIO()
        with contextlib.redirect_stdout(self.stdout), contextlib.redirect_stderr(io.StringIO()):
            return broker_cli.main([*argv])

    def test_wrap_writes_a_capture_and_refuses_to_overwrite_it(self):
        out = self.dir / "capture.json"
        self.assertEqual(self.run_cli("wrap", str(self.export), "--broker", "fidelity_csv",
                                      "--account", ACCOUNT, "--as-of", AS_OF, "--out", str(out)), 0)
        body = json.loads(out.read_text(encoding="utf-8"))
        self.assertEqual(body["broker"], "fidelity_csv")
        before = out.read_bytes()
        self.assertEqual(self.run_cli("wrap", str(self.export), "--broker", "fidelity_csv",
                                      "--account", ACCOUNT, "--as-of", AS_OF, "--out", str(out)), 1)
        self.assertEqual(out.read_bytes(), before)

    def test_wrapped_export_accepts_and_a_rewrap_is_a_replay_not_a_second_snapshot(self):
        db = self.dir / "holdings.sqlite"
        args = ["ingest-raw", str(self.export), "--broker", "fidelity_csv",
                "--account", ACCOUNT, "--as-of", AS_OF, "--db", str(db)]
        self.assertEqual(self.run_cli(*args), 0)
        self.assertEqual(self.run_cli(*args), 0)
        report = json.loads(subprocess.run(
            [sys.executable, "-B", "broker_cli.py", "report", "--db", str(db)],
            cwd=ARTIFACT, capture_output=True, text=True, check=True).stdout)
        self.assertEqual(report["counts"]["accepted_snapshots"], 1)
        self.assertEqual(report["counts"]["attempts"], 2)
        accounts = [(a["broker"], a["account"]) for a in report["current"]]
        self.assertEqual(accounts, [("fidelity_csv", ACCOUNT)])

    def test_account_that_does_not_match_the_export_quarantines(self):
        db = self.dir / "mismatch.sqlite"
        code = self.run_cli("ingest-raw", str(self.export), "--broker", "fidelity_csv",
                            "--account", "Z99999999", "--as-of", AS_OF, "--db", str(db))
        self.assertEqual(code, 2)

    def test_real_preserved_export_gets_the_same_verdict_as_the_authored_capture(self):
        # sources/fidelity-external-2026-07-02.csv carries pending activity and several
        # accounts. Wrapping must not soften that: the raw path quarantines for the same
        # reason captures/03-fidelity-original.json does.
        db = self.dir / "real.sqlite"
        code = self.run_cli("ingest-raw", str(ARTIFACT / "sources/fidelity-external-2026-07-02.csv"),
                            "--broker", "fidelity_csv", "--account", ACCOUNT,
                            "--as-of", AS_OF, "--db", str(db))
        self.assertEqual(code, 2)

    def test_raw_commands_require_their_envelope_flags(self):
        db = self.dir / "unused.sqlite"
        for missing in (["--account", ACCOUNT], ["--broker", "fidelity_csv"]):
            with self.assertRaises(SystemExit):
                self.run_cli("ingest-raw", str(self.export), *missing, "--as-of", AS_OF, "--db", str(db))
        self.assertFalse(db.exists())

    def test_wrap_needs_no_database_and_creates_none(self):
        out = self.dir / "nodb.json"
        self.assertEqual(self.run_cli("wrap", str(self.export), "--broker", "fidelity_csv",
                                      "--account", ACCOUNT, "--as-of", AS_OF, "--out", str(out)), 0)
        self.assertEqual(sorted(p.suffix for p in self.dir.iterdir()), [".csv", ".json"])


if __name__ == "__main__":
    unittest.main()
