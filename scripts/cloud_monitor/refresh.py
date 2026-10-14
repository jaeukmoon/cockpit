"""Closing-price research monitor. No accounts, broker SDKs or order endpoints.

Inputs, observations and the browser snapshot stay encrypted at rest. This
process owns a separate cloud ledger; it never writes a PC trading database.
"""
from __future__ import annotations

import base64
from copy import deepcopy
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
import json
import hashlib
import math
import os
from pathlib import Path
import secrets
import sys
import time
from zoneinfo import ZoneInfo

import exchange_calendars as calendars
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
import requests

import llm_engine as engine
import research_views as views
from research_data import build_research_data

AAD = b"WBQ closing research v1"
UTC = timezone.utc
US_SYMBOLS = ('VOO', 'SPMO')
US_RETRY_DELAYS = (5, 15)  # Three attempts within the existing job timeout.


class USDataUnavailable(Exception):
    """Only classified transient US availability failures permit delayed output."""
    def __init__(self, symbol, expected, received, reason, attempts, observations=None):
        self.diagnostic = dict(provider='yahoo', symbol=symbol,
            expected_session=expected, received_latest_session=received,
            reason=reason, attempts=attempts)
        self.observations = observations or {}  # Private validation input, never logged.
        super().__init__('US closing data temporarily unavailable')


class USProviderError(ValueError):
    """Invalid data or non-transient HTTP errors remain fatal, with safe context."""
    def __init__(self, symbol, expected, reason):
        self.diagnostic = dict(provider='yahoo', symbol=symbol,
            expected_session=expected, reason=reason)
        super().__init__('US provider response failed validation')


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


def parse_us(payload, symbol, through):
    if not isinstance(payload, dict) or not isinstance(payload.get('chart'), dict):
        raise ValueError('Invalid chart envelope')
    chart = payload['chart']
    if chart.get('error') or not isinstance(chart['result'], list) or len(chart['result']) != 1:
        raise ValueError('Invalid chart result')
    raw = chart['result'][0]
    if raw['meta']['symbol'] != symbol or raw['meta']['exchangeTimezoneName'] != 'America/New_York':
        raise ValueError('Unexpected ETF identity/timezone')
    stamps, closes = raw['timestamp'], raw['indicators']['quote'][0]['close']
    if not isinstance(stamps, list) or not isinstance(closes, list) or len(stamps) != len(closes):
        raise ValueError('Mismatched daily arrays')
    rows = {}
    seen = set()
    calendar = calendars.get_calendar('XNYS')
    for stamp, close in zip(stamps, closes):
        if type(stamp) not in (int, float) or not math.isfinite(stamp) or stamp <= 0:
            raise ValueError('Invalid daily timestamp')
        day = datetime.fromtimestamp(stamp, ZoneInfo('America/New_York')).date().isoformat()
        if day in seen:
            raise ValueError('Duplicate daily date')
        seen.add(day)
        if close is not None and (type(close) not in (int, float) or not math.isfinite(close) or close <= 0):
            raise ValueError('Invalid ETF closing price')
        if day <= through:
            if not calendar.is_session(day):
                raise ValueError('Daily bar is not an exchange session')
            if close is not None:
                rows[day] = float(close)
    if through not in rows:
        raise USDataUnavailable(symbol, through, max(rows, default=None), 'missing_session', 1, rows)
    return rows


def fetch_us(symbol, through):
    if symbol not in US_SYMBOLS:
        raise ValueError('Only declared public ETF benchmarks are permitted')
    target = date.fromisoformat(through)
    if target.isoformat() != through or not calendars.get_calendar('XNYS').is_session(through):
        raise ValueError('Expected US date must be an exchange session')
    # Fixed explicit bounds include the entire expected local day, without a
    # moving range/cache alias or treating a future/intraday candle as its close.
    zone = ZoneInfo('America/New_York')
    start = datetime.combine(target - timedelta(days=40), datetime.min.time(), zone)
    end = datetime.combine(target + timedelta(days=1), datetime.min.time(), zone)
    observed = {}
    for attempt in range(1, len(US_RETRY_DELAYS) + 2):
        try:
            response = requests.get(f'https://query1.finance.yahoo.com/v8/finance/chart/{symbol}',
                params={'period1': int(start.timestamp()), 'period2': int(end.timestamp()), 'interval': '1d'},
                headers={'User-Agent': 'Mozilla/5.0 WBQ-research', 'Cache-Control': 'no-cache'}, timeout=30)
            if response.status_code in (408, 429, 500, 502, 503, 504):
                raise USDataUnavailable(symbol, through, None, f'http_{response.status_code}', attempt)
            try:
                response.raise_for_status()
            except requests.HTTPError:
                raise USProviderError(symbol, through, f'http_{response.status_code}') from None
            try:
                rows = parse_us(response.json(), symbol, through)
            except (ValueError, KeyError, TypeError, IndexError, OverflowError, OSError):
                raise USProviderError(symbol, through, 'invalid_response') from None
            print(json.dumps(dict(status='RECOVERED' if attempt > 1 else 'US_SESSION_VALIDATED',
                provider='yahoo', symbol=symbol, expected_session=through,
                received_latest_session=max(rows), attempts=attempt, rows=len(rows))))
            return rows
        except (requests.Timeout, requests.ConnectionError) as error:
            issue = USDataUnavailable(symbol, through, None, type(error).__name__, attempt)
        except USDataUnavailable as error:
            issue = error
            issue.diagnostic['attempts'] = attempt
        observed.update(issue.observations)
        issue.observations = dict(observed)
        if issue.diagnostic['received_latest_session'] is None:
            issue.diagnostic['received_latest_session'] = max(observed, default=None)
        if attempt > len(US_RETRY_DELAYS):
            raise issue
        print(json.dumps(dict(status='RETRYING', **issue.diagnostic)))
        time.sleep(US_RETRY_DELAYS[attempt - 1])


