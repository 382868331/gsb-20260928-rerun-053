from __future__ import annotations

from dataclasses import dataclass, replace

from risk_platform.holdings import Holding
from risk_platform.paths import ActionPlan, PathStep, RiskLimits, StressPath
from risk_platform.pricing import CashPricer, EquityPricer, Pricer, ZeroCouponBondPricer
from risk_platform.results import (
    ActionFill,
    ActionReconciliation,
    Contribution,
    LimitViolation,
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
        policies: tuple[str, ...] | None = None,
    ) -> PathRunResult:
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
        policies: tuple[str, ...] | None,
    ) -> PathScenarioResult:
        policy_names = self._policy_names(path, policies)
        policy_results: list[PolicyPathResult] = []
        missing_data: str | None = None
        for policy in policy_names:
            result, missing = self._replay(holdings, path, policy)
            policy_results.append(result)
            if missing is not None and missing_data is None:
                missing_data = missing
        status = (
            "complete"
            if all(result.complete for result in policy_results)
            else "partial"
        )
        first_trigger = next(
            (result.first_trigger_step_id for result in policy_results if result.first_trigger_step_id),
            None,
        )
        return PathScenarioResult(
            path_id=path.path_id,
            path_version=path.version,
            path_fingerprint=path.fingerprint,
            status=status,
            missing_data=missing_data,
            policies=tuple(policy_results),
            first_trigger_step_id=first_trigger,
            trigger_basis_fingerprint=path.fingerprint if first_trigger else None,
        )

    @staticmethod
    def _policy_names(
        path: StressPath,
        requested: tuple[str, ...] | None,
    ) -> tuple[str, ...]:
        action_names = tuple(f"action:{plan.plan_id}" for plan in path.action_plans)
        available = DEFAULT_POLICIES + action_names
        if requested is None:
            return available
        unknown = [policy for policy in requested if policy not in available]
        if unknown:
            raise ValueError(f"Unknown policy: {', '.join(unknown)}")
        return requested

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
        first_trigger: str | None = None
        first_breaches: tuple[LimitViolation, ...] = ()
        unresolved: tuple[LimitViolation, ...] = ()
        action_plan = self._action_plan(path, policy)
        action_executed = False
        external_cash_base = 0.0

        initial_step = path.steps[0]
        try:
            previous_total = self._total(holdings, quantities, cash, initial_step.scenario)
            initial_value = previous_total
        except MissingMarketDataError as error:
            missing = str(error)
            previous_total = 0.0
            initial_value = 0.0
            unresolved = self._unconfirmed_limits(path.limits)

        if missing is None:
            initial_breaches = self._check_limits(
                holdings,
                quantities,
                cash,
                initial_step.scenario,
                initial_step.scenario,
                initial_value,
                initial_value,
                0.0,
                path.limits,
            )
            initial_breached = tuple(
                item for item in initial_breaches if item.status == "breached"
            )
            if initial_breached:
                first_trigger = initial_step.step_id
                first_breaches = tuple(
                    replace(item, trigger_step_id=initial_step.step_id)
                    for item in initial_breached
                )
                unresolved = tuple(item for item in initial_breaches if item.status != "within")
            if (
                action_plan is not None
                and action_plan.trigger_step_id == initial_step.step_id
                and initial_breached
            ):
                action_result = self._execute_actions(
                    holdings,
                    quantities,
                    cash,
                    initial_step.scenario,
                    action_plan,
                    initial_value,
                )
                quantities, cash, initial_value, _ = action_result
                previous_total = initial_value
                action_executed = True
                post_initial_breaches = self._check_limits(
                    holdings,
                    quantities,
                    cash,
                    initial_step.scenario,
                    initial_step.scenario,
                    initial_value,
                    initial_value,
                    0.0,
                    path.limits,
                )
                initial_status = {
                    item.metric: item.status for item in initial_breached
                }
                post_initial_breaches = tuple(
                    replace(
                        item,
                        status=(
                            "confirmed_within"
                            if item.status == "within"
                            and initial_status.get(item.metric) == "breached"
                            else item.status
                        ),
                    )
                    for item in post_initial_breaches
                )
                unresolved = tuple(
                    item
                    for item in post_initial_breaches
                    if item.status in {"breached", "unconfirmed"}
                )
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
                        action_plan if not action_executed else None,
                        previous_total,
                        initial_value,
                        external_cash_base,
                        path.limits,
                    )
                except MissingMarketDataError as error:
                    missing = str(error)
                    unresolved = self._unconfirmed_limits(path.limits)
                    break
                step_result, quantities, cash, previous_total, breaches, executed = result
                external_cash_base += step_result.cash_flow_effect
                seen_metrics = {item.metric for item in first_breaches}
                newly_breached = [
                    item
                    for item in breaches["pre"]
                    if item.status == "breached" and item.metric not in seen_metrics
                ]
                if newly_breached:
                    first_trigger = step.step_id
                    first_breaches = first_breaches + tuple(
                        replace(item, trigger_step_id=step.step_id)
                        for item in newly_breached
                    )
                unresolved = tuple(
                    item
                    for item in breaches["post"]
                    if item.status in {"breached", "unconfirmed"}
                )
                if executed:
                    action_executed = True
                steps.append(step_result)
                previous_scenario = scenario

        final_value = previous_total
        if missing is not None and not unresolved:
            unresolved = self._unconfirmed_limits(path.limits)
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
                total_action_effect=sum(step.action_effect for step in steps),
                total_action_fees=sum(step.action_fees for step in steps),
                total_cash_flow_from_actions=sum(
                    step.cash_flow_from_actions for step in steps
                ),
                first_trigger_step_id=first_trigger,
                first_breaches=first_breaches,
                unresolved_metrics=unresolved,
            ),
            missing,
        )

    @staticmethod
    def _action_plan(path: StressPath, policy: str) -> ActionPlan | None:
        if not policy.startswith("action:"):
            return None
        plan_id = policy.removeprefix("action:")
        matches = [plan for plan in path.action_plans if plan.plan_id == plan_id]
        return matches[0] if matches else None

    def _step(
        self,
        holdings: list[Holding],
        quantities: dict[str, float],
        cash: dict[str, float],
        previous_scenario: MarketScenario,
        scenario: MarketScenario,
        step: PathStep,
        policy: str,
        action_plan: ActionPlan | None,
        previous_total: float,
        initial_value: float,
        external_cash_base: float,
        limits: RiskLimits,
    ) -> tuple[
        PathStepResult,
        dict[str, float],
        dict[str, float],
        float,
        dict[str, tuple[LimitViolation, ...]],
        bool,
    ]:
        market_value = self._total(holdings, quantities, cash, scenario)
        market_effect = market_value - previous_total

        cash_flow_effect = 0.0
        new_cash = dict(cash)
        for flow in step.cash_flows:
            cash_flow_effect += flow.amount * self._fx(scenario, flow.currency)
            new_cash[flow.currency] = new_cash.get(flow.currency, 0.0) + flow.amount

        pre_action_value = self._total(holdings, quantities, new_cash, scenario)
        pre_breaches = self._check_limits(
            holdings,
            quantities,
            new_cash,
            scenario,
            previous_scenario,
            pre_action_value,
            initial_value,
            external_cash_base + cash_flow_effect,
            limits,
        )
        triggered = any(item.status == "breached" for item in pre_breaches)
        reconciliation = None
        action_effect = 0.0
        action_fees = 0.0
        cash_flow_from_actions = 0.0
        action_plan_id = None
        executed_plan = False

        if action_plan is not None and action_plan.trigger_step_id == step.step_id and triggered:
            action_plan_id = action_plan.plan_id
            (new_quantities, post_cash, post_action_value, reconciliation) = self._execute_actions(
                holdings,
                quantities,
                new_cash,
                scenario,
                action_plan,
                pre_action_value,
            )
            action_effect = reconciliation.action_effect
            action_fees = reconciliation.fees
            cash_flow_from_actions = reconciliation.cash_flow_from_actions
            executed_plan = True
        elif policy == REBALANCE_POLICY and step.trades:
            new_quantities = dict(quantities)
            for trade in step.trades:
                updated = new_quantities[trade.holding_id] + trade.quantity_delta
                if updated < 0:
                    raise ValueError(
                        f"Trade in step {step.step_id} would make holding "
                        f"{trade.holding_id} negative"
                    )
                new_quantities[trade.holding_id] = updated
            post_cash = new_cash
            trade_value_old_market = self._total(
                holdings, new_quantities, cash, previous_scenario
            )
            trade_effect = trade_value_old_market - previous_total
            new_value = self._total(holdings, new_quantities, cash, scenario)
            interaction_effect = new_value - market_value - trade_effect
        else:
            new_quantities = dict(quantities)
            post_cash = new_cash
            trade_effect = 0.0
            interaction_effect = 0.0

        post_action_value_final = self._total(
            holdings, new_quantities, post_cash, scenario
        )
        if action_plan_id is not None:
            trade_effect = 0.0
            interaction_effect = 0.0
        post_breaches = self._check_limits(
            holdings,
            new_quantities,
            post_cash,
            scenario,
            previous_scenario,
            post_action_value_final,
            initial_value,
            external_cash_base + cash_flow_effect,
            limits,
        )
        pre_status = {item.metric: item.status for item in pre_breaches}
        post_breaches = tuple(
            replace(
                item,
                status=(
                    "confirmed_within"
                    if item.status == "within"
                    and pre_status.get(item.metric) == "breached"
                    else item.status
                ),
            )
            for item in post_breaches
        )

        total_change = (
            market_effect
            + trade_effect
            + interaction_effect
            + cash_flow_effect
            + action_effect
        )
        portfolio_value = previous_total + total_change

        contributions = self._contributions(
            holdings,
            quantities,
            cash,
            new_quantities,
            post_cash,
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
                limit_checks=post_breaches,
                action_plan_id=action_plan_id,
                action_effect=action_effect,
                action_fees=action_fees,
                cash_flow_from_actions=cash_flow_from_actions,
                action_reconciliation=reconciliation,
            ),
            new_quantities,
            post_cash,
            portfolio_value,
            {"pre": pre_breaches, "post": post_breaches},
            executed_plan,
        )

    def _fx(self, scenario: MarketScenario, currency: str) -> float:
        try:
            return scenario.fx_rate(currency)
        except KeyError as error:
            raise MissingMarketDataError(
                f"Scenario {scenario.scenario_id} v{scenario.version} lacks "
                f"fx_rate:{currency}"
            ) from error

    def _check_limits(
        self,
        holdings: list[Holding],
        quantities: dict[str, float],
        cash: dict[str, float],
        scenario: MarketScenario,
        previous_scenario: MarketScenario,
        current_value: float,
        initial_value: float,
        external_cash_base: float,
        limits: RiskLimits,
    ) -> tuple[LimitViolation, ...]:
        checks: list[LimitViolation] = []
        if limits.max_loss is not None:
            pnl = current_value - initial_value - external_cash_base
            loss = max(0.0, -pnl)
            checks.append(
                LimitViolation(
                    metric="loss",
                    limit_value=limits.max_loss,
                    observed_value=loss,
                    excess=max(0.0, loss - limits.max_loss),
                    source="portfolio",
                    status="breached" if loss > limits.max_loss else "within",
                )
            )

        currency_exposures: dict[str, float] = {}
        holding_values: dict[str, float] = {}
        type_values: dict[str, float] = {}
        for holding in holdings:
            value = self._holding_value(holding, quantities[holding.id], scenario)
            currency_exposures[holding.currency] = (
                currency_exposures.get(holding.currency, 0.0) + value
            )
            holding_values[holding.id] = value
            type_values[holding.asset_type] = (
                type_values.get(holding.asset_type, 0.0) + value
            )
        for currency, amount in cash.items():
            currency_exposures[currency] = (
                currency_exposures.get(currency, 0.0) + amount * self._fx(scenario, currency)
            )

        for currency, threshold in limits.currency_exposure.items():
            observed = currency_exposures.get(currency, 0.0)
            checks.append(
                LimitViolation(
                    metric=f"currency_exposure:{currency}",
                    limit_value=threshold,
                    observed_value=observed,
                    excess=max(0.0, observed - threshold),
                    source=f"currency:{currency}",
                    source_currency=currency,
                    status="breached" if observed > threshold else "within",
                )
            )

        for key, threshold in limits.asset_concentration.items():
            observed_value = holding_values.get(key, type_values.get(key, 0.0))
            ratio = observed_value / current_value if current_value else 0.0
            checks.append(
                LimitViolation(
                    metric=f"asset_concentration:{key}",
                    limit_value=threshold,
                    observed_value=ratio,
                    excess=max(0.0, ratio - threshold),
                    source=f"asset:{key}",
                    source_holding_id=key if key in holding_values else None,
                    status="breached" if ratio > threshold else "within",
                )
            )
        return tuple(checks)

    @staticmethod
    def _unconfirmed_limits(limits: RiskLimits) -> tuple[LimitViolation, ...]:
        checks: list[LimitViolation] = []
        if limits.max_loss is not None:
            checks.append(
                LimitViolation(
                    metric="loss",
                    limit_value=limits.max_loss,
                    observed_value=None,
                    excess=None,
                    source="portfolio",
                    status="unconfirmed",
                )
            )
        for currency, threshold in limits.currency_exposure.items():
            checks.append(
                LimitViolation(
                    metric=f"currency_exposure:{currency}",
                    limit_value=threshold,
                    observed_value=None,
                    excess=None,
                    source=f"currency:{currency}",
                    source_currency=currency,
                    status="unconfirmed",
                )
            )
        for key, threshold in limits.asset_concentration.items():
            checks.append(
                LimitViolation(
                    metric=f"asset_concentration:{key}",
                    limit_value=threshold,
                    observed_value=None,
                    excess=None,
                    source=f"asset:{key}",
                    source_holding_id=key,
                    status="unconfirmed",
                )
            )
        return tuple(checks)

    def _execute_actions(
        self,
        holdings: list[Holding],
        quantities: dict[str, float],
        cash: dict[str, float],
        scenario: MarketScenario,
        plan: ActionPlan,
        pre_action_value: float,
    ) -> tuple[
        dict[str, float],
        dict[str, float],
        float,
        ActionReconciliation,
    ]:
        new_quantities = dict(quantities)
        new_cash = dict(cash)
        fills: list[ActionFill] = []
        total_fees = 0.0
        total_gross_cash = 0.0
        holding_by_id = {holding.id: holding for holding in holdings}

        for order in plan.orders:
            if order.order_type == "sell":
                holding = holding_by_id[order.holding_id]
                available = new_quantities[holding.id]
                quantity = min(order.quantity, available)
                if quantity <= 0:
                    raise ValueError(
                        f"Action plan {plan.plan_id} cannot sell {holding.id}; none available"
                    )
                local_value = quantity * self._unit_local_value(holding, scenario)
                fx_rate = self._fx(scenario, holding.currency)
                gross = local_value * fx_rate
                fee = gross * order.fee_bps / 10000.0
                net = gross - fee
                new_quantities[holding.id] = available - quantity
                new_cash[holding.currency] = (
                    new_cash.get(holding.currency, 0.0) + local_value
                )
                fee_currency = holding.currency
                new_cash[fee_currency] -= fee / fx_rate
                total_fees += fee
                total_gross_cash += gross
                fills.append(
                    ActionFill(
                        type="sell",
                        holding_id=holding.id,
                        quantity=quantity,
                        from_currency=None,
                        to_currency=holding.currency,
                        local_amount=local_value,
                        base_gross_amount=gross,
                        fee_base_amount=fee,
                        base_net_amount=net,
                    )
                )
            else:
                from_currency = order.from_currency
                to_currency = order.to_currency
                available = new_cash.get(from_currency, 0.0)
                local_amount = min(order.from_amount, available)
                if local_amount <= 0:
                    raise ValueError(
                        f"Action plan {plan.plan_id} cannot convert {from_currency}; none available"
                    )
                from_fx = self._fx(scenario, from_currency)
                to_fx = self._fx(scenario, to_currency)
                gross = local_amount * from_fx
                fee = gross * order.fee_bps / 10000.0
                received_base = gross - fee
                received_local = received_base / to_fx
                new_cash[from_currency] = available - local_amount
                new_cash[to_currency] = new_cash.get(to_currency, 0.0) + received_local
                total_fees += fee
                fills.append(
                    ActionFill(
                        type="fx_conversion",
                        holding_id=None,
                        quantity=local_amount,
                        from_currency=from_currency,
                        to_currency=to_currency,
                        local_amount=local_amount,
                        base_gross_amount=gross,
                        fee_base_amount=fee,
                        base_net_amount=received_base,
                    )
                )

        post_action_value = self._total(
            holdings, new_quantities, new_cash, scenario
        )
        action_effect = post_action_value - pre_action_value
        reconciliation = ActionReconciliation(
            pre_action_value=pre_action_value,
            post_action_value=post_action_value,
            action_effect=action_effect,
            fees=total_fees,
            cash_flow_from_actions=total_gross_cash,
            fills=tuple(fills),
        )
        return new_quantities, new_cash, post_action_value, reconciliation

    def _holding_value(
        self,
        holding: Holding,
        quantity: float,
        scenario: MarketScenario,
    ) -> float:
        sized = replace(holding, quantity=quantity)
        local_value = self.pricers[holding.asset_type].price_in_currency(sized, scenario)
        return local_value * self._fx(scenario, holding.currency)

    def _unit_local_value(self, holding: Holding, scenario: MarketScenario) -> float:
        unit = replace(holding, quantity=1)
        return self.pricers[holding.asset_type].price_in_currency(unit, scenario)

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
