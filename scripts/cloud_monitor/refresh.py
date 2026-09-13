"""Closing-price research monitor. No accounts, broker SDKs or order endpoints.

Inputs, observations and the browser snapshot stay encrypted at rest. This
process owns a separate cloud ledger; it never writes a PC trading database.
"""
from __future__ import annotations

import base64
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import json
import math
import os
from pathlib import Path
import secrets
import sys

import exchange_calendars as calendars
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
import requests

import llm_engine as engine
import research_views as views

AAD = b"WBQ closing research v1"
UTC = timezone.utc


def encode(value, key):
    nonce = secrets.token_bytes(12)
    raw = json.dumps(value, ensure_ascii=False, allow_nan=False).encode()
    return {"v": 1, "alg": "AES-256-GCM", "nonce": base64.b64encode(nonce).decode(),
            "ct": base64.b64encode(AESGCM(key).encrypt(nonce, raw, AAD)).decode()}


def decode(envelope, key):
    if envelope.get("v") != 1 or envelope.get("alg") != "AES-256-GCM":
        raise ValueError("Invalid encrypted research envelope")
    return json.loads(AESGCM(key).decrypt(base64.b64decode(envelope["nonce"]),
                                         base64.b64decode(envelope["ct"]), AAD))


def closed_session(market, now):
    cal = calendars.get_calendar(market)
    # Buffer prevents consumption of an incomplete or not-yet-final close.
    cutoff = now - timedelta(minutes=15)
    sessions = cal.sessions_in_range((now - timedelta(days=20)).date().isoformat(), now.date().isoformat())
    completed = [d for d in sessions if cal.session_close(d).to_pydatetime() <= cutoff]
    if not completed:
        raise ValueError("No completed exchange session")
    return completed[-1].date().isoformat()


def fetch_us(symbol, through):
    if symbol not in {"VOO", "SPMO"}:
        raise ValueError("Only declared public ETF benchmarks are permitted")
    response = requests.get(f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}",
                            params={"range": "1mo", "interval": "1d"},
                            headers={"User-Agent": "Mozilla/5.0 WBQ-research"}, timeout=30)
    response.raise_for_status()
    raw = response.json()["chart"]["result"][0]
    if raw["meta"]["symbol"] != symbol:
        raise ValueError("Unexpected ETF identity")
    from zoneinfo import ZoneInfo
    rows = {}
    for stamp, close in zip(raw["timestamp"], raw["indicators"]["quote"][0]["close"]):
        day = datetime.fromtimestamp(stamp, ZoneInfo("America/New_York")).date().isoformat()
        if day <= through and close is not None:
            if not math.isfinite(close) or close <= 0 or day in rows:
                raise ValueError("Invalid ETF closing price")
            rows[day] = float(close)
    if through not in rows:
        raise ValueError("Latest completed US session is missing")
    return rows


def advance(state, now, kr_loader=engine.fetch_history, us_loader=fetch_us):
    if state.get("orders_enabled") is not False or state.get("schema") != 1:
        raise ValueError("Read-only research contract required")
    kr_day, us_day = closed_session("XKRX", now), closed_session("XNYS", now)
    codes = sorted({c["code"] for cohort in state["cohorts"] for c in cohort["candidates"]} | {"069500", "KOSPI"})
    with ThreadPoolExecutor(max_workers=4) as pool:
        kr = dict(zip(codes, pool.map(lambda c: kr_loader(c, kr_day), codes)))
    if any(kr_day not in h for h in kr.values()):
        raise ValueError("Latest completed Korean session is missing; snapshot unchanged")
    previous = state.setdefault("observations", {})
    completed = state.setdefault("completed", [])
    cohort_views = []
    for cohort in state["cohorts"]:
        engine.validate(cohort)
        old = previous.get(cohort["id"], [])
        if cohort["id"] in completed:
            if not old:
                raise ValueError("Completed cohort has no observations")
            bars = old
        else:
            calendar = calendars.get_calendar("XKRX")
            end = min(kr_day, cohort["end_date"])
            if end >= cohort["entry_not_before"]:
                required = calendar.sessions_in_range(cohort["entry_not_before"], end)
                if any(d.date().isoformat() not in kr["KOSPI"] for d in required):
                    raise ValueError("Missing index session inside the observation window")
            bars = engine.build_observations(cohort, kr)
        # Completed windows are immutable. A changed provider history cannot
        # silently restate an already published return series.
        if old and old != bars[:len(old)]:
            raise ValueError("Previously recorded research observations changed")
        previous[cohort["id"]] = bars
        status = "REVIEW_DUE" if kr_day >= cohort["end_date"] else "TRACKING" if bars else "WAITING_ENTRY"
        if status == "REVIEW_DUE" and cohort["id"] not in completed:
            completed.append(cohort["id"])
        cohort_views.append({**cohort, "history": bars, "state": status,
                             "last_checked": now.isoformat(), "reviews": state.get("reviews", {}).get(cohort["id"], [])})
    benchmarks = []
    baseline = state.setdefault("us_baselines", {})
    recorded = state.setdefault("us_observations", {})
    for symbol in ("VOO", "SPMO"):
        bars = us_loader(symbol, us_day)
        if us_day not in bars:
            raise ValueError("Missing US closing price")
        if symbol not in baseline:
            baseline[symbol] = {"date": us_day, "close": bars[us_day]}
        old = recorded.setdefault(symbol, {})
        for day, value in old.items():
            if day in bars and not math.isclose(value, bars[day], rel_tol=1e-8):
                raise ValueError("ETF history revision requires review")
        for day, value in bars.items():
            if day >= baseline[symbol]["date"]:
                old[day] = value
        benchmarks.append({"symbol": symbol, "as_of": us_day, "close": bars[us_day],
                           "baseline_date": baseline[symbol]["date"],
                           "return_pct": (bars[us_day] / baseline[symbol]["close"] - 1) * 100})
    state["last_success_at"] = now.isoformat()
    projection = views.project_experiments({"state": "READY", "cohorts": cohort_views})
    return {"schema": 1, "orders_enabled": False, "generated_at": now.isoformat(),
            "kr_as_of": kr_day, "us_as_of": us_day,
            "llm_html": views.llm_page(projection), "benchmarks": benchmarks,
            "prices": [{"code": c, "close": kr[c][kr_day]["close"]} for c in codes],
            "cohort_count": len(cohort_views)}


def main():
    key = base64.b64decode(os.environ["MONITORING_DATA_KEY"], validate=True)
    if len(key) != 32:
        raise ValueError("Monitoring key must be 256 bits")
    root = Path(__file__).resolve().parents[2]
    state_path = root / "monitoring/state.enc.json"
    state = decode(json.loads(state_path.read_text()), key)
    snapshot = advance(state, datetime.now(UTC))
    # No files are touched unless every required source and invariant passes.
    for path, value in ((state_path, state), (root / "data/monitoring.enc.json", snapshot)):
        path.write_text(json.dumps(encode(value, key)), encoding="utf-8")
    print(json.dumps({"status": "success", "kr_as_of": snapshot["kr_as_of"],
                      "us_as_of": snapshot["us_as_of"], "orders_enabled": False}))


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        # Do not send upstream bodies, cohort contents or secrets into public logs.
        print("Closing research refresh failed: " + type(error).__name__, file=sys.stderr)
        raise SystemExit(1)
