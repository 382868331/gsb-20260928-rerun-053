from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from risk_platform.scenarios import MarketScenario


@dataclass(frozen=True, slots=True)
class CashFlow:
    """An external cash flow received at a step, in the holding's currency.

    Positive values add units (coupon received / reinvestment in), negative
    values withdraw units. Cash flows settle *at* the step snapshot and are
    kept separate from the market/trading decomposition.
    """

    holding_id: str
    amount: float

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "CashFlow":
        for key in ("holding_id", "amount"):
            if key not in data:
                raise ValueError(f"Cash flow is missing field: {key}")
        return cls(holding_id=str(data["holding_id"]), amount=float(data["amount"]))


@dataclass(frozen=True, slots=True)
class Trade:
    """A pre-arranged rebalancing instruction executed at a step.

    The quantity delta is applied to the target holding. Non-cash trades move
    a balancing cash leg in the holding's currency automatically, so trades
    never create or destroy portfolio value at execution prices.
    """

    holding_id: str
    quantity_delta: float

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Trade":
        for key in ("holding_id", "quantity_delta"):
            if key not in data:
                raise ValueError(f"Trade is missing field: {key}")
        return cls(
            holding_id=str(data["holding_id"]),
            quantity_delta=float(data["quantity_delta"]),
        )


@dataclass(frozen=True, slots=True)
class PathStep:
    label: str
    market: MarketScenario
    cash_flows: tuple[CashFlow, ...] = ()
    trades: tuple[Trade, ...] = ()


@dataclass(frozen=True, slots=True)
class StressPath:
    path_id: str
    version: int
    description: str
    strategy: str  # "hold" or "rebalance"
    steps: tuple[PathStep, ...]
    fingerprint: str

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "StressPath":
        required = ("path_id", "version", "strategy", "steps")
        missing = [key for key in required if key not in data]
        if missing:
            raise ValueError(f"Path is missing fields: {', '.join(missing)}")

        path_id = str(data["path_id"])
        version = int(data["version"])
        description = str(data.get("description", ""))
        strategy = str(data["strategy"]).lower()
        if strategy not in ("hold", "rebalance"):
            raise ValueError("Path strategy must be 'hold' or 'rebalance'")

        raw_steps = data["steps"]
        if not isinstance(raw_steps, list) or len(raw_steps) < 2:
            raise ValueError(f"Path {path_id} needs at least a start and one later step")

        steps: list[PathStep] = []
        for index, raw_step in enumerate(raw_steps):
            label = str(raw_step.get("label", f"t{index}"))
            if "market" not in raw_step:
                raise ValueError(f"Path {path_id} step {label} is missing market data")
            market = MarketScenario.from_dict(raw_step["market"])
            cash_flows = tuple(
                CashFlow.from_dict(item) for item in raw_step.get("cash_flows", ())
            )
            trades = tuple(Trade.from_dict(item) for item in raw_step.get("trades", ()))
            if index == 0 and (cash_flows or trades):
                raise ValueError(f"Path {path_id} start step must contain no flows or trades")
            steps.append(
                PathStep(label=label, market=market, cash_flows=cash_flows, trades=trades)
            )

        if strategy == "hold" and any(step.trades for step in steps):
            raise ValueError(f"Hold path {path_id} must not contain rebalancing trades")

        partial = cls(
            path_id=path_id,
            version=version,
            description=description,
            strategy=strategy,
            steps=tuple(steps),
            fingerprint="",
        )
        digest = hashlib.sha256(partial._canonical_bytes()).hexdigest()
        return cls(
            path_id=path_id,
            version=version,
            description=description,
            strategy=strategy,
            steps=tuple(steps),
            fingerprint=digest,
        )

    def _canonical_bytes(self) -> bytes:
        canonical = {
            "path_id": self.path_id,
            "version": self.version,
            "description": self.description,
            "strategy": self.strategy,
            "steps": [
                {
                    "label": step.label,
                    "market": step.market.canonical_dict(),
                    "cash_flows": [
                        {"holding_id": flow.holding_id, "amount": flow.amount}
                        for flow in step.cash_flows
                    ],
                    "trades": [
                        {"holding_id": trade.holding_id, "quantity_delta": trade.quantity_delta}
                        for trade in step.trades
                    ],
                }
                for step in self.steps
            ],
        }
        return json.dumps(
            canonical, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")


def load_paths(path: str | Path) -> list[StressPath]:
    with Path(path).open("r", encoding="utf-8") as handle:
        payload = json.load(handle)
    raw_paths = payload.get("paths", payload)
    if not isinstance(raw_paths, list) or not raw_paths:
        raise ValueError("At least one stress path is required")

    paths = [StressPath.from_dict(item) for item in raw_paths]
    keys = [(item.path_id, item.version) for item in paths]
    if len(keys) != len(set(keys)):
        raise ValueError("(path_id, version) pairs must be unique")
    return paths
