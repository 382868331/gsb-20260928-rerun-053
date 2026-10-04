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
    compare_disposal_policies,
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
                        + step.cash_flow_effect
                        + step.action_effect,
                        step.total_change,
                    )
                self.assertAlmostEqual(
                    policy.total_market_effect
                    + policy.total_trade_effect
                    + policy.total_interaction_effect
                    + policy.total_cash_flow_effect
                    + policy.total_action_effect,
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
            {
                "v1/hold",
                "v1/rebalance",
                "v1/action:sell_equity",
                "v1/action:sell_and_buy_eur",
                "v2/hold",
                "v2/rebalance",
                "v2/action:sell_equity",
                "v2/action:sell_and_buy_eur",
                "v1/constrained:sell_equity",
                "v1/constrained:sell_and_buy_eur",
                "v2/constrained:sell_equity",
                "v2/constrained:sell_and_buy_eur",
            },
        )
        for key, column in columns.items():
            self.assertEqual(column["status"], "complete")
            self.assertAlmostEqual(
                column["market_effect"]
                + column["trade_effect"]
                + column["interaction_effect"]
                + column["cash_flow_effect"]
                + column["action_effect"],
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

    def test_first_trigger_records_each_limit_and_independent_resolution(self) -> None:
        path = make_path(
            [
                scenario("base", 50, 0.03, 7.1),
                scenario("stress", 40, 0.03, 7.1),
            ],
        )
        path["limits"] = {
            "max_loss": 5000,
            "currency_exposure": {"USD": 10000},
            "asset_concentration": {"frog-equity": 0.07},
        }
        path["action_plans"] = [
            {
                "plan_id": "sell_ten",
                "trigger_step_id": "t0",
                "orders": [
                    {"type": "sell", "holding_id": "frog-equity", "quantity": 10, "fee_bps": 10}
                ],
            }
        ]
        with tempfile.TemporaryDirectory() as directory:
            path_file = Path(directory) / "paths.json"
            path_file.write_text(json.dumps({"paths": [path]}), encoding="utf-8")
            loaded = load_paths(path_file)
        result = self.engine.run("limits", self.holdings, loaded)
        path_result = result.results[0]
        self.assertEqual(path_result.first_trigger_step_id, "t0")
        self.assertEqual(
            path_result.trigger_basis_fingerprint, path_result.path_fingerprint
        )
        policies = {item.policy: item for item in path_result.policies}
        hold = policies["hold"]
        self.assertEqual(hold.first_trigger_step_id, "t0")
        self.assertEqual(
            {item.metric for item in hold.first_breaches},
            {"currency_exposure:USD", "asset_concentration:frog-equity"},
        )
        sources = {item.metric: item.source for item in hold.first_breaches}
        self.assertEqual(sources["currency_exposure:USD"], "currency:USD")
        self.assertEqual(sources["asset_concentration:frog-equity"], "asset:frog-equity")
        self.assertTrue(
            all(
                item.trigger_step_id == "t0"
                for item in hold.first_breaches
            )
        )

        sell_only = policies["action:sell_ten"]
        unresolved_metrics = {item.metric for item in sell_only.unresolved_metrics}
        self.assertIn("currency_exposure:USD", unresolved_metrics)
        self.assertNotIn("asset_concentration:frog-equity", unresolved_metrics)

    def test_action_costs_cash_flow_and_reconciliation_do_not_double_count(self) -> None:
        result = self.engine.run("actions", self.holdings, self.paths)
        policies = {
            item.policy: item
            for item in result.results[0].policies
        }
        sell_only = policies["action:sell_equity"]
        sell_step = sell_only.steps[0]
        self.assertAlmostEqual(sell_step.action_fees, 10 * 40 * 7.1 * 10 / 10000)
        self.assertAlmostEqual(
            sell_step.cash_flow_from_actions, 10 * 40 * 7.1
        )
        self.assertAlmostEqual(
            sell_step.action_reconciliation.post_action_value
            - sell_step.action_reconciliation.pre_action_value,
            sell_step.action_reconciliation.action_effect,
        )
        self.assertAlmostEqual(sell_step.action_effect, -sell_step.action_fees)

        hedge = policies["action:sell_and_buy_eur"]
        hedge_step = hedge.steps[0]
        self.assertEqual(len(hedge_step.action_reconciliation.fills), 2)
        self.assertAlmostEqual(
            hedge_step.cash_flow_from_actions, 10 * 40 * 7.1
        )
        self.assertAlmostEqual(hedge_step.action_effect, -hedge_step.action_fees)

    def test_missing_future_data_keeps_confirmed_breach_and_unconfirmed_metrics(self) -> None:
        good = scenario("a", 50, 0.03, 7.1)
        breach = scenario("b", 40, 0.03, 7.1)
        broken = scenario("c", 40, 0.03, 7.1)
        broken["fx_rates"] = {}
        path = make_path(
            [good, breach, broken],
            step_extras={
                1: {
                    "cash_flows": [{"currency": "USD", "amount": 0}],
                }
            },
        )
        path["limits"] = {
            "max_loss": 100,
            "currency_exposure": {"USD": 1000000},
        }
        with tempfile.TemporaryDirectory() as directory:
            path_file = Path(directory) / "paths.json"
            path_file.write_text(json.dumps({"paths": [path]}), encoding="utf-8")
            loaded = load_paths(path_file)
            result = self.engine.run("partial-limits", self.holdings, loaded)
        hold = result.results[0].policies[0]
        self.assertEqual(hold.first_trigger_step_id, "t1")
        self.assertEqual(hold.first_breaches[0].metric, "loss")
        self.assertEqual(
            {item.metric: item.status for item in hold.unresolved_metrics},
            {"loss": "unconfirmed", "currency_exposure:USD": "unconfirmed"},
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
            policies = [
                policy["policy"]
                for policy in payload["results"][0]["policies"]
            ]
            self.assertIn("action:sell_equity", policies)
            self.assertIn("action:sell_and_buy_eur", policies)

    def test_constrained_fills_settle_and_keep_future_funds_unusable(self) -> None:
        path = make_path(
            [
                scenario("a", 50, 0.03, 7.1),
                scenario("b", 40, 0.03, 7.1),
                scenario("c", 40, 0.03, 7.3),
                scenario("d", 40, 0.03, 7.3),
            ],
        )
        path["limits"] = {"asset_concentration": {"frog-equity": 0.01}}
        path["action_plans"] = [
            {
                "plan_id": "partial_sell",
                "trigger_step_id": "t0",
                "allocation": "priority",
                "fill_price": "worst",
                "orders": [
                    {"order_id": "s1", "type": "sell", "holding_id": "frog-equity", "quantity": 10, "fee_bps": 10}
                ],
            }
        ]
        path["steps"][1]["fill_windows"] = [
            {"kind": "sell", "key": "frog-equity", "capacity": 4, "price_low": 39, "price_high": 40, "settlement_steps": 1}
        ]
        with tempfile.TemporaryDirectory() as directory:
            path_file = Path(directory) / "paths.json"
            path_file.write_text(json.dumps({"paths": [path]}), encoding="utf-8")
            loaded = load_paths(path_file)
        result = self.engine.run("constrained", self.holdings, loaded)
        policy = next(
            item for item in result.results[0].policies
            if item.policy == "constrained:partial_sell"
        )
        fill = policy.steps[1].action_reconciliation.fills[0]
        self.assertEqual(fill.quantity, 4)
        self.assertEqual(fill.unfilled_quantity, 6)
        self.assertEqual(fill.fill_price, 39)
        self.assertEqual(fill.settlement_step_id, "t2")
        self.assertAlmostEqual(policy.steps[1].execution_ledger.quantities["frog-equity"], 16)
        self.assertNotIn("USD", policy.steps[1].execution_ledger.available_cash)
        self.assertAlmostEqual(
            policy.steps[1].execution_ledger.pending_cash_local["USD"],
            4 * 39 * (1 - 10 / 10000),
        )
        self.assertAlmostEqual(
            policy.steps[2].pending_settled_base_amount,
            4 * 39 * 7.1 * (1 - 10 / 10000),
        )
        for step in policy.steps:
            self.assertAlmostEqual(
                step.market_effect
                + step.cash_flow_effect
                + step.action_effect,
                step.total_change,
            )
        self.assertIn(
            "USD",
            policy.steps[2].execution_ledger.available_cash,
        )

    def test_priority_capacity_is_fixed_before_losses_and_fee_counted_once(self) -> None:
        base = make_path(
            [
                scenario("a", 50, 0.03, 7.1),
                scenario("b", 40, 0.03, 7.1),
                scenario("c", 40, 0.03, 7.1),
            ],
        )
        base["limits"] = {"asset_concentration": {"frog-equity": 0.01}}
        base["action_plans"] = [
            {
                "plan_id": "two_sells",
                "trigger_step_id": "t0",
                "allocation": "priority",
                "orders": [
                    {"order_id": "first", "type": "sell", "holding_id": "frog-equity", "quantity": 5, "fee_bps": 10},
                    {"order_id": "second", "type": "sell", "holding_id": "frog-equity", "quantity": 5, "fee_bps": 10},
                ],
            }
        ]
        base["steps"][1]["fill_windows"] = [
            {"kind": "sell", "key": "frog-equity", "capacity": 6, "price_low": 40, "price_high": 40, "settlement_steps": 0}
        ]
        with tempfile.TemporaryDirectory() as directory:
            path_file = Path(directory) / "paths.json"
            path_file.write_text(json.dumps({"paths": [base]}), encoding="utf-8")
            loaded = load_paths(path_file)
        result = self.engine.run("priority", self.holdings, loaded)
        policy = next(item for item in result.results[0].policies if item.policy == "constrained:two_sells")
        fills = policy.steps[1].action_reconciliation.fills
        self.assertEqual([(fill.order_id, fill.quantity) for fill in fills], [("first", 5), ("second", 1)])
        self.assertAlmostEqual(
            policy.steps[1].action_fees,
            6 * 40 * 7.1 * 10 / 10000,
        )
        self.assertAlmostEqual(
            policy.steps[2].action_fees,
            0.0,
        )

    def test_cancel_keeps_fills_and_state_round_trip_blocks_rewritten_history(self) -> None:
        path = make_path([
            scenario("a", 50, 0.03, 7.1),
            scenario("b", 40, 0.03, 7.1),
            scenario("c", 40, 0.03, 7.1),
        ])
        path["limits"] = {"asset_concentration": {"frog-equity": 0.01}}
        path["action_plans"] = [{
            "plan_id": "cancel_me",
            "trigger_step_id": "t0",
            "orders": [
                {"order_id": "s1", "type": "sell", "holding_id": "frog-equity", "quantity": 10, "fee_bps": 0}
            ],
        }]
        path["steps"][1]["fill_windows"] = [
            {"kind": "sell", "key": "frog-equity", "capacity": 4, "price_low": 40, "price_high": 40, "settlement_steps": 0}
        ]
        path["steps"][2]["cancellations"] = [{"plan_id": "cancel_me"}]
        with tempfile.TemporaryDirectory() as directory:
            path_file = Path(directory) / "paths.json"
            path_file.write_text(json.dumps({"paths": [path]}), encoding="utf-8")
            loaded = load_paths(path_file)
            result = self.engine.run("cancel", self.holdings, loaded)
            store = ResultStore(directory)
            store.save(result)
            restored = load_path_run_result(directory, "cancel")
        policy = next(item for item in restored.results[0].policies if item.policy == "constrained:cancel_me")
        self.assertEqual(policy.steps[2].canceled_action_order_ids, ("cancel_me:s1",))
        self.assertEqual(policy.steps[2].execution_ledger.quantities["frog-equity"], 16)
        self.assertEqual(policy.steps[2].execution_ledger.open_orders, ())
        self.assertTrue(policy.execution_state is not None)
        rewritten = json.loads(json.dumps(path))
        rewritten["steps"][1]["scenario"]["equity_prices"]["FROG"] = 30
        rewritten["version"] = 3
        with tempfile.TemporaryDirectory() as directory:
            path_file = Path(directory) / "paths.json"
            path_file.write_text(json.dumps({"paths": [rewritten]}), encoding="utf-8")
            rewritten_paths = load_paths(path_file)
            with self.assertRaises(ValueError):
                self.engine.resume("resume", self.holdings, rewritten_paths, restored)

    def test_disposal_comparison_names_risk_drivers(self) -> None:
        result = self.engine.run("compare", self.holdings, self.paths)
        comparison = compare_disposal_policies(result)["stagflation_ladder"]["sell_equity"]
        self.assertTrue(comparison["unfilled_orders"])
        self.assertIn("insufficient_fills", comparison["risk_sources"])
        self.assertIn("price_band_slippage", comparison["risk_sources"])
        self.assertIn("fees", comparison["risk_sources"])
        self.assertIn("settlement_lag", comparison["risk_sources"])

    def test_resume_continues_from_saved_partial_fill_state(self) -> None:
        broken = scenario("c", 40, 0.03, 7.3)
        broken["fx_rates"] = {}
        path = make_path([
            scenario("a", 50, 0.03, 7.1),
            scenario("b", 40, 0.03, 7.1),
            broken,
        ])
        path["limits"] = {"asset_concentration": {"frog-equity": 0.01}}
        path["action_plans"] = [{
            "plan_id": "resume_plan",
            "trigger_step_id": "t0",
            "orders": [
                {"order_id": "s1", "type": "sell", "holding_id": "frog-equity", "quantity": 10, "fee_bps": 10}
            ],
        }]
        path["steps"][1]["fill_windows"] = [
            {"kind": "sell", "key": "frog-equity", "capacity": 4, "price_low": 40, "price_high": 40, "settlement_steps": 0}
        ]
        with tempfile.TemporaryDirectory() as directory:
            path_file = Path(directory) / "paths.json"
            path_file.write_text(json.dumps({"paths": [path]}), encoding="utf-8")
            partial_paths = load_paths(path_file)
            partial = self.engine.run("partial-run", self.holdings, partial_paths)
            self.assertFalse(partial.complete)
            repaired = json.loads(json.dumps(path))
            repaired["steps"][2]["scenario"]["fx_rates"] = {"USD": 7.3, "EUR": 7.5}
            path_file.write_text(json.dumps({"paths": [repaired]}), encoding="utf-8")
            resumed_paths = load_paths(path_file)
            resumed = self.engine.resume("resumed-run", self.holdings, resumed_paths, partial)
        policy = next(item for item in resumed.results[0].policies if item.policy == "constrained:resume_plan")
        self.assertTrue(policy.complete)
        self.assertEqual(len(policy.steps), 3)
        self.assertAlmostEqual(policy.steps[0].portfolio_value, policy.steps[0].portfolio_value)
        self.assertEqual(policy.steps[1].action_reconciliation.fills[0].quantity, 4)
        self.assertEqual(policy.steps[1].action_reconciliation.fills[0].fill_price, 40)
        self.assertAlmostEqual(policy.steps[2].execution_ledger.quantities["frog-equity"], 16)

    def test_settlement_events_drive_availability_and_distinguish_branches(self) -> None:
        def settlement_path(branch: str):
            path = make_path([
                scenario("a", 50, 0.03, 7.1),
                scenario("b", 40, 0.03, 7.1),
                scenario("c", 40, 0.03, 7.1),
                scenario("d", 40, 0.03, 7.1),
                scenario("e", 40, 0.03, 7.1),
            ])
            path["path_id"] = f"settlement_{branch}"
            path["limits"] = {"asset_concentration": {"frog-equity": 0.01}}
            path["action_plans"] = [{
                "plan_id": "sell",
                "trigger_step_id": "t0",
                "allocation": "priority",
                "fill_price": "worst",
                "orders": [
                    {"order_id": "s1", "type": "sell", "holding_id": "frog-equity", "quantity": 10, "fee_bps": 0}
                ],
            }]
            path["steps"][1]["fill_windows"] = [
                {"kind": "sell", "key": "frog-equity", "capacity": 10, "price_low": 40, "price_high": 40, "settlement_steps": 1}
            ]
            settlement_id = "sell:s1:t1"
            path["steps"][2]["settlement_events"] = [
                {"event_id": "partial-credit", "type": "settled", "settlement_id": settlement_id, "amount_local": 160}
            ]
            if branch == "wait":
                path["steps"][3]["settlement_events"] = [
                    {"event_id": "reject-remaining", "type": "rejected", "settlement_id": settlement_id, "amount_local": 240}
                ]
            elif branch == "cancel":
                path["steps"][3]["settlement_events"] = [
                    {"event_id": "abandon-remaining", "type": "canceled", "settlement_id": settlement_id, "amount_local": 240}
                ]
            else:
                path["steps"][3]["settlement_events"] = [
                    {"event_id": "reject-remaining", "type": "rejected", "settlement_id": settlement_id, "amount_local": 240},
                    {
                        "event_id": "move-to-new-agent",
                        "type": "resubmitted",
                        "settlement_id": settlement_id,
                        "replacement_settlement_id": "new-agent-settlement",
                        "amount_local": 240,
                        "counterparty": "new-agent",
                        "fee_bps": 20,
                        "settlement_steps": 1,
                    },
                ]
                path["steps"][4]["settlement_events"] = [
                    {
                        "event_id": "new-agent-credit",
                        "type": "settled",
                        "settlement_id": "new-agent-settlement",
                        "amount_local": 240,
                    },
                    {
                        "event_id": "late-old-rejection",
                        "type": "rejected",
                        "settlement_id": settlement_id,
                        "amount_local": 240,
                        "corrects_event_id": "reject-remaining",
                    },
                    {
                        "event_id": "partial-credit",
                        "type": "settled",
                        "settlement_id": settlement_id,
                        "amount_local": 160,
                    },
                ]
            return path

        branches = {}
        all_paths = []
        for branch in ("wait", "cancel", "resettle"):
            raw_path = settlement_path(branch)
            with tempfile.TemporaryDirectory() as directory:
                path_file = Path(directory) / "paths.json"
                path_file.write_text(
                    json.dumps({"paths": [raw_path]}),
                    encoding="utf-8",
                )
                loaded = load_paths(path_file)
                all_paths.extend(loaded)
            run = self.engine.run(f"settlement-{branch}", self.holdings, loaded)
            branches[branch] = next(
                policy for policy in run.results[0].policies
                if policy.policy == "constrained:sell"
            )

        wait_ledger = branches["wait"].steps[-1].execution_ledger
        self.assertAlmostEqual(wait_ledger.available_cash["USD"], 160)
        self.assertNotIn("USD", wait_ledger.pending_cash_local)
        self.assertAlmostEqual(wait_ledger.rejected_cash_local["USD"], 240)
        self.assertAlmostEqual(wait_ledger.disputed_receivables_base, 240 * 7.1)
        self.assertAlmostEqual(wait_ledger.cumulative_realized_settlement_loss_base, 0.0)
        self.assertAlmostEqual(wait_ledger.cumulative_settlement_fees, 0.0)

        cancel_ledger = branches["cancel"].steps[-1].execution_ledger
        self.assertAlmostEqual(cancel_ledger.available_cash["USD"], 160)
        self.assertAlmostEqual(cancel_ledger.disputed_receivables_base, 0.0)
        self.assertAlmostEqual(
            cancel_ledger.cumulative_realized_settlement_loss_base,
            240 * 7.1,
        )

        resettle_ledger = branches["resettle"].steps[-1].execution_ledger
        self.assertAlmostEqual(resettle_ledger.available_cash["USD"], 400 - 4.8 / 10)
        self.assertNotIn("USD", resettle_ledger.rejected_cash_local)
        self.assertAlmostEqual(resettle_ledger.cumulative_realized_settlement_loss_base, 0.0)
        self.assertAlmostEqual(resettle_ledger.cumulative_settlement_fees, 240 * 7.1 * 20 / 10000)
        self.assertAlmostEqual(
            resettle_ledger.cumulative_settled_base,
            400 * 7.1 - 240 * 7.1 * 20 / 10000,
        )
        final_events = {
            item.event_id: item
            for item in branches["resettle"].steps[-1].settlement_event_results
        }
        self.assertEqual(final_events["new-agent-credit"].status, "applied")
        self.assertEqual(final_events["late-old-rejection"].status, "ignored_stale")
        self.assertEqual(final_events["partial-credit"].status, "ignored_duplicate")

        comparison_run = self.engine.run("compare-branches", self.holdings, all_paths)
        comparison = compare_disposal_policies(comparison_run)
        self.assertEqual(
            set(comparison),
            {"settlement_wait", "settlement_cancel", "settlement_resettle"},
        )
        self.assertAlmostEqual(
            comparison["settlement_wait"]["sell"]["disputed_receivables_base"],
            240 * 7.1,
        )
        self.assertAlmostEqual(
            comparison["settlement_cancel"]["sell"]["realized_settlement_loss_base"],
            240 * 7.1,
        )
        self.assertAlmostEqual(
            comparison["settlement_resettle"]["sell"]["settlement_fees"],
            240 * 7.1 * 20 / 10000,
        )

    def test_resume_settlement_events_reproduces_uninterrupted_cash_path(self) -> None:
        def path_payload(broken: bool):
            path = make_path([
                scenario("a", 50, 0.03, 7.1),
                scenario("b", 40, 0.03, 7.1),
                scenario("c", 40, 0.03, 7.1),
                scenario("d", 40, 0.03, 7.1),
                scenario("e", 40, 0.03, 7.1),
            ], path_id="resume_settlement")
            if broken:
                path["steps"][3]["scenario"]["fx_rates"] = {}
            path["limits"] = {"asset_concentration": {"frog-equity": 0.01}}
            path["action_plans"] = [{
                "plan_id": "sell",
                "trigger_step_id": "t0",
                "fill_price": "worst",
                "orders": [
                    {"order_id": "s1", "type": "sell", "holding_id": "frog-equity", "quantity": 10, "fee_bps": 0}
                ],
            }]
            path["steps"][1]["fill_windows"] = [
                {"kind": "sell", "key": "frog-equity", "capacity": 10, "price_low": 40, "price_high": 40, "settlement_steps": 1}
            ]
            settlement_id = "sell:s1:t1"
            path["steps"][2]["settlement_events"] = [
                {"event_id": "partial", "type": "settled", "settlement_id": settlement_id, "amount_local": 160},
                {"event_id": "rejected", "type": "rejected", "settlement_id": settlement_id, "amount_local": 240},
            ]
            path["steps"][3]["settlement_events"] = [
                {
                    "event_id": "resubmit",
                    "type": "resubmitted",
                    "settlement_id": settlement_id,
                    "replacement_settlement_id": "replacement",
                    "amount_local": 240,
                    "fee_bps": 20,
                    "settlement_steps": 1,
                }
            ]
            path["steps"][4]["settlement_events"] = [
                {"event_id": "replacement-paid", "type": "settled", "settlement_id": "replacement", "amount_local": 240},
                {"event_id": "partial", "type": "settled", "settlement_id": settlement_id, "amount_local": 160},
            ]
            return {"paths": [path]}

        with tempfile.TemporaryDirectory() as directory:
            path_file = Path(directory) / "paths.json"
            path_file.write_text(json.dumps(path_payload(True)), encoding="utf-8")
            partial_run = self.engine.run(
                "partial-settlement",
                self.holdings,
                load_paths(path_file),
            )
            store = ResultStore(directory)
            store.save(partial_run)
            loaded_partial = load_path_run_result(directory, "partial-settlement")
            path_file.write_text(json.dumps(path_payload(False)), encoding="utf-8")
            resumed_run = self.engine.resume(
                "resumed-settlement",
                self.holdings,
                load_paths(path_file),
                loaded_partial,
            )

        with tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".json", delete=False) as handle:
            handle.write(json.dumps(path_payload(False)))
            uninterrupted_path = Path(handle.name)
        uninterrupted_run = self.engine.run(
            "uninterrupted-settlement",
            self.holdings,
            load_paths(uninterrupted_path),
        )
        resumed_policy = next(
            item for item in resumed_run.results[0].policies
            if item.policy == "constrained:sell"
        )
        uninterrupted_policy = next(
            item for item in uninterrupted_run.results[0].policies
            if item.policy == "constrained:sell"
        )
        for resumed_step, uninterrupted_step in zip(
            resumed_policy.steps, uninterrupted_policy.steps
        ):
            self.assertEqual(
                resumed_step.execution_ledger.available_cash,
                uninterrupted_step.execution_ledger.available_cash,
            )
            self.assertEqual(
                resumed_step.execution_ledger.pending_cash_local,
                uninterrupted_step.execution_ledger.pending_cash_local,
            )
            self.assertEqual(
                resumed_step.execution_ledger.rejected_cash_local,
                uninterrupted_step.execution_ledger.rejected_cash_local,
            )
            self.assertAlmostEqual(
                resumed_step.portfolio_value,
                uninterrupted_step.portfolio_value,
            )


if __name__ == "__main__":
    unittest.main()
