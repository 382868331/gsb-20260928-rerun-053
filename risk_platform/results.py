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


@dataclass(frozen=True, slots=True)
class PathScenarioResult:
    path_id: str
    path_version: int
    path_fingerprint: str
    status: str
    missing_data: str | None
    policies: tuple[PolicyPathResult, ...]


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
        policies = tuple(
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
                steps=tuple(
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
                    )
                    for step in policy["steps"]
                ),
            )
            for policy in item["policies"]
        )
        results.append(
            PathScenarioResult(
                path_id=item["path_id"],
                path_version=int(item["path_version"]),
                path_fingerprint=item["path_fingerprint"],
                status=item["status"],
                missing_data=item.get("missing_data"),
                policies=policies,
            )
        )
    return PathRunResult(run_id=payload["run_id"], results=tuple(results))


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
                }
    return table


def load_path_run_result(directory: str | Path, run_id: str) -> PathRunResult:
    path = ResultStore(directory).path_for(run_id)
    with path.open("r", encoding="utf-8") as handle:
        payload = json.load(handle)
    return path_run_result_from_dict(payload)
