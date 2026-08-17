"""Contracts for the public SEC Form 13F monitor."""

from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PUBLIC_PAGE = ROOT / "index.html"
UPDATER = ROOT / "scripts" / "update_13f.py"
WORKFLOW = ROOT / ".github" / "workflows" / "update-13f.yml"
SNAPSHOT = ROOT / "data" / "guru_13f.json"


def load_updater():
    spec = importlib.util.spec_from_file_location("update_13f", UPDATER)
    if spec is None or spec.loader is None:
        raise AssertionError("Unable to load SEC 13F updater")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class GuruPublicPagePrivacyTests(unittest.TestCase):
    def test_public_portfolio_does_not_expose_private_13f_monitor(self) -> None:
        source = PUBLIC_PAGE.read_text(encoding="utf-8")

        for private_marker in (
            'href="#guru-13f"',
            'id="guru-13f"',
            'id="guruInvestor"',
            'fetch("data/guru_13f.json"',
            "GURU / 13F",
        ):
            with self.subTest(marker=private_marker):
                self.assertNotIn(private_marker, source)


class GuruCollectorContractTests(unittest.TestCase):
    def test_sec_13f_updater_exists(self) -> None:
        self.assertTrue(UPDATER.is_file(), "SEC 13F updater is not implemented")

    def test_parser_aggregates_duplicate_lines_without_assuming_value_units(self) -> None:
        updater = load_updater()
        xml = """<?xml version="1.0" encoding="UTF-8"?>
        <informationTable xmlns="http://www.sec.gov/edgar/document/thirteenf/informationtable">
          <infoTable>
            <nameOfIssuer>APPLE INC</nameOfIssuer><titleOfClass>COM</titleOfClass>
            <cusip>037833100</cusip><value>100</value>
            <shrsOrPrnAmt><sshPrnamt>10</sshPrnamt><sshPrnamtType>SH</sshPrnamtType></shrsOrPrnAmt>
          </infoTable>
          <infoTable>
            <nameOfIssuer>APPLE INC</nameOfIssuer><titleOfClass>COM</titleOfClass>
            <cusip>037833100</cusip><value>50</value>
            <shrsOrPrnAmt><sshPrnamt>5</sshPrnamt><sshPrnamtType>SH</sshPrnamtType></shrsOrPrnAmt>
          </infoTable>
          <infoTable>
            <nameOfIssuer>MICROSOFT CORP</nameOfIssuer><titleOfClass>COM</titleOfClass>
            <cusip>594918104</cusip><value>50</value>
            <shrsOrPrnAmt><sshPrnamt>2</sshPrnamt><sshPrnamtType>SH</sshPrnamtType></shrsOrPrnAmt>
          </infoTable>
        </informationTable>"""

        holdings = updater.parse_information_table(xml)

        self.assertEqual(2, len(holdings))
        apple = next(item for item in holdings if item["cusip"] == "037833100")
        self.assertEqual(150, apple["reported_value"])
        self.assertEqual(15, apple["shares"])

    def test_snapshot_uses_total_portfolio_weights_and_share_count_changes(self) -> None:
        updater = load_updater()
        manager = {
            "slug": "test-investor",
            "investor": "Test Investor",
            "firm": "Test Capital",
            "cik": "0000000001",
        }
        filing = {
            "report_date": "2026-06-30",
            "filed_at": "2026-08-14",
            "accession_number": "0000000001-26-000001",
            "filing_url": "https://www.sec.gov/Archives/example-index.html",
            "filer_name": "TEST CAPITAL LLC",
        }
        current = [
            {"issuer": "APPLE INC", "title_of_class": "COM", "cusip": "A", "put_call": None, "shares": 120, "share_type": "SH", "reported_value": 600_000},
            {"issuer": "MICROSOFT CORP", "title_of_class": "COM", "cusip": "M", "put_call": None, "shares": 30, "share_type": "SH", "reported_value": 300_000},
            {"issuer": "NVIDIA CORP", "title_of_class": "COM", "cusip": "N", "put_call": None, "shares": 10, "share_type": "SH", "reported_value": 100_000},
        ]
        previous = [
            {"issuer": "APPLE INC", "title_of_class": "COM", "cusip": "A", "put_call": None, "shares": 100, "share_type": "SH", "reported_value": 500_000},
            {"issuer": "MICROSOFT CORP", "title_of_class": "COM", "cusip": "M", "put_call": None, "shares": 60, "share_type": "SH", "reported_value": 300_000},
            {"issuer": "ALPHABET INC", "title_of_class": "COM", "cusip": "G", "put_call": None, "shares": 5, "share_type": "SH", "reported_value": 200_000},
        ]

        snapshot = updater.build_manager_snapshot(manager, filing, current, previous)

        self.assertNotIn("total_reported_value", snapshot)
        self.assertEqual([60.0, 30.0, 10.0], [item["weight_pct"] for item in snapshot["top_holdings"]])
        self.assertTrue(all("reported_value" not in item for item in snapshot["top_holdings"]))
        changes = {item["cusip"]: item for item in snapshot["changes"]}
        self.assertEqual(("INCREASED", 20.0), (changes["A"]["change_type"], changes["A"]["share_change_pct"]))
        self.assertEqual(("DECREASED", -50.0), (changes["M"]["change_type"], changes["M"]["share_change_pct"]))
        self.assertEqual("NEW", changes["N"]["change_type"])
        self.assertEqual("EXITED", changes["G"]["change_type"])

    def test_github_action_refreshes_only_the_public_13f_snapshot(self) -> None:
        self.assertTrue(WORKFLOW.is_file(), "Scheduled 13F workflow is not implemented")
        source = WORKFLOW.read_text(encoding="utf-8")

        self.assertIn("schedule:", source)
        self.assertIn("workflow_dispatch:", source)
        self.assertIn("contents: write", source)
        self.assertIn("python scripts/update_13f.py", source)
        self.assertIn("python -m unittest discover -s tests -v", source)
        self.assertIn("git add data/guru_13f.json", source)

    def test_newer_notice_is_distinguished_from_the_last_holdings_report(self) -> None:
        updater = load_updater()
        submissions = {
            "filings": {
                "recent": {
                    "accessionNumber": ["notice", "current", "prior"],
                    "form": ["13F-NT", "13F-HR", "13F-HR"],
                    "reportDate": ["2026-06-30", "2026-03-31", "2025-12-31"],
                    "filingDate": ["2026-08-14", "2026-05-15", "2026-02-17"],
                    "primaryDocument": ["notice.xml", "current.xml", "prior.xml"],
                }
            }
        }

        latest = updater.latest_13f_submission(submissions)
        holdings = updater._latest_original_13f_filings(submissions)

        self.assertEqual(("13F-NT", "2026-06-30"), (latest["form"], latest["reportDate"]))
        self.assertEqual("2026-03-31", holdings[0]["reportDate"])


class GuruGeneratedDataContractTests(unittest.TestCase):
    def test_snapshot_contains_weights_without_ambiguous_absolute_values(self) -> None:
        import json

        payload = json.loads(SNAPSHOT.read_text(encoding="utf-8"))

        self.assertEqual("wbq.guru-13f.v1", payload["schema_version"])
        self.assertGreaterEqual(len(payload["managers"]), 5)
        self.assertNotIn("reported_value", SNAPSHOT.read_text(encoding="utf-8"))
        for manager in payload["managers"]:
            total = sum(item["weight_pct"] for item in manager["top_holdings"])
            total += manager["other_weight_pct"]
            self.assertAlmostEqual(100.0, total, places=3)
            self.assertTrue(manager["filing_url"].startswith("https://www.sec.gov/"))
        ackman = next(item for item in payload["managers"] if item["investor"] == "Bill Ackman")
        self.assertEqual("13F-NT", ackman["latest_submission_form"])
        self.assertGreater(ackman["latest_submission_report_date"], ackman["report_date"])


if __name__ == "__main__":
    unittest.main()
