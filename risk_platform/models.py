from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Mapping


Number = Decimal | int | float | str


def money(value: Number) -> Decimal:
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


@dataclass(frozen=True, slots=True)
class Holding:
    holding_id: str
    symbol: str
    asset_type: str
    quantity: Decimal
    currency: str

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "Holding":
        return cls(
            holding_id=str(data["holding_id"]),
            symbol=str(data["symbol"]),
            asset_type=str(data["asset_type"]),
            quantity=money(data["quantity"]),
            currency=str(data["currency"]),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "holding_id": self.holding_id,
            "symbol": self.symbol,
            "asset_type": self.asset_type,
            "quantity": str(self.quantity),
            "currency": self.currency,
        }


@dataclass(frozen=True, slots=True)
class ScenarioSnapshot:
    scenario_id: str
    version: int
    as_of: str
    prices: Mapping[tuple[str, str], Decimal]
    fx_rates_to_base: Mapping[str, Decimal]
    base_currency: str = "USD"
    description: str = ""

    @classmethod
    def from_dict(cls, scenario_id: str, data: Mapping[str, Any]) -> "ScenarioSnapshot":
        prices = {
            (str(item["symbol"]), str(item["currency"])): money(item["price"])
            for item in data["prices"]
        }
        fx_rates = {
            str(currency): money(rate)
            for currency, rate in data["fx_rates_to_base"].items()
        }
        return cls(
            scenario_id=scenario_id,
            version=int(data["version"]),
            as_of=str(data["as_of"]),
            prices=prices,
            fx_rates_to_base=fx_rates,
            base_currency=str(data.get("base_currency", "USD")),
            description=str(data.get("description", "")),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "scenario_id": self.scenario_id,
            "version": self.version,
            "as_of": self.as_of,
            "base_currency": self.base_currency,
            "description": self.description,
            "fx_rates_to_base": {
                currency: str(rate)
                for currency, rate in sorted(self.fx_rates_to_base.items())
            },
            "prices": [
                {"symbol": symbol, "currency": currency, "price": str(price)}
                for (symbol, currency), price in sorted(self.prices.items())
            ],
        }


@dataclass(frozen=True, slots=True)
class HoldingValuation:
    holding_id: str
    symbol: str
    quantity: Decimal
    currency: str
    local_price: Decimal
    fx_rate_to_base: Decimal
    market_value_base: Decimal

    def to_dict(self) -> dict[str, Any]:
        return {
            "holding_id": self.holding_id,
            "symbol": self.symbol,
            "quantity": str(self.quantity),
            "currency": self.currency,
            "local_price": str(self.local_price),
            "fx_rate_to_base": str(self.fx_rate_to_base),
            "market_value_base": str(self.market_value_base),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "HoldingValuation":
        return cls(
            holding_id=str(data["holding_id"]),
            symbol=str(data["symbol"]),
            quantity=money(data["quantity"]),
            currency=str(data["currency"]),
            local_price=money(data["local_price"]),
            fx_rate_to_base=money(data["fx_rate_to_base"]),
            market_value_base=money(data["market_value_base"]),
        )


@dataclass(frozen=True, slots=True)
class ValuationResult:
    run_id: str
    scenario_id: str
    scenario_version: int
    scenario_as_of: str
    scenario_hash: str
    base_currency: str
    holdings: tuple[HoldingValuation, ...]
    total_value_base: Decimal

    def holding_map(self) -> dict[str, HoldingValuation]:
        return {item.holding_id: item for item in self.holdings}

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": "valuation",
            "run_id": self.run_id,
            "scenario_id": self.scenario_id,
            "scenario_version": self.scenario_version,
            "scenario_as_of": self.scenario_as_of,
            "scenario_hash": self.scenario_hash,
            "base_currency": self.base_currency,
            "total_value_base": str(self.total_value_base),
            "holdings": [item.to_dict() for item in self.holdings],
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "ValuationResult":
        return cls(
            run_id=str(data["run_id"]),
            scenario_id=str(data["scenario_id"]),
            scenario_version=int(data["scenario_version"]),
            scenario_as_of=str(data["scenario_as_of"]),
            scenario_hash=str(data["scenario_hash"]),
            base_currency=str(data["base_currency"]),
            holdings=tuple(HoldingValuation.from_dict(x) for x in data["holdings"]),
            total_value_base=money(data["total_value_base"]),
        )


@dataclass(frozen=True, slots=True)
class PnlContribution:
    holding_id: str
    symbol: str
    baseline_value_base: Decimal
    scenario_value_base: Decimal
    pnl_base: Decimal

    def to_dict(self) -> dict[str, Any]:
        return {
            "holding_id": self.holding_id,
            "symbol": self.symbol,
            "baseline_value_base": str(self.baseline_value_base),
            "scenario_value_base": str(self.scenario_value_base),
            "pnl_base": str(self.pnl_base),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "PnlContribution":
        return cls(
            holding_id=str(data["holding_id"]),
            symbol=str(data["symbol"]),
            baseline_value_base=money(data["baseline_value_base"]),
            scenario_value_base=money(data["scenario_value_base"]),
            pnl_base=money(data["pnl_base"]),
        )


@dataclass(frozen=True, slots=True)
class StressResult:
    run_id: str
    scenario_id: str
    scenario_version: int
    baseline_run_id: str
    contributions: tuple[PnlContribution, ...]
    total_pnl_base: Decimal

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": "stress_pnl",
            "run_id": self.run_id,
            "scenario_id": self.scenario_id,
            "scenario_version": self.scenario_version,
            "baseline_run_id": self.baseline_run_id,
            "total_pnl_base": str(self.total_pnl_base),
            "contributions": [item.to_dict() for item in self.contributions],
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "StressResult":
        return cls(
            run_id=str(data["run_id"]),
            scenario_id=str(data["scenario_id"]),
            scenario_version=int(data["scenario_version"]),
            baseline_run_id=str(data["baseline_run_id"]),
            contributions=tuple(
                PnlContribution.from_dict(x) for x in data["contributions"]
            ),
            total_pnl_base=money(data["total_pnl_base"]),
        )
