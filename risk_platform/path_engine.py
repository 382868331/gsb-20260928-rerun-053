from __future__ import annotations

from dataclasses import dataclass, replace

from risk_platform.holdings import Holding
from risk_platform.paths import StressPath, PathStep
from risk_platform.pricing import CashPricer, EquityPricer, Pricer, ZeroCouponBondPricer
from risk_platform.results import (
    Contribution,
    PathRunResult,
    PathScenarioResult,
    PathStepResult,
    PolicyPathResult,
)
from risk_platform.scenarios import MarketScenario, MissingMarketDataError

HOLD_POLICY = "hold"
REBALANCE_POLICY = "rebalance"
DEFAULT_POLICIES = (HOLD_POLICY, REBALANCE_POLICY)


@dataclass(frozen=True, slots=True)
class PathEngine:
    """Replays ordered stress paths step by step.

    Each step decomposes the portfolio change into a market effect (previous
    quantities, new market), a trade effect (rebalancing valued at the
    previous market), their interaction, and cash flows received during the
    step. The four components sum exactly to the step change, and the step
    changes sum exactly to the initial-to-final difference. If a step lacks
    market data, the replay stops, keeps every completed step, and marks the
    path result as partial instead of publishing a completed-path total.
    """

    pricers: dict[str, Pricer]

    @classmethod
    def default(cls) -> "PathEngine":
        pricers = (CashPricer(), EquityPricer(), ZeroCouponBondPricer())
        return cls({pricer.asset_type: pricer for pricer in pricers})

    def run(
        self,
        run_id: str,
        holdings: list[Holding],
        paths: list[StressPath],
        policies: tuple[str, ...] = DEFAULT_POLICIES,
    ) -> PathRunResult:
        for policy in policies:
            if policy not in DEFAULT_POLICIES:
                raise ValueError(f"Unknown policy: {policy}")
        for path in paths:
            path.validate_holdings(holdings)

        results = tuple(
            self._run_path(holdings, path, policies) for path in paths
        )
        return PathRunResult(run_id=run_id, results=results)

    def _run_path(
        self,
        holdings: list[Holding],
        path: StressPath,
        policies: tuple[str, ...],
    ) -> PathScenarioResult:
        policy_results: list[PolicyPathResult] = []
        missing_data: str | None = None
        for policy in policies:
            result, missing = self._replay(holdings, path, policy)
            policy_results.append(result)
            if missing is not None and missing_data is None:
                missing_data = missing
        status = (
            "complete"
            if all(result.complete for result in policy_results)
            else "partial"
        )
        return PathScenarioResult(
            path_id=path.path_id,
            path_version=path.version,
            path_fingerprint=path.fingerprint,
            status=status,
            missing_data=missing_data,
            policies=tuple(policy_results),
        )

    def _replay(
        self,
        holdings: list[Holding],
        path: StressPath,
        policy: str,
    ) -> tuple[PolicyPathResult, str | None]:
        quantities = {holding.id: holding.quantity for holding in holdings}
        cash: dict[str, float] = {}
        steps: list[PathStepResult] = []
        missing: str | None = None

        initial_step = path.steps[0]
        try:
            previous_total = self._total(holdings, quantities, cash, initial_step.scenario)
            initial_value = previous_total
        except MissingMarketDataError as error:
            missing = str(error)
            previous_total = 0.0
            initial_value = 0.0

        completed_steps: list[PathStep] = []
        if missing is None:
            completed_steps.append(initial_step)
            previous_scenario = initial_step.scenario
            for step in path.steps[1:]:
                scenario = step.scenario
                try:
                    result = self._step(
                        holdings,
                        quantities,
                        cash,
                        previous_scenario,
                        scenario,
                        step,
                        policy,
                        previous_total,
                    )
                except MissingMarketDataError as error:
                    missing = str(error)
                    break
                (
                    step_result,
                    quantities,
                    cash,
                    previous_total,
                ) = result
                steps.append(step_result)
                previous_scenario = scenario
                completed_steps.append(step)

        final_value = previous_total
        return (
            PolicyPathResult(
                policy=policy,
                complete=missing is None,
                initial_value=initial_value,
                final_value=final_value,
                total_market_effect=sum(step.market_effect for step in steps),
                total_trade_effect=sum(step.trade_effect for step in steps),
                total_interaction_effect=sum(step.interaction_effect for step in steps),
                total_cash_flow_effect=sum(step.cash_flow_effect for step in steps),
                total_change=sum(step.total_change for step in steps),
                steps=tuple(steps),
            ),
            missing,
        )

    def _step(
        self,
        holdings: list[Holding],
        quantities: dict[str, float],
        cash: dict[str, float],
        previous_scenario: MarketScenario,
        scenario: MarketScenario,
        step: PathStep,
        policy: str,
        previous_total: float,
    ) -> tuple[PathStepResult, dict[str, float], dict[str, float], float]:
        market_value = self._total(holdings, quantities, cash, scenario)
        market_effect = market_value - previous_total

        cash_flow_effect = 0.0
        new_cash = dict(cash)
        for flow in step.cash_flows:
            cash_flow_effect += flow.amount * self._fx(scenario, flow.currency)
            new_cash[flow.currency] = new_cash.get(flow.currency, 0.0) + flow.amount

        new_quantities = dict(quantities)
        if policy == REBALANCE_POLICY:
            for trade in step.trades:
                updated = new_quantities[trade.holding_id] + trade.quantity_delta
                if updated < 0:
                    raise ValueError(
                        f"Trade in step {step.step_id} would make holding "
                        f"{trade.holding_id} negative"
                    )
                new_quantities[trade.holding_id] = updated

        trade_value_old_market = self._total(
            holdings, new_quantities, cash, previous_scenario
        )
        trade_effect = trade_value_old_market - previous_total
        new_value = self._total(holdings, new_quantities, cash, scenario)
        interaction_effect = new_value - market_value - trade_effect

        total_change = market_effect + trade_effect + interaction_effect + cash_flow_effect
        portfolio_value = previous_total + total_change

        contributions = self._contributions(
            holdings,
            quantities,
            cash,
            new_quantities,
            new_cash,
            scenario,
            previous_scenario,
        )
        return (
            PathStepResult(
                step_id=step.step_id,
                scenario_id=scenario.scenario_id,
                scenario_version=scenario.version,
                scenario_fingerprint=scenario.fingerprint,
                market_effect=market_effect,
                trade_effect=trade_effect,
                interaction_effect=interaction_effect,
                cash_flow_effect=cash_flow_effect,
                total_change=total_change,
                portfolio_value=portfolio_value,
                contributions=contributions,
            ),
            new_quantities,
            new_cash,
            portfolio_value,
        )

    def _fx(self, scenario: MarketScenario, currency: str) -> float:
        try:
            return scenario.fx_rate(currency)
        except KeyError as error:
            raise MissingMarketDataError(
                f"Scenario {scenario.scenario_id} v{scenario.version} lacks "
                f"fx_rate:{currency}"
            ) from error

    def _total(
        self,
        holdings: list[Holding],
        quantities: dict[str, float],
        cash: dict[str, float],
        scenario: MarketScenario,
    ) -> float:
        total = 0.0
        for holding in holdings:
            pricer = self.pricers[holding.asset_type]
            sized = replace(holding, quantity=quantities[holding.id])
            try:
                local_value = pricer.price_in_currency(sized, scenario)
            except KeyError as error:
                raise MissingMarketDataError(
                    f"Scenario {scenario.scenario_id} v{scenario.version} lacks "
                    f"market data for {holding.id}: {error}"
                ) from error
            total += local_value * self._fx(scenario, holding.currency)
        for currency, amount in cash.items():
            total += amount * self._fx(scenario, currency)
        return total

    def _contributions(
        self,
        holdings: list[Holding],
        previous_quantities: dict[str, float],
        previous_cash: dict[str, float],
        quantities: dict[str, float],
        cash: dict[str, float],
        scenario: MarketScenario,
        previous_scenario: MarketScenario,
    ) -> tuple[Contribution, ...]:
        contributions: list[Contribution] = []
        for holding in holdings:
            pricer = self.pricers[holding.asset_type]
            sized = replace(holding, quantity=quantities[holding.id])
            local_value = pricer.price_in_currency(sized, scenario)
            fx_rate = self._fx(scenario, holding.currency)
            previous_sized = replace(
                holding, quantity=previous_quantities[holding.id]
            )
            previous_local = pricer.price_in_currency(previous_sized, previous_scenario)
            previous_value = previous_local * previous_scenario.fx_rate(holding.currency)
            base_value = local_value * fx_rate
            contributions.append(
                Contribution(
                    holding_id=holding.id,
                    name=holding.name,
                    asset_type=holding.asset_type,
                    currency=holding.currency,
                    local_value=local_value,
                    fx_rate=fx_rate,
                    base_value=base_value,
                    base_scenario_value=previous_value,
                    pnl_vs_base=base_value - previous_value,
                )
            )
        for currency, amount in cash.items():
            fx_rate = self._fx(scenario, currency)
            previous_amount = previous_cash.get(currency, 0.0)
            previous_value = previous_amount * previous_scenario.fx_rate(currency)
            base_value = amount * fx_rate
            contributions.append(
                Contribution(
                    holding_id=f"reinvested-cash:{currency}",
                    name=f"Reinvested cash {currency}",
                    asset_type="cash",
                    currency=currency,
                    local_value=amount,
                    fx_rate=fx_rate,
                    base_value=base_value,
                    base_scenario_value=previous_value,
                    pnl_vs_base=base_value - previous_value,
                )
            )
        return tuple(contributions)
