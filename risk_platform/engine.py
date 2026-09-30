from __future__ import annotations

from dataclasses import dataclass, replace

from risk_platform.attribution import FACTOR_EFFECT_FIELDS, FactorAttribution, attribute_portfolio, zero_attribution
from risk_platform.holdings import Holding
from risk_platform.pricing import CashPricer, EquityPricer, Pricer, ZeroCouponBondPricer
from risk_platform.results import Contribution, RunResult, ScenarioResult, with_relative_values
from risk_platform.scenarios import MarketScenario


@dataclass(frozen=True, slots=True)
class RevaluationEngine:
    pricers: dict[str, Pricer]

    @classmethod
    def default(cls) -> "RevaluationEngine":
        pricers = (CashPricer(), EquityPricer(), ZeroCouponBondPricer())
        return cls({pricer.asset_type: pricer for pricer in pricers})

    def run(
        self,
        run_id: str,
        holdings: list[Holding],
        scenarios: list[MarketScenario],
        base_scenario_id: str = "base",
    ) -> RunResult:
        for scenario in scenarios:
            scenario.validate_holdings(holdings)

        base_matches = [item for item in scenarios if item.scenario_id == base_scenario_id]
        if len(base_matches) != 1:
            raise ValueError(f"Expected exactly one scenario named {base_scenario_id!r}")
        base_scenario = base_matches[0]

        base_contributions = self._value_scenario(holdings, base_scenario)
        base_values = {
            contribution.holding_id: contribution.base_value
            for contribution in base_contributions
        }
        scenario_results = [
            self._to_scenario_result(
                base_scenario,
                with_relative_values(
                    self._attach_attributions(
                        base_contributions,
                        zero_attribution(holdings),
                    ),
                    base_values,
                ),
            )
        ]

        for scenario in scenarios:
            if scenario == base_scenario:
                continue
            contributions = self._value_scenario(holdings, scenario)
            attributions = attribute_portfolio(holdings, self.pricers, base_scenario, scenario)
            contributions = self._attach_attributions(contributions, attributions)
            scenario_results.append(
                self._to_scenario_result(
                    scenario,
                    with_relative_values(contributions, base_values),
                )
            )

        return RunResult(
            run_id=run_id,
            base_scenario_id=base_scenario.scenario_id,
            base_scenario_version=base_scenario.version,
            base_scenario_fingerprint=base_scenario.fingerprint,
            results=tuple(scenario_results),
        )

    def _value_scenario(
        self,
        holdings: list[Holding],
        scenario: MarketScenario,
    ) -> tuple[Contribution, ...]:
        contributions: list[Contribution] = []
        for holding in holdings:
            try:
                pricer = self.pricers[holding.asset_type]
            except KeyError as error:
                raise ValueError(f"Unsupported asset type: {holding.asset_type}") from error
            local_value = pricer.price_in_currency(holding, scenario)
            fx_rate = scenario.fx_rate(holding.currency)
            contributions.append(
                Contribution(
                    holding_id=holding.id,
                    name=holding.name,
                    asset_type=holding.asset_type,
                    currency=holding.currency,
                    local_value=local_value,
                    fx_rate=fx_rate,
                    base_value=local_value * fx_rate,
                    base_scenario_value=0.0,
                    pnl_vs_base=0.0,
                )
            )
        return tuple(contributions)

    @staticmethod
    def _attach_attributions(
        contributions: tuple[Contribution, ...],
        attributions: tuple[FactorAttribution, ...],
    ) -> tuple[Contribution, ...]:
        effects_by_holding = {item.holding_id: item.effects for item in attributions}
        return tuple(
            replace(contribution, **effects_by_holding[contribution.holding_id])
            for contribution in contributions
        )

    @staticmethod
    def _to_scenario_result(
        scenario: MarketScenario,
        contributions: tuple[Contribution, ...],
    ) -> ScenarioResult:
        factor_totals = {
            field: sum(getattr(item, field) for item in contributions)
            for field in FACTOR_EFFECT_FIELDS
        }
        result = ScenarioResult(
            scenario_id=scenario.scenario_id,
            scenario_version=scenario.version,
            scenario_fingerprint=scenario.fingerprint,
            total_base_value=sum(item.base_value for item in contributions),
            total_pnl_vs_base=sum(item.pnl_vs_base for item in contributions),
            contributions=contributions,
            **factor_totals,
        )
        attributed_pnl = sum(factor_totals.values())
        if abs(attributed_pnl - result.total_pnl_vs_base) > 1e-9:
            raise RuntimeError(
                f"Factor attribution for scenario {scenario.scenario_id} does not tie out: "
                f"{attributed_pnl} != {result.total_pnl_vs_base}"
            )
        return result
