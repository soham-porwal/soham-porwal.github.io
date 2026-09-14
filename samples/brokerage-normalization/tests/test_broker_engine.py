"""Behavioral invariants over deliberately synthetic 2026 captures."""
import json
from pathlib import Path
import tempfile
import unittest

from broker_engine import Engine, Rejected, dec, read_json
from durable_store import Store

ARTIFACT = Path(__file__).resolve().parent.parent


NOW = "2026-09-10T18:00:00Z"


def reference():
    instruments = [
        dict(id="eq", type="equity", currency="USD", multiplier="1", status="active"),
        dict(id="other", type="equity", currency="USD", multiplier="1", status="active"),
        dict(id="usd", type="cash", currency="USD", multiplier="1", status="active"),
        dict(id="opt", type="option", currency="USD", multiplier="100", status="active",
             underlying_id="eq", expiry="2026-12-18", right="call", strike="100", deliverable="standard"),
    ]
    aliases = []
    for iid, ticker, native, cusip, isin in [
        ("eq", "SYN", "n-eq", "SYN000001", "US-SYN-001"),
        ("other", "ALT", "n-other", "SYN000002", "US-SYN-002"),
        ("usd", "USD", "n-usd", None, None),
        ("opt", "SYN261218C00100000", "n-opt", None, None),
    ]:
        for scheme, value in [("ticker", ticker), ("native", native), ("cusip", cusip), ("isin", isin)]:
            if value:
                aliases.append(dict(broker="*", scheme=scheme, value=value, instrument_id=iid,
                                    **{"from": "2026-01-01T00:00:00Z", "until": "2027-01-01T00:00:00Z"}))
    return dict(version="synthetic-2026-v1", instruments=instruments, aliases=aliases)


def alpaca():
    return [dict(asset_id="n-eq", symbol="SYN", asset_class="us_equity", qty="2", side="long",
                 current_price="10", market_value="20", cost_basis="16")]


def plaid(option=False, cash=False):
    native, ticker, kind = ("n-opt", "SYN261218C00100000", "derivative") if option else (
        ("n-usd", "USD", "cash") if cash else ("n-eq", "SYN", "equity"))
    security = dict(security_id=native, ticker_symbol=ticker, type=kind, iso_currency_code="USD")
    if option:
        security["option_contract"] = dict(contract_type="call", expiration_date="2026-12-18",
                                           strike_price="100", underlying_security_ticker="SYN")
    holding = dict(account_id="test-account", security_id=native, quantity="200" if option else "2",
                   institution_price="1" if cash else "10", institution_value="2000" if option else ("2" if cash else "20"),
                   cost_basis="1600" if option else "16", iso_currency_code="USD", institution_price_datetime=NOW)
    return dict(accounts=[dict(account_id="test-account")], holdings=[holding], securities=[security])


def capture(broker="alpaca", payload=None):
    return dict(schema="capture/v1", broker=broker, account="test-account", snapshot_id="synthetic-snapshot",
                as_of=NOW, pages=[dict(cursor=None, next_cursor=None, status=200,
                                      payload=alpaca() if payload is None else payload)],
                context=dict(currency="USD", price_timestamps={"n-eq": NOW, "n-opt": NOW},
                             cash_balances=[], reported_total=None, total_scope="holdings",
                             expected_rows=1, settlement_basis="broker_reported_positions"))


def run(c, r=None):
    return Engine(reference() if r is None else r)(json.dumps(c, allow_nan=False).encode())


def committed_engine():
    """Engine over the committed reviewed reference, not the synthetic inline one."""
    return Engine(read_json((ARTIFACT / "reference.json").read_bytes()))


def committed_capture(name):
    return (ARTIFACT / "captures" / name).read_bytes()


