from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any


@dataclass(frozen=True, slots=True)
class Contribution:
    holding_id: str
    name: str
    asset_type: str
    currency: str
    local_value: float
    fx_rate: float
    base_value: float
    base_scenario_value: float
    pnl_vs_base: float


@dataclass(frozen=True, slots=True)
class FactorEffect:
    holding_id: str
    factors: tuple[str, ...]
    effect: float


@dataclass(frozen=True, slots=True)
class ScenarioResult:
    scenario_id: str
    scenario_version: int
    scenario_fingerprint: str
    total_base_value: float
    total_pnl_vs_base: float
    contributions: tuple[Contribution, ...]
    factor_effects: tuple[FactorEffect, ...] = ()


@dataclass(frozen=True, slots=True)
class RunResult:
    run_id: str
    base_scenario_id: str
    base_scenario_version: int
    base_scenario_fingerprint: str
    results: tuple[ScenarioResult, ...]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class ResultStore:
    def __init__(self, directory: str | Path):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)

    def path_for(self, run_id: str) -> Path:
        safe_run_id = Path(run_id).name
        if safe_run_id != run_id or not run_id:
            raise ValueError("Invalid run_id")
        return self.directory / f"{safe_run_id}.json"

    def save(self, result: RunResult) -> Path:
        destination = self.path_for(result.run_id)
        if destination.exists():
            raise FileExistsError(f"Result already exists: {destination}")

        payload = json.dumps(result.to_dict(), ensure_ascii=False, indent=2, sort_keys=True)
        fd, temporary_name = tempfile.mkstemp(
            prefix=f".{result.run_id}.", suffix=".json.tmp", dir=self.directory
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(payload)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary_name, destination)
        except BaseException:
            try:
                os.unlink(temporary_name)
            except FileNotFoundError:
                pass
            raise
        return destination

    def load(self, run_id: str) -> RunResult:
        with self.path_for(run_id).open("r", encoding="utf-8") as handle:
            payload = json.load(handle)
        return run_result_from_dict(payload)


def run_result_from_dict(payload: dict[str, Any]) -> RunResult:
    scenario_results = tuple(
        ScenarioResult(
            scenario_id=item["scenario_id"],
            scenario_version=int(item["scenario_version"]),
            scenario_fingerprint=item["scenario_fingerprint"],
            total_base_value=float(item["total_base_value"]),
            total_pnl_vs_base=float(item["total_pnl_vs_base"]),
            contributions=tuple(Contribution(**entry) for entry in item["contributions"]),
            factor_effects=tuple(
                FactorEffect(
                    holding_id=entry["holding_id"],
                    factors=tuple(entry["factors"]),
                    effect=float(entry["effect"]),
                )
                for entry in item.get("factor_effects", ())
            ),
        )
        for item in payload["results"]
    )
    return RunResult(
        run_id=payload["run_id"],
        base_scenario_id=payload["base_scenario_id"],
        base_scenario_version=int(payload["base_scenario_version"]),
        base_scenario_fingerprint=payload["base_scenario_fingerprint"],
        results=scenario_results,
    )


def with_relative_values(
    contributions: tuple[Contribution, ...],
    base_values: dict[str, float],
) -> tuple[Contribution, ...]:
    return tuple(
        replace(
            contribution,
            base_scenario_value=base_values[contribution.holding_id],
            pnl_vs_base=contribution.base_value - base_values[contribution.holding_id],
        )
        for contribution in contributions
    )


