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


@dataclass(frozen=True, slots=True)
class StepContribution:
    holding_id: str
    currency: str
    quantity: float
    unit_local_value: float
    fx_rate: float
    value: float


@dataclass(frozen=True, slots=True)
class TradeRecord:
    holding_id: str
    amount: float


@dataclass(frozen=True, slots=True)
class StepResult:
    index: int
    label: str
    scenario_id: str
    scenario_version: int
    scenario_fingerprint: str
    portfolio_value: float
    change_vs_previous: float
    market_impact: float
    trading_impact: float
    interaction_impact: float
    cash_flow_effect: float
    executed_trades: tuple[TradeRecord, ...]
    applied_cash_flows: tuple[TradeRecord, ...]
    contributions: tuple[StepContribution, ...]


@dataclass(frozen=True, slots=True)
class PathResult:
    path_id: str
    path_version: int
    path_fingerprint: str
    description: str
    strategy: str
    status: str  # "complete" or "incomplete"
    completed_steps: int
    total_steps: int
    missing_data_reason: str | None
    start_value: float
    final_value: float
    total_change: float
    total_market_impact: float
    total_trading_impact: float
    total_interaction_impact: float
    total_cash_flow_effect: float
    steps: tuple[StepResult, ...]

    @property
    def is_complete(self) -> bool:
        return self.status == "complete" and self.completed_steps == self.total_steps

    def attribution_closes(self, tolerance: float = 1e-9) -> bool:
        explained = (
            self.total_market_impact
            + self.total_trading_impact
            + self.total_interaction_impact
            + self.total_cash_flow_effect
        )
        return abs(explained - self.total_change) <= tolerance


@dataclass(frozen=True, slots=True)
class PathReplayResult:
    run_id: str
    paths: tuple[PathResult, ...]

    @property
    def all_complete(self) -> bool:
        return bool(self.paths) and all(path.is_complete for path in self.paths)

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
        return self.save_object(result)

    def save_object(self, result: RunResult | PathReplayResult) -> Path:
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

    def save_path_replay(self, result: PathReplayResult) -> Path:
        return self.save_object(result)

    def load_path_replay(self, run_id: str) -> PathReplayResult:
        with self.path_for(run_id).open("r", encoding="utf-8") as handle:
            payload = json.load(handle)
        return path_replay_from_dict(payload)


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


def path_replay_from_dict(payload: dict[str, Any]) -> PathReplayResult:
    return PathReplayResult(
        run_id=payload["run_id"],
        paths=tuple(_path_from_dict(item) for item in payload["paths"]),
    )


def _path_from_dict(item: dict[str, Any]) -> PathResult:
    return PathResult(
        path_id=item["path_id"],
        path_version=int(item["path_version"]),
        path_fingerprint=item["path_fingerprint"],
        description=item.get("description", ""),
        strategy=item["strategy"],
        status=item["status"],
        completed_steps=int(item["completed_steps"]),
        total_steps=int(item["total_steps"]),
        missing_data_reason=item.get("missing_data_reason"),
        start_value=float(item["start_value"]),
        final_value=float(item["final_value"]),
        total_change=float(item["total_change"]),
        total_market_impact=float(item["total_market_impact"]),
        total_trading_impact=float(item["total_trading_impact"]),
        total_interaction_impact=float(item["total_interaction_impact"]),
        total_cash_flow_effect=float(item["total_cash_flow_effect"]),
        steps=tuple(_step_from_dict(step) for step in item["steps"]),
    )


def _step_from_dict(item: dict[str, Any]) -> StepResult:
    return StepResult(
        index=int(item["index"]),
        label=item["label"],
        scenario_id=item["scenario_id"],
        scenario_version=int(item["scenario_version"]),
        scenario_fingerprint=item["scenario_fingerprint"],
        portfolio_value=float(item["portfolio_value"]),
        change_vs_previous=float(item["change_vs_previous"]),
        market_impact=float(item["market_impact"]),
        trading_impact=float(item["trading_impact"]),
        interaction_impact=float(item["interaction_impact"]),
        cash_flow_effect=float(item["cash_flow_effect"]),
        executed_trades=tuple(TradeRecord(**record) for record in item["executed_trades"]),
        applied_cash_flows=tuple(
            TradeRecord(**record) for record in item["applied_cash_flows"]
        ),
        contributions=tuple(
            StepContribution(**record) for record in item["contributions"]
        ),
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
