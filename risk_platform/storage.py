from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from .models import Holding, ScenarioSnapshot, StressResult, ValuationResult


class JsonMarketDataStore:
    def __init__(self, root: str | Path):
        self.root = Path(root)

    def load_holdings(self, name: str = "holdings.json") -> list[Holding]:
        with (self.root / name).open("r", encoding="utf-8") as fh:
            data = json.load(fh)
        return [Holding.from_dict(item) for item in data["holdings"]]

    def load_scenario(self, scenario_id: str) -> ScenarioSnapshot:
        path = self.root / "scenarios" / f"{scenario_id}.json"
        with path.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
        return ScenarioSnapshot.from_dict(scenario_id, data)


class JsonResultStore:
    def __init__(self, root: str | Path):
        self.root = Path(root)
        self.results_dir = self.root / "results"
        self.valuation_dir = self.results_dir / "valuations"
        self.stress_dir = self.results_dir / "stress"

    def publish(self, result: ValuationResult | StressResult) -> Path:
        if isinstance(result, ValuationResult):
            directory = self.valuation_dir
        elif isinstance(result, StressResult):
            directory = self.stress_dir
        else:
            raise TypeError("unsupported result type")
        return self._atomic_write(directory, f"{result.run_id}.json", result.to_dict())

    def get_valuation(self, run_id: str) -> ValuationResult:
        with (self.valuation_dir / f"{run_id}.json").open(encoding="utf-8") as fh:
            return ValuationResult.from_dict(json.load(fh))

    def list_valuations(self) -> list[ValuationResult]:
        if not self.valuation_dir.exists():
            return []
        items = []
        for path in sorted(self.valuation_dir.glob("*.json")):
            with path.open(encoding="utf-8") as fh:
                items.append(ValuationResult.from_dict(json.load(fh)))
        return items

    def _atomic_write(
        self, directory: Path, filename: str, payload: dict[str, Any]
    ) -> Path:
        directory.mkdir(parents=True, exist_ok=True)
        target = directory / filename
        temporary = directory / f".{filename}.tmp"
        text = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)
        with temporary.open("w", encoding="utf-8") as fh:
            fh.write(text)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(temporary, target)
        return target
