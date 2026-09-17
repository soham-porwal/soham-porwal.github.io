"""Deterministic capture validation; no model, network, or persistence imports."""
from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
from decimal import Context, Decimal, localcontext
import hashlib
import json
import re

ARITHMETIC = Context(prec=60)
MAX_BYTES = 2_000_000
MAX_ROWS = 5000


class Rejected(ValueError):
    def __init__(self, code, path="$"):
        self.code, self.path = code, path
        super().__init__(f"{code} at {path}")


def check(ok, code, path="$"):
    if not ok:
        raise Rejected(code, path)


def object_fields(value, required, optional=(), path="$"):
    check(type(value) is dict, "OBJECT_REQUIRED", path)
    check(set(required) <= value.keys() and value.keys() <= set(required) | set(optional),
          "SCHEMA_DRIFT", path)


def text(value, path="$"):
    check(type(value) is str and 0 < len(value) <= 256 and not any(ord(c) < 32 for c in value),
          "INVALID_TEXT", path)
    return value


def dec(value, nullable=False, signed=True, path="$"):
    if value is None and nullable:
        return None
    check(type(value) in (str, int, Decimal), "INVALID_DECIMAL", path)
    s = str(value)
    check(re.fullmatch(r"-?(?:0|[1-9][0-9]{0,14})(?:\.[0-9]{1,8})?", s) is not None,
          "INVALID_DECIMAL", path)
    result = Decimal(s)
    check(signed or result >= 0, "NEGATIVE_VALUE", path)
    return result


def stamp(value, path="$"):
    check(type(value) is str and re.fullmatch(
        r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:Z|[+-][0-9]{2}:[0-9]{2})", value),
        "TIMEZONE_REQUIRED", path)
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError as exc:
        raise Rejected("INVALID_TIMESTAMP", path) from exc


def iso(value):
    return value.isoformat(timespec="seconds").replace("+00:00", "Z")


def money_text(value):
    if value is None:
        return None
    if value == 0:
        return "0"
    s = format(value, "f")
    return s.rstrip("0").rstrip(".") if "." in s else s


