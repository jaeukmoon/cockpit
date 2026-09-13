# Generated verbatim from reviewed WBQ research functions. No broker dependencies.
from datetime import date, datetime, timezone, timedelta
import math
import re
import xml.etree.ElementTree as ET
KST = timezone(timedelta(hours=9))
HISTORY_URL = "https://fchart.stock.naver.com/sise.nhn"

def validate(cohort):
    if not re.fullmatch(r"[a-z0-9_-]+", cohort["id"]):
        raise ValueError("Invalid cohort identifier")
    rows = cohort["candidates"]
    codes = [row["code"] for row in rows]
    selected = [row for row in rows if row["selected"]]
    if len(rows) != 20 or len(set(codes)) != 20 or not all(type(r["selected"]) is bool for r in rows):
        raise ValueError("Expected twenty unique candidates and explicit selection decisions")
    cap = cohort.get("selection_weight_cap", .1)
    if not isinstance(cap, (int, float)) or not math.isfinite(cap) or not 0 < cap <= .1:
        raise ValueError("Selection entry weight cap must be positive and at most ten percent")
    if not all(re.fullmatch(r"[A-Z0-9]{6}", code) for code in codes):
        raise ValueError("Invalid security code")
    if [r["rank"] for r in rows] != list(range(1, 21)):
        raise ValueError("Quant order must be preserved")
    decided = datetime.fromisoformat(cohort["decided_at"])
    if decided.tzinfo is None or date.fromisoformat(cohort["entry_not_before"]) <= decided.astimezone(KST).date():
        raise ValueError("Entry must be strictly after the decision date")
    if cohort["end_date"] <= cohort["entry_not_before"] or cohort["orders_enabled"] is not False:
        raise ValueError("Invalid research contract")
    if cohort["cost"] != .003 or not all(r["reason"] and r["falsifier"] for r in rows):
        raise ValueError("Missing predeclared costs or decisions")
    return cohort

def parse_history(content, code, through):
    chart = ET.fromstring(content.decode("euc-kr")).find("chartdata")
    if chart is None or chart.attrib.get("symbol") != code:
        raise ValueError("Unexpected chart identity")
    result = {}
    for item in chart.iter("item"):
        parts = item.attrib["data"].split("|")
        if len(parts) != 6:
            raise ValueError("Invalid OHLCV")
        day = datetime.strptime(parts[0], "%Y%m%d").date().isoformat()
        value, volume = float(parts[4]), float(parts[5])
        if day > through:
            continue
        if day in result or not math.isfinite(value) or value <= 0 or not math.isfinite(volume) or volume < 0:
            raise ValueError("Duplicate date or invalid price/volume")
        result[day] = {"close": value, "volume": volume}
    if len(result) < 25:
        raise ValueError("Insufficient price history")
    return result

def fetch_history(code, through):
    import requests
    response = requests.get(HISTORY_URL, params={"symbol": code, "timeframe": "day", "count": 300, "requestType": "0"}, timeout=20)
    response.raise_for_status()
    return parse_history(response.content, code, through)

def market_states(index):
    """State at close; portfolio executes the PREVIOUS close's state."""
    days = sorted(index)
    active, above, below = True, 0, 0
    states = {}
    for i, day in enumerate(days):
        if i >= 19:
            average = sum(index[d]["close"] for d in days[i-19:i+1]) / 20
            value = index[day]["close"]
            above = above + 1 if value > average * 1.02 else 0
            below = below + 1 if value <= average * .98 else 0
            if above >= 5:
                active = True
            elif below >= 5:
                active = False
        states[day] = active
    return states

def build_observations(cohort, histories):
    """Replay fixed, one-window analytical accounts from dated closes."""
    validate(cohort)
    codes = [r["code"] for r in cohort["candidates"]]
    if not set(codes + ["069500", "KOSPI"]) <= set(histories):
        raise ValueError("Missing candidate/benchmark history")
    seed = cohort.get("index_seed", {})
    index = {**seed, **{d: v for d, v in histories["KOSPI"].items() if not seed or d > max(seed)}}
    all_days = sorted(index)
    states = market_states(index)
    days = [d for d in all_days if cohort["entry_not_before"] <= d <= cohort["end_date"]]
    if not days:
        return []
    if all_days.index(days[0]) < 24:
        raise ValueError("Insufficient market-filter warmup")
    for code in codes + ["069500"]:
        if any(d not in histories[code] or histories[code][d]["volume"] <= 0 for d in days):
            raise ValueError("Missing/suspended daily price: " + code)
        for before, after in zip(days, days[1:]):
            if abs(histories[code][after]["close"] / histories[code][before]["close"] - 1) > .5:
                raise ValueError("Corporate action/price jump requires audit: " + code)
    chosen = [r["code"] for r in cohort["candidates"] if r["selected"]]
    groups = {"quant10": codes[:10], "quant20": codes, "quant_matched": codes[:len(chosen)],
              "llm_selected": chosen, "kodex200": ["069500"]}
    accounts = {key: {"cash": 1.0, "shares": {}, "peak": 1.0, "mdd": 0.0, "turnover": 0.0} for key in groups}
    base = {code: histories[code][days[0]]["close"] for code in codes + ["069500"]}
    output = []
    for day in days:
        previous = all_days[all_days.index(day)-1]
        portfolios = {}
        for key, members in groups.items():
            a = accounts[key]
            risk_on = key == "kodex200" or states[previous]
            price = {c: histories[c][day]["close"] for c in members}
            if not risk_on and a["shares"]:
                gross = sum(q * price[c] for c, q in a["shares"].items())
                a["cash"] += gross * (1 - cohort["cost"])
                a["turnover"] += gross
                a["shares"] = {}
            elif risk_on and not a["shares"] and members:
                fraction = min(1., len(members) * cohort.get("selection_weight_cap", .1)) if key in {"llm_selected", "quant_matched"} else 1.
                invest = a["cash"] * fraction / (1 + cohort["cost"])
                a["shares"] = {c: invest / len(members) / price[c] for c in members}
                a["cash"] *= 1 - fraction
                a["turnover"] += invest
            nav = a["cash"] + sum(q * price[c] for c, q in a["shares"].items())
            a["peak"] = max(a["peak"], nav)
            a["mdd"] = min(a["mdd"], nav / a["peak"] - 1)
            portfolios[key] = {"return_pct": (nav - 1) * 100, "mdd_pct": a["mdd"] * 100,
                               "cash_pct": a["cash"] / nav * 100, "turnover": a["turnover"], "count": len(members)}
        output.append({"date": day, "entry_date": days[0], "market_signal_on": states[day],
                       "execution_signal_on": states[previous], "portfolios": portfolios,
                       "prices": {c: histories[c][day]["close"] for c in codes + ["069500", "KOSPI"]},
                       "candidate_returns": {c: (histories[c][day]["close"] / base[c] - 1) * 100 for c in codes},
                       "llm_excess_pct": portfolios["llm_selected"]["return_pct"] - portfolios["quant10"]["return_pct"],
                       "llm_matched_excess_pct": portfolios["llm_selected"]["return_pct"] - portfolios["quant_matched"]["return_pct"]})
    return output
