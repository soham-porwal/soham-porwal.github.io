"""Frozen, deterministic broker profiles. Unrecognized formats never guess a mapping."""
import csv
from datetime import date, datetime
import io
import re

from broker_engine import Rejected, check, dec, object_fields, stamp, text


def row(refs, asset_type, currency, quantity, price, price_at, reported_value, cost_basis,
        units="shares", lots=None, option_terms=None):
    return dict(refs=refs, asset_type=asset_type, currency=currency, quantity=quantity,
                price=price, price_at=price_at, reported_value=reported_value,
                cost_basis=cost_basis, units=units, lots=[] if lots is None else lots,
                option_terms=option_terms)


def alpaca(payload, account, context):
    check(type(payload) is list, "UNRECOGNIZED_ALPACA_SHAPE")
    output = []
    for i, p in enumerate(payload):
        object_fields(p, ("asset_id", "symbol", "asset_class", "qty", "side", "market_value", "cost_basis", "current_price"),
            ("account_id", "exchange", "asset_marginable", "avg_entry_price", "unrealized_pl", "unrealized_plpc",
             "unrealized_intraday_pl", "unrealized_intraday_plpc", "lastday_price", "change_today", "qty_available", "usd"),
             path=f"$.payload[{i}]")
        if "account_id" in p:
            check(p["account_id"] == account, "ACCOUNT_MISMATCH")
        check(p["asset_class"] in ("us_equity", "us_option"), "UNSUPPORTED_ASSET")
        quantity = dec(p["qty"])
        check(p["side"] in ("long", "short"), "INVALID_SIDE")
        check((p["side"] == "long" and quantity >= 0) or (p["side"] == "short" and quantity <= 0), "SIDE_SIGN_MISMATCH")
        asset = text(p["asset_id"])
        if p.get("usd") is not None:
            check(context["currency"] == "USD", "DUAL_CURRENCY_UNSUPPORTED")
            usd = p["usd"]
            object_fields(usd, ("current_price", "market_value", "cost_basis"),
                ("avg_entry_price", "unrealized_pl", "unrealized_plpc", "unrealized_intraday_pl",
                 "unrealized_intraday_plpc", "lastday_price", "change_today"))
            for field in ("current_price", "market_value", "cost_basis"):
                check(dec(usd[field], nullable=True) == dec(p[field], nullable=True), "DUAL_CURRENCY_CONFLICT")
        # Quote timestamps do not exist on this positions profile. Optional independently
        # captured timestamps are explicit sidecar metadata; capture time is never price time.
        output.append(row([("native", asset), ("ticker", text(p["symbol"]))],
            "option" if p["asset_class"] == "us_option" else "equity", context["currency"],
            quantity, p["current_price"], context["price_timestamps"].get(asset), p["market_value"], p["cost_basis"],
            units="contracts" if p["asset_class"] == "us_option" else "shares"))
    return {"rows": output}


