# Generated verbatim from reviewed WBQ research functions. No broker dependencies.
import html
import math

def pick(row, fields):
    return {key: row.get(key) for key in fields.split()}

def project_experiments(source):
    result = {"state": source.get("state"), "cohorts": []}
    for row in source.get("cohorts", []):
        cohort = pick(row, "id name state screen_date entry_not_before end_date decided_at last_checked version selection_weight_cap")
        cohort["candidates"] = [pick(c, "code name rank selected reason falsifier selection_reason") for c in row["candidates"]]
        cohort["history"] = []
        for observation in row.get("history", []):
            item = pick(observation, "date llm_excess_pct llm_matched_excess_pct")
            item["portfolios"] = {
                key: pick(value, "return_pct mdd_pct cash_pct count")
                for key, value in observation["portfolios"].items()
                if key in ("quant10", "quant20", "quant_matched", "llm_selected", "kodex200")
            }
            cohort["history"].append(item)
        cohort["reviews"] = [pick(r, "created_at status summary") for r in row.get("reviews", [])]
        result["cohorts"].append(cohort)
    return result

def esc(value):
    return html.escape(str(value if value is not None else "미기록"))

def pct(value, scale=1):
    if value is None or not math.isfinite(float(value)):
        return "미집계"
    return f"{float(value)*scale:+.2f}%"

def llm_page(source):
    result = '<section class="page" id="page-llm" hidden><div class="eyebrow">FORWARD EXPERIMENT</div><h1>LLM 비교 실험</h1><p class="lead">퀀트는 고정하고, LLM이 고른 종목과 제외한 종목을 함께 추적합니다.</p>'
    if source.get("state") == "ERROR":
        return result + '<div role="alert">실험 원장을 읽지 못했습니다. 미집계는 0% 수익률이 아닙니다.</div></section>'
    cohorts = source.get("cohorts", [])
    for cohort in sorted(cohorts, key=lambda c: c["state"] == "SUPERSEDED"):
        selected = sum(bool(c["selected"]) for c in cohort["candidates"])
        state = {"WAITING_ENTRY":"첫 기준 종가 대기", "STALE":"가격 갱신 확인 필요", "ERROR":"데이터 오류 · 집계 보류", "SUPERSEDED":"시작 전 대체됨 · 보존", "TRACKING":"추적 중", "REVIEW_DUE":"월간 복기 대기"}.get(cohort["state"], cohort["state"])
        result += f'<article class="llm-cohort"><h2>{esc(cohort["entry_not_before"])} ~ {esc(cohort["end_date"])} · LLM {selected}종목</h2><span class="stage-badge">{esc(state)}</span><p>선정 기준 {esc(cohort["screen_date"])} · 마지막 확인 {esc(cohort["last_checked"])}</p><div class="notice">진입 시 종목당 최대 10% · LLM 목표 현금 {max(0,100-selected*100*(cohort.get("selection_weight_cap") or .1)):.0f}%. 동일 종목 수·현금 비중의 퀀트 비교군을 따로 둡니다. 가상 비용 편도 0.3% · 배당/세금 제외 · 실제 주문 없음.</div>'
        series = [("quant10","퀀트 상위 10"),("llm_selected",f"LLM 선정 {selected}"),("quant_matched",f"퀀트 {selected} · 동일 현금"),("quant20","퀀트 상위 20"),("kodex200","KODEX200")]
        history = cohort["history"]
        latest = history[-1] if history else {}
        result += '<div class="research-grid llm-metrics">' + ''.join(f'<div class="metric"><span>{label}</span><strong>{pct(latest.get("portfolios",{}).get(key,{}).get("return_pct"))}</strong></div>' for key,label in series) + '</div>'
        result += comparison_svg(history, series)
        result += '<details><summary>전체 후보 · 선정/제외 근거</summary><div class="candidate-grid">'
        for c in cohort["candidates"]:
            result += f'<article class="candidate"><h3>{esc(c["rank"])}. {esc(c["name"])} <small>{esc(c["code"])}</small></h3><span class="stage-badge">{"LLM 선정" if c["selected"] else "LLM 제외"}</span><p>{esc(c["reason"])}</p><p>{esc(c["selection_reason"])}</p><small>다음 확인: {esc(c["falsifier"])}</small></article>'
        result += '</div></details><details><summary>복기와 판단 개선 기록</summary>'
        result += ''.join(f'<p>{esc(r["created_at"])} · {esc(r["status"])}<br>{esc(r["summary"])}</p>' for r in cohort["reviews"]) or '<p>아직 종료된 관측기간이 없습니다. 한 달 결과로 LLM 우월성을 확정하지 않습니다.</p>'
        result += '<p>퀀트·매매 규칙은 고정합니다. 프롬프트 개선안은 별도 후보로 기록하고 승인 전 자동 적용하지 않습니다.</p></details></article>'
    if not cohorts:
        result += '<div class="empty-state">등록된 실험이 없습니다.</div>'
    return result + '</section>'

def comparison_svg(history, series):
    if not history:
        return '<div class="empty-state">미집계 · 첫 기준가격 이후 누적 그래프가 시작됩니다. 과거 수익률을 소급해서 만들지 않습니다.</div>'
    values = [float(day["portfolios"][key]["return_pct"]) for day in history for key,_ in series]
    low, high = min(0,min(values))-1, max(0,max(values))+1
    colors = ['#72aaff','#50dfb0','#ffa6d4','#bca5ff','#ffc86e']
    content = '<svg class="research-chart" viewBox="0 0 700 260" role="img" aria-label="LLM 비교 누적 수익률">'
    for v in (low,0,high):
        y = 210-(v-low)/(high-low)*180
        content += f'<line x1="60" x2="660" y1="{y}" y2="{y}" stroke="#344356"/><text x="3" y="{y+4}" fill="#aabbcc">{v:.1f}%</text>'
    for (key,label),color in zip(series,colors):
        points = ' '.join(f'{60+i/max(1,len(history)-1)*600:.2f},{210-(day["portfolios"][key]["return_pct"]-low)/(high-low)*180:.2f}' for i,day in enumerate(history))
        content += f'<polyline points="{points}" fill="none" stroke="{color}" stroke-width="3"><title>{esc(label)}</title></polyline>'
        if len(history) == 1:
            x,y = points.split(',')
            content += f'<circle cx="{x}" cy="{y}" r="3" fill="{color}"/>'
    content += f'<text x="60" y="248" fill="#aabbcc">{esc(history[0]["date"])}</text><text x="660" y="248" text-anchor="end" fill="#aabbcc">{esc(history[-1]["date"])}</text></svg>'
    return content + '<p class="chart-legend">' + ''.join(f'<span style="color:{color}">{esc(label)}</span>' for (_,label),color in zip(series,colors)) + '</p>'
