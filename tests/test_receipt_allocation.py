from __future__ import annotations

import json
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from risk_platform.receipt_allocation import (
    DUE_FIRST,
    EXPLICIT,
    FEE_SHARED,
    PRO_RATA,
    AllocationResearch,
    Claim,
    ExplicitLine,
    ReceiptNotice,
    WithdrawalNotice,
    load_research,
    reconciliation,
    save_study,
)


def D(value: str) -> Decimal:
    return Decimal(value)


def two_settlement_research() -> AllocationResearch:
    builder = AllocationResearch(study_id="resettle-research")
    builder.add_claim(Claim("s1", "c1", "2026-01-10", "USD", D("600.00")))
    builder.add_claim(Claim("s2", "c2", "2026-01-05", "USD", D("400.00")))
    builder.set_fx("2026-01-01", "USD", "EUR", "0.90")
    builder.set_currency_places("USD", 2)
    builder.set_currency_places("EUR", 2)
    builder.receive(
        ReceiptNotice("r1", "2026-01-01", "USD", D("500.00"), D("10.00"))
    )
    builder.receive(
        ReceiptNotice(
            "r2",
            "2026-01-02",
            "USD",
            D("300.00"),
            D("6.00"),
            FEE_SHARED,
        )
    )
    builder.receive(ReceiptNotice("r3", "2026-01-03", "USD", D("100.00")))
    builder.withdraw(
        WithdrawalNotice("w1", "r2", "2026-01-04", D("120.00"))
    )
    builder.set_explicit("r1", [ExplicitLine("s1", "c1", D("490.00"))])
    return builder


