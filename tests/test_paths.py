from __future__ import annotations

import json
import math
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from risk_platform.holdings import load_holdings
from risk_platform.path_engine import PathEngine
from risk_platform.paths import load_paths
from risk_platform.results import (
    ResultStore,
    compare_path_results,
    load_path_run_result,
)

ROOT = Path(__file__).resolve().parents[1]


def make_path(step_scenarios, path_id="p", version=1, step_extras=None):
    steps = []
    for index, scenario in enumerate(step_scenarios):
        step = {"step_id": f"t{index}", "scenario": scenario}
        if step_extras and index in step_extras:
            step.update(step_extras[index])
        steps.append(step)
    return {
        "path_id": path_id,
        "version": version,
        "description": "test path",
        "steps": steps,
    }


def scenario(sid, price, yield_, usd, eur=7.8):
    return {
        "scenario_id": sid,
        "version": 1,
        "description": sid,
        "equity_prices": {"FROG": price},
        "bond_yields": {"FROG-BOND-1Y": yield_},
        "fx_rates": {"USD": usd, "EUR": eur},
    }


class StressPathTests(unittest.TestCase):
    def setUp(self) -> None:
        self.holdings = load_holdings(ROOT / "data" / "holdings.json")
        self.paths = load_paths(ROOT / "data" / "paths.json")
        self.engine = PathEngine.default()

    def test_step_components_close_to_step_and_total_change(self) -> None:
        result = self.engine.run("closure", self.holdings, self.paths)
        self.assertTrue(result.complete)
        for path_result in result.results:
            for policy in path_result.policies:
                for step in policy.steps:
                    self.assertAlmostEqual(
                        step.market_effect
                        + step.trade_effect
                        + step.interaction_effect
                        + step.cash_flow_effect,
                        step.total_change,
                    )
                self.assertAlmostEqual(
                    policy.total_market_effect
                    + policy.total_trade_effect
                    + policy.total_interaction_effect
                    + policy.total_cash_flow_effect,
                    policy.total_change,
                )
                self.assertAlmostEqual(
                    sum(step.total_change for step in policy.steps),
                    policy.final_value - policy.initial_value,
                )
                self.assertAlmostEqual(
                    policy.total_change, policy.final_value - policy.initial_value
                )

    def test_hand_calculated_hold_and_rebalance_steps(self) -> None:
        path = make_path(
            [scenario("a", 50, 0.03, 7.1), scenario("b", 40, 0.03, 7.2)],
            step_extras={
                1: {
                    "cash_flows": [{"currency": "USD", "amount": 100}],
                    "trades": [{"holding_id": "frog-equity", "quantity_delta": -5}],
                }
            },
        )
        with tempfile.TemporaryDirectory() as directory:
            path_file = Path(directory) / "paths.json"
            path_file.write_text(json.dumps({"paths": [path]}), encoding="utf-8")
            loaded = load_paths(path_file)
        result = self.engine.run("hand", self.holdings, loaded)
        policies = {p.policy: p for p in result.results[0].policies}

        hold_step = policies["hold"].steps[0]
        # Market: equity 20*(40*7.2-50*7.1); cash 1000*(7.2-7.1); bond unchanged.
        self.assertAlmostEqual(
            hold_step.market_effect, 20 * (40 * 7.2 - 50 * 7.1) + 1000 * 0.1
        )
        self.assertAlmostEqual(hold_step.trade_effect, 0.0)
        self.assertAlmostEqual(hold_step.interaction_effect, 0.0)
        self.assertAlmostEqual(hold_step.cash_flow_effect, 100 * 7.2)

        re_step = policies["rebalance"].steps[0]
        # Trade valued at old market: -5 shares at 50 * 7.1.
        self.assertAlmostEqual(re_step.trade_effect, -5 * 50 * 7.1)
        # Interaction: those 5 shares no longer reprice at the new market.
        self.assertAlmostEqual(re_step.interaction_effect, 5 * (50 * 7.1 - 40 * 7.2))
        # Quantities are unchanged until the trade, so the market effect
        # matches the hold policy; the interaction isolates the traded shares.
        self.assertAlmostEqual(re_step.market_effect, hold_step.market_effect)
        self.assertAlmostEqual(
            re_step.market_effect + re_step.trade_effect + re_step.interaction_effect,
            hold_step.market_effect - 5 * 40 * 7.2,
        )

    def test_reinvested_cash_flow_appears_and_moves_with_fx(self) -> None:
        path = make_path(
            [
                scenario("a", 50, 0.03, 7.1),
                scenario("b", 50, 0.03, 7.1),
                scenario("c", 50, 0.03, 7.3),
            ],
            step_extras={1: {"cash_flows": [{"currency": "USD", "amount": 200}]}},
        )
        with tempfile.TemporaryDirectory() as directory:
            path_file = Path(directory) / "paths.json"
            path_file.write_text(json.dumps({"paths": [path]}), encoding="utf-8")
            loaded = load_paths(path_file)
        result = self.engine.run("reinvest", self.holdings, loaded)
        hold = next(p for p in result.results[0].policies if p.policy == "hold")

        first, second = hold.steps
        self.assertAlmostEqual(first.cash_flow_effect, 200 * 7.1)
        cash_line = next(
            c for c in second.contributions if c.holding_id == "reinvested-cash:USD"
        )
        self.assertAlmostEqual(cash_line.local_value, 200)
        self.assertAlmostEqual(cash_line.pnl_vs_base, 200 * (7.3 - 7.1))
        self.assertAlmostEqual(second.market_effect, 2200 * (7.3 - 7.1))

    def test_missing_step_data_keeps_partial_results(self) -> None:
        good = scenario("a", 50, 0.03, 7.1)
        mid = scenario("b", 45, 0.03, 7.1)
        broken = scenario("c", 40, 0.03, 7.1)
        broken["equity_prices"] = {}
        path = make_path([good, mid, broken])
        with tempfile.TemporaryDirectory() as directory:
            path_file = Path(directory) / "paths.json"
            path_file.write_text(json.dumps({"paths": [path]}), encoding="utf-8")
            loaded = load_paths(path_file)
            result = self.engine.run("partial", self.holdings, loaded)
            self.assertFalse(result.complete)
            path_result = result.results[0]
            self.assertEqual(path_result.status, "partial")
            self.assertIn("FROG", path_result.missing_data)
            for policy in path_result.policies:
                self.assertFalse(policy.complete)
                self.assertEqual(len(policy.steps), 1)
                self.assertAlmostEqual(
                    policy.total_change, policy.final_value - policy.initial_value
                )
            store = ResultStore(directory)
            store.save(result)
            loaded_result = load_path_run_result(directory, "partial")
            self.assertEqual(loaded_result, result)
            self.assertEqual(loaded_result.results[0].status, "partial")

    def test_versions_replay_side_by_side_and_close(self) -> None:
        result = self.engine.run("versions", self.holdings, self.paths)
        table = compare_path_results([result])
        columns = table["stagflation_ladder"]
        self.assertEqual(
            set(columns),
            {"v1/hold", "v1/rebalance", "v2/hold", "v2/rebalance"},
        )
        for key, column in columns.items():
            self.assertEqual(column["status"], "complete")
            self.assertAlmostEqual(
                column["market_effect"]
                + column["trade_effect"]
                + column["interaction_effect"]
                + column["cash_flow_effect"],
                column["total_change"],
            )
            self.assertAlmostEqual(
                column["final_value"] - column["initial_value"],
                column["total_change"],
            )
        # Same endpoints, different ordering: totals match, step split differs.
        self.assertAlmostEqual(
            columns["v1/hold"]["total_change"], columns["v2/hold"]["total_change"]
        )
        self.assertNotEqual(
            columns["v1/hold"]["fingerprint"], columns["v2/hold"]["fingerprint"]
        )

    def test_result_store_round_trip(self) -> None:
        result = self.engine.run("round-trip", self.holdings, self.paths)
        with tempfile.TemporaryDirectory() as directory:
            store = ResultStore(directory)
            store.save(result)
            loaded = load_path_run_result(directory, "round-trip")
        self.assertEqual(result, loaded)

    def test_cli_publishes_path_results(self) -> None:
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
            published = Path(directory) / "cli-paths-paths.json"
            self.assertTrue(published.exists())
            payload = json.loads(published.read_text(encoding="utf-8"))
            self.assertEqual(payload["run_id"], "cli-paths-paths")


if __name__ == "__main__":
    unittest.main()
