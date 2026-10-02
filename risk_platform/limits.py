from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

LOSS_METRIC = "loss"
CURRENCY_EXPOSURE_METRIC = "currency_exposure"
CONCENTRATION_METRIC = "concentration"
SUPPORTED_METRICS = (LOSS_METRIC, CURRENCY_EXPOSURE_METRIC, CONCENTRATION_METRIC)


@dataclass(frozen=True, slots=True)
class PositionSnapshot:
    """Base-currency value of one position at one step."""

    holding_id: str
    currency: str
    base_value: float


@dataclass(frozen=True, slots=True)
class RiskLimit:
    """One portfolio limit evaluated independently at every step.

    metric="loss": breached when initial_value - portfolio_value >= threshold.
    metric="currency_exposure": breached when the absolute base-currency
        exposure to `currency` >= threshold.
    metric="concentration": breached when the largest single position share
        of the portfolio >= threshold (a fraction between 0 and 1).
    """

    limit_id: str
    metric: str
    threshold: float
    currency: str | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "RiskLimit":
        required = ("limit_id", "metric", "threshold")
        missing = [key for key in required if key not in data]
        if missing:
            raise ValueError(f"Limit is missing fields: {', '.join(missing)}")
        metric = str(data["metric"])
        if metric not in SUPPORTED_METRICS:
            raise ValueError(f"Unsupported limit metric: {metric}")
        threshold = float(data["threshold"])
        if threshold < 0:
            raise ValueError("Limit threshold must not be negative")
        currency = data.get("currency")
        if metric == CURRENCY_EXPOSURE_METRIC:
            if not currency:
                raise ValueError("currency_exposure limit requires currency")
            currency = str(currency).upper()
        elif currency is not None:
            currency = str(currency).upper()
        if metric == CONCENTRATION_METRIC and threshold > 1:
            raise ValueError("concentration threshold is a fraction and must be <= 1")
        return cls(
            limit_id=str(data["limit_id"]),
            metric=metric,
            threshold=threshold,
            currency=currency,
        )

    def canonical_dict(self) -> dict[str, Any]:
        return {
            "limit_id": self.limit_id,
            "metric": self.metric,
            "threshold": self.threshold,
            "currency": self.currency,
        }


@dataclass(frozen=True, slots=True)
class LimitBreach:
    """One confirmed breach with the observed value and its source."""

    limit_id: str
    metric: str
    observed: float
    threshold: float
    sources: tuple[dict[str, Any], ...]


@dataclass(frozen=True, slots=True)
class LimitEvaluation:
    """Outcome of checking every limit against one portfolio state.

    Each limit is judged on its own: one metric moving back inside its
    range never clears another metric's breach.
    """

    breached: tuple[LimitBreach, ...]

    @property
    def breached_limit_ids(self) -> tuple[str, ...]:
        return tuple(breach.limit_id for breach in self.breached)

    def breach_for(self, limit_id: str) -> LimitBreach | None:
        for breach in self.breached:
            if breach.limit_id == limit_id:
                return breach
        return None


def evaluate_limits(
    limits: list[RiskLimit],
    positions: tuple[PositionSnapshot, ...],
    portfolio_value: float,
    initial_value: float,
    initial_positions: tuple[PositionSnapshot, ...],
) -> LimitEvaluation:
    breaches: list[LimitBreach] = []
    for limit in limits:
        breach = _evaluate_limit(
            limit, positions, portfolio_value, initial_value, initial_positions
        )
        if breach is not None:
            breaches.append(breach)
    return LimitEvaluation(breached=tuple(breaches))


def _evaluate_limit(
    limit: RiskLimit,
    positions: tuple[PositionSnapshot, ...],
    portfolio_value: float,
    initial_value: float,
    initial_positions: tuple[PositionSnapshot, ...],
) -> LimitBreach | None:
    if limit.metric == LOSS_METRIC:
        observed = initial_value - portfolio_value
        if observed < limit.threshold:
            return None
        initial_by_id = {item.holding_id: item.base_value for item in initial_positions}
        drops = []
        for item in positions:
            drop = initial_by_id.get(item.holding_id, 0.0) - item.base_value
            if drop > 0:
                drops.append({"holding_id": item.holding_id, "loss": drop})
        drops.sort(key=lambda entry: entry["loss"], reverse=True)
        return LimitBreach(
            limit_id=limit.limit_id,
            metric=limit.metric,
            observed=observed,
            threshold=limit.threshold,
            sources=tuple(drops[:3]),
        )

    if limit.metric == CURRENCY_EXPOSURE_METRIC:
        exposure = sum(
            item.base_value for item in positions if item.currency == limit.currency
        )
        observed = abs(exposure)
        if observed < limit.threshold:
            return None
        return LimitBreach(
            limit_id=limit.limit_id,
            metric=limit.metric,
            observed=observed,
            threshold=limit.threshold,
            sources=({"currency": limit.currency, "exposure": exposure},),
        )

    if limit.metric == CONCENTRATION_METRIC:
        if portfolio_value <= 0 or not positions:
            return None
        largest = max(positions, key=lambda item: item.base_value)
        observed = largest.base_value / portfolio_value
        if observed < limit.threshold:
            return None
        return LimitBreach(
            limit_id=limit.limit_id,
            metric=limit.metric,
            observed=observed,
            threshold=limit.threshold,
            sources=(
                {
                    "holding_id": largest.holding_id,
                    "share": observed,
                    "base_value": largest.base_value,
                },
            ),
        )

    raise ValueError(f"Unsupported limit metric: {limit.metric}")


def load_limits(path: str | Path) -> list[RiskLimit]:
    with Path(path).open("r", encoding="utf-8") as handle:
        payload = json.load(handle)
    raw_limits = payload.get("limits", payload)
    if not isinstance(raw_limits, list) or not raw_limits:
        raise ValueError("At least one limit is required")
    limits = [RiskLimit.from_dict(item) for item in raw_limits]
    ids = [limit.limit_id for limit in limits]
    if len(ids) != len(set(ids)):
        raise ValueError("Limit ids must be unique")
    return limits
