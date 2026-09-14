"""Small, dependency-free ranking scorer and paired-result summarizer."""

from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path
from typing import Iterable


HERE = Path(__file__).resolve().parent


def recall_at_k(retrieved: list[str], gold: set[str], k: int) -> float | None:
    """Return hits / |gold|; undefined when gold is empty."""
    if k < 1:
        raise ValueError("k must be positive")
    if not gold:
        return None
    return sum(item in gold for item in retrieved[:k]) / len(gold)


def mrr(retrieved: list[str], gold: set[str]) -> float | None:
    """Return reciprocal rank of the first relevant item."""
    if not gold:
        return None
    for rank, item in enumerate(retrieved, start=1):
        if item in gold:
            return 1.0 / rank
    return 0.0


def ndcg_at_k(retrieved: list[str], gold: set[str], k: int) -> float | None:
    """Return binary-relevance normalized discounted cumulative gain."""
    if k < 1:
        raise ValueError("k must be positive")
    if not gold:
        return None
    dcg = sum(
        1.0 / math.log2(rank + 2)
        for rank, item in enumerate(retrieved[:k])
        if item in gold
    )
    ideal = sum(1.0 / math.log2(rank + 2) for rank in range(min(len(gold), k)))
    return dcg / ideal


def mean(values: Iterable[float]) -> float:
    values = list(values)
    if not values:
        raise ValueError("mean requires at least one value")
    return sum(values) / len(values)


def paired_t(differences: list[float]) -> float:
    """Return the one-sample t-statistic for paired differences."""
    if len(differences) < 2:
        raise ValueError("paired t requires at least two differences")
    average = mean(differences)
    variance = sum((value - average) ** 2 for value in differences) / (
        len(differences) - 1
    )
    if variance == 0:
        if average == 0:
            return 0.0
        return math.copysign(math.inf, average)
    return average / math.sqrt(variance / len(differences))


def load_json(path: Path) -> dict:
    try:
        with path.open("r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot load {path.name}: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"{path.name} must contain a JSON object")
    return payload


def summarize_historical(path: Path) -> dict[str, float | int | str]:
    payload = load_json(path)
    rows = payload.get("scores")
    if not isinstance(rows, list) or not rows:
        raise ValueError("historical scores must be a non-empty list")

    labels: set[str] = set()
    baseline: list[float] = []
    candidate: list[float] = []
    for row in rows:
        if not isinstance(row, dict) or set(row) != {"pair", "baseline", "candidate"}:
            raise ValueError("each score row must contain pair, baseline, and candidate")
        label = row["pair"]
        if not isinstance(label, str) or label in labels:
            raise ValueError("pair labels must be unique strings")
        labels.add(label)
        values = (row["baseline"], row["candidate"])
        if any(isinstance(value, bool) or not isinstance(value, (int, float)) for value in values):
            raise ValueError(f"{label} contains a non-numeric score")
        if any(not math.isfinite(value) or not 0 <= value <= 1 for value in values):
            raise ValueError(f"{label} contains a score outside [0, 1]")
        baseline.append(float(values[0]))
        candidate.append(float(values[1]))

    differences = [new - old for old, new in zip(baseline, candidate)]
    base_mean = mean(baseline)
    candidate_mean = mean(candidate)
    return {
        "scope": "historical_privacy_safe_numeric_export",
        "metric": "recall_at_6",
        "n_paired": len(rows),
        "baseline_mean": base_mean,
        "candidate_mean": candidate_mean,
        # Average the paired differences directly. This is the operation used by the
        # experiment gate and avoids a last-bit change from subtracting two means.
        "delta": mean(differences),
        "paired_t": paired_t(differences),
    }


def score_synthetic(path: Path, k: int = 3) -> dict:
    payload = load_json(path)
    cases = payload.get("cases")
    if not isinstance(cases, list) or not cases:
        raise ValueError("synthetic cases must be a non-empty list")
    output = []
    for case in cases:
        if not isinstance(case, dict):
            raise ValueError("synthetic case must be an object")
        retrieved = case.get("retrieved")
        gold_items = case.get("gold")
        if not isinstance(retrieved, list) or not isinstance(gold_items, list):
            raise ValueError("retrieved and gold must be lists")
        gold = set(gold_items)
        output.append(
            {
                "case": case.get("case"),
                f"recall_at_{k}": recall_at_k(retrieved, gold, k),
                "mrr": mrr(retrieved, gold),
                f"ndcg_at_{k}": ndcg_at_k(retrieved, gold, k),
            }
        )
    return {"scope": "synthetic_demonstration", "cases": output}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--synthetic", action="store_true", help="score the separate invented example"
    )
    args = parser.parse_args(argv)
    try:
        if args.synthetic:
            result = score_synthetic(HERE / "synthetic_example.json")
        else:
            result = summarize_historical(HERE / "historical_scores.json")
        print(json.dumps(result, indent=2, sort_keys=True, allow_nan=False))
        return 0
    except (ValueError, OSError) as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