@dataclass(frozen=True, slots=True)
class PathStepResult:
    """One step of a path replay, explaining the move from the previous step."""

    step_id: str
    scenario_id: str
    scenario_version: int
    scenario_fingerprint: str
    market_effect: float
    trade_effect: float
    interaction_effect: float
    cash_flow_effect: float
    total_change: float
    portfolio_value: float
    contributions: tuple[Contribution, ...]
    limit_checks: tuple["LimitViolation", ...] = ()
    action_plan_id: str | None = None
    action_effect: float = 0.0
    action_fees: float = 0.0
    cash_flow_from_actions: float = 0.0
    action_reconciliation: "ActionReconciliation | None" = None
    action_slippage: float = 0.0
    pending_settlements: tuple[PendingSettlement, ...] = ()
    pending_settled_base_amount: float = 0.0
    canceled_action_order_ids: tuple[str, ...] = ()
    execution_ledger: "StepExecutionLedger | None" = None
    pre_limit_checks: tuple["LimitViolation", ...] = ()
    settlement_effect: float = 0.0
    settlement_events: tuple[SettlementEvent, ...] = ()


@dataclass(frozen=True, slots=True)
class LimitViolation:
    metric: str
    limit_value: float | None
    observed_value: float | None
    excess: float | None
    source: str
    source_currency: str | None = None
    source_holding_id: str | None = None
    status: str = "breached"
    trigger_step_id: str | None = None


@dataclass(frozen=True, slots=True)
class ActionFill:
    type: str
    holding_id: str | None
    quantity: float
    from_currency: str | None
    to_currency: str | None
    local_amount: float
    base_gross_amount: float
    fee_base_amount: float
    base_net_amount: float
    plan_id: str | None = None
    order_id: str | None = None
    requested_quantity: float | None = None
    unfilled_quantity: float | None = None
    fill_step_id: str | None = None
    settlement_step_id: str | None = None
    window_price_low: float | None = None
    window_price_high: float | None = None
    fill_price: float | None = None
    slippage_base_amount: float = 0.0
    status: str = "filled"


@dataclass(frozen=True, slots=True)
class ActionReconciliation:
    pre_action_value: float
    post_action_value: float
    action_effect: float
    fees: float
    cash_flow_from_actions: float
    fills: tuple[ActionFill, ...]
    slippage: float = 0.0
    requested_quantity: float = 0.0
    filled_quantity: float = 0.0
    unfilled_quantity: float = 0.0
    pending_added_base_amount: float = 0.0
    pending_settled_base_amount: float = 0.0
    canceled_orders: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class PendingSettlement:
    """Executed proceeds whose funds remain unusable until settlement."""

    type: str
    plan_id: str
    order_id: str
    fill_step_id: str
    settlement_step_id: str
    currency: str
    local_amount: float
    gross_base_amount: float
    fee_base_amount: float
    net_base_amount_at_fill: float
    holding_id: str | None = None
    settled_local: float = 0.0
    disputed_local: float = 0.0
    written_off_local: float = 0.0
    extra_fees_local: float = 0.0
    extra_fees_base: float = 0.0
    counterparty: str | None = None
    event_driven_settlement: bool = False
    events: tuple["SettlementEvent", ...] = ()

    @property
    def outstanding_local(self) -> float:
        """Amount still expected to arrive and become usable cash."""
        return (
            self.local_amount
            - self.settled_local
            - self.disputed_local
            - self.written_off_local
            - self.extra_fees_local
        )

    @property
    def unresolved_local(self) -> float:
        """Amount still counted in exposure: outstanding plus disputed."""
        return self.outstanding_local + self.disputed_local


@dataclass(frozen=True, slots=True)
class SettlementEvent:
    """Evidence of one settlement-stage observation or decision.

    ``kind`` is one of ``settle`` (actual arrival), ``reject`` (portion
    refused), ``reschedule`` (re-routed to a new counterparty),
    ``write_off`` (realized loss), ``duplicate_receipt`` (arrival beyond the
    outstanding amount, not credited), ``late_rejection`` (refusal arriving
    after the funds were already settled or otherwise resolved) and
    ``counterparty_mismatch`` (notice from a party other than the current
    settlement counterparty) and ``stale_notice`` (notice for an order with
    nothing left unresolved).
    ``applied_local`` is the amount that actually changed state.
    """

    step_id: str
    plan_id: str
    order_id: str
    kind: str
    amount_local: float
    applied_local: float
    counterparty: str | None = None
    note: str | None = None


