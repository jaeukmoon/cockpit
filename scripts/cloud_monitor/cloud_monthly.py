"""Generated pure aggregation functions from dashboard/monthly_returns.py.

No database, account, collector or broker dependency is included.
"""
from calendar import monthrange
from datetime import date, timedelta
import math


def _number(value):
    if isinstance(value, bool):
        raise ValueError('Boolean is not a return')
    result = float(value)
    if not math.isfinite(result):
        raise ValueError('Non-finite performance')
    return result

def _points(rows):
    result = {}
    for day, value in rows:
        stamp = date.fromisoformat(day)
        numeric = _number(value)
        if stamp in result or numeric <= 0:
            raise ValueError('Invalid or duplicate NAV')
        result[stamp] = numeric
    if list(result) != sorted(result):
        raise ValueError('NAV must be chronological')
    return result

def _nav_months(rows, benchmark, start, today):
    points = _points(rows)
    if not points:
        return []
    start = date.fromisoformat(start)
    if start > min(points) or max(points) > today:
        raise ValueError('Invalid observation dates')
    try:
        benchmark = _points(benchmark)
    except (ValueError, TypeError):
        benchmark = {}
    ends = {}
    for day in points:
        ends[day.strftime('%Y-%m')] = day
    result = []
    for month, end in ends.items():
        first = date(end.year, end.month, 1)
        previous = first - timedelta(days=1)
        baseline = ends.get(previous.strftime('%Y-%m'))
        initial = start.strftime('%Y-%m') == month
        if initial:
            baseline, base_nav, base_benchmark = start, 100.0, 100.0
        else:
            base_nav = points.get(baseline)
            base_benchmark = benchmark.get(baseline)
        # Never bridge an unobserved month or silently use an early prior mark.
        valid_base = baseline is not None and (initial or (first-baseline).days <= 7)
        value = (points[end]/base_nav-1)*100 if valid_base else None
        bm = (benchmark[end]/base_benchmark-1)*100 if valid_base and base_benchmark and end in benchmark else None
        status = 'IN_PROGRESS' if month == today.strftime('%Y-%m') else 'RECORDED'
        if end == max(points) and end.day < monthrange(end.year, end.month)[1] and status == 'RECORDED':
            status = 'LAST_OBSERVATION'
        if not valid_base:
            status = 'INSUFFICIENT_DATA'
        result.append({'month': month, 'return_pct': value, 'benchmark_return_pct': bm,
                       'excess_pct': value-bm if value is not None and bm is not None else None,
                       'start_date': baseline.isoformat() if baseline else None,
                       'end_date': end.isoformat(), 'observations': sum(d.strftime('%Y-%m') == month for d in points),
                       'partial_start': initial and start.day > 1, 'status': status})
    return result

