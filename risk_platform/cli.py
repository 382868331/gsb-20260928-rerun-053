from __future__ import annotations

import argparse
import uuid

from risk_platform.engine import RevaluationEngine
from risk_platform.holdings import load_holdings
from risk_platform.path_engine import PathRunner
from risk_platform.paths import load_paths
from risk_platform.results import PathReplayResult
from risk_platform.results import ResultStore
from risk_platform.scenarios import load_scenarios


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run versioned portfolio stress revaluations")
    parser.add_argument("--holdings", required=True)
    parser.add_argument("--scenarios")
    parser.add_argument("--results-dir", required=True)
    parser.add_argument("--run-id", default=f"run-{uuid.uuid4().hex[:12]}")
    parser.add_argument("--base-scenario-id", default="base")
    parser.add_argument(
        "--paths",
        help="Replay multi-step stress paths instead of single-snapshot scenarios",
    )
    parser.add_argument("--path-id", help="Replay only the versions of one path id")
    return parser


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    holdings = load_holdings(args.holdings)
    store = ResultStore(args.results_dir)

    if args.paths:
        paths = load_paths(args.paths)
        path_results = PathRunner.default().replay(args.path_id, holdings, paths)
        result = PathReplayResult(run_id=args.run_id, paths=path_results)
        print(store.save_path_replay(result))
        for path in path_results:
            state = path.status.upper()
            print(
                f"{path.path_id} v{path.path_version} [{path.strategy}] {state} "
                f"{path.completed_steps}/{path.total_steps} steps "
                f"change={path.total_change:.6f}"
            )
            if path.missing_data_reason:
                print(f"  paused: {path.missing_data_reason}")
        if not result.all_complete:
            raise SystemExit(
                "One or more paths are incomplete; partial results were published, "
                "but no complete-path conclusion is made."
            )
        return

    if not args.scenarios:
        raise SystemExit("--scenarios is required unless --paths is used")
    scenarios = load_scenarios(args.scenarios)
    result = RevaluationEngine.default().run(
        run_id=args.run_id,
        holdings=holdings,
        scenarios=scenarios,
        base_scenario_id=args.base_scenario_id,
    )
    print(store.save(result))


if __name__ == "__main__":
    main()