class BrokerEngineInvariants(unittest.TestCase):
    def rejected(self, c, code, r=None):
        with self.assertRaises(Rejected) as caught:
            run(c, r)
        self.assertEqual(caught.exception.code, code)

    def test_01_null_stale_halted_delisted_prices_never_become_zero(self):
        for case, reason in [("null", "MISSING_PRICE"), ("stale", "STALE_PRICE"),
                             ("halted", "INACTIVE_INSTRUMENT"), ("delisted", "INACTIVE_INSTRUMENT")]:
            with self.subTest(case=case):
                c, r = capture(), reference()
                if case == "null":
                    c["pages"][0]["payload"][0]["current_price"] = None
                elif case == "stale":
                    c["context"]["price_timestamps"]["n-eq"] = "2026-09-08T18:00:00Z"
                else:
                    r["instruments"][0]["status"] = case
                result = run(c, r)
                self.assertIsNone(result["positions"][0]["value"])
                self.assertEqual(result["positions"][0]["value_status"], reason)
                self.assertFalse(result["valuation_complete"])

    def test_02_missing_basis_stays_missing_and_complete_lots_can_supply_basis(self):
        p = plaid()
        p["holdings"][0]["cost_basis"] = None
        c = capture("plaid", p)
        position = run(c)["positions"][0]
        self.assertIsNone(position["cost_basis"])
        self.assertEqual(position["basis_source"], "missing")
        p["holdings"][0]["tax_lots"] = [dict(institution_lot_id="lot", quantity="2", cost_basis="16")]
        position = run(c)["positions"][0]
        self.assertEqual((position["cost_basis"], position["basis_source"]), ("16", "lots"))

    def test_03_lot_quantity_and_basis_mismatch_reject(self):
        for qty, basis, code in [("1", "16", "LOT_QUANTITY_MISMATCH"), ("2", "99", "LOT_BASIS_MISMATCH")]:
            p = plaid()
            p["holdings"][0]["tax_lots"] = [dict(institution_lot_id="lot", quantity=qty, cost_basis=basis)]
            self.rejected(capture("plaid", p), code)

    def test_04_short_is_negative_exposure_but_conflicting_side_rejects(self):
        c = capture()
        p = c["pages"][0]["payload"][0]
        p.update(qty="-2", side="short", market_value="-20", cost_basis="-16")
        self.assertEqual(run(c)["positions"][0]["value"], "-20")
        p["side"] = "long"
        self.rejected(c, "SIDE_SIGN_MISMATCH")

    def test_05_reported_total_scope_and_mismatch(self):
        c = capture()
        c["context"].update(cash_balances=[dict(currency="USD", amount="5")], reported_total="25",
                              total_scope="holdings_plus_cash")
        self.assertEqual(run(c)["reconciliation"], "MATCHED")
        c["context"]["reported_total"] = "26"
        self.rejected(c, "REPORTED_TOTAL_MISMATCH")

    def test_06_cash_position_and_balance_cannot_double_count(self):
        c = capture("plaid", plaid(cash=True))
        self.assertEqual(run(c)["cash_value"], "2")
        c["context"]["cash_balances"] = [dict(currency="USD", amount="2")]
        self.rejected(c, "CASH_DOUBLE_COUNT")

    def test_07_every_native_ticker_cusip_isin_must_agree(self):
        for key, value in [("security_id", "n-other"), ("ticker_symbol", "ALT"),
                           ("cusip", "SYN000002"), ("isin", "US-SYN-002")]:
            with self.subTest(key=key):
                p = plaid()
                p["securities"][0][key] = value
                if key == "security_id":
                    p["holdings"][0][key] = value
                self.rejected(capture("plaid", p), "CONFLICTING_IDENTIFIERS")
        p = plaid()
        p["securities"][0]["isin"] = "UNKNOWN"
        self.rejected(capture("plaid", p), "UNRESOLVED_IDENTITY")

    def test_08_dated_symbol_change_preserves_identity_and_split_snapshot_is_absolute(self):
        r = reference()
        next(a for a in r["aliases"] if a["value"] == "SYN")["until"] = "2026-09-01T00:00:00Z"
        r["aliases"].append(dict(broker="*", scheme="ticker", value="NEW", instrument_id="eq",
                                  **{"from": "2026-09-01T00:00:00Z", "until": "2027-01-01T00:00:00Z"}))
        old = capture()
        old["as_of"] = "2026-08-31T18:00:00Z"
        old["context"]["price_timestamps"]["n-eq"] = old["as_of"]
        new = capture()
        new["pages"][0]["payload"][0].update(symbol="NEW", qty="4", current_price="5")
        self.assertEqual(run(old, r)["positions"][0]["instrument_id"], run(new, r)["positions"][0]["instrument_id"])
        self.assertEqual(run(new, r)["positions"][0]["quantity"], "4")
        self.rejected(capture(), "UNRESOLVED_IDENTITY", r)

    def test_09_timezone_equivalent_snapshot_quote_and_lot_instants(self):
        p = plaid()
        p["holdings"][0]["tax_lots"] = [dict(institution_lot_id="lot", quantity="2", cost_basis="16",
                                               original_purchase_datetime="2026-09-09T11:00:00-07:00")]
        c = capture("plaid", p)
        c["as_of"] = "2026-09-10T11:00:00-07:00"
        result = run(c)
        self.assertEqual(result["as_of"], NOW)
        self.assertEqual(result["positions"][0]["lots"][0]["acquired_at"], "2026-09-09T18:00:00Z")

    def test_10_partial_or_unterminated_capture_never_returns_partial_positions(self):
        c = capture()
        c["pages"][0]["next_cursor"] = "p2"
        self.rejected(c, "INCOMPLETE_PAGINATION")
        c["pages"].append(dict(cursor="p2", next_cursor=None, status=503, payload=[]))
        self.rejected(c, "PARTIAL_FETCH_FAILURE")
        c["pages"][1]["status"] = 200
        self.rejected(c, "UNEXPECTED_PAGINATION")

    def test_11_options_contracts_and_underlying_units_produce_same_exposure(self):
        a = capture()
        a["pages"][0]["payload"][0].update(asset_id="n-opt", symbol="SYN261218C00100000",
                                             asset_class="us_option", qty="2", market_value="2000", cost_basis="1600")
        pa, pp = run(a)["positions"][0], run(capture("plaid", plaid(option=True)))["positions"][0]
        for key in ("instrument_id", "quantity", "quantity_unit", "multiplier", "value", "cost_basis", "option"):
            self.assertEqual(pa[key], pp[key], key)

    def test_12_option_terms_fractional_contracts_and_invalid_deliverables(self):
        p = plaid(option=True)
        p["securities"][0]["option_contract"]["strike_price"] = "101"
        self.rejected(capture("plaid", p), "OPTION_TERMS_CONFLICT")
        p = plaid(option=True)
        p["holdings"][0]["quantity"] = "201"
        self.rejected(capture("plaid", p), "FRACTIONAL_OPTION_CONTRACT")
        for field, value, code in [("multiplier", "0", "INVALID_MULTIPLIER"),
                                   ("multiplier", "0.5", "INVALID_MULTIPLIER"),
                                   ("deliverable", "100 shares plus cash", "UNSUPPORTED_OPTION_DELIVERABLE")]:
            r = reference()
            r["instruments"][3][field] = value
            self.rejected(capture(), code, r)

    def test_13_option_lots_share_the_canonical_parent_contract_unit(self):
        p = plaid(option=True)
        p["holdings"][0]["tax_lots"] = [dict(institution_lot_id="lot", quantity="200", cost_basis="1600")]
        position = run(capture("plaid", p))["positions"][0]
        self.assertEqual(position["quantity_unit"], "contracts")
        self.assertEqual(position["lots"][0]["quantity"], position["quantity"],
                         "Canonical option lots must not silently retain underlying-share units")

    def test_14_unknown_shape_and_model_proposed_fields_cannot_enter_canonical(self):
        c = capture()
        c["pages"][0]["payload"][0]["model_instrument_id"] = "other"
        self.rejected(c, "SCHEMA_DRIFT")
        c = capture(payload={"positions": alpaca()})
        self.rejected(c, "UNRECOGNIZED_ALPACA_SHAPE")
        c = capture()
        c["model_approved"] = True
        self.rejected(c, "SCHEMA_DRIFT")

    def test_15_decimal_rejects_malformed_nonfinite_boolean_and_binary_float(self):
        for value in [True, "NaN", "Infinity", "1,000", "1e3", " 2", "02", "2.000000001"]:
            with self.subTest(value=value):
                c = capture()
                c["pages"][0]["payload"][0]["qty"] = value
                self.rejected(c, "INVALID_DECIMAL")
        with self.assertRaises(Rejected):
            dec(0.1)
        for token in (b"NaN", b"Infinity", b"-Infinity"):
            raw = json.dumps(capture()).encode().replace(b'"qty": "2"', b'"qty": '+token)
            with self.assertRaises(Rejected) as caught:
                Engine(reference())(raw)
            self.assertEqual(caught.exception.code, "NONFINITE_JSON")

    def test_16_numeric_json_decimal_arithmetic_is_exact(self):
        c = capture()
        c["pages"][0]["payload"][0].update(qty="0.1", current_price="0.2", market_value="0.02", cost_basis=None)
        raw = json.dumps(c).encode().replace(b'"qty": "0.1"', b'"qty": 0.1').replace(
            b'"current_price": "0.2"', b'"current_price": 0.2')
        result = Engine(reference())(raw)
        self.assertEqual(result["positions"][0]["value"], "0.02")

    def test_17_pending_unsettled_and_settled_activity_never_reapply_to_snapshot(self):
        for status in ("pending", "unsettled", "settled"):
            with self.subTest(status=status):
                c = capture()
                c["context"]["activities"] = [dict(id="trade", status=status,
                    trade_at="2026-09-09T23:30:00-07:00", settlement_date="2026-09-10", quantity="100")]
                result = run(c)
                self.assertEqual(result["positions"][0]["quantity"], "2")
                self.assertEqual(result["known_holdings_value"], "20")
                self.assertEqual(result["activities"][0]["trade_at"], "2026-09-10T06:30:00Z")
                self.assertEqual(result["activities"][0]["treatment"], "excluded_from_holdings_snapshot")

    def test_18_settlement_uses_provider_local_trade_day_not_utc_day(self):
        c = capture()
        c["context"]["activities"] = [dict(id="trade", status="settled",
            trade_at="2026-09-09T23:30:00-07:00", settlement_date="2026-09-09", quantity="2")]
        self.assertEqual(run(c)["activities"][0]["settlement_date"], "2026-09-09")
        c["context"]["activities"][0]["settlement_date"] = "2026-09-08"
        self.rejected(c, "SETTLEMENT_BEFORE_TRADE")

    def test_19_old_fidelity_source_date_cannot_be_relabelled_as_new_snapshot(self):
        header = "Account Number,Account Name,Symbol,Description,Quantity,Last Price,Current Value,Cost Basis Total,Type"
        c = capture("fidelity_csv", "Positions for account(s) as of Jan-01-2026\n" + header +
                    "\ntest-account,Example,SYN,Synthetic,2,10,20,16,Cash")
        c["context"]["price_timestamps"]["SYN"] = NOW
        self.rejected(c, "SOURCE_DATE_CONFLICT")
        c["pages"][0]["payload"] = c["pages"][0]["payload"].replace("Jan-01-2026", "Sep-10-2026")
        self.assertEqual(run(c)["positions"][0]["quantity"], "2")

    def test_20_malformed_plaid_collections_raise_domain_rejection(self):
        for kind in ("account_element", "account_id", "security_type"):
            with self.subTest(kind=kind):
                p = plaid()
                if kind == "account_element":
                    p["accounts"].append(None)
                elif kind == "account_id":
                    p["accounts"][0]["account_id"] = []
                else:
                    p["securities"][0]["type"] = {}
                with self.assertRaises(Rejected):
                    run(capture("plaid", p))

    def test_21_supplied_figi_and_sedol_cannot_be_silently_discarded(self):
        for field in ("figi", "sedol"):
            with self.subTest(field=field):
                p = plaid()
                p["securities"][0][field] = "UNKNOWN-IDENTIFIER"
                self.rejected(capture("plaid", p), "UNRESOLVED_IDENTITY")

    def test_22_expired_options_keep_quantity_but_have_no_complete_value(self):
        p, r = plaid(option=True), reference()
        p["securities"][0]["option_contract"]["expiration_date"] = "2026-08-01"
        r["instruments"][3]["expiry"] = "2026-08-01"
        result = run(capture("plaid", p), r)
        position = result["positions"][0]
        self.assertEqual(position["quantity"], "2")
        self.assertIsNone(position["value"])
        self.assertEqual(position["value_status"], "EXPIRED_OPTION")
        self.assertFalse(result["valuation_complete"])

    def test_23_nonstandard_multiplier_cannot_claim_standard_deliverable(self):
        r = reference()
        r["instruments"][3]["multiplier"] = "50"
        self.rejected(capture(), "UNSUPPORTED_OPTION_MULTIPLIER", r)

    def test_24_quote_date_conflicts_with_datetime_or_sidecar_reject(self):
        for source in ("native", "sidecar"):
            with self.subTest(source=source):
                p = plaid()
                p["holdings"][0]["institution_price_as_of"] = "2026-01-01"
                if source == "sidecar":
                    del p["holdings"][0]["institution_price_datetime"]
                self.rejected(capture("plaid", p), "PRICE_DATE_CONFLICT")

    def test_25_quote_date_matches_original_offset_calendar_day(self):
        p = plaid()
        p["holdings"][0]["institution_price_datetime"] = "2026-09-09T23:30:00-07:00"
        p["holdings"][0]["institution_price_as_of"] = "2026-09-09"
        position = run(capture("plaid", p))["positions"][0]
        self.assertEqual(position["price_at"], "2026-09-10T06:30:00Z")
        self.assertEqual(position["value_status"], "COMPLETE")


    def test_26_plaid_resolved_snapshot_accepts_with_underlying_unit_quantity(self):
        # Capture 02 is the published-docs plaid sample and must keep failing closed. Capture
        # 07 is its authored resolvable sibling: without it the plaid adapter has no accepted
        # output anywhere in the artifact and a reader cannot tell working code from dead code.
        engine = committed_engine()
        raw = committed_capture("07-plaid-resolved.json")
        holdings = read_json(raw)["pages"][0]["payload"]["holdings"]
        stated = next(h for h in holdings if h["security_id"] == "synthetic-plaid-aapl-call")
        self.assertEqual(str(stated["quantity"]), "-200",
                         "capture must state the option in the adapter's underlying-unit convention")
        snapshot = engine(raw)
        self.assertEqual((snapshot["broker"], snapshot["status"]), ("plaid", "OK"))
        self.assertEqual([p["instrument_id"] for p in snapshot["positions"]],
                         ["sample:AAPL", "synthetic:AAPL-call"])
        equity, option = snapshot["positions"]
        self.assertEqual((equity["quantity"], equity["quantity_unit"], equity["value"]),
                         ("5", "shares", "600"))
        # -200 underlying units over the reviewed multiplier of 100 is -2 contracts, and the
        # tax lot is converted with its parent rather than left in share units.
        self.assertEqual((option["quantity"], option["quantity_unit"], option["multiplier"]),
                         ("-2", "contracts", "100"))
        self.assertEqual(option["lots"][0]["quantity"], "-2")
        self.assertEqual(option["value"], "-500")
        # Currency subtotals: a cash-typed position becomes a currency subtotal, not a holding.
        self.assertEqual(snapshot["cash"], [{"currency": "USD", "amount": "250",
                                             "source": "position",
                                             "instrument_id": "synthetic:USD-cash"}])
        self.assertEqual((snapshot["currency"], snapshot["known_holdings_value"],
                          snapshot["cash_value"], snapshot["reported_total"]),
                         ("USD", "100", "250", "350"))
        # Completeness flags.
        self.assertTrue(snapshot["valuation_complete"])
        self.assertEqual(snapshot["issues"], [])
        self.assertEqual([p["value_status"] for p in snapshot["positions"]],
                         ["COMPLETE", "COMPLETE"])
        self.assertEqual((snapshot["reconciliation"], snapshot["reconciliation_scope"]),
                         ("MATCHED", "holdings_plus_cash"))
        # The identical short exposure stated in alpaca contract units (capture 05, qty "-2")
        # must land on a byte-identical canonical row: units differ upstream, exposure does not.
        alpaca_raw = committed_capture("05-synthetic-short-option.json")
        self.assertEqual(read_json(alpaca_raw)["pages"][0]["payload"][0]["qty"], "-2")
        alpaca_option = engine(alpaca_raw)["positions"][0]
        for key in ("instrument_id", "quantity", "quantity_unit", "multiplier",
                    "observed_price", "price_at", "value", "cost_basis", "option"):
            self.assertEqual(option[key], alpaca_option[key], key)
        # Adding capture 07 must not have unblocked the unresolvable docs sample beside it.
        with self.assertRaises(Rejected) as caught:
            engine(committed_capture("02-plaid-upstream.json"))
        self.assertEqual(caught.exception.code, "UNRESOLVED_IDENTITY")