@dataclass(frozen=True, slots=True)
class UnfilledOrder:
    plan_id: str
    order_id: str
    type: str
    requested_quantity: float
    filled_quantity: float
    unfilled_quantity: float
    holding_id: str | None = None
    from_currency: str | None = None
    to_currency: str | None = None
    status: str = "open"


@dataclass(frozen=True, slots=True)
class StepExecutionLedger:
    """End-of-step positions, usable funds and action accounting."""

    quantities: dict[str, float]
    available_cash: dict[str, float]
    pending_cash_local: dict[str, float]
    pending_cash_base: float
    cumulative_fees: float
    cumulative_slippage: float
    cumulative_filled_base: float
    cumulative_settled_base: float
    cumulative_settlement_lag: float = 0.0
    open_orders: tuple[UnfilledOrder, ...] = ()
    disputed_cash_local: dict[str, float] | None = None
    disputed_cash_base: float = 0.0
    cumulative_written_off_base: float = 0.0
    cumulative_settlement_extra_fees_base: float = 0.0
    settlement_events: tuple[SettlementEvent, ...] = ()


@dataclass(frozen=True, slots=True)
class PolicyPathResult:
    """Replay of one path under one policy (hold or rebalance)."""

    policy: str
    complete: bool
    initial_value: float
    final_value: float
    total_market_effect: float
    total_trade_effect: float
    total_interaction_effect: float
    total_cash_flow_effect: float
    total_change: float
    steps: tuple[PathStepResult, ...]
    total_action_effect: float = 0.0
    total_action_fees: float = 0.0
    total_cash_flow_from_actions: float = 0.0
    first_trigger_step_id: str | None = None
    first_breaches: tuple[LimitViolation, ...] = ()
    unresolved_metrics: tuple[LimitViolation, ...] = ()
    execution_mode: str = "immediate"
    execution_state: "ExecutionState | None" = None
    total_settlement_effect: float = 0.0


@dataclass(frozen=True, slots=True)
class ExecutionState:
    """Serializable replay state; executed history never changes on resume."""

    policy: str
    plan_id: str | None
    mode: str
    completed_step_index: int
    quantities: dict[str, float]
    available_cash: dict[str, float]
    pending: tuple[PendingSettlement, ...]
    open_orders: tuple[UnfilledOrder, ...]
    canceled_order_ids: tuple[str, ...]
    cumulative_fees: float
    cumulative_slippage: float
    cumulative_filled_base: float
    cumulative_settled_base: float
    cumulative_settlement_lag: float
    external_cash_base: float
    initial_value: float
    previous_total: float
    first_trigger_step_id: str | None = None
    first_breaches: tuple[LimitViolation, ...] = ()
    historical_step_fingerprints: tuple[tuple[str, str], ...] = ()
    cumulative_written_off_base: float = 0.0
    cumulative_settlement_extra_fees_base: float = 0.0
    settlement_log: tuple[SettlementEvent, ...] = ()


@dataclass(frozen=True, slots=True)
class PathScenarioResult:
    path_id: str
    path_version: int
    path_fingerprint: str
    status: str
    missing_data: str | None
    policies: tuple[PolicyPathResult, ...]
    first_trigger_step_id: str | None = None
    trigger_basis_fingerprint: str | None = None


