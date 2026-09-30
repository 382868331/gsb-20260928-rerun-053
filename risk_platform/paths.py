from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from risk_platform.holdings import Holding
from risk_platform.scenarios import MarketScenario


@dataclass(frozen=True, slots=True)
class CashFlow:
    currency: str
    amount: float

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "CashFlow":
        if "currency" not in data or "amount" not in data:
            raise ValueError("Cash flow requires currency and amount")
        return cls(currency=str(data["currency"]).upper(), amount=float(data["amount"]))

    def canonical_dict(self) -> dict[str, Any]:
        return {"currency": self.currency, "amount": self.amount}


@dataclass(frozen=True, slots=True)
class Trade:
    holding_id: str
    quantity_delta: float

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Trade":
        if "holding_id" not in data or "quantity_delta" not in data:
            raise ValueError("Trade requires holding_id and quantity_delta")
        return cls(
            holding_id=str(data["holding_id"]),
            quantity_delta=float(data["quantity_delta"]),
        )

    def canonical_dict(self) -> dict[str, Any]:
        return {"holding_id": self.holding_id, "quantity_delta": self.quantity_delta}


@dataclass(frozen=True, slots=True)
class PathStep:
    step_id: str
    scenario: MarketScenario
    cash_flows: tuple[CashFlow, ...]
    trades: tuple[Trade, ...]

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "PathStep":
        if "step_id" not in data or "scenario" not in data:
            raise ValueError("Path step requires step_id and scenario")
        return cls(
            step_id=str(data["step_id"]),
            scenario=MarketScenario.from_dict(data["scenario"]),
            cash_flows=tuple(
                CashFlow.from_dict(item) for item in data.get("cash_flows", ())
            ),
            trades=tuple(Trade.from_dict(item) for item in data.get("trades", ())),
        )

    def canonical_dict(self) -> dict[str, Any]:
        return {
            "step_id": self.step_id,
            "scenario": self.scenario.canonical_dict(),
            "cash_flows": [item.canonical_dict() for item in self.cash_flows],
            "trades": [item.canonical_dict() for item in self.trades],
        }


@dataclass(frozen=True, slots=True)
class StressPath:
    """An ordered sequence of stress steps replayed against a portfolio.

    The first step is the initial market state; every later step moves the
    market, may pay cash flows (reinvested as cash in their currency) and may
    execute a pre-defined rebalancing schedule. Paths are versioned so a
    revised path can be replayed side-by-side with its history.
    """

    path_id: str
    version: int
    description: str
    steps: tuple[PathStep, ...]
    fingerprint: str

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "StressPath":
        required = ("path_id", "version", "steps")
        missing = [key for key in required if key not in data]
        if missing:
            raise ValueError(f"Path is missing fields: {', '.join(missing)}")

        steps = tuple(PathStep.from_dict(item) for item in data["steps"])
        if len(steps) < 2:
            raise ValueError("A stress path needs an initial step and at least one move")
        step_ids = [step.step_id for step in steps]
        if len(step_ids) != len(set(step_ids)):
            raise ValueError("Step ids must be unique within a path")

        partial = cls(
            path_id=str(data["path_id"]),
            version=int(data["version"]),
            description=str(data.get("description", "")),
            steps=steps,
            fingerprint="",
        )
        digest = hashlib.sha256(partial.canonical_bytes()).hexdigest()
        return cls(
            path_id=partial.path_id,
            version=partial.version,
            description=partial.description,
            steps=steps,
            fingerprint=digest,
        )

    def canonical_dict(self) -> dict[str, Any]:
        return {
            "path_id": self.path_id,
            "version": self.version,
            "description": self.description,
            "steps": [step.canonical_dict() for step in self.steps],
        }

    def canonical_bytes(self) -> bytes:
        return json.dumps(
            self.canonical_dict(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")

    def validate_holdings(self, holdings: list[Holding]) -> None:
        holding_ids = {holding.id for holding in holdings}
        for step in self.steps:
            for trade in step.trades:
                if trade.holding_id not in holding_ids:
                    raise ValueError(
                        f"Path {self.path_id} step {step.step_id} trades unknown "
                        f"holding {trade.holding_id}"
                    )


def load_paths(path: str | Path) -> list[StressPath]:
    with Path(path).open("r", encoding="utf-8") as handle:
        payload = json.load(handle)
    raw_paths = payload.get("paths", payload)
    if not isinstance(raw_paths, list) or not raw_paths:
        raise ValueError("At least one path is required")

    paths = [StressPath.from_dict(item) for item in raw_paths]
    keys = [(item.path_id, item.version) for item in paths]
    if len(keys) != len(set(keys)):
        raise ValueError("(path_id, version) pairs must be unique")
    return paths
