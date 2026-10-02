from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from risk_platform.disposal import DisposalPlan
from risk_platform.holdings import Holding
from risk_platform.limit_engine import LimitPathEngine, NO_ACTION_PLAN_ID
from risk_platform.limits import RiskLimit
from risk_platform.paths import StressPath
from risk_platform.results import (
    ResultStore,
    compare_limit_results,
    load_limit_run_result,
)


def _holding() -> list[Holding]:
    return [
        Holding.from_dict(
            {
                "id": "usd-cash",
                "name": "USD cash",
                "asset_type": "cash",
                "currency": "USD",
                "quantity": 50,
            }
        ),
        Holding.from_dict(
            {
                "id": "eq-a",
                "name": "Equity A",
                "asset_type": "equity",
                "currency": "USD",
                "quantity": 10,
                "market_data_key": "A",
            }
        ),
    ]


def _scenario(scenario_id: str, version: int, price: float, eur_fx: float = 2.0) -> dict:
    return {
        "scenario_id": scenario_id,
        "version": version,
        "equity_prices": {"A": price},
        "bond_yields": {},
        "fx_rates": {"USD": 1.0, "EUR": eur_fx},
    }


def _path(version: int = 1, final_price: float = 20.0, drop_t2_data: bool = False, eur_fx_final: float = 2.0) -> StressPath:
    t2 = _scenario("t2", version, final_price, eur_fx=eur_fx_final)
    if drop_t2_data:
        t2["equity_prices"] = {}
    return StressPath.from_dict(
        {
            "path_id": "swing",
            "version": version,
            "steps": [
                {"step_id": "t0", "scenario": _scenario("t0", 1, 10.0)},
                {"step_id": "t1", "scenario": _scenario("t1", 1, 5.0)},
                {"step_id": "t2", "scenario": t2},
            ],
        }
    )


def _limits() -> list[RiskLimit]:
    return [
        RiskLimit.from_dict({"limit_id": "loss-cap", "metric": "loss", "threshold": 40}),
        RiskLimit.from_dict(
            {"limit_id": "conc-cap", "metric": "concentration", "threshold": 0.4}
        ),
        RiskLimit.from_dict(
            {
                "limit_id": "usd-cap",
                "metric": "currency_exposure",
                "currency": "USD",
                "threshold": 150,
            }
        ),
    ]


def _sell_plan(fee_rate: float = 0.01, version: int = 1) -> DisposalPlan:
    return DisposalPlan.from_dict(
        {
            "plan_id": "sell-all",
            "version": version,
            "trigger_limit_id": "loss-cap",
            "actions": [
                {"action": "sell", "holding_id": "eq-a", "fraction": 1.0, "fee_rate": fee_rate}
            ],
        }
    )


def _convert_plan() -> DisposalPlan:
    return DisposalPlan.from_dict(
        {
            "plan_id": "convert",
            "version": 1,
            "trigger_limit_id": "loss-cap",
            "actions": [
                {
                    "action": "fx_convert",
                    "from_currency": "USD",
                    "to_currency": "EUR",
                    "amount": 50,
                    "fee_rate": 0.0,
                }
            ],
        }
    )


def _run(paths, plans=(), limits=None):
    return LimitPathEngine.default().run(
        run_id="test",
        holdings=_holding(),
        paths=paths,
        limits=limits if limits is not None else _limits(),
        plans=list(plans),
    )


class LimitEvaluationTests(unittest.TestCase):
    def test_first_breach_location_and_source(self):
        result = _run([_path()]).results[0]
        self.assertEqual(result.status, "complete")
        first = {record.limit_id: record for record in result.first_breaches}
        self.assertEqual(first["loss-cap"].step_id, "t1")
        self.assertAlmostEqual(first["loss-cap"].observed, 50.0)
        self.assertEqual(first["loss-cap"].sources[0]["holding_id"], "eq-a")
        self.assertEqual(first["conc-cap"].step_id, "t0")
        self.assertEqual(first["conc-cap"].sources[0]["holding_id"], "eq-a")

    def test_recovered_metric_does_not_mask_other_breaches(self):
        result = _run([_path()]).results[0]
        baseline = result.plans[0]
        self.assertEqual(baseline.plan_id, NO_ACTION_PLAN_ID)
        final_step = baseline.steps[-1]
        breached = {breach.limit_id for breach in final_step.breaches}
        # loss recovered (price rebounded to 20) but concentration and USD
        # exposure remain breached at the final step.
        self.assertNotIn("loss-cap", breached)
        self.assertIn("conc-cap", breached)
        self.assertIn("usd-cap", breached)

    def test_currency_exposure_source(self):
        result = _run([_path()]).results[0]
        first = {record.limit_id: record for record in result.first_breaches}
        self.assertEqual(first["usd-cap"].step_id, "t0")
        self.assertEqual(first["usd-cap"].sources[0]["currency"], "USD")
        self.assertAlmostEqual(first["usd-cap"].observed, 150.0)


