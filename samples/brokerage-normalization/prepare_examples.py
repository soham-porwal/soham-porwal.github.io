"""Rebuild authored capture wrappers from preserved source fixtures; never fetches or executes code.

Regeneration is ADDITIVE. reference.json and captures/provenance.json are reviewed
registries that later work appends to, so this script refreshes only the records it
authors itself and leaves every other record in place, in place. A rerun that quietly
dropped a reviewed instrument, alias, provenance row or capture would be indistinguishable
from a correct rerun until some unrelated test failed days later, which is precisely the
silent-failure class this file is written to avoid.
"""
import csv
from decimal import Decimal
import hashlib
import io
import json
from pathlib import Path
import sys

from broker_engine import read_json

HERE = Path(__file__).resolve().parent

# Written only when reference.json does not exist yet. An on-disk marker records a human
# review this script did not perform, so it is preserved rather than rolled back.
AUTHORED_VERSION = "reviewed-samples-2026-09-v1"


def wire(value):
    """Serialize Decimal as a JSON number without a binary-float intermediate."""
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise ValueError("nonfinite fixture")
        return format(value, "f")
    if type(value) is float:
        raise ValueError("binary float forbidden")
    if type(value) is list:
        return "[" + ",".join(wire(v) for v in value) + "]"
    if type(value) is dict:
        return "{" + ",".join(json.dumps(k) + ":" + wire(v) for k, v in value.items()) + "}"
    return json.dumps(value, allow_nan=False)


def capture(broker, account, payload, sid, as_of="2026-09-10T20:00:00Z"):
    return {"schema": "capture/v1", "broker": broker, "account": account, "snapshot_id": sid,
            "as_of": as_of, "pages": [{"cursor": None, "next_cursor": None, "status": 200, "payload": payload}],
            "context": {"currency": "USD", "price_timestamps": {}, "cash_balances": [],
                        "reported_total": None, "total_scope": "holdings", "expected_rows": None,
                        "settlement_basis": "broker_reported_positions"}}


def alias_key(alias):
    """Effective-dated lookup coordinates; two brokers may map one ticker differently."""
    return tuple(alias[field] for field in ("broker", "scheme", "value", "from", "until"))


def merge(on_disk, authored, key, label):
    """Refresh the records this script authors; keep every other reviewed record in place.

    Output order follows the file already on disk, with genuinely new authored records
    appended at the end, so a second run is byte-stable and nothing is renumbered.
    Returns the merged list, the identities preserved rather than authored, and the
    authored identities that are absent from the on-disk registry.
    """
    fresh = {}
    for record in authored:
        if key(record) in fresh:
            raise ValueError(f"authored {label} repeats key {key(record)!r}")
        fresh[key(record)] = record
    merged, seen, preserved = [], set(), []
    for record in on_disk:
        identity = key(record)
        if identity in seen:
            raise ValueError(f"on-disk {label} repeats key {identity!r}; refusing to rewrite it")
        seen.add(identity)
        if identity in fresh:
            merged.append(fresh[identity])
        else:
            merged.append(record)
            preserved.append(identity)
    introduced = [identity for identity in fresh if identity not in seen]
    merged.extend(fresh[identity] for identity in introduced)
    return merged, preserved, introduced


def read_existing(path, expected_type, label):
    """Reviewed state already on disk, or None when the file is genuinely absent.

    A malformed file is never treated as absent. Falling back to a fresh write there is
    exactly how a reviewed registry gets clobbered while the run still prints success, so
    an unreadable registry raises and the run exits nonzero without writing anything.
    """
    if not path.exists():
        return None
    document = read_json(path.read_bytes())
    if type(document) is not expected_type:
        raise ValueError(f"{label} is not a JSON {expected_type.__name__}; refusing to overwrite it")
    return document


