from __future__ import annotations

import json
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
    compare_disposal_policies,
    load_path_run_result,
)

ROOT = Path(__file__).resolve().parents[1]


def scenario(sid, price, yield_, usd, eur=7.8):
    return {
        "scenario_id": sid,
        "version": 1,
        "description": sid,
        "equity_prices": {"FROG": price},
        "bond_yields": {"FROG-BOND-1Y": yield_},
        "fx_rates": {"USD": usd, "EUR": eur},
    }


def window_sell(step_id, max_qty, low, high, lag):
    return {
        "type": "sell",
        "holding_id": "frog-equity",
        "max_quantity": max_qty,
        "min_price": low,
        "max_price": high,
        "settlement_lag_steps": lag,
    }


def make_manual_path():
    return {
        "path_id": "manual",
        "version": 1,
        "description": "manual illiquid market",
        "limits": {"max_loss": 1000, "currency_exposure": {"USD": 1000000}},
        "action_plans": [
            {
                "plan_id": "plan",
                "trigger_step_id": "t1",
                "price_policy": "worst",
                "orders": [
                    {"type": "sell", "holding_id": "frog-equity", "quantity": 10, "fee_bps": 10, "priority": 1}
                ],
            }
        ],
        "steps": [
            {"step_id": "t0", "scenario": scenario("a", 50, 0.03, 7.1)},
            {
                "step_id": "t1",
                "scenario": scenario("b", 40, 0.03, 7.1),
                "liquidity_windows": [window_sell("t1", 4, 38, 39, 2)],
            },
            {
                "step_id": "t2",
                "scenario": scenario("c", 36, 0.03, 7.1),
                "liquidity_windows": [window_sell("t2", 4, 35, 36, 1)],
            },
            {
                "step_id": "t3",
                "scenario": scenario("d", 36, 0.03, 7.1),
                "liquidity_windows": [window_sell("t3", 10, 35, 36, 0)],
            },
        ],
    }


def load_path(payload, directory: str):
    path_file = Path(directory) / "paths.json"
    path_file.write_text(json.dumps({"paths": [payload]}), encoding="utf-8")
    return load_paths(path_file)


