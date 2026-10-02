from __future__ import annotations

import argparse
import uuid

from risk_platform.disposal import load_disposal_plans
from risk_platform.engine import RevaluationEngine
from risk_platform.holdings import load_holdings
from risk_platform.limit_engine import LimitPathEngine
from risk_platform.limits import load_limits
from risk_platform.path_engine import DEFAULT_POLICIES, PathEngine
from risk_platform.paths import load_paths
from risk_platform.results import ResultStore
from risk_platform.scenarios import load_scenarios


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run versioned portfolio stress revaluations")
    parser.add_argument("--holdings", required=True)
    parser.add_argument("--scenarios")
    parser.add_argument("--paths", help="Ordered multi-step stress paths JSON")
    parser.add_argument("--limits", help="Portfolio risk limits JSON (requires --paths)")
    parser.add_argument("--disposals", help="Disposal plans JSON (requires --limits)")
    parser.add_argument(
        "--policies",
        default=",".join(DEFAULT_POLICIES),
        help="Comma-separated path replay policies (hold,rebalance)",
    )
    parser.add_argument("--results-dir", required=True)
    parser.add_argument("--run-id", default=f"run-{uuid.uuid4().hex[:12]}")
    parser.add_argument("--base-scenario-id", default="base")
    return parser


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    if not args.scenarios and not args.paths:
        raise SystemExit("At least one of --scenarios or --paths is required")
    if args.disposals and not args.limits:
        raise SystemExit("--disposals requires --limits")
    if args.limits and not args.paths:
        raise SystemExit("--limits requires --paths")
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
        policies = tuple(item.strip() for item in args.policies.split(",") if item.strip())
        path_result = PathEngine.default().run(
            run_id=f"{args.run_id}-paths",
            holdings=holdings,
            paths=paths,
            policies=policies,
        )
        print(store.save(path_result))
        if not path_result.complete:
            print("warning: incomplete path replay; partial results only")

    if args.limits:
        limits = load_limits(args.limits)
        plans = load_disposal_plans(args.disposals) if args.disposals else []
        limit_result = LimitPathEngine.default().run(
            run_id=f"{args.run_id}-limits",
            holdings=holdings,
            paths=paths,
            limits=limits,
            plans=plans,
        )
        print(store.save(limit_result))
        if not limit_result.complete:
            print("warning: incomplete limit replay; confirmed breaches kept, remainder unconfirmed")


if __name__ == "__main__":
    main()
