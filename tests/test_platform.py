from __future__ import annotations

import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from risk_platform.app import run_suite
from risk_platform.engine import scenario_hash, stress_pnl, value_portfolio
from risk_platform.errors import MissingMarketDataError
from risk_platform.storage import JsonMarketDataStore, JsonResultStore


ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"


class RiskPlatformTests(unittest.TestCase):
    def setUp(self) -> None:
        self.market = JsonMarketDataStore(DATA)
        self.holdings = self.market.load_holdings()

    def test_direct_holding_level_baseline_calculation(self) -> None:
        result = value_portfolio(
            self.holdings,
            self.market.load_scenario("baseline"),
            "baseline__v1__value",
        )
        by_id = result.holding_map()

        self.assertEqual(by_id["h001"].market_value_base, Decimal("1000.00"))
        self.assertEqual(by_id["h002"].market_value_base, Decimal("880.00"))
        self.assertEqual(by_id["h003"].market_value_base, Decimal("750.0000"))
        self.assertEqual(result.total_value_base, Decimal("2630.0000"))

    def test_direct_stress_attribution_sums_to_total(self) -> None:
        baseline = value_portfolio(
            self.holdings,
            self.market.load_scenario("baseline"),
            "baseline__v1__value",
        )
        stressed = value_portfolio(
            self.holdings,
            self.market.load_scenario("equity_crash"),
            "equity_crash__v1__value",
        )
        pnl = stress_pnl(baseline, stressed, "equity_crash__v1__pnl")

        expected = {
            "h001": Decimal("700.00") - Decimal("1000.00"),
            "h002": Decimal("798.00") - Decimal("880.00"),
            "h003": Decimal("684.0000") - Decimal("750.0000"),
        }
        actual = {item.holding_id: item.pnl_base for item in pnl.contributions}
        self.assertEqual(actual, expected)
        self.assertEqual(
            pnl.total_pnl_base,
            Decimal("700.00")
            + Decimal("798.00")
            + Decimal("684.0000")
            - Decimal("2630.0000"),
        )

    def test_missing_fx_fails_without_fallback_or_publish(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            store = JsonResultStore(Path(temp))
            with self.assertRaises(MissingMarketDataError) as ctx:
                value_portfolio(
                    self.holdings,
                    self.market.load_scenario("incomplete_missing_fx"),
                    "incomplete__v1__value",
                )
            self.assertIn("JPY/USD", str(ctx.exception))
            self.assertEqual(store.list_valuations(), [])

    def test_suite_failure_publishes_no_new_incomplete_total(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            with self.assertRaises(MissingMarketDataError):
                run_suite(DATA, Path(temp), ["incomplete_missing_fx"])
            self.assertEqual(JsonResultStore(Path(temp)).list_valuations(), [])

    def test_updated_scenario_keeps_old_result_traceable(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp)
            run_suite(DATA, output, ["equity_crash", "equity_crash_v2"])
            store = JsonResultStore(output)
            v1 = store.get_valuation("equity_crash__v1__value")
            v2 = store.get_valuation("equity_crash_v2__v2__value")

            self.assertEqual(v1.scenario_version, 1)
            self.assertEqual(v2.scenario_version, 2)
            self.assertNotEqual(v1.scenario_hash, v2.scenario_hash)
            self.assertEqual(
                v1.scenario_hash,
                scenario_hash(self.market.load_scenario("equity_crash")),
            )
            self.assertEqual(
                v2.scenario_hash,
                scenario_hash(self.market.load_scenario("equity_crash_v2")),
            )
            self.assertEqual(v1.total_value_base, Decimal("2182.0000"))
            self.assertEqual(v2.total_value_base, Decimal("1984.8000"))


if __name__ == "__main__":
    unittest.main()
