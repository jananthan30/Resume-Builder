# evidence_engine/scoring/experience.py
"""Relevant-experience durations from month-interval unions (never mention counts)."""
from __future__ import annotations

from evidence_engine.models import (
    ExperienceDurations, Judgment, Relationship, Resume,
)
from evidence_engine.parsing.dates import (
    longest_contiguous_months, merge_intervals, month_index,
    months_within_window, parse_month, total_months,
)

_STRICT = {
    Relationship.EXACT_EXPLICIT, Relationship.EQUIVALENT_SYNONYM,
    Relationship.ABBREVIATION_EQUIVALENT, Relationship.CHILD_SUPPORTS_PARENT,
}


def _exp_interval(exp, now_index: int):
    if not exp.start_date:
        return None
    start = parse_month(exp.start_date)
    if start is None:
        return None
    if exp.is_current or not exp.end_date:
        end_idx = now_index
    else:
        end = parse_month(exp.end_date)
        if end is None:
            return None
        end_idx = month_index(*end)
    return (month_index(*start), end_idx)


def calculate_durations(
    resume: Resume,
    judgments: list[Judgment],
    spans_by_id: dict,
    policy,
    now_index: int,
) -> ExperienceDurations:
    strict_exps, adjacent_exps = set(), set()
    for j in judgments:
        span = spans_by_id.get(j.evidence_span_id)
        if span is None or not span.experience_id:
            continue
        if j.relationship in _STRICT:
            strict_exps.add(span.experience_id)
        elif j.relationship == Relationship.ADJACENT_TRANSFERABLE:
            adjacent_exps.add(span.experience_id)

    def intervals_for(exp_ids):
        out = []
        for exp in resume.experiences:
            if exp.experience_id in exp_ids:
                iv = _exp_interval(exp, now_index)
                if iv:
                    out.append(iv)
        return merge_intervals(out)

    all_iv = merge_intervals(
        [iv for exp in resume.experiences if (iv := _exp_interval(exp, now_index))])
    strict_iv = intervals_for(strict_exps)
    adjacent_iv = intervals_for(adjacent_exps - strict_exps)

    return ExperienceDurations(
        total_career_years=round(total_months(all_iv) / 12, 2),
        strict_relevant_years=round(total_months(strict_iv) / 12, 2),
        adjacent_relevant_years=round(total_months(adjacent_iv) / 12, 2),
        recent_relevant_years=round(
            months_within_window(strict_iv, policy.recent_window_years * 12,
                                 now_index) / 12, 2),
        longest_continuous_relevant_years=round(
            longest_contiguous_months(strict_iv) / 12, 2),
    )
