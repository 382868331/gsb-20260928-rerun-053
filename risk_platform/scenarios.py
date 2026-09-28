from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from risk_platform.holdings import Holding


class MissingMarketDataError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class MarketScenario:
    scenario_id: str
    version: int
    description: str
    equity_prices: dict[str, float]
    bond_yields: dict[str, float]
    fx_rates: dict[str, float]
    fingerprint: str

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "MarketScenario":
        required = ("scenario_id", "version", "equity_prices", "bond_yields", "fx_rates")
        missing = [key for key in required if key not in data]
        if missing:
            raise ValueError(f"Scenario is missing fields: {', '.join(missing)}")

        scenario_id = str(data["scenario_id"])
        version = int(data["version"])
        description = str(data.get("description", ""))
        equity_prices = _positive_number_map(data["equity_prices"], "equity_prices")
        bond_yields = _number_map(data["bond_yields"], "bond_yields")
        fx_rates = _positive_number_map(data["fx_rates"], "fx_rates")
        partial = cls(
            scenario_id=scenario_id,
            version=version,
            description=description,
            equity_prices=equity_prices,
            bond_yields=bond_yields,
            fx_rates=fx_rates,
            fingerprint="",
        )
        digest = hashlib.sha256(partial.canonical_bytes()).hexdigest()
        return cls(
            scenario_id=scenario_id,
            version=version,
            description=description,
            equity_prices=equity_prices,
            bond_yields=bond_yields,
            fx_rates=fx_rates,
            fingerprint=digest,
        )

    def canonical_dict(self) -> dict[str, Any]:
        return {
            "scenario_id": self.scenario_id,
            "version": self.version,
            "description": self.description,
            "equity_prices": dict(self.equity_prices),
            "bond_yields": dict(self.bond_yields),
            "fx_rates": dict(self.fx_rates),
        }

    def canonical_bytes(self) -> bytes:
        return json.dumps(
            self.canonical_dict(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")

    def validate_holdings(self, holdings: list[Holding]) -> None:
        missing: list[str] = []
        for holding in holdings:
            if holding.asset_type == "equity":
                if holding.market_data_key not in self.equity_prices:
                    missing.append(f"{holding.id}:equity_price:{holding.market_data_key}")
            elif holding.asset_type == "zero_coupon_bond":
                if holding.market_data_key not in self.bond_yields:
                    missing.append(f"{holding.id}:bond_yield:{holding.market_data_key}")
            elif holding.asset_type != "cash":
                raise ValueError(f"Unsupported asset type for {holding.id}: {holding.asset_type}")

            if holding.currency not in self.fx_rates:
                missing.append(f"{holding.id}:fx_rate:{holding.currency}")

        if missing:
            raise MissingMarketDataError(
                f"Scenario {self.scenario_id} v{self.version} lacks market data: {', '.join(missing)}"
            )

    def fx_rate(self, currency: str) -> float:
        return self.fx_rates[currency]


def _number_map(value: Any, field: str) -> dict[str, float]:
    if not isinstance(value, dict):
        raise ValueError(f"{field} must be an object")
    return {str(key): float(number) for key, number in value.items()}


def _positive_number_map(value: Any, field: str) -> dict[str, float]:
    result = _number_map(value, field)
    invalid = [key for key, number in result.items() if number <= 0]
    if invalid:
        raise ValueError(f"{field} values must be positive: {', '.join(invalid)}")
    return result


def load_scenarios(path: str | Path) -> list[MarketScenario]:
    with Path(path).open("r", encoding="utf-8") as handle:
        payload = json.load(handle)
    raw_scenarios = payload.get("scenarios", payload)
    if not isinstance(raw_scenarios, list) or not raw_scenarios:
        raise ValueError("At least one scenario is required")

    scenarios = [MarketScenario.from_dict(item) for item in raw_scenarios]
    keys = [(scenario.scenario_id, scenario.version) for scenario in scenarios]
    if len(keys) != len(set(keys)):
        raise ValueError("(scenario_id, version) pairs must be unique")
    return scenarios
