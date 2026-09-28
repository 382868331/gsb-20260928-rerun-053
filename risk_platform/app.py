from __future__ import annotations

import argparse
from pathlib import Path

from .engine import stress_pnl, value_portfolio
from .errors import RiskPlatformError
from .storage import JsonMarketDataStore, JsonResultStore


def _run_id(scenario_id: str, scenario_version: int, suffix: str = "value") -> str:
    return f"{scenario_id}__v{scenario_version}__{suffix}"


def run_scenario(
    data_dir: Path, output_dir: Path, scenario_id: str
) -> tuple[Path, Path | None]:
    market = JsonMarketDataStore(data_dir)
    results = JsonResultStore(output_dir)
    holdings = market.load_holdings()
    scenario = market.load_scenario(scenario_id)
    valued = value_portfolio(
        holdings, scenario, _run_id(scenario_id, scenario.version)
    )
    if scenario_id == "baseline":
        return results.publish(valued), None

    baseline_snapshot = market.load_scenario("baseline")
    baseline = value_portfolio(
        holdings,
        baseline_snapshot,
        _run_id("baseline", baseline_snapshot.version),
    )
    stressed = stress_pnl(
        baseline,
        valued,
        _run_id(scenario_id, scenario.version, "pnl"),
    )
    return results.publish(valued), results.publish(stressed)


def run_suite(data_dir: Path, output_dir: Path, scenario_ids: list[str]) -> None:
    market = JsonMarketDataStore(data_dir)
    holdings = market.load_holdings()
    baseline_snapshot = market.load_scenario("baseline")
    baseline = value_portfolio(
        holdings,
        baseline_snapshot,
        _run_id("baseline", baseline_snapshot.version),
    )

    valuations = [baseline]
    stresses = []
    for scenario_id in scenario_ids:
        scenario = market.load_scenario(scenario_id)
        valuations.append(
            value_portfolio(
                holdings, scenario, _run_id(scenario_id, scenario.version)
            )
        )
        stresses.append(
            stress_pnl(
                baseline,
                valuations[-1],
                _run_id(scenario_id, scenario.version, "pnl"),
            )
        )

    store = JsonResultStore(output_dir)
    for result in valuations:
        store.publish(result)
    for result in stresses:
        store.publish(result)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Investment risk revaluation")
    parser.add_argument("--data-dir", type=Path, default=Path("data"))
    parser.add_argument("--output-dir", type=Path, default=Path("output"))
    parser.add_argument(
        "--scenarios",
        nargs="+",
        default=["equity_crash", "rates_fx_shock", "equity_crash_v2"],
    )
    args = parser.parse_args(argv)
    try:
        run_suite(args.data_dir, args.output_dir, args.scenarios)
    except (OSError, RiskPlatformError, ValueError) as exc:
        parser.exit(2, f"valuation failed; no incomplete result published: {exc}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
