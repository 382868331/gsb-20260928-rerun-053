"""Exact P&L attribution to common driving factors.

Given a base scenario and a stress scenario that may move equity prices,
bond yields and FX rates simultaneously, every portfolio value change is
decomposed into single-factor contributions and interaction terms using
the inclusion-exclusion identity over the 2**3 factor corner valuations.

The decomposition is exact: the seven terms add up to V(stress) - V(base)
for every holding and for the portfolio total. Nothing is allocated
proportionally or spread to force a match.
"""
from __future__ import annotations

from dataclasses import dataclass

from risk_platform.holdings import Holding
from risk_platform.pricing import Pricer
from risk_platform.scenarios import MarketScenario

FACTOR_EFFECT_FIELDS = (
    "factor_equity_price",
    "factor_bond_yield",
    "factor_fx",
    "interaction_equity_yield",
    "interaction_equity_fx",
    "interaction_yield_fx",
    "interaction_all",
)

# factor mask (equity, yield, fx) -> result field
_MASK_TO_FIELD = {
    0b100: "factor_equity_price",
    0b010: "factor_bond_yield",
    0b001: "factor_fx",
    0b110: "interaction_equity_yield",
    0b101: "interaction_equity_fx",
    0b011: "interaction_yield_fx",
    0b111: "interaction_all",
}

_ZERO_EFFECTS = {field: 0.0 for field in FACTOR_EFFECT_FIELDS}


@dataclass(frozen=True, slots=True)
class FactorAttribution:
    holding_id: str
    effects: dict[str, float]

    def total_pnl(self) -> float:
        return sum(self.effects.values())


def attribute_portfolio(
    holdings: list[Holding],
    pricers: dict[str, Pricer],
    base: MarketScenario,
    stress: MarketScenario,
) -> tuple[FactorAttribution, ...]:
    """Decompose the base -> stress P&L of every holding.

    Both scenarios must already pass validate_holdings; corner scenarios
    only reuse slices of those validated inputs.
    """
    corner_values = tuple(
        _value_holdings(holdings, pricers, _mixed_scenario(base, stress, mask))
        for mask in range(8)
    )

    attributions: list[FactorAttribution] = []
    for holding in holdings:
        effects = dict(_ZERO_EFFECTS)
        for mask, field in _MASK_TO_FIELD.items():
            effects[field] = _inclusion_exclusion(mask, corner_values, holding.id)
        attributions.append(FactorAttribution(holding_id=holding.id, effects=effects))
    return tuple(attributions)


def zero_attribution(holdings: list[Holding]) -> tuple[FactorAttribution, ...]:
    return tuple(
        FactorAttribution(holding_id=holding.id, effects=dict(_ZERO_EFFECTS))
        for holding in holdings
    )


def _inclusion_exclusion(
    mask: int,
    corner_values: tuple[dict[str, float], ...],
    holding_id: str,
) -> float:
    # Sum over sub-masks with sign (-1) ** (|mask| - |sub|):
    # V(mask) - sum V(mask without one bit) + ... + sign * V(000).
    total = 0.0
    sub = mask
    while True:
        sign = 1.0 if (mask.bit_count() - sub.bit_count()) % 2 == 0 else -1.0
        total += sign * corner_values[sub][holding_id]
        if sub == 0:
            break
        sub = (sub - 1) & mask
    return total


def _value_holdings(
    holdings: list[Holding],
    pricers: dict[str, Pricer],
    scenario: MarketScenario,
) -> dict[str, float]:
    values: dict[str, float] = {}
    for holding in holdings:
        pricer = pricers[holding.asset_type]
        local_value = pricer.price_in_currency(holding, scenario)
        values[holding.id] = local_value * scenario.fx_rate(holding.currency)
    return values


def _mixed_scenario(
    base: MarketScenario,
    stress: MarketScenario,
    mask: int,
) -> MarketScenario:
    """Build a corner scenario taking each factor slice from base or stress.

    Mask bits: 0b100 equity prices, 0b010 bond yields, 0b001 FX rates. Only
    keys that actually differ between the scenarios switch to stress values,
    so an inactive factor keeps its base values exactly.
    """
    equity_keys = {
        key
        for key in stress.equity_prices
        if stress.equity_prices[key] != base.equity_prices.get(key)
    }
    yield_keys = {
        key
        for key in stress.bond_yields
        if stress.bond_yields[key] != base.bond_yields.get(key)
    }
    fx_keys = {
        key
        for key in stress.fx_rates
        if stress.fx_rates[key] != base.fx_rates.get(key)
    }

    equity_prices = dict(base.equity_prices)
    if mask & 0b100:
        equity_prices.update({key: stress.equity_prices[key] for key in equity_keys})

    bond_yields = dict(base.bond_yields)
    if mask & 0b010:
        bond_yields.update({key: stress.bond_yields[key] for key in yield_keys})

    fx_rates = dict(base.fx_rates)
    if mask & 0b001:
        fx_rates.update({key: stress.fx_rates[key] for key in fx_keys})

    return MarketScenario.from_dict(
        {
            "scenario_id": f"corner:{base.scenario_id}:{stress.scenario_id}:{mask:03b}",
            "version": stress.version,
            "description": "internal factor corner",
            "equity_prices": equity_prices,
            "bond_yields": bond_yields,
            "fx_rates": fx_rates,
        }
    )
