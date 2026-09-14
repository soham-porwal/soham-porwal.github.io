"""Offline guard tests; no real model is called by this suite."""
from copy import deepcopy
from decimal import Decimal
import inspect
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import broker_adapters
from broker_engine import Rejected
import dev_mapping as dev
import frozen_vendorx

ROOT = Path(__file__).resolve().parents[1]
VALID = {"units_held": "quantity", "unit_quote": "price",
         "security_code": "instrument_id", "quote_ccy": "currency"}


def response(content=None):
    return {"model": dev.MODEL, "done": True, "done_reason": "stop",
            "message": {"content": json.dumps({"mapping": VALID}) if content is None else content}}


class MappingGuards(unittest.TestCase):
    def test_unknown_fields(self):
        for mapping in ({**VALID, "real_account": "currency"},
                        {**VALID, "quote_ccy": "execute"},
                        {**VALID, "units_held": "price"},
                        {**VALID, "units_held": "currency", "quote_ccy": "quantity"}):
            with self.subTest(mapping=mapping), self.assertRaises(ValueError):
                dev.validate_mapping({"mapping": mapping})

    def test_malicious_model_response(self):
        for raw in ('__import__("os").system("echo bad")',
                    json.dumps({"mapping": VALID, "code": "print(1)"}),
                    '{"mapping":{},"mapping":{}}'):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                dev.validate_mapping(dev.strict_json(raw))

    def test_endpoint_and_redirect_restriction(self):
        with patch("urllib.request.build_opener") as opener:
            for url in ("https://example.com/api/chat", "http://localhost:11434/api/chat",
                        dev.CHAT_URL + "?destination=remote", "http://127.0.0.1:11435/api/chat"):
                with self.subTest(url=url), self.assertRaises(ValueError):
                    dev.local_json(url)
            opener.assert_not_called()
        with self.assertRaises(ValueError):
            dev.NoRedirect().redirect_request(None, None, 302, "", {}, "https://example.com")

    def test_no_promotion_and_overwrite_guard(self):
        with tempfile.TemporaryDirectory(prefix="mapping-test-", dir=ROOT) as directory:
            output = Path(directory) / "proposal.json"
            with patch.object(dev, "local_json", side_effect=[{"models": []}, response()]) as call:
                proposal = dev.create_proposal(output)
                self.assertEqual(proposal["status"], "review_required")
                self.assertTrue(proposal["synthetic_only"])
                self.assertEqual(proposal["mapping"], VALID)
                self.assertEqual(call.call_count, 2)
            before = output.read_bytes()
            with patch.object(dev, "local_json") as call, self.assertRaises(FileExistsError):
                dev.create_proposal(output)
            call.assert_not_called()
            self.assertEqual(output.read_bytes(), before)
            self.assertEqual({p.name for p in Path(directory).iterdir()},
                             {"proposal.json", "proposal.json.events.jsonl"})

    def test_invalid_response_is_logged_without_artifact(self):
        with tempfile.TemporaryDirectory(prefix="mapping-test-", dir=ROOT) as directory:
            output = Path(directory) / "proposal.json"
            with patch.object(dev, "local_json", side_effect=[{"models": []}, response("bad")]):
                self.assertEqual(dev.main(["--output", str(output)]), 1)
            self.assertFalse(output.exists())
            events = Path(str(output) + ".events.jsonl").read_text()
            self.assertIn('"severity":"FAIL"', events)

    def test_truncated_or_wrong_model_is_rejected(self):
        for change in ({"done": False}, {"done_reason": "length"}, {"model": "other"}):
            with tempfile.TemporaryDirectory(prefix="mapping-test-", dir=ROOT) as directory:
                output = Path(directory) / "proposal.json"
                with patch.object(dev, "local_json", side_effect=[{"models": []}, {**response(), **change}]):
                    self.assertEqual(dev.main(["--output", str(output)]), 1)
                self.assertFalse(output.exists())


def comparable(value):
    """Decimals to strings and tuples to lists, so a fixture can be read by eye."""
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (list, tuple)):
        return [comparable(v) for v in value]
    if isinstance(value, dict):
        return {k: comparable(v) for k, v in value.items()}
    return value


def load(name):
    return json.loads((ROOT / "examples" / name).read_text(encoding="utf-8"))