def build(payload):
    """Allowlisted aggregates only; never serialize holdings or account fields."""
    try:
        today = date.fromisoformat(payload['generated_at'][:10])
    except (ValueError, KeyError, TypeError):
        return {'schema_version': 'wbq.strategy-monthly.v1', 'generated_at': '',
                'months': [], 'series': [], 'state': 'INSUFFICIENT_DATA'}
    series = []
    latest = {}
    for run in payload.get('backtests', []):
        key = run['strategy_id']
        if key not in latest or str(run.get('as_of', '')) > str(latest[key].get('as_of', '')):
            latest[key] = run
    for run in latest.values():
        item = _identity(run['strategy_id'], run.get('title'), 'backtest',
                         'USD' if run['strategy_id'].startswith('us_') else 'KRW',
                         '과거 백테스트', f"원본 비용 가정: {run.get('fee_pct', '미기록')}% · 원본 월별 수익률")
        try:
            if run.get('nav'):
                points = _points(run['nav'])
                base = next(iter(points.values()))
                start = next(iter(points)).isoformat()
                normalized = [[d.isoformat(), v/base*100] for d, v in points.items()]
                benchmark = _points(run.get('benchmark_nav', []))
                bm_base = benchmark.get(date.fromisoformat(start))
                bm_normalized = [[d.isoformat(), v/bm_base*100] for d,v in benchmark.items()] if bm_base else []
                item['months'] = _nav_months(normalized, bm_normalized, start, today)
                source_months = {str(row['date'])[:7] for row in run.get('monthly', [])}
                item['months'] = [row for row in item['months'] if row['month'] in source_months]
                item['state'] = 'OBSERVED' if item['months'] else 'NO_OBSERVATIONS'
                series.append(item)
                continue
            seen = set()
            for row in run.get('monthly', []):
                month = str(row['date'])[:7]
                date.fromisoformat(month+'-01')
                value = _number(row['return_pct'])
                if month in seen or month > today.strftime('%Y-%m') or value < -100:
                    raise ValueError('Invalid monthly record')
                seen.add(month)
                item['months'].append({'month': month, 'return_pct': value,
                    'benchmark_return_pct': None, 'excess_pct': None,
                    'start_date': None, 'end_date': str(row['date']), 'observations': None,
                    'partial_start': False, 'status': 'SOURCE_MONTHLY'})
            item['months'].sort(key=lambda row: row['month'])
            if item['months']:
                item['state'] = 'OBSERVED'
        except (ValueError, KeyError, TypeError):
            item.update(state='INSUFFICIENT_DATA', months=[])
        series.append(item)
    for row in (payload.get('strategy_comparison') or {}).get('strategies', []):
        item = _identity(row['strategy_id'], row.get('name'), 'simulation', 'KRW',
                         '가격 기반 모의', row.get('cost_note', '비용 가정 미기록'))
        _attach_nav(item, row.get('nav', []), row.get('benchmark_nav', []), row.get('start_date'), today)
        series.append(item)
    for item in payload.get('monthly_trackers', []):
        projected = _identity(item['strategy_id'], item.get('name'), 'simulation', item['currency'],
                              '가격 기반 모의', item['cost_note'])
        _attach_nav(projected, item['nav'], item.get('benchmark_nav', []), item.get('start'), today)
        projected['benchmark_name'] = item.get('benchmark_name', '벤치마크')
        series.append(projected)
    labels = {'quant10': '고정 퀀트 10종목', 'quant20': '고정 퀀트 20종목',
              'quant_matched': 'LLM 동일 종목수·현금 대조군', 'llm_selected': 'LLM 선택'}
    for cohort in ((payload.get('research') or {}).get('llm') or {}).get('cohorts', []):
        if cohort.get('state') == 'SUPERSEDED':
            continue
        history = cohort.get('history', [])
        for key, label in labels.items():
            if not any(key in row.get('portfolios', {}) for row in history):
                continue
            item = _identity(cohort['id']+':'+key, label+' · '+str(cohort.get('entry_not_before') or history[0]['date']),
                             'simulation', 'KRW', '고정 바스켓 분석', '원본 가정 수수료·현금 비중 반영 · 브로커 체결 아님')
            try:
                rows = [[r['date'], 100+_number(r['portfolios'][key]['return_pct'])] for r in history]
                benchmark = [[r['date'], 100+_number(r['portfolios']['kodex200']['return_pct'])] for r in history
                             if 'kodex200' in r['portfolios']]
                _attach_nav(item, rows, benchmark, rows[0][0], today)
                for month in item['months']:
                    observation = next(r for r in history if r['date'] == month['end_date'])
                    cash = observation['portfolios'][key].get('cash_pct')
                    month['cash_pct'] = _number(cash) if cash is not None else None
            except (ValueError, TypeError, KeyError):
                item.update(state='INSUFFICIENT_DATA', months=[])
            series.append(item)
    present = {item['strategy_id'] for item in series if item['stage'] == 'simulation'}
    for card in payload.get('strategies', []):
        if card['strategy_id'] not in present:
            series.append(_identity(card['strategy_id'], card.get('name'), 'simulation',
                          'USD' if card['strategy_id'].startswith('us_') else 'KRW',
                          '가격 기반 모의', '순방향 NAV 기록 대기'))
    months = sorted({today.strftime('%Y-%m')} | {m['month'] for s in series for m in s['months']}, reverse=True)
    return {'schema_version': 'wbq.strategy-monthly.v1', 'generated_at': payload['generated_at'],
            'months': months, 'series': series}

def _identity(strategy_id, name, stage, currency, label, cost):
    return {'strategy_id': strategy_id, 'name': name or strategy_id, 'stage': stage,
            'currency': currency, 'label': label, 'cost_note': cost,
            'benchmark_name': 'KODEX200' if currency == 'KRW' else '벤치마크',
            'state': 'NO_OBSERVATIONS', 'months': []}

def _attach_nav(item, rows, benchmark, start, today):
    try:
        item['months'] = _nav_months(rows, benchmark, start, today)
        if item['months']:
            item['state'] = 'OBSERVED'
    except (ValueError, TypeError, KeyError):
        item.update(state='INSUFFICIENT_DATA', months=[])