@dataclass(frozen=True, slots=True)
class PathRunResult:
    run_id: str
    results: tuple[PathScenarioResult, ...]

    @property
    def complete(self) -> bool:
        return all(item.status == "complete" for item in self.results)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def path_run_result_from_dict(payload: dict[str, Any]) -> PathRunResult:
    results = []
    for item in payload["results"]:
        policies = []
        for policy in item["policies"]:
            steps = tuple(
                PathStepResult(
                    step_id=step["step_id"],
                    scenario_id=step["scenario_id"],
                    scenario_version=int(step["scenario_version"]),
                    scenario_fingerprint=step["scenario_fingerprint"],
                    market_effect=float(step["market_effect"]),
                    trade_effect=float(step["trade_effect"]),
                    interaction_effect=float(step["interaction_effect"]),
                    cash_flow_effect=float(step["cash_flow_effect"]),
                    total_change=float(step["total_change"]),
                    portfolio_value=float(step["portfolio_value"]),
                    contributions=tuple(
                        Contribution(**entry) for entry in step["contributions"]
                    ),
                    limit_checks=tuple(
                        LimitViolation(**entry) for entry in step.get("limit_checks", ())
                    ),
                    action_plan_id=step.get("action_plan_id"),
                    action_effect=float(step.get("action_effect", 0.0)),
                    action_fees=float(step.get("action_fees", 0.0)),
                    cash_flow_from_actions=float(
                        step.get("cash_flow_from_actions", 0.0)
                    ),
                    action_reconciliation=_action_reconciliation_from_dict(
                        step.get("action_reconciliation")
                    ),
                    action_slippage=float(step.get("action_slippage", 0.0)),
                    pending_settlements=tuple(
                        _pending_from_dict(entry)
                        for entry in step.get("pending_settlements", ())
                    ),
                    pending_settled_base_amount=float(
                        step.get("pending_settled_base_amount", 0.0)
                    ),
                    canceled_action_order_ids=tuple(
                        step.get("canceled_action_order_ids", ())
                    ),
                    execution_ledger=_ledger_from_dict(step.get("execution_ledger")),
                    pre_limit_checks=tuple(
                        LimitViolation(**entry)
                        for entry in step.get("pre_limit_checks", ())
                    ),
                    settlement_effect=float(step.get("settlement_effect", 0.0)),
                    settlement_events=tuple(
                        _settlement_event_from_dict(entry)
                        for entry in step.get("settlement_events", ())
                    ),
                )
                for step in policy["steps"]
            )
            policies.append(
                PolicyPathResult(
                policy=policy["policy"],
                complete=bool(policy["complete"]),
                initial_value=float(policy["initial_value"]),
                final_value=float(policy["final_value"]),
                total_market_effect=float(policy["total_market_effect"]),
                total_trade_effect=float(policy["total_trade_effect"]),
                total_interaction_effect=float(policy["total_interaction_effect"]),
                total_cash_flow_effect=float(policy["total_cash_flow_effect"]),
                total_change=float(policy["total_change"]),
                steps=steps,
                total_action_effect=float(policy.get("total_action_effect", 0.0)),
                total_action_fees=float(policy.get("total_action_fees", 0.0)),
                total_cash_flow_from_actions=float(
                    policy.get("total_cash_flow_from_actions", 0.0)
                ),
                first_trigger_step_id=policy.get("first_trigger_step_id"),
                first_breaches=tuple(
                    LimitViolation(**entry) for entry in policy.get("first_breaches", ())
                ),
                unresolved_metrics=tuple(
                    LimitViolation(**entry)
                    for entry in policy.get("unresolved_metrics", ())
                ),
                execution_mode=policy.get("execution_mode", "immediate"),
                execution_state=_execution_state_from_dict(
                    policy.get("execution_state")
                ),
                total_settlement_effect=float(
                    policy.get("total_settlement_effect", 0.0)
                ),
            )
        )
        results.append(
            PathScenarioResult(
                path_id=item["path_id"],
                path_version=int(item["path_version"]),
                path_fingerprint=item["path_fingerprint"],
                status=item["status"],
                missing_data=item.get("missing_data"),
                policies=tuple(policies),
                first_trigger_step_id=item.get("first_trigger_step_id"),
                trigger_basis_fingerprint=item.get("trigger_basis_fingerprint"),
            )
        )
    return PathRunResult(run_id=payload["run_id"], results=tuple(results))