def json_bytes(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def read_json(raw):
    check(type(raw) is bytes and len(raw) <= MAX_BYTES, "INPUT_TOO_LARGE")
    def pairs(items):
        result = {}
        for k, v in items:
            check(k not in result, "DUPLICATE_JSON_KEY")
            result[k] = v
        return result
    def bad_constant(_):
        raise Rejected("NONFINITE_JSON")
    try:
        # A JSON number never passes through a binary float, including Plaid numbers.
        return json.loads(raw.decode("utf-8-sig"), parse_float=Decimal,
                          parse_constant=bad_constant, object_pairs_hook=pairs)
    except Rejected:
        raise
    except (ValueError, UnicodeDecodeError, RecursionError) as exc:
        raise Rejected("INVALID_JSON") from exc


class Reference:
    """Versioned, human-reviewed effective-dated alias registry; ticker never guessed."""
    def __init__(self, document):
        self.document = deepcopy(document)
        object_fields(document, ("version", "instruments", "aliases"))
        text(document["version"])
        check(type(document["instruments"]) is list and type(document["aliases"]) is list,
              "INVALID_REFERENCE")
        self.instruments = {}
        for item in document["instruments"]:
            object_fields(item, ("id", "type", "currency", "multiplier", "status"),
                          ("underlying_id", "expiry", "right", "strike", "deliverable"))
            key = text(item["id"])
            check(key not in self.instruments, "DUPLICATE_REFERENCE_ID")
            check(item["type"] in ("equity", "fund", "option", "cash"), "UNSUPPORTED_ASSET")
            check(item["currency"] in ("USD", "CAD"), "UNSUPPORTED_CURRENCY")
            check(item["status"] in ("active", "halted", "delisted"), "INVALID_REFERENCE_STATUS")
            multiplier = dec(item["multiplier"], signed=False)
            check(multiplier > 0, "INVALID_MULTIPLIER")
            if item["type"] != "option":
                check(multiplier == 1, "INVALID_MULTIPLIER")
            else:
                check(set(("underlying_id", "expiry", "right", "strike", "deliverable")) <= item.keys(),
                      "OPTION_TERMS_MISSING")
                check(item["right"] in ("put", "call") and item["deliverable"] == "standard",
                      "UNSUPPORTED_OPTION_DELIVERABLE")
                try:
                    date.fromisoformat(item["expiry"])
                except (TypeError, ValueError) as exc:
                    raise Rejected("INVALID_EXPIRY") from exc
                dec(item["strike"], signed=False)
                check(multiplier == multiplier.to_integral_value(), "INVALID_MULTIPLIER")
                check(multiplier == 100, "UNSUPPORTED_OPTION_MULTIPLIER")
            self.instruments[key] = item
        for item in self.instruments.values():
            if item["type"] == "option":
                check(item["underlying_id"] in self.instruments and
                      self.instruments[item["underlying_id"]]["type"] in ("equity", "fund"),
                      "UNKNOWN_UNDERLYING")
        for alias in document["aliases"]:
            object_fields(alias, ("broker", "scheme", "value", "instrument_id", "from", "until"))
            for field in ("broker", "scheme", "value"):
                text(alias[field])
            check(alias["instrument_id"] in self.instruments, "ORPHAN_ALIAS")
            start, end = stamp(alias["from"]), stamp(alias["until"])
            check(start < end, "INVALID_ALIAS_INTERVAL")
        self.hash = hashlib.sha256(json_bytes(document)).hexdigest()

    def resolve(self, broker, refs, currency, as_of):
        check(type(refs) is list and refs, "IDENTITY_MISSING")
        matched = set()
        evidence = []
        for scheme, value in refs:
            if value is None:
                continue
            text(value)
            candidates = {a["instrument_id"] for a in self.document["aliases"]
                          if a["broker"] in (broker, "*") and a["scheme"] == scheme and a["value"] == value
                          and stamp(a["from"]) <= as_of < stamp(a["until"])}
            check(len(candidates) <= 1, "AMBIGUOUS_IDENTITY")
            # Every supplied strong identifier must resolve. A known ticker cannot mask
            # an unknown native ID/CUSIP/ISIN, nor can disagreement be silently ranked.
            check(candidates, "UNRESOLVED_IDENTITY")
            matched |= candidates
            evidence.append({"scheme": scheme, "value": value})
        check(len(matched) == 1, "CONFLICTING_IDENTIFIERS")
        item = self.instruments[next(iter(matched))]
        check(item["currency"] == currency, "CURRENCY_IDENTITY_MISMATCH")
        return item, sorted(evidence, key=lambda e: (e["scheme"], e["value"]))


def pages_complete(pages):
    check(type(pages) is list and 0 < len(pages) <= 100, "INVALID_PAGES")
    cursor, seen = None, set()
    for i, page in enumerate(pages):
        object_fields(page, ("cursor", "next_cursor", "status", "payload"), path=f"$.pages[{i}]")
        check(type(page["status"]) is int and page["status"] == 200, "PARTIAL_FETCH_FAILURE")
        check(page["cursor"] == cursor, "PAGINATION_GAP")
        nxt = page["next_cursor"]
        check(nxt is None or type(nxt) is str, "INVALID_CURSOR")
        check(nxt is None or nxt not in seen, "PAGINATION_LOOP")
        if nxt is not None:
            seen.add(nxt)
        check(nxt is not None or i == len(pages) - 1, "UNEXPECTED_PAGE")
        cursor = nxt
    check(cursor is None, "INCOMPLETE_PAGINATION")
    return [p["payload"] for p in pages]


class Engine:
    def __init__(self, reference, max_price_age_seconds=86400):
        self.reference = Reference(reference)
        check(type(max_price_age_seconds) is int and 0 <= max_price_age_seconds <= 604800,
              "INVALID_FRESHNESS_POLICY")
        self.max_age = max_price_age_seconds

    def __call__(self, raw):
        # Local import avoids circular dependency; adapters cannot write persistence.
        from broker_adapters import adapt
        capture = read_json(raw)
        object_fields(capture, ("schema", "broker", "account", "snapshot_id", "as_of", "pages", "context"))
        check(capture["schema"] == "capture/v1", "UNSUPPORTED_CAPTURE_VERSION")
        for field in ("broker", "account", "snapshot_id"):
            text(capture[field], "$." + field)
        observed = stamp(capture["as_of"], "$.as_of")
        payloads = pages_complete(capture["pages"])
        context = capture["context"]
        object_fields(context, ("currency", "price_timestamps", "cash_balances", "reported_total",
                                "total_scope", "expected_rows", "settlement_basis"), ("activities",))
        check(context["currency"] in ("USD", "CAD"), "UNSUPPORTED_CURRENCY")
        check(context["settlement_basis"] == "broker_reported_positions", "UNSUPPORTED_SETTLEMENT_BASIS")
        check(type(context["price_timestamps"]) is dict, "INVALID_PRICE_TIMESTAMPS")
        activities, activity_ids = [], set()
        check(type(context.get("activities", [])) is list, "INVALID_ACTIVITIES")
        for activity in context.get("activities", []):
            object_fields(activity, ("id", "status", "trade_at", "settlement_date", "quantity"))
            key = text(activity["id"])
            check(key not in activity_ids, "DUPLICATE_ACTIVITY")
            activity_ids.add(key)
            check(activity["status"] in ("pending", "unsettled", "settled"), "UNSUPPORTED_ACTIVITY")
            traded = stamp(activity["trade_at"])
            check(traded <= observed, "FUTURE_TRADE")
            try:
                settlement = date.fromisoformat(activity["settlement_date"])
                # Settlement is a provider-local calendar date, never a substitute for
                # trade time; no inferred T+1/business-day calendar is applied.
                local_trade_date = datetime.fromisoformat(activity["trade_at"].replace("Z", "+00:00")).date()
            except (TypeError, ValueError) as exc:
                raise Rejected("INVALID_SETTLEMENT_DATE") from exc
            check(settlement >= local_trade_date, "SETTLEMENT_BEFORE_TRADE")
            activities.append({"id": key, "status": activity["status"], "trade_at": iso(traded),
                               "settlement_date": settlement.isoformat(),
                               "quantity": money_text(dec(activity["quantity"])),
                               "treatment": "excluded_from_holdings_snapshot"})
        draft = adapt(capture["broker"], capture["account"], payloads, context)
        if draft.get("source_date") is not None:
            capture_local_date = datetime.fromisoformat(capture["as_of"].replace("Z", "+00:00")).date().isoformat()
            check(draft["source_date"] == capture_local_date, "SOURCE_DATE_CONFLICT")
        rows = draft["rows"]
        check(len(rows) <= MAX_ROWS, "TOO_MANY_ROWS")
        expected = context["expected_rows"]
        check(expected is None or type(expected) is int and expected >= 0, "INVALID_EXPECTED_ROWS")
        check(expected is None or expected == len(rows), "ROW_COVERAGE_MISMATCH")
        positions, cash_positions, issues, seen = [], [], [], set()
        with localcontext(ARITHMETIC):
            for i, row in enumerate(rows):
                path = f"$.rows[{i}]"
                item, refs = self.reference.resolve(capture["broker"], row["refs"], row["currency"], observed)
                check(item["id"] not in seen, "DUPLICATE_POSITION", path)
                seen.add(item["id"])
                check(row["asset_type"] is None or row["asset_type"] == item["type"], "ASSET_TYPE_MISMATCH", path)
                quantity = dec(row["quantity"], path=path + ".quantity")
                multiplier = dec(item["multiplier"], signed=False)
                units = row["units"]
                if item["type"] == "option":
                    check(units in ("contracts", "underlying"), "OPTION_UNITS_UNKNOWN", path)
                    if units == "underlying":
                        quantity /= multiplier
                    check(quantity == quantity.to_integral_value(), "FRACTIONAL_OPTION_CONTRACT", path)
                    terms = row.get("option_terms")
                    if terms:
                        check(terms["right"] == item["right"] and terms["expiry"] == item["expiry"] and
                              dec(terms["strike"], signed=False) == dec(item["strike"], signed=False),
                              "OPTION_TERMS_CONFLICT", path)
                        # Underlying ticker is a reviewed alias, not a root parsed from OCC text.
                        underlying, _ = self.reference.resolve(capture["broker"],
                            [("ticker", terms["underlying_ticker"])], row["currency"], observed)
                        check(underlying["id"] == item["underlying_id"], "OPTION_UNDERLYING_CONFLICT", path)
                else:
                    check(units == ("currency" if item["type"] == "cash" else "shares"), "UNIT_MISMATCH", path)
                price = dec(row["price"], nullable=True, signed=False, path=path + ".price")
                quote_time = None if row["price_at"] is None else stamp(row["price_at"], path + ".price_at")
                check(quote_time is None or quote_time <= observed, "FUTURE_PRICE", path)
                reason = ("MISSING_PRICE" if price is None else
                          "INACTIVE_INSTRUMENT" if item["status"] != "active" else
                          "EXPIRED_OPTION" if item["type"] == "option" and observed.date() > date.fromisoformat(item["expiry"]) else
                          "UNKNOWN_PRICE_TIME" if quote_time is None else
                          "STALE_PRICE" if observed - quote_time > timedelta(seconds=self.max_age) else None)
                if item["type"] == "cash":
                    check(price == 1, "INVALID_CASH_PRICE", path)
                    reason = None
                value = None if reason else quantity * multiplier * price
                reported = dec(row["reported_value"], nullable=True, path=path + ".reported_value")
                if value is not None and reported is not None:
                    check(abs(value - reported) <= Decimal("0.01"), "POSITION_VALUE_MISMATCH", path)
                basis = dec(row["cost_basis"], nullable=True, path=path + ".cost_basis")
                lots = row["lots"]
                check(type(lots) is list, "INVALID_LOTS", path)
                lot_ids, lot_qty, lot_basis, lot_complete = set(), Decimal(0), Decimal(0), True
                normalized_lots = []
                for lot in lots:
                    object_fields(lot, ("id", "quantity", "cost_basis", "acquired_at"))
                    key = text(lot["id"])
                    check(key not in lot_ids, "DUPLICATE_LOT", path)
                    lot_ids.add(key)
                    q = dec(lot["quantity"], nullable=True)
                    b = dec(lot["cost_basis"], nullable=True)
                    if q is not None and item["type"] == "option" and units == "underlying":
                        q /= multiplier
                        check(q == q.to_integral_value(), "FRACTIONAL_OPTION_CONTRACT", path)
                    acquired = None if lot["acquired_at"] is None else iso(stamp(lot["acquired_at"]))
                    check(acquired is None or stamp(acquired) <= observed, "FUTURE_LOT", path)
                    lot_complete &= q is not None and b is not None
                    lot_qty += q or Decimal(0)
                    lot_basis += b or Decimal(0)
                    normalized_lots.append({"id": key, "quantity": money_text(q), "cost_basis": money_text(b),
                                            "acquired_at": acquired})
                if lots and lot_complete:
                    # Adapter converts option lots into contracts just like parent quantity.
                    check(lot_qty == quantity, "LOT_QUANTITY_MISMATCH", path)
                    check(basis is None or abs(lot_basis - basis) <= Decimal("0.01"), "LOT_BASIS_MISMATCH", path)
                basis_source = "aggregate" if basis is not None else "lots" if lots and lot_complete else "missing"
                if basis is None and lots and lot_complete:
                    basis = lot_basis
                if reason:
                    issues.append({"code": reason, "instrument_id": item["id"]})
                if basis is None and item["type"] != "cash":
                    issues.append({"code": "MISSING_COST_BASIS", "instrument_id": item["id"]})
                if lots and not lot_complete:
                    issues.append({"code": "INCOMPLETE_LOTS", "instrument_id": item["id"]})
                position = {"instrument_id": item["id"], "asset_type": item["type"], "currency": item["currency"],
                    "quantity": money_text(quantity), "quantity_unit": "contracts" if item["type"] == "option" else units,
                    "multiplier": money_text(multiplier), "observed_price": money_text(price),
                    "price_at": None if quote_time is None else iso(quote_time), "value": money_text(value),
                    "price_date": row.get("price_date"),
                    "value_status": reason or "COMPLETE", "reported_value": money_text(reported),
                    "cost_basis": money_text(basis), "basis_source": basis_source,
                    "lots": sorted(normalized_lots, key=lambda l: l["id"]), "identity_evidence": refs}
                if item["type"] == "option":
                    position["option"] = {k: item[k] for k in ("underlying_id", "expiry", "right", "strike", "deliverable")}
                if item["type"] == "cash":
                    cash_positions.append(position)
                else:
                    positions.append(position)
            balances = context["cash_balances"]
            check(type(balances) is list, "INVALID_CASH_BALANCES")
            cash, cash_ccy = [], set()
            for balance in balances:
                object_fields(balance, ("currency", "amount"))
                ccy = balance["currency"]
                check(ccy in ("USD", "CAD") and ccy not in cash_ccy, "DUPLICATE_OR_INVALID_CASH")
                check(not any(p["currency"] == ccy for p in cash_positions), "CASH_DOUBLE_COUNT")
                cash_ccy.add(ccy)
                cash.append({"currency": ccy, "amount": money_text(dec(balance["amount"])), "source": "balance"})
            for p in cash_positions:
                check(p["currency"] not in cash_ccy, "DUPLICATE_OR_INVALID_CASH")
                cash_ccy.add(p["currency"])
                # A currency unit is worth one of itself; cash classification is from registry.
                check(p["observed_price"] == "1", "INVALID_CASH_PRICE")
                cash.append({"currency": p["currency"], "amount": p["quantity"], "source": "position",
                             "instrument_id": p["instrument_id"]})
            currency = context["currency"]
            # Explicit single-currency account scope. Multi-currency remains separate accounts/reports.
            check(all(p["currency"] == currency for p in positions) and all(c["currency"] == currency for c in cash),
                  "MIXED_ACCOUNT_CURRENCY")
            known = sum((Decimal(p["value"]) for p in positions if p["value"] is not None), Decimal(0))
            cash_value = sum((Decimal(c["amount"]) for c in cash), Decimal(0))
            complete = all(p["value"] is not None for p in positions)
            reported_total = dec(context["reported_total"], nullable=True)
            scope = context["total_scope"]
            check(scope in ("holdings", "holdings_plus_cash"), "UNSUPPORTED_TOTAL_SCOPE")
            target = known + (cash_value if scope == "holdings_plus_cash" else Decimal(0))
            reconciliation = "NOT_PROVIDED" if reported_total is None else "INCOMPLETE" if not complete else "MATCHED"
            if reported_total is not None and complete:
                check(abs(target - reported_total) <= Decimal("0.01"), "REPORTED_TOTAL_MISMATCH")
        return {"contract": "broker-holdings/v2", "reference_version": self.reference.document["version"],
            "reference_sha256": self.reference.hash, "broker": capture["broker"], "account": capture["account"],
            "source_date": draft.get("source_date"),
            "snapshot_id": capture["snapshot_id"], "as_of": iso(observed), "settlement_basis": context["settlement_basis"],
            "positions": sorted(positions, key=lambda p: p["instrument_id"]), "cash": sorted(cash, key=lambda c: c["currency"]),
            "activities": sorted(activities, key=lambda a: a["id"]),
            "input_rows": len(rows), "cash_rows": len(cash_positions), "currency": currency,
            "known_holdings_value": money_text(known), "cash_value": money_text(cash_value),
            "valuation_complete": complete, "reported_total": money_text(reported_total),
            "reconciliation_scope": scope, "reconciliation": reconciliation,
            "issues": sorted(issues, key=lambda i: (i["instrument_id"], i["code"])),
            "status": "WARN" if issues or not complete else "SUSPECT" if not rows and not cash else "OK"}
