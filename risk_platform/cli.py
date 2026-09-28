from __future__ import annotations

import argparse
import uuid

from risk_platform.engine import RevaluationEngine
from risk_platform.holdings import load_holdings
from risk_platform.results import ResultStore
from risk_platform.scenarios import load_scenarios


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run versioned portfolio stress revaluations")
    parser.add_argument("--holdings", required=True)
    parser.add_argument("--scenarios", required=True)
    parser.add_argument("--results-dir", required=True)
    parser.add_argument("--run-id", default=f"run-{uuid.uuid4().hex[:12]}")
    parser.add_argument("--base-scenario-id", default="base")
    return parser


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    holdings = load_holdings(args.holdings)
    scenarios = load_scenarios(args.scenarios)
    result = RevaluationEngine.default().run(
        run_id=args.run_id,
        holdings=holdings,
        scenarios=scenarios,
        base_scenario_id=args.base_scenario_id,
    )
    print(ResultStore(args.results_dir).save(result))


if __name__ == "__main__":
    main()