class FrozenTransformer(unittest.TestCase):
    """The second half of the design: reviewed once, then frozen into ordinary code."""

    def test_frozen_vendorx_transformer_matches_fixture(self):
        fixture = load("vendorx-frozen-fixture.json")
        rows = frozen_vendorx.vendorx(fixture["payload"], fixture["account"], fixture["context"])["rows"]
        self.assertEqual(comparable(rows), fixture["expected_rows"])
        # No quote timestamp is invented for the rows the dialect does not stamp.
        self.assertEqual([r["price_at"] for r in rows], ["2026-07-02T20:00:00Z", None, None])
        # Value and basis stay null rather than being derived from quantity * price.
        self.assertEqual({r["reported_value"] for r in rows} | {r["cost_basis"] for r in rows}, {None})
        repeat = frozen_vendorx.vendorx(fixture["payload"], fixture["account"], fixture["context"])["rows"]
        self.assertEqual(comparable(repeat), comparable(rows))

    def test_frozen_vendorx_rejects_unreviewed_input(self):
        fixture = load("vendorx-frozen-fixture.json")
        account, context = fixture["account"], fixture["context"]

        def payload(position=None, **overrides):
            base = deepcopy(fixture["payload"])
            if position is not None:
                base["positions"][0] = {**base["positions"][0], **position}
            return {**base, **overrides}

        cases = {
            # An unreviewed vendor field is drift, never an inferred mapping.
            "SCHEMA_DRIFT": payload({"settlement_hint": "T+1"}),
            "VENDORX_DIALECT_UNREVIEWED": payload(dialect_version="vendorx.positions.v2"),
            "ACCOUNT_MISMATCH": payload(account_ref="VX-ACCOUNT-0002"),
            "UNRECOGNIZED_VENDORX_SHAPE": payload(positions={"units_held": "1"}),
            # A JSON float would silently lose precision; the dialect must send a string.
            "INVALID_DECIMAL": payload({"units_held": 125.0}),
            "NEGATIVE_VALUE": payload({"unit_quote": "-17.40"}),
            "UNSUPPORTED_CURRENCY": payload({"quote_ccy": "usd"}),
            "INVALID_TEXT": payload({"security_code": ""}),
        }
        for code, value in cases.items():
            with self.subTest(code=code):
                with self.assertRaises(Rejected) as caught:
                    frozen_vendorx.vendorx(value, account, context)
                self.assertEqual(caught.exception.code, code)
        duplicated = deepcopy(fixture["payload"])
        duplicated["positions"][1]["security_code"] = duplicated["positions"][0]["security_code"]
        with self.assertRaises(Rejected) as caught:
            frozen_vendorx.vendorx(duplicated, account, context)
        self.assertEqual(caught.exception.code, "DUPLICATE_SECURITY")

    def test_frozen_vendorx_mapping_matches_reviewed_proposal(self):
        proposal = load("mapping-proposal.json")
        for field in ("model", "request_sha256", "response_sha256", "created_utc", "synthetic_only"):
            self.assertIn(field, proposal)
        self.assertTrue(proposal["synthetic_only"])
        self.assertEqual(proposal["status"], "review_required")
        # Regenerating the proposal must not silently re-bless the frozen literal: a new
        # model run changes response_sha256 and fails here until a human reviews again.
        self.assertEqual(frozen_vendorx.FROZEN_MAPPING, proposal["mapping"])
        self.assertEqual(frozen_vendorx.REVIEWED_PROPOSAL["model"], proposal["model"])
        self.assertEqual(frozen_vendorx.REVIEWED_PROPOSAL["response_sha256"], proposal["response_sha256"])
        # The tie is asserted here, in the test. The module never reads the proposal itself.
        source = (ROOT / "frozen_vendorx.py").read_text(encoding="utf-8")
        self.assertNotIn("open(", source)
        self.assertNotIn("import json", source)
        self.assertNotIn("json.load", source)

    def test_frozen_transformer_has_no_automatic_promotion_path(self):
        # Registering a dialect is a reviewed step; importing the module must not perform it.
        self.assertNotIn("vendorx", broker_adapters.PROFILES)
        self.assertEqual(frozen_vendorx.PROFILE_NAME, "vendorx")
        # It is nonetheless a drop-in profile: same call shape as the reviewed adapters.
        expected = inspect.signature(broker_adapters.PROFILES["alpaca"])
        self.assertEqual(inspect.signature(frozen_vendorx.vendorx), expected)


if __name__ == "__main__":
    unittest.main()
