"""Contracts for the public case study and encrypted private dashboard."""

from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PUBLIC_PAGE = ROOT / "index.html"
LOCK_PAGE = ROOT / "dashboard.html"


class PublicCaseStudyContractTests(unittest.TestCase):
    def test_presents_the_live_record_without_private_capital(self) -> None:
        source = PUBLIC_PAGE.read_text(encoding="utf-8")

        self.assertIn("+21.8%", source)
        self.assertIn("July 2025", source)
        self.assertIn("April 2026", source)
        self.assertIn("brokerage fees", source.lower())
        self.assertNotIn("58,260,000", source)
        self.assertNotIn("5826", source)

    def test_exposes_only_aggregate_monthly_returns(self) -> None:
        source = PUBLIC_PAGE.read_text(encoding="utf-8")

        for value in ("1.68", "1.77", "6.01", "-2.92", "-1.10", "7.75", "3.50", "-0.20", "-2.13", "6.18"):
            self.assertIn(value, source)
        self.assertNotIn("portfolio.enc.json", source)
        self.assertNotIn("cockpit.enc.json", source)

    def test_links_to_the_separate_private_dashboard(self) -> None:
        source = PUBLIC_PAGE.read_text(encoding="utf-8")

        self.assertIn('href="dashboard.html"', source)
        self.assertIn("Personal Quant Research", source)


class LockPageContractTests(unittest.TestCase):
    def test_uses_browser_side_authenticated_decryption(self) -> None:
        source = LOCK_PAGE.read_text(encoding="utf-8")

        self.assertIn('decryptFile("data/cockpit.enc.json"', source)
        self.assertIn('name:"PBKDF2"', source)
        self.assertIn('name:"AES-GCM"', source)
        self.assertIn('sandbox="allow-scripts"', source)

    def test_passphrase_is_never_persisted(self) -> None:
        source = LOCK_PAGE.read_text(encoding="utf-8")

        self.assertNotIn("localStorage", source)
        self.assertNotIn("sessionStorage", source)
        self.assertNotIn('id="remember"', source)
        self.assertNotIn("<form", source.lower())

    def test_decrypts_the_seasonality_payload_on_demand_in_memory(self) -> None:
        source = LOCK_PAGE.read_text(encoding="utf-8")

        self.assertIn('"data/analysis.enc.json"', source)
        self.assertIn("DecompressionStream", source)
        self.assertIn("wbq-analysis-request", source)
        self.assertIn("wbq-seasonality-data", source)
        self.assertIn("KEY_MATERIAL", source)
        self.assertIn("KEY_MATERIAL!==analysisMaterial", source)
        self.assertIn("frame.contentWindow", source)
        self.assertNotIn("PASS =", source)

    def test_matches_the_worldbestquant_product_shell(self) -> None:
        source = LOCK_PAGE.read_text(encoding="utf-8")

        self.assertIn("WorldBestQuant", source)
        self.assertIn("Personal Quant Investment Platform", source)
        self.assertIn('aria-label="대시보드 잠금 해제"', source)


if __name__ == "__main__":
    unittest.main()