class PaginationFailureDurability(unittest.TestCase):
    """A broken page chain must refuse the whole capture and leave accepted state untouched.

    The capture envelope models a multi-response fetch, so the failures that matter are the
    ones where part of the chain is wrong: a page that errored, a row served on both sides of
    a boundary, and a cursor that cycles. None may produce a partial snapshot, and none may
    disturb a snapshot already accepted for the same account.
    """

    def setUp(self):
        # The database is scratch state, not part of the sample; keep it out of the artifact.
        self.temp = tempfile.TemporaryDirectory(prefix="broker-pagination-")
        self.addCleanup(self.temp.cleanup)
        self.store = Store(Path(self.temp.name) / "state.sqlite", Engine(reference()),
                           "pagination-test-policy")
        self.addCleanup(self.store.close)
        accepted = self.store.ingest(json.dumps(capture(), allow_nan=False).encode(), "good.json")
        self.assertEqual(accepted["status"], "ACCEPTED")
        self.baseline = accepted["fingerprint"]

    def persisted(self):
        """Accepted and current rows as stored, so comparison is byte-level, not semantic."""
        return [tuple(r) for r in self.store.connection.execute(
            "SELECT broker,account,snapshot_id,as_of,fingerprint,snapshot_json FROM accepted"
            " UNION ALL"
            " SELECT broker,account,snapshot_id,as_of,fingerprint,snapshot_json FROM current")]

    def rejects(self, c, code, path):
        with self.assertRaises(Rejected) as caught:
            run(c)
        self.assertEqual((caught.exception.code, caught.exception.path), (code, path))

    def quarantines(self, broken, code, source):
        """Ingest a broken capture and prove total rejection over an undisturbed snapshot."""
        before = self.persisted()
        event = self.store.ingest(json.dumps(broken, allow_nan=False).encode(), source)
        self.assertEqual((event["status"], event["code"], event["path"]),
                         ("QUARANTINED", code, "$"))
        # Nothing partial was written: a quarantined attempt carries no snapshot identity or
        # fingerprint at all, and the durable queue records the specific code for review.
        self.assertNotIn("fingerprint", event)
        report = self.store.report()
        self.assertEqual(report["counts"]["accepted_snapshots"], 1)
        self.assertEqual(report["counts"]["current_accounts"], 1)
        self.assertEqual(report["quarantine_queue"][-1]["code"], code)
        # The previously accepted snapshot is unchanged, fingerprint asserted explicitly
        # rather than inferred from the absence of an exception.
        self.assertEqual(self.persisted(), before)
        self.assertEqual({row[4] for row in self.persisted()}, {self.baseline})
        self.assertEqual(report["current"][0]["positions"][0]["quantity"], "2")
        self.assertTrue(self.store.verify()["ok"])
        return event

    def test_27_page_failure_mid_pagination_quarantines_and_never_reads_as_empty_data(self):
        broken = capture()
        broken["pages"][0]["next_cursor"] = "cursor-2"
        broken["pages"].append(dict(cursor="cursor-2", next_cursor=None, status=500, payload=[]))
        self.quarantines(broken, "PARTIAL_FETCH_FAILURE", "page-2-http-500.json")
        # The page chain is checked before any adapter runs, so the HTTP failure is reported
        # as one instead of being masked by the adapter's single-response rejection.
        self.rejects(broken, "PARTIAL_FETCH_FAILURE", "$")
        # A fetch that failed while returning no rows must stay distinguishable from an
        # account that genuinely holds nothing. The failure quarantines; the empty account is
        # accepted as SUSPECT. Neither path can report a clean OK over zero rows.
        failed = capture(payload=[])
        failed["pages"][0]["status"] = 500
        failed["context"]["expected_rows"] = 0
        self.quarantines(failed, "PARTIAL_FETCH_FAILURE", "only-page-http-500.json")
        empty = capture(payload=[])
        empty["context"]["expected_rows"] = 0
        self.assertEqual(run(empty)["status"], "SUSPECT")

    def test_28_position_repeated_across_a_page_boundary_is_never_merged_or_double_counted(self):
        broken = capture()
        broken["pages"][0]["next_cursor"] = "cursor-2"
        broken["pages"].append(dict(cursor="cursor-2", next_cursor=None, status=200,
                                    payload=alpaca()))
        broken["context"]["expected_rows"] = 2
        # What fires today: no reviewed profile consumes a multi-response capture, so the
        # overlap is refused at the adapter boundary before a merge is ever attempted.
        self.quarantines(broken, "UNEXPECTED_PAGINATION", "row-on-both-pages.json")
        # Second line of defence, proven independently of that restriction: hand the engine
        # the merged rows a paginating profile would produce and the repeat is still caught
        # per row, so deduplication does not rest on the single-response rule alone.
        merged = capture(payload=alpaca() + alpaca())
        merged["context"]["expected_rows"] = 2
        self.rejects(merged, "DUPLICATE_POSITION", "$.rows[1]")
        # Against the provider's own honest row count the overlap trips accounting first.
        merged["context"]["expected_rows"] = 1
        self.rejects(merged, "ROW_COVERAGE_MISMATCH", "$")

    def test_29_repeated_cursor_is_a_loop_and_never_replays_pages_into_the_snapshot(self):
        # A cursor already followed in this capture means the provider is cycling. Following
        # it would re-append the same rows indefinitely, so the whole capture is refused.
        immediate = capture()
        immediate["pages"][0]["next_cursor"] = "cursor-2"
        immediate["pages"].append(dict(cursor="cursor-2", next_cursor="cursor-2", status=200,
                                       payload=[]))
        self.quarantines(immediate, "PAGINATION_LOOP", "cursor-points-at-itself.json")
        # The loop is caught just as well when it closes over an earlier cursor than its own.
        indirect = capture()
        indirect["pages"][0]["next_cursor"] = "cursor-2"
        indirect["pages"].append(dict(cursor="cursor-2", next_cursor="cursor-3", status=200,
                                      payload=[]))
        indirect["pages"].append(dict(cursor="cursor-3", next_cursor="cursor-2", status=200,
                                      payload=[]))
        self.quarantines(indirect, "PAGINATION_LOOP", "cursor-returns-to-page-2.json")


if __name__ == "__main__":
    unittest.main()
