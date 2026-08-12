"""Security and product-shell contracts for the public lock page."""

from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class LockPageContractTests(unittest.TestCase):
    def test_uses_browser_side_authenticated_decryption(self) -> None:
        source = (ROOT / "index.html").read_text(encoding="utf-8")

        self.assertIn('decryptFile("data/cockpit.enc.json"', source)
        self.assertIn('name:"PBKDF2"', source)
        self.assertIn('name:"AES-GCM"', source)
        self.assertIn('sandbox="allow-scripts"', source)

    def test_passphrase_is_never_persisted(self) -> None:
        source = (ROOT / "index.html").read_text(encoding="utf-8")

        self.assertNotIn("localStorage", source)
        self.assertNotIn("sessionStorage", source)
        self.assertNotIn('id="remember"', source)
        self.assertNotIn("<form", source.lower())

    def test_decrypts_the_seasonality_payload_on_demand_in_memory(self) -> None:
        source = (ROOT / "index.html").read_text(encoding="utf-8")

        self.assertIn('"data/analysis.enc.json"', source)
        self.assertIn("DecompressionStream", source)
        self.assertIn("wbq-analysis-request", source)
        self.assertIn("wbq-seasonality-data", source)
        self.assertIn("KEY_MATERIAL", source)
        self.assertIn("KEY_MATERIAL!==analysisMaterial", source)
        self.assertIn("frame.contentWindow", source)
        self.assertNotIn("PASS =", source)

    def test_matches_the_worldbestquant_product_shell(self) -> None:
        source = (ROOT / "index.html").read_text(encoding="utf-8")

        self.assertIn("WorldBestQuant", source)
        self.assertIn("Personal Quant Investment Platform", source)
        self.assertIn('aria-label="대시보드 잠금 해제"', source)


if __name__ == "__main__":
    unittest.main()