def plaid(payload, account, context):
    object_fields(payload, ("holdings", "securities", "accounts"), ("item", "request_id"))
    check(all(type(payload[k]) is list for k in ("holdings", "securities", "accounts")), "UNRECOGNIZED_PLAID_SHAPE")
    account_ids = []
    for a in payload["accounts"]:
        check(type(a) is dict, "OBJECT_REQUIRED")
        check("account_id" in a, "ACCOUNT_MISMATCH")
        account_ids.append(text(a["account_id"]))
    check(len(account_ids) == len(set(account_ids)) and account in account_ids, "ACCOUNT_MISMATCH")
    # This input contract requires capture of a single requested account. No silent dropping
    # of other accounts inside a payload; callers split full multi-account captures explicitly.
    check(account_ids == [account], "MULTI_ACCOUNT_CAPTURE")
    securities = {}
    for s in payload["securities"]:
        object_fields(s, ("security_id", "ticker_symbol", "type", "iso_currency_code"),
            ("cusip", "isin", "sedol", "institution_security_id", "institution_id", "name", "proxy_security_id",
             "is_cash_equivalent", "subtype", "close_price", "close_price_as_of", "update_datetime", "unofficial_currency_code",
             "option_contract", "fixed_income", "sector", "industry", "market_identifier_code", "cfi_code", "figi"))
        key = text(s["security_id"])
        check(key not in securities, "DUPLICATE_SECURITY")
        securities[key] = s
    output = []
    for h in payload["holdings"]:
        object_fields(h, ("account_id", "security_id", "quantity", "institution_price", "institution_value", "cost_basis", "iso_currency_code"),
            ("institution_price_as_of", "institution_price_datetime", "unofficial_currency_code", "tax_lots", "vested_quantity", "vested_value"))
        check(h["account_id"] == account, "ACCOUNT_MISMATCH")
        security_id = text(h["security_id"])
        check(security_id in securities, "MISSING_SECURITY_JOIN")
        s = securities[security_id]
        check(h.get("unofficial_currency_code") is None and s.get("unofficial_currency_code") is None,
              "UNSUPPORTED_CURRENCY")
        check(h["iso_currency_code"] == s["iso_currency_code"], "CURRENCY_IDENTITY_MISMATCH")
        refs = [("native", security_id)]
        for native_field, scheme in (("cusip", "cusip"), ("isin", "isin"), ("ticker_symbol", "ticker"),
                                    ("figi", "figi"), ("sedol", "sedol")):
            if s.get(native_field) is not None:
                refs.append((scheme, s[native_field]))
        if s.get("institution_security_id") is not None:
            refs.append(("institution:" + text(s.get("institution_id")), text(s["institution_security_id"])))
        option = s.get("option_contract")
        terms = None
        if s["type"] == "derivative":
            object_fields(option, ("contract_type", "expiration_date", "strike_price", "underlying_security_ticker"))
            asset_type, units = "option", "underlying"
            terms = {"right": option["contract_type"], "expiry": option["expiration_date"],
                     "strike": option["strike_price"], "underlying_ticker": option["underlying_security_ticker"]}
        else:
            types = {"equity": "equity", "etf": "fund", "mutual fund": "fund", "cash": "cash"}
            check(type(s["type"]) is str and s["type"] in types, "UNSUPPORTED_ASSET")
            asset_type, units = types[s["type"]], "currency" if s["type"] == "cash" else "shares"
            # A money-market fund isn't a currency balance just because it is cash-equivalent.
            check(asset_type != "cash" or s.get("subtype") in (None, "cash"), "AMBIGUOUS_CASH_CLASSIFICATION")
        lots = []
        check(type(h.get("tax_lots", [])) is list, "INVALID_LOTS")
        for lot in h.get("tax_lots", []):
            object_fields(lot, ("institution_lot_id", "quantity", "cost_basis"),
                          ("original_purchase_datetime", "purchase_price", "current_value", "position_type"))
            q = dec(lot["quantity"], nullable=True)
            side = lot.get("position_type")
            check(side in (None, "LONG", "SHORT"), "INVALID_LOT_SIDE")
            # Institutional sign conventions are not inferred; reject conflicting labels.
            check(q is None or side is None or (q >= 0 if side == "LONG" else q <= 0), "LOT_SIDE_SIGN_MISMATCH")
            lots.append(dict(id=lot["institution_lot_id"], quantity=q, cost_basis=lot["cost_basis"],
                             acquired_at=lot.get("original_purchase_datetime")))
        price_at = h.get("institution_price_datetime")
        # A date-only quote is not silently converted to midnight and called precise.
        if price_at is None:
            price_at = context["price_timestamps"].get(security_id)
        quoted_date = h.get("institution_price_as_of")
        if quoted_date is not None:
            try:
                source_date = date.fromisoformat(quoted_date)
            except (TypeError, ValueError) as exc:
                raise Rejected("INVALID_PRICE_DATE") from exc
            if price_at is not None:
                stamp(price_at)
                # Compare provider-local dates before UTC normalization.
                time_date = datetime.fromisoformat(price_at.replace("Z", "+00:00")).date()
                check(source_date == time_date, "PRICE_DATE_CONFLICT")
        output.append(row(refs, asset_type, h["iso_currency_code"], h["quantity"], h["institution_price"], price_at,
                          h["institution_value"], h["cost_basis"], units, lots, terms))
        output[-1]["price_date"] = quoted_date
    return {"rows": output}


