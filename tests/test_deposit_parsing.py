import unittest
from decimal import Decimal

from tendercheck.deposit_parsing import (
    detect_deposit_type,
    extract_amount,
    extract_erratum_amount,
    extract_original_amount,
    extract_receipt_no,
    extract_reference,
    extract_serial,
    format_amount,
    parse_amount,
    receipt_matches,
)


class DepositParsingTests(unittest.TestCase):
    def test_parse_amount_with_currency_and_commas(self):
        self.assertEqual(parse_amount("¥ 12,345.60 元"), Decimal("12345.60"))
        self.assertEqual(parse_amount("￥1，000"), Decimal("1000.00"))

    def test_parse_missing_amount_is_none(self):
        self.assertIsNone(parse_amount("未标注"))
        self.assertEqual(format_amount(None), "—")
        self.assertEqual(format_amount(Decimal("1234.5")), "1,234.50")

    def test_detect_deposit_type(self):
        self.assertEqual(detect_deposit_type("保证金到账明细"), "arrival")
        self.assertEqual(detect_deposit_type("保证金退款明细"), "refund")
        self.assertEqual(detect_deposit_type("退款金额勘误"), "erratum")
        self.assertIsNone(detect_deposit_type("目录说明"))

    def test_extract_ledger_fields(self):
        text = (
            "项目编号：GSB-2026-001 收据编号：SJ-2026-0007 "
            "业务类型：退款 流水号：R-0002 退款金额：15,000.00 元"
        )
        self.assertEqual(extract_receipt_no(text), "SJ-2026-0007")
        self.assertEqual(extract_serial(text), "R-0002")
        self.assertEqual(extract_amount(text), Decimal("15000.00"))

    def test_extract_erratum_reference_and_corrected_amount(self):
        text = (
            "勘误引用：R-0002 原退款金额：15,000.00 元 "
            "更正后退款金额：12,000.00 元 发布时间：2026-10-31 10:00"
        )
        self.assertEqual(extract_reference(text), "R-0002")
        self.assertEqual(extract_original_amount(text), Decimal("15000.00"))
        self.assertEqual(extract_erratum_amount(text), Decimal("12000.00"))

    def test_erratum_without_reference(self):
        self.assertIsNone(extract_reference("经财务复核，现对退款金额作勘误。"))

    def test_receipt_matching_is_token_based(self):
        self.assertTrue(receipt_matches("收据编号：SJ-2026-0007", "ｓｊ－2026－0007"))
        self.assertFalse(receipt_matches("收据编号：SJ-2026-00070", "SJ-2026-0007"))
        self.assertFalse(receipt_matches("收据编号：XSJ-2026-0007", "SJ-2026-0007"))


if __name__ == "__main__":
    unittest.main()
