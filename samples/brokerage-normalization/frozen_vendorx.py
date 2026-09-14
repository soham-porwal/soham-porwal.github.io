"""Frozen deterministic transformer for the synthetic `vendorx` dialect.

FROZEN_MAPPING below was proposed once by a local model, reviewed by a human, and then
written out as a literal. This module never calls a model, never reads the proposal file,
and never dispatches on model output; examples/mapping-proposal.json is review-only
evidence of where the literal came from, not an input. See MAPPING-FREEZE.md.

Freezing is the point. A new vendor field does not widen this transformer: it raises
SCHEMA_DRIFT, the capture quarantines, and the change goes back through propose -> review
-> freeze. Silent absorption of an unreviewed field is the failure this design prevents.

`vendorx` is synthetic, used to demonstrate that path end to end. It is deliberately NOT
registered in broker_adapters.PROFILES -- registering a dialect is itself a reviewed step,
and this sample does not claim a real broker integration it never observed.
"""
import re

from broker_adapters import row
from broker_engine import check, dec, object_fields, text

PROFILE_NAME = "vendorx"
DIALECT_VERSION = "vendorx.positions.v1"

# Frozen 2026-09-13 from examples/mapping-proposal.json after human review, accepted
# unchanged. Edit only by repeating that review; this literal is the contract.
FROZEN_MAPPING = {
    "units_held": "quantity",
    "unit_quote": "price",
    "security_code": "instrument_id",
    "quote_ccy": "currency",
}
REVIEWED_PROPOSAL = {
    "path": "examples/mapping-proposal.json",
    "model": "qwen3:14b",
    "response_sha256": "0222fd022b36f44d598a43b4631046135ba8553397e1f0d3d301e207a8c4e63e",
    "reviewed_utc": "2026-09-13",
    "decision": "accepted unchanged; representation rules added by review, not by the model",
}


def vendorx(payload, account, context):
    """Map one vendorx payload to canonical rows. Signature matches broker_adapters.PROFILES."""
    object_fields(payload, ("dialect_version", "account_ref", "positions"), path="$.payload")
    # A vendor version bump is an unreviewed dialect, not a compatible upgrade.
    check(payload["dialect_version"] == DIALECT_VERSION,
          "VENDORX_DIALECT_UNREVIEWED", "$.payload.dialect_version")
    check(text(payload["account_ref"], "$.payload.account_ref") == account,
          "ACCOUNT_MISMATCH", "$.payload.account_ref")
    check(type(payload["positions"]) is list, "UNRECOGNIZED_VENDORX_SHAPE", "$.payload.positions")
    output, seen = [], set()
    for i, p in enumerate(payload["positions"]):
        path = f"$.payload.positions[{i}]"
        # Exactly the reviewed source allowlist. An added field is drift, never a guess.
        object_fields(p, tuple(FROZEN_MAPPING), path=path)
        code = text(p["security_code"], path + ".security_code")
        check(code not in seen, "DUPLICATE_SECURITY", path + ".security_code")
        seen.add(code)
        currency = text(p["quote_ccy"], path + ".quote_ccy")
        check(re.fullmatch(r"[A-Z]{3}", currency) is not None,
              "UNSUPPORTED_CURRENCY", path + ".quote_ccy")
        # dec() refuses JSON floats, so a decimal quantity must arrive as a string. The model
        # proposed names and types; it could not decide representation. Review did.
        output.append(row(
            [("native", code)],
            None,  # the reviewed mapping carries no asset class; the registry decides it
            currency,
            dec(p["units_held"], path=path + ".units_held"),
            dec(p["unit_quote"], signed=False, path=path + ".unit_quote"),
            context["price_timestamps"].get(code),
            None,  # dialect reports no position value; never inferred from quantity * price
            None,  # dialect reports no cost basis
        ))
    return {"rows": output}
