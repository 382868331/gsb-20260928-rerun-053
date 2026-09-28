from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True, slots=True)
class Holding:
    id: str
    name: str
    asset_type: str
    currency: str
    quantity: float
    market_data_key: str
    years_to_maturity: float | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Holding":
        required = ("id", "name", "asset_type", "currency", "quantity")
        missing = [key for key in required if key not in data]
        if missing:
            raise ValueError(f"Holding is missing fields: {', '.join(missing)}")

        asset_type = str(data["asset_type"]).lower()
        if asset_type == "cash":
            market_data_key = data.get("market_data_key", data["id"])
        else:
            if "market_data_key" not in data:
                raise ValueError(f"Holding {data['id']} requires market_data_key")
            market_data_key = data["market_data_key"]

        quantity = float(data["quantity"])
        if quantity < 0:
            raise ValueError(f"Holding {data['id']} quantity must not be negative")

        years_to_maturity = None
        if asset_type == "zero_coupon_bond":
            if "years_to_maturity" not in data:
                raise ValueError(f"Holding {data['id']} requires years_to_maturity")
            years_to_maturity = float(data["years_to_maturity"])
            if years_to_maturity <= 0:
                raise ValueError(f"Holding {data['id']} years_to_maturity must be positive")

        return cls(
            id=str(data["id"]),
            name=str(data["name"]),
            asset_type=asset_type,
            currency=str(data["currency"]).upper(),
            quantity=quantity,
            market_data_key=str(market_data_key),
            years_to_maturity=years_to_maturity,
        )


def load_holdings(path: str | Path) -> list[Holding]:
    with Path(path).open("r", encoding="utf-8") as handle:
        payload = json.load(handle)
    raw_holdings = payload.get("holdings", payload)
    if not isinstance(raw_holdings, list) or not raw_holdings:
        raise ValueError("At least one holding is required")

    holdings = [Holding.from_dict(item) for item in raw_holdings]
    ids = [holding.id for holding in holdings]
    if len(ids) != len(set(ids)):
        raise ValueError("Holding ids must be unique")
    return holdings
