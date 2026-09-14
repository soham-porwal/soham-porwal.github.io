"""Independent synthetic demonstration. Python 3.12+, standard library only."""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from copy import deepcopy
from datetime import datetime
from decimal import Context, Decimal, localcontext
import hashlib
import json
from pathlib import Path
import re
import sys

MAX_BYTES = 1_000_000
MAX_ROWS = 1000
ARITHMETIC = Context(prec=50)
# Invented security master, not real securities or broker identifiers.
INSTRUMENTS = {
    "demo-us-1": ("DUP", "XNYS", "USD"),
    "demo-ca-1": ("DUP", "XTSE", "CAD"),
    "demo-halt": ("HALT", "XNAS", "USD"),
}
ADAPTERS = {
    "alpha": ("positions", {"instrument_id": "instrument", "symbol": "symbol",
        "mic": "mic", "currency": "currency", "quantity": "quantity", "price": "price"}),
    "beta": ("holdings", {"instrument_id": "security", "symbol": "ticker",
        "mic": "venue", "currency": "ccy", "quantity": "units", "price": "mark"}),
}


class ContractError(ValueError):
    def __init__(self, code, path):
        self.code, self.path = code, path
        super().__init__(f"{code} at {path}")


def require(condition, code, path):
    if not condition:
        raise ContractError(code, path)


def keys(obj, expected, path):
    require(type(obj) is dict, "OBJECT_REQUIRED", path)
    require(set(obj) == set(expected), "FIELDS_MISMATCH", path)


def identifier(value, path):
    require(type(value) is str and re.fullmatch(r"[A-Za-z0-9_-]{1,64}", value),
            "INVALID_ID", path)
    return value


def number(value, path, nullable=False):
    if value is None and nullable:
        return None
    # Strings only: disallow floats, bools, negatives, exponent notation, NaN/Inf,
    # whitespace, locale separators; at most 12 integer and 6 fractional digits.
    require(type(value) is str and re.fullmatch(r"(?:0|[1-9][0-9]{0,11})(?:\.[0-9]{1,6})?", value),
            "INVALID_DECIMAL", path)
    return Decimal(value)


