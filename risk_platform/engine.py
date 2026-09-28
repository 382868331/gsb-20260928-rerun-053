from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from decimal import Decimal

from .errors import MissingMarketDataError
from .models import (
    Holding,
    HoldingValuation,
    PnlContribution,
    ScenarioSnapshot,
    StressResult,
    ValuationResult,
)
from .pricing import PRICING_MODELS


def scenario_hash(scenario: ScenarioSnapshot) -> str:
    payload = json.dumps(
        scenario.to_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def fx_rate(holding: Holding, scenario: ScenarioSnapshot) -> Decimal:
    if holding.currency == scenario.base_currency:
        rate = scenario.fx_rates_to_base.get(holding.currency, Decimal("1"))
        if rate != Decimal("1"):
            raise MissingMarketDataError(
                f"base currency {scenario.base_currency} FX must be exactly 1"
            )
        return rate
    rate = scenario.fx_rates_to_base.get(holding.currency)
    if rate is None:
        raise MissingMarketDataError(
            f"missing {scenario.scenario_id} v{scenario.version} FX rate "
            f"{holding.currency}/{scenario.base_currency}"
        )
    return rate


def value_holding(
    holding: Holding, scenario: ScenarioSnapshot
) -> HoldingValuation:
    model = PRICING_MODELS.get(holding.asset_type)
    if model is None:
        raise MissingMarketDataError(
            f"no pricing model for asset type {holding.asset_type}"
        )
    local_price = model(holding, scenario)
    rate = fx_rate(holding, scenario)
    return HoldingValuation(
        holding_id=holding.holding_id,
        symbol=holding.symbol,
        quantity=holding.quantity,
        currency=holding.currency,
        local_price=local_price,
        fx_rate_to_base=rate,
        market_value_base=holding.quantity * local_price * rate,
    )


def value_portfolio(
    holdings: Iterable[Holding],
    scenario: ScenarioSnapshot,
    run_id: str,
) -> ValuationResult:
    """Revalue every holding from one scenario, failing atomically on gaps."""

    materialized = tuple(holdings)
    if not materialized:
        raise ValueError("portfolio must contain at least one holding")
    valued = tuple(value_holding(h, scenario) for h in materialized)
    return ValuationResult(
        run_id=run_id,
        scenario_id=scenario.scenario_id,
        scenario_version=scenario.version,
        scenario_as_of=scenario.as_of,
        scenario_hash=scenario_hash(scenario),
        base_currency=scenario.base_currency,
        holdings=valued,
        total_value_base=sum(
            (item.market_value_base for item in valued), Decimal("0")
        ),
    )


def stress_pnl(
    baseline: ValuationResult,
    stressed: ValuationResult,
    run_id: str,
) -> StressResult:
    if baseline.scenario_hash == stressed.scenario_hash:
        raise ValueError("stressed result must use a different scenario snapshot")
    baseline_map = baseline.holding_map()
    stressed_map = stressed.holding_map()
    if set(baseline_map) != set(stressed_map):
        raise ValueError("baseline and stressed holdings must match")

    contributions = tuple(
        PnlContribution(
            holding_id=holding_id,
            symbol=baseline_map[holding_id].symbol,
            baseline_value_base=baseline_map[holding_id].market_value_base,
            scenario_value_base=stressed_map[holding_id].market_value_base,
            pnl_base=stressed_map[holding_id].market_value_base
            - baseline_map[holding_id].market_value_base,
        )
        for holding_id in sorted(baseline_map)
    )
    return StressResult(
        run_id=run_id,
        scenario_id=stressed.scenario_id,
        scenario_version=stressed.scenario_version,
        baseline_run_id=baseline.run_id,
        contributions=contributions,
        total_pnl_base=sum((x.pnl_base for x in contributions), Decimal("0")),
    )