class DisposalPlanTests(unittest.TestCase):
    def test_plan_triggers_once_and_charges_fees(self):
        result = _run([_path()], plans=[_sell_plan()]).results[0]
        baseline, plan = result.plans
        self.assertFalse(baseline.applied)
        self.assertTrue(plan.applied)
        self.assertEqual(plan.trigger.step_id, "t1")
        self.assertEqual(plan.trigger.limit_id, "loss-cap")
        self.assertAlmostEqual(plan.total_fees, 0.5)
        # selling at t1 avoids the rebound; no_action rides the equity back up
        self.assertAlmostEqual(baseline.final_value, 250.0)
        self.assertAlmostEqual(plan.final_value, 99.5)

    def test_attribution_identity_and_no_double_counting(self):
        result = _run([_path()], plans=[_sell_plan(), _convert_plan()]).results[0]
        for plan in result.plans:
            component_sum = (
                plan.total_market_effect
                + plan.total_cash_flow_effect
                + plan.total_trade_effect
                + plan.total_disposal_effect
                + plan.total_fee_effect
            )
            self.assertAlmostEqual(component_sum, plan.total_change)
            self.assertAlmostEqual(
                plan.initial_value + plan.total_change, plan.final_value
            )
            for step in plan.steps:
                step_sum = (
                    step.market_effect
                    + step.cash_flow_effect
                    + step.trade_effect
                    + step.disposal_effect
                    + step.fee_effect
                )
                self.assertAlmostEqual(step_sum, step.total_change)
        plan = result.plans[1]
        trigger_step = next(s for s in plan.steps if s.step_id == "t1")
        # sale is value-neutral at the trigger market; only the fee moves value
        self.assertAlmostEqual(trigger_step.disposal_effect, 0.0)
        self.assertAlmostEqual(trigger_step.fee_effect, -0.5)

    def test_fx_convert_plan_exposes_later_fx_risk(self):
        result = _run([_path(final_price=5.0, eur_fx_final=1.0)], plans=[_convert_plan()]).results[0]
        convert = result.plans[1]
        self.assertTrue(convert.applied)
        # 50 USD became 25 EUR at EUR=2.0; EUR falls to 1.0 at t2 -> -25
        self.assertAlmostEqual(convert.final_value, 75.0)

    def test_decision_uses_only_trigger_step_data(self):
        v1 = _run([_path(version=1, final_price=20.0)], plans=[_sell_plan()]).results[0]
        v2 = _run([_path(version=2, final_price=100.0)], plans=[_sell_plan()]).results[0]
        t1, t2 = v1.plans[1].trigger, v2.plans[1].trigger
        self.assertEqual(t1.step_id, t2.step_id)
        self.assertEqual(t1.observed, t2.observed)
        self.assertEqual(t1.scenario_fingerprint, t2.scenario_fingerprint)
        self.assertEqual(v1.plans[1].total_fees, v2.plans[1].total_fees)


class MissingDataTests(unittest.TestCase):
    def test_partial_path_keeps_confirmed_breach(self):
        result = _run([_path(drop_t2_data=True)], plans=[_sell_plan()]).results[0]
        self.assertEqual(result.status, "partial")
        self.assertIsNotNone(result.missing_data)
        first = {record.limit_id: record for record in result.first_breaches}
        self.assertIn("loss-cap", first)
        self.assertEqual(first["loss-cap"].step_id, "t1")
        self.assertEqual(
            set(result.unconfirmed_limits), {"loss-cap", "conc-cap", "usd-cap"}
        )
        for plan in result.plans:
            self.assertFalse(plan.complete)


class PersistenceTests(unittest.TestCase):
    def test_roundtrip_and_versioned_comparison(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ResultStore(tmp)
            run_v1 = _run([_path(version=1)], plans=[_sell_plan(version=1)])
            run_v1 = run_v1.__class__(run_id="run-v1", results=run_v1.results)
            store.save(run_v1)

            revised_plan = _sell_plan(fee_rate=0.02, version=2)
            run_v2 = _run([_path(version=2)], plans=[revised_plan])
            run_v2 = run_v2.__class__(run_id="run-v2", results=run_v2.results)
            store.save(run_v2)

            with self.assertRaises(FileExistsError):
                store.save(run_v1)

            loaded_v1 = load_limit_run_result(tmp, "run-v1")
            loaded_v2 = load_limit_run_result(tmp, "run-v2")
            self.assertEqual(
                loaded_v1.results[0].plans[1].trigger,
                run_v1.results[0].plans[1].trigger,
            )
            table = compare_limit_results([loaded_v1, loaded_v2])
            self.assertIn("swing", table)
            self.assertIn("v1/no_action@v0", table["swing"])
            self.assertIn("v1/sell-all@v1", table["swing"])
            self.assertIn("v2/sell-all@v2", table["swing"])
            # original trigger basis stays queryable after the revision
            self.assertEqual(
                table["swing"]["v1/sell-all@v1"]["trigger_step"], "t1"
            )
            self.assertNotEqual(
                table["swing"]["v1/sell-all@v1"]["total_fees"],
                table["swing"]["v2/sell-all@v2"]["total_fees"],
            )


if __name__ == "__main__":
    unittest.main()