def advance(state, now, kr_loader=engine.fetch_history, us_loader=fetch_us):
    if state.get("orders_enabled") is not False or state.get("schema") != 1:
        raise ValueError("Read-only research contract required")
    original = state
    state = deepcopy(state)  # Discard every partial attempt on any failure.
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
    us_prices, unavailable = {}, []
    # Check both ETFs: one delayed feed must not hide another feed's invalid
    # prices or revision of a previously recorded observation.
    for symbol in US_SYMBOLS:
        try:
            bars = us_loader(symbol, us_day)
            if not isinstance(bars, dict):
                raise ValueError('Invalid US daily observations')
            for day, value in bars.items():
                if not isinstance(day, str) or date.fromisoformat(day).isoformat() != day or type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
                    raise ValueError('Invalid US daily observation')
            for day, value in state.get('us_observations', {}).get(symbol, {}).items():
                if day in bars and not math.isclose(value, bars[day], rel_tol=1e-8):
                    raise ValueError('ETF history revision requires review')
            if us_day not in bars:
                raise USDataUnavailable(symbol, us_day, max((d for d in bars if d <= us_day), default=None), 'missing_session', 1)
            us_prices[symbol] = bars
        except USDataUnavailable as issue:
            for day, value in state.get('us_observations', {}).get(symbol, {}).items():
                if day in issue.observations and not math.isclose(value, issue.observations[day], rel_tol=1e-8):
                    raise ValueError('ETF history revision requires review')
            unavailable.append(issue)
    if unavailable:
        unavailable[0].diagnostic['issues'] = [dict(issue.diagnostic) for issue in unavailable]
        raise unavailable[0]
    benchmarks = []
    baseline = state.setdefault("us_baselines", {})
    recorded = state.setdefault("us_observations", {})
    for symbol in US_SYMBOLS:
        bars = us_prices[symbol]
        if symbol not in baseline:
            baseline[symbol] = {"date": us_day, "close": bars[us_day]}
        old = recorded.setdefault(symbol, {})
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
    original.clear()
    original.update(state)
    return snapshot


def validate_snapshot(snapshot, state, now):
    """Validate required output fields before any writes or a success receipt."""
    if state.get('schema') != 1 or state.get('orders_enabled') is not False or not isinstance(snapshot, dict) or snapshot.get('schema') != 1 or snapshot.get('orders_enabled') is not False or snapshot.get('data_state') not in ('READY', 'PARTIAL_HELD', 'DELAYED'):
        raise ValueError('Validated research snapshot required')
    generated = datetime.fromisoformat(snapshot['generated_at'])
    if generated.tzinfo is None or generated > now:
        raise ValueError('Invalid research generation time')
    for name in ('kr_as_of', 'us_as_of'):
        if date.fromisoformat(snapshot[name]).isoformat() != snapshot[name]:
            raise ValueError('Invalid research session')
    for name in ('held_cohort_count', 'cohort_count'):
        if type(snapshot[name]) is not int or snapshot[name] < 0:
            raise ValueError('Invalid research count')
    benchmarks = snapshot['benchmarks']
    if not isinstance(benchmarks, list) or len(benchmarks) != 2 or {row['symbol'] for row in benchmarks} != set(US_SYMBOLS) or not isinstance(snapshot['prices'], list) or not isinstance(snapshot['research_data'], dict) or snapshot['research_data'].get('schema_version') != 'wbq.cloud-research.v1' or snapshot['research_data'].get('generated_at') != snapshot['generated_at']:
        raise ValueError('Invalid research projection')
    for row in benchmarks:
        recorded = state['us_observations'][row['symbol']][snapshot['us_as_of']]
        if row['as_of'] != snapshot['us_as_of'] or type(row['close']) not in (int, float) or not math.isfinite(row['close']) or row['close'] <= 0 or row['close'] != recorded:
            raise ValueError('Research benchmark differs from the immutable ledger')
        baseline = state['us_baselines'][row['symbol']]
        if type(baseline['close']) not in (int, float) or not math.isfinite(baseline['close']) or baseline['close'] <= 0 or row['baseline_date'] != baseline['date'] or type(row['return_pct']) not in (int, float) or not math.isfinite(row['return_pct']) or not math.isclose(row['return_pct'], (row['close']/baseline['close']-1)*100, abs_tol=1e-8):
            raise ValueError('Research benchmark differs from its immutable baseline')


