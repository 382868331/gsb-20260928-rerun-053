from __future__ import annotations

import math
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from risk_platform.holdings import load_holdings
from risk_platform.path_engine import PathRunner
from risk_platform.paths import load_paths
from risk_platform.results import PathReplayResult, ResultStore

ROOT = Path(__file__).resolve().parents[1]


class PathReplayTests(unittest.TestCase):
    def setUp(self) -> None:
        self.holdings = load_holdings(ROOT / "data" / "holdings.json")
        self.paths = load_paths(ROOT / "data" / "paths.json")
        self.runner = PathRunner.default()

    def test_historical_and_revised_paths_replay_side_by_side(self) -> None:
        replay = self.runner.replay("recession", self.holdings, self.paths)
        self.assertEqual([path.path_version for path in replay], [1, 2])
        strategies = {path.path_version: path.strategy for path in replay}
        self.assertEqual(strategies, {1: "hold", 2: "rebalance"})
        self.assertTrue(all(path.is_complete for path in replay))
        # Same starting portfolio, different end states because of rebalancing.
        self.assertAlmostEqual(replay[0].start_value, replay[1].start_value)
        self.assertNotAlmostEqual(replay[0].final_value, replay[1].final_value)

    def test_each_step_explains_change_against_previous_step(self) -> None:
        path = self.runner.replay("recession", self.holdings, self.paths)[0]
        previous_value = path.steps[0].portfolio_value
        for step in path.steps[1:]:
            explained = (
                step.market_impact
                + step.trading_impact
                + step.interaction_impact
                + step.cash_flow_effect
            )
            self.assertAlmostEqual(explained, step.change_vs_previous, places=9)
            self.assertAlmostEqual(
                previous_value + step.change_vs_previous, step.portfolio_value, places=9
            )
            previous_value = step.portfolio_value

    def test_cash_flow_is_valued_at_current_prices_and_kept_separate(self) -> None:
        path = self.runner.replay("recession", self.holdings, self.paths)[0]
        t1 = path.steps[1]
        # 50 extra USD received at t1 when USD fx is 7.2.
        self.assertAlmostEqual(t1.cash_flow_effect, 50 * 7.2)
        self.assertEqual(t1.trading_impact, 0.0)
        usd = next(item for item in t1.contributions if item.holding_id == "usd-cash")
        self.assertAlmostEqual(usd.quantity, 1050)

    def test_rebalance_splits_market_trading_and_interaction(self) -> None:
        path = self.runner.replay("recession", self.holdings, self.paths)[1]
        t1 = path.steps[1]

        # Selling 5000 bonds is value neutral at previous prices: trading impact
        # (bond leg plus automatic EUR cash leg) nets to zero.
        self.assertAlmostEqual(t1.trading_impact, 0.0, places=9)

        # Interaction is the sold bond position being re-priced: cash held from
        # t1 does not suffer the bond move that the remaining portfolio takes.
        sold_local_units = 5000 * math.exp(-0.03)
        old_bond_base = math.exp(-0.03) * 7.8
        new_bond_base = math.exp(-0.035) * 7.7
        # Sold bonds become EUR cash: bond move avoided plus EUR fx move on cash.
        expected_interaction = (
            -5000 * (new_bond_base - old_bond_base)
            + sold_local_units * (7.7 - 7.8)
        )
        self.assertAlmostEqual(t1.interaction_impact, expected_interaction, places=6)
        self.assertNotAlmostEqual(t1.interaction_impact, 0.0)

        # Auto balancing cash leg in EUR is recorded.
        cash_leg = next(record for record in t1.executed_trades if record.holding_id == "eur-cash")
        self.assertAlmostEqual(cash_leg.amount, 5000 * math.exp(-0.03), places=6)

        # Market + trading + interaction + flow closes the whole path exactly.
        self.assertTrue(path.attribution_closes())
        self.assertAlmostEqual(
            path.total_market_impact
            + path.total_trading_impact
            + path.total_interaction_impact
            + path.total_cash_flow_effect,
            path.total_change,
            places=9,
        )

    def test_rebalance_outperforms_hold_through_protected_cash(self) -> None:
        hold, rebalance = self.runner.replay("recession", self.holdings, self.paths)
        # De-risking before further EUR/bond losses cushions total PnL.
        self.assertGreater(rebalance.total_change, hold.total_change)

    def test_missing_step_data_keeps_prior_results_but_marks_path_incomplete(self) -> None:
        broken = []
        for path in self.paths:
            if path.version == 2:
                steps = list(path.steps)
                last = steps[-1]
                broken_market = last.market.__class__.from_dict(
                    {**last.market.canonical_dict(), "equity_prices": {}}
                )
                steps[-1] = type(last)(
                    label=last.label,
                    market=broken_market,
                    cash_flows=last.cash_flows,
                    trades=last.trades,
                )
                broken.append(
                    type(path)(
                        path_id=path.path_id,
                        version=path.version,
                        description=path.description,
                        strategy=path.strategy,
                        steps=tuple(steps),
                        fingerprint=path.fingerprint,
                    )
                )
            else:
                broken.append(path)

        replay = self.runner.replay("recession", self.holdings, broken)
        complete, incomplete = replay
        self.assertTrue(complete.is_complete)
        self.assertFalse(incomplete.is_complete)
        self.assertEqual(incomplete.status, "incomplete")
        self.assertEqual(incomplete.completed_steps, 2)
        self.assertEqual(incomplete.total_steps, 3)
        self.assertIn("lacks market data", incomplete.missing_data_reason)
        # The valid t0/t1 results are retained.
        self.assertEqual(len(incomplete.steps), 2)
        self.assertEqual(incomplete.final_value, incomplete.steps[-1].portfolio_value)
        self.assertTrue(incomplete.attribution_closes())

    def test_path_replay_round_trips_through_result_store(self) -> None:
        result = PathReplayResult(
            run_id="paths-round-trip",
            paths=self.runner.replay("recession", self.holdings, self.paths),
        )
        with tempfile.TemporaryDirectory() as directory:
            store = ResultStore(directory)
            store.save_path_replay(result)
            loaded = store.load_path_replay("paths-round-trip")
        self.assertEqual(result, loaded)
        self.assertTrue(loaded.all_complete)

    def test_cli_replays_paths_and_loads_partial_results(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            completed = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "risk_platform.cli",
                    "--holdings",
                    str(ROOT / "data" / "holdings.json"),
                    "--paths",
                    str(ROOT / "data" / "paths.json"),
                    "--results-dir",
                    directory,
                    "--run-id",
                    "cli-paths",
                ],
                cwd=ROOT,
                check=True,
                capture_output=True,
                text=True,
            )
            output_path = Path(completed.stdout.strip().splitlines()[0])
            self.assertTrue(output_path.exists())
            loaded = ResultStore(directory).load_path_replay("cli-paths")
            self.assertEqual(len(loaded.paths), 2)
            self.assertTrue(loaded.all_complete)


if __name__ == "__main__":
    unittest.main()
