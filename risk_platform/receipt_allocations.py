"""Research tool for allocating multiple actual receipts across settlements.

Several settlements (obligations) can be paid together by one actual receipt,
and one settlement can be covered by several receipts arriving on different
dates. This module lets a researcher keep several parallel allocation schemes
side by side -- explicit designation, pro-rata and earliest-due-first -- and
reopen the study later. Only the scheme explicitly adopted feeds the current
reconciliation; saving schemes or adopting a scheme repeatedly never creates
money, and expected/scheduled amounts are never used to top up shortfalls.

All amounts are tracked per event in the receipt's own currency so that fees,
FX rounding and unallocated residuals stay attributable to the actual funds
that caused them. A withdrawal only reverses allocations (and residual cash)
supported by its own source receipt; funds genuinely received from other
receipts are untouched.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from decimal import ROUND_FLOOR, ROUND_HALF_UP, Decimal
from typing import Any

EXPLICIT = "explicit"
PRO_RATA = "pro_rata"
EARLIEST_DUE = "earliest_due"
METHODS = (EXPLICIT, PRO_RATA, EARLIEST_DUE)

_DECI = Decimal


def _q(value: float, digits: int, rounding: str = ROUND_HALF_UP) -> float:
    return float(
        _DECI(str(value)).quantize(_DECI(10) ** -digits, rounding=rounding)
    )


@dataclass(frozen=True, slots=True)
class SettlementObligation:
    """One settlement's still-unpaid balance in its own currency."""

    settlement_id: str
    currency: str
    amount_local: float
    due_date: str
    label: str | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "SettlementObligation":
        missing = [
            name
            for name in ("settlement_id", "currency", "amount_local", "due_date")
            if name not in data
        ]
        if missing:
            raise ValueError(f"Obligation missing fields: {', '.join(missing)}")
        amount = float(data["amount_local"])
        if amount <= 0:
            raise ValueError("Obligation amount_local must be positive")
        return cls(
            settlement_id=str(data["settlement_id"]),
            currency=str(data["currency"]),
            amount_local=amount,
            due_date=str(data["due_date"]),
            label=(str(data["label"]) if data.get("label") is not None else None),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "settlement_id": self.settlement_id,
            "currency": self.currency,
            "amount_local": self.amount_local,
            "due_date": self.due_date,
            "label": self.label,
        }


@dataclass(frozen=True, slots=True)
class Receipt:
    """An actual, confirmed arrival of funds on a given date.

    ``fee_local`` is charged on this receipt. ``fee_mode`` is ``deduct_first``
    (the fee is taken off the top before any allocation) or ``amortize`` (the
    same cash deduction, but the fee is attributed back to individual
    allocations pro-rata for reporting). ``fx_rates`` maps an obligation
    currency to units of that currency paid per one unit of the receipt
    currency; obligations in other currencies cannot receive this receipt.
    """

    receipt_id: str
    date: str
    currency: str
    amount_local: float
    fee_local: float = 0.0
    fee_mode: str = "deduct_first"
    fx_rates: dict[str, float] = field(default_factory=dict)
    source: str | None = None
    note: str | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Receipt":
        missing = [
            name
            for name in ("receipt_id", "date", "currency", "amount_local")
            if name not in data
        ]
        if missing:
            raise ValueError(f"Receipt missing fields: {', '.join(missing)}")
        amount = float(data["amount_local"])
        fee = float(data.get("fee_local", 0.0))
        if amount <= 0:
            raise ValueError("Receipt amount_local must be positive")
        if fee < 0 or fee >= amount:
            raise ValueError(
                "Receipt fee_local must be non-negative and below amount"
            )
        fee_mode = str(data.get("fee_mode", "deduct_first"))
        if fee_mode not in {"deduct_first", "amortize"}:
            raise ValueError("Receipt fee_mode must be deduct_first or amortize")
        fx = {
            str(currency): float(rate)
            for currency, rate in (data.get("fx_rates") or {}).items()
        }
        if any(rate <= 0 for rate in fx.values()):
            raise ValueError("Receipt fx rates must be positive")
        return cls(
            receipt_id=str(data["receipt_id"]),
            date=str(data["date"]),
            currency=str(data["currency"]),
            amount_local=amount,
            fee_local=fee,
            fee_mode=fee_mode,
            fx_rates=fx,
            source=(str(data["source"]) if data.get("source") is not None else None),
            note=(str(data["note"]) if data.get("note") is not None else None),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "receipt_id": self.receipt_id,
            "date": self.date,
            "currency": self.currency,
            "amount_local": self.amount_local,
            "fee_local": self.fee_local,
            "fee_mode": self.fee_mode,
            "fx_rates": dict(self.fx_rates),
            "source": self.source,
            "note": self.note,
        }


