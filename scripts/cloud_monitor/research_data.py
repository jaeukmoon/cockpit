"""Structured read-only research display projection; no HTML or account data."""
import math

import cloud_monthly


def _number(value):
    return value if type(value) in (int, float) and math.isfinite(value) else None


def _cohort(payload, monthly):
    cohorts = ((payload.get('research') or {}).get('llm') or {}).get('cohorts') or []
    observed = [row for row in cohorts if row.get('state') != 'SUPERSEDED' and row.get('history')]
    if not observed:
        return None
    selected = max(observed, key=lambda row: (str(row.get('entry_not_before') or ''), str(row.get('id') or '')))
    identifier = str(selected.get('id') or '')
    count = sum(candidate.get('selected') is True for candidate in selected.get('candidates') or [])
    weight = _number(selected.get('selection_weight_cap'))
    # Initial budget comes from the immutable cohort, not price-drifted cash marks.
    known_budget = count > 0 and weight is not None and 0 < weight <= 1 and count * weight <= 1
    available = {series['strategy_id'] for series in monthly['series'] if series['stage'] == 'simulation'}
    return {
        'source': f'data/llm_experiments.db · {identifier}',
        'id': identifier, 'state': str(selected.get('state') or 'UNKNOWN'),
        'initial_cash_pct': round((1-count*weight)*100, 10) if known_budget else None,
        'position_count': count if known_budget else None,
        'series_ids': [identifier+':'+key for key in ('quant10','quant20','quant_matched','llm_selected')
                       if identifier+':'+key in available],
    }


def build_research_data(projection, generated_at):
    """Reuse the current WBQ monthly and immutable-budget display contracts."""
    payload = {"generated_at": generated_at, "research": {"llm": projection}}
    monthly = cloud_monthly.build(payload)
    for series in monthly["series"]:
        series["total_months"] = len(series["months"])
    cohort = _cohort(payload, monthly)
    if cohort:
        cohort["source"] = "Cloud closing observations · " + cohort["id"]
    return {
        "schema_version": "wbq.cloud-research.v1",
        "generated_at": generated_at,
        "cohort_ids": [row["id"] for row in projection.get("cohorts", []) if row.get("state") != "SUPERSEDED"],
        "monthly": monthly,
        "cohort": cohort,
    }
