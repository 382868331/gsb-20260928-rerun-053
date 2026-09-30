from __future__ import annotations

import math
import tempfile
import unittest
from pathlib import Path

from risk_platform.attribution import FACTOR_EFFECT_FIELDS, attribute_portfolio
from risk_platform.engine import RevaluationEngine
from risk_platform.holdings import load_holdings
from risk_platform.results import ResultStore
from risk_platform.scenarios import MissingMarketDataError, load_scenarios

ROOT = Path(__file__).resolve().parents[1]

FACTOR_FIELDS = FACTOR_EFFECT_FIELDS


class AttributionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.holdings = load_holdings(ROOT / "data" / "holdings.json")
        self.scenarios = load_scenarios(ROOT / "data" / "scenarios.json")
        self.engine = RevaluationEngine.default()

    def test_combined_scenario_terms_tie_out_per_holding_and_total(self) -> None:
        run = self.engine.run("combo", self.holdings, self.scenarios)
        combo = next(item for item in run.results if item.scenario_id == "risk_off_combo")

        for contribution in combo.contributions:
            attributed = sum(getattr(contribution, field) for field in FACTOR_FIELDS)
            self.assertAlmostEqual(attributed, contribution.pnl_vs_base, places=9)

        total_attributed = sum(getattr(combo, field) for field in FACTOR_FIELDS)
        self.assertAlmostEqual(total_attributed, combo.total_pnl_vs_base, places=9)
        self.assertAlmostEqual(
            combo.total_pnl_vs_base,
            sum(item.pnl_vs_base for item in combo.contributions),
            places=9,
        )

    def test_equity_holding_attribution_is_manually_calculable(self) -> None:
        run = self.engine.run("manual", self.holdings, self.scenarios)
        combo = next(item for item in run.results if item.scenario_id == "risk_off_combo")
        equity = next(item for item in combo.contributions if item.holding_id == "frog-equity")

        # Corners (q=20, base price 50 -> 40, base FX 7.1 -> 7.2):
        v000 = 20 * 50 * 7.1
        v100 = 20 * 40 * 7.1
        v001 = 20 * 50 * 7.2
        v101 = 20 * 40 * 7.2

        self.assertAlmostEqual(equity.base_scenario_value, v000)
        self.assertAlmostEqual(equity.base_value, v101)
        self.assertAlmostEqual(equity.factor_equity_price, v100 - v000)
        self.assertAlmostEqual(equity.factor_fx, v001 - v000)
        self.assertAlmostEqual(
            equity.interaction_equity_fx,
            v101 - v100 - v001 + v000,
        )
        # Bond yield does not move an equity position.
        self.assertEqual(equity.factor_bond_yield, 0.0)
        self.assertEqual(equity.interaction_equity_yield, 0.0)
        self.assertEqual(equity.interaction_yield_fx, 0.0)
        self.assertEqual(equity.interaction_all, 0.0)
        self.assertAlmostEqual(equity.pnl_vs_base, v101 - v000)

    def test_bond_holding_attribution_is_manually_calculable(self) -> None:
        run = self.engine.run("manual-bond", self.holdings, self.scenarios)
        combo = next(item for item in run.results if item.scenario_id == "risk_off_combo")
        bond = next(item for item in combo.contributions if item.holding_id == "euro-bond")

        # Corners: q=10000, y 0.03 -> 0.05, t=1, FX 7.8 -> 7.5.
        v000 = 10000 * math.exp(-0.03) * 7.8
        v010 = 10000 * math.exp(-0.05) * 7.8
        v001 = 10000 * math.exp(-0.03) * 7.5
        v011 = 10000 * math.exp(-0.05) * 7.5

        self.assertAlmostEqual(bond.factor_bond_yield, v010 - v000, places=9)
        self.assertAlmostEqual(bond.factor_fx, v001 - v000, places=9)
        self.assertAlmostEqual(
            bond.interaction_yield_fx,
            v011 - v010 - v001 + v000,
            places=9,
        )
        self.assertEqual(bond.factor_equity_price, 0.0)
        self.assertEqual(bond.interaction_equity_yield, 0.0)
        self.assertEqual(bond.interaction_equity_fx, 0.0)
        self.assertEqual(bond.interaction_all, 0.0)
        self.assertAlmostEqual(bond.pnl_vs_base, v011 - v000, places=9)

    def test_cash_only_has_fx_contribution(self) -> None:
        run = self.engine.run("manual-cash", self.holdings, self.scenarios)
        combo = next(item for item in run.results if item.scenario_id == "risk_off_combo")
        cash = next(item for item in combo.contributions if item.holding_id == "usd-cash")

        self.assertAlmostEqual(cash.factor_fx, 1000 * (7.2 - 7.1))
        self.assertEqual(cash.factor_equity_price, 0.0)
        self.assertEqual(cash.factor_bond_yield, 0.0)
        for field in (
            "interaction_equity_yield",
            "interaction_equity_fx",
            "interaction_yield_fx",
            "interaction_all",
        ):
            self.assertEqual(getattr(cash, field), 0.0)

    def test_single_factor_scenarios_have_no_interactions(self) -> None:
        run = self.engine.run("single", self.holdings, self.scenarios)
        crash = next(item for item in run.results if item.scenario_id == "equity_crash")
        # equity_crash moves equity price and USD FX at once, so their pair
        # interaction is non-zero while yield terms must vanish.
        self.assertEqual(crash.factor_bond_yield, 0.0)
        self.assertEqual(crash.interaction_equity_yield, 0.0)
        self.assertEqual(crash.interaction_yield_fx, 0.0)
        self.assertEqual(crash.interaction_all, 0.0)
        self.assertNotAlmostEqual(crash.interaction_equity_fx, 0.0)

    def test_missing_data_in_combined_scenario_publishes_nothing(self) -> None:
        broken = [
            scenario.__class__.from_dict(
                {**scenario.canonical_dict(), "bond_yields": {}}
            )
            if scenario.scenario_id == "risk_off_combo"
            else scenario
            for scenario in self.scenarios
        ]
        with tempfile.TemporaryDirectory() as directory:
            store = ResultStore(directory)
            with self.assertRaises(MissingMarketDataError):
                self.engine.run("incomplete", self.holdings, broken)
            self.assertEqual(list(Path(directory).iterdir()), [])

    def test_same_holdings_under_different_market_versions_stay_bound(self) -> None:
        run_v2 = self.engine.run("v2-run", self.holdings, self.scenarios)
        old_scenarios = [
            scenario.__class__.from_dict(
                {**scenario.canonical_dict(), "version": 1}
            )
            for scenario in self.scenarios
        ]
        run_v1 = self.engine.run("v1-run", self.holdings, old_scenarios)

        with tempfile.TemporaryDirectory() as directory:
            store = ResultStore(directory)
            store.save(run_v1)
            store.save(run_v2)

            loaded_v1 = store.load("v1-run")
            loaded_v2 = store.load("v2-run")
            combo_v1 = next(
                item for item in loaded_v1.results if item.scenario_id == "risk_off_combo"
            )
            combo_v2 = next(
                item for item in loaded_v2.results if item.scenario_id == "risk_off_combo"
            )
            self.assertEqual(combo_v1.scenario_version, 1)
            self.assertEqual(combo_v2.scenario_version, 2)
            self.assertNotEqual(
                combo_v1.scenario_fingerprint, combo_v2.scenario_fingerprint
            )
            # Same content except version -> identical valuation and terms.
            for field in ("total_base_value", "total_pnl_vs_base", *FACTOR_FIELDS):
                self.assertAlmostEqual(
                    getattr(combo_v1, field), getattr(combo_v2, field), places=9
                )

    def test_attribution_uses_validated_corner_slices(self) -> None:
        base = next(item for item in self.scenarios if item.scenario_id == "base")
        stress = next(item for item in self.scenarios if item.scenario_id == "risk_off_combo")
        base.validate_holdings(self.holdings)
        stress.validate_holdings(self.holdings)
        attributions = {
            item.holding_id: item
            for item in attribute_portfolio(self.holdings, self.engine.pricers, base, stress)
        }
        # Every factor term sum equals a direct corner revaluation difference.
        total_pnl = sum(item.total_pnl() for item in attributions.values())
        expected = 20 * 40 * 7.2 - 20 * 50 * 7.1
        expected += 10000 * math.exp(-0.05) * 7.5 - 10000 * math.exp(-0.03) * 7.8
        expected += 1000 * (7.2 - 7.1)
        self.assertAlmostEqual(total_pnl, expected, places=9)


if __name__ == "__main__":
    unittest.main()
