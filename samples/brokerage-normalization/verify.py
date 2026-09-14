"""Compare the full representative report with a committed deterministic golden file."""
import json
from pathlib import Path
import sys

from normalize import Pipeline, log_event

HERE = Path(__file__).resolve().parent


def scenario():
    pipeline = Pipeline()
    for name in json.loads((HERE / "examples/scenario.json").read_text(encoding="utf-8")):
        pipeline.ingest((HERE / "fixtures" / name).read_bytes(), name)
    return pipeline.report()


def main():
    try:
        actual = scenario()
        expected = json.loads((HERE / "examples/expected.json").read_text(encoding="utf-8"))
        if actual != expected:
            raise ValueError("representative report differs from examples/expected.json")
        # Independently specified business expectations, not just a self-generated golden.
        alpha, beta = actual["accounts"]
        if actual["counts"] != {"accepted": 4, "replay": 1, "rejected": 1}:
            raise ValueError("scenario outcome accounting differs")
        if alpha["snapshot_id"] != "alpha-2" or beta["positions"] != []:
            raise ValueError("invalid refresh rollback or explicit clearing failed")
        values = {v["currency"]: (v["known_market_value"], v["complete"]) for v in alpha["valuations"]}
        if values != {"CAD": ("105", True), "USD": ("216", False)}:
            raise ValueError("scenario valuation/completeness differs")
        print("PASS: exact golden report, rollback, clearing, currency values, replay and input accounting")
        return 0
    except (OSError, ValueError, KeyError, TypeError) as exc:
        log_event(HERE / "verify-audit.jsonl", {"status": "FAIL", "detail": str(exc)})
        print(f"FAIL: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
