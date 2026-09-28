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
class ScenarioResult:
    scenario_id: str
    scenario_version: int
    scenario_fingerprint: str
    total_base_value: float
    total_pnl_vs_base: float
    contributions: tuple[Contribution, ...]


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