@dataclass(frozen=True, slots=True)
class ReceiptWithdrawal:
    """A partial claw-back of funds that a specific receipt brought in."""

    withdrawal_id: str
    receipt_id: str
    date: str
    amount_local: float
    note: str | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ReceiptWithdrawal":
        missing = [
            name
            for name in ("withdrawal_id", "receipt_id", "date", "amount_local")
            if name not in data
        ]
        if missing:
            raise ValueError(f"Withdrawal missing fields: {', '.join(missing)}")
        amount = float(data["amount_local"])
        if amount <= 0:
            raise ValueError("Withdrawal amount_local must be positive")
        return cls(
            withdrawal_id=str(data["withdrawal_id"]),
            receipt_id=str(data["receipt_id"]),
            date=str(data["date"]),
            amount_local=amount,
            note=(str(data["note"]) if data.get("note") is not None else None),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "withdrawal_id": self.withdrawal_id,
            "receipt_id": self.receipt_id,
            "date": self.date,
            "amount_local": self.amount_local,
            "note": self.note,
        }


@dataclass(frozen=True, slots=True)
class Allocation:
    """One slice of a receipt applied to one settlement."""

    receipt_id: str
    settlement_id: str
    net_receipt_ccy: float
    fee_share_receipt_ccy: float
    fx_rate: float
    applied_obligation_ccy: float
    reversed_receipt_ccy: float = 0.0
    reinstated_obligation_ccy: float = 0.0

    @property
    def active_receipt_ccy(self) -> float:
        return self.net_receipt_ccy - self.reversed_receipt_ccy

    @property
    def active_obligation_ccy(self) -> float:
        return self.applied_obligation_ccy - self.reinstated_obligation_ccy

    def to_dict(self) -> dict[str, Any]:
        return {
            "receipt_id": self.receipt_id,
            "settlement_id": self.settlement_id,
            "net_receipt_ccy": self.net_receipt_ccy,
            "fee_share_receipt_ccy": self.fee_share_receipt_ccy,
            "fx_rate": self.fx_rate,
            "applied_obligation_ccy": self.applied_obligation_ccy,
            "reversed_receipt_ccy": self.reversed_receipt_ccy,
            "reinstated_obligation_ccy": self.reinstated_obligation_ccy,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Allocation":
        return cls(
            receipt_id=str(data["receipt_id"]),
            settlement_id=str(data["settlement_id"]),
            net_receipt_ccy=float(data["net_receipt_ccy"]),
            fee_share_receipt_ccy=float(data["fee_share_receipt_ccy"]),
            fx_rate=float(data["fx_rate"]),
            applied_obligation_ccy=float(data["applied_obligation_ccy"]),
            reversed_receipt_ccy=float(data.get("reversed_receipt_ccy", 0.0)),
            reinstated_obligation_ccy=float(
                data.get("reinstated_obligation_ccy", 0.0)
            ),
        )


@dataclass(frozen=True, slots=True)
class ReceiptAccount:
    """Per-receipt money trail; the identity always closes."""

    receipt_id: str
    date: str
    currency: str
    received_local: float
    fee_local: float
    fee_mode: str
    allocated_net_local: float
    unallocated_residual_local: float
    withdrawn_from_residual_local: float
    withdrawn_from_allocations_local: float
    rejected_withdrawal_local: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "receipt_id": self.receipt_id,
            "date": self.date,
            "currency": self.currency,
            "received_local": self.received_local,
            "fee_local": self.fee_local,
            "fee_mode": self.fee_mode,
            "allocated_net_local": self.allocated_net_local,
            "unallocated_residual_local": self.unallocated_residual_local,
            "withdrawn_from_residual_local": self.withdrawn_from_residual_local,
            "withdrawn_from_allocations_local": self.withdrawn_from_allocations_local,
            "rejected_withdrawal_local": self.rejected_withdrawal_local,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ReceiptAccount":
        return cls(
            receipt_id=str(data["receipt_id"]),
            date=str(data["date"]),
            currency=str(data["currency"]),
            received_local=float(data["received_local"]),
            fee_local=float(data["fee_local"]),
            fee_mode=str(data["fee_mode"]),
            allocated_net_local=float(data["allocated_net_local"]),
            unallocated_residual_local=float(data["unallocated_residual_local"]),
            withdrawn_from_residual_local=float(
                data["withdrawn_from_residual_local"]
            ),
            withdrawn_from_allocations_local=float(
                data["withdrawn_from_allocations_local"]
            ),
            rejected_withdrawal_local=float(data["rejected_withdrawal_local"]),
        )


@dataclass(frozen=True, slots=True)
class AllocationPlanResult:
    plan_id: str
    method: str
    fx_round_digits: int
    allocations: tuple[Allocation, ...]
    receipt_accounts: tuple[ReceiptAccount, ...]
    remaining_by_settlement: dict[str, float]
    shortfall_by_settlement: dict[str, float]
    notice_log: tuple[tuple[str, str, str, str, float], ...] = ()
    adopted: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "plan_id": self.plan_id,
            "method": self.method,
            "fx_round_digits": self.fx_round_digits,
            "allocations": [item.to_dict() for item in self.allocations],
            "receipt_accounts": [item.to_dict() for item in self.receipt_accounts],
            "remaining_by_settlement": dict(self.remaining_by_settlement),
            "shortfall_by_settlement": dict(self.shortfall_by_settlement),
            "notice_log": [list(entry) for entry in self.notice_log],
            "adopted": self.adopted,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "AllocationPlanResult":
        return cls(
            plan_id=str(data["plan_id"]),
            method=str(data["method"]),
            fx_round_digits=int(data.get("fx_round_digits", 2)),
            allocations=tuple(
                Allocation.from_dict(item) for item in data.get("allocations", ())
            ),
            receipt_accounts=tuple(
                ReceiptAccount.from_dict(item)
                for item in data.get("receipt_accounts", ())
            ),
            remaining_by_settlement={
                str(key): float(value)
                for key, value in data.get("remaining_by_settlement", {}).items()
            },
            shortfall_by_settlement={
                str(key): float(value)
                for key, value in data.get("shortfall_by_settlement", {}).items()
            },
            notice_log=tuple(
                (str(a), str(b), str(c), str(d), float(e))
                for a, b, c, d, e in data.get("notice_log", ())
            ),
            adopted=bool(data.get("adopted", False)),
        )


def _unit(digits: int) -> Decimal:
    return _DECI(10) ** -digits


@dataclass(frozen=True, slots=True)
class AllocationStudy:
    """Parallel-scheme research workspace for receipt allocation.

    Obligations, actual receipts, partial withdrawals and late duplicate
    notices are shared inputs. Every saved plan is an independent replay of
    those inputs, so plans never interact. ``adopted_plan_id`` names the only
    plan that feeds the current reconciliation; adopting is idempotent.
    """

    obligations: tuple[SettlementObligation, ...] = ()
    receipts: tuple[Receipt, ...] = ()
    withdrawals: tuple[ReceiptWithdrawal, ...] = ()
    plans: tuple[AllocationPlanResult, ...] = ()
    adopted_plan_id: str | None = None
    fx_round_digits: int = 2
    duplicate_notices: tuple[tuple[str, str, float, str | None], ...] = ()

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "AllocationStudy":
        return cls(
            obligations=tuple(
                SettlementObligation.from_dict(item)
                for item in data.get("obligations", ())
            ),
            receipts=tuple(
                Receipt.from_dict(item) for item in data.get("receipts", ())
            ),
            withdrawals=tuple(
                ReceiptWithdrawal.from_dict(item)
                for item in data.get("withdrawals", ())
            ),
            plans=tuple(
                AllocationPlanResult.from_dict(item)
                for item in data.get("plans", ())
            ),
            adopted_plan_id=(
                str(data["adopted_plan_id"])
                if data.get("adopted_plan_id") is not None
                else None
            ),
            fx_round_digits=int(data.get("fx_round_digits", 2)),
            duplicate_notices=tuple(
                (str(a), str(b), float(c), d if d is None else str(d))
                for a, b, c, d in data.get("duplicate_notices", ())
            ),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "obligations": [item.to_dict() for item in self.obligations],
            "receipts": [item.to_dict() for item in self.receipts],
            "withdrawals": [item.to_dict() for item in self.withdrawals],
            "plans": [item.to_dict() for item in self.plans],
            "adopted_plan_id": self.adopted_plan_id,
            "fx_round_digits": self.fx_round_digits,
            "duplicate_notices": [list(item) for item in self.duplicate_notices],
        }

    def obligation(self, settlement_id: str) -> SettlementObligation:
        for item in self.obligations:
            if item.settlement_id == settlement_id:
                return item
        raise ValueError(f"Unknown settlement {settlement_id}")

    def add_obligation(self, obligation: SettlementObligation) -> "AllocationStudy":
        if any(
            item.settlement_id == obligation.settlement_id
            for item in self.obligations
        ):
            raise ValueError(f"Duplicate settlement {obligation.settlement_id}")
        return replace(self, obligations=self.obligations + (obligation,))

    def add_receipt(self, receipt: Receipt) -> "AllocationStudy":
        if any(item.receipt_id == receipt.receipt_id for item in self.receipts):
            raise ValueError(f"Duplicate receipt {receipt.receipt_id}")
        return replace(self, receipts=self.receipts + (receipt,))

    def add_withdrawal(
        self, withdrawal: ReceiptWithdrawal
    ) -> "AllocationStudy":
        if not any(
            item.receipt_id == withdrawal.receipt_id for item in self.receipts
        ):
            raise ValueError(
                f"Withdrawal {withdrawal.withdrawal_id} references unknown receipt "
                f"{withdrawal.receipt_id}"
            )
        if any(
            item.withdrawal_id == withdrawal.withdrawal_id
            for item in self.withdrawals
        ):
            raise ValueError(
                f"Duplicate withdrawal {withdrawal.withdrawal_id}"
            )
        return replace(self, withdrawals=self.withdrawals + (withdrawal,))

    def record_duplicate_notice(
        self,
        receipt_id: str,
        date: str,
        amount_local: float,
        note: str | None = None,
    ) -> "AllocationStudy":
        """Log a late/duplicate arrival notice; it never adds to any balance."""
        if amount_local <= 0:
            raise ValueError("Duplicate notice amount_local must be positive")
        entry = (receipt_id, date, amount_local, note)
        return replace(
            self, duplicate_notices=self.duplicate_notices + (entry,)
        )

    def save_plan(
        self,
        plan_id: str,
        method: str,
        designations: dict[str, dict[str, float]] | None = None,
    ) -> "AllocationStudy":
        """Compute and store one parallel plan; recomputation is idempotent."""
        if method not in METHODS:
            raise ValueError(f"Unknown allocation method {method}")
        if any(item.plan_id == plan_id for item in self.plans):
            raise ValueError(f"Plan {plan_id} already saved")
        result = self._compute_plan(plan_id, method, designations or {})
        return replace(self, plans=self.plans + (result,))

    def replace_plan(
        self,
        plan_id: str,
        method: str,
        designations: dict[str, dict[str, float]] | None = None,
    ) -> "AllocationStudy":
        """Recompute a stored plan from the current shared inputs."""
        result = self._compute_plan(plan_id, method, designations or {})
        kept = tuple(item for item in self.plans if item.plan_id != plan_id)
        study = replace(self, plans=kept + (result,))
        if self.adopted_plan_id == plan_id:
            study = replace(study, adopted_plan_id=None)
        return study

    def adopt(self, plan_id: str) -> "AllocationStudy":
        """Mark the sole plan feeding the current reconciliation (idempotent)."""
        if not any(item.plan_id == plan_id for item in self.plans):
            raise ValueError(f"Unknown plan {plan_id}")
        plans = tuple(
            replace(item, adopted=(item.plan_id == plan_id))
            for item in self.plans
        )
        return replace(self, plans=plans, adopted_plan_id=plan_id)

    def plan(self, plan_id: str) -> AllocationPlanResult:
        for item in self.plans:
            if item.plan_id == plan_id:
                return item
        raise ValueError(f"Unknown plan {plan_id}")

    def _ordered_events(self) -> list[tuple[str, int, Any]]:
        events: list[tuple[str, int, Any]] = [
            ("receipt", 0, item) for item in self.receipts
        ]
        events.extend(("withdrawal", 1, item) for item in self.withdrawals)
        return sorted(events, key=lambda entry: (entry[2].date, entry[1]))

    def _compute_plan(
        self,
        plan_id: str,
        method: str,
        designations: dict[str, dict[str, float]],
    ) -> AllocationPlanResult:
        digits = self.fx_round_digits
        unit = _unit(digits)
        remaining: dict[str, Decimal] = {
            item.settlement_id: _DECI(str(item.amount_local))
            for item in self.obligations
        }
        allocations: list[Allocation] = []
        residual: dict[str, Decimal] = {
            item.receipt_id: _DECI("0") for item in self.receipts
        }
        withdrawn_residual: dict[str, Decimal] = {
            item.receipt_id: _DECI("0") for item in self.receipts
        }
        withdrawn_alloc: dict[str, Decimal] = {
            item.receipt_id: _DECI("0") for item in self.receipts
        }
        rejected_withdrawal: dict[str, Decimal] = {
            item.receipt_id: _DECI("0") for item in self.receipts
        }

        def capacity_receipt_ccy(
            receipt: Receipt, obligation: SettlementObligation
        ) -> Decimal | None:
            if remaining.get(obligation.settlement_id, _DECI("0")) <= 0:
                return None
            rate = receipt.fx_rates.get(obligation.currency)
            if rate is None:
                return None
            rate_d = _DECI(str(rate))
            # floor so conversion rounding can never overpay the obligation
            converted = (
                remaining[obligation.settlement_id] / rate_d
            ).quantize(unit, rounding=ROUND_FLOOR)
            return max(converted, _DECI("0"))

        def allocate_slice(
            receipt: Receipt,
            settlement_id: str,
            net_slice: Decimal,
        ) -> None:
            obligation = self.obligation(settlement_id)
            rate = receipt.fx_rates.get(obligation.currency)
            if rate is None or net_slice <= 0:
                return
            cap = capacity_receipt_ccy(receipt, obligation)
            if cap is None or cap <= 0:
                return
            used = min(net_slice, cap)
            if used <= 0:
                return
            rate_d = _DECI(str(rate))
            applied = (used * rate_d).quantize(unit, rounding=ROUND_FLOOR)
            if applied <= 0:
                return
            remaining[settlement_id] -= applied
            allocations.append(
                Allocation(
                    receipt_id=receipt.receipt_id,
                    settlement_id=settlement_id,
                    net_receipt_ccy=float(used),
                    fee_share_receipt_ccy=0.0,
                    fx_rate=rate,
                    applied_obligation_ccy=float(applied),
                )
            )

        notice_log: list[tuple[str, str, str, str, float]] = []
        for receipt_id, date, amount, note in self.duplicate_notices:
            notice_log.append(
                (date, receipt_id, "duplicate_receipt", note or "", amount)
            )

        for kind, _, event in self._ordered_events():
            if kind == "receipt":
                receipt = event
                net_pool = _DECI(str(receipt.amount_local - receipt.fee_local))
                if method == EXPLICIT:
                    specified = designations.get(receipt.receipt_id, {})
                    requested = {
                        str(settlement_id): _DECI(str(value))
                        for settlement_id, value in specified.items()
                    }
                    total_requested = sum(requested.values(), _DECI("0"))
                    if total_requested > net_pool + unit / 2:
                        raise ValueError(
                            f"Explicit designations for receipt {receipt.receipt_id} "
                            "exceed its net received amount"
                        )
                    for settlement_id in requested:
                        obligation = self.obligation(settlement_id)
                        if obligation.currency not in receipt.fx_rates:
                            raise ValueError(
                                f"Receipt {receipt.receipt_id} has no FX rate "
                                f"for settlement {settlement_id} currency "
                                f"{obligation.currency}"
                            )
                    eligible = requested
                elif method == PRO_RATA:
                    caps = {
                        obligation.settlement_id: cap
                        for obligation in self.obligations
                        if (cap := capacity_receipt_ccy(receipt, obligation))
                        is not None
                        and cap > 0
                    }
                    total_cap = sum(caps.values(), _DECI("0"))
                    eligible = self._largest_remainder_shares(
                        net_pool, caps, total_cap, unit
                    )
                else:
                    eligible = {}
                    ordered = sorted(
                        self.obligations,
                        key=lambda item: (item.due_date, item.settlement_id),
                    )
                    left = net_pool
                    for obligation in ordered:
                        cap = capacity_receipt_ccy(receipt, obligation)
                        if cap is None or cap <= 0 or left <= 0:
                            continue
                        used = min(left, cap)
                        eligible[obligation.settlement_id] = used
                        left -= used
                allocated_before = len(allocations)
                for settlement_id, net_slice in eligible.items():
                    allocate_slice(receipt, settlement_id, net_slice)
                allocated_net = sum(
                    (
                        _DECI(str(item.net_receipt_ccy))
                        for item in allocations[allocated_before:]
                    ),
                    _DECI("0"),
                )
                residual[receipt.receipt_id] = net_pool - allocated_net
                if receipt.fee_mode == "amortize" and allocated_net > 0:
                    fee = _DECI(str(receipt.fee_local))
                    fee_shares = self._largest_remainder_shares(
                        fee,
                        {
                            index: _DECI(
                                str(allocations[index].net_receipt_ccy)
                            )
                            for index in range(allocated_before, len(allocations))
                        },
                        allocated_net,
                        unit,
                        key_by_index=True,
                        start_index=allocated_before,
                    )
                    for index, share in fee_shares.items():
                        current = allocations[index]
                        allocations[index] = replace(
                            current, fee_share_receipt_ccy=float(share)
                        )
            else:
                withdrawal = event
                available = (
                    residual[withdrawal.receipt_id]
                    + sum(
                        (
                            _DECI(str(item.net_receipt_ccy))
                            - _DECI(str(item.reversed_receipt_ccy))
                            for item in allocations
                            if item.receipt_id == withdrawal.receipt_id
                        ),
                        _DECI("0"),
                    )
                )
                request = _DECI(str(withdrawal.amount_local))
                if request > available + unit / 2:
                    rejected_withdrawal[withdrawal.receipt_id] += request - available
                    notice_log.append(
                        (
                            withdrawal.date,
                            withdrawal.receipt_id,
                            "withdrawal_shortfall",
                            withdrawal.note or "",
                            float(request - available),
                        )
                    )
                left = min(request, available)
                from_res = min(left, residual[withdrawal.receipt_id])
                residual[withdrawal.receipt_id] -= from_res
                withdrawn_residual[withdrawal.receipt_id] += from_res
                left -= from_res
                indexes = [
                    index
                    for index, item in enumerate(allocations)
                    if item.receipt_id == withdrawal.receipt_id
                    and item.active_receipt_ccy > 0
                ]
                indexes.reverse()
                for index in indexes:
                    if left <= 0:
                        break
                    current = allocations[index]
                    active = _DECI(str(current.net_receipt_ccy)) - _DECI(
                        str(current.reversed_receipt_ccy)
                    )
                    used = min(left, active)
                    reinstated = (
                        used * _DECI(str(current.fx_rate))
                    ).quantize(unit, rounding=ROUND_FLOOR)
                    remaining[current.settlement_id] = (
                        remaining.get(current.settlement_id, _DECI("0"))
                        + reinstated
                    )
                    allocations[index] = replace(
                        current,
                        reversed_receipt_ccy=float(
                            _DECI(str(current.reversed_receipt_ccy)) + used
                        ),
                        reinstated_obligation_ccy=float(
                            _DECI(str(current.reinstated_obligation_ccy))
                            + reinstated
                        ),
                    )
                    withdrawn_alloc[withdrawal.receipt_id] += used
                    left -= used
                notice_log.append(
                    (
                        withdrawal.date,
                        withdrawal.receipt_id,
                        "withdrawal",
                        withdrawal.note or "",
                        float(
                            min(request, available)
                            if request > available + unit / 2
                            else request
                        ),
                    )
                )

        accounts = []
        for receipt in self.receipts:
            allocated_net = sum(
                (
                    _DECI(str(item.net_receipt_ccy))
                    - _DECI(str(item.reversed_receipt_ccy))
                    for item in allocations
                    if item.receipt_id == receipt.receipt_id
                ),
                _DECI("0"),
            )
            accounts.append(
                ReceiptAccount(
                    receipt_id=receipt.receipt_id,
                    date=receipt.date,
                    currency=receipt.currency,
                    received_local=receipt.amount_local,
                    fee_local=receipt.fee_local,
                    fee_mode=receipt.fee_mode,
                    allocated_net_local=float(allocated_net),
                    unallocated_residual_local=float(
                        residual[receipt.receipt_id]
                    ),
                    withdrawn_from_residual_local=float(
                        withdrawn_residual[receipt.receipt_id]
                    ),
                    withdrawn_from_allocations_local=float(
                        withdrawn_alloc[receipt.receipt_id]
                    ),
                    rejected_withdrawal_local=float(
                        rejected_withdrawal[receipt.receipt_id]
                    ),
                )
            )
        notice_log.sort(key=lambda entry: (entry[0], entry[2]))
        shortfall = {
            settlement_id: float(value)
            for settlement_id, value in remaining.items()
            if value > unit / 2
        }
        return AllocationPlanResult(
            plan_id=plan_id,
            method=method,
            fx_round_digits=digits,
            allocations=tuple(allocations),
            receipt_accounts=tuple(accounts),
            remaining_by_settlement={
                key: float(value) for key, value in remaining.items()
            },
            shortfall_by_settlement=shortfall,
            notice_log=tuple(notice_log),
            adopted=self.adopted_plan_id == plan_id,
        )

    @staticmethod
    def _largest_remainder_shares(
        pool: Decimal,
        caps: dict[Any, Decimal],
        total_cap: Decimal,
        unit: Decimal,
        key_by_index: bool = False,
        start_index: int = 0,
    ) -> dict[Any, Decimal]:
        """Split pool pro-rata by caps, flooring each share to the unit.

        The floor keepings are handed out one unit at a time by largest
        remainder, so shares never exceed caps and never exceed the pool.
        """
        if total_cap <= 0 or pool <= 0:
            return {}
        pool = min(pool, total_cap)
        raw = {key: pool * cap / total_cap for key, cap in caps.items()}
        floored = {
            key: value.quantize(unit, rounding=ROUND_FLOOR) for key, value in raw.items()
        }
        if key_by_index:
            order = sorted(
                caps,
                key=lambda index: (
                    -(raw[index] - floored[index]),
                    index - start_index,
                ),
            )
        else:
            order = sorted(
                caps,
                key=lambda key: (-(raw[key] - floored[key]), str(key)),
            )
        leftover = pool - sum(floored.values(), _DECI("0"))
        steps = int((leftover / unit).to_integral_value(rounding=ROUND_HALF_UP))
        for key in order:
            if steps <= 0:
                break
            if floored[key] < caps[key]:
                floored[key] += unit
                steps -= 1
        return {key: value for key, value in floored.items() if value > 0}

    def compare_plans(self) -> dict[str, dict[str, Any]]:
        """Side-by-side comparison of every saved parallel plan."""
        columns: dict[str, dict[str, Any]] = {}
        for plan in self.plans:
            cash_by_currency = self._cash_by_currency(plan)
            allocated_by_currency: dict[str, float] = {}
            for allocation in plan.allocations:
                obligation = self.obligation(allocation.settlement_id)
                allocated_by_currency[obligation.currency] = (
                    allocated_by_currency.get(obligation.currency, 0.0)
                    + allocation.active_obligation_ccy
                )
            fees_by_currency: dict[str, float] = {}
            for account in plan.receipt_accounts:
                fees_by_currency[account.currency] = (
                    fees_by_currency.get(account.currency, 0.0)
                    + account.fee_local
                )
            residual_by_currency: dict[str, float] = {}
            for account in plan.receipt_accounts:
                residual_by_currency[account.currency] = (
                    residual_by_currency.get(account.currency, 0.0)
                    + account.unallocated_residual_local
                )
            columns[plan.plan_id] = {
                "method": plan.method,
                "adopted": plan.adopted,
                "cash_remaining_by_ccy": cash_by_currency,
                "fees_by_ccy": fees_by_currency,
                "unallocated_residual_by_ccy": residual_by_currency,
                "allocated_by_settlement_ccy": {
                    settlement_id: self._allocated_to(plan, settlement_id)
                    for settlement_id in plan.remaining_by_settlement
                },
                "remaining_by_settlement_ccy": dict(
                    plan.remaining_by_settlement
                ),
                "shortfall_by_settlement_ccy": dict(
                    plan.shortfall_by_settlement
                ),
                "notice_kinds": self._notice_kinds(plan),
            }
        return columns

    def current_reconciliation(self) -> dict[str, Any]:
        """Cash, fees and remaining obligations under the adopted plan only.

        Saving, recomputing or re-adopting plans leaves the current book
        untouched until a plan is explicitly adopted; adopting the same plan
        again returns the identical reconciliation.
        """
        if self.adopted_plan_id is None:
            return {
                "adopted_plan_id": None,
                "status": "no_adopted_plan",
                "cash_remaining_by_ccy": {},
                "fees_by_ccy": {},
                "unallocated_residual_by_ccy": {},
                "allocated_by_settlement_ccy": {},
                "remaining_obligations_by_ccy": self._obligations_by_currency(),
                "duplicate_notice_count": len(self.duplicate_notices),
            }
        plan = self.plan(self.adopted_plan_id)
        remaining_by_currency: dict[str, float] = {}
        for obligation in self.obligations:
            value = plan.remaining_by_settlement.get(
                obligation.settlement_id, obligation.amount_local
            )
            remaining_by_currency[obligation.currency] = (
                remaining_by_currency.get(obligation.currency, 0.0) + value
            )
        return {
            "adopted_plan_id": self.adopted_plan_id,
            "method": plan.method,
            "status": "active",
            "cash_remaining_by_ccy": self._cash_by_currency(plan),
            "fees_by_ccy": {
                account.currency: sum(
                    item.fee_local
                    for item in plan.receipt_accounts
                    if item.currency == account.currency
                )
                for account in plan.receipt_accounts
            },
            "unallocated_residual_by_ccy": {
                account.currency: sum(
                    item.unallocated_residual_local
                    for item in plan.receipt_accounts
                    if item.currency == account.currency
                )
                for account in plan.receipt_accounts
            },
            "allocated_by_settlement_ccy": {
                settlement_id: self._allocated_to(plan, settlement_id)
                for settlement_id in plan.remaining_by_settlement
            },
            "remaining_obligations_by_ccy": remaining_by_currency,
            "shortfall_by_settlement_ccy": dict(plan.shortfall_by_settlement),
            "duplicate_notice_count": len(self.duplicate_notices),
            "notice_log": list(plan.notice_log),
        }

    def _cash_by_currency(self, plan: AllocationPlanResult) -> dict[str, float]:
        cash: dict[str, float] = {}
        for account in plan.receipt_accounts:
            cash[account.currency] = (
                cash.get(account.currency, 0.0)
                + account.unallocated_residual_local
            )
        return cash

    def _obligations_by_currency(self) -> dict[str, float]:
        totals: dict[str, float] = {}
        for obligation in self.obligations:
            totals[obligation.currency] = (
                totals.get(obligation.currency, 0.0) + obligation.amount_local
            )
        return totals

    @staticmethod
    def _allocated_to(
        plan: AllocationPlanResult, settlement_id: str
    ) -> float:
        return sum(
            item.active_obligation_ccy
            for item in plan.allocations
            if item.settlement_id == settlement_id
        )

    @staticmethod
    def _notice_kinds(plan: AllocationPlanResult) -> dict[str, int]:
        kinds: dict[str, int] = {}
        for _, _, kind, _, _ in plan.notice_log:
            kinds[kind] = kinds.get(kind, 0) + 1
        return kinds

    def assert_books_close(self, plan_id: str, tolerance: float = 0.005) -> None:
        """Verify the per-receipt and per-settlement money identities."""
        plan = self.plan(plan_id)
        for account in plan.receipt_accounts:
            used = (
                account.allocated_net_local
                + account.unallocated_residual_local
                + account.withdrawn_from_residual_local
                + account.withdrawn_from_allocations_local
                + account.fee_local
            )
            if abs(used - account.received_local) > tolerance:
                raise AssertionError(
                    f"Receipt {account.receipt_id} books do not close: "
                    f"{used} != {account.received_local}"
                )
            active_allocations = sum(
                item.active_receipt_ccy
                for item in plan.allocations
                if item.receipt_id == account.receipt_id
            )
            if abs(active_allocations - account.allocated_net_local) > tolerance:
                raise AssertionError(
                    f"Receipt {account.receipt_id} allocations do not match ledger"
                )
        for obligation in self.obligations:
            allocated = self._allocated_to(plan, obligation.settlement_id)
            remaining = plan.remaining_by_settlement.get(
                obligation.settlement_id, obligation.amount_local
            )
            if allocated > obligation.amount_local + tolerance:
                raise AssertionError(
                    f"Settlement {obligation.settlement_id} was overpaid"
                )
            if abs(allocated + remaining - obligation.amount_local) > tolerance:
                raise AssertionError(
                    f"Settlement {obligation.settlement_id} books do not close"
                )


def load_allocation_study(path: str) -> AllocationStudy:
    import json

    with open(path, encoding="utf-8") as handle:
        return AllocationStudy.from_dict(json.load(handle))


def save_allocation_study(study: AllocationStudy, path: str) -> None:
    """Atomically persist the study so parallel plans survive reopening."""
    import json
    import os

    temporary = f"{path}.tmp"
    with open(temporary, "w", encoding="utf-8") as handle:
        json.dump(study.to_dict(), handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")
    os.replace(temporary, path)
