from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

SELL_ACTION = "sell"
FX_CONVERT_ACTION = "fx_convert"
SUPPORTED_ACTIONS = (SELL_ACTION, FX_CONVERT_ACTION)


@dataclass(frozen=True, slots=True)
class DisposalAction:
    """One de-risking or FX-conversion instruction executed at the trigger step.

    action="sell": sell `fraction` (0-1] of `holding_id`, proceeds are kept as
        cash in the holding's currency.
    action="fx_convert": convert `amount` of cash from `from_currency` into
        `to_currency` at the trigger step's FX rate.
    Every action charges `fee_rate` times the transacted base-currency value;
    fees are paid in cash of the transacted currency.
    """

    action: str
    fee_rate: float
    holding_id: str | None = None
    fraction: float | None = None
    from_currency: str | None = None
    to_currency: str | None = None
    amount: float | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "DisposalAction":
        if "action" not in data:
            raise ValueError("Disposal action requires action")
        action = str(data["action"])
        if action not in SUPPORTED_ACTIONS:
            raise ValueError(f"Unsupported disposal action: {action}")
        fee_rate = float(data.get("fee_rate", 0.0))
        if fee_rate < 0:
            raise ValueError("fee_rate must not be negative")
        if action == SELL_ACTION:
            if "holding_id" not in data or "fraction" not in data:
                raise ValueError("sell action requires holding_id and fraction")
            fraction = float(data["fraction"])
            if not 0 < fraction <= 1:
                raise ValueError("sell fraction must be in (0, 1]")
            return cls(
                action=action,
                fee_rate=fee_rate,
                holding_id=str(data["holding_id"]),
                fraction=fraction,
            )
        for field in ("from_currency", "to_currency", "amount"):
            if field not in data:
                raise ValueError(f"fx_convert action requires {field}")
        amount = float(data["amount"])
        if amount <= 0:
            raise ValueError("fx_convert amount must be positive")
        from_currency = str(data["from_currency"]).upper()
        to_currency = str(data["to_currency"]).upper()
        if from_currency == to_currency:
            raise ValueError("fx_convert currencies must differ")
        return cls(
            action=action,
            fee_rate=fee_rate,
            from_currency=from_currency,
            to_currency=to_currency,
            amount=amount,
        )

    def canonical_dict(self) -> dict[str, Any]:
        return {
            "action": self.action,
            "fee_rate": self.fee_rate,
            "holding_id": self.holding_id,
            "fraction": self.fraction,
            "from_currency": self.from_currency,
            "to_currency": self.to_currency,
            "amount": self.amount,
        }


@dataclass(frozen=True, slots=True)
class DisposalPlan:
    """A versioned disposal plan triggered by one limit breach.

    The plan fires once, at the first step where `trigger_limit_id` is
    breached, using only that step's market data. Plans are versioned so a
    revised plan can be replayed side-by-side with its history; saved runs
    keep the original trigger evidence and plan fingerprint queryable.
    """

    plan_id: str
    version: int
    trigger_limit_id: str
    actions: tuple[DisposalAction, ...]
    description: str
    fingerprint: str

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "DisposalPlan":
        required = ("plan_id", "version", "trigger_limit_id", "actions")
        missing = [key for key in required if key not in data]
        if missing:
            raise ValueError(f"Disposal plan is missing fields: {', '.join(missing)}")
        actions = tuple(DisposalAction.from_dict(item) for item in data["actions"])
        if not actions:
            raise ValueError("Disposal plan needs at least one action")
        partial = cls(
            plan_id=str(data["plan_id"]),
            version=int(data["version"]),
            trigger_limit_id=str(data["trigger_limit_id"]),
            actions=actions,
            description=str(data.get("description", "")),
            fingerprint="",
        )
        digest = hashlib.sha256(partial.canonical_bytes()).hexdigest()
        return cls(
            plan_id=partial.plan_id,
            version=partial.version,
            trigger_limit_id=partial.trigger_limit_id,
            actions=actions,
            description=partial.description,
            fingerprint=digest,
        )

    def canonical_dict(self) -> dict[str, Any]:
        return {
            "plan_id": self.plan_id,
            "version": self.version,
            "trigger_limit_id": self.trigger_limit_id,
            "description": self.description,
            "actions": [action.canonical_dict() for action in self.actions],
        }

    def canonical_bytes(self) -> bytes:
        return json.dumps(
            self.canonical_dict(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")


def load_disposal_plans(path: str | Path) -> list[DisposalPlan]:
    with Path(path).open("r", encoding="utf-8") as handle:
        payload = json.load(handle)
    raw_plans = payload.get("plans", payload)
    if not isinstance(raw_plans, list) or not raw_plans:
        raise ValueError("At least one disposal plan is required")
    plans = [DisposalPlan.from_dict(item) for item in raw_plans]
    keys = [(plan.plan_id, plan.version) for plan in plans]
    if len(keys) != len(set(keys)):
        raise ValueError("(plan_id, version) pairs must be unique")
    return plans
