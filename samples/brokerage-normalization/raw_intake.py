"""Wrap one raw broker export in a capture envelope. Adds no validation and removes none.

A capture is the unit this pipeline trusts: one account, one observation time, one complete
response. A raw export carries only part of that — the rows, and sometimes a source date. The
envelope fields it cannot know (which account, which broker dialect, what time the holdings
were observed) come from explicit flags, never from a guess, never from the clock.

This module therefore makes exactly one judgement: whether it was told enough to build an
envelope at all. Every judgement about the data itself stays in broker_engine, after wrapping,
so a wrapped export is held to the same contract as a hand-authored capture.
"""
import hashlib
import json
import re

from broker_adapters import PROFILES
from broker_engine import read_json, stamp

# The capture contract's observation time is exactly this shape. stamp() alone is more
# permissive than the contract - it parses an offset like +00:00 that a capture must not
# carry - so the wrapper checks the literal grammar first and rejects early rather than
# emitting an envelope the engine would refuse for a reason the operator cannot see here.
AS_OF_GRAMMAR = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z\Z")

# A raw export reaches its adapter either as parsed JSON or as the original text. A profile
# missing from this map is refused rather than assumed: adding an adapter must include a
# deliberate statement of how its raw bytes are carried.
PAYLOAD_KIND = {"alpaca": "json", "plaid": "json", "fidelity_csv": "text"}

TOTAL_SCOPES = ("holdings", "holdings_and_cash")


class IntakeError(ValueError):
    """The wrapper was not told enough to build an envelope. Operational, not a data verdict."""


def snapshot_id(raw):
    """Content-addressed, so re-wrapping one unchanged export replays instead of re-landing.

    Two wraps of the same bytes produce the same id and the same envelope, which the accepted
    ledger then recognises as a replay. A changed export yields a different id and is judged
    on its merits.
    """
    return "raw-" + hashlib.sha256(raw).hexdigest()[:12]


def wrap(raw, broker, account, as_of, currency="USD", total_scope="holdings"):
    """Build one capture/v1 envelope around raw export bytes.

    Raises IntakeError for anything the wrapper cannot legitimately decide. Notably it never
    substitutes the current time for as_of: download time is not observation time, and a stale
    export relabelled as fresh is the failure this pipeline exists to prevent.
    """
    if broker not in PROFILES:
        raise IntakeError(f"unknown broker {broker!r}; known profiles: {', '.join(sorted(PROFILES))}")
    if broker not in PAYLOAD_KIND:
        raise IntakeError(f"profile {broker!r} declares no raw payload kind; refusing to assume one")
    if total_scope not in TOTAL_SCOPES:
        raise IntakeError(f"total_scope must be one of {', '.join(TOTAL_SCOPES)}")
    if not account:
        raise IntakeError("--account is required; the export's own account field is checked against it")
    if not (isinstance(as_of, str) and AS_OF_GRAMMAR.fullmatch(as_of)):
        raise IntakeError("--as-of must be an exact YYYY-MM-DDTHH:MM:SSZ observation time")
    try:
        stamp(as_of)
    except Exception as exc:
        # Well-shaped but not a real instant, e.g. February 30th.
        raise IntakeError(f"--as-of is not a valid calendar instant: {exc}") from exc

    if PAYLOAD_KIND[broker] == "json":
        # Reuse the engine's strict reader so duplicate keys and nonfinite constants are
        # refused here exactly as they would be inside a hand-authored capture.
        payload = read_json(raw)
    else:
        try:
            payload = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise IntakeError(f"export is not UTF-8 text: {exc}") from exc

    return {
        "schema": "capture/v1",
        "broker": broker,
        "account": account,
        "snapshot_id": snapshot_id(raw),
        "as_of": as_of,
        "pages": [{"cursor": None, "next_cursor": None, "status": 200, "payload": payload}],
        "context": {
            "currency": currency,
            # No quote timestamps are invented. Without them the engine reports a valuation as
            # incomplete rather than treating the export's download time as a quote time.
            "price_timestamps": {},
            "cash_balances": [],
            "reported_total": None,
            "total_scope": total_scope,
            "expected_rows": None,
            "settlement_basis": "broker_reported_positions",
        },
    }


def wrap_file(path, broker, account, as_of, currency="USD", total_scope="holdings", max_bytes=None):
    """Read one export read-only and return (capture dict, capture bytes). Never writes."""
    with path.open("rb") as stream:
        raw = stream.read() if max_bytes is None else stream.read(max_bytes + 1)
    if not raw:
        raise IntakeError(f"{path} is empty; an empty file is not an empty account")
    if max_bytes is not None and len(raw) > max_bytes:
        raise IntakeError(f"{path} exceeds the {max_bytes}-byte capture limit; no partial wrap is written")
    capture = wrap(raw, broker, account, as_of, currency, total_scope)
    body = json.dumps(capture, indent=2, sort_keys=True, allow_nan=False).encode("utf-8")
    return capture, body
