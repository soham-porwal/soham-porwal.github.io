"""One-command offline acceptance run; creates a new, inspectable evidence directory."""
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import uuid

HERE = Path(__file__).resolve().parent


def main():
    run_dir = None
    try:
        root = HERE / "checkpoints"
        root.mkdir(exist_ok=True)
        run_dir = root / (datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:8])
        run_dir.mkdir()

        def command(name, args, expected=0):
            completed = subprocess.run([sys.executable, "-B", *args], cwd=HERE,
                                       capture_output=True, text=True, timeout=180)
            (run_dir / (name + ".stdout.txt")).write_text(completed.stdout, encoding="utf-8")
            (run_dir / (name + ".stderr.txt")).write_text(completed.stderr, encoding="utf-8")
            if completed.returncode != expected:
                raise ValueError(f"{name}: exit {completed.returncode}, expected {expected}")
            return completed

        command("tests", ["-m", "unittest", "discover", "-s", "tests", "-v"])
        verified_sources = []
        official = json.loads((HERE / "sources/official-provenance.json").read_text(encoding="utf-8"))
        for item in official["files"]:
            path = HERE / item["file"]
            if hashlib.sha256(path.read_bytes()).hexdigest() != item["sha256"]:
                raise ValueError(f"source hash mismatch: {item['file']}")
            verified_sources.append(item["file"])
        external = json.loads((HERE / "sources/external-provenance.json").read_text(encoding="utf-8"))
        for kind in ("source", "csv", "license"):
            path = HERE / "sources" / external[kind + "_file"]
            if hashlib.sha256(path.read_bytes()).hexdigest() != external[kind + "_sha256"]:
                raise ValueError("external fixture hash mismatch")
            verified_sources.append("sources/" + path.name)
        inputs = sorted((HERE / "captures").glob("[0-9][0-9]-*.json"))
        if len(inputs) != 7:
            raise ValueError("expected seven representative captures")
        database = run_dir / "holdings.sqlite"
        args = ["broker_cli.py", "ingest", *[str(p.relative_to(HERE)) for p in inputs], "--db", str(database)]
        first = json.loads(command("first-ingest", args, 2).stdout)
        if first["counts"] != {"accepted": 4, "accepted_snapshots": 4, "attempts": 7,
                               "current_accounts": 4, "quarantined": 3, "replay": 0}:
            raise ValueError("first ingestion accounting differs from acceptance contract")
        codes = {e["code"] for e in first["quarantine_queue"]}
        if codes != {"UNRESOLVED_IDENTITY", "PENDING_ACTIVITY_NOT_POSITION", "UNRECOGNIZED_BROKER_REVIEW_REQUIRED"}:
            raise ValueError("unexpected quarantine reasons")
        plaid = next(a for a in first["current"] if a["broker"] == "plaid")
        if (plaid["known_holdings_value"], plaid["cash_value"], plaid["reported_total"], plaid["reconciliation"]) != (
                "100", "250", "350", "MATCHED"):
            raise ValueError("independent Plaid arithmetic expectation failed")
        alpaca_option = next(a for a in first["current"] if a["account"] == "synthetic-option-account")
        if alpaca_option["positions"][0]["value"] != "-500":
            raise ValueError("short option multiplier/sign expectation failed")
        second = json.loads(command("restart-replay", args, 2).stdout)
        if first["current"] != second["current"]:
            raise ValueError("restart replay changed canonical positions")
        if second["counts"]["accepted"] != 4 or second["counts"]["replay"] != 4 or second["counts"]["quarantined"] != 6:
            raise ValueError("restart replay accounting failed")
        command("integrity", ["broker_cli.py", "verify", "--db", str(database)])
        (run_dir / "normalized-output.json").write_text(json.dumps(first, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        summary = {"status": "PASS", "suite": "tests.stderr.txt", "source_files_verified": verified_sources,
                   "first_ingest": first["counts"], "restart": second["counts"],
                   "expected_quarantine": sorted(codes), "live_broker_tested": False,
                   "note": "Upstream examples and synthetic cases; no production-readiness certification."}
        (run_dir / "summary.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
        print("PASS: full suite, source hashes, representative values, durable replay and SQLite integrity")
        print("Evidence: " + str(run_dir))
        return 0
    except Exception as exc:
        error = {"status": "FAIL", "error_type": type(exc).__name__, "detail": str(exc)}
        if run_dir is not None:
            try:
                (run_dir / "failure.json").write_text(json.dumps(error, indent=2), encoding="utf-8")
            except OSError:
                pass  # The original failure remains explicit on stderr.
        print(json.dumps(error), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
