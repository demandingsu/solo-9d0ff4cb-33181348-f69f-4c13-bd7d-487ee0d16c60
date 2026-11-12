from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Optional


TX_ARRIVAL = "arrival"
TX_REFUND = "refund"
TX_ERRATUM = "erratum"

TX_LABELS = {
    TX_ARRIVAL: "到账",
    TX_REFUND: "退款",
    TX_ERRATUM: "勘误",
}

STATUS_CLEARED = "cleared"
STATUS_OUTSTANDING = "outstanding"
STATUS_PENDING = "pending"

DEPOSIT_STATUS_LABELS = {
    STATUS_CLEARED: "已退清",
    STATUS_OUTSTANDING: "尚有未退",
    STATUS_PENDING: "待核实",
}


@dataclass
class DepositRecord:
    """One deposit ledger entry read from a detail page."""

    url: str
    title: str
    tx_type: str
    serial: Optional[str]
    amount: Optional[Decimal]
    publish_time: Optional[datetime]
    project_no: Optional[str] = None
    receipt_no: Optional[str] = None
    reference: Optional[str] = None
    original_amount: Optional[Decimal] = None
    found_on: Optional[str] = None
    fetch_ok: bool = True
    error: Optional[str] = None
    source_note: Optional[str] = None
    # Set during resolution so the template can explain how an erratum was used.
    applied_to: Optional[str] = None

    @property
    def type_label(self) -> str:
        return TX_LABELS.get(self.tx_type, "其他")


@dataclass
class RefundLine:
    """The effective amount of one refund after applying errata."""

    serial: str
    publish_time: Optional[datetime]
    amount: Optional[Decimal]
    original_amount: Optional[Decimal]
    url: str
    title: str
    found_on: Optional[str] = None
    errata: list[DepositRecord] = field(default_factory=list)
    corrected: bool = False
    conflict_note: Optional[str] = None
    fetch_ok: bool = True

    @property
    def effective_amount(self) -> Optional[Decimal]:
        """The amount counted toward the refund total after errata."""

        return self.amount


@dataclass
class DepositCheckResult:
    project_no: str
    receipt_no: str
    directory_url: str
    status: str
    status_label: str
    headline: str
    checked_at: datetime
    arrival_amount: Optional[Decimal] = None
    refunded_amount: Optional[Decimal] = None
    outstanding_amount: Optional[Decimal] = None
    arrival_records: list[DepositRecord] = field(default_factory=list)
    refunds: list[RefundLine] = field(default_factory=list)
    errata: list[DepositRecord] = field(default_factory=list)
    records: list[DepositRecord] = field(default_factory=list)
    failed_details: list[DepositRecord] = field(default_factory=list)
    issues: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    pages: list[str] = field(default_factory=list)
    duplicates_skipped: int = 0
    detail_count: int = 0

    @property
    def refund_count(self) -> int:
        return len(self.refunds)
    @property
    def record_count(self) -> int:
        return len(self.records)

    @property
    def is_pending(self) -> bool:
        return self.status == STATUS_PENDING