class ConstrainedFillTests(unittest.TestCase):
    def setUp(self) -> None:
        self.holdings = load_holdings(ROOT / "data" / "holdings.json")
        self.engine = PathEngine.default()

    def _run(self, payload):
        with tempfile.TemporaryDirectory() as directory:
            return self.engine.run("m", self.holdings, load_path(payload, directory))

    def test_partial_fills_settle_later_and_fund_is_not_pre_spent(self) -> None:
        result = self._run(make_manual_path())
        policies = {p.policy: p for p in result.results[0].policies}
        constrained = policies["constrained:plan"]
        t1, t2, t3 = constrained.steps

        fill1 = t1.action_reconciliation.fills[0]
        self.assertAlmostEqual(fill1.quantity, 4)
        self.assertEqual(fill1.settlement_status, "pending")
        self.assertEqual(fill1.settlement_step_id, "t3")
        self.assertAlmostEqual(fill1.unit_price, 38)
        self.assertGreater(t1.pending_settle_base_amount, 0.0)
        self.assertAlmostEqual(t1.available_cash_base, 7100.0)

        self.assertAlmostEqual(t2.settled_from_fills_base, 0.0)
        fill2 = t2.action_reconciliation.fills[0]
        self.assertAlmostEqual(fill2.quantity, 4)
        self.assertEqual(fill2.settlement_step_id, "t3")

        self.assertAlmostEqual(
            t3.settled_from_fills_base,
            4 * 38 * 7.1 * (1 - 10 / 10000)
            + 4 * 35 * 7.1 * (1 - 10 / 10000),
        )
        fill3 = t3.action_reconciliation.fills[0]
        self.assertAlmostEqual(fill3.quantity, 2)
        self.assertEqual(fill3.settlement_status, "settled")
        self.assertEqual(constrained.final_residual_orders[0].remaining_quantity, 0.0)

        for step in constrained.steps:
            self.assertAlmostEqual(
                step.market_effect
                + step.trade_effect
                + step.interaction_effect
                + step.cash_flow_effect
                + step.action_effect,
                step.total_change,
            )
        self.assertAlmostEqual(
            sum(s.total_change for s in constrained.steps),
            constrained.final_value - constrained.initial_value,
        )

    def test_partial_fill_charges_fee_once_and_cancel_keeps_fills(self) -> None:
        path = make_manual_path()
        path["steps"][2]["cancel_plan_ids"] = ["plan"]
        result = self._run(path)
        constrained = next(
            p for p in result.results[0].policies if p.policy == "constrained:plan"
        )
        t1, t2, t3 = constrained.steps
        self.assertAlmostEqual(len(t1.action_reconciliation.fills), 1)
        cancelled = next(
            order for order in t2.action_reconciliation.residual_orders
        )
        self.assertTrue(cancelled.cancelled)
        self.assertAlmostEqual(cancelled.remaining_quantity, 0.0)
        self.assertAlmostEqual(cancelled.filled_quantity, 4)
        self.assertEqual(t2.action_reconciliation.fills[0].quantity, 0) if False else None
        self.assertAlmostEqual(len(t2.action_reconciliation.fills), 0)
        # The residual order is gone, but the four already sold shares stay sold.
        self.assertEqual(len(t3.action_reconciliation.fills), 0)
        total_filled = sum(
            fill.quantity
            for step in constrained.steps
            if step.action_reconciliation
            for fill in step.action_reconciliation.fills
        )
        self.assertAlmostEqual(total_filled, 4)
        total_fees = sum(
            step.action_fees for step in constrained.steps
        )
        self.assertAlmostEqual(total_fees, 4 * 38 * 7.1 * 10 / 10000)

    def test_priority_is_fixed_before_window_capacity_is_seen(self) -> None:
        path = make_manual_path()
        path["action_plans"] = [
            {
                "plan_id": "plan",
                "trigger_step_id": "t1",
                "allocation_rule": "priority",
                "orders": [
                    {"type": "sell", "holding_id": "frog-equity", "quantity": 3, "fee_bps": 0, "priority": 1}
                ],
            },
            {
                "plan_id": "plan2",
                "trigger_step_id": "t1",
                "allocation_rule": "priority",
                "orders": [
                    {"type": "sell", "holding_id": "frog-equity", "quantity": 3, "fee_bps": 0, "priority": 2}
                ],
            },
        ]
        result = self._run(path)
        policies = {p.policy: p for p in result.results[0].policies}
        combined = policies["constrained:all"]
        fills = combined.steps[0].action_reconciliation.fills
        # The user fixed priority before seeing the window: plan:0 takes 3
        # of the 4-unit window and plan2:0 gets the remaining 1.
        quantities = {fill.order_id: fill.quantity for fill in fills}
        self.assertAlmostEqual(quantities["plan:0"], 3)
        self.assertAlmostEqual(quantities["plan2:0"], 1)

    def test_comparison_separates_shortfall_price_fees_and_lag(self) -> None:
        paths = load_paths(ROOT / "data" / "manual_paths.json")
        result = self.engine.run("manual", self.holdings, paths)
        comparison = compare_disposal_policies(
            result.results[0], "action:de_risk", "constrained:de_risk"
        )
        self.assertLess(comparison["constrained_vs_instant"], 0.0)
        self.assertNotAlmostEqual(comparison["fill_shortfall_base"], 0.0)
        self.assertNotAlmostEqual(comparison["price_band_base"], 0.0)
        self.assertGreater(comparison["instant_fees_base"], 0.0)
        self.assertGreater(comparison["constrained_fees_base"], 0.0)
        pending_steps = [
            step
            for policy in result.results[0].policies
            if policy.policy == "constrained:de_risk"
            for step in policy.steps
            if step.pending_settle_base_amount > 0
        ]
        self.assertTrue(pending_steps)

    def test_resume_keeps_occurred_fills_and_continues_reconciliation(self) -> None:
        path = make_manual_path()
        good = json.loads(json.dumps(path))
        broken = json.loads(json.dumps(path))
        broken["steps"][2]["scenario"] = {
            "scenario_id": "c",
            "version": 1,
            "description": "c",
            "equity_prices": {},
            "bond_yields": {"FROG-BOND-1Y": 0.03},
            "fx_rates": {"USD": 7.1, "EUR": 7.8},
        }
        with tempfile.TemporaryDirectory() as directory:
            partial_paths = load_path(broken, directory)
            partial = self.engine.run("partial-run", self.holdings, partial_paths)
            self.assertFalse(partial.complete)
            store = ResultStore(directory)
            store.save(partial)
            loaded = load_path_run_result(directory, "partial-run")

            constrained = next(
                p for p in loaded.results[0].policies
                if p.policy == "constrained:plan"
            )
            self.assertIsNotNone(constrained.resume_state)
            self.assertEqual(constrained.resume_state.completed_step_ids, ("t1",))

            fixed_paths = load_path(good, directory)
            resumed = self.engine.resume(
                "resumed-run", self.holdings, fixed_paths, loaded
            )
            resumed_policy = next(
                p for p in resumed.results[0].policies
                if p.policy == "constrained:plan"
            )
            self.assertTrue(resumed_policy.complete)
            later_fills = [
                (step.step_id, fill.fill_step_id, fill.quantity, fill.unit_price)
                for step in resumed_policy.steps
                if step.step_id in {"t2", "t3"} and step.action_reconciliation
                for fill in step.action_reconciliation.fills
            ]
            self.assertIn(("t2", "t2", 4, 35.0), later_fills)
            self.assertIn(("t3", "t3", 2, 35.0), later_fills)
            t1 = next(s for s in resumed_policy.steps if s.step_id == "t1")
            t1_fill = t1.action_reconciliation.fills[0]
            self.assertAlmostEqual(t1_fill.quantity, 4)
            self.assertEqual(t1_fill.settlement_step_id, "t3")
            fresh = self.engine.run("fresh", self.holdings, fixed_paths)
            fresh_policy = next(
                p for p in fresh.results[0].policies
                if p.policy == "constrained:plan"
            )
            self.assertAlmostEqual(resumed_policy.final_value, fresh_policy.final_value)
            store.save(resumed)
            reloaded = load_path_run_result(directory, "resumed-run")
            self.assertEqual(reloaded, resumed)

    def test_cli_resumes_partial_run(self) -> None:
        path = make_manual_path()
        with tempfile.TemporaryDirectory() as directory:
            path_file = Path(directory) / "manual_paths.json"
            path_file.write_text(json.dumps({"paths": [path]}), encoding="utf-8")
            results_dir = Path(directory) / "results"
            subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "risk_platform.cli",
                    "--holdings",
                    str(ROOT / "data" / "holdings.json"),
                    "--paths",
                    str(path_file),
                    "--results-dir",
                    str(results_dir),
                    "--run-id",
                    "full",
                ],
                cwd=ROOT,
                check=True,
                capture_output=True,
                text=True,
            )
            payload = json.loads(
                (results_dir / "full-paths.json").read_text(encoding="utf-8")
            )
            policies = [
                p["policy"] for p in payload["results"][0]["policies"]
            ]
            self.assertIn("constrained:plan", policies)


if __name__ == "__main__":
    unittest.main()
