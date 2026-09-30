from __future__ import annotations

from dataclasses import replace

from risk_platform.holdings import Holding
from risk_platform.pricing import Pricer
from risk_platform.scenarios import MarketScenario

FACTOR_FIELDS: dict[str, str] = {
    "price": "equity_prices",
    "rates": "bond_yields",
    "fx": "fx_rates",
}


def factors_for(holding: Holding) -> tuple[str, ...]:
    factors: list[str] = []
    if holding.asset_type == "equity":
        factors.append("price")
    elif holding.asset_type == "zero_coupon_bond":
        factors.append("rates")
    factors.append("fx")
    return tuple(factors)


def attribute_holding(
    holding: Holding,
    pricer: Pricer,
    base: MarketScenario,
    stressed: MarketScenario,
) -> tuple[tuple[tuple[str, ...], float], ...]:
    """Exact factor decomposition of one holding's PnL.

    For every non-empty subset S of the holding's driving factors, the effect
    of S is the inclusion-exclusion sum over subsets T of S of the holding's
    value when exactly the factors in T are stressed. Singletons are main
    effects, larger subsets are interaction terms, and the effects sum
    exactly to the holding's PnL without any ad-hoc allocation.
    """
    factors = factors_for(holding)

    def value_with(stressed_factors: frozenset) -> float:
        overrides = {
            field: getattr(stressed, field)
            for factor, field in FACTOR_FIELDS.items()
            if factor in stressed_factors
        }
        mixed = replace(base, **overrides)
        return pricer.price_in_currency(holding, mixed) * mixed.fx_rate(holding.currency)

    values: dict[frozenset, float] = {}
    for mask in range(1 << len(factors)):
        subset = frozenset(
            factor for index, factor in enumerate(factors) if mask & (1 << index)
        )
        values[subset] = value_with(subset)

    effects: list[tuple[tuple[str, ...], float]] = []
    for size in range(1, len(factors) + 1):
        for mask in range(1 << len(factors)):
            subset = frozenset(
                factor for index, factor in enumerate(factors) if mask & (1 << index)
            )
            if len(subset) != size:
                continue
            effect = 0.0
            for inner, inner_value in values.items():
                if inner <= subset:
                    sign = 1.0 if (size - len(inner)) % 2 == 0 else -1.0
                    effect += sign * inner_value
            effects.append((tuple(sorted(subset)), effect))
    return tuple(effects)


def aggregate_effects(
    effects: list[tuple[tuple[str, ...], float]],
) -> dict[tuple[str, ...], float]:
    totals: dict[tuple[str, ...], float] = {}
    for factors, effect in effects:
        totals[factors] = totals.get(factors, 0.0) + effect
    return totals