def csv_decimal(value, nullable=False):
    if nullable and value in ("", "--", "n/a"):
        return None
    # Explicit US money dialect; validate grouping before removing delimiters.
    check(type(value) is str, "INVALID_CSV_DECIMAL")
    check(re.fullmatch(r"-?\$?(?:[0-9]+|[1-9][0-9]{0,2}(?:,[0-9]{3})+)(?:\.[0-9]+)?", value) is not None,
          "INVALID_CSV_DECIMAL")
    return dec(value.replace("$", "").replace(",", ""))


def fidelity(payload, account, context):
    check(type(payload) is str, "UNRECOGNIZED_FIDELITY_SHAPE")
    # Known external 2026 test dialect. It is not claimed to cover every Fidelity export.
    records = list(csv.reader(io.StringIO(payload.lstrip("\ufeff")), strict=True))
    header_at = next((i for i, r in enumerate(records) if r and r[0].strip().casefold() == "account number"), None)
    check(header_at is not None and header_at <= 5, "CSV_HEADER_NOT_FOUND")
    source_date = None
    for preamble in records[:header_at]:
        if not preamble or not any(preamble):
            continue
        check(len(preamble) == 1 and preamble[0].startswith("Positions for account(s) as of "), "CSV_UNKNOWN_PREAMBLE")
        try:
            source_date = datetime.strptime(preamble[0].split(" as of ", 1)[1], "%b-%d-%Y").date()
        except ValueError as exc:
            raise Rejected("INVALID_SOURCE_DATE") from exc
    header = [s.strip().casefold() for s in records[header_at]]
    required = {"account number", "account name", "symbol", "description", "quantity", "last price", "current value", "cost basis total", "type"}
    check(len(header) == len(set(header)) and set(header) == required, "CSV_SCHEMA_DRIFT")
    output = []
    for i, values in enumerate(records[header_at + 1:]):
        if not values or all(not v.strip() for v in values):
            continue
        # Only the exact known disclaimer prefix ends records. Unknown lines quarantine.
        if len(values) == 1 and values[0].startswith("The data and information in this spreadsheet"):
            check(all(not any(v.strip() for v in r) for r in records[header_at + 2 + i:]), "CSV_ROWS_AFTER_FOOTER")
            break
        check(len(values) == len(header), "CSV_ROW_WIDTH")
        p = dict(zip(header, values))
        check(p["account number"] == account, "MULTI_ACCOUNT_CAPTURE")
        # Pending activity is not a holding. Rejecting prevents a partial account rewrite;
        # a reviewed activity adapter can handle it separately in a future version.
        check("pending" not in p["description"].casefold() and "pending" not in p["symbol"].casefold(),
              "PENDING_ACTIVITY_NOT_POSITION")
        symbol = text(p["symbol"])
        # CSV Type=Cash describes account settlement type, not asset class. Registry decides.
        output.append(row([("ticker", symbol)], None,
                          context["currency"], csv_decimal(p["quantity"]), csv_decimal(p["last price"], True),
                          context["price_timestamps"].get(symbol), csv_decimal(p["current value"], True),
                          csv_decimal(p["cost basis total"], True)))
    return {"rows": output, "source_date": None if source_date is None else source_date.isoformat()}


PROFILES = {"alpaca": alpaca, "plaid": plaid, "fidelity_csv": fidelity}


def adapt(broker, account, payloads, context):
    check(broker in PROFILES, "UNRECOGNIZED_BROKER_REVIEW_REQUIRED")
    # These holdings endpoints and the pinned CSV dialect are complete single responses.
    # Capture-chain validation runs first so a mid-fetch error can never look like empty data.
    check(len(payloads) == 1, "UNEXPECTED_PAGINATION")
    try:
        return PROFILES[broker](payloads[0], account, context)
    except csv.Error as exc:
        raise Rejected("INVALID_CSV") from exc