def _action_reconciliation_from_dict(
    value: dict[str, Any] | None,
) -> ActionReconciliation | None:
    if value is None:
        return None
    fills = tuple(
        ActionFill(
            type=fill["type"],
            holding_id=fill.get("holding_id"),
            quantity=float(fill["quantity"]),
            from_currency=fill.get("from_currency"),
            to_currency=fill.get("to_currency"),
            local_amount=float(fill["local_amount"]),
            base_gross_amount=float(fill["base_gross_amount"]),
            fee_base_amount=float(fill["fee_base_amount"]),
            base_net_amount=float(fill["base_net_amount"]),
            plan_id=fill.get("plan_id"),
            order_id=fill.get("order_id"),
            requested_quantity=(
                float(fill["requested_quantity"])
                if fill.get("requested_quantity") is not None
                else None
            ),
            unfilled_quantity=(
                float(fill["unfilled_quantity"])
                if fill.get("unfilled_quantity") is not None
                else None
            ),
            fill_step_id=fill.get("fill_step_id"),
            settlement_step_id=fill.get("settlement_step_id"),
            window_price_low=(
                float(fill["window_price_low"])
                if fill.get("window_price_low") is not None
                else None
            ),
            window_price_high=(
                float(fill["window_price_high"])
                if fill.get("window_price_high") is not None
                else None
            ),
            fill_price=(
                float(fill["fill_price"]) if fill.get("fill_price") is not None else None
            ),
            slippage_base_amount=float(fill.get("slippage_base_amount", 0.0)),
            status=fill.get("status", "filled"),
        )
        for fill in value.get("fills", ())
    )
    return ActionReconciliation(
        pre_action_value=float(value["pre_action_value"]),
        post_action_value=float(value["post_action_value"]),
        action_effect=float(value["action_effect"]),
        fees=float(value["fees"]),
        cash_flow_from_actions=float(value["cash_flow_from_actions"]),
        fills=fills,
        slippage=float(value.get("slippage", 0.0)),
        requested_quantity=float(value.get("requested_quantity", 0.0)),
        filled_quantity=float(value.get("filled_quantity", 0.0)),
        unfilled_quantity=float(value.get("unfilled_quantity", 0.0)),
        pending_added_base_amount=float(value.get("pending_added_base_amount", 0.0)),
        pending_settled_base_amount=float(
            value.get("pending_settled_base_amount", 0.0)
        ),
        canceled_orders=tuple(value.get("canceled_orders", ())),
    )


def _unfilled_order_from_dict(value: dict[str, Any]) -> UnfilledOrder:
    return UnfilledOrder(
        plan_id=value["plan_id"],
        order_id=value["order_id"],
        type=value["type"],
        requested_quantity=float(value["requested_quantity"]),
        filled_quantity=float(value["filled_quantity"]),
        unfilled_quantity=float(value["unfilled_quantity"]),
        holding_id=value.get("holding_id"),
        from_currency=value.get("from_currency"),
        to_currency=value.get("to_currency"),
        status=value.get("status", "open"),
    )


def _pending_from_dict(value: dict[str, Any]) -> PendingSettlement:
    return PendingSettlement(
        type=value["type"],
        plan_id=value["plan_id"],
        order_id=value["order_id"],
        fill_step_id=value["fill_step_id"],
        settlement_step_id=value["settlement_step_id"],
        currency=value["currency"],
        local_amount=float(value["local_amount"]),
        gross_base_amount=float(value["gross_base_amount"]),
        fee_base_amount=float(value["fee_base_amount"]),
        net_base_amount_at_fill=float(value["net_base_amount_at_fill"]),
        holding_id=value.get("holding_id"),
        settled_local=float(value.get("settled_local", 0.0)),
        disputed_local=float(value.get("disputed_local", 0.0)),
        written_off_local=float(value.get("written_off_local", 0.0)),
        extra_fees_local=float(value.get("extra_fees_local", 0.0)),
        extra_fees_base=float(value.get("extra_fees_base", 0.0)),
        counterparty=value.get("counterparty"),
        event_driven_settlement=bool(
            value.get("event_driven_settlement", False)
        ),
        events=tuple(
            _settlement_event_from_dict(item) for item in value.get("events", ())
        ),
    )


