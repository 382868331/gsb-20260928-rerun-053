"""Multi-receipt allocation research across resettled settlements.

Several settlements can share one real incoming payment, and one settlement's
obligations can be covered by payments arriving on different dates. A
researcher keeps several parallel allocation schemes (explicit, pro-rata or
earliest-due-first) and adopts at most one into the current reconciliation.

Only real arrivals fund allocations. Expected/scheduled amounts never fill a
shortfall. Processing fees are either deducted from a receipt before any
allocation or shared across its allocations. Currency conversion truncates to
the claim currency's minor unit; the truncation tail stays with the receipt as
an unallocated remainder. Partial withdrawals reverse only allocations backed
by that receipt; other real receipts are untouched. Repeated or late
duplicate notices are idempotent and cannot raise any balance.
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass, field, replace
from decimal import ROUND_DOWN, Decimal
from pathlib import Path
from typing import Any

EXPLICIT = "explicit"
PRO_RATA = "pro_rata"
DUE_FIRST = "due_first"
METHODS = (EXPLICIT, PRO_RATA, DUE_FIRST)

FEE_DEDUCT_FIRST = "deduct_first"
FEE_SHARED = "shared"
FEE_MODES = (FEE_DEDUCT_FIRST, FEE_SHARED)

ZERO = Decimal("0")


def _decimal(value: Any) -> Decimal:
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


def _quantize(amount: Decimal, places: int) -> Decimal:
    quantum = Decimal(1).scaleb(-places)
    return amount.quantize(quantum, rounding=ROUND_DOWN)


@dataclass(frozen=True, slots=True)
class Claim:
    """One outstanding obligation of one settlement, in its own currency."""

    settlement_id: str
    claim_id: str
    due_date: str
    currency: str
    amount_local: Decimal

    @property
    def key(self) -> tuple[str, str]:
        return (self.settlement_id, self.claim_id)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Claim":
        amount = _decimal(data["amount_local"])
        if amount <= 0:
            raise ValueError("Claim amount_local must be positive")
        return cls(
            settlement_id=str(data["settlement_id"]),
            claim_id=str(data["claim_id"]),
            due_date=str(data["due_date"]),
            currency=str(data["currency"]),
            amount_local=amount,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "settlement_id": self.settlement_id,
            "claim_id": self.claim_id,
            "due_date": self.due_date,
            "currency": self.currency,
            "amount_local": str(self.amount_local),
        }


@dataclass(frozen=True, slots=True)
class ReceiptNotice:
    """An actual arrival of funds. The fee is real money withheld."""

    receipt_id: str
    date: str
    currency: str
    amount_local: Decimal
    fee_local: Decimal = ZERO
    fee_mode: str = FEE_DEDUCT_FIRST

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ReceiptNotice":
        fee_mode = str(data.get("fee_mode", FEE_DEDUCT_FIRST))
        if fee_mode not in FEE_MODES:
            raise ValueError("fee_mode must be deduct_first or shared")
        amount = _decimal(data["amount_local"])
        fee = _decimal(data.get("fee_local", 0))
        if amount <= 0:
            raise ValueError("Receipt amount_local must be positive")
        if fee < 0 or fee > amount:
            raise ValueError("Receipt fee must be between 0 and the amount")
        return cls(
            receipt_id=str(data["receipt_id"]),
            date=str(data["date"]),
            currency=str(data["currency"]),
            amount_local=amount,
            fee_local=fee,
            fee_mode=fee_mode,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "receipt_id": self.receipt_id,
            "date": self.date,
            "currency": self.currency,
            "amount_local": str(self.amount_local),
            "fee_local": str(self.fee_local),
            "fee_mode": self.fee_mode,
        }


@dataclass(frozen=True, slots=True)
class WithdrawalNotice:
    """A partial clawback of funds that had arrived on one real receipt."""

    withdrawal_id: str
    receipt_id: str
    date: str
    amount_local: Decimal

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "WithdrawalNotice":
        amount = _decimal(data["amount_local"])
        if amount <= 0:
            raise ValueError("Withdrawal amount_local must be positive")
        return cls(
            withdrawal_id=str(data["withdrawal_id"]),
            receipt_id=str(data["receipt_id"]),
            date=str(data["date"]),
            amount_local=amount,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "withdrawal_id": self.withdrawal_id,
            "receipt_id": self.receipt_id,
            "date": self.date,
            "amount_local": str(self.amount_local),
        }


@dataclass(frozen=True, slots=True)
class ExplicitLine:
    """A researcher-specified assignment, stated in receipt currency."""

    settlement_id: str
    claim_id: str
    amount_receipt_local: Decimal

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ExplicitLine":
        amount = _decimal(data["amount_receipt_local"])
        if amount <= 0:
            raise ValueError("Explicit allocation must be positive")
        return cls(
            settlement_id=str(data["settlement_id"]),
            claim_id=str(data["claim_id"]),
            amount_receipt_local=amount,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "settlement_id": self.settlement_id,
            "claim_id": self.claim_id,
            "amount_receipt_local": str(self.amount_receipt_local),
        }


@dataclass(frozen=True, slots=True)
class AllocationLine:
    receipt_id: str
    withdrawal_id: str | None
    date: str
    settlement_id: str
    claim_id: str
    receipt_currency: str
    amount_receipt_local: Decimal
    claim_currency: str
    fx_rate: Decimal
    amount_claim_local: Decimal
    fee_share_receipt_local: Decimal
    fx_rounding_tail_receipt_local: Decimal

    def to_dict(self) -> dict[str, Any]:
        return {
            "receipt_id": self.receipt_id,
            "withdrawal_id": self.withdrawal_id,
            "date": self.date,
            "settlement_id": self.settlement_id,
            "claim_id": self.claim_id,
            "receipt_currency": self.receipt_currency,
            "amount_receipt_local": str(self.amount_receipt_local),
            "claim_currency": self.claim_currency,
            "fx_rate": str(self.fx_rate),
            "amount_claim_local": str(self.amount_claim_local),
            "fee_share_receipt_local": str(self.fee_share_receipt_local),
            "fx_rounding_tail_receipt_local": str(
                self.fx_rounding_tail_receipt_local
            ),
        }


@dataclass(frozen=True, slots=True)
class ReceiptState:
    receipt_id: str
    date: str
    currency: str
    gross_local: Decimal
    fee_local: Decimal
    fee_mode: str
    allocated_receipt_local: Decimal
    withdrawn_receipt_local: Decimal
    unallocated_receipt_local: Decimal
    reversal_receipt_local: Decimal = ZERO
    duplicate_notices: int = 0
    overdrawn_notice_local: Decimal = ZERO

    def to_dict(self) -> dict[str, Any]:
        return {
            "receipt_id": self.receipt_id,
            "date": self.date,
            "currency": self.currency,
            "gross_local": str(self.gross_local),
            "fee_local": str(self.fee_local),
            "fee_mode": self.fee_mode,
            "allocated_receipt_local": str(self.allocated_receipt_local),
            "withdrawn_receipt_local": str(self.withdrawn_receipt_local),
            "unallocated_receipt_local": str(self.unallocated_receipt_local),
            "reversal_receipt_local": str(self.reversal_receipt_local),
            "duplicate_notices": self.duplicate_notices,
            "overdrawn_notice_local": str(self.overdrawn_notice_local),
        }


@dataclass(frozen=True, slots=True)
class ClaimState:
    settlement_id: str
    claim_id: str
    due_date: str
    currency: str
    original_local: Decimal
    funded_local: Decimal
    reversed_local: Decimal

    @property
    def settled_local(self) -> Decimal:
        return self.funded_local - self.reversed_local

    @property
    def remaining_local(self) -> Decimal:
        return self.original_local - self.settled_local

    def to_dict(self) -> dict[str, Any]:
        return {
            "settlement_id": self.settlement_id,
            "claim_id": self.claim_id,
            "due_date": self.due_date,
            "currency": self.currency,
            "original_local": str(self.original_local),
            "funded_local": str(self.funded_local),
            "reversed_local": str(self.reversed_local),
            "settled_local": str(self.settled_local),
            "remaining_local": str(self.remaining_local),
        }


@dataclass(frozen=True, slots=True)
class SchemeResult:
    scheme_id: str
    method: str
    receipts: tuple[ReceiptState, ...]
    claims: tuple[ClaimState, ...]
    allocations: tuple[AllocationLine, ...]
    reversals: tuple[AllocationLine, ...]
    unallocated_by_currency: dict[str, Decimal]
    cash_by_currency: dict[str, Decimal]
    fees_by_currency: dict[str, Decimal]
    remaining_obligation_by_currency: dict[str, Decimal]
    explicit_keys: tuple[tuple[str, str], ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "scheme_id": self.scheme_id,
            "method": self.method,
            "receipts": [item.to_dict() for item in self.receipts],
            "claims": [item.to_dict() for item in self.claims],
            "allocations": [item.to_dict() for item in self.allocations],
            "reversals": [item.to_dict() for item in self.reversals],
            "unallocated_by_currency": _string_map(self.unallocated_by_currency),
            "cash_by_currency": _string_map(self.cash_by_currency),
            "fees_by_currency": _string_map(self.fees_by_currency),
            "remaining_obligation_by_currency": _string_map(
                self.remaining_obligation_by_currency
            ),
            "explicit_keys": [list(item) for item in self.explicit_keys],
        }


def _string_map(values: dict[str, Decimal]) -> dict[str, str]:
    return {key: str(value) for key, value in values.items()}


@dataclass(frozen=True, slots=True)
class AllocationStudy:
    """Several parallel schemes replayed over the same real-money events."""

    study_id: str
    claims: tuple[Claim, ...]
    receipts: tuple[ReceiptNotice, ...]
    withdrawals: tuple[WithdrawalNotice, ...]
    fx_rates: dict[str, dict[tuple[str, str], Decimal]]
    currency_places: dict[str, int]
    explicit: dict[str, tuple[ExplicitLine, ...]]
    schemes: dict[str, SchemeResult]
    adopted_scheme_id: str | None = None
    notice_sequence: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "study_id": self.study_id,
            "claims": [item.to_dict() for item in self.claims],
            "receipts": [item.to_dict() for item in self.receipts],
            "withdrawals": [item.to_dict() for item in self.withdrawals],
            "fx_rates": [
                {
                    "date": date,
                    "rates": [
                        {"from_ccy": pair[0], "to_ccy": pair[1], "rate": str(rate)}
                        for pair, rate in sorted(pairs.items())
                    ],
                }
                for date, pairs in sorted(self.fx_rates.items())
            ],
            "currency_places": self.currency_places,
            "explicit": [
                {
                    "receipt_id": receipt_id,
                    "lines": [line.to_dict() for line in lines],
                }
                for receipt_id, lines in sorted(self.explicit.items())
            ],
            "schemes": {
                scheme_id: result.to_dict()
                for scheme_id, result in sorted(self.schemes.items())
            },
            "adopted_scheme_id": self.adopted_scheme_id,
            "notice_sequence": list(self.notice_sequence),
        }

    def adopted_scheme(self) -> SchemeResult | None:
        if self.adopted_scheme_id is None:
            return None
        return self.schemes[self.adopted_scheme_id]


@dataclass(frozen=True, slots=True)
class Reconciliation:
    """Current reconciliation ledger, driven only by the adopted scheme."""

    study_id: str
    adopted_scheme_id: str | None
    cash_by_currency: dict[str, Decimal]
    fees_by_currency: dict[str, Decimal]
    unallocated_by_currency: dict[str, Decimal]
    remaining_obligation_by_currency: dict[str, Decimal]

    def to_dict(self) -> dict[str, Any]:
        return {
            "study_id": self.study_id,
            "adopted_scheme_id": self.adopted_scheme_id,
            "cash_by_currency": _string_map(self.cash_by_currency),
            "fees_by_currency": _string_map(self.fees_by_currency),
            "unallocated_by_currency": _string_map(self.unallocated_by_currency),
            "remaining_obligation_by_currency": _string_map(
                self.remaining_obligation_by_currency
            ),
        }


@dataclass(slots=True)
class AllocationResearch:
    """Builds parallel schemes from immutable notices and FX observations."""

    study_id: str
    claims: list[Claim] = field(default_factory=list)
    receipts: list[ReceiptNotice] = field(default_factory=list)
    withdrawals: list[WithdrawalNotice] = field(default_factory=list)
    fx_rates: dict[str, dict[tuple[str, str], Decimal]] = field(default_factory=dict)
    currency_places: dict[str, int] = field(default_factory=dict)
    explicit: dict[str, list[ExplicitLine]] = field(default_factory=dict)
    adopted_scheme_id: str | None = None
    _notice_order: list[str] = field(default_factory=list)
    _duplicate_receipts: dict[str, int] = field(default_factory=dict)

    def add_claim(self, claim: Claim) -> "AllocationResearch":
        if any(
            existing.key == claim.key
            for existing in self.claims
        ):
            raise ValueError(f"Duplicate claim {claim.settlement_id}/{claim.claim_id}")
        self.claims.append(claim)
        return self

    def set_fx(
        self,
        date: str,
        from_ccy: str,
        to_ccy: str,
        rate: Any,
    ) -> "AllocationResearch":
        rate_decimal = _decimal(rate)
        if rate_decimal <= 0:
            raise ValueError("FX rate must be positive")
        self.fx_rates.setdefault(date, {})[(from_ccy, to_ccy)] = rate_decimal
        self.fx_rates[date][(to_ccy, from_ccy)] = Decimal(1) / rate_decimal
        return self

    def set_currency_places(self, currency: str, places: int) -> "AllocationResearch":
        if places < 0:
            raise ValueError("Currency places must not be negative")
        self.currency_places[currency] = places
        return self

    def set_explicit(
        self, receipt_id: str, lines: list[ExplicitLine]
    ) -> "AllocationResearch":
        self.explicit[receipt_id] = list(lines)
        return self

    def receive(self, notice: ReceiptNotice) -> "AllocationResearch":
        if any(item.receipt_id == notice.receipt_id for item in self.receipts):
            self._duplicate_receipts[notice.receipt_id] = (
                self._duplicate_receipts.get(notice.receipt_id, 0) + 1
            )
            return self
        self.receipts.append(notice)
        self._notice_order.append(notice.receipt_id)
        return self

    def withdraw(self, notice: WithdrawalNotice) -> "AllocationResearch":
        if any(
            item.withdrawal_id == notice.withdrawal_id for item in self.withdrawals
        ):
            return self
        if not any(item.receipt_id == notice.receipt_id for item in self.receipts):
            raise ValueError(
                f"Withdrawal {notice.withdrawal_id} references unknown receipt "
                f"{notice.receipt_id}"
            )
        self.withdrawals.append(notice)
        self._notice_order.append(notice.withdrawal_id)
        return self

    def adopt(self, scheme_id: str | None) -> "AllocationResearch":
        if scheme_id is not None and scheme_id not in METHODS:
            raise ValueError("Only explicit/pro_rata/due_first schemes can be adopted")
        self.adopted_scheme_id = scheme_id
        return self

    def build(self, methods: tuple[str, ...] = METHODS) -> AllocationStudy:
        for method in methods:
            if method not in METHODS:
                raise ValueError(f"Unknown allocation method {method}")
        claim_index = {claim.key: claim for claim in self.claims}
        if len(claim_index) != len(self.claims):
            raise ValueError("Duplicate claims")
        receipt_index = {
            receipt.receipt_id: receipt for receipt in self.receipts
        }
        if len(receipt_index) != len(self.receipts):
            raise ValueError("Duplicate receipts")
        for receipt_id, lines in self.explicit.items():
            if receipt_id not in receipt_index:
                raise ValueError(f"Explicit lines reference unknown receipt {receipt_id}")
            for line in lines:
                if (line.settlement_id, line.claim_id) not in claim_index:
                    raise ValueError(
                        "Explicit line references unknown claim "
                        f"{line.settlement_id}/{line.claim_id}"
                    )
        schemes = {
            method: self._replay(method, claim_index, receipt_index)
            for method in methods
        }
        return AllocationStudy(
            study_id=self.study_id,
            claims=tuple(self.claims),
            receipts=tuple(self.receipts),
            withdrawals=tuple(self.withdrawals),
            fx_rates={
                date: dict(pairs.items()) for date, pairs in self.fx_rates.items()
            },
            currency_places=dict(self.currency_places),
            explicit={
                receipt_id: tuple(lines)
                for receipt_id, lines in self.explicit.items()
            },
            schemes=schemes,
            adopted_scheme_id=self.adopted_scheme_id,
            notice_sequence=tuple(self._notice_order),
        )

    def _rate(self, date: str, from_ccy: str, to_ccy: str) -> Decimal | None:
        if from_ccy == to_ccy:
            return Decimal(1)
        known = sorted(
            (day for day in self.fx_rates if day <= date), reverse=True
        )
        for day in known:
            pair = self.fx_rates[day].get((from_ccy, to_ccy))
            if pair is not None:
                return pair
        return None

    def _places(self, currency: str) -> int:
        return self.currency_places.get(currency, 2)

    def _replay(
        self,
        method: str,
        claim_index: dict[tuple[str, str], Claim],
        receipt_index: dict[str, ReceiptNotice],
    ) -> SchemeResult:
        remaining = {key: claim.amount_local for key, claim in claim_index.items()}
        funded = {key: ZERO for key in claim_index}
        reversed_amounts = {key: ZERO for key in claim_index}
        pools = {
            receipt.receipt_id: receipt.amount_local - receipt.fee_local
            for receipt in self.receipts
        }
        allocated_total = {receipt.receipt_id: ZERO for receipt in self.receipts}
        withdrawn_total = {receipt.receipt_id: ZERO for receipt in self.receipts}
        reversal_total = {receipt.receipt_id: ZERO for receipt in self.receipts}
        overdrawn = {receipt.receipt_id: ZERO for receipt in self.receipts}
        allocations: list[AllocationLine] = []
        reversals: list[AllocationLine] = []
        # Per-receipt outstanding funded lines, used for partial withdrawals.
        funded_lines: dict[str, list[AllocationLine]] = {
            receipt.receipt_id: [] for receipt in self.receipts
        }

        order = {
            identifier: position
            for position, identifier in enumerate(self._notice_order)
        }
        receipt_events = [
            (receipt.date, order[receipt.receipt_id], "receipt", receipt)
            for receipt in self.receipts
        ]
        withdrawal_events = [
            (
                withdrawal.date,
                order[withdrawal.withdrawal_id],
                "withdrawal",
                withdrawal,
            )
            for withdrawal in self.withdrawals
        ]
        events = sorted(receipt_events + withdrawal_events, key=lambda item: item[:2])

        for _date, _sequence, kind, notice in events:
            if kind == "receipt":
                lines = self._plan_allocation(
                    method=method,
                    receipt=notice,
                    claim_index=claim_index,
                    remaining=remaining,
                    pool=pools[notice.receipt_id],
                )
                for line in lines:
                    key = (line.settlement_id, line.claim_id)
                    remaining[key] -= line.amount_claim_local
                    funded[key] += line.amount_claim_local
                    pools[notice.receipt_id] -= line.amount_receipt_local
                    allocated_total[notice.receipt_id] += line.amount_receipt_local
                    allocations.append(line)
                    funded_lines[notice.receipt_id].append(line)
            else:
                self._apply_withdrawal(
                    withdrawal=notice,
                    receipt=receipt_index[notice.receipt_id],
                    pools=pools,
                    funded_lines=funded_lines,
                    remaining=remaining,
                    reversed_amounts=reversed_amounts,
                    withdrawn_total=withdrawn_total,
                    reversal_total=reversal_total,
                    overdrawn=overdrawn,
                    reversals=reversals,
                )

        return self._scheme_result(
            method=method,
            claim_index=claim_index,
            funded=funded,
            reversed_amounts=reversed_amounts,
            pools=pools,
            allocated_total=allocated_total,
            withdrawn_total=withdrawn_total,
            reversal_total=reversal_total,
            overdrawn=overdrawn,
            allocations=allocations,
            reversals=reversals,
            remaining=remaining,
            duplicates=self._duplicate_receipts,
        )

    def _round_receipt(self, amount: Decimal) -> Decimal:
        return _quantize(amount, 10)

    def _convert_principal(
        self,
        date: str,
        receipt_ccy: str,
        claim: Claim,
        principal_receipt: Decimal,
    ) -> tuple[Decimal, Decimal, Decimal]:
        rate = self._rate(date, receipt_ccy, claim.currency)
        if rate is None:
            raise ValueError(
                f"No FX rate {receipt_ccy}->{claim.currency} on {date}"
            )
        places = self._places(claim.currency)
        converted = _quantize(principal_receipt * rate, places)
        if receipt_ccy == claim.currency:
            return principal_receipt, converted, ZERO
        # A claim-currency unit costs 1/rate receipt units. Allocate only the
        # receipt amount that delivers whole claim units; the tail remains on
        # the receipt as its unallocated, per-line rounding remainder.
        used_receipt = _quantize(converted / rate, 10)
        if used_receipt > principal_receipt:
            used_receipt = principal_receipt
        return used_receipt, converted, principal_receipt - used_receipt

    def _make_line(
        self,
        receipt: ReceiptNotice,
        claim: Claim,
        principal_receipt: Decimal,
        fee_share: Decimal,
        withdrawal_id: str | None,
    ) -> AllocationLine:
        used_receipt, converted, tail = self._convert_principal(
            receipt.date, receipt.currency, claim, principal_receipt
        )
        rate = self._rate(receipt.date, receipt.currency, claim.currency) or Decimal(1)
        return AllocationLine(
            receipt_id=receipt.receipt_id,
            withdrawal_id=withdrawal_id,
            date=receipt.date,
            settlement_id=claim.settlement_id,
            claim_id=claim.claim_id,
            receipt_currency=receipt.currency,
            amount_receipt_local=used_receipt,
            claim_currency=claim.currency,
            fx_rate=rate,
            amount_claim_local=converted,
            fee_share_receipt_local=fee_share,
            fx_rounding_tail_receipt_local=tail,
        )

    def _targets_for(
        self,
        method: str,
        receipt: ReceiptNotice,
        claim_index: dict[tuple[str, str], Claim],
        remaining: dict[tuple[str, str], Decimal],
    ) -> list[tuple[Claim, Decimal]]:
        if method == EXPLICIT:
            return [
                (
                    claim_index[(line.settlement_id, line.claim_id)],
                    line.amount_receipt_local,
                )
                for line in self.explicit.get(receipt.receipt_id, ())
            ]
        eligible: list[tuple[Claim, Decimal]] = []
        for key, claim in claim_index.items():
            if remaining[key] <= Decimal("0.0000000001"):
                continue
            unit = self._rate(receipt.date, receipt.currency, claim.currency)
            if unit is None:
                continue
            eligible.append((claim, remaining[key] / unit))
        if method == DUE_FIRST:
            eligible.sort(
                key=lambda item: (
                    item[0].due_date,
                    item[0].settlement_id,
                    item[0].claim_id,
                )
            )
            return [(claim, pool) for claim, pool in eligible]
        eligible.sort(
            key=lambda item: (
                item[0].due_date,
                item[0].settlement_id,
                item[0].claim_id,
            )
        )
        return eligible

    def _plan_allocation(
        self,
        method: str,
        receipt: ReceiptNotice,
        claim_index: dict[tuple[str, str], Claim],
        remaining: dict[tuple[str, str], Decimal],
        pool: Decimal,
    ) -> list[AllocationLine]:
        if pool <= ZERO:
            return []
        targets = self._targets_for(method, receipt, claim_index, remaining)
        if not targets:
            return []
        lines: list[AllocationLine] = []
        available = pool
        shared_fee_left = (
            receipt.fee_local if receipt.fee_mode == FEE_SHARED else ZERO
        )
        original_weights = {claim.key: weight for claim, weight in targets}
        total_weight = sum(original_weights.values(), ZERO)
        for position, (claim, requested) in enumerate(targets):
            if available <= ZERO:
                break
            key = claim.key
            unit = self._rate(receipt.date, receipt.currency, claim.currency)
            if unit is None:
                continue
            # Cap in receipt currency so claim-currency rounding cannot
            # allocate more than the outstanding claim balance.
            balance_cap = self._round_receipt(remaining[key] / unit)
            if method == PRO_RATA:
                active_keys = [
                    active_key
                    for active_key, active_weight in original_weights.items()
                    if remaining[active_key] > Decimal("0.0000000001")
                ]
                active_weight = sum(
                    (original_weights[active_key] for active_key in active_keys),
                    ZERO,
                )
                is_last = key == active_keys[-1]
                gross_request = (
                    available
                    if is_last or active_weight <= ZERO
                    else self._round_receipt(
                        available * original_weights[key] / active_weight
                    )
                )
            else:
                gross_request = requested
            gross = self._round_receipt(min(gross_request, available, balance_cap))
            if gross <= ZERO:
                continue
            if receipt.fee_mode == FEE_SHARED:
                distributable = receipt.amount_local - receipt.fee_local
                if position == len(targets) - 1:
                    fee_share = shared_fee_left
                else:
                    fee_share = _quantize(
                        receipt.fee_local * gross / distributable, 10
                    )
                fee_share = min(fee_share, shared_fee_left, gross)
            else:
                fee_share = ZERO
            principal = self._round_receipt(gross)
            if principal <= ZERO:
                continue
            line = self._make_line(receipt, claim, principal, fee_share, None)
            # Shared fees were already withheld from the distributable pool;
            # the per-line fee share is an informational split only.
            available -= line.amount_receipt_local
            shared_fee_left -= fee_share
            lines.append(line)
        return lines

    def _apply_withdrawal(
        self,
        withdrawal: WithdrawalNotice,
        receipt: ReceiptNotice,
        pools: dict[str, Decimal],
        funded_lines: dict[str, list[AllocationLine]],
        remaining: dict[tuple[str, str], Decimal],
        reversed_amounts: dict[tuple[str, str], Decimal],
        withdrawn_total: dict[str, Decimal],
        reversal_total: dict[str, Decimal],
        overdrawn: dict[str, Decimal],
        reversals: list[AllocationLine],
    ) -> None:
        amount = withdrawal.amount_local
        from_unallocated = min(pools[receipt.receipt_id], amount)
        pools[receipt.receipt_id] -= from_unallocated
        need = amount - from_unallocated
        outstanding_indices = [
            index
            for index, line in enumerate(funded_lines[receipt.receipt_id])
            if line.amount_receipt_local > ZERO
        ]
        total_outstanding = sum(
            (
                funded_lines[receipt.receipt_id][index].amount_receipt_local
                for index in outstanding_indices
            ),
            ZERO,
        )
        claw = min(need, total_outstanding)
        claw_left = claw
        for position, line_index in enumerate(outstanding_indices):
            line = funded_lines[receipt.receipt_id][line_index]
            if claw_left <= ZERO:
                break
            share = (
                claw_left
                if position == len(outstanding_indices) - 1
                else self._round_receipt(
                    claw * line.amount_receipt_local / total_outstanding
                )
            )
            share = min(share, claw_left, line.amount_receipt_local)
            if share <= ZERO:
                continue
            claim = Claim(
                settlement_id=line.settlement_id,
                claim_id=line.claim_id,
                due_date=line.date,
                currency=line.claim_currency,
                amount_local=line.amount_claim_local,
            )
            used_receipt, claim_amount, _tail = self._convert_principal(
                withdrawal.date, receipt.currency, claim, share
            )
            rate = self._rate(
                withdrawal.date, receipt.currency, line.claim_currency
            ) or line.fx_rate
            reversal = AllocationLine(
                receipt_id=receipt.receipt_id,
                withdrawal_id=withdrawal.withdrawal_id,
                date=withdrawal.date,
                settlement_id=line.settlement_id,
                claim_id=line.claim_id,
                receipt_currency=receipt.currency,
                amount_receipt_local=used_receipt,
                claim_currency=line.claim_currency,
                fx_rate=rate,
                amount_claim_local=claim_amount,
                fee_share_receipt_local=ZERO,
                fx_rounding_tail_receipt_local=ZERO,
            )
            key = (line.settlement_id, line.claim_id)
            remaining[key] += claim_amount
            reversed_amounts[key] += claim_amount
            reversal_total[receipt.receipt_id] += used_receipt
            claw_left -= used_receipt
            updated = replace(
                line,
                amount_receipt_local=line.amount_receipt_local - share,
                amount_claim_local=line.amount_claim_local - claim_amount,
            )
            funded_lines[receipt.receipt_id][line_index] = updated
            reversals.append(reversal)
        withdrawn_total[receipt.receipt_id] += min(
            amount, from_unallocated + total_outstanding
        )
        settled = from_unallocated + claw
        if amount > settled:
            overdrawn[receipt.receipt_id] += amount - settled

    def _scheme_result(
        self,
        method: str,
        claim_index: dict[tuple[str, str], Claim],
        funded: dict[tuple[str, str], Decimal],
        reversed_amounts: dict[tuple[str, str], Decimal],
        pools: dict[str, Decimal],
        allocated_total: dict[str, Decimal],
        withdrawn_total: dict[str, Decimal],
        reversal_total: dict[str, Decimal],
        overdrawn: dict[str, Decimal],
        allocations: list[AllocationLine],
        reversals: list[AllocationLine],
        remaining: dict[tuple[str, str], Decimal],
        duplicates: dict[str, int],
    ) -> SchemeResult:
        unallocated: dict[str, Decimal] = {}
        cash: dict[str, Decimal] = {}
        fees: dict[str, Decimal] = {}
        receipt_states = []
        for receipt in self.receipts:
            unallocated[receipt.currency] = (
                unallocated.get(receipt.currency, ZERO)
                + pools[receipt.receipt_id]
            )
            fees[receipt.currency] = (
                fees.get(receipt.currency, ZERO) + receipt.fee_local
            )
            net = (
                receipt.amount_local
                - receipt.fee_local
                - withdrawn_total[receipt.receipt_id]
            )
            cash[receipt.currency] = (
                cash.get(receipt.currency, ZERO) + net
            )
            receipt_states.append(ReceiptState(
                receipt_id=receipt.receipt_id,
                date=receipt.date,
                currency=receipt.currency,
                gross_local=receipt.amount_local,
                fee_local=receipt.fee_local,
                fee_mode=receipt.fee_mode,
                allocated_receipt_local=allocated_total[receipt.receipt_id],
                withdrawn_receipt_local=withdrawn_total[receipt.receipt_id],
                unallocated_receipt_local=pools[receipt.receipt_id],
                reversal_receipt_local=reversal_total[receipt.receipt_id],
                overdrawn_notice_local=overdrawn[receipt.receipt_id],
                duplicate_notices=duplicates.get(receipt.receipt_id, 0),
            ))
        obligations: dict[str, Decimal] = {}
        claim_states = []
        for key, claim in claim_index.items():
            obligations[claim.currency] = (
                obligations.get(claim.currency, ZERO) + remaining[key]
            )
            claim_states.append(ClaimState(
                settlement_id=claim.settlement_id,
                claim_id=claim.claim_id,
                due_date=claim.due_date,
                currency=claim.currency,
                original_local=claim.amount_local,
                funded_local=funded[key],
                reversed_local=reversed_amounts[key],
            ))
        explicit_keys = tuple(
            (line.settlement_id, line.claim_id)
            for lines in self.explicit.values()
            for line in lines
        )
        return SchemeResult(
            scheme_id=method,
            method=method,
            receipts=tuple(receipt_states),
            claims=tuple(claim_states),
            allocations=tuple(allocations),
            reversals=tuple(reversals),
            unallocated_by_currency=unallocated,
            cash_by_currency=cash,
            fees_by_currency=fees,
            remaining_obligation_by_currency=obligations,
            explicit_keys=explicit_keys,
        )


def reconciliation(study: AllocationStudy) -> Reconciliation:
    scheme = study.adopted_scheme()
    if scheme is None:
        obligations: dict[str, Decimal] = {}
        for claim in study.claims:
            obligations[claim.currency] = (
                obligations.get(claim.currency, ZERO) + claim.amount_local
            )
        empty: dict[str, Decimal] = {}
        return Reconciliation(
            study_id=study.study_id,
            adopted_scheme_id=None,
            cash_by_currency=empty,
            fees_by_currency=empty,
            unallocated_by_currency=empty,
            remaining_obligation_by_currency=obligations,
        )
    return Reconciliation(
        study_id=study.study_id,
        adopted_scheme_id=scheme.scheme_id,
        cash_by_currency=dict(scheme.cash_by_currency),
        fees_by_currency=dict(scheme.fees_by_currency),
        unallocated_by_currency=dict(scheme.unallocated_by_currency),
        remaining_obligation_by_currency=dict(
            scheme.remaining_obligation_by_currency
        ),
    )


def save_study(study: AllocationStudy, path: str | Path) -> Path:
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(study.to_dict(), ensure_ascii=False, indent=2, sort_keys=True)
    fd, temporary_name = tempfile.mkstemp(
        prefix=f".{study.study_id}.", suffix=".json.tmp", dir=destination.parent
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, destination)
    except BaseException:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass
        raise
    return destination


def load_research(path: str | Path) -> AllocationResearch:
    with Path(path).open("r", encoding="utf-8") as handle:
        payload = json.load(handle)
    builder = AllocationResearch(study_id=str(payload["study_id"]))
    for item in payload["claims"]:
        builder.add_claim(Claim.from_dict(item))
    for block in payload["fx_rates"]:
        for rate_item in block["rates"]:
            builder.set_fx(
                block["date"],
                rate_item["from_ccy"],
                rate_item["to_ccy"],
                rate_item["rate"],
            )
    for currency, places in payload.get("currency_places", {}).items():
        builder.set_currency_places(currency, int(places))
    for item in payload["receipts"]:
        builder.receive(ReceiptNotice.from_dict(item))
    for item in payload["withdrawals"]:
        builder.withdraw(WithdrawalNotice.from_dict(item))
    for block in payload.get("explicit", ()):
        builder.set_explicit(
            block["receipt_id"],
            [ExplicitLine.from_dict(line) for line in block["lines"]],
        )
    if payload.get("adopted_scheme_id"):
        builder.adopt(payload["adopted_scheme_id"])
    if payload.get("notice_sequence"):
        builder._notice_order = list(payload["notice_sequence"])
    return builder