class ReceiptAllocationTests(unittest.TestCase):
    def test_explicit_scheme_only_uses_named_assignments(self) -> None:
        study = two_settlement_research().build((EXPLICIT,))
        scheme = study.schemes[EXPLICIT]
        by_claim = {(item.settlement_id, item.claim_id): item for item in scheme.claims}
        self.assertEqual(by_claim[("s1", "c1")].settled_local, D("490.00"))
        self.assertEqual(by_claim[("s2", "c2")].settled_local, D("0.00"))
        self.assertEqual(scheme.unallocated_by_currency["USD"], D("274.0000000000"))

    def test_due_first_funds_earliest_claim_before_later_ones(self) -> None:
        study = two_settlement_research().build((DUE_FIRST,))
        scheme = study.schemes[DUE_FIRST]
        funded = [
            (line.receipt_id, line.claim_id, line.amount_receipt_local)
            for line in scheme.allocations
        ]
        self.assertEqual(funded[0][:2], ("r1", "c2"))
        by_claim = {(item.settlement_id, item.claim_id): item for item in scheme.claims}
        self.assertEqual(by_claim[("s2", "c2")].settled_local, D("400.00"))

    def test_pro_rata_splits_receipts_by_outstanding_balance(self) -> None:
        study = two_settlement_research().build((PRO_RATA,))
        scheme = study.schemes[PRO_RATA]
        first = scheme.allocations[0]
        second = scheme.allocations[1]
        self.assertEqual(first.receipt_id, "r1")
        self.assertEqual(first.amount_receipt_local + second.amount_receipt_local, D("490.00"))
        ratio = first.amount_receipt_local / D("490.00")
        self.assertAlmostEqual(float(ratio), 0.4, places=3)

    def test_fx_truncation_tail_remains_unallocated(self) -> None:
        builder = AllocationResearch(study_id="fx")
        builder.add_claim(Claim("s1", "c1", "2026-02-01", "EUR", D("100.00")))
        builder.set_fx("2026-01-01", "USD", "EUR", "0.33333333")
        builder.set_currency_places("EUR", 2)
        builder.receive(ReceiptNotice("r1", "2026-01-01", "USD", D("1.01")))
        study = builder.build((DUE_FIRST,))
        scheme = study.schemes[DUE_FIRST]
        line = scheme.allocations[0]
        self.assertEqual(line.amount_claim_local, D("0.33"))
        self.assertGreater(line.fx_rounding_tail_receipt_local, D("0"))
        self.assertGreater(scheme.unallocated_by_currency["USD"], D("0"))

    def test_partial_withdrawal_only_reverses_backed_receipt(self) -> None:
        study = two_settlement_research().build((PRO_RATA,))
        scheme = study.schemes[PRO_RATA]
        reversed_receipts = {line.receipt_id for line in scheme.reversals}
        self.assertEqual(reversed_receipts, {"r2"})
        reversed_total = sum(
            (line.amount_receipt_local for line in scheme.reversals), D("0")
        )
        self.assertEqual(reversed_total, D("120.00"))
        r2 = next(item for item in scheme.receipts if item.receipt_id == "r2")
        self.assertEqual(r2.withdrawn_receipt_local, D("120.00"))
        r1 = next(item for item in scheme.receipts if item.receipt_id == "r1")
        self.assertEqual(r1.withdrawn_receipt_local, D("0"))

    def test_cash_fees_and_remaining_obligation_close_exactly(self) -> None:
        study = two_settlement_research().build()
        scheme = study.schemes[DUE_FIRST]
        allocated = sum(
            (line.amount_receipt_local for line in scheme.allocations), D("0")
        )
        unallocated = sum(scheme.unallocated_by_currency.values(), D("0"))
        fees = scheme.fees_by_currency["USD"]
        self.assertEqual(allocated + fees + unallocated, D("900.00"))
        obligations = sum(
            scheme.remaining_obligation_by_currency.values(), D("0")
        )
        self.assertEqual(obligations, D("236.00"))

    def test_expected_amounts_never_fill_shortfall(self) -> None:
        builder = AllocationResearch(study_id="short")
        builder.add_claim(Claim("s1", "c1", "2026-01-10", "USD", D("1000.00")))
        builder.set_fx("2026-01-01", "USD", "USD", "1")
        builder.receive(ReceiptNotice("r1", "2026-01-01", "USD", D("100.00")))
        scheme = builder.build((DUE_FIRST,)).schemes[DUE_FIRST]
        self.assertEqual(scheme.remaining_obligation_by_currency["USD"], D("900.00"))

    def test_duplicate_and_late_notices_are_idempotent(self) -> None:
        builder = two_settlement_research()
        builder.receive(ReceiptNotice("r1", "2026-01-05", "USD", D("500.00")))
        builder.withdraw(
            WithdrawalNotice("w1", "r2", "2026-01-09", D("120.00"))
        )
        study = builder.build((PRO_RATA,))
        scheme = study.schemes[PRO_RATA]
        r1 = next(item for item in scheme.receipts if item.receipt_id == "r1")
        self.assertEqual(r1.duplicate_notices, 1)
        self.assertEqual(scheme.fees_by_currency["USD"], D("16.00"))
        self.assertEqual(
            sum((line.amount_receipt_local for line in scheme.reversals), D("0")),
            D("120.00"),
        )

    def test_only_adopted_scheme_drives_reconciliation(self) -> None:
        builder = two_settlement_research().adopt(PRO_RATA)
        study = builder.build()
        adopted = reconciliation(study)
        self.assertEqual(adopted.adopted_scheme_id, PRO_RATA)
        pro_rata = study.schemes[PRO_RATA]
        self.assertEqual(adopted.cash_by_currency, pro_rata.cash_by_currency)
        builder.adopt(None)
        no_scheme = reconciliation(builder.build())
        self.assertIsNone(no_scheme.adopted_scheme_id)
        self.assertEqual(no_scheme.cash_by_currency, {})

    def test_parallel_schemes_survive_reopen(self) -> None:
        builder = two_settlement_research().adopt(PRO_RATA)
        study = builder.build()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "study.json"
            save_study(study, path)
            payload = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(set(payload["schemes"]), {EXPLICIT, PRO_RATA, DUE_FIRST})
            reopened_builder = load_research(path)
            reopened = reopened_builder.build()
        for method in (EXPLICIT, PRO_RATA, DUE_FIRST):
            before = study.schemes[method].to_dict()
            after = reopened.schemes[method].to_dict()
            self.assertEqual(before["claims"], after["claims"])
            self.assertEqual(before["receipts"], after["receipts"])
        self.assertEqual(reconciliation(reopened).adopted_scheme_id, PRO_RATA)

    def test_other_receipts_unaffected_by_partial_withdrawal(self) -> None:
        builder = two_settlement_research()
        study = builder.build((PRO_RATA, DUE_FIRST))
        for method in (PRO_RATA, DUE_FIRST):
            scheme = study.schemes[method]
            affected = {line.receipt_id for line in scheme.reversals}
            self.assertEqual(affected, {"r2"})
            for state in scheme.receipts:
                if state.receipt_id == "r2":
                    self.assertEqual(state.withdrawn_receipt_local, D("120.00"))
                else:
                    self.assertEqual(state.withdrawn_receipt_local, D("0"))
                    self.assertEqual(state.reversal_receipt_local, D("0"))


if __name__ == "__main__":
    unittest.main()