def _settlement_event_from_dict(value: dict[str, Any]) -> SettlementEvent:
    return SettlementEvent(
        step_id=value["step_id"],
        plan_id=value["plan_id"],
        order_id=value["order_id"],
        kind=value["kind"],
        amount_local=float(value["amount_local"]),
        applied_local=float(value["applied_local"]),
        counterparty=value.get("counterparty"),
        note=value.get("note"),
    )


def _ledger_from_dict(value: dict[str, Any] | None) -> StepExecutionLedger | None:
    if value is None:
        return None
    return StepExecutionLedger(
        quantities={key: float(number) for key, number in value["quantities"].items()},
        available_cash={
            key: float(number) for key, number in value["available_cash"].items()
        },
        pending_cash_local={
            key: float(number)
            for key, number in value.get("pending_cash_local", {}).items()
        },
        pending_cash_base=float(value.get("pending_cash_base", 0.0)),
        cumulative_fees=float(value.get("cumulative_fees", 0.0)),
        cumulative_slippage=float(value.get("cumulative_slippage", 0.0)),
        cumulative_filled_base=float(value.get("cumulative_filled_base", 0.0)),
        cumulative_settled_base=float(value.get("cumulative_settled_base", 0.0)),
        cumulative_settlement_lag=float(value.get("cumulative_settlement_lag", 0.0)),
        open_orders=tuple(
            _unfilled_order_from_dict(item) for item in value.get("open_orders", ())
        ),
        disputed_cash_local={
            key: float(number)
            for key, number in (value.get("disputed_cash_local") or {}).items()
        },
        disputed_cash_base=float(value.get("disputed_cash_base", 0.0)),
        cumulative_written_off_base=float(value.get("cumulative_written_off_base", 0.0)),
        cumulative_settlement_extra_fees_base=float(
            value.get("cumulative_settlement_extra_fees_base", 0.0)
        ),
        settlement_events=tuple(
            _settlement_event_from_dict(item)
            for item in value.get("settlement_events", ())
        ),
    )


def _execution_state_from_dict(value: dict[str, Any] | None) -> ExecutionState | None:
    if value is None:
        return None
    return ExecutionState(
        policy=value["policy"],
        plan_id=value.get("plan_id"),
        mode=value["mode"],
        completed_step_index=int(value["completed_step_index"]),
        quantities={key: float(number) for key, number in value["quantities"].items()},
        available_cash={
            key: float(number) for key, number in value["available_cash"].items()
        },
        pending=tuple(_pending_from_dict(item) for item in value.get("pending", ())),
        open_orders=tuple(
            _unfilled_order_from_dict(item) for item in value.get("open_orders", ())
        ),
        canceled_order_ids=tuple(value.get("canceled_order_ids", ())),
        cumulative_fees=float(value.get("cumulative_fees", 0.0)),
        cumulative_slippage=float(value.get("cumulative_slippage", 0.0)),
        cumulative_filled_base=float(value.get("cumulative_filled_base", 0.0)),
        cumulative_settled_base=float(value.get("cumulative_settled_base", 0.0)),
        cumulative_settlement_lag=float(value.get("cumulative_settlement_lag", 0.0)),
        external_cash_base=float(value.get("external_cash_base", 0.0)),
        initial_value=float(value["initial_value"]),
        previous_total=float(value["previous_total"]),
        first_trigger_step_id=value.get("first_trigger_step_id"),
        first_breaches=tuple(
            LimitViolation(**entry) for entry in value.get("first_breaches", ())
        ),
        historical_step_fingerprints=tuple(
            (item[0], item[1]) for item in value.get("historical_step_fingerprints", ())
        ),
        cumulative_written_off_base=float(value.get("cumulative_written_off_base", 0.0)),
        cumulative_settlement_extra_fees_base=float(
            value.get("cumulative_settlement_extra_fees_base", 0.0)
        ),
        settlement_log=tuple(
            _settlement_event_from_dict(item)
            for item in value.get("settlement_log", ())
        ),
    )


