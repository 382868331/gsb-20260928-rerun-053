from __future__ import annotations

from collections.abc import Callable, Sequence
from decimal import Decimal

from .errors import MissingMarketDataError
from .models import Holding, ScenarioSnapshot


PriceFn = Callable[[Holding, ScenarioSnapshot], Decimal]


def spot_price(holding: Holding, scenario: ScenarioSnapshot) -> Decimal:
    price = scenario.prices.get((holding.symbol, holding.currency))
    if price is None:
        raise MissingMarketDataError(
            f"missing {scenario.scenario_id} v{scenario.version} price for "
            f"{holding.symbol} in {holding.currency}"
        )
    return price


PRICING_MODELS: dict[str, PriceFn] = {
    "equity": spot_price,
    "bond": spot_price,
    "fund": spot_price,
}


def register_pricing_model(asset_type: str, model: PriceFn) -> None:
    PRICING_MODELS[asset_type] = model


def supported_asset_types() -> Sequence[str]:
    return tuple(sorted(PRICING_MODELS))