def decimal_text(value):
    text = format(value, "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                    allow_nan=False).encode()).hexdigest()


def normalize(payload):
    require(type(payload) is dict, "OBJECT_REQUIRED", "$")
    broker = payload.get("broker")
    require(type(broker) is str and broker in ADAPTERS, "UNKNOWN_BROKER", "$.broker")
    row_field, mapping = ADAPTERS[broker]
    keys(payload, ("version", "broker", "account", "snapshot_id", "as_of", row_field), "$")
    require(type(payload["version"]) is int and payload["version"] == 1,
            "UNSUPPORTED_VERSION", "$.version")
    account = identifier(payload["account"], "$.account")
    snapshot_id = identifier(payload["snapshot_id"], "$.snapshot_id")
    stamp = payload["as_of"]
    require(type(stamp) is str and re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z", stamp),
            "INVALID_TIMESTAMP", "$.as_of")
    try:
        datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError as exc:
        raise ContractError("INVALID_TIMESTAMP", "$.as_of") from exc
    rows = payload[row_field]
    require(type(rows) is list and len(rows) <= MAX_ROWS, "INVALID_ROWS", f"$.{row_field}")
    positions, seen = [], set()
    for i, raw in enumerate(rows):
        path = f"$.{row_field}[{i}]"
        keys(raw, mapping.values(), path)
        row = {k: raw[v] for k, v in mapping.items()}
        instrument = row["instrument_id"]
        require(type(instrument) is str and instrument in INSTRUMENTS, "UNKNOWN_INSTRUMENT", path)
        require((row["symbol"], row["mic"], row["currency"]) == INSTRUMENTS[instrument],
                "INSTRUMENT_IDENTITY_MISMATCH", path)
        require(instrument not in seen, "DUPLICATE_POSITION", path)
        seen.add(instrument)
        qty = number(row["quantity"], path + "." + mapping["quantity"])
        price = number(row["price"], path + "." + mapping["price"], nullable=True)
        # Max 36 product digits, plus <4 digits for a 1000-row sum. No implicit rounding.
        with localcontext(ARITHMETIC):
            value = None if price is None else qty * price
        positions.append({"instrument_id": instrument, "symbol": row["symbol"],
                          "mic": row["mic"], "currency": row["currency"],
                          "quantity": decimal_text(qty),
                          "price": None if price is None else decimal_text(price),
                          "market_value": None if value is None else decimal_text(value)})
    return {"broker": broker, "account": account, "snapshot_id": snapshot_id, "as_of": stamp,
            "positions": sorted(positions, key=lambda p: p["instrument_id"])}


def strict_json(data):
    def pairs(items):
        result = {}
        for key, value in items:
            require(key not in result, "DUPLICATE_JSON_KEY", "$")
            result[key] = value
        return result

    def constant(_):
        raise ContractError("NONFINITE_JSON", "$")

    try:
        return json.loads(data.decode("utf-8"), object_pairs_hook=pairs, parse_constant=constant)
    except ContractError:
        raise
    except (UnicodeDecodeError, ValueError, RecursionError) as exc:
        raise ContractError("INVALID_JSON", "$") from exc


class Pipeline:
    """Single-run ordered snapshot reducer. Failed inputs never mutate accepted state."""
    def __init__(self):
        self.current = {}
        self.ledger = {}
        self.events = []

    def ingest(self, data, source, truncated=False):
        event = {"source": source,
                 "raw_sha256": None if truncated else hashlib.sha256(data).hexdigest(),
                 "prefix_sha256": hashlib.sha256(data).hexdigest() if truncated else None,
                 "bytes_read": len(data), "truncated": truncated,
                 "rows_seen": None, "status": "REJECTED"}
        try:
            require(len(data) <= MAX_BYTES, "INPUT_TOO_LARGE", "$")
            payload = strict_json(data)
            if type(payload) is dict and type(payload.get("broker")) is str and payload["broker"] in ADAPTERS:
                rows = payload.get(ADAPTERS[payload["broker"]][0])
                if type(rows) is list:
                    event["rows_seen"] = len(rows)
            snapshot = normalize(payload)
            key = (snapshot["broker"], snapshot["account"])
            delivery = (*key, snapshot["snapshot_id"])
            fingerprint = digest(snapshot)
            event.update(broker=key[0], account=key[1], snapshot_id=snapshot["snapshot_id"],
                         canonical_sha256=fingerprint)
            if delivery in self.ledger:
                require(self.ledger[delivery] == fingerprint, "REPLAY_CONFLICT", "$.snapshot_id")
                event["status"] = "REPLAY"
            else:
                previous = self.current.get(key)
                if previous:
                    require(snapshot["as_of"] > previous["as_of"],
                            "STALE_OR_EQUAL_TIMESTAMP", "$.as_of")
                self.ledger[delivery] = fingerprint
                self.current[key] = snapshot
                event["status"] = "ACCEPTED"
        except ContractError as exc:
            event.update(code=exc.code, path=exc.path)
        self.events.append(event)

    def report(self):
        accounts = []
        for key in sorted(self.current):
            snapshot = self.current[key]
            buckets = defaultdict(list)
            for position in snapshot["positions"]:
                buckets[position["currency"]].append(position)
            totals = []
            for currency, rows in sorted(buckets.items()):
                missing = sum(p["market_value"] is None for p in rows)
                with localcontext(ARITHMETIC):
                    known = sum((Decimal(p["market_value"]) for p in rows
                                 if p["market_value"] is not None), Decimal(0))
                totals.append({"currency": currency, "known_market_value": decimal_text(known),
                               "missing_price_count": missing, "complete": missing == 0})
            accounts.append({**snapshot, "valuations": totals,
                             "empty_snapshot": not snapshot["positions"]})
        counts = Counter(e["status"] for e in self.events)
        # Invariants are always evaluated, not Python assert statements (-O safe).
        require(sum(counts.values()) == len(self.events), "ACCOUNTING_FAILURE", "$report")
        require(len(self.ledger) == counts["ACCEPTED"], "LEDGER_FAILURE", "$report")
        for account in accounts:
            require(sum(len([p for p in account["positions"] if p["currency"] == v["currency"]])
                        for v in account["valuations"]) == len(account["positions"]),
                    "VALUATION_COVERAGE_FAILURE", "$report")
            key = (account["broker"], account["account"], account["snapshot_id"])
            require(self.ledger.get(key) == digest(self.current[key[:2]]), "STATE_HASH_FAILURE", "$report")
        incomplete = any(not v["complete"] for a in accounts for v in a["valuations"])
        status = ("WARN" if counts["REJECTED"] else
                  "SUSPECT" if not accounts or not any(a["positions"] for a in accounts) else
                  "WARN" if incomplete else "OK")
        result = {"contract": "synthetic-holdings/v1", "status": status,
                "input_count": len(self.events),
                "counts": {k.lower(): counts[k] for k in ("ACCEPTED", "REPLAY", "REJECTED")},
                "row_accounting": {
                    "recognized_input_rows": sum(e["rows_seen"] or 0 for e in self.events),
                    "accepted_rows": sum(e["rows_seen"] for e in self.events if e["status"] == "ACCEPTED"),
                    "replayed_rows": sum(e["rows_seen"] for e in self.events if e["status"] == "REPLAY"),
                    "rejected_rows": sum(e["rows_seen"] or 0 for e in self.events if e["status"] == "REJECTED"),
                    "unknown_row_count_inputs": sum(e["rows_seen"] is None for e in self.events),
                    "active_rows": sum(len(a["positions"]) for a in accounts),
                    "superseded_rows": sum(e["rows_seen"] for e in self.events if e["status"] == "ACCEPTED")
                                       - sum(len(a["positions"]) for a in accounts)},
                "events": list(self.events), "accounts": accounts}
        rows = result["row_accounting"]
        require(rows["recognized_input_rows"] == rows["accepted_rows"] + rows["replayed_rows"] + rows["rejected_rows"],
                "ROW_ACCOUNTING_FAILURE", "$report")
        require(rows["superseded_rows"] >= 0, "ROW_ACCOUNTING_FAILURE", "$report")
        return deepcopy(result)


def log_event(path, event):
    try:
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event, sort_keys=True) + "\n")
    except OSError as exc:
        print(f"FAIL: audit log unavailable: {type(exc).__name__}", file=sys.stderr)
        return False
    return True


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("inputs", nargs="+", type=Path, help="Ordered whole-account JSON snapshots")
    parser.add_argument("--log", type=Path, default=Path("audit.jsonl"), help="Append-only run diagnostics")
    args = parser.parse_args(argv)
    pipeline = Pipeline()
    # Must occur before error logging too: otherwise the failure handler itself appends
    # to an input. resolve catches symlinks; samefile catches existing hard links.
    try:
        for path in args.inputs:
            if (args.log.resolve() == path.resolve() or
                    (args.log.exists() and path.exists() and args.log.samefile(path))):
                print("FAIL: audit path aliases an input; nothing written", file=sys.stderr)
                return 1
    except OSError as exc:
        print(f"FAIL: cannot validate audit path: {exc}", file=sys.stderr)
        return 1
    try:
        for path in args.inputs:
            with path.open("rb") as handle:
                data = handle.read(MAX_BYTES + 1)
            pipeline.ingest(data, path.as_posix(), truncated=len(data) > MAX_BYTES)
        report = pipeline.report()
        if not log_event(args.log, report):
            return 1
        print(json.dumps(report, indent=2, sort_keys=True, allow_nan=False))
        return 2 if report["counts"]["rejected"] else 0
    except (OSError, ValueError, TypeError) as exc:
        log_event(args.log, {"status": "FAIL", "error_type": type(exc).__name__,
                             "detail": str(exc), "events": pipeline.events})
        print(f"FAIL: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