def compare_path_results(
    runs: list[PathRunResult],
) -> dict[str, dict[str, dict[str, float | str | None]]]:
    """Side-by-side replay table keyed by path_id, then "v<version>/<policy>".

    Historical and revised versions of the same path appear next to each
    other so their totals can be reconciled column by column.
    """
    table: dict[str, dict[str, dict[str, float | str | None]]] = {}
    for run in runs:
        for result in run.results:
            columns = table.setdefault(result.path_id, {})
            for policy in result.policies:
                key = f"v{result.path_version}/{policy.policy}"
                columns[key] = {
                    "run_id": run.run_id,
                    "status": result.status,
                    "fingerprint": result.path_fingerprint,
                    "initial_value": policy.initial_value,
                    "final_value": policy.final_value,
                    "total_change": policy.total_change,
                    "market_effect": policy.total_market_effect,
                    "trade_effect": policy.total_trade_effect,
                    "interaction_effect": policy.total_interaction_effect,
                    "cash_flow_effect": policy.total_cash_flow_effect,
                    "action_effect": policy.total_action_effect,
                    "action_fees": policy.total_action_fees,
                    "cash_flow_from_actions": policy.total_cash_flow_from_actions,
                    "first_trigger_step_id": policy.first_trigger_step_id,
                }
    return table


def compare_disposal_policies(
    run: PathRunResult,
) -> dict[str, dict[str, dict[str, Any]]]:
    """Compare hold, full-fill and constrained fills per action plan.

    Differences are classified into execution shortfall, price slippage,
    fees and settlement lag. Values are taken from step ledgers, so they
    remain auditable even when a path replay is partial.
    """
    table: dict[str, dict[str, dict[str, Any]]] = {}
    for path in run.results:
        by_policy = {policy.policy: policy for policy in path.policies}
        plans = {}
        for name in by_policy:
            if name.startswith("constrained:"):
                plans[name.removeprefix("constrained:")] = name
        for plan_id, constrained_name in plans.items():
            constrained = by_policy[constrained_name]
            immediate = by_policy.get(f"action:{plan_id}")
            hold = by_policy.get("hold")
            final_step = constrained.steps[-1] if constrained.steps else None
            ledger = final_step.execution_ledger if final_step else None
            driver = {
                "status": path.status,
                "constrained_final_value": constrained.final_value,
                "immediate_final_value": immediate.final_value if immediate else None,
                "hold_final_value": hold.final_value if hold else None,
                "difference_vs_immediate": (
                    constrained.final_value - immediate.final_value
                    if immediate
                    else None
                ),
                "unfilled_orders": (
                    [asdict(item) for item in ledger.open_orders] if ledger else []
                ),
                "pending_settlement_local": (
                    dict(ledger.pending_cash_local) if ledger else {}
                ),
                "pending_settlement_base": ledger.pending_cash_base if ledger else 0.0,
                "cumulative_fees": ledger.cumulative_fees if ledger else 0.0,
                "cumulative_price_slippage": (
                    ledger.cumulative_slippage if ledger else 0.0
                ),
                "cumulative_filled_base": (
                    ledger.cumulative_filled_base if ledger else 0.0
                ),
                "cumulative_settled_base": (
                    ledger.cumulative_settled_base if ledger else 0.0
                ),
                "fees_difference_vs_immediate": (
                    ledger.cumulative_fees
                    - (immediate.total_action_fees if immediate else 0.0)
                    if ledger
                    else None
                ),
            }
            immediate_fees = immediate.total_action_fees if immediate else 0.0
            if immediate is not None and ledger is not None:
                fee_difference = ledger.cumulative_fees - immediate_fees
                explained = (
                    ledger.cumulative_slippage
                    + fee_difference
                )
                remaining_difference = (
                    constrained.final_value
                    - immediate.final_value
                )
                driver["price_band_effect"] = ledger.cumulative_slippage
                driver["fee_difference"] = fee_difference
                driver["settlement_lag_effect"] = ledger.cumulative_settlement_lag
                driver["unfilled_and_settlement_lag_effect"] = (
                    remaining_difference - explained - ledger.cumulative_settlement_lag
                )
                driver["risk_sources"] = [
                    "price_band_slippage" if ledger.cumulative_slippage else None,
                    "fees" if ledger.cumulative_fees else None,
                    "settlement_lag" if any(
                        fill.settlement_step_id != fill.fill_step_id
                        for step in constrained.steps
                        for fill in (
                            step.action_reconciliation.fills
                            if step.action_reconciliation
                            else ()
                        )
                    ) else None,
                    "insufficient_fills" if ledger.open_orders else None,
                ]
                driver["risk_sources"] = [
                    item for item in driver["risk_sources"] if item is not None
                ]
            table.setdefault(path.path_id, {})[plan_id] = driver
    return table