def main():
    try:
        source = HERE / "sources"
        dest = HERE / "captures"
        dest.mkdir(exist_ok=True)
        instruments, aliases = [], []
        def security(key, kind, broker, refs, **extra):
            instruments.append({"id": key, "type": kind, "currency": "USD", "multiplier": "1", "status": "active", **extra})
            for scheme, value in refs:
                aliases.append({"broker": broker, "scheme": scheme, "value": value, "instrument_id": key,
                                "from": "2000-01-01T00:00:00Z", "until": "2100-01-01T00:00:00Z"})
        alpaca = read_json((source / "alpaca-sdk-positions-response.json").read_bytes())
        security("sample:AAPL", "equity", "alpaca", [("native", alpaca[0]["asset_id"]), ("ticker", "AAPL")])
        plaid_security = read_json((source / "plaid-docs-security.json").read_bytes())
        holding = read_json((source / "plaid-docs-holding.json").read_bytes())
        # The FIGI this security carries is deliberately NOT registered. Capture 02 supplies it,
        # resolve() requires every supplied strong identifier to resolve, and so capture 02 fails
        # closed on an unregistered FIGI - which is the whole reason that capture is kept. Authoring
        # the alias here would silently make capture 02 resolve instead. Measured 2026-09-13: doing
        # so turns capture 02 into an accepted WARN snapshot over sample:DBLTX and breaks test_26.
        security("sample:DBLTX", "fund", "plaid", [("native", plaid_security["security_id"]), ("ticker", "DBLTX"),
                                                   ("cusip", plaid_security["cusip"]), ("isin", plaid_security["isin"])])
        for symbol in ("VTI", "SPAXX**", "FXAIX"):
            security("sample:" + symbol, "fund", "fidelity_csv", [("ticker", symbol)])
        # Standard option fixture terms are invented, not a quote or broker observation.
        security("synthetic:AAPL-call", "option", "alpaca", [("native", "synthetic-option"), ("ticker", "AAPL261218C00200000")],
                 multiplier="100", underlying_id="sample:AAPL", expiry="2026-12-18", right="call", strike="200", deliverable="standard")
        csv_original = (source / "fidelity-external-2026-07-02.csv").read_text(encoding="utf-8-sig")
        cases = {
            "01-alpaca-upstream.json": capture("alpaca", "vendor-demo-account", alpaca, "upstream-sdk-1"),
            "02-plaid-upstream.json": capture("plaid", holding["account_id"],
                {"accounts": [{"account_id": holding["account_id"]}], "holdings": [holding], "securities": [plaid_security]}, "docs-1"),
            "03-fidelity-original.json": capture("fidelity_csv", "Z12345678", csv_original, "external-july-2", "2026-07-02T20:00:00Z")}
        # Explicit derived single-account fixture: omit pending line and the other account.
        # The original multi-account/pending report above is retained and must quarantine.
        rows = list(csv.reader(io.StringIO(csv_original)))
        clean = io.StringIO(newline="")
        writer = csv.writer(clean)
        writer.writerow(rows[1])
        writer.writerows(r for r in rows[2:] if r and r[0] == "Z12345678" and r[2] != "Pending Activity")
        cases["04-fidelity-derived.json"] = capture("fidelity_csv", "Z12345678", clean.getvalue(), "derived-july-2", "2026-07-02T20:00:00Z")
        option = {"asset_id": "synthetic-option", "symbol": "AAPL261218C00200000", "asset_class": "us_option",
                  "qty": "-2", "side": "short", "market_value": "-500", "cost_basis": "-450", "current_price": "2.5"}
        cases["05-synthetic-short-option.json"] = capture("alpaca", "synthetic-option-account", [option], "option-1")
        cases["05-synthetic-short-option.json"]["context"].update(price_timestamps={"synthetic-option": "2026-09-10T19:59:00Z"},
            reported_total="-500", expected_rows=1)
        cases["06-unknown-quarantine.json"] = capture("robinhood", "synthetic-unverified", {"results": []}, "unknown-1")

        # Everything is built and merged in memory before a single byte is written, so a
        # failure part way through cannot leave half a rewritten registry on disk.
        authored_captures = {name: (wire(value) + "\n").encode() for name, value in cases.items()}
        authored_provenance = [{"file": "captures/" + name, "sha256": hashlib.sha256(raw).hexdigest(),
                                "class": "synthetic_edge_case" if name.startswith(("05", "06")) else
                                         "derived_external_fixture" if name.startswith("04") else "assembled_upstream_example",
                                "capture_as_of_is_authored": True}
                               for name, raw in authored_captures.items()]
        foreign_captures = sorted(p.name for p in dest.glob("*.json")
                                  if p.name != "provenance.json" and p.name not in cases)
        foreign_digests = {name: hashlib.sha256((dest / name).read_bytes()).hexdigest()
                           for name in foreign_captures}

        existing_reference = read_existing(HERE / "reference.json", dict, "reference.json")
        if existing_reference is None:
            prior_instruments, prior_aliases, version = [], [], AUTHORED_VERSION
        else:
            missing = {"version", "instruments", "aliases"} - existing_reference.keys()
            if missing:
                raise ValueError(f"reference.json lacks {sorted(missing)}; refusing to overwrite reviewed state")
            prior_instruments, prior_aliases = existing_reference["instruments"], existing_reference["aliases"]
            if type(prior_instruments) is not list or type(prior_aliases) is not list:
                raise ValueError("reference.json instruments/aliases are not lists; refusing to overwrite")
            version = existing_reference["version"]
        existing_provenance = read_existing(dest / "provenance.json", list, "captures/provenance.json")
        merged_instruments, kept_instruments, new_instruments = merge(
            prior_instruments, instruments, lambda i: i["id"], "instrument")
        merged_aliases, kept_aliases, new_aliases = merge(prior_aliases, aliases, alias_key, "alias")
        merged_provenance, kept_provenance, new_provenance = merge(
            existing_provenance or [], authored_provenance, lambda p: p["file"], "provenance row")
        reference = {"version": version, "instruments": merged_instruments, "aliases": merged_aliases}

        # Adding an identity to a registry that already exists changes how captures resolve, so it
        # is never done silently. This is the guard that would have caught the FIGI alias above:
        # authoring one extra alias flipped capture 02 from quarantined to accepted.
        introduced = ([("instrument", i) for i in (new_instruments if existing_reference else [])]
                      + [("alias", a) for a in (new_aliases if existing_reference else [])]
                      + [("provenance row", p) for p in (new_provenance if existing_provenance is not None else [])])
        if introduced:
            raise ValueError(
                "regeneration would introduce %d record(s) absent from the reviewed registry on "
                "disk: %s. Reviewed state is not extended automatically; add them deliberately or "
                "stop authoring them here. Nothing was written."
                % (len(introduced), ", ".join("%s %r" % pair for pair in introduced)))

        # A JSON number anywhere in a reviewed registry arrives here as Decimal and makes
        # json.dumps raise, which fails the run closed rather than writing a mangled file.
        reference_text = json.dumps(reference, indent=2) + "\n"
        provenance_text = json.dumps(merged_provenance, indent=2) + "\n"
        (HERE / "reference.json").write_text(reference_text, encoding="utf-8")
        for name, raw in authored_captures.items():
            (dest / name).write_bytes(raw)
        (dest / "provenance.json").write_text(provenance_text, encoding="utf-8")

        # Verify stage. Read back what was just written and prove nothing was lost, so a
        # regression fails here rather than in an unrelated test days later.
        written_reference = read_existing(HERE / "reference.json", dict, "reference.json")
        written_provenance = read_existing(dest / "provenance.json", list, "captures/provenance.json")
        for label, got, want in (
                ("instruments", {i["id"] for i in written_reference["instruments"]},
                 {i["id"] for i in merged_instruments}),
                ("aliases", {alias_key(a) for a in written_reference["aliases"]},
                 {alias_key(a) for a in merged_aliases}),
                ("provenance rows", {p["file"] for p in written_provenance},
                 {p["file"] for p in merged_provenance})):
            if got != want:
                raise ValueError(f"round-trip check lost {label}: {sorted(want - got)!r}")
        if written_reference["version"] != version:
            raise ValueError("round-trip check lost the reviewed registry version marker")
        for name, digest in foreign_digests.items():
            if hashlib.sha256((dest / name).read_bytes()).hexdigest() != digest:
                raise ValueError(f"captures/{name} is not authored here but changed on disk")
        if not authored_captures or not merged_instruments:
            # Succeeded while producing nothing is SUSPECT, never OK.
            print("SUSPECT: regeneration produced no captures or no instruments", file=sys.stderr)
            return 1

        print("OK: rewrote %d authored captures, %d instruments, %d aliases, %d provenance rows"
              % (len(authored_captures), len(instruments), len(aliases), len(authored_provenance)))
        print("    preserved %d instruments %s, %d aliases, %d provenance rows %s, %d captures %s"
              % (len(kept_instruments), sorted(kept_instruments),
                 len(kept_aliases), len(kept_provenance), sorted(kept_provenance),
                 len(foreign_captures), foreign_captures))
        print("    registry version %s (%s)"
              % (version, "authored here" if existing_reference is None else "preserved from disk, not rolled back"))
        print("    source files unchanged; no fetch performed and no generated code executed")
        return 0
    except Exception as exc:
        print(f"FAIL preparing examples: {type(exc).__name__}: {exc}", file=sys.stderr)
        record = {"status": "FAIL", "error_type": type(exc).__name__}
        try:
            with (HERE / "prepare-errors.jsonl").open("a", encoding="utf-8") as log:
                log.write(json.dumps(record) + "\n")
        except Exception as logging_error:
            # A failure to log must not replace the original failure with its own traceback.
            print(json.dumps({**record, "logging_error_type": type(logging_error).__name__}),
                  file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
