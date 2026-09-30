from __future__ import annotations

import json
import math
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from risk_platform.engine import RevaluationEngine
from risk_platform.holdings import load_holdings
from risk_platform.metrics import LargestLossContributionMetric, PnLMetric, calculate_metrics
from risk_platform.results import ResultStore
from risk_platform.scenarios import MissingMarketDataError, load_scenarios

ROOT = Path(__file__).resolve().parents[1]


class RiskPlatformTests(unittest.TestCase):
    def setUp(self) -> None:
        self.holdings = load_holdings(ROOT / "data" / "holdings.json")
        self.scenarios = load_scenarios(ROOT / "data" / "scenarios.json")
        self.engine = RevaluationEngine.default()

    def test_fictional_holdings_are_calculated_directly(self) -> None:
        run = self.engine.run("manual-check", self.holdings, self.scenarios)
        base = next(item for item in run.results if item.scenario_id == "base")
        values = {item.holding_id: item for item in base.contributions}
        expected_bond = 10000 * math.exp(-0.03) * 7.8

        self.assertAlmostEqual(values["usd-cash"].local_value, 1000)
        self.assertAlmostEqual(values["usd-cash"].base_value, 7100)
        self.assertAlmostEqual(values["frog-equity"].local_value, 1000)
        self.assertAlmostEqual(values["frog-equity"].base_value, 7100)
        self.assertAlmostEqual(values["euro-bond"].local_value, 10000 * math.exp(-0.03))
        self.assertAlmostEqual(values["euro-bond"].base_value, expected_bond)
        self.assertAlmostEqual(base.total_base_value, 14200 + expected_bond)

    def test_stress_uses_prices_and_fx_from_same_scenario(self) -> None:
        run = self.engine.run("same-scenario", self.holdings, self.scenarios)
        crash = next(item for item in run.results if item.scenario_id == "equity_crash")
        values = {item.holding_id: item for item in crash.contributions}

        self.assertAlmostEqual(values["frog-equity"].local_value, 20 * 35)
        self.assertAlmostEqual(values["frog-equity"].fx_rate, 7.25)
        self.assertAlmostEqual(values["frog-equity"].base_value, 20 * 35 * 7.25)
        self.assertAlmostEqual(values["usd-cash"].base_value, 7250)
        self.assertAlmostEqual(values["euro-bond"].fx_rate, 7.8)
        self.assertAlmostEqual(crash.total_pnl_vs_base, -1875)
        self.assertAlmostEqual(
            crash.total_pnl_vs_base,
            sum(item.pnl_vs_base for item in crash.contributions),
        )

    def test_factor_attribution_matches_hand_calculated_effects(self) -> None:
        run = self.engine.run("attribution", self.holdings, self.scenarios)
        crash = next(item for item in run.results if item.scenario_id == "equity_crash")
        effects = {
            (item.holding_id, item.factors): item.effect
            for item in crash.factor_effects
        }

        # Equity: 20 shares, FROG 50 -> 35, USD 7.1 -> 7.25.
        self.assertAlmostEqual(effects[("frog-equity", ("price",))], 20 * 35 * 7.1 - 7100)
        self.assertAlmostEqual(effects[("frog-equity", ("fx",))], 20 * 50 * 7.25 - 7100)
        self.assertAlmostEqual(
            effects[("frog-equity", ("fx", "price"))],
            20 * 35 * 7.25 - 20 * 35 * 7.1 - 20 * 50 * 7.25 + 7100,
        )
        # Cash only moves with FX.
        self.assertAlmostEqual(effects[("usd-cash", ("fx",))], 150)
        # Bond is untouched in this scenario.
        self.assertAlmostEqual(effects[("euro-bond", ("fx",))], 0.0)
        self.assertAlmostEqual(effects[("euro-bond", ("rates",))], 0.0)
        self.assertAlmostEqual(effects[("euro-bond", ("fx", "rates"))], 0.0)

        # Effects decompose the total exactly, with no residual allocation.
        self.assertAlmostEqual(
            sum(item.effect for item in crash.factor_effects),
            crash.total_pnl_vs_base,
        )

    def test_rates_fx_interaction_matches_hand_calculation(self) -> None:
        run = self.engine.run("attribution-rates", self.holdings, self.scenarios)
        shock = next(item for item in run.results if item.scenario_id == "rates_up_fx_shock")
        effects = {
            (item.holding_id, item.factors): item.effect
            for item in shock.factor_effects
        }

        base_bond = 10000 * math.exp(-0.03) * 7.8
        rates_only = 10000 * math.exp(-0.04) * 7.8
        fx_only = 10000 * math.exp(-0.03) * 7.6
        both = 10000 * math.exp(-0.04) * 7.6
        self.assertAlmostEqual(effects[("euro-bond", ("rates",))], rates_only - base_bond)
        self.assertAlmostEqual(effects[("euro-bond", ("fx",))], fx_only - base_bond)
        self.assertAlmostEqual(
            effects[("euro-bond", ("fx", "rates"))],
            both - rates_only - fx_only + base_bond,
        )
        self.assertAlmostEqual(
            sum(item.effect for item in shock.factor_effects),
            shock.total_pnl_vs_base,
        )

    def test_factor_effects_survive_result_store_round_trip(self) -> None:
        run = self.engine.run("round-trip", self.holdings, self.scenarios)
        with tempfile.TemporaryDirectory() as directory:
            store = ResultStore(directory)
            store.save(run)
            loaded = store.load("round-trip")
        self.assertEqual(run, loaded)
        shock = next(item for item in loaded.results if item.scenario_id == "rates_up_fx_shock")
        self.assertTrue(shock.factor_effects)
        self.assertAlmostEqual(
            sum(item.effect for item in shock.factor_effects),
            shock.total_pnl_vs_base,
        )

    def test_missing_market_data_fails_without_publishing_total(self) -> None:
        scenarios = load_scenarios(ROOT / "data" / "scenarios.json")
        broken = [
            scenario.__class__.from_dict({**scenario.canonical_dict(), "equity_prices": {}})
            if scenario.scenario_id == "rates_up_fx_shock"
            else scenario
            for scenario in scenarios
        ]

        with tempfile.TemporaryDirectory() as directory:
            store = ResultStore(directory)
            with self.assertRaises(MissingMarketDataError):
                self.engine.run("must-not-publish", self.holdings, broken)
            self.assertEqual(list(Path(directory).iterdir()), [])

    def test_old_run_remains_traceable_after_scenario_update(self) -> None:
        old_data = {
            "scenarios": [
                {
                    "scenario_id": "base",
                    "version": 1,
                    "description": "Old baseline",
                    "equity_prices": {"FROG": 48},
                    "bond_yields": {"FROG-BOND-1Y": 0.035},
                    "fx_rates": {"USD": 7.0, "EUR": 7.7},
                }
            ]
        }
        with tempfile.TemporaryDirectory() as directory:
            scenario_path = Path(directory) / "scenarios.json"
            scenario_path.write_text(json.dumps(old_data), encoding="utf-8")
            old_scenarios = load_scenarios(scenario_path)
            store = ResultStore(Path(directory) / "results")
            old_run = self.engine.run("old-v1", self.holdings, old_scenarios)
            store.save(old_run)
            new_run = self.engine.run("new-v2", self.holdings, self.scenarios)
            store.save(new_run)

            loaded_old = store.load("old-v1")
            self.assertEqual(loaded_old.base_scenario_version, 1)
            self.assertEqual(loaded_old.base_scenario_fingerprint, old_scenarios[0].fingerprint)
            self.assertNotEqual(loaded_old.base_scenario_fingerprint, new_run.base_scenario_fingerprint)
            self.assertEqual(store.load("new-v2").base_scenario_version, 2)

    def test_new_risk_metrics_reuse_published_scenario_results(self) -> None:
        run = self.engine.run("metrics", self.holdings, self.scenarios)
        values = calculate_metrics(run, [PnLMetric(), LargestLossContributionMetric()])
        self.assertAlmostEqual(values["equity_crash"]["pnl_vs_base"], -1875)
        self.assertAlmostEqual(
            values["equity_crash"]["largest_loss_contribution"],
            -2025,
        )

    def test_cli_publishes_named_result(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            completed = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "risk_platform.cli",
                    "--holdings",
                    str(ROOT / "data" / "holdings.json"),
                    "--scenarios",
                    str(ROOT / "data" / "scenarios.json"),
                    "--results-dir",
                    directory,
                    "--run-id",
                    "cli-check",
                ],
                cwd=ROOT,
                check=True,
                capture_output=True,
                text=True,
            )
            self.assertTrue(Path(completed.stdout.strip()).exists())


if __name__ == "__main__":
    unittest.main()
