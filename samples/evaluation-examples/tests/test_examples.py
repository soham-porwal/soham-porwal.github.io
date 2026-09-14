from __future__ import annotations

import json
import sys
import unittest
from decimal import Decimal
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "retrieval_eval"))
sys.path.insert(0, str(ROOT / "brokerage_normalization"))

import normalize  # noqa: E402
import scorer  # noqa: E402


class RetrievalScoringTests(unittest.TestCase):
    def test_historical_claims_rederive_from_anonymous_pairs(self) -> None:
        result = scorer.summarize_historical(
            ROOT / "retrieval_eval" / "historical_scores.json"
        )
        self.assertEqual(result["n_paired"], 40)
        self.assertAlmostEqual(result["baseline_mean"], 0.4763510101010101, places=15)
        self.assertAlmostEqual(result["candidate_mean"], 0.37306141774891777, places=15)
        self.assertAlmostEqual(result["delta"], -0.10328959235209237, places=15)
        self.assertAlmostEqual(result["paired_t"], -3.11001986924922, places=14)

    def test_recall_uses_full_gold_denominator_and_empty_gold_is_undefined(self) -> None:
        self.assertEqual(scorer.recall_at_k(["a", "b"], {"a", "b", "c", "d"}, 2), 0.5)
        self.assertIsNone(scorer.recall_at_k(["a"], set(), 3))


class BrokerageNormalizationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        alpha = normalize.load_payload(
            ROOT / "brokerage_normalization" / "fixtures" / "broker_alpha.json"
        )
        beta = normalize.load_payload(
            ROOT / "brokerage_normalization" / "fixtures" / "broker_beta.json"
        )
        cls.alpha_accounts, cls.alpha_positions = normalize.normalize_alpha(alpha)
        cls.beta_accounts, cls.beta_positions = normalize.normalize_beta(beta)

    def test_null_price_stays_unknown(self) -> None:
        halted = next(item for item in self.alpha_positions if item["symbol"] == "HALT")
        self.assertIsNone(halted["price"])
        self.assertIsNone(halted["market_value"])

    def test_unknown_account_value_is_distinct_from_exact_zero(self) -> None:
        unknown = next(
            item for item in self.beta_accounts if item["account_id"] == "beta-unknown"
        )
        zero = next(item for item in self.beta_accounts if item["account_id"] == "beta-zero")
        self.assertIsNone(unknown["account_value"])
        self.assertEqual(zero["account_value"], Decimal("0"))

    def test_duplicate_symbols_aggregate_exactly(self) -> None:
        aggregate = normalize.aggregate_positions(
            self.alpha_positions + self.beta_positions
        )
        acme = next(item for item in aggregate if item["symbol"] == "ACME")
        self.assertEqual(acme["quantity"], Decimal("12.5"))
        self.assertEqual(acme["known_market_value"], Decimal("320.000"))
        self.assertTrue(acme["market_value_complete"])
        self.assertEqual(acme["lot_count"], 2)


if __name__ == "__main__":
    unittest.main()
