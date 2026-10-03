from __future__ import annotations

import argparse
import uuid

from risk_platform.engine import RevaluationEngine
from risk_platform.holdings import load_holdings
from risk_platform.path_engine import PathEngine
from risk_platform.paths import load_paths
from risk_platform.results import ResultStore
from risk_platform.results import load_path_run_result
from risk_platform.scenarios import load_scenarios


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run versioned portfolio stress revaluations")
    parser.add_argument("--holdings", required=True)
    parser.add_argument("--scenarios")
    parser.add_argument("--paths", help="Ordered multi-step stress paths JSON")
    parser.add_argument(
        "--policies",
        help="Comma-separated policies; defaults to hold,rebalance and every action plan",
    )
    parser.add_argument("--results-dir", required=True)
    parser.add_argument("--run-id", default=f"run-{uuid.uuid4().hex[:12]}")
    parser.add_argument("--base-scenario-id", default="base")
    parser.add_argument(
        "--resume-run-id",
        help="Resume a previously saved partial path run from its resume state",
    )
    return parser


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    if not args.scenarios and not args.paths:
        raise SystemExit("At least one of --scenarios or --paths is required")
    holdings = load_holdings(args.holdings)
    store = ResultStore(args.results_dir)

    if args.scenarios:
        scenarios = load_scenarios(args.scenarios)
        result = RevaluationEngine.default().run(
            run_id=args.run_id,
            holdings=holdings,
            scenarios=scenarios,
            base_scenario_id=args.base_scenario_id,
        )
        print(store.save(result))

    if args.paths:
        paths = load_paths(args.paths)
        policies = (
            None
            if args.policies is None
            else tuple(item.strip() for item in args.policies.split(",") if item.strip())
        )
        engine = PathEngine.default()
        if args.resume_run_id:
            saved = load_path_run_result(args.results_dir, args.resume_run_id)
            path_result = engine.resume(
                run_id=f"{args.run_id}-paths",
                holdings=holdings,
                paths=paths,
                saved=saved,
            )
        else:
            path_result = engine.run(
                run_id=f"{args.run_id}-paths",
                holdings=holdings,
                paths=paths,
                policies=policies,
            )
        print(store.save(path_result))
        if not path_result.complete:
            print("warning: incomplete path replay; partial results only")


if __name__ == "__main__":
    main()