def delayed_snapshot(previous, state, issue, now):
    """Preserve validated last-good content, including every observation date."""
    validate_snapshot(previous, state, now)
    if previous['us_as_of'] > issue.diagnostic['expected_session']:
        raise ValueError('Last-good session is ahead of the expected session')
    result = deepcopy(previous)
    result.update(data_state='DELAYED', last_attempt_at=now.isoformat(),
        expected_us_as_of=issue.diagnostic['expected_session'], refresh_delay=issue.diagnostic,
        last_good_data_state=previous.get('last_good_data_state', previous['data_state']))
    return result


def main():
    root = Path(__file__).resolve().parents[2]
    receipt=root/'.refresh-receipt.json'
    receipt.write_text(json.dumps({'status':'IN_PROGRESS'}),encoding='utf-8')
    key = base64.b64decode(os.environ["MONITORING_DATA_KEY"], validate=True)
    if len(key) != 32:
        raise ValueError("Monitoring key must be 256 bits")
    state_path = root / "monitoring/state.enc.json"
    state = decode(json.loads(state_path.read_text()), key)
    original_state = deepcopy(state)
    now = datetime.now(UTC)
    try:
        snapshot = advance(state, now)
    except USDataUnavailable as issue:
        previous = decode(json.loads((root / 'data/monitoring.enc.json').read_text()), key)
        snapshot = delayed_snapshot(previous, original_state, issue, now)
    if snapshot['data_state'] not in ('READY', 'PARTIAL_HELD', 'DELAYED'):
        raise ValueError("Validated research outcome required")
    validate_snapshot(snapshot, original_state if snapshot['data_state'] == 'DELAYED' else state, now)
    owned={'monitoring/state.enc.json':state,'data/monitoring.enc.json':snapshot}
    source_hashes={'scripts/cloud_monitor/'+name:hashlib.sha256((root/'scripts/cloud_monitor'/name).read_bytes()).hexdigest()
                   for name in ('refresh.py','llm_engine.py','research_views.py','research_data.py','cloud_monthly.py')}
    before={name:hashlib.sha256((root/name).read_bytes()).hexdigest() for name in owned}
    # Validate and serialize every output before touching either file. DELAYED
    # leaves the ledger byte-identical; fatal errors never produce a valid receipt.
    encoded = {name: json.dumps(encode(value, key)) for name,value in owned.items()
        if not (snapshot['data_state'] == 'DELAYED' and name == 'monitoring/state.enc.json')}
    for name,content in encoded.items():
        (root/name).write_text(content, encoding='utf-8')
    after={name:hashlib.sha256((root/name).read_bytes()).hexdigest() for name in owned}
    receipt.write_text(json.dumps({'status':snapshot['data_state'],'before':before,'after':after,'source_hashes':source_hashes}),encoding='utf-8')
    result = dict(status=snapshot['data_state'], held_cohort_count=snapshot['held_cohort_count'],
        kr_as_of=snapshot['kr_as_of'], us_as_of=snapshot['us_as_of'], orders_enabled=False)
    if snapshot['data_state'] == 'DELAYED':
        result.update(generated_at=snapshot['generated_at'], diagnostic=snapshot['refresh_delay'])
    print(json.dumps(result))
    # READY/PARTIAL_HELD completed collection; DELAYED explicitly reports an
    # exhausted transient attempt with verified last-good data. Fatal errors
    # still reach the CLI failure handler and cannot publish a valid receipt.
    return 0


def cli():
    try:
        return main()
    except Exception as error:
        # Do not send upstream bodies, cohort contents or secrets into public logs.
        import traceback
        frames=traceback.extract_tb(error.__traceback__)
        location=frames[-1] if frames else None
        result = dict(status='FAILED_CLOSED', error=type(error).__name__,
            stage=location.name if location else 'startup', line=location.lineno if location else None)
        if isinstance(error, USProviderError):
            result['diagnostic'] = error.diagnostic
        print(json.dumps(result), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(cli())
