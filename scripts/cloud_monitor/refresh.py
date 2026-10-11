"""Closing-price research monitor. No accounts, broker SDKs or order endpoints.

Inputs, observations and the browser snapshot stay encrypted at rest. This
process owns a separate cloud ledger; it never writes a PC trading database.
"""
from __future__ import annotations

import base64
from copy import deepcopy
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import json
import hashlib
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
from research_data import build_research_data

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
            try:
                bars = engine.build_observations(cohort, kr)
            except engine.ObservationUnavailable:
                # Hold this window only. Neither observations nor its completion
                # state may advance; independent market closes can still update.
                cohort_views.append({**cohort,'history':old,'state':'STALE','last_checked':now.isoformat(),
                    'reviews':state.get('reviews',{}).get(cohort['id'],[]),'data_issue':'missing_or_nontrading_bar'})
                continue
            if old and old != bars[:len(old)]:
                # Validate the provider replay first (including missing bars and
                # corporate actions), then replay with our immutable prior marks.
                # Never restate published returns to match a revised feed.
                pinned = deepcopy(kr)
                revisions = []
                for row in old:
                    for code, recorded_close in row['prices'].items():
                        provider = pinned[code][row['date']]
                        if provider['close'] != recorded_close:
                            revisions.append({'cohort_id': cohort['id'], 'date': row['date'],
                                              'code': code, 'recorded_close': recorded_close,
                                              'provider_close': provider['close']})
                            provider['close'] = recorded_close
                bars = engine.build_observations(cohort, pinned)
                if old == bars[:len(old)]:
                    audit = state.setdefault('price_revisions', [])
                    for revision in revisions:
                        if not any(all(item.get(k) == v for k, v in revision.items()) for item in audit):
                            audit.append({**revision, 'observed_at': now.isoformat()})
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
    held=[c for c in cohort_views if c.get('data_issue')]
    state['last_attempt_at']=now.isoformat()
    if not held:state["last_success_at"] = now.isoformat()
    projection = views.project_experiments({"state": "READY", "cohorts": cohort_views})
    snapshot={"schema": 1, "orders_enabled": False, "generated_at": now.isoformat(),
            "kr_as_of": kr_day, "us_as_of": us_day,
            "benchmarks": benchmarks,
            "research_data": build_research_data(projection, now.isoformat()),
            "price_revision_count": len(state.get('price_revisions', [])),
            "data_state":'PARTIAL_HELD' if held else 'READY',
            "held_cohort_count":len(held),
            "prices": [{"code": c, "close": kr[c][kr_day]["close"],
                        'quote_state':'OBSERVED_CLOSE' if kr[c][kr_day]['volume']>0 else 'NO_TRADES'} for c in codes],
            "cohort_count": len(cohort_views)}
    return snapshot


def main():
    root = Path(__file__).resolve().parents[2]
    receipt=root/'.refresh-receipt.json'
    receipt.write_text(json.dumps({'status':'IN_PROGRESS'}),encoding='utf-8')
    key = base64.b64decode(os.environ["MONITORING_DATA_KEY"], validate=True)
    if len(key) != 32:
        raise ValueError("Monitoring key must be 256 bits")
    state_path = root / "monitoring/state.enc.json"
    state = decode(json.loads(state_path.read_text()), key)
    snapshot = advance(state, datetime.now(UTC))
    if snapshot['data_state'] not in ('READY', 'PARTIAL_HELD'):
        raise ValueError("Validated research outcome required")
    owned={'monitoring/state.enc.json':state,'data/monitoring.enc.json':snapshot}
    source_hashes={'scripts/cloud_monitor/'+name:hashlib.sha256((root/'scripts/cloud_monitor'/name).read_bytes()).hexdigest()
                   for name in ('refresh.py','llm_engine.py','research_views.py','research_data.py','cloud_monthly.py')}
    before={name:hashlib.sha256((root/name).read_bytes()).hexdigest() for name in owned}
    # Partial results are explicit HOLD, with the prior window untouched. A fatal
    # invariant error produces no valid receipt and cannot be published.
    for name,value in owned.items():
        path=root/name
        path.write_text(json.dumps(encode(value, key)), encoding="utf-8")
    after={name:hashlib.sha256((root/name).read_bytes()).hexdigest() for name in owned}
    receipt.write_text(json.dumps({'status':snapshot['data_state'],'before':before,'after':after,'source_hashes':source_hashes}),encoding='utf-8')
    print(json.dumps({"status":snapshot['data_state'],"held_cohort_count":snapshot['held_cohort_count'],
        "kr_as_of":snapshot['kr_as_of'],"us_as_of":snapshot['us_as_of'],"orders_enabled":False}))
    # A validated HOLD is a completed refresh: its affected window stays frozen
    # and the receipt permits only independent, validated outputs to publish.
    # Fatal collection errors still reach the CLI failure handler below.
    return 0


def cli():
    try:
        return main()
    except Exception as error:
        # Do not send upstream bodies, cohort contents or secrets into public logs.
        import traceback
        frames=traceback.extract_tb(error.__traceback__)
        location=frames[-1] if frames else None
        print(json.dumps({'status':'FAILED_CLOSED','error':type(error).__name__,
            'stage':location.name if location else 'startup','line':location.lineno if location else None}),file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(cli())
