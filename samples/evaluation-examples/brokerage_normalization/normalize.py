"""Normalize two invented brokerage payloads without losing financial semantics."""

from __future__ import annotations

import json
import sys
from collections import defaultdict
from decimal import Decimal, InvalidOperation
from pathlib import Path


HERE = Path(__file__).resolve().parent


def decimal_or_none(value: object, field: str) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"invalid decimal in {field}") from exc


def normalize_alpha(payload: dict) -> tuple[list[dict], list[dict]]:
    accounts, positions = [], []
    for account in payload.get("accounts", []):
        account_id = str(account["account_id"])
        accounts.append(
            {
                "broker": str(payload["broker"]),
                "account_id": account_id,
                "account_value": decimal_or_none(account.get("total_value"), "total_value"),
            }
        )
        for position in account.get("positions", []):
            quantity = decimal_or_none(position.get("quantity"), "quantity")
            if quantity is None:
                raise ValueError("quantity cannot be null")
            price = decimal_or_none(position.get("last_price"), "last_price")
            positions.append(
                {
                    "broker": str(payload["broker"]),
                    "account_id": account_id,
                    "symbol": str(position["symbol"]).upper(),
                    "quantity": quantity,
                    "price": price,
                    "market_value": quantity * price if price is not None else None,
                }
            )
    return accounts, positions


def normalize_beta(payload: dict) -> tuple[list[dict], list[dict]]:
    accounts, positions = [], []
    for account in payload.get("portfolios", []):
        account_id = str(account["id"])
        cents = decimal_or_none(account.get("equity_cents"), "equity_cents")
        accounts.append(
            {
                "broker": str(payload["provider"]),
                "account_id": account_id,
                "account_value": cents / Decimal("100") if cents is not None else None,
            }
        )
        for position in account.get("holdings", []):
            quantity = decimal_or_none(position.get("units"), "units")
            if quantity is None:
                raise ValueError("units cannot be null")
            price = decimal_or_none(position.get("mark"), "mark")
            positions.append(
                {
                    "broker": str(payload["provider"]),
                    "account_id": account_id,
                    "symbol": str(position["ticker"]).upper(),
                    "quantity": quantity,
                    "price": price,
                    "market_value": quantity * price if price is not None else None,
                }
            )
    return accounts, positions


def aggregate_positions(positions: list[dict]) -> list[dict]:
    """Deduplicate symbols while preserving whether valuation is incomplete."""
    grouped: dict[str, list[dict]] = defaultdict(list)
    for position in positions:
        grouped[position["symbol"]].append(position)

    output = []
    for symbol in sorted(grouped):
        lots = grouped[symbol]
        complete = all(lot["market_value"] is not None for lot in lots)
        output.append(
            {
                "symbol": symbol,
                "quantity": sum((lot["quantity"] for lot in lots), Decimal("0")),
                "known_market_value": sum(
                    (lot["market_value"] for lot in lots if lot["market_value"] is not None),
                    Decimal("0"),
                ),
                "market_value_complete": complete,
                "lot_count": len(lots),
            }
        )
    return output


def load_payload(path: Path) -> dict:
    try:
        with path.open("r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot load fixture {path.name}: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"fixture {path.name} must contain an object")
    return payload


def json_ready(value: object) -> object:
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, list):
        return [json_ready(item) for item in value]
    if isinstance(value, dict):
        return {key: json_ready(item) for key, item in value.items()}
    return value


def build_example() -> dict:
    alpha_accounts, alpha_positions = normalize_alpha(
        load_payload(HERE / "fixtures" / "broker_alpha.json")
    )
    beta_accounts, beta_positions = normalize_beta(
        load_payload(HERE / "fixtures" / "broker_beta.json")
    )
    accounts = alpha_accounts + beta_accounts
    positions = alpha_positions + beta_positions
    return json_ready(
        {
            "scope": "invented_illustrative_reimplementation",
            "accounts": accounts,
            "positions_by_symbol": aggregate_positions(positions),
        }
    )


def main() -> int:
    try:
        print(json.dumps(build_example(), indent=2, sort_keys=True, allow_nan=False))
        return 0
    except (KeyError, TypeError, ValueError) as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
