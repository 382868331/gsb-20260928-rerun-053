from __future__ import annotations

from dataclasses import dataclass, replace

from risk_platform.holdings import Holding
from risk_platform.paths import ActionPlan, PathStep, RiskLimits, StressPath
from risk_platform.pricing import CashPricer, EquityPricer, Pricer, ZeroCouponBondPricer
from risk_platform.results import (
    ActionFill,
    ActionReconciliation,
    Contribution,
    ExecutionState,
    LimitViolation,
    PathRunResult,
    PathScenarioResult,
    PathStepResult,
    PendingSettlement,
    PolicyPathResult,
    SettlementEvent,
    StepExecutionLedger,
    UnfilledOrder,
)
from risk_platform.scenarios import MarketScenario, MissingMarketDataError

HOLD_POLICY = "hold"
REBALANCE_POLICY = "rebalance"
IMMEDIATE_PREFIX = "action:"
CONSTRAINED_PREFIX = "constrained:"
DEFAULT_POLICIES = (HOLD_POLICY, REBALANCE_POLICY)


@dataclass(frozen=True, slots=True)
class PathEngine:
    """Replays paths under hold, rebalance and disposal policies.

    Immediate action policies assume full fills. Constrained policies use
    the manual market windows and keep executed proceeds out of usable cash
    until their settlement step.
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
        results = tuple(self._run_path(holdings, path, policies) for path in paths)
        return PathRunResult(run_id=run_id, results=results)

    def resume(
        self,
        run_id: str,
        holdings: list[Holding],
        paths: list[StressPath],
        previous: PathRunResult,
    ) -> PathRunResult:
        for path in paths:
            path.validate_holdings(holdings)
        results = tuple(
            self._resume_path(holdings, path, self._previous_for_path(path, previous))
            for path in paths
        )
        return PathRunResult(run_id=run_id, results=results)

    @staticmethod
    def _previous_for_path(
        path: StressPath, previous: PathRunResult
    ) -> PathScenarioResult | None:
        exact = [
            item
            for item in previous.results
            if item.path_id == path.path_id and item.path_version == path.version
        ]
        same_id = [item for item in previous.results if item.path_id == path.path_id]
        candidates = exact or same_id
        if not candidates:
            return None
        PathEngine._assert_history_unchanged(path, candidates)
        return candidates[0]

    def _run_path(
        self,
        holdings: list[Holding],
        path: StressPath,
        policies: tuple[str, ...] | None,
    ) -> PathScenarioResult:
        names = self._policy_names(path, policies)
        return self._build_path(holdings, path, names, {}, {})

    def _resume_path(
        self,
        holdings: list[Holding],
        path: StressPath,
        previous: PathScenarioResult | None,
    ) -> PathScenarioResult:
        names = self._policy_names(path, None)
        states: dict[str, ExecutionState] = {}
        history: dict[str, tuple[PathStepResult, ...]] = {}
        if previous is not None:
            for policy_result in previous.policies:
                state = policy_result.execution_state
                if state is None:
                    continue
                recorded = dict(state.historical_step_fingerprints)
                for step in path.steps[: state.completed_step_index + 1]:
                    old_fingerprint = recorded.get(step.step_id)
                    if old_fingerprint is not None and old_fingerprint != step.scenario.fingerprint:
                        raise ValueError(
                            f"Cannot resume {state.policy}: executed step {step.step_id} "
                            "market data was modified"
                        )
                states[state.policy] = state
                history[state.policy] = tuple(
                    policy_result.steps[: state.completed_step_index + 1]
                )
        return self._build_path(holdings, path, names, states, history)

    def _build_path(
        self,
        holdings: list[Holding],
        path: StressPath,
        names: tuple[str, ...],
        states: dict[str, ExecutionState],
        history: dict[str, tuple[PathStepResult, ...]],
    ) -> PathScenarioResult:
        policy_results = []
        missing_data = None
        for name in names:
            result, missing = self._replay(
                holdings, path, name, states.get(name), history.get(name, ())
            )
            policy_results.append(result)
            if missing is not None and missing_data is None:
                missing_data = missing
        first_trigger = next(
            (item.first_trigger_step_id for item in policy_results if item.first_trigger_step_id),
            None,
        )
        return PathScenarioResult(
            path_id=path.path_id,
            path_version=path.version,
            path_fingerprint=path.fingerprint,
            status="complete" if all(item.complete for item in policy_results) else "partial",
            missing_data=missing_data,
            policies=tuple(policy_results),
            first_trigger_step_id=first_trigger,
            trigger_basis_fingerprint=path.fingerprint if first_trigger else None,
        )

    @staticmethod
    def _policy_names(
        path: StressPath, requested: tuple[str, ...] | None
    ) -> tuple[str, ...]:
        action_names: tuple[str, ...] = ()
        for plan in path.action_plans:
            action_names += (
                f"{IMMEDIATE_PREFIX}{plan.plan_id}",
                f"{CONSTRAINED_PREFIX}{plan.plan_id}",
            )
        available = DEFAULT_POLICIES + action_names
        if requested is None:
            return available
        unknown = [name for name in requested if name not in available]
        if unknown:
            raise ValueError(f"Unknown policy: {', '.join(unknown)}")
        return tuple(name for name in available if name in requested)

    def _replay(
        self,
        holdings: list[Holding],
        path: StressPath,
        policy: str,
        state: ExecutionState | None,
        history: tuple[PathStepResult, ...],
    ):
        plan = self._action_plan(path, policy)
        if policy.startswith(CONSTRAINED_PREFIX):
            return self._replay_constrained(holdings, path, policy, plan, state, history)
        return self._replay_immediate(holdings, path, policy, plan, state, history)

    @staticmethod
    def _action_plan(path: StressPath, policy: str) -> ActionPlan | None:
        for prefix in (IMMEDIATE_PREFIX, CONSTRAINED_PREFIX):
            if policy.startswith(prefix):
                plan_id = policy.removeprefix(prefix)
                match = [plan for plan in path.action_plans if plan.plan_id == plan_id]
                return match[0] if match else None
        return None

    def _replay_immediate(
        self,
        holdings: list[Holding],
        path: StressPath,
        policy: str,
        action_plan: ActionPlan | None,
        resume_state: ExecutionState | None,
        historical_steps: tuple[PathStepResult, ...],
    ) -> tuple[PolicyPathResult, str | None]:
        quantities = {holding.id: holding.quantity for holding in holdings}
        cash: dict[str, float] = {}
        steps: list[PathStepResult] = list(historical_steps)
        missing = None
        first_trigger = resume_state.first_trigger_step_id if resume_state else None
        first_breaches = resume_state.first_breaches if resume_state else ()
        unresolved: tuple[LimitViolation, ...] = ()
        action_executed = bool(historical_steps)
        external_cash_base = resume_state.external_cash_base if resume_state else 0.0
        cumulative_fees = 0.0
        cumulative_filled = 0.0
        start_index = resume_state.completed_step_index + 1 if resume_state else 0
        if resume_state is not None:
            quantities = dict(resume_state.quantities)
            cash = dict(resume_state.available_cash)

        initial_step = path.steps[0]
        if start_index == 0:
            try:
                previous_total = self._total(
                    holdings, quantities, cash, {}, initial_step.scenario
                )
                initial_value = previous_total
            except MissingMarketDataError as error:
                return self._immediate_result(
                    policy, holdings, quantities, cash, initial_value=0.0,
                    previous_total=0.0, steps=steps, first_trigger=None,
                    first_breaches=(), unresolved=self._unconfirmed_limits(path.limits),
                    missing=str(error), external_cash_base=0.0,
                )
            initial_breaches = self._check_limits(
                holdings, quantities, cash, {}, initial_step.scenario,
                initial_step.scenario, initial_value, initial_value, 0.0, path.limits,
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
                unresolved = tuple(
                    item for item in initial_breaches if item.status != "within"
                )
            if (
                action_plan is not None
                and action_plan.trigger_step_id == initial_step.step_id
                and initial_breached
            ):
                quantities, cash, initial_value, reconciliation = self._execute_immediate(
                    holdings, quantities, cash, initial_step.scenario, action_plan, initial_value
                )
                previous_total = initial_value
                action_executed = True
                post_breaches = self._check_limits(
                    holdings, quantities, cash, {}, initial_step.scenario,
                    initial_step.scenario, initial_value, initial_value, 0.0, path.limits,
                )
                statuses = {item.metric: item.status for item in initial_breached}
                post_breaches = tuple(
                    replace(
                        item,
                        status="confirmed_within"
                        if item.status == "within" and statuses.get(item.metric) == "breached"
                        else item.status,
                    )
                    for item in post_breaches
                )
                unresolved = tuple(
                    item for item in post_breaches if item.status in {"breached", "unconfirmed"}
                )
            start_index = 1
        else:
            initial_value = resume_state.initial_value
            previous_total = resume_state.previous_total

        previous_scenario = path.steps[min(start_index - 1, len(path.steps) - 1)].scenario
        for index in range(start_index, len(path.steps)):
            step = path.steps[index]
            scenario = step.scenario
            try:
                result = self._immediate_step(
                    holdings, quantities, cash, previous_scenario, scenario, step,
                    policy, action_plan if not action_executed else None,
                    previous_total, initial_value, external_cash_base, path.limits,
                    cumulative_fees, cumulative_filled,
                )
            except MissingMarketDataError as error:
                missing = str(error)
                unresolved = self._unconfirmed_limits(path.limits)
                break
            step_result, quantities, cash, previous_total, breach_map, executed, \
                cumulative_fees, cumulative_filled = result
            external_cash_base += step_result.cash_flow_effect
            seen = {item.metric for item in first_breaches}
            new_breaches = [
                replace(item, trigger_step_id=step.step_id)
                for item in breach_map["pre"]
                if item.status == "breached" and item.metric not in seen
            ]
            if new_breaches:
                first_trigger = step.step_id
                first_breaches = first_breaches + tuple(new_breaches)
            unresolved = tuple(
                item
                for item in breach_map["post"]
                if item.status in {"breached", "unconfirmed"}
            )
            action_executed = action_executed or executed
            steps.append(step_result)
            previous_scenario = scenario

        if missing is not None and not unresolved:
            unresolved = self._unconfirmed_limits(path.limits)
        return self._immediate_result(
            policy, holdings, quantities, cash, initial_value, previous_total,
            tuple(steps), first_trigger, first_breaches, unresolved, missing,
            external_cash_base,
        )

    @staticmethod
    def _immediate_result(
        policy: str,
        holdings: list[Holding],
        quantities: dict[str, float],
        cash: dict[str, float],
        initial_value: float,
        previous_total: float,
        steps: tuple[PathStepResult, ...],
        first_trigger: str | None,
        first_breaches: tuple[LimitViolation, ...],
        unresolved: tuple[LimitViolation, ...],
        missing: str | None,
        external_cash_base: float,
    ) -> tuple[PolicyPathResult, str | None]:
        result = PolicyPathResult(
            policy=policy,
            complete=missing is None,
            initial_value=initial_value,
            final_value=previous_total,
            total_market_effect=sum(step.market_effect for step in steps),
            total_trade_effect=sum(step.trade_effect for step in steps),
            total_interaction_effect=sum(step.interaction_effect for step in steps),
            total_cash_flow_effect=sum(step.cash_flow_effect for step in steps),
            total_change=sum(step.total_change for step in steps),
            steps=steps,
            total_action_effect=sum(step.action_effect for step in steps),
            total_action_fees=sum(step.action_fees for step in steps),
            total_cash_flow_from_actions=sum(
                step.cash_flow_from_actions for step in steps
            ),
            first_trigger_step_id=first_trigger,
            first_breaches=first_breaches,
            unresolved_metrics=unresolved,
            execution_mode="immediate",
        )
        return result, missing

    def _constrained_step(
        self,
        holdings: list[Holding],
        path: StressPath,
        index: int,
        quantities: dict[str, float],
        cash: dict[str, float],
        pending: list[PendingSettlement],
        open_orders: list[UnfilledOrder],
        canceled: set[str],
        plan: ActionPlan,
        initial_value: float,
        external_cash_base: float,
        cumulative_fees: float,
        cumulative_slippage: float,
        cumulative_filled: float,
        cumulative_settled: float,
        cumulative_lag: float,
        limits: RiskLimits,
        first_breaches: tuple[LimitViolation, ...] | None = None,
        cumulative_written_off: float = 0.0,
        cumulative_extra_fees: float = 0.0,
        settlement_log: list[SettlementEvent] | None = None,
    ):
        step = path.steps[index]
        scenario = step.scenario
        previous_scenario = path.steps[index - 1].scenario if index else scenario
        start_pending_map = self._pending_by_currency(tuple(pending))
        start_previous_value = self._total(
            holdings, quantities, cash, start_pending_map, previous_scenario
        )
        market_value = self._total(
            holdings, quantities, cash, start_pending_map, scenario
        )
        market_effect = market_value - start_previous_value if index else 0.0

        new_cash = dict(cash)
        cash_flow_effect = 0.0
        for flow in step.cash_flows:
            cash_flow_effect += flow.amount * self._fx(scenario, flow.currency)
            new_cash[flow.currency] = new_cash.get(flow.currency, 0.0) + flow.amount

        if settlement_log is None:
            settlement_log = []
        step_events: list[SettlementEvent] = []
        settlement_effect = 0.0
        pending_settled_base = 0.0
        settlement_lag = 0.0

        # External settlement events: actual arrivals and rejections. Funds
        # only move when an event (or the scheduled settlement below) says
        # so; rejected amounts become disputed, never cash.
        for instruction in step.settlement_events:
            if instruction.plan_id != plan.plan_id:
                continue
            remaining = instruction.amount_local
            matched = False
            had_settled = False
            for position, item in enumerate(pending):
                if (
                    item.plan_id != instruction.plan_id
                    or item.order_id != instruction.order_id
                ):
                    continue
                matched = True
                had_settled = had_settled or item.settled_local > 1e-12
                if remaining <= 1e-12:
                    continue
                if instruction.kind == "settle":
                    updated, applied, base_at_fill, lag = self._settle_amount(
                        item, remaining, new_cash, scenario
                    )
                    if applied <= 1e-12:
                        continue
                    pending_settled_base += base_at_fill
                    settlement_lag += lag
                else:
                    applied = min(remaining, item.outstanding_local)
                    if applied <= 1e-12:
                        continue
                    updated = replace(
                        item, disputed_local=item.disputed_local + applied
                    )
                remaining -= applied
                event = SettlementEvent(
                    step_id=step.step_id,
                    plan_id=instruction.plan_id,
                    order_id=instruction.order_id,
                    kind=instruction.kind,
                    amount_local=instruction.amount_local,
                    applied_local=applied,
                    counterparty=instruction.counterparty,
                    note=instruction.note,
                )
                step_events.append(event)
                pending[position] = replace(
                    updated, events=updated.events + (event,)
                )
            if remaining > 1e-12:
                if instruction.kind == "settle":
                    kind = "duplicate_receipt" if matched else "stale_notice"
                else:
                    had_settled = had_settled or any(
                        entry.plan_id == instruction.plan_id
                        and entry.order_id == instruction.order_id
                        and entry.kind == "settle"
                        and entry.applied_local > 1e-12
                        for entry in settlement_log
                    )
                    kind = "late_rejection" if had_settled else "stale_notice"
                step_events.append(SettlementEvent(
                    step_id=step.step_id,
                    plan_id=instruction.plan_id,
                    order_id=instruction.order_id,
                    kind=kind,
                    amount_local=remaining,
                    applied_local=0.0,
                    counterparty=instruction.counterparty,
                    note=instruction.note,
                ))

        # Remediation decisions for disputed (rejected) proceeds.
        for action in step.settlement_actions:
            if action.plan_id != plan.plan_id:
                continue
            for position, item in enumerate(pending):
                if item.plan_id != action.plan_id or item.order_id != action.order_id:
                    continue
                disputed = item.disputed_local
                if disputed <= 1e-12:
                    continue
                fx_rate = self._fx(scenario, item.currency)
                if action.action == "write_off":
                    loss_base = disputed * fx_rate
                    updated = replace(
                        item,
                        disputed_local=0.0,
                        written_off_local=item.written_off_local + disputed,
                    )
                    cumulative_written_off += loss_base
                    settlement_effect -= loss_base
                    event = SettlementEvent(
                        step_id=step.step_id,
                        plan_id=action.plan_id,
                        order_id=action.order_id,
                        kind="write_off",
                        amount_local=disputed,
                        applied_local=disputed,
                        counterparty=item.counterparty,
                    )
                else:
                    fee_local = disputed * action.fee_bps / 10000.0
                    fee_base = fee_local * fx_rate
                    target_index = min(
                        index + action.settlement_steps, len(path.steps) - 1
                    )
                    updated = replace(
                        item,
                        disputed_local=0.0,
                        extra_fees_local=item.extra_fees_local + fee_local,
                        extra_fees_base=item.extra_fees_base + fee_base,
                        settlement_step_id=path.steps[target_index].step_id,
                        counterparty=action.counterparty,
                    )
                    cumulative_extra_fees += fee_base
                    settlement_effect -= fee_base
                    event = SettlementEvent(
                        step_id=step.step_id,
                        plan_id=action.plan_id,
                        order_id=action.order_id,
                        kind="reschedule",
                        amount_local=disputed,
                        applied_local=disputed,
                        counterparty=action.counterparty,
                    )
                step_events.append(event)
                pending[position] = replace(
                    updated, events=updated.events + (event,)
                )

        # Scheduled settlement: only the still-outstanding amount arrives.
        # Rejected, written-off or fee-consumed portions never become cash.
        for position, item in enumerate(pending):
            if (
                item.settlement_step_id != step.step_id
                or item.outstanding_local <= 1e-12
            ):
                continue
            updated, applied, base_at_fill, lag = self._settle_amount(
                item, item.outstanding_local, new_cash, scenario
            )
            pending_settled_base += base_at_fill
            settlement_lag += lag
            event = SettlementEvent(
                step_id=step.step_id,
                plan_id=item.plan_id,
                order_id=item.order_id,
                kind="settle",
                amount_local=applied,
                applied_local=applied,
                counterparty=item.counterparty,
                note="scheduled",
            )
            step_events.append(event)
            pending[position] = replace(updated, events=updated.events + (event,))

        settlement_log.extend(step_events)
        cumulative_settled += pending_settled_base
        cumulative_lag += settlement_lag
        remaining_pending = [
            item for item in pending if item.unresolved_local > 1e-12
        ]

        canceled_ids = []
        for cancellation in step.cancellations:
            if cancellation.plan_id != plan.plan_id:
                continue
            global_ids = self._cancel_order_ids(plan, open_orders, cancellation.order_id, canceled)
            canceled_ids.extend(global_ids)
            for global_id in global_ids:
                canceled.add(global_id)
            open_orders[:] = [
                item
                for item in open_orders
                if not self._order_matches_cancel(plan, item, cancellation.order_id)
            ]

        pre_value = self._total(
            holdings, quantities, new_cash, tuple(remaining_pending), scenario
        )
        pre_breaches = self._check_limits(
            holdings, quantities, new_cash, tuple(remaining_pending), scenario,
            previous_scenario, pre_value, initial_value,
            external_cash_base + cash_flow_effect, limits,
        )
        if first_breaches is not None:
            initial_hit_metrics = {item.metric for item in first_breaches}
            pre_breaches = tuple(
                replace(item, status="breached")
                if item.metric in initial_hit_metrics
                else item
                for item in pre_breaches
            )
        triggered = first_breaches is not None or any(
            item.status == "breached" for item in pre_breaches
        )
        if triggered and not open_orders and plan.trigger_step_id == step.step_id:
            open_orders.extend(self._initial_open_orders(plan))

        fills, added_pending, open_orders, fees, slippage, gross_cash, pending_added = \
            self._fill_open_orders(
                holdings, path, index, quantities, new_cash, remaining_pending,
                open_orders, step, scenario, plan,
            )
        cumulative_fees += fees
        cumulative_slippage += slippage
        cumulative_filled += gross_cash
        post_value = self._total(
            holdings, quantities, new_cash, tuple(remaining_pending + added_pending), scenario
        )
        action_effect = post_value - pre_value
        post_breaches = self._check_limits(
            holdings, quantities, new_cash, tuple(remaining_pending + added_pending),
            scenario, previous_scenario, post_value, initial_value,
            external_cash_base + cash_flow_effect, limits,
        )
        pre_status = {item.metric: item.status for item in pre_breaches}
        post_breaches = tuple(
            replace(
                item,
                status="confirmed_within"
                if item.status == "within" and pre_status.get(item.metric) == "breached"
                else item.status,
            )
            for item in post_breaches
        )
        total_change = (
            market_effect + cash_flow_effect + action_effect + settlement_effect
        )
        pending[:] = remaining_pending + added_pending
        end_unresolved_map = self._pending_by_currency(tuple(pending))
        end_pending_map = self._outstanding_by_currency(pending)
        end_disputed_map = self._disputed_by_currency(pending)
        portfolio_value = (
            post_value if index == 0 else start_previous_value + total_change
        )
        contributions = self._contributions(
            holdings, quantities, cash, start_pending_map, quantities, new_cash,
            end_unresolved_map, scenario, previous_scenario,
        )
        ledger = StepExecutionLedger(
            quantities=dict(quantities),
            available_cash={key: value for key, value in new_cash.items() if value},
            pending_cash_local=end_pending_map,
            pending_cash_base=sum(
                amount * self._fx(scenario, currency)
                for currency, amount in end_pending_map.items()
            ),
            cumulative_fees=cumulative_fees,
            cumulative_slippage=cumulative_slippage,
            cumulative_filled_base=cumulative_filled,
            cumulative_settled_base=cumulative_settled,
            cumulative_settlement_lag=cumulative_lag,
            open_orders=tuple(open_orders),
            disputed_cash_local=end_disputed_map,
            disputed_cash_base=sum(
                amount * self._fx(scenario, currency)
                for currency, amount in end_disputed_map.items()
            ),
            cumulative_written_off_base=cumulative_written_off,
            cumulative_settlement_extra_fees_base=cumulative_extra_fees,
            settlement_events=tuple(settlement_log),
        )
        reconciliation = ActionReconciliation(
            pre_action_value=pre_value,
            post_action_value=post_value,
            action_effect=action_effect,
            fees=fees,
            cash_flow_from_actions=gross_cash,
            fills=tuple(fills),
            slippage=slippage,
            requested_quantity=sum(item.requested_quantity for item in open_orders)
            + sum(item.quantity for item in fills),
            filled_quantity=sum(item.quantity for item in fills),
            unfilled_quantity=sum(item.unfilled_quantity for item in open_orders),
            pending_added_base_amount=pending_added,
            pending_settled_base_amount=pending_settled_base,
            canceled_orders=tuple(canceled_ids),
        )
        result = PathStepResult(
            step_id=step.step_id,
            scenario_id=scenario.scenario_id,
            scenario_version=scenario.version,
            scenario_fingerprint=scenario.fingerprint,
            market_effect=market_effect,
            trade_effect=0.0,
            interaction_effect=0.0,
            cash_flow_effect=cash_flow_effect,
            total_change=total_change,
            portfolio_value=portfolio_value,
            contributions=contributions,
            limit_checks=post_breaches,
            action_plan_id=plan.plan_id if (fills or canceled_ids) else None,
            action_effect=action_effect,
            action_fees=fees,
            cash_flow_from_actions=gross_cash,
            action_reconciliation=reconciliation if fills or canceled_ids else None,
            action_slippage=slippage,
            pending_settlements=tuple(added_pending),
            pending_settled_base_amount=pending_settled_base,
            canceled_action_order_ids=tuple(canceled_ids),
            execution_ledger=ledger,
            pre_limit_checks=pre_breaches,
            settlement_effect=settlement_effect,
            settlement_events=tuple(step_events),
        )
        cash.clear()
        cash.update(new_cash)
        return (
            result, quantities, cash, pending, open_orders, cumulative_fees,
            cumulative_slippage, cumulative_filled, cumulative_settled,
            cumulative_lag, cumulative_written_off, cumulative_extra_fees,
        )

    def _settle_amount(
        self,
        item: PendingSettlement,
        amount: float,
        cash: dict[str, float],
        scenario: MarketScenario,
    ):
        """Credit up to ``amount`` of a pending record as usable cash."""
        applied = min(amount, item.outstanding_local)
        if applied <= 1e-12:
            return item, 0.0, 0.0, 0.0
        updated = replace(item, settled_local=item.settled_local + applied)
        cash[item.currency] = cash.get(item.currency, 0.0) + applied
        ratio = (
            item.net_base_amount_at_fill / item.local_amount
            if item.local_amount
            else 0.0
        )
        base_at_fill = applied * ratio
        lag = applied * self._fx(scenario, item.currency) - base_at_fill
        return updated, applied, base_at_fill, lag

    def _initial_open_orders(self, plan: ActionPlan) -> list[UnfilledOrder]:
        orders = []
        for index, order in enumerate(plan.orders):
            order_id = order.order_id or str(index)
            requested = order.quantity if order.order_type == "sell" else order.from_amount
            orders.append(UnfilledOrder(
                plan_id=plan.plan_id,
                order_id=order_id,
                type=order.order_type,
                requested_quantity=requested,
                filled_quantity=0.0,
                unfilled_quantity=requested,
                holding_id=order.holding_id,
                from_currency=order.from_currency,
                to_currency=order.to_currency,
                status="open",
            ))
        return orders

    @staticmethod
    def _order_global_id(plan_id: str, order_id: str) -> str:
        return f"{plan_id}:{order_id}"

    def _cancel_order_ids(
        self,
        plan: ActionPlan,
        open_orders: list[UnfilledOrder],
        order_id: str | None,
        already_canceled: set[str],
    ) -> list[str]:
        if order_id is None:
            targets = [item.order_id for item in open_orders if item.plan_id == plan.plan_id]
        else:
            targets = [order_id]
        result = []
        for target in targets:
            global_id = self._order_global_id(plan.plan_id, target)
            if global_id not in already_canceled:
                result.append(global_id)
        return result

    @staticmethod
    def _order_matches_cancel(
        plan: ActionPlan, order: UnfilledOrder, order_id: str | None
    ) -> bool:
        return order.plan_id == plan.plan_id and (
            order_id is None or order.order_id == order_id
        )

    def _instrument_key(self, order: UnfilledOrder) -> str:
        if order.type == "sell":
            return f"sell:{order.holding_id}"
        return f"fx:{order.from_currency}->{order.to_currency}"

    def _fill_open_orders(
        self,
        holdings: list[Holding],
        path: StressPath,
        step_index: int,
        quantities: dict[str, float],
        cash: dict[str, float],
        pending: list[PendingSettlement],
        open_orders: list[UnfilledOrder],
        step: PathStep,
        scenario: MarketScenario,
        plan: ActionPlan,
    ):
        windows = {f"{window.kind}:{window.key}": window for window in step.fill_windows}
        holding_by_id = {holding.id: holding for holding in holdings}
        fills = []
        added_pending = []
        remaining_orders: list[UnfilledOrder] = []
        total_fees = total_slippage = total_gross = total_pending = 0.0
        ordered_groups: dict[str, list[UnfilledOrder]] = {}
        for order in open_orders:
            ordered_groups.setdefault(self._instrument_key(order), []).append(order)

        for key, orders in ordered_groups.items():
            window = windows.get(key)
            if window is None:
                remaining_orders.extend(orders)
                continue
            target_index = min(step_index + window.settlement_steps, len(path.steps) - 1)
            settlement_step_id = path.steps[target_index].step_id
            fee_bps_by_id = self._fee_bps_by_order(plan)
            requested = {order.order_id: order.unfilled_quantity for order in orders}
            available = {}
            for order in orders:
                if order.type == "sell":
                    available[order.order_id] = quantities.get(order.holding_id, 0.0)
                else:
                    available[order.order_id] = cash.get(order.from_currency, 0.0)
            allocations = self._allocate_capacity(
                plan.allocation, requested, available, window.capacity
            )
            pool_remaining = sum(available.values())
            for order_id in requested:
                allocations[order_id] = min(allocations[order_id], pool_remaining)
                pool_remaining -= allocations[order_id]
            for order in orders:
                allocated = allocations.get(order.order_id, 0.0)
                fee_bps = fee_bps_by_id[order.order_id]
                if order.type == "sell":
                    fill = self._fill_sell(
                        order, holding_by_id[order.holding_id], allocated, window,
                        fee_bps, plan, step, scenario,
                        settlement_step_id,
                    )
                else:
                    fill = self._fill_fx(
                        order, allocated, window, fee_bps, plan, step, scenario,
                        settlement_step_id,
                    )
                if fill is None:
                    remaining_orders.append(order)
                    continue
                fill_record, pending_record, fee, slippage, gross = fill
                if order.type == "sell":
                    quantities[order.holding_id] -= fill_record.quantity
                else:
                    cash[order.from_currency] = cash.get(order.from_currency, 0.0) - fill_record.quantity
                fills.append(fill_record)
                added_pending.append(pending_record)
                total_fees += fee
                total_slippage += slippage
                total_gross += gross
                total_pending += pending_record.net_base_amount_at_fill
                filled = order.filled_quantity + fill_record.quantity
                unfilled = order.requested_quantity - filled
                if unfilled > 1e-12:
                    remaining_orders.append(replace(
                        order,
                        filled_quantity=filled,
                        unfilled_quantity=unfilled,
                        status="partial",
                    ))
        return (
            fills, added_pending, remaining_orders, total_fees,
            total_slippage, total_gross, total_pending,
        )

    @staticmethod
    def _fee_bps_by_order(plan: ActionPlan) -> dict[str, float]:
        return {
            order.order_id or str(index): order.fee_bps
            for index, order in enumerate(plan.orders)
        }

    @staticmethod
    def _allocate_capacity(
        rule: str,
        requested: dict[str, float],
        available: dict[str, float],
        capacity: float,
    ) -> dict[str, float]:
        eligible = {key: min(amount, available.get(key, 0.0)) for key, amount in requested.items()}
        allocations = {key: 0.0 for key in requested}
        remaining_capacity = capacity
        if rule == "priority":
            for key in requested:
                allocated = min(eligible[key], remaining_capacity)
                allocations[key] = allocated
                remaining_capacity -= allocated
        else:
            total_eligible = sum(eligible.values())
            if total_eligible <= 0:
                return allocations
            for key in requested:
                share = eligible[key] / total_eligible * capacity
                allocations[key] = min(eligible[key], share)
                remaining_capacity -= allocations[key]
            if remaining_capacity > 1e-12:
                for key in requested:
                    room = eligible[key] - allocations[key]
                    extra = min(room, remaining_capacity)
                    allocations[key] += extra
                    remaining_capacity -= extra
        return allocations

    def _fill_sell(
        self,
        order: UnfilledOrder,
        holding: Holding,
        quantity: float,
        window,
        fee_bps: float,
        plan: ActionPlan,
        step: PathStep,
        scenario: MarketScenario,
        settlement_step_id: str,
    ):
        if quantity <= 1e-12:
            return None
        fill_price = window.price_for(plan.fill_price)
        local_gross = quantity * fill_price
        fx_rate = self._fx(scenario, holding.currency)
        gross_base = local_gross * fx_rate
        fee_base = gross_base * fee_bps / 10000.0
        fee_local = fee_base / fx_rate
        net_local = local_gross - fee_local
        net_base = gross_base - fee_base
        market_unit = self._unit_local_value(holding, scenario)
        slippage_base = quantity * (market_unit - fill_price) * fx_rate
        if slippage_base < 0:
            slippage_base = 0.0
        pending = PendingSettlement(
            type="sell",
            plan_id=plan.plan_id,
            order_id=order.order_id,
            fill_step_id=step.step_id,
            settlement_step_id=settlement_step_id,
            currency=holding.currency,
            local_amount=net_local,
            gross_base_amount=gross_base,
            fee_base_amount=fee_base,
            net_base_amount_at_fill=net_base,
            holding_id=holding.id,
        )
        fill = ActionFill(
            type="sell", holding_id=holding.id, quantity=quantity,
            from_currency=None, to_currency=holding.currency,
            local_amount=net_local, base_gross_amount=gross_base,
            fee_base_amount=fee_base, base_net_amount=net_base,
            plan_id=plan.plan_id, order_id=order.order_id,
            requested_quantity=order.requested_quantity,
            unfilled_quantity=max(0.0, order.unfilled_quantity - quantity),
            fill_step_id=step.step_id,
            settlement_step_id=settlement_step_id,
            window_price_low=window.price_low,
            window_price_high=window.price_high,
            fill_price=fill_price,
            slippage_base_amount=slippage_base,
            status="partial",
        )
        return fill, pending, fee_base, slippage_base, gross_base

    def _fill_fx(
        self,
        order: UnfilledOrder,
        from_amount: float,
        window,
        fee_bps: float,
        plan: ActionPlan,
        step: PathStep,
        scenario: MarketScenario,
        settlement_step_id: str,
    ):
        if from_amount <= 1e-12:
            return None
        deal_rate = window.price_for(plan.fill_price)
        gross_base = from_amount * self._fx(scenario, order.from_currency)
        fee_base = gross_base * fee_bps / 10000.0
        gross_to = from_amount * deal_rate
        fee_to_local = fee_base / self._fx(scenario, order.to_currency)
        net_to = gross_to - fee_to_local
        net_base = gross_base - fee_base
        market_rate = (
            self._fx(scenario, order.from_currency)
            / self._fx(scenario, order.to_currency)
        )
        slippage_base = from_amount * (market_rate - deal_rate) * self._fx(
            scenario, order.to_currency
        )
        if slippage_base < 0:
            slippage_base = 0.0
        pending = PendingSettlement(
            type="fx_conversion",
            plan_id=plan.plan_id,
            order_id=order.order_id,
            fill_step_id=step.step_id,
            settlement_step_id=settlement_step_id,
            currency=order.to_currency,
            local_amount=net_to,
            gross_base_amount=gross_base,
            fee_base_amount=fee_base,
            net_base_amount_at_fill=net_base,
        )
        fill = ActionFill(
            type="fx_conversion", holding_id=None, quantity=from_amount,
            from_currency=order.from_currency, to_currency=order.to_currency,
            local_amount=net_to, base_gross_amount=gross_base,
            fee_base_amount=fee_base, base_net_amount=net_base,
            plan_id=plan.plan_id, order_id=order.order_id,
            requested_quantity=order.requested_quantity,
            unfilled_quantity=max(0.0, order.unfilled_quantity - from_amount),
            fill_step_id=step.step_id,
            settlement_step_id=settlement_step_id,
            window_price_low=window.price_low,
            window_price_high=window.price_high,
            fill_price=deal_rate,
            slippage_base_amount=slippage_base,
            status="partial",
        )
        return fill, pending, fee_base, slippage_base, gross_base


    def _immediate_step(
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
        cumulative_fees: float,
        cumulative_filled: float,
    ):
        market_value = self._total(holdings, quantities, cash, {}, scenario)
        market_effect = market_value - previous_total
        cash_flow_effect = 0.0
        new_cash = dict(cash)
        for flow in step.cash_flows:
            cash_flow_effect += flow.amount * self._fx(scenario, flow.currency)
            new_cash[flow.currency] = new_cash.get(flow.currency, 0.0) + flow.amount

        pre_value = self._total(holdings, quantities, new_cash, {}, scenario)
        pre_breaches = self._check_limits(
            holdings, quantities, new_cash, {}, scenario, previous_scenario,
            pre_value, initial_value, external_cash_base + cash_flow_effect, limits,
        )
        triggered = any(item.status == "breached" for item in pre_breaches)
        reconciliation = None
        action_effect = action_fees = cash_flow_from_actions = 0.0
        action_plan_id = None
        executed = False

        if action_plan is not None and action_plan.trigger_step_id == step.step_id and triggered:
            action_plan_id = action_plan.plan_id
            new_quantities, post_cash, post_value, reconciliation = self._execute_immediate(
                holdings, quantities, new_cash, scenario, action_plan, pre_value
            )
            action_effect = reconciliation.action_effect
            action_fees = reconciliation.fees
            cash_flow_from_actions = reconciliation.cash_flow_from_actions
            cumulative_fees += action_fees
            cumulative_filled += cash_flow_from_actions
            executed = True
            trade_effect = interaction_effect = 0.0
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
            trade_effect = self._total(
                holdings, new_quantities, cash, {}, previous_scenario
            ) - previous_total
            interaction_effect = (
                self._total(holdings, new_quantities, cash, {}, scenario)
                - market_value
                - trade_effect
            )
        else:
            new_quantities = dict(quantities)
            post_cash = new_cash
            trade_effect = interaction_effect = 0.0

        post_value = self._total(holdings, new_quantities, post_cash, {}, scenario)
        post_breaches = self._check_limits(
            holdings, new_quantities, post_cash, {}, scenario, previous_scenario,
            post_value, initial_value, external_cash_base + cash_flow_effect, limits,
        )
        pre_status = {item.metric: item.status for item in pre_breaches}
        post_breaches = tuple(
            replace(
                item,
                status="confirmed_within"
                if item.status == "within" and pre_status.get(item.metric) == "breached"
                else item.status,
            )
            for item in post_breaches
        )
        total_change = (
            market_effect + trade_effect + interaction_effect
            + cash_flow_effect + action_effect
        )
        portfolio_value = previous_total + total_change
        contributions = self._contributions(
            holdings, quantities, cash, {}, new_quantities, post_cash, {},
            scenario, previous_scenario,
        )
        ledger = StepExecutionLedger(
            quantities=dict(new_quantities),
            available_cash={key: value for key, value in post_cash.items() if value},
            pending_cash_local={},
            pending_cash_base=0.0,
            cumulative_fees=cumulative_fees,
            cumulative_slippage=0.0,
            cumulative_filled_base=cumulative_filled,
            cumulative_settled_base=cumulative_filled,
            disputed_cash_local={},
        )
        result = PathStepResult(
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
            execution_ledger=ledger,
        )
        return result, new_quantities, post_cash, portfolio_value, {
            "pre": pre_breaches,
            "post": post_breaches,
        }, executed, cumulative_fees, cumulative_filled

    def _execute_immediate(
        self,
        holdings: list[Holding],
        quantities: dict[str, float],
        cash: dict[str, float],
        scenario: MarketScenario,
        plan: ActionPlan,
        pre_action_value: float,
    ):
        new_quantities = dict(quantities)
        new_cash = dict(cash)
        fills = []
        total_fees = 0.0
        total_gross_cash = 0.0
        holding_by_id = {holding.id: holding for holding in holdings}

        for index, order in enumerate(plan.orders):
            order_id = order.order_id or str(index)
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
                new_cash[holding.currency] -= fee / fx_rate
                total_fees += fee
                total_gross_cash += gross
                fills.append(ActionFill(
                    type="sell", holding_id=holding.id, quantity=quantity,
                    from_currency=None, to_currency=holding.currency,
                    local_amount=local_value, base_gross_amount=gross,
                    fee_base_amount=fee, base_net_amount=net,
                    plan_id=plan.plan_id, order_id=order_id,
                    requested_quantity=order.quantity, unfilled_quantity=order.quantity - quantity,
                    fill_price=self._unit_local_value(holding, scenario),
                    status="filled" if quantity == order.quantity else "partial",
                ))
            else:
                available = new_cash.get(order.from_currency, 0.0)
                local_amount = min(order.from_amount, available)
                if local_amount <= 0:
                    raise ValueError(
                        f"Action plan {plan.plan_id} cannot convert {order.from_currency}; none available"
                    )
                from_fx = self._fx(scenario, order.from_currency)
                to_fx = self._fx(scenario, order.to_currency)
                gross = local_amount * from_fx
                fee = gross * order.fee_bps / 10000.0
                received_base = gross - fee
                received_local = received_base / to_fx
                new_cash[order.from_currency] = available - local_amount
                new_cash[order.to_currency] = (
                    new_cash.get(order.to_currency, 0.0) + received_local
                )
                total_fees += fee
                fills.append(ActionFill(
                    type="fx_conversion", holding_id=None, quantity=local_amount,
                    from_currency=order.from_currency, to_currency=order.to_currency,
                    local_amount=local_amount, base_gross_amount=gross,
                    fee_base_amount=fee, base_net_amount=received_base,
                    plan_id=plan.plan_id, order_id=order_id,
                    requested_quantity=order.from_amount,
                    unfilled_quantity=order.from_amount - local_amount,
                    fill_price=from_fx / to_fx,
                    status="filled" if local_amount == order.from_amount else "partial",
                ))

        post_value = self._total(holdings, new_quantities, new_cash, {}, scenario)
        reconciliation = ActionReconciliation(
            pre_action_value=pre_action_value,
            post_action_value=post_value,
            action_effect=post_value - pre_action_value,
            fees=total_fees,
            cash_flow_from_actions=total_gross_cash,
            fills=tuple(fills),
            requested_quantity=sum(fill.requested_quantity or 0 for fill in fills),
            filled_quantity=sum(fill.quantity for fill in fills),
            unfilled_quantity=sum(fill.unfilled_quantity or 0 for fill in fills),
        )
        return new_quantities, new_cash, post_value, reconciliation

    def _fx(self, scenario: MarketScenario, currency: str) -> float:
        try:
            return scenario.fx_rate(currency)
        except KeyError as error:
            raise MissingMarketDataError(
                f"Scenario {scenario.scenario_id} v{scenario.version} lacks fx_rate:{currency}"
            ) from error

    def _check_limits(
        self,
        holdings: list[Holding],
        quantities: dict[str, float],
        cash: dict[str, float],
        pending: tuple[PendingSettlement, ...],
        scenario: MarketScenario,
        previous_scenario: MarketScenario,
        current_value: float,
        initial_value: float,
        external_cash_base: float,
        limits: RiskLimits,
    ) -> tuple[LimitViolation, ...]:
        checks = []
        if limits.max_loss is not None:
            loss = max(0.0, initial_value - current_value + external_cash_base)
            checks.append(LimitViolation(
                metric="loss", limit_value=limits.max_loss,
                observed_value=loss, excess=max(0.0, loss - limits.max_loss),
                source="portfolio",
                status="breached" if loss > limits.max_loss else "within",
            ))

        currency_exposures = {}
        holding_values = {}
        type_values = {}
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
        for item in pending:
            currency_exposures[item.currency] = (
                currency_exposures.get(item.currency, 0.0)
                + item.unresolved_local * self._fx(scenario, item.currency)
            )

        for currency, threshold in limits.currency_exposure.items():
            observed = currency_exposures.get(currency, 0.0)
            checks.append(LimitViolation(
                metric=f"currency_exposure:{currency}", limit_value=threshold,
                observed_value=observed, excess=max(0.0, observed - threshold),
                source=f"currency:{currency}", source_currency=currency,
                status="breached" if observed > threshold else "within",
            ))

        for key, threshold in limits.asset_concentration.items():
            observed_value = holding_values.get(key, type_values.get(key, 0.0))
            ratio = observed_value / current_value if current_value else 0.0
            checks.append(LimitViolation(
                metric=f"asset_concentration:{key}", limit_value=threshold,
                observed_value=ratio, excess=max(0.0, ratio - threshold),
                source=f"asset:{key}",
                source_holding_id=key if key in holding_values else None,
                status="breached" if ratio > threshold else "within",
            ))
        return tuple(checks)

    @staticmethod
    def _unconfirmed_limits(limits: RiskLimits) -> tuple[LimitViolation, ...]:
        checks = []
        if limits.max_loss is not None:
            checks.append(LimitViolation(
                metric="loss", limit_value=limits.max_loss, observed_value=None,
                excess=None, source="portfolio", status="unconfirmed",
            ))
        for currency, threshold in limits.currency_exposure.items():
            checks.append(LimitViolation(
                metric=f"currency_exposure:{currency}", limit_value=threshold,
                observed_value=None, excess=None, source=f"currency:{currency}",
                source_currency=currency, status="unconfirmed",
            ))
        for key, threshold in limits.asset_concentration.items():
            checks.append(LimitViolation(
                metric=f"asset_concentration:{key}", limit_value=threshold,
                observed_value=None, excess=None, source=f"asset:{key}",
                source_holding_id=key, status="unconfirmed",
            ))
        return tuple(checks)

    def _holding_value(self, holding: Holding, quantity: float, scenario: MarketScenario) -> float:
        sized = replace(holding, quantity=quantity)
        local_value = self.pricers[holding.asset_type].price_in_currency(sized, scenario)
        return local_value * self._fx(scenario, holding.currency)

    def _unit_local_value(self, holding: Holding, scenario: MarketScenario) -> float:
        return self.pricers[holding.asset_type].price_in_currency(
            replace(holding, quantity=1), scenario
        )

    def _total(
        self,
        holdings: list[Holding],
        quantities: dict[str, float],
        cash: dict[str, float],
        pending: tuple[PendingSettlement, ...] | dict[str, float],
        scenario: MarketScenario,
    ) -> float:
        total = 0.0
        for holding in holdings:
            try:
                local_value = self.pricers[holding.asset_type].price_in_currency(
                    replace(holding, quantity=quantities[holding.id]), scenario
                )
            except KeyError as error:
                raise MissingMarketDataError(
                    f"Scenario {scenario.scenario_id} v{scenario.version} lacks market data for {holding.id}: {error}"
                ) from error
            total += local_value * self._fx(scenario, holding.currency)
        for currency, amount in cash.items():
            total += amount * self._fx(scenario, currency)
        if isinstance(pending, dict):
            for currency, amount in pending.items():
                total += amount * self._fx(scenario, currency)
        else:
            for item in pending:
                total += item.unresolved_local * self._fx(scenario, item.currency)
        return total

    @staticmethod
    def _pending_by_currency(
        pending: tuple[PendingSettlement, ...],
    ) -> dict[str, float]:
        totals = {}
        for item in pending:
            totals[item.currency] = (
                totals.get(item.currency, 0.0) + item.unresolved_local
            )
        return totals

    @staticmethod
    def _outstanding_by_currency(
        pending: tuple[PendingSettlement, ...] | list[PendingSettlement],
    ) -> dict[str, float]:
        totals = {}
        for item in pending:
            if item.outstanding_local > 1e-12:
                totals[item.currency] = (
                    totals.get(item.currency, 0.0) + item.outstanding_local
                )
        return totals

    @staticmethod
    def _disputed_by_currency(
        pending: tuple[PendingSettlement, ...] | list[PendingSettlement],
    ) -> dict[str, float]:
        totals = {}
        for item in pending:
            if item.disputed_local > 1e-12:
                totals[item.currency] = (
                    totals.get(item.currency, 0.0) + item.disputed_local
                )
        return totals

    def _contributions(
        self,
        holdings: list[Holding],
        previous_quantities: dict[str, float],
        previous_cash: dict[str, float],
        previous_pending: dict[str, float],
        quantities: dict[str, float],
        cash: dict[str, float],
        pending: dict[str, float],
        scenario: MarketScenario,
        previous_scenario: MarketScenario,
    ) -> tuple[Contribution, ...]:
        contributions = []
        for holding in holdings:
            pricer = self.pricers[holding.asset_type]
            local_value = pricer.price_in_currency(
                replace(holding, quantity=quantities[holding.id]), scenario
            )
            previous_local = pricer.price_in_currency(
                replace(holding, quantity=previous_quantities[holding.id]),
                previous_scenario,
            )
            contributions.append(Contribution(
                holding_id=holding.id,
                name=holding.name,
                asset_type=holding.asset_type,
                currency=holding.currency,
                local_value=local_value,
                fx_rate=self._fx(scenario, holding.currency),
                base_value=local_value * self._fx(scenario, holding.currency),
                base_scenario_value=previous_local
                * previous_scenario.fx_rate(holding.currency),
                pnl_vs_base=local_value * self._fx(scenario, holding.currency)
                - previous_local * previous_scenario.fx_rate(holding.currency),
            ))
        currencies = set(cash) | set(pending) | set(previous_cash) | set(previous_pending)
        for currency in currencies:
            amount = cash.get(currency, 0.0)
            pending_amount = pending.get(currency, 0.0)
            previous_amount = previous_cash.get(currency, 0.0)
            previous_pending_amount = previous_pending.get(currency, 0.0)
            total_amount = amount + pending_amount
            prior_total = previous_amount + previous_pending_amount
            label = "settling-cash" if pending_amount else "reinvested-cash"
            if total_amount:
                contributions.append(Contribution(
                    holding_id=f"{label}:{currency}",
                    name=f"{label.title()} {currency}",
                    asset_type="cash",
                    currency=currency,
                    local_value=total_amount,
                    fx_rate=self._fx(scenario, currency),
                    base_value=total_amount * self._fx(scenario, currency),
                    base_scenario_value=prior_total
                    * previous_scenario.fx_rate(currency),
                    pnl_vs_base=total_amount * self._fx(scenario, currency)
                    - prior_total * previous_scenario.fx_rate(currency),
                ))
        return tuple(contributions)

    def _replay_constrained(
        self,
        holdings: list[Holding],
        path: StressPath,
        policy: str,
        plan: ActionPlan | None,
        resume_state: ExecutionState | None,
        historical_steps: tuple[PathStepResult, ...],
    ) -> tuple[PolicyPathResult, str | None]:
        if plan is None:
            return self._replay_immediate(
                holdings, path, policy, None, resume_state, historical_steps
            )
        if resume_state is not None:
            quantities = dict(resume_state.quantities)
            cash = dict(resume_state.available_cash)
            pending = list(resume_state.pending)
            open_orders = list(resume_state.open_orders)
            canceled = set(resume_state.canceled_order_ids)
            cumulative_fees = resume_state.cumulative_fees
            cumulative_slippage = resume_state.cumulative_slippage
            cumulative_filled = resume_state.cumulative_filled_base
            cumulative_settled = resume_state.cumulative_settled_base
            cumulative_lag = resume_state.cumulative_settlement_lag
            cumulative_written_off = resume_state.cumulative_written_off_base
            cumulative_extra_fees = (
                resume_state.cumulative_settlement_extra_fees_base
            )
            settlement_log = list(resume_state.settlement_log)
            external_cash_base = resume_state.external_cash_base
            initial_value = resume_state.initial_value
            previous_total = resume_state.previous_total
            first_trigger = resume_state.first_trigger_step_id
            first_breaches = resume_state.first_breaches
            steps = list(historical_steps)
            start_index = resume_state.completed_step_index + 1
            missing = None
            unresolved: tuple[LimitViolation, ...] = ()
        else:
            quantities = {holding.id: holding.quantity for holding in holdings}
            cash = {}
            pending = []
            open_orders = []
            canceled = set()
            cumulative_fees = cumulative_slippage = cumulative_filled = 0.0
            cumulative_settled = external_cash_base = cumulative_lag = 0.0
            cumulative_written_off = cumulative_extra_fees = 0.0
            settlement_log: list[SettlementEvent] = []
            first_trigger = None
            first_breaches = ()
            steps = []
            unresolved = ()
            missing = None
            initial_step = path.steps[0]
            try:
                initial_value = self._total(holdings, quantities, cash, (), initial_step.scenario)
                previous_total = initial_value
                initial_breaches = self._check_limits(
                    holdings, quantities, cash, (), initial_step.scenario,
                    initial_step.scenario, initial_value, initial_value, 0.0, path.limits,
                )
            except MissingMarketDataError as error:
                return self._constrained_result(
                    policy, holdings, quantities, cash, pending, open_orders,
                    canceled, 0, initial_value=0.0, previous_total=0.0,
                    steps=(), first_trigger=None, first_breaches=(),
                    unresolved=self._unconfirmed_limits(path.limits),
                    missing=str(error), cumulative_fees=0.0,
                    cumulative_slippage=0.0, cumulative_filled=0.0,
                    cumulative_settled=0.0, cumulative_lag=0.0,
                    cumulative_written_off=0.0, cumulative_extra_fees=0.0,
                    settlement_log=(),
                    external_cash_base=0.0, plan=plan,
                )
            initial_hits = tuple(item for item in initial_breaches if item.status == "breached")
            post_breaches = initial_breaches
            if initial_hits:
                first_trigger = initial_step.step_id
                first_breaches = tuple(
                    replace(item, trigger_step_id=initial_step.step_id) for item in initial_hits
                )
                step_result, quantities, cash, pending, open_orders, cumulative_fees, \
                    cumulative_slippage, cumulative_filled, cumulative_settled, \
                    cumulative_lag, cumulative_written_off, cumulative_extra_fees = \
                    self._constrained_step(
                        holdings, path, 0, quantities, cash, pending, open_orders,
                        canceled, plan, initial_value, 0.0, cumulative_fees,
                        cumulative_slippage, cumulative_filled, cumulative_settled,
                        cumulative_lag,
                        path.limits, first_breaches=initial_hits,
                        cumulative_written_off=cumulative_written_off,
                        cumulative_extra_fees=cumulative_extra_fees,
                        settlement_log=settlement_log,
                    )
                previous_total = step_result.portfolio_value
                post_breaches = step_result.limit_checks
                steps.append(step_result)
            unresolved = tuple(
                item for item in post_breaches if item.status in {"breached", "unconfirmed"}
            )
            start_index = 1

        previous_scenario = path.steps[min(start_index - 1, len(path.steps) - 1)].scenario
        for index in range(start_index, len(path.steps)):
            try:
                step_result, quantities, cash, pending, open_orders, cumulative_fees, \
                    cumulative_slippage, cumulative_filled, cumulative_settled, \
                    cumulative_lag, cumulative_written_off, cumulative_extra_fees = \
                    self._constrained_step(
                        holdings, path, index, quantities, cash, pending, open_orders,
                        canceled, plan, initial_value, external_cash_base,
                        cumulative_fees, cumulative_slippage, cumulative_filled,
                        cumulative_settled, cumulative_lag, path.limits,
                        cumulative_written_off=cumulative_written_off,
                        cumulative_extra_fees=cumulative_extra_fees,
                        settlement_log=settlement_log,
                    )
            except MissingMarketDataError as error:
                missing = str(error)
                unresolved = self._unconfirmed_limits(path.limits)
                break
            external_cash_base += step_result.cash_flow_effect
            previous_total = step_result.portfolio_value
            seen = {item.metric for item in first_breaches}
            new_breaches = [
                replace(item, trigger_step_id=path.steps[index].step_id)
                for item in step_result.pre_limit_checks
                if item.status == "breached" and item.metric not in seen
            ]
            if new_breaches:
                first_trigger = path.steps[index].step_id
                first_breaches = first_breaches + tuple(new_breaches)
            unresolved = tuple(
                item
                for item in step_result.limit_checks
                if item.status in {"breached", "unconfirmed"}
            )
            steps.append(step_result)
            previous_scenario = path.steps[index].scenario

        completed_index = len(steps) - 1
        if missing is not None and not unresolved:
            unresolved = self._unconfirmed_limits(path.limits)
        return self._constrained_result(
            policy, holdings, quantities, cash, pending, open_orders, canceled,
            completed_index, initial_value, previous_total, tuple(steps),
            first_trigger, first_breaches, unresolved, missing, cumulative_fees,
            cumulative_slippage, cumulative_filled, cumulative_settled,
            cumulative_lag,
            cumulative_written_off, cumulative_extra_fees, tuple(settlement_log),
            external_cash_base, plan,
        )

    def _constrained_result(
        self,
        policy: str,
        holdings: list[Holding],
        quantities: dict[str, float],
        cash: dict[str, float],
        pending: list[PendingSettlement],
        open_orders: list[UnfilledOrder],
        canceled: set[str],
        completed_index: int,
        initial_value: float,
        previous_total: float,
        steps: tuple[PathStepResult, ...],
        first_trigger: str | None,
        first_breaches: tuple[LimitViolation, ...],
        unresolved: tuple[LimitViolation, ...],
        missing: str | None,
        cumulative_fees: float,
        cumulative_slippage: float,
        cumulative_filled: float,
        cumulative_settled: float,
        cumulative_lag: float,
        cumulative_written_off: float,
        cumulative_extra_fees: float,
        settlement_log: tuple[SettlementEvent, ...],
        external_cash_base: float,
        plan: ActionPlan,
    ) -> tuple[PolicyPathResult, str | None]:
        fingerprints = tuple(
            (step.step_id, step.scenario_fingerprint) for step in steps
        )
        state = ExecutionState(
            policy=policy,
            plan_id=plan.plan_id,
            mode="constrained",
            completed_step_index=completed_index,
            quantities=dict(quantities),
            available_cash=dict(cash),
            pending=tuple(pending),
            open_orders=tuple(open_orders),
            canceled_order_ids=tuple(sorted(canceled)),
            cumulative_fees=cumulative_fees,
            cumulative_slippage=cumulative_slippage,
            cumulative_filled_base=cumulative_filled,
            cumulative_settled_base=cumulative_settled,
            cumulative_settlement_lag=cumulative_lag,
            external_cash_base=external_cash_base,
            initial_value=initial_value,
            previous_total=previous_total,
            first_trigger_step_id=first_trigger,
            first_breaches=first_breaches,
            historical_step_fingerprints=fingerprints,
            cumulative_written_off_base=cumulative_written_off,
            cumulative_settlement_extra_fees_base=cumulative_extra_fees,
            settlement_log=settlement_log,
        )
        result = PolicyPathResult(
            policy=policy,
            complete=missing is None,
            initial_value=initial_value,
            final_value=previous_total,
            total_market_effect=sum(step.market_effect for step in steps),
            total_trade_effect=0.0,
            total_interaction_effect=0.0,
            total_cash_flow_effect=sum(step.cash_flow_effect for step in steps),
            total_change=sum(step.total_change for step in steps),
            steps=steps,
            total_action_effect=sum(step.action_effect for step in steps),
            total_action_fees=sum(step.action_fees for step in steps),
            total_cash_flow_from_actions=sum(
                step.cash_flow_from_actions for step in steps
            ),
            first_trigger_step_id=first_trigger,
            first_breaches=first_breaches,
            unresolved_metrics=unresolved,
            execution_mode="constrained",
            execution_state=state,
            total_settlement_effect=sum(
                step.settlement_effect for step in steps
            ),
        )
        return result, missing

    @staticmethod
    def _assert_history_unchanged(
        path: StressPath, candidates: list[PathScenarioResult]
    ) -> None:
        for candidate in candidates:
            for policy in candidate.policies:
                state = policy.execution_state
                if state is None:
                    continue
                recorded = dict(state.historical_step_fingerprints)
                for step in path.steps[: state.completed_step_index + 1]:
                    old_fingerprint = recorded.get(step.step_id)
                    if old_fingerprint is not None and old_fingerprint != step.scenario.fingerprint:
                        raise ValueError(
                            f"Cannot resume {state.policy}: executed step {step.step_id} "
                            "market data was modified"
                        )
