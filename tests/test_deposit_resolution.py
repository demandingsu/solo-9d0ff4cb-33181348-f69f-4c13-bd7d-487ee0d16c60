import unittest
from datetime import datetime
from decimal import Decimal

from tendercheck.deposit_models import (
    DepositCheckResult,
    DepositRecord,
    STATUS_CLEARED,
    STATUS_OUTSTANDING,
    STATUS_PENDING,
    TX_ARRIVAL,
    TX_ERRATUM,
    TX_REFUND,
)
from tendercheck.deposit_resolution import resolve_deposit_check
from tendercheck.parsing import BEIJING_TZ, parse_datetime


NOW = datetime(2026, 11, 12, 12, 0, tzinfo=BEIJING_TZ)


def record(tx_type, serial, amount, publish, reference=None, fetch_ok=True, receipt="SJ-2026-0007"):
    return DepositRecord(
        url=f"http://example.test/{tx_type}-{serial or 'none'}",
        title=f"{tx_type}-{serial}",
        tx_type=tx_type,
        serial=serial,
        amount=Decimal(amount) if amount is not None else None,
        publish_time=parse_datetime(publish) if publish else None,
        receipt_no=receipt,
        reference=reference,
        fetch_ok=fetch_ok,
        error=None if fetch_ok else "HTTP 404",
    )


def resolve(records, issues=None, failed=None, pages=None):
    return resolve_deposit_check(
        project_no="GSB-2026-001",
        receipt_no="SJ-2026-0007",
        directory_url="http://example.test/deposits/page-1.html",
        records=records,
        issues=list(issues or []),
        failed_details=list(failed or []),
        pages=list(pages or ["http://example.test/deposits/page-1.html"]),
        duplicates_skipped=0,
        detail_count=len(records),
        now=NOW,
    )