def load_path_run_result(directory: str | Path, run_id: str) -> PathRunResult:
    path = ResultStore(directory).path_for(run_id)
    with path.open("r", encoding="utf-8") as handle:
        payload = json.load(handle)
    return path_run_result_from_dict(payload)


def compare_settlement_outcomes(
    runs: list[PathRunResult],
) -> dict[str, dict[str, dict[str, Any]]]:
    """Compare settlement remediation branches across runs and versions.

    Keyed by path_id, then ``"v<version>/<policy>"``. Each column separates
    realized losses (written-off amounts, extra counterparty fees and the
    original trade fees, which are never refunded) from temporarily
    unavailable funds (outstanding pending) and amounts still in dispute.
    Event-kind counts preserve the evidence trail (duplicate receipts, late
    rejections, stale notices) for each branch.
    """
    table: dict[str, dict[str, dict[str, Any]]] = {}
    for run in runs:
        for result in run.results:
            columns = table.setdefault(result.path_id, {})
            for policy in result.policies:
                final_step = policy.steps[-1] if policy.steps else None
                ledger = final_step.execution_ledger if final_step else None
                state = policy.execution_state
                log: tuple[SettlementEvent, ...] = ()
                if state is not None:
                    log = state.settlement_log
                elif ledger is not None:
                    log = ledger.settlement_events
                event_kinds: dict[str, int] = {}
                for event in log:
                    event_kinds[event.kind] = event_kinds.get(event.kind, 0) + 1
                written_off = (
                    ledger.cumulative_written_off_base if ledger else 0.0
                )
                extra_fees = (
                    ledger.cumulative_settlement_extra_fees_base if ledger else 0.0
                )
                trade_fees = ledger.cumulative_fees if ledger else 0.0
                columns[f"v{result.path_version}/{policy.policy}"] = {
                    "run_id": run.run_id,
                    "status": result.status,
                    "settled_base": (
                        ledger.cumulative_settled_base if ledger else 0.0
                    ),
                    "unavailable_pending_base": (
                        ledger.pending_cash_base if ledger else 0.0
                    ),
                    "disputed_base": ledger.disputed_cash_base if ledger else 0.0,
                    "written_off_base": written_off,
                    "settlement_extra_fees_base": extra_fees,
                    "trade_fees_base": trade_fees,
                    "realized_loss_base": written_off + extra_fees + trade_fees,
                    "settlement_event_kinds": event_kinds,
                }
    return table
