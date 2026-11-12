import threading
import unittest
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

try:
    from tendercheck.deposit_models import (
        STATUS_CLEARED,
        STATUS_OUTSTANDING,
        STATUS_PENDING,
    )
    from tendercheck.deposit_scraper import DepositScraper
except ModuleNotFoundError:  # requests/beautifulsoup4 are optional for core tests
    DepositScraper = None

ROOT = Path(__file__).resolve().parents[1] / "sample_site"


class QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, format, *args):
        pass


@unittest.skipIf(DepositScraper is None, "scraper dependencies are not installed")
class DepositScraperIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        handler = partial(QuietHandler, directory=str(ROOT))
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)

    def check(self, receipt_no, project_no="GSB-2026-001"):
        return DepositScraper(
            project_no=project_no,
            receipt_no=receipt_no,
            directory_url=f"{self.base}/deposits/page-1.html",
        ).run()

    def test_outstanding_balance_with_errata_and_dedupe(self):
        result = self.check("SJ-2026-0007")
        self.assertEqual(result.status, STATUS_OUTSTANDING)
        self.assertEqual(len(result.pages), 3)
        self.assertEqual(result.arrival_amount, 50000)
        # R-0001 (20,000) + R-0002 corrected from 15,000 to 12,000.
        self.assertEqual(str(result.refunded_amount), "32000.00")
        self.assertEqual(str(result.outstanding_amount), "18000.00")
        self.assertEqual(len(result.refunds), 2)
        self.assertGreaterEqual(result.duplicates_skipped, 2)
        r2 = next(line for line in result.refunds if line.serial == "R-0002")
        self.assertEqual(str(r2.original_amount), "15000.00")
        self.assertEqual(str(r2.amount), "12000.00")
        self.assertTrue(r2.corrected)

    def test_arrival_amount_conflict_is_pending(self):
        result = self.check("SJ-2026-0008", "GSB-2026-002")
        self.assertEqual(result.status, STATUS_PENDING)
        self.assertIsNone(result.outstanding_amount)
        self.assertTrue(any("到账流水号 A-0010" in issue for issue in result.issues))

    def test_same_moment_errata_conflict_is_pending(self):
        result = self.check("SJ-2026-0009", "GSB-2026-003")
        self.assertEqual(result.status, STATUS_PENDING)
        self.assertIsNone(result.outstanding_amount)
        self.assertTrue(any("勘误互相冲突" in issue for issue in result.issues))

    def test_errata_unknown_reference_is_pending(self):
        result = self.check("SJ-2026-0010", "GSB-2026-004")
        self.assertEqual(result.status, STATUS_PENDING)
        self.assertIsNone(result.outstanding_amount)
        self.assertTrue(any("R-9999" in issue for issue in result.issues))

    def test_fully_refunded_receipt(self):
        result = self.check("SJ-2026-0011", "GSB-2026-005")
        self.assertEqual(result.status, STATUS_CLEARED)
        self.assertEqual(str(result.outstanding_amount), "0.00")

    def test_failed_detail_is_pending(self):
        result = self.check("SJ-2026-0012", "GSB-2026-002")
        self.assertEqual(result.status, STATUS_PENDING)
        self.assertIsNone(result.outstanding_amount)
        self.assertTrue(any("明细抓取失败" in issue for issue in result.issues))

    def test_over_refunded_is_pending(self):
        result = self.check("SJ-2026-0013")
        self.assertEqual(result.status, STATUS_PENDING)
        self.assertIsNone(result.outstanding_amount)
        self.assertTrue(any("超过到账金额" in issue for issue in result.issues))

    def test_other_receipts_are_excluded(self):
        result = self.check("SJ-2026-0007")
        self.assertTrue(all("SJ-2026-0007" in item.url or item.receipt_no == "SJ-2026-0007" for item in result.records))
        self.assertFalse(any("R-0099" == line.serial for line in result.refunds))


if __name__ == "__main__":
    unittest.main()