class DepositResolutionTests(unittest.TestCase):
    def test_simple_outstanding_balance(self):
        result = resolve([
            record(TX_ARRIVAL, "A-1", "50000.00", "2026-09-20 09:00"),
            record(TX_REFUND, "R-1", "20000.00", "2026-10-15 14:00"),
        ])
        self.assertEqual(result.status, STATUS_OUTSTANDING)
        self.assertEqual(result.outstanding_amount, Decimal("30000.00"))
        self.assertEqual(result.arrival_amount, Decimal("50000.00"))
        self.assertEqual(result.refunded_amount, Decimal("20000.00"))
        self.assertFalse(result.issues)

    def test_fully_refunded_is_cleared(self):
        result = resolve([
            record(TX_ARRIVAL, "A-1", "50000.00", "2026-09-20 09:00"),
            record(TX_REFUND, "R-1", "30000.00", "2026-10-15 14:00"),
            record(TX_REFUND, "R-2", "20000.00", "2026-10-20 16:00"),
        ])
        self.assertEqual(result.status, STATUS_CLEARED)
        self.assertEqual(result.outstanding_amount, Decimal("0.00"))

    def test_erratum_replaces_refund_by_latest_publish_time(self):
        result = resolve([
            record(TX_ARRIVAL, "A-1", "50000.00", "2026-09-20 09:00"),
            record(TX_REFUND, "R-1", "20000.00", "2026-10-15 14:00"),
            record(TX_REFUND, "R-2", "15000.00", "2026-10-20 16:00"),
            record(TX_ERRATUM, "E-1", "12000.00", "2026-10-31 10:00", reference="R-2"),
            record(TX_ERRATUM, "E-2", "13000.00", "2026-11-01 10:00", reference="R-2"),
        ])
        self.assertEqual(result.status, STATUS_OUTSTANDING)
        self.assertEqual(result.outstanding_amount, Decimal("17000.00"))
        self.assertEqual(result.refunded_amount, Decimal("33000.00"))
        r2 = next(line for line in result.refunds if line.serial == "R-2")
        self.assertEqual(r2.amount, Decimal("13000.00"))
        self.assertEqual(r2.original_amount, Decimal("15000.00"))
        self.assertTrue(r2.corrected)

    def test_serial_dedupe_keeps_one_refund(self):
        result = resolve([
            record(TX_ARRIVAL, "A-1", "50000.00", "2026-09-20 09:00"),
            record(TX_REFUND, "R-1", "20000.00", "2026-10-15 14:00"),
            record(TX_REFUND, "R-1", "20000.00", "2026-10-15 14:00"),
        ])
        self.assertEqual(result.status, STATUS_OUTSTANDING)
        self.assertEqual(result.outstanding_amount, Decimal("30000.00"))
        self.assertEqual(len(result.refunds), 1)
        self.assertEqual(result.duplicates_skipped, 1)

    def test_arrival_amount_conflict_is_pending(self):
        result = resolve([
            record(TX_ARRIVAL, "A-1", "50000.00", "2026-09-20 09:00"),
            record(TX_ARRIVAL, "A-1", "55000.00", "2026-09-20 09:00"),
        ])
        self.assertEqual(result.status, STATUS_PENDING)
        self.assertIsNone(result.outstanding_amount)
        self.assertTrue(any("到账流水号 A-1" in issue for issue in result.issues))

    def test_same_moment_errata_conflict_is_pending(self):
        result = resolve([
            record(TX_ARRIVAL, "A-1", "50000.00", "2026-09-20 09:00"),
            record(TX_REFUND, "R-1", "20000.00", "2026-10-15 14:00"),
            record(TX_ERRATUM, "E-1", "12000.00", "2026-10-31 10:00", reference="R-1"),
            record(TX_ERRATUM, "E-2", "13000.00", "2026-10-31 10:00", reference="R-1"),
        ])
        self.assertEqual(result.status, STATUS_PENDING)
        self.assertIsNone(result.outstanding_amount)
        self.assertTrue(any("勘误互相冲突" in issue for issue in result.issues))
        line = result.refunds[0]
        self.assertEqual(line.amount, Decimal("20000.00"))
        self.assertIn("未采用", line.conflict_note)

    def test_errata_for_different_refunds_at_same_moment_do_not_conflict(self):
        result = resolve([
            record(TX_ARRIVAL, "A-1", "50000.00", "2026-09-20 09:00"),
            record(TX_REFUND, "R-1", "20000.00", "2026-10-15 14:00"),
            record(TX_REFUND, "R-2", "15000.00", "2026-10-20 16:00"),
            record(TX_ERRATUM, "E-1", "12000.00", "2026-10-31 10:00", reference="R-1"),
            record(TX_ERRATUM, "E-2", "14000.00", "2026-10-31 10:00", reference="R-2"),
        ])
        self.assertEqual(result.status, STATUS_OUTSTANDING)
        self.assertEqual(result.outstanding_amount, Decimal("24000.00"))

    def test_errata_reference_missing_serial_is_pending(self):
        result = resolve([
            record(TX_ARRIVAL, "A-1", "50000.00", "2026-09-20 09:00"),
            record(TX_REFUND, "R-1", "20000.00", "2026-10-15 14:00"),
            record(TX_ERRATUM, "E-9", "12000.00", "2026-10-31 10:00", reference="R-999"),
        ])
        self.assertEqual(result.status, STATUS_PENDING)
        self.assertIsNone(result.outstanding_amount)
        self.assertTrue(any("R-999" in issue and "不存在" in issue for issue in result.issues))

    def test_errata_without_reference_is_pending(self):
        result = resolve([
            record(TX_ARRIVAL, "A-1", "50000.00", "2026-09-20 09:00"),
            record(TX_REFUND, "R-1", "20000.00", "2026-10-15 14:00"),
            record(TX_ERRATUM, "E-1", "12000.00", "2026-10-31 10:00", reference=None),
        ])
        self.assertEqual(result.status, STATUS_PENDING)
        self.assertTrue(any("未标注引用" in issue for issue in result.issues))

    def test_refund_exceeding_arrival_is_pending(self):
        result = resolve([
            record(TX_ARRIVAL, "A-1", "50000.00", "2026-09-20 09:00"),
            record(TX_REFUND, "R-1", "55000.00", "2026-10-15 14:00"),
        ])
        self.assertEqual(result.status, STATUS_PENDING)
        self.assertIsNone(result.outstanding_amount)
        self.assertTrue(any("超过到账金额" in issue for issue in result.issues))

    def test_failed_detail_blocks_even_when_failed_list_not_passed(self):
        failed = record(TX_REFUND, None, None, "2026-10-20 16:00", fetch_ok=False)
        result = resolve([
            record(TX_ARRIVAL, "A-1", "50000.00", "2026-09-20 09:00"),
            failed,
        ])
        self.assertEqual(result.status, STATUS_PENDING)
        self.assertIsNone(result.outstanding_amount)
        self.assertTrue(any("明细抓取失败" in issue for issue in result.issues))

    def test_failed_detail_is_pending(self):
        failed = record(TX_REFUND, None, None, "2026-10-20 16:00", fetch_ok=False)
        result = resolve(
            [record(TX_ARRIVAL, "A-1", "50000.00", "2026-09-20 09:00"), failed],
            failed=[failed],
        )
        self.assertEqual(result.status, STATUS_PENDING)
        self.assertTrue(any("明细抓取失败" in issue for issue in result.issues))
        self.assertIn(failed.url, result.issues[0])

    def test_missing_refund_serial_is_pending(self):
        result = resolve([
            record(TX_ARRIVAL, "A-1", "50000.00", "2026-09-20 09:00"),
            record(TX_REFUND, None, "20000.00", "2026-10-15 14:00"),
        ])
        self.assertEqual(result.status, STATUS_PENDING)
        self.assertTrue(any("缺少流水号" in issue for issue in result.issues))

    def test_errata_referencing_arrival_serial_is_pending(self):
        result = resolve([
            record(TX_ARRIVAL, "A-1", "50000.00", "2026-09-20 09:00"),
            record(TX_REFUND, "R-1", "20000.00", "2026-10-15 14:00"),
            record(TX_ERRATUM, "E-1", "49000.00", "2026-10-31 10:00", reference="A-1"),
        ])
        self.assertEqual(result.status, STATUS_PENDING)
        self.assertTrue(any("不是退款记录" in issue for issue in result.issues))

    def test_serial_reused_across_types_is_pending(self):
        result = resolve([
            record(TX_ARRIVAL, "A-1", "50000.00", "2026-09-20 09:00"),
            record(TX_REFUND, "A-1", "20000.00", "2026-10-15 14:00"),
        ])
        self.assertEqual(result.status, STATUS_PENDING)
        self.assertTrue(any("不同明细类型" in issue for issue in result.issues))

    def test_missing_arrival_is_pending(self):
        result = resolve([record(TX_REFUND, "R-1", "20000.00", "2026-10-15 14:00")])
        self.assertEqual(result.status, STATUS_PENDING)
        self.assertTrue(any("未找到该收据的有效到账记录" in issue for issue in result.issues))

    def test_refund_without_amount_is_pending(self):
        result = resolve([
            record(TX_ARRIVAL, "A-1", "50000.00", "2026-09-20 09:00"),
            record(TX_REFUND, "R-1", None, "2026-10-15 14:00"),
        ])
        self.assertEqual(result.status, STATUS_PENDING)
        self.assertIsNone(result.refunded_amount)
        self.assertTrue(any("R-1" in issue and "金额" in issue for issue in result.issues))


if __name__ == "__main__":
    unittest.main()
