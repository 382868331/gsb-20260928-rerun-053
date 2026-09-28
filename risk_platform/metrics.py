from __future__ import annotations

from typing import Protocol

from risk_platform.results import RunResult, ScenarioResult


class RiskMetric(Protocol):
    name: str

    def calculate(self, result: ScenarioResult) -> float:
        ...


class TotalExposureMetric:
    name = "total_exposure"

    def calculate(self, result: ScenarioResult) -> float:
        return result.total_base_value


class PnLMetric:
    name = "pnl_vs_base"

    def calculate(self, result: ScenarioResult) -> float:
        return result.total_pnl_vs_base


class LargestLossContributionMetric:
    name = "largest_loss_contribution"

    def calculate(self, result: ScenarioResult) -> float:
        return min((item.pnl_vs_base for item in result.contributions), default=0.0)


def calculate_metrics(run: RunResult, metrics: list[RiskMetric]) -> dict[str, dict[str, float]]:
    return {
        result.scenario_id: {
            metric.name: metric.calculate(result) for metric in metrics
        }
        for result in run.results
    }
