#!/usr/bin/env python3
"""Build the public WorldBestQuant 13F snapshot from SEC EDGAR filings.

The browser reads generated JSON from this repository and never calls SEC
directly, avoiding CORS failures and keeping EDGAR traffic centrally paced.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = ROOT / "data" / "guru_13f.json"
SEC_SUBMISSIONS = "https://data.sec.gov/submissions/CIK{cik}.json"
SEC_ARCHIVES = "https://www.sec.gov/Archives/edgar/data/{cik}/{accession}"
DEFAULT_USER_AGENT = (
    "WorldBestQuant 13F dashboard "
    "jaewookmo@gmail.com"
)

MANAGERS: tuple[dict[str, str], ...] = (
    {"slug": "warren-buffett", "investor": "Warren Buffett", "firm": "Berkshire Hathaway", "cik": "0001067983"},
    {"slug": "michael-burry", "investor": "Michael Burry", "firm": "Scion Asset Management", "cik": "0001649339"},
    {"slug": "bill-ackman", "investor": "Bill Ackman", "firm": "Pershing Square Capital Management", "cik": "0001336528"},
    {"slug": "seth-klarman", "investor": "Seth Klarman", "firm": "The Baupost Group", "cik": "0001061768"},
    {"slug": "daniel-loeb", "investor": "Daniel Loeb", "firm": "Third Point", "cik": "0001040273"},
    {"slug": "ray-dalio", "investor": "Ray Dalio", "firm": "Bridgewater Associates", "cik": "0001350694"},
    {"slug": "renaissance-technologies", "investor": "Renaissance Technologies", "firm": "Renaissance Technologies", "cik": "0001037389"},
    {"slug": "chase-coleman", "investor": "Chase Coleman", "firm": "Tiger Global Management", "cik": "0001167483"},
)


class SecClient:
    """Small EDGAR client with an explicit identity and conservative pacing."""

    def __init__(self, user_agent: str, min_interval_seconds: float = 0.25) -> None:
        if "@" not in user_agent:
            raise ValueError("SEC_USER_AGENT must include a contact email address")
        self.user_agent = user_agent
        self.min_interval_seconds = min_interval_seconds
        self._last_request_at = 0.0

    def get_bytes(self, url: str) -> bytes:
        elapsed = time.monotonic() - self._last_request_at
        if elapsed < self.min_interval_seconds:
            time.sleep(self.min_interval_seconds - elapsed)
        request = urllib.request.Request(
            url,
            headers={
                "User-Agent": self.user_agent,
                "Accept": "application/json, application/xml, text/xml, */*",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                body = response.read()
        finally:
            self._last_request_at = time.monotonic()
        return body

    def get_json(self, url: str) -> dict[str, Any]:
        return json.loads(self.get_bytes(url).decode("utf-8"))


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _descendant_text(node: ET.Element, wanted: str) -> str | None:
    for child in node.iter():
        if _local_name(child.tag).lower() == wanted.lower():
            text = (child.text or "").strip()
            return text or None
    return None


def _number(text: str | None) -> Decimal:
    if not text:
        return Decimal(0)
    try:
        return Decimal(text.replace(",", "").strip())
    except InvalidOperation as exc:
        raise ValueError(f"Invalid SEC numeric value: {text!r}") from exc


def _json_number(value: Decimal) -> int | float:
    integral = value.to_integral_value()
    return int(integral) if value == integral else float(value)


def _security_key(holding: dict[str, Any]) -> tuple[str, str, str, str, str]:
    return (
        str(holding.get("issuer") or "").strip().upper(),
        str(holding.get("title_of_class") or "").strip().upper(),
        str(holding.get("cusip") or "").strip().upper(),
        str(holding.get("put_call") or "").strip().upper(),
        str(holding.get("share_type") or "").strip().upper(),
    )


def parse_information_table(xml: str | bytes) -> list[dict[str, Any]]:
    """Parse and aggregate the numeric values exactly as filed with the SEC."""

    root = ET.fromstring(xml)
    aggregated: dict[tuple[str, str, str, str, str], dict[str, Any]] = {}
    for node in root.iter():
        if _local_name(node.tag).lower() != "infotable":
            continue
        issuer = _descendant_text(node, "nameOfIssuer")
        cusip = _descendant_text(node, "cusip")
        if not issuer or not cusip:
            continue
        holding: dict[str, Any] = {
            "issuer": issuer,
            "title_of_class": _descendant_text(node, "titleOfClass") or "",
            "cusip": cusip,
            "put_call": _descendant_text(node, "putCall"),
            "share_type": _descendant_text(node, "sshPrnamtType") or "",
            "shares": _json_number(_number(_descendant_text(node, "sshPrnamt"))),
            "reported_value": int(_number(_descendant_text(node, "value"))),
        }
        key = _security_key(holding)
        if key not in aggregated:
            aggregated[key] = holding
            continue
        prior = aggregated[key]
        prior["shares"] = _json_number(
            Decimal(str(prior["shares"])) + Decimal(str(holding["shares"]))
        )
        prior["reported_value"] += holding["reported_value"]

    holdings = list(aggregated.values())
    if not holdings:
        raise ValueError("No 13F information-table rows were found")
    holdings.sort(key=lambda item: (-int(item["reported_value"]), str(item["issuer"])))
    return holdings


def _holding_with_weight(holding: dict[str, Any], total_value: int) -> dict[str, Any]:
    weighted = dict(holding)
    weighted.pop("reported_value", None)
    weighted["weight_pct"] = (
        round(int(holding["reported_value"]) / total_value * 100, 4)
        if total_value
        else 0.0
    )
    return weighted


def _portfolio_map(holdings: list[dict[str, Any]]) -> dict[tuple[str, str, str, str, str], dict[str, Any]]:
    return {_security_key(holding): holding for holding in holdings}


def _build_changes(
    current: list[dict[str, Any]],
    previous: list[dict[str, Any]],
    change_limit: int,
) -> list[dict[str, Any]]:
    current_total = sum(int(item["reported_value"]) for item in current)
    previous_total = sum(int(item["reported_value"]) for item in previous)
    current_map = _portfolio_map(current)
    previous_map = _portfolio_map(previous)
    changes: list[dict[str, Any]] = []

    for key in current_map.keys() | previous_map.keys():
        now = current_map.get(key)
        before = previous_map.get(key)
        if now is None:
            source = before or {}
            kind = "EXITED"
            share_change_pct: float | None = -100.0
            current_shares: int | float = 0
            current_weight = 0.0
        elif before is None:
            source = now
            kind = "NEW"
            share_change_pct = None
            current_shares = now["shares"]
            current_weight = round(int(now["reported_value"]) / current_total * 100, 4) if current_total else 0.0
        else:
            now_shares = float(now["shares"])
            before_shares = float(before["shares"])
            if math.isclose(now_shares, before_shares, rel_tol=0, abs_tol=1e-9):
                continue
            source = now
            kind = "INCREASED" if now_shares > before_shares else "DECREASED"
            share_change_pct = (
                round((now_shares - before_shares) / before_shares * 100, 4)
                if before_shares
                else None
            )
            current_shares = now["shares"]
            current_weight = round(int(now["reported_value"]) / current_total * 100, 4) if current_total else 0.0

        previous_weight = (
            round(int(before["reported_value"]) / previous_total * 100, 4)
            if before is not None and previous_total
            else 0.0
        )
        changes.append(
            {
                "issuer": source.get("issuer"),
                "title_of_class": source.get("title_of_class"),
                "cusip": source.get("cusip"),
                "put_call": source.get("put_call"),
                "share_type": source.get("share_type"),
                "change_type": kind,
                "share_change_pct": share_change_pct,
                "previous_shares": before["shares"] if before is not None else 0,
                "current_shares": current_shares,
                "previous_weight_pct": previous_weight,
                "weight_pct": current_weight,
            }
        )

    changes.sort(
        key=lambda item: (
            -max(float(item["weight_pct"]), float(item["previous_weight_pct"])),
            str(item["issuer"]),
        )
    )
    return changes[:change_limit]


def build_manager_snapshot(
    manager: dict[str, str],
    filing: dict[str, Any],
    current: list[dict[str, Any]],
    previous: list[dict[str, Any]],
    *,
    top_limit: int = 20,
    change_limit: int = 30,
) -> dict[str, Any]:
    """Create one public manager record from current and prior holdings."""

    total_value = sum(int(item["reported_value"]) for item in current)
    weighted = [_holding_with_weight(item, total_value) for item in current]
    weighted.sort(key=lambda item: (-float(item["weight_pct"]), str(item["issuer"])))
    top_holdings = weighted[:top_limit]
    other_weight = round(
        max(0.0, 100.0 - sum(float(item["weight_pct"]) for item in top_holdings)),
        4,
    )
    return {
        **manager,
        "filer_name": filing.get("filer_name") or manager["firm"],
        "report_date": filing["report_date"],
        "filed_at": filing["filed_at"],
        "accession_number": filing["accession_number"],
        "filing_url": filing["filing_url"],
        "previous_report_date": filing.get("previous_report_date"),
        "latest_submission_form": filing.get("latest_submission_form", "13F-HR"),
        "latest_submission_report_date": filing.get(
            "latest_submission_report_date", filing["report_date"]
        ),
        "latest_submission_filed_at": filing.get(
            "latest_submission_filed_at", filing["filed_at"]
        ),
        "holding_count": len(current),
        "top_holdings": top_holdings,
        "other_weight_pct": other_weight,
        "changes": _build_changes(current, previous, change_limit),
    }


def _filing_rows(submissions: dict[str, Any]) -> list[dict[str, Any]]:
    recent = submissions.get("filings", {}).get("recent", {})
    accession_numbers = recent.get("accessionNumber", [])
    rows: list[dict[str, Any]] = []
    for index, accession in enumerate(accession_numbers):
        row = {
            key: values[index] if index < len(values) else None
            for key, values in recent.items()
            if isinstance(values, list)
        }
        row["accession_number"] = accession
        rows.append(row)
    return rows


def _latest_original_13f_filings(submissions: dict[str, Any], limit: int = 2) -> list[dict[str, Any]]:
    candidates = [
        row
        for row in _filing_rows(submissions)
        if row.get("form") == "13F-HR" and row.get("reportDate")
    ]
    candidates.sort(
        key=lambda row: (str(row.get("reportDate")), str(row.get("filingDate"))),
        reverse=True,
    )
    selected: list[dict[str, Any]] = []
    seen_report_dates: set[str] = set()
    for row in candidates:
        report_date = str(row["reportDate"])
        if report_date in seen_report_dates:
            continue
        selected.append(row)
        seen_report_dates.add(report_date)
        if len(selected) == limit:
            break
    if len(selected) < limit:
        raise ValueError("Fewer than two original 13F-HR filings are available")
    return selected


def latest_13f_submission(submissions: dict[str, Any]) -> dict[str, Any]:
    """Return the newest original holdings report or notice."""

    candidates = [
        row
        for row in _filing_rows(submissions)
        if row.get("form") in {"13F-HR", "13F-NT"} and row.get("reportDate")
    ]
    if not candidates:
        raise ValueError("No original Form 13F submission is available")
    candidates.sort(
        key=lambda row: (str(row.get("reportDate")), str(row.get("filingDate"))),
        reverse=True,
    )
    return candidates[0]


def _archive_base(cik: str, accession_number: str) -> str:
    return SEC_ARCHIVES.format(cik=int(cik), accession=accession_number.replace("-", ""))


def _information_table(client: SecClient, cik: str, filing: dict[str, Any]) -> list[dict[str, Any]]:
    base = _archive_base(cik, str(filing["accession_number"]))
    index = client.get_json(base + "/index.json")
    items = index.get("directory", {}).get("item", [])
    candidates = [
        str(item.get("name"))
        for item in items
        if str(item.get("name", "")).lower().endswith(".xml")
    ]
    primary = str(filing.get("primaryDocument") or "")
    if primary and primary not in candidates:
        candidates.append(primary)
    errors: list[str] = []
    for name in candidates:
        url = base + "/" + urllib.parse.quote(name)
        try:
            body = client.get_bytes(url)
            if b"nameOfIssuer" not in body:
                continue
            return parse_information_table(body)
        except (ET.ParseError, ValueError) as exc:
            errors.append(f"{name}: {exc}")
    detail = "; ".join(errors) if errors else "no information-table XML candidate"
    raise ValueError(f"Unable to parse 13F information table ({detail})")


def collect_manager(client: SecClient, manager: dict[str, str]) -> dict[str, Any]:
    submissions = client.get_json(SEC_SUBMISSIONS.format(cik=manager["cik"].zfill(10)))
    newest_submission = latest_13f_submission(submissions)
    filings = _latest_original_13f_filings(submissions)
    latest, prior = filings[0], filings[1]
    current_holdings = _information_table(client, manager["cik"], latest)
    previous_holdings = _information_table(client, manager["cik"], prior)
    accession = str(latest["accession_number"])
    primary = urllib.parse.quote(str(latest.get("primaryDocument") or ""))
    filing = {
        "filer_name": submissions.get("name") or manager["firm"],
        "report_date": latest["reportDate"],
        "filed_at": latest["filingDate"],
        "accession_number": accession,
        "filing_url": _archive_base(manager["cik"], accession) + "/" + primary,
        "previous_report_date": prior["reportDate"],
        "latest_submission_form": newest_submission["form"],
        "latest_submission_report_date": newest_submission["reportDate"],
        "latest_submission_filed_at": newest_submission["filingDate"],
    }
    return build_manager_snapshot(manager, filing, current_holdings, previous_holdings)


def collect_snapshot(client: SecClient) -> dict[str, Any]:
    managers: list[dict[str, Any]] = []
    for manager in MANAGERS:
        print(f"Collecting {manager['investor']} ({manager['firm']})...", file=sys.stderr)
        managers.append(collect_manager(client, manager))
    return {
        "schema_version": "wbq.guru-13f.v1",
        "generated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "source": {
            "name": "SEC EDGAR Form 13F",
            "api_url": "https://www.sec.gov/search-filings/edgar-application-programming-interfaces",
            "form_13f_url": "https://www.sec.gov/data-research/sec-markets-data/form-13f-data-sets",
            "lag_note": "Quarter-end snapshot; managers may file up to 45 days later.",
            "coverage_note": "Reportable U.S. long securities only. Originals are shown; amendments are not merged.",
            "value_note": "Weights use each filing's own values. Absolute values are not displayed because filer unit conventions can be inconsistent.",
        },
        "managers": managers,
    }


def _same_filing_data(existing: dict[str, Any], fresh: dict[str, Any]) -> bool:
    def without_generation(payload: dict[str, Any]) -> dict[str, Any]:
        comparable = dict(payload)
        comparable.pop("generated_at", None)
        return comparable

    return without_generation(existing) == without_generation(fresh)


def write_snapshot(output: Path, payload: dict[str, Any]) -> bool:
    """Atomically write only when filing content changed."""

    if output.is_file():
        try:
            existing = json.loads(output.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            existing = {}
        if _same_filing_data(existing, payload):
            print(f"No filing changes; keeping {output}", file=sys.stderr)
            return False
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(output.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(output)
    print(f"Wrote {output}", file=sys.stderr)
    return True


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT,
        help=f"JSON destination (default: {DEFAULT_OUTPUT})",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    user_agent = os.environ.get("SEC_USER_AGENT", DEFAULT_USER_AGENT).strip()
    payload = collect_snapshot(SecClient(user_agent))
    write_snapshot(args.output, payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
