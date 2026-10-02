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
class RiskLimits:
    max_loss: float | None
    currency_exposure: dict[str, float]
    asset_concentration: dict[str, float]

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "RiskLimits":
        max_loss = data.get("max_loss")
        currency_exposure = _number_map(
            data.get("currency_exposure", {}), "currency_exposure"
        )
        asset_concentration = _number_map(
            data.get("asset_concentration", {}), "asset_concentration"
        )
        invalid = [name for name, value in currency_exposure.items() if value < 0]
        if invalid:
            raise ValueError("Currency exposure limits must not be negative")
        invalid = [
            name
            for name, value in asset_concentration.items()
            if value < 0 or value > 1
        ]
        if invalid:
            raise ValueError("Asset concentration limits must be between 0 and 1")
        return cls(
            max_loss=None if max_loss is None else float(max_loss),
            currency_exposure={str(key).upper(): value for key, value in currency_exposure.items()},
            asset_concentration={str(key): value for key, value in asset_concentration.items()},
        )

    def canonical_dict(self) -> dict[str, Any]:
        return {
            "max_loss": self.max_loss,
            "currency_exposure": dict(self.currency_exposure),
            "asset_concentration": dict(self.asset_concentration),
        }

    @property
    def configured(self) -> bool:
        return (
            self.max_loss is not None
            or bool(self.currency_exposure)
            or bool(self.asset_concentration)
        )


@dataclass(frozen=True, slots=True)
class ActionOrder:
    order_type: str
    fee_bps: float = 0.0
    holding_id: str | None = None
    quantity: float | None = None
    from_currency: str | None = None
    to_currency: str | None = None
    from_amount: float | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ActionOrder":
        order_type = str(data.get("type", "")).lower()
        fee_bps = float(data.get("fee_bps", 0.0))
        if fee_bps < 0:
            raise ValueError("Action fee_bps must not be negative")
        if order_type == "sell":
            required = ("holding_id", "quantity")
            missing = [key for key in required if key not in data]
            if missing:
                raise ValueError(f"Sell action missing fields: {', '.join(missing)}")
            quantity = float(data["quantity"])
            if quantity <= 0:
                raise ValueError("Sell quantity must be positive")
            return cls(
                order_type=order_type,
                fee_bps=fee_bps,
                holding_id=str(data["holding_id"]),
                quantity=quantity,
            )
        if order_type == "fx_conversion":
            required = ("from_currency", "to_currency", "from_amount")
            missing = [key for key in required if key not in data]
            if missing:
                raise ValueError(f"FX action missing fields: {', '.join(missing)}")
            from_amount = float(data["from_amount"])
            if from_amount <= 0:
                raise ValueError("FX conversion amount must be positive")
            return cls(
                order_type=order_type,
                fee_bps=fee_bps,
                from_currency=str(data["from_currency"]).upper(),
                to_currency=str(data["to_currency"]).upper(),
                from_amount=from_amount,
            )
        raise ValueError(f"Unsupported action type: {order_type}")

    def canonical_dict(self) -> dict[str, Any]:
        if self.order_type == "sell":
            return {
                "type": self.order_type,
                "holding_id": self.holding_id,
                "quantity": self.quantity,
                "fee_bps": self.fee_bps,
            }
        return {
            "type": self.order_type,
            "from_currency": self.from_currency,
            "to_currency": self.to_currency,
            "from_amount": self.from_amount,
            "fee_bps": self.fee_bps,
        }


@dataclass(frozen=True, slots=True)
class ActionPlan:
    plan_id: str
    trigger_step_id: str
    orders: tuple[ActionOrder, ...]

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ActionPlan":
        required = ("plan_id", "trigger_step_id", "orders")
        missing = [key for key in required if key not in data]
        if missing:
            raise ValueError(f"Action plan is missing fields: {', '.join(missing)}")
        orders = tuple(ActionOrder.from_dict(item) for item in data["orders"])
        if not orders:
            raise ValueError("Action plan requires at least one order")
        return cls(
            plan_id=str(data["plan_id"]),
            trigger_step_id=str(data["trigger_step_id"]),
            orders=orders,
        )

    def canonical_dict(self) -> dict[str, Any]:
        return {
            "plan_id": self.plan_id,
            "trigger_step_id": self.trigger_step_id,
            "orders": [order.canonical_dict() for order in self.orders],
        }


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
    limits: RiskLimits
    action_plans: tuple[ActionPlan, ...]
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
        limits = RiskLimits.from_dict(data.get("limits", {}))
        action_plans = tuple(
            ActionPlan.from_dict(item) for item in data.get("action_plans", ())
        )
        plan_ids = [plan.plan_id for plan in action_plans]
        if len(plan_ids) != len(set(plan_ids)):
            raise ValueError("Action plan ids must be unique within a path")
        unknown_trigger_steps = [
            plan.plan_id
            for plan in action_plans
            if plan.trigger_step_id not in step_ids
        ]
        if unknown_trigger_steps:
            raise ValueError(
                "Action plans reference unknown trigger steps: "
                + ", ".join(unknown_trigger_steps)
            )

        partial = cls(
            path_id=str(data["path_id"]),
            version=int(data["version"]),
            description=str(data.get("description", "")),
            steps=steps,
            limits=limits,
            action_plans=action_plans,
            fingerprint="",
        )
        digest = hashlib.sha256(partial.canonical_bytes()).hexdigest()
        return cls(
            path_id=partial.path_id,
            version=partial.version,
            description=partial.description,
            steps=steps,
            limits=limits,
            action_plans=action_plans,
            fingerprint=digest,
        )

    def canonical_dict(self) -> dict[str, Any]:
        return {
            "path_id": self.path_id,
            "version": self.version,
            "description": self.description,
            "steps": [step.canonical_dict() for step in self.steps],
            "limits": self.limits.canonical_dict(),
            "action_plans": [plan.canonical_dict() for plan in self.action_plans],
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
        holding_by_id = {holding.id: holding for holding in holdings}
        currencies = {holding.currency for holding in holdings}
        for step in self.steps:
            currencies.update(step.scenario.fx_rates)
        for step in self.steps:
            for trade in step.trades:
                if trade.holding_id not in holding_ids:
                    raise ValueError(
                        f"Path {self.path_id} step {step.step_id} trades unknown "
                        f"holding {trade.holding_id}"
                    )
        for plan in self.action_plans:
            for order in plan.orders:
                if order.order_type == "sell":
                    if order.holding_id not in holding_ids:
                        raise ValueError(
                            f"Action plan {plan.plan_id} sells unknown holding "
                            f"{order.holding_id}"
                        )
                    if holding_by_id[order.holding_id].asset_type == "cash":
                        raise ValueError(
                            f"Action plan {plan.plan_id} cannot sell cash holding "
                            f"{order.holding_id}"
                        )
                elif order.order_type == "fx_conversion":
                    currencies.update(
                        currency
                        for currency in (order.from_currency, order.to_currency)
                        if currency is not None
                    )
        unknown_currency_limits = sorted(
            set(self.limits.currency_exposure) - currencies
        )
        if unknown_currency_limits:
            raise ValueError(
                "Currency limits reference unknown currencies: "
                + ", ".join(unknown_currency_limits)
            )


def _number_map(value: Any, field: str) -> dict[str, float]:
    if not isinstance(value, dict):
        raise ValueError(f"{field} must be an object")
    return {str(key): float(number) for key, number in value.items()}


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
