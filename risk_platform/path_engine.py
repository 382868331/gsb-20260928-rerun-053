from __future__ import annotations

from dataclasses import dataclass, replace

from risk_platform.holdings import Holding
from risk_platform.paths import (
    ActionPlan,
    LiquidityWindow,
    PathStep,
    RiskLimits,
    StressPath,
)
from risk_platform.pricing import CashPricer, EquityPricer, Pricer, ZeroCouponBondPricer
from risk_platform.results import (
    ActionFill,
    ActionReconciliation,
    Contribution,
    LimitViolation,
    PendingSettlement,
    PathRunResult,
    PathScenarioResult,
    PathStepResult,
    PolicyResumeState,
    PolicyPathResult,
    ResidualOrder,
)
from risk_platform.scenarios import MarketScenario, MissingMarketDataError

HOLD_POLICY = "hold"
REBALANCE_POLICY = "rebalance"
CONSTRAINED_POLICY = "constrained"
DEFAULT_POLICIES = (HOLD_POLICY, REBALANCE_POLICY)


@dataclass(slots=True)
class ReplayState:
    quantities: dict[str, float]
    cash: dict[str, float]
    pending: tuple[PendingSettlement, ...]
    residuals: tuple[ResidualOrder, ...]
    cancelled: set[str]
    external_cash_base: float
    first_trigger: str | None
    first_breaches: tuple[LimitViolation, ...]


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

    def resume(
        self,
        run_id: str,
        holdings: list[Holding],
        paths: list[StressPath],
        saved: PathRunResult,
    ) -> PathRunResult:
        """Replay onward from saved partial-fill states without rewriting fills.

        Already executed fills, pending settlement entries and residual orders
        come from the saved run; only steps strictly after the saved prefix
        are replayed. Changing later market data therefore cannot rewrite a
        fill that already happened.
        """
        path_by_key = {(path.path_id, path.version): path for path in paths}
        resumed_results: list[PathScenarioResult] = []
        for old in saved.results:
            path = path_by_key.get((old.path_id, old.path_version))
            saved_steps = {
                step_id
                for policy in old.policies
                if policy.resume_state is not None
                for step_id in policy.resume_state.completed_step_ids
            }
            prefix_fingerprints = {
                step.scenario_fingerprint
                for step in old.policies[0].steps
            }
            new_prefix_fingerprints = {
                step.scenario.fingerprint
                for step in path.steps
                if step.step_id in saved_steps
            } if path is not None else set()
            prefix_unchanged = (
                path is not None and prefix_fingerprints == new_prefix_fingerprints
            )
            if path is None or not prefix_unchanged:
                resumed_results.append(old)
                continue
            policies: list[PolicyPathResult] = []
            for old_policy in old.policies:
                if old_policy.complete or old_policy.resume_state is None:
                    policies.append(old_policy)
                    continue
                policies.append(
                    self._resume_policy(holdings, path, old_policy, old.path_fingerprint)
                )
            status = "complete" if all(policy.complete for policy in policies) else "partial"
            resumed_results.append(
                PathScenarioResult(
                    path_id=old.path_id,
                    path_version=old.path_version,
                    path_fingerprint=old.path_fingerprint,
                    status=status,
                    missing_data=(
                        None
                        if status == "complete"
                        else next(
                            (item for item in policies if not item.complete), None
                        ) and "resumed path still incomplete"
                    ),
                    policies=tuple(policies),
                    first_trigger_step_id=old.first_trigger_step_id,
                    trigger_basis_fingerprint=old.trigger_basis_fingerprint,
                )
            )
        return PathRunResult(run_id=run_id, results=tuple(resumed_results))

    def _resume_policy(
        self,
        holdings: list[Holding],
        path: StressPath,
        old_policy: PolicyPathResult,
        path_fingerprint: str,
    ) -> PolicyPathResult:
        resume = old_policy.resume_state
        assert resume is not None
        constrained, action_plan = self._policy_plan(path, resume.policy)
        steps = list(old_policy.steps)
        state = ReplayState(
            quantities=dict(resume.quantities),
            cash=dict(resume.cash),
            pending=resume.pending,
            residuals=resume.residual_orders,
            cancelled={
                order.order_id
                for order in resume.residual_orders
                if order.cancelled
            },
            external_cash_base=resume.external_cash_base,
            first_trigger=resume.first_trigger_step_id,
            first_breaches=resume.first_breaches,
        )
        previous_total = resume.previous_total
        initial_value = resume.initial_value
        action_executed = resume.action_executed
        missing: str | None = None
        unresolved = old_policy.unresolved_metrics
        completed = set(resume.completed_step_ids)
        if completed:
            prefix_size = (
                max(
                    index
                    for index, step in enumerate(path.steps)
                    if step.step_id in completed
                )
                + 1
            )
        else:
            prefix_size = 0
        previous_scenario = next(
            step.scenario
            for step in reversed(path.steps)
            if step.step_id in completed
        )
        for index, step in enumerate(path.steps):
            if index < prefix_size or step.step_id in completed:
                continue
            try:
                plan_for_step = (
                    action_plan
                    if action_plan is not None
                    and not action_executed
                    and action_plan.trigger_step_id == step.step_id
                    else None
                )
                step_result, state, _value, previous_total = self._process_step(
                    holdings=holdings,
                    path=path,
                    step=step,
                    previous_scenario=previous_scenario,
                    previous_total=previous_total,
                    initial_value=initial_value,
                    state=state,
                    policy=resume.policy,
                    constrained=constrained,
                    action_plan=plan_for_step,
                    action_executed=action_executed,
                    is_initial=False,
                    step_index=index,
                )
            except MissingMarketDataError as error:
                missing = str(error)
                unresolved = self._unconfirmed_limits(path.limits)
                break
            steps.append(step_result)
            completed.add(step.step_id)
            previous_scenario = step.scenario
            trigger_index = (
                next(
                    item_index
                    for item_index, path_step in enumerate(path.steps)
                    if path_step.step_id == action_plan.trigger_step_id
                )
                if action_plan is not None
                else -1
            )
            if action_plan is not None and index >= trigger_index:
                action_executed = True

        complete = missing is None
        resume_state = None if complete else PolicyResumeState(
            path_id=path.path_id,
            path_version=path.version,
            path_fingerprint=path_fingerprint,
            policy=resume.policy,
            completed_step_ids=tuple(step.step_id for step in steps),
            quantities=dict(state.quantities),
            cash=dict(state.cash),
            pending=state.pending,
            residual_orders=state.residuals,
            initial_value=initial_value,
            previous_total=previous_total,
            external_cash_base=state.external_cash_base,
            action_executed=action_executed,
            first_trigger_step_id=state.first_trigger,
            first_breaches=state.first_breaches,
        )
        return replace(
            old_policy,
            complete=complete,
            final_value=previous_total,
            steps=tuple(steps),
            total_market_effect=sum(step.market_effect for step in steps),
            total_trade_effect=sum(step.trade_effect for step in steps),
            total_interaction_effect=sum(step.interaction_effect for step in steps),
            total_cash_flow_effect=sum(step.cash_flow_effect for step in steps),
            total_change=sum(step.total_change for step in steps),
            total_action_effect=sum(step.action_effect for step in steps),
            total_action_fees=sum(step.action_fees for step in steps),
            total_cash_flow_from_actions=sum(
                step.cash_flow_from_actions for step in steps
            ),
            first_trigger_step_id=state.first_trigger,
            first_breaches=state.first_breaches,
            unresolved_metrics=unresolved,
            final_pending_settle=state.pending,
            final_residual_orders=tuple(
                order for order in state.residuals if not order.cancelled
            ),
            total_settlement_lag_effect=sum(
                step.settled_from_fills_base for step in steps
            ),
            resume_state=resume_state,
        )

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
        constrained_names = (() )
        if PathEngine._path_has_liquidity(path):
            constrained_names = tuple(
                [f"{CONSTRAINED_POLICY}:{plan.plan_id}" for plan in path.action_plans]
                + [f"{CONSTRAINED_POLICY}:all"]
            )
        available = DEFAULT_POLICIES + action_names + constrained_names
        if requested is None:
            return available
        unknown = [policy for policy in requested if policy not in available]
        if unknown:
            raise ValueError(f"Unknown policy: {', '.join(unknown)}")
        return requested

    @staticmethod
    def _path_has_liquidity(path: StressPath) -> bool:
        return any(step.liquidity_windows for step in path.steps)

    def _replay(
        self,
        holdings: list[Holding],
        path: StressPath,
        policy: str,
    ) -> tuple[PolicyPathResult, str | None]:
        constrained, action_plan = self._policy_plan(path, policy)
        state = ReplayState(
            quantities={holding.id: holding.quantity for holding in holdings},
            cash={},
            pending=(),
            residuals=(),
            cancelled=set(),
            external_cash_base=0.0,
            first_trigger=None,
            first_breaches=(),
        )
        steps: list[PathStepResult] = []
        missing: str | None = None
        unresolved: tuple[LimitViolation, ...] = ()
        action_executed = False

        initial_step = path.steps[0]
        try:
            previous_total = self._total(holdings, state.quantities, state.cash, initial_step.scenario, pending=state.pending)
            initial_value = previous_total
        except MissingMarketDataError as error:
            missing = str(error)
            previous_total = 0.0
            initial_value = 0.0
            unresolved = self._unconfirmed_limits(path.limits)

        if missing is None:
            previous_scenario = initial_step.scenario
            initial_result = self._process_step(
                holdings=holdings,
                path=path,
                step=initial_step,
                previous_scenario=initial_step.scenario,
                previous_total=previous_total,
                initial_value=initial_value,
                state=state,
                policy=policy,
                constrained=constrained,
                action_plan=action_plan if action_plan is not None and action_plan.trigger_step_id == initial_step.step_id else None,
                action_executed=action_executed,
                is_initial=True,
                step_index=0,
            )
            initial_step_result, state, initial_value, previous_total = initial_result
            if action_plan is not None and action_plan.trigger_step_id == initial_step.step_id:
                action_executed = True
            unresolved = tuple(
                item
                for item in initial_step_result.limit_checks
                if item.status in {"breached", "unconfirmed"}
            )
            if state.first_breaches:
                unresolved = tuple(
                    item
                    for item in initial_step_result.limit_checks
                    if item.status in {"breached", "unconfirmed"}
                )

            for step in path.steps[1:]:
                scenario = step.scenario
                try:
                    plan_for_step = (
                        action_plan
                        if action_plan is not None
                        and not action_executed
                        and action_plan.trigger_step_id == step.step_id
                        else None
                    )
                    step_result_tuple = self._process_step(
                        holdings=holdings,
                        path=path,
                        step=step,
                        previous_scenario=previous_scenario,
                        previous_total=previous_total,
                        initial_value=initial_value,
                        state=state,
                        policy=policy,
                        constrained=constrained,
                        action_plan=plan_for_step,
                        action_executed=action_executed,
                        is_initial=False,
                        step_index=path.steps.index(step),
                    )
                except MissingMarketDataError as error:
                    missing = str(error)
                    unresolved = self._unconfirmed_limits(path.limits)
                    break
                step_result, state, post_value, previous_total = step_result_tuple
                if action_plan is not None and path.steps.index(step) >= next(
                    index
                    for index, item in enumerate(path.steps)
                    if item.step_id == action_plan.trigger_step_id
                ):
                    action_executed = True
                steps.append(step_result)
                previous_scenario = scenario

        final_value = previous_total
        if missing is not None and not unresolved:
            unresolved = self._unconfirmed_limits(path.limits)
        resume_state = None
        if missing is not None:
            resume_state = PolicyResumeState(
                path_id=path.path_id,
                path_version=path.version,
                path_fingerprint=path.fingerprint,
                policy=policy,
                completed_step_ids=tuple(step.step_id for step in steps),
                quantities=dict(state.quantities),
                cash=dict(state.cash),
                pending=state.pending,
                residual_orders=state.residuals,
                initial_value=initial_value,
                previous_total=previous_total,
                external_cash_base=state.external_cash_base,
                action_executed=action_executed,
                first_trigger_step_id=state.first_trigger,
                first_breaches=state.first_breaches,
            )
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
                first_trigger_step_id=state.first_trigger,
                first_breaches=state.first_breaches,
                unresolved_metrics=unresolved,
                final_pending_settle=state.pending,
                final_residual_orders=tuple(
                    order for order in state.residuals if not order.cancelled
                ),
                total_settlement_lag_effect=sum(
                    step.settled_from_fills_base for step in steps
                ),
                resume_state=resume_state,
            ),
            missing,
        )

    def _policy_plan(
        self,
        path: StressPath,
        policy: str,
    ) -> tuple[bool, ActionPlan | None]:
        if policy.startswith(f"{CONSTRAINED_POLICY}:"):
            plan_id = policy.removeprefix(f"{CONSTRAINED_POLICY}:")
            if plan_id == "all":
                return True, None
            matches = [plan for plan in path.action_plans if plan.plan_id == plan_id]
            return True, (matches[0] if matches else None)
        return False, self._action_plan(path, policy)

    def _process_step(
        self,
        *,
        holdings: list[Holding],
        path: StressPath,
        step: PathStep,
        previous_scenario: MarketScenario,
        previous_total: float,
        initial_value: float,
        state: ReplayState,
        policy: str,
        constrained: bool,
        action_plan: ActionPlan | None,
        action_executed: bool,
        is_initial: bool,
        step_index: int,
    ) -> tuple[PathStepResult, ReplayState, float, float]:
        scenario = step.scenario
        settled_base = 0.0
        if not is_initial:
            state.pending, settled_base = self._settle_due(state.pending, state.cash, step.step_id)

        market_value = self._total(holdings, state.quantities, state.cash, scenario, pending=state.pending)
        market_effect = market_value - previous_total
        pre_cashflow_quantities = dict(state.quantities)
        pre_cashflow_cash = dict(state.cash)
        step_start_quantities = dict(state.quantities)
        step_start_cash = dict(state.cash)
        step_start_pending = state.pending

        cash_flow_effect = 0.0
        for flow in step.cash_flows:
            cash_flow_effect += flow.amount * self._fx(scenario, flow.currency)
            state.cash[flow.currency] = state.cash.get(flow.currency, 0.0) + flow.amount
        state.external_cash_base += cash_flow_effect

        if constrained:
            for cancelled_id in step.cancel_plan_ids:
                self._cancel_residuals(state, cancelled_id, step.step_id)
            cancellations_this_step = tuple(step.cancel_plan_ids)
        else:
            cancellations_this_step = ()

        pre_action_value = self._total(holdings, state.quantities, state.cash, scenario, pending=state.pending)
        pre_breaches = self._check_limits(
            holdings,
            state.quantities,
            state.cash,
            state.pending,
            scenario,
            pre_action_value,
            initial_value,
            state.external_cash_base,
            path.limits,
        )
        triggered = any(item.status == "breached" for item in pre_breaches)
        seen_metrics = {item.metric for item in state.first_breaches}
        newly_breached = [
            replace(item, trigger_step_id=step.step_id)
            for item in pre_breaches
            if item.status == "breached" and item.metric not in seen_metrics
        ]
        if newly_breached and state.first_trigger is None:
            state.first_trigger = step.step_id
        if newly_breached:
            existing_trigger = state.first_trigger or step.step_id
            state.first_trigger = existing_trigger
            state.first_breaches = state.first_breaches + tuple(newly_breached)

        reconciliation = None
        action_effect = 0.0
        action_fees = 0.0
        cash_flow_from_actions = 0.0
        action_plan_id = None
        trade_effect = 0.0
        interaction_effect = 0.0
        new_quantities = dict(state.quantities)

        if constrained:
            plans_triggered = (
                [
                    plan
                    for plan in path.action_plans
                    if (
                        plan.trigger_step_id == step.step_id
                        and (
                            policy == "constrained:all"
                            or (
                                action_plan is not None
                                and plan.plan_id == action_plan.plan_id
                            )
                        )
                    )
                ]
                if triggered
                else []
            )
            for plan in plans_triggered:
                if any(
                    order.order_id.startswith(f"{plan.plan_id}:")
                    for order in state.residuals
                ):
                    continue
                self._cancel_residuals(state, plan.plan_id, step.step_id)
                for cancelled_id in plan.cancel_plan_ids:
                    self._cancel_residuals(state, cancelled_id, step.step_id)
                state.residuals = self._submit_plan_orders(plan, state.residuals, step.step_id)
            if plans_triggered:
                action_plan_id = ",".join(plan.plan_id for plan in plans_triggered)
            fill_result = self._fill_residuals(
                holdings=holdings,
                step=step,
                state=state,
                pre_action_value=pre_action_value,
                step_index=step_index,
                step_ids=tuple(item.step_id for item in path.steps),
                cancellations_this_step=cancellations_this_step,
            )
            (
                state.quantities,
                state.cash,
                state.pending,
                state.residuals,
                reconciliation,
                settled_immediately,
            ) = fill_result
            action_effect = reconciliation.action_effect
            action_fees = reconciliation.fees
            cash_flow_from_actions = reconciliation.cash_flow_from_actions
            post_cash = state.cash
            post_pending = state.pending
        elif action_plan is not None and triggered:
            action_plan_id = action_plan.plan_id
            (new_quantities, post_cash, post_action_value, reconciliation) = self._execute_actions(
                holdings,
                state.quantities,
                state.cash,
                scenario,
                action_plan,
                pre_action_value,
            )
            action_effect = reconciliation.action_effect
            action_fees = reconciliation.fees
            cash_flow_from_actions = reconciliation.cash_flow_from_actions
            state.quantities = new_quantities
            state.cash = post_cash
            post_pending = state.pending
        elif policy == REBALANCE_POLICY and step.trades:
            new_quantities = dict(state.quantities)
            for trade in step.trades:
                updated = new_quantities[trade.holding_id] + trade.quantity_delta
                if updated < 0:
                    raise ValueError(
                        f"Trade in step {step.step_id} would make holding "
                        f"{trade.holding_id} negative"
                    )
                new_quantities[trade.holding_id] = updated
            state.quantities = new_quantities
            post_cash = state.cash
            post_pending = state.pending
            trade_value_old_market = self._total(
                holdings, new_quantities, pre_cashflow_cash, previous_scenario, pending=state.pending
            )
            trade_effect = trade_value_old_market - previous_total
            new_value = self._total(holdings, new_quantities, pre_cashflow_cash, scenario, pending=state.pending)
            interaction_effect = new_value - market_value - trade_effect
        else:
            post_cash = state.cash
            post_pending = state.pending

        post_action_value_final = self._total(
            holdings, state.quantities, post_cash, scenario, pending=post_pending
        )
        post_breaches = self._check_limits(
            holdings,
            state.quantities,
            post_cash,
            post_pending,
            scenario,
            post_action_value_final,
            initial_value,
            state.external_cash_base,
            path.limits,
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

        contributions = self._step_contributions(
            holdings,
            start_quantities=step_start_quantities,
            start_cash=step_start_cash,
            start_pending=step_start_pending,
            end_quantities=state.quantities,
            end_cash=post_cash,
            end_pending=post_pending,
            scenario=scenario,
            previous_scenario=previous_scenario,
        )

        step_result = PathStepResult(
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
            pending_settle_base_amount=sum(entry.base_amount for entry in post_pending),
            pending_settle_entries=post_pending,
            available_cash_base=sum(
                amount * self._fx(scenario, currency)
                for currency, amount in post_cash.items()
            ) + sum(
                self._holding_value(holding, state.quantities[holding.id], scenario)
                for holding in holdings
                if holding.asset_type == "cash"
            ),
            residual_orders=state.residuals,
            settled_from_fills_base=settled_base,
        )
        return step_result, state, portfolio_value, portfolio_value

    def _step_contributions(
        self,
        holdings: list[Holding],
        *,
        start_quantities: dict[str, float],
        start_cash: dict[str, float],
        start_pending: tuple[PendingSettlement, ...],
        end_quantities: dict[str, float],
        end_cash: dict[str, float],
        end_pending: tuple[PendingSettlement, ...],
        scenario: MarketScenario,
        previous_scenario: MarketScenario,
    ) -> tuple[Contribution, ...]:
        contributions: list[Contribution] = []
        for holding in holdings:
            pricer = self.pricers[holding.asset_type]
            sized = replace(holding, quantity=end_quantities[holding.id])
            local_value = pricer.price_in_currency(sized, scenario)
            fx_rate = self._fx(scenario, holding.currency)
            base_value = local_value * fx_rate
            previous_sized = replace(holding, quantity=start_quantities[holding.id])
            previous_local = pricer.price_in_currency(previous_sized, previous_scenario)
            previous_value = previous_local * previous_scenario.fx_rate(holding.currency)
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
        cash_currencies = set(end_cash) | set(start_cash)
        for currency in cash_currencies:
            amount = end_cash.get(currency, 0.0)
            fx_rate = self._fx(scenario, currency)
            previous_amount = start_cash.get(currency, 0.0)
            previous_value = previous_amount * previous_scenario.fx_rate(currency)
            contributions.append(
                Contribution(
                    holding_id=f"reinvested-cash:{currency}",
                    name=f"Reinvested cash {currency}",
                    asset_type="cash",
                    currency=currency,
                    local_value=amount,
                    fx_rate=fx_rate,
                    base_value=amount * fx_rate,
                    base_scenario_value=previous_value,
                    pnl_vs_base=amount * fx_rate - previous_value,
                )
            )
        pending_by_currency: dict[str, float] = {}
        for entry in end_pending:
            pending_by_currency[entry.currency] = (
                pending_by_currency.get(entry.currency, 0.0) + entry.local_amount
            )
        previous_pending_by_currency: dict[str, float] = {}
        for entry in start_pending:
            previous_pending_by_currency[entry.currency] = (
                previous_pending_by_currency.get(entry.currency, 0.0) + entry.local_amount
            )
        for currency in set(pending_by_currency) | set(previous_pending_by_currency):
            amount = pending_by_currency.get(currency, 0.0)
            fx_rate = self._fx(scenario, currency)
            previous_amount = previous_pending_by_currency.get(currency, 0.0)
            previous_value = previous_amount * previous_scenario.fx_rate(currency)
            contributions.append(
                Contribution(
                    holding_id=f"pending-settlement:{currency}",
                    name=f"Pending settlement {currency}",
                    asset_type="cash",
                    currency=currency,
                    local_value=amount,
                    fx_rate=fx_rate,
                    base_value=amount * fx_rate,
                    base_scenario_value=previous_value,
                    pnl_vs_base=amount * fx_rate - previous_value,
                )
            )
        return tuple(contributions)

    @staticmethod
    def _action_plan(path: StressPath, policy: str) -> ActionPlan | None:
        if not policy.startswith("action:"):
            return None
        plan_id = policy.removeprefix("action:")
        matches = [plan for plan in path.action_plans if plan.plan_id == plan_id]
        return matches[0] if matches else None

    def _settle_due(
        self,
        pending: tuple[PendingSettlement, ...],
        cash: dict[str, float],
        step_id: str,
    ) -> tuple[tuple[PendingSettlement, ...], float]:
        remaining: list[PendingSettlement] = []
        settled_base = 0.0
        for entry in pending:
            if entry.settlement_step_id == step_id:
                cash[entry.currency] = cash.get(entry.currency, 0.0) + entry.local_amount
                settled_base += entry.base_amount
            else:
                remaining.append(entry)
        return tuple(remaining), settled_base

    @staticmethod
    def _cancel_residuals(
        state: ReplayState,
        plan_id: str,
        step_id: str,
    ) -> None:
        updated: list[ResidualOrder] = []
        for order in state.residuals:
            if order.order_id.startswith(f"{plan_id}:") and not order.cancelled:
                updated.append(
                    replace(
                        order,
                        cancelled=True,
                        cancel_step_id=step_id,
                        remaining_quantity=0.0,
                    )
                )
            else:
                updated.append(order)
        state.residuals = tuple(updated)

    @staticmethod
    def _submit_plan_orders(
        plan: ActionPlan,
        existing: tuple[ResidualOrder, ...],
        step_id: str,
    ) -> tuple[ResidualOrder, ...]:
        new_orders: list[ResidualOrder] = []
        for index, order in enumerate(plan.orders):
            requested = (
                order.quantity if order.order_type == "sell" else order.from_amount
            )
            assert requested is not None
            order_id = f"{plan.plan_id}:{index}"
            if any(item.order_id == order_id for item in existing):
                continue
            new_orders.append(
                ResidualOrder(
                    order_id=order_id,
                    type=order.order_type,
                    holding_id=order.holding_id,
                    from_currency=order.from_currency,
                    to_currency=order.to_currency,
                    requested_quantity=requested,
                    filled_quantity=0.0,
                    remaining_quantity=requested,
                    fee_bps=order.fee_bps,
                    trigger_step_id=step_id,
                    priority=order.priority,
                    allocation_rule=plan.allocation_rule,
                    price_policy=plan.price_policy,
                )
            )
        return existing + tuple(new_orders)

    @staticmethod
    def _executable_price(window: LiquidityWindow, policy: str) -> float:
        if policy == "worst":
            return window.min_price
        if policy == "best":
            return window.max_price
        return (window.min_price + window.max_price) / 2.0

    def _window_for(
        self,
        order: ResidualOrder,
        step: PathStep,
    ) -> LiquidityWindow | None:
        for window in step.liquidity_windows:
            if order.type == "sell" and (
                window.window_type == "sell"
                and window.holding_id == order.holding_id
            ):
                return window
            if order.type == "fx_conversion" and window.window_type == "fx":
                from_currency, to_currency = window.pair_currencies
                if (
                    order.from_currency == from_currency
                    and order.to_currency == to_currency
                ):
                    return window
        return None

    def _fill_residuals(
        self,
        *,
        holdings: list[Holding],
        step: PathStep,
        state: ReplayState,
        pre_action_value: float,
        step_index: int,
        step_ids: tuple[str, ...],
        cancellations_this_step: tuple[str, ...] = (),
    ) -> tuple[
        dict[str, float],
        dict[str, float],
        tuple[PendingSettlement, ...],
        tuple[ResidualOrder, ...],
        ActionReconciliation,
        float,
    ]:
        scenario = step.scenario
        holding_by_id = {holding.id: holding for holding in holdings}
        new_quantities = dict(state.quantities)
        new_cash = dict(state.cash)
        available_cash_by_currency: dict[str, float] = {}
        for holding in holdings:
            if holding.asset_type == "cash":
                available_cash_by_currency[holding.currency] = (
                    available_cash_by_currency.get(holding.currency, 0.0)
                    + new_quantities[holding.id]
                )
        for currency, amount in new_cash.items():
            available_cash_by_currency[currency] = (
                available_cash_by_currency.get(currency, 0.0) + amount
            )
        fills: list[ActionFill] = []
        updated_orders: list[ResidualOrder] = []
        total_fees = 0.0
        total_gross_cash = 0.0
        pending_list = list(state.pending)

        active_orders = [order for order in state.residuals if not order.cancelled]
        windows_used: dict[str, float] = {}
        cash_used: dict[str, float] = {}

        ordered = sorted(
            active_orders,
            key=lambda item: (item.trigger_step_id, item.priority, item.order_id),
        )
        allocations = self._pro_rata_allocations(ordered, step)
        for order in ordered:
            window = self._window_for(order, step)
            residual = order.remaining_quantity
            if window is None or residual <= 0:
                updated_orders.append(order)
                continue
            window_key = self._window_key(window)
            if order.allocation_rule == "pro_rata":
                capacity = allocations.get(order.order_id, 0.0)
            else:
                capacity = window.max_quantity - windows_used.get(window_key, 0.0)
            if order.type == "sell":
                holding = holding_by_id[order.holding_id]
                available = new_quantities[holding.id]
                fill_qty = min(residual, capacity, available)
                windows_used[self._window_key(window)] = (
                    windows_used.get(window_key, 0.0) + fill_qty
                )
                if fill_qty <= 0:
                    updated_orders.append(order)
                    continue
                unit_price = self._executable_price(window, order.price_policy)
                local_value = fill_qty * unit_price
                fx_rate = self._fx(scenario, holding.currency)
                gross = local_value * fx_rate
                fee = gross * order.fee_bps / 10000.0
                net_local = local_value - fee / fx_rate
                new_quantities[holding.id] = available - fill_qty
                total_fees += fee
                total_gross_cash += gross
                fills.append(
                    ActionFill(
                        type="sell",
                        holding_id=holding.id,
                        quantity=fill_qty,
                        from_currency=None,
                        to_currency=holding.currency,
                        local_amount=local_value,
                        base_gross_amount=gross,
                        fee_base_amount=fee,
                        base_net_amount=gross - fee,
                        order_id=order.order_id,
                        requested_quantity=order.requested_quantity,
                        unit_price=unit_price,
                        trigger_step_id=order.trigger_step_id,
                        fill_step_id=step.step_id,
                        settlement_step_id=self._settlement_step(
                            step_ids, step_index, window.settlement_lag_steps
                        ),
                        settlement_status=(
                            "settled" if window.settlement_lag_steps == 0 else "pending"
                        ),
                    )
                )
                if window.settlement_lag_steps == 0:
                    self._give_cash(
                        holdings,
                        new_quantities,
                        new_cash,
                        holding.currency,
                        net_local,
                    )
                else:
                    pending_list.append(
                        PendingSettlement(
                            type="sell",
                            holding_id=holding.id,
                            currency=holding.currency,
                            local_amount=net_local,
                            base_amount=(gross - fee),
                            gross_base_amount=gross,
                            fee_base_amount=fee,
                            fill_step_id=step.step_id,
                            settlement_step_id=self._settlement_step(
                                step_ids, step_index, window.settlement_lag_steps
                            ),
                        )
                    )
            else:
                assert order.from_currency is not None and order.to_currency is not None
                available = available_cash_by_currency.get(order.from_currency, 0.0) - cash_used.get(
                    order.from_currency, 0.0
                )
                fill_local = min(residual, capacity, available)
                windows_used[window_key] = (
                    windows_used.get(window_key, 0.0) + fill_local
                )
                if fill_local <= 0:
                    updated_orders.append(order)
                    continue
                rate = self._executable_price(window, order.price_policy)
                gross_base = fill_local * self._fx(scenario, order.from_currency)
                fee = gross_base * order.fee_bps / 10000.0
                received_local = (fill_local * rate) - fee / self._fx(
                    scenario, order.to_currency
                )
                cash_used[order.from_currency] = (
                    cash_used.get(order.from_currency, 0.0) + fill_local
                )
                self._take_cash(
                    holdings,
                    new_quantities,
                    new_cash,
                    order.from_currency,
                    fill_local,
                )
                total_fees += fee
                fills.append(
                    ActionFill(
                        type="fx_conversion",
                        holding_id=None,
                        quantity=fill_local,
                        from_currency=order.from_currency,
                        to_currency=order.to_currency,
                        local_amount=fill_local,
                        base_gross_amount=gross_base,
                        fee_base_amount=fee,
                        base_net_amount=gross_base - fee,
                        order_id=order.order_id,
                        requested_quantity=order.requested_quantity,
                        unit_price=rate,
                        trigger_step_id=order.trigger_step_id,
                        fill_step_id=step.step_id,
                        settlement_step_id=self._settlement_step(
                            step_ids, step_index, window.settlement_lag_steps
                        ),
                        settlement_status=(
                            "settled" if window.settlement_lag_steps == 0 else "pending"
                        ),
                    )
                )
                if window.settlement_lag_steps == 0:
                    self._give_cash(
                        holdings,
                        new_quantities,
                        new_cash,
                        order.to_currency,
                        received_local,
                    )
                else:
                    pending_list.append(
                        PendingSettlement(
                            type="fx_conversion",
                            holding_id=None,
                            currency=order.to_currency,
                            local_amount=received_local,
                            base_amount=gross_base - fee,
                            gross_base_amount=gross_base,
                            fee_base_amount=fee,
                            fill_step_id=step.step_id,
                            settlement_step_id=self._settlement_step(
                                step_ids, step_index, window.settlement_lag_steps
                            ),
                        )
                    )
            updated_order = replace(
                order,
                filled_quantity=order.filled_quantity + (
                    fill_qty if order.type == "sell" else fill_local
                ),
                remaining_quantity=residual - (
                    fill_qty if order.type == "sell" else fill_local
                ),
            )
            updated_orders.append(updated_order)

        post_action_value = self._total(
            holdings, new_quantities, new_cash, scenario, pending=tuple(pending_list)
        )
        pending_final = tuple(pending_list)
        for order in state.residuals:
            if (
                order.cancelled
                and order.cancel_step_id == step.step_id
                and not any(item.order_id == order.order_id for item in updated_orders)
            ):
                updated_orders.append(order)
        requested_value = 0.0
        unfilled_value = 0.0
        for order in updated_orders:
            if order.type == "sell":
                holding = holding_by_id[order.holding_id]
                unit_value = self._unit_local_value(holding, scenario) * self._fx(
                    scenario, holding.currency
                )
                requested_value += order.requested_quantity * unit_value
                unfilled_value += order.remaining_quantity * unit_value
            else:
                assert order.from_currency is not None
                requested_value += order.requested_quantity * self._fx(
                    scenario, order.from_currency
                )
                unfilled_value += order.remaining_quantity * self._fx(
                    scenario, order.from_currency
                )
        reconciliation = ActionReconciliation(
            pre_action_value=pre_action_value,
            post_action_value=post_action_value,
            action_effect=post_action_value - pre_action_value,
            fees=total_fees,
            cash_flow_from_actions=total_gross_cash,
            fills=tuple(fills),
            pending_base_value=sum(entry.base_amount for entry in pending_final),
            residual_orders=tuple(updated_orders),
            requested_value=requested_value,
            unfilled_value=unfilled_value,
            cancelled_order_ids=tuple(
                order_id
                for plan_id in cancellations_this_step
                for order_id in (
                    order.order_id
                    for order in updated_orders
                    if order.cancelled and order.order_id.startswith(f"{plan_id}:")
                )
            ),
        )
        return (
            new_quantities,
            new_cash,
            pending_final,
            tuple(updated_orders),
            reconciliation,
            0.0,
        )

    @staticmethod
    def _window_key(window: LiquidityWindow) -> str:
        if window.window_type == "sell":
            return f"sell:{window.holding_id}"
        return f"fx:{window.pair}"

    @staticmethod
    def _give_cash(holdings, quantities, cash, currency, amount):
        cash[currency] = cash.get(currency, 0.0) + amount

    @staticmethod
    def _take_cash(holdings, quantities, cash, currency, amount):
        remaining = amount
        if cash.get(currency, 0.0) >= remaining:
            cash[currency] -= remaining
            return
        remaining -= cash.get(currency, 0.0)
        cash[currency] = 0.0
        for holding in holdings:
            if holding.asset_type != "cash" or holding.currency != currency:
                continue
            take = min(quantities[holding.id], remaining)
            quantities[holding.id] -= take
            remaining -= take
            if remaining <= 0:
                return

    def _pro_rata_allocations(
        self,
        ordered: list[ResidualOrder],
        step: PathStep,
    ) -> dict[str, float]:
        """Pre-fixed pro-rata shares of each window, decided before fills.

        Orders sharing a window under a pro-rata plan split its capacity in
        proportion to their remaining size. The split is computed once before
        any fill, so later losses cannot change which order filled.
        """
        allocations: dict[str, float] = {}
        groups: dict[str, list[ResidualOrder]] = {}
        for order in ordered:
            window = self._window_for(order, step)
            if window is not None and order.allocation_rule == "pro_rata":
                groups.setdefault(self._window_key(window), []).append(order)
        for window_key, orders in groups.items():
            window = next(
                item
                for item in step.liquidity_windows
                if self._window_key(item) == window_key
            )
            total_demand = sum(order.remaining_quantity for order in orders)
            for order in orders:
                share = (
                    window.max_quantity * order.remaining_quantity / total_demand
                    if total_demand > 0
                    else 0.0
                )
                allocations[order.order_id] = min(share, order.remaining_quantity)
        return allocations

    @staticmethod
    def _order_priority(order: ResidualOrder) -> tuple[int, str]:
        plan_id = order.order_id.rsplit(":", 1)[0]
        return (0, order.order_id)

    @staticmethod
    def _settlement_step(step_ids: tuple[str, ...], step_index: int, lag: int) -> str:
        target_index = step_index + lag
        if target_index < len(step_ids):
            return step_ids[target_index]
        return f"{step_ids[step_index]}+{lag}"


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
        pending: tuple[PendingSettlement, ...] = (),
        scenario: MarketScenario | None = None,
        current_value: float = 0.0,
        initial_value: float = 0.0,
        external_cash_base: float = 0.0,
        limits: RiskLimits | None = None,
    ) -> tuple[LimitViolation, ...]:
        assert scenario is not None and limits is not None
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
        for entry in pending:
            currency_exposures[entry.currency] = (
                currency_exposures.get(entry.currency, 0.0)
                + entry.local_amount * self._fx(scenario, entry.currency)
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
        pending: tuple[PendingSettlement, ...] = (),
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
        for entry in pending:
            total += entry.local_amount * self._fx(scenario, entry.currency)
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
