import os
import tempfile
import unittest

from risk_platform.receipt_allocations import (
    AllocationStudy,
    AllocationPlanResult,
    EARLIEST_DUE,
    EXPLICIT,
    PRO_RATA,
    Receipt,
    ReceiptWithdrawal,
    SettlementObligation,
    load_allocation_study,
    save_allocation_study,
)


def build_study() -> AllocationStudy:
    """Two settlements, three receipts on different dates, one withdrawal.

    Settlement S1 is 120.00 USD due 2026-03-10; S2 is 100.00 EUR due
    2026-03-20. Receipts arrive in USD on 03-01, EUR on 03-08 and USD on
    03-15, with a fee-deduct-first receipt, an amortized fee receipt and a
    partial withdrawal from the first USD receipt on 03-18.
    """
    study = AllocationStudy(fx_round_digits=2)
    study = study.add_obligation(
        SettlementObligation("S1", "USD", 120.00, "2026-03-10", "settlement-a")
    )
    study = study.add_obligation(
        SettlementObligation("S2", "EUR", 100.00, "2026-03-20", "settlement-b")
    )
    study = study.add_receipt(
        Receipt(
            receipt_id="R1",
            date="2026-03-01",
            currency="USD",
            amount_local=100.00,
            fee_local=2.00,
            fee_mode="deduct_first",
            fx_rates={"USD": 1.0, "EUR": 0.9},
            source="bank-a",
        )
    )
    study = study.add_receipt(
        Receipt(
            receipt_id="R2",
            date="2026-03-08",
            currency="EUR",
            amount_local=60.00,
            fee_local=3.00,
            fee_mode="amortize",
            fx_rates={"EUR": 1.0},
            source="bank-b",
        )
    )
    study = study.add_receipt(
        Receipt(
            receipt_id="R3",
            date="2026-03-15",
            currency="USD",
            amount_local=50.00,
            fee_local=0.00,
            fee_mode="deduct_first",
            fx_rates={"USD": 1.0, "EUR": 0.9},
            source="bank-c",
        )
    )
    study = study.add_withdrawal(
        ReceiptWithdrawal(
            withdrawal_id="W1",
            receipt_id="R1",
            date="2026-03-18",
            amount_local=40.00,
            note="partial claw-back",
        )
    )
    study = study.record_duplicate_notice(
        "R2", "2026-03-19", 60.00, "late duplicate advice"
    )
    return study


class ReceiptAllocationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.study = build_study()

    def test_explicit_designation_and_shortfall(self) -> None:
        study = self.study.save_plan(
            "P-explicit",
            EXPLICIT,
            designations={
                "R1": {"S1": 98.00},
                "R2": {"S2": 57.00},
                "R3": {"S1": 22.00, "S2": 20.00},
            },
        )
        plan = study.plan("P-explicit")
        study.assert_books_close("P-explicit")
        allocated_s1 = sum(
            item.active_obligation_ccy
            for item in plan.allocations
            if item.settlement_id == "S1"
        )
        allocated_s2 = sum(
            item.active_obligation_ccy
            for item in plan.allocations
            if item.settlement_id == "S2"
        )
        # R1 contributes 98 USD then 40 USD is withdrawn back from R1's own
        # allocation (source specific); R3's genuine 22 USD is untouched.
        self.assertAlmostEqual(allocated_s1, 80.00, places=6)
        # R2 57 EUR net (amortized fee) + R3 20 USD -> 18 EUR; floor rounding.
        self.assertLessEqual(allocated_s2, 75.00 + 1e-9)
        self.assertGreaterEqual(allocated_s2, 75.00 - 1e-9)
        self.assertIn("S1", plan.shortfall_by_settlement)
        self.assertIn("S2", plan.shortfall_by_settlement)
        accounts = {item.receipt_id: item for item in plan.receipt_accounts}
        self.assertEqual(accounts["R3"].unallocated_residual_local, 8.00)
        self.assertEqual(
            accounts["R1"].withdrawn_from_allocations_local, 40.00
        )
        # the amortized fee is spread across R2's allocations in full
        r2_fee_shares = sum(
            item.fee_share_receipt_ccy
            for item in plan.allocations
            if item.receipt_id == "R2"
        )
        self.assertAlmostEqual(r2_fee_shares, 3.00, places=6)
        # no expected/scheduled amount ever tops the allocation up
        self.assertEqual(
            plan.remaining_by_settlement["S1"] + allocated_s1, 120.00
        )

    def test_explicit_cannot_exceed_actual_receipt(self) -> None:
        with self.assertRaises(ValueError):
            self.study.save_plan(
                "P-bad",
                EXPLICIT,
                designations={"R1": {"S1": 99.00, "S2": 10.00}},
            )
        with self.assertRaises(ValueError):
            self.study.save_plan("P-missing-fx", EXPLICIT, designations={
                "R2": {"S1": 10.00},
            })

    def test_pro_rata_uses_unsettled_balances_and_fx_floor(self) -> None:
        study = self.study.save_plan("P-pro", PRO_RATA)
        plan = study.plan("P-pro")
        study.assert_books_close("P-pro")
        # R1 net 98 USD is split across S1 and the EUR-equivalent cap of S2
        r1 = [item for item in plan.allocations if item.receipt_id == "R1"]
        self.assertEqual({item.settlement_id for item in r1}, {"S1", "S2"})
        # FX conversion floors, so S2 can never be overpaid by rounding
        for item in plan.allocations:
            self.assertGreater(item.applied_obligation_ccy, 0.0)
        self.assertGreater(
            plan.remaining_by_settlement["S2"], 20.0
        )

    def test_earliest_due_priority_order(self) -> None:
        study = self.study.save_plan("P-early", EARLIEST_DUE)
        plan = study.plan("P-early")
        study.assert_books_close("P-early")
        # R1 is USD; S1 (USD) is due earlier and has capacity, so all 98 net
        # USD goes to S1 before S2 is touched; R2 EUR can only reach S2.
        r1 = [item for item in plan.allocations if item.receipt_id == "R1"]
        self.assertEqual([item.settlement_id for item in r1], ["S1"])
        r2 = [item for item in plan.allocations if item.receipt_id == "R2"]
        self.assertTrue(all(item.settlement_id == "S2" for item in r2))
        # R3 arrives after S1 is partly repaid; S1 still due-first but the
        # partial withdrawal of R1 reinstates S1's outstanding balance
        self.assertIn("S1", plan.shortfall_by_settlement)

    def test_partial_withdrawal_only_touches_its_source(self) -> None:
        study = self.study.save_plan("P-pro", PRO_RATA)
        plan = study.plan("P-pro")
        before_r3 = {
            item.settlement_id: item.active_obligation_ccy
            for item in plan.allocations
            if item.receipt_id == "R3"
        }
        reversed_r1 = sum(
            item.reversed_receipt_ccy
            for item in plan.allocations
            if item.receipt_id == "R1"
        )
        reversed_r3 = sum(
            item.reversed_receipt_ccy
            for item in plan.allocations
            if item.receipt_id == "R3"
        )
        self.assertEqual(reversed_r1, 40.00)
        self.assertEqual(reversed_r3, 0.0)
        self.assertTrue(all(value > 0 for value in before_r3.values()))
        kinds = {
            entry[2] for entry in plan.notice_log if entry[1] == "R1"
        }
        self.assertIn("withdrawal", kinds)

    def test_only_adopted_plan_feeds_current_book_and_is_idempotent(self) -> None:
        study = self.study
        study = study.save_plan("P-pro", PRO_RATA)
        study = study.save_plan("P-early", EARLIEST_DUE)
        before = study.current_reconciliation()
        self.assertEqual(before["status"], "no_adopted_plan")
        study = study.adopt("P-pro")
        first = study.current_reconciliation()
        study = study.adopt("P-pro")
        second = study.current_reconciliation()
        self.assertEqual(first, second)
        # adopting the other plan changes the current book
        study_other = study.adopt("P-early")
        self.assertNotEqual(
            first["remaining_obligations_by_ccy"],
            study_other.current_reconciliation()["remaining_obligations_by_ccy"],
        )
        # the duplicate advice is recorded but never adds cash
        self.assertEqual(first["duplicate_notice_count"], 1)
        total_eur_cash = first["cash_remaining_by_ccy"].get("EUR", 0.0)
        self.assertLessEqual(total_eur_cash, 60.0)

    def test_duplicate_receipt_id_is_rejected_and_recompute_is_stable(self) -> None:
        dup = Receipt(
            receipt_id="R1",
            date="2026-03-21",
            currency="USD",
            amount_local=500.0,
        )
        with self.assertRaises(ValueError):
            self.study.add_receipt(dup)
        study = self.study.save_plan("P-pro", PRO_RATA)
        snapshot = study.plan("P-pro").to_dict()
        study = study.replace_plan("P-pro", PRO_RATA)
        self.assertEqual(snapshot, study.plan("P-pro").to_dict())

    def test_save_parallel_plans_and_reopen_reconciles_cash_fees_obligations(self):
        study = self.study
        study = study.save_plan("P-explicit", EXPLICIT, designations={
            "R1": {"S1": 98.00},
            "R2": {"S2": 57.00},
            "R3": {"S1": 40.00},
        })
        study = study.save_plan("P-pro", PRO_RATA)
        study = study.save_plan("P-early", EARLIEST_DUE)
        study = study.adopt("P-explicit")
        for plan_id in ("P-explicit", "P-pro", "P-early"):
            study.assert_books_close(plan_id)
        comparison = study.compare_plans()
        self.assertEqual(set(comparison), {"P-explicit", "P-pro", "P-early"})
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "study.json")
            save_allocation_study(study, path)
            reopened = load_allocation_study(path)
        self.assertIsInstance(reopened.plan("P-pro"), AllocationPlanResult)
        self.assertEqual(reopened.adopted_plan_id, "P-explicit")
        current = reopened.current_reconciliation()
        # R1 net 98 allocated then 40 withdrawn (cash leaves); when R3
        # arrived S1 only had 22 outstanding, so of the designated 40 only
        # 22 applied and 18 stayed residual, plus the 10 never designated.
        self.assertAlmostEqual(
            current["cash_remaining_by_ccy"].get("USD", 0.0), 28.0, places=6
        )
        self.assertAlmostEqual(
            current["cash_remaining_by_ccy"].get("EUR", 0.0), 0.0, places=6
        )
        # fees are real losses tracked by receipt currency
        self.assertAlmostEqual(current["fees_by_ccy"]["USD"], 2.0, places=6)
        self.assertAlmostEqual(current["fees_by_ccy"]["EUR"], 3.0, places=6)
        # S1: R1 98 active-58 after the 40 claw-back, R3 22 -> active 80
        self.assertAlmostEqual(
            current["allocated_by_settlement_ccy"]["S1"], 80.0, places=6
        )
        self.assertAlmostEqual(
            current["remaining_obligations_by_ccy"]["USD"], 40.0, places=6
        )
        self.assertAlmostEqual(
            current["allocated_by_settlement_ccy"]["S2"], 57.0, places=6
        )
        self.assertAlmostEqual(
            current["remaining_obligations_by_ccy"]["EUR"], 43.0, places=6
        )
        reopened.assert_books_close("P-pro")
        reopened.assert_books_close("P-early")


if __name__ == "__main__":
    unittest.main()
