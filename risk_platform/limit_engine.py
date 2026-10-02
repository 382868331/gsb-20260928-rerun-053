from __future__ import annotations

from dataclasses import dataclass, replace

from risk_platform.disposal import FX_CONVERT_ACTION, SELL_ACTION, DisposalPlan
from risk_platform.holdings import Holding
from risk_platform.limits import (
    LimitEvaluation,
    PositionSnapshot,
    RiskLimit,
    evaluate_limits,
)
from risk_platform.path_engine import PathEngine
from risk_platform.paths import StressPath
from risk_platform.pricing import Pricer
from risk_platform.results import (
    DisposalReplayResult,
    LimitBreachRecord,
    LimitPathResult,
    LimitRunResult,
    LimitStepResult,
    TriggerRecord,
)
from risk_platform.scenarios import MarketScenario, MissingMarketDataError

NO_ACTION_PLAN_ID = "no_action"
NO_ACTION_PLAN_VERSION = 0


@dataclass(frozen=True, slots=True)
class LimitPathEngine:
    """Replays stress paths under portfolio limits and disposal plans.

    Every step evaluates every limit independently: one metric returning to
    its range never clears another metric's breach. The no_action baseline
    is always replayed next to each disposal plan so risk, fees and cash
    flows can be compared. A plan fires once, at the first step where its
    trigger limit is breached, using only that step's market data; later
    steps are never consulted for the disposal decision. Disposal proceeds
    are valued at the trigger step's market, so the disposal effect and the
    fee effect are disjoint from the market effect and no trade is counted
    twice. If a step lacks market data, confirmed breaches are kept, the
    remaining limits are reported as unconfirmed, and the path is marked
    partial instead of being declared safe.
    """

    pricers: dict[str, Pricer]

    @classmethod
    def default(cls) -> "LimitPathEngine":
        return cls(PathEngine.default().pricers)

    def run(
        self,
        run_id: str,
        holdings: list[Holding],
        paths: list[StressPath],
        limits: list[RiskLimit],
        plans: list[DisposalPlan],
    ) -> LimitRunResult:
        if not limits:
            raise ValueError("At least one limit is required")
        limit_ids = {limit.limit_id for limit in limits}
        holding_ids = {holding.id for holding in holdings}
        for plan in plans:
            if plan.trigger_limit_id not in limit_ids:
                raise ValueError(
                    f"Plan {plan.plan_id} triggers on unknown limit "
                    f"{plan.trigger_limit_id}"
                )
            for action in plan.actions:
                if action.action == SELL_ACTION and action.holding_id not in holding_ids:
                    raise ValueError(
                        f"Plan {plan.plan_id} sells unknown holding {action.holding_id}"
                    )
        for path in paths:
            path.validate_holdings(holdings)

        results = tuple(
            self._run_path(holdings, path, limits, plans) for path in paths
        )
        return LimitRunResult(run_id=run_id, results=results)

    def _run_path(
        self,
        holdings: list[Holding],
        path: StressPath,
        limits: list[RiskLimit],
        plans: list[DisposalPlan],
    ) -> LimitPathResult:
        baseline, missing = self._replay(holdings, path, limits, None)
        replays = [baseline]
        for plan in plans:
            replay, plan_missing = self._replay(holdings, path, limits, plan)
            replays.append(replay)
            if missing is None and plan_missing is not None:
                missing = plan_missing

        first_breaches: list[TriggerRecord] = []
        seen: set[str] = set()
        for step in baseline.steps:
            for breach in step.breaches:
                if breach.limit_id in seen:
                    continue
                seen.add(breach.limit_id)
                first_breaches.append(
                    TriggerRecord(
                        plan_id=NO_ACTION_PLAN_ID,
                        plan_version=NO_ACTION_PLAN_VERSION,
                        limit_id=breach.limit_id,
                        step_id=step.step_id,
                        scenario_id=step.scenario_id,
                        scenario_version=step.scenario_version,
                        scenario_fingerprint=step.scenario_fingerprint,
                        observed=breach.observed,
                        threshold=breach.threshold,
                        sources=breach.sources,
                    )
                )

        status = "complete" if all(r.complete for r in replays) else "partial"
        return LimitPathResult(
            path_id=path.path_id,
            path_version=path.version,
            path_fingerprint=path.fingerprint,
            status=status,
            missing_data=missing,
            first_breaches=tuple(first_breaches),
            unconfirmed_limits=baseline.unconfirmed_limits,
            plans=tuple(replays),
        )

    def _replay(
        self,
        holdings: list[Holding],
        path: StressPath,
        limits: list[RiskLimit],
        plan: DisposalPlan | None,
    ) -> tuple[DisposalReplayResult, str | None]:
        quantities = {holding.id: holding.quantity for holding in holdings}
        cash: dict[str, float] = {}
        steps: list[LimitStepResult] = []
        missing: str | None = None
        unconfirmed: tuple[str, ...] = ()
        applied = False
        trigger: TriggerRecord | None = None
        total_fees = 0.0

        initial_step = path.steps[0]
        try:
            initial_positions = self._positions(holdings, quantities, cash, initial_step.scenario)
            initial_value = sum(item.base_value for item in initial_positions)
            initial_evaluation = evaluate_limits(
                limits, initial_positions, initial_value, initial_value, initial_positions
            )
        except MissingMarketDataError as error:
            missing = str(error)
            unconfirmed = tuple(limit.limit_id for limit in limits)
            return (
                DisposalReplayResult(
                    plan_id=plan.plan_id if plan else NO_ACTION_PLAN_ID,
                    plan_version=plan.version if plan else NO_ACTION_PLAN_VERSION,
                    plan_fingerprint=plan.fingerprint if plan else "",
                    applied=False,
                    trigger=None,
                    complete=False,
                    initial_value=0.0,
                    final_value=0.0,
                    total_market_effect=0.0,
                    total_cash_flow_effect=0.0,
                    total_trade_effect=0.0,
                    total_disposal_effect=0.0,
                    total_fee_effect=0.0,
                    total_change=0.0,
                    total_fees=0.0,
                    unconfirmed_limits=unconfirmed,
                    steps=(),
                ),
                missing,
            )

        initial_report = LimitStepResult(
            step_id=initial_step.step_id,
            scenario_id=initial_step.scenario.scenario_id,
            scenario_version=initial_step.scenario.version,
            scenario_fingerprint=initial_step.scenario.fingerprint,
            market_effect=0.0,
            cash_flow_effect=0.0,
            trade_effect=0.0,
            disposal_effect=0.0,
            fee_effect=0.0,
            total_change=0.0,
            portfolio_value=initial_value,
            breaches=_breach_records(initial_evaluation),
        )

        previous_total = initial_value
        for step in path.steps[1:]:
            scenario = step.scenario
            try:
                market_value = self._total(holdings, quantities, cash, scenario)
                cash_flow_effect = 0.0
                new_cash = dict(cash)
                for flow in step.cash_flows:
                    cash_flow_effect += flow.amount * self._fx(scenario, flow.currency)
                    new_cash[flow.currency] = new_cash.get(flow.currency, 0.0) + flow.amount

                scheduled_quantities = dict(quantities)
                for trade in step.trades:
                    updated = scheduled_quantities[trade.holding_id] + trade.quantity_delta
                    if updated < 0:
                        raise ValueError(
                            f"Trade in step {step.step_id} would make holding "
                            f"{trade.holding_id} negative"
                        )
                    scheduled_quantities[trade.holding_id] = updated
                pre_value = market_value + cash_flow_effect
                scheduled_value = self._total(
                    holdings, scheduled_quantities, new_cash, scenario
                )
                trade_effect = scheduled_value - pre_value
                pre_positions = self._positions(
                    holdings, scheduled_quantities, new_cash, scenario
                )
                evaluation = evaluate_limits(
                    limits, pre_positions, scheduled_value, initial_value, initial_positions
                )

                disposal_effect = 0.0
                fee_effect = 0.0
                new_quantities = dict(scheduled_quantities)
                if plan is not None and not applied:
                    breach = evaluation.breach_for(plan.trigger_limit_id)
                    if breach is not None:
                        new_quantities, new_cash, fees = self._apply_plan(
                            holdings, plan, new_quantities, new_cash, scenario, step.step_id
                        )
                        total_fees += fees
                        fee_effect = -fees
                        after_value = self._total(holdings, new_quantities, new_cash, scenario)
                        disposal_effect = after_value - scheduled_value - fee_effect
                        applied = True
                        trigger = TriggerRecord(
                            plan_id=plan.plan_id,
                            plan_version=plan.version,
                            limit_id=breach.limit_id,
                            step_id=step.step_id,
                            scenario_id=scenario.scenario_id,
                            scenario_version=scenario.version,
                            scenario_fingerprint=scenario.fingerprint,
                            observed=breach.observed,
                            threshold=breach.threshold,
                            sources=breach.sources,
                        )
                        post_positions = self._positions(
                            holdings, new_quantities, new_cash, scenario
                        )
                        post_value = scheduled_value + disposal_effect + fee_effect
                        evaluation = evaluate_limits(
                            limits,
                            post_positions,
                            post_value,
                            initial_value,
                            initial_positions,
                        )

                total_change = (
                    (market_value - previous_total)
                    + cash_flow_effect
                    + trade_effect
                    + disposal_effect
                    + fee_effect
                )
                portfolio_value = previous_total + total_change
                steps.append(
                    LimitStepResult(
                        step_id=step.step_id,
                        scenario_id=scenario.scenario_id,
                        scenario_version=scenario.version,
                        scenario_fingerprint=scenario.fingerprint,
                        market_effect=market_value - previous_total,
                        cash_flow_effect=cash_flow_effect,
                        trade_effect=trade_effect,
                        disposal_effect=disposal_effect,
                        fee_effect=fee_effect,
                        total_change=total_change,
                        portfolio_value=portfolio_value,
                        breaches=_breach_records(evaluation),
                    )
                )
                quantities = new_quantities
                cash = new_cash
                previous_total = portfolio_value
            except MissingMarketDataError as error:
                missing = str(error)
                unconfirmed = tuple(limit.limit_id for limit in limits)
                break

        all_steps = [initial_report] + steps
        return (
            DisposalReplayResult(
                plan_id=plan.plan_id if plan else NO_ACTION_PLAN_ID,
                plan_version=plan.version if plan else NO_ACTION_PLAN_VERSION,
                plan_fingerprint=plan.fingerprint if plan else "",
                applied=applied,
                trigger=trigger,
                complete=missing is None,
                initial_value=initial_value,
                final_value=previous_total,
                total_market_effect=sum(s.market_effect for s in all_steps),
                total_cash_flow_effect=sum(s.cash_flow_effect for s in all_steps),
                total_trade_effect=sum(s.trade_effect for s in all_steps),
                total_disposal_effect=sum(s.disposal_effect for s in all_steps),
                total_fee_effect=sum(s.fee_effect for s in all_steps),
                total_change=sum(s.total_change for s in all_steps),
                total_fees=total_fees,
                unconfirmed_limits=unconfirmed,
                steps=tuple(all_steps),
            ),
            missing,
        )

    def _apply_plan(
        self,
        holdings: list[Holding],
        plan: DisposalPlan,
        quantities: dict[str, float],
        cash: dict[str, float],
        scenario: MarketScenario,
        step_id: str,
    ) -> tuple[dict[str, float], dict[str, float], float]:
        by_id = {holding.id: holding for holding in holdings}
        new_quantities = dict(quantities)
        new_cash = dict(cash)
        fees_base = 0.0
        for action in plan.actions:
            if action.action == SELL_ACTION:
                holding = by_id[action.holding_id]
                current = new_quantities[holding.id]
                sold = current * action.fraction
                pricer = self.pricers[holding.asset_type]
                sized = replace(holding, quantity=sold)
                try:
                    proceeds_local = pricer.price_in_currency(sized, scenario)
                except KeyError as error:
                    raise MissingMarketDataError(
                        f"Scenario {scenario.scenario_id} v{scenario.version} lacks "
                        f"market data for {holding.id}: {error}"
                    ) from error
                fee_local = proceeds_local * action.fee_rate
                fx_rate = self._fx(scenario, holding.currency)
                fees_base += fee_local * fx_rate
                new_quantities[holding.id] = current - sold
                new_cash[holding.currency] = (
                    new_cash.get(holding.currency, 0.0) + proceeds_local - fee_local
                )
            elif action.action == FX_CONVERT_ACTION:
                from_currency = action.from_currency
                to_currency = action.to_currency
                fee_local = action.amount * action.fee_rate
                needed = action.amount + fee_local
                cash_holdings = [
                    holding
                    for holding in holdings
                    if holding.asset_type == "cash" and holding.currency == from_currency
                ]
                available = new_cash.get(from_currency, 0.0) + sum(
                    new_quantities[holding.id] for holding in cash_holdings
                )
                if available < needed - 1e-12:
                    raise ValueError(
                        f"Plan {plan.plan_id} cannot convert {action.amount} "
                        f"{from_currency} at step {step_id}: only {available} "
                        f"{from_currency} cash available"
                    )
                remaining = needed
                pooled = new_cash.get(from_currency, 0.0)
                take = min(pooled, remaining)
                if take:
                    new_cash[from_currency] = pooled - take
                    remaining -= take
                for holding in cash_holdings:
                    if remaining <= 1e-12:
                        break
                    take = min(new_quantities[holding.id], remaining)
                    new_quantities[holding.id] -= take
                    remaining -= take
                to_fx = self._fx(scenario, to_currency)
                from_fx = self._fx(scenario, from_currency)
                fees_base += fee_local * from_fx
                new_cash[to_currency] = (
                    new_cash.get(to_currency, 0.0)
                    + action.amount * from_fx / to_fx
                )
            else:  # pragma: no cover - guarded at parse time
                raise ValueError(f"Unsupported disposal action: {action.action}")
        return new_quantities, new_cash, fees_base

    def _positions(
        self,
        holdings: list[Holding],
        quantities: dict[str, float],
        cash: dict[str, float],
        scenario: MarketScenario,
    ) -> tuple[PositionSnapshot, ...]:
        positions: list[PositionSnapshot] = []
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
            positions.append(
                PositionSnapshot(
                    holding_id=holding.id,
                    currency=holding.currency,
                    base_value=local_value * self._fx(scenario, holding.currency),
                )
            )
        for currency, amount in cash.items():
            positions.append(
                PositionSnapshot(
                    holding_id=f"cash:{currency}",
                    currency=currency,
                    base_value=amount * self._fx(scenario, currency),
                )
            )
        return tuple(positions)

    def _total(
        self,
        holdings: list[Holding],
        quantities: dict[str, float],
        cash: dict[str, float],
        scenario: MarketScenario,
    ) -> float:
        return sum(
            item.base_value
            for item in self._positions(holdings, quantities, cash, scenario)
        )

    def _fx(self, scenario: MarketScenario, currency: str) -> float:
        try:
            return scenario.fx_rate(currency)
        except KeyError as error:
            raise MissingMarketDataError(
                f"Scenario {scenario.scenario_id} v{scenario.version} lacks "
                f"fx_rate:{currency}"
            ) from error


def _breach_records(evaluation: LimitEvaluation) -> tuple[LimitBreachRecord, ...]:
    return tuple(
        LimitBreachRecord(
            limit_id=breach.limit_id,
            metric=breach.metric,
            observed=breach.observed,
            threshold=breach.threshold,
            sources=breach.sources,
        )
        for breach in evaluation.breached
    )
