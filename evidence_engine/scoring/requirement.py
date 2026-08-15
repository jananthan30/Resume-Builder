# evidence_engine/scoring/requirement.py
"""Deterministic requirement scoring. No LLM-generated number reaches here."""
from __future__ import annotations

from typing import Optional

from evidence_engine.models import (
    EvidenceStatus, Judgment, Relationship, Requirement,
)

_ZERO_RELATIONS = {
    Relationship.NONE, Relationship.UNSUPPORTED_INFERENCE, Relationship.CONTRADICTORY,
}


def calculate_span_evidence_score(j: Judgment, policy) -> Optional[float]:
    if j.relationship == Relationship.UNDETERMINED:
        return None
    if j.relationship in _ZERO_RELATIONS:
        return 0.0
    rel_key = j.relationship.value
    w = policy.span_base_weights
    base = (
        w["relationship"] * policy.relationship_values[rel_key]
        + w["specificity"] * j.specificity
        + w["scope"] * j.scope
        + w["context"] * j.context
        + w["temporal"] * j.temporal_relevance
    )
    return min(base, policy.relationship_ceilings[rel_key])


def combine_independent_evidence(scores: list[float], alphas: list[float]) -> float:
    ordered = sorted(scores, reverse=True)[: len(alphas)]
    product = 1.0
    for score, alpha in zip(ordered, alphas):
        product *= 1.0 - alpha * score
    return 1.0 - product


def deduplicate_within_roles(
    scored: list[tuple[Judgment, float]],
    spans_by_id: dict,
    ratio: float,
) -> list[tuple[Judgment, float]]:
    """Collapse restatements of the same claim inside one role.

    Two bullets in the same job that say the same thing are one piece of
    evidence, not two. The same claim at two different employers is genuinely
    independent, so cross-role repetition survives and earns diminishing
    returns instead of being discarded.
    """
    from evidence_engine.retrieval.fusion import text_similarity

    kept: list[tuple[Judgment, float]] = []
    kept_by_role: dict[str, list[str]] = {}
    for judgment, score in sorted(scored, key=lambda t: t[1], reverse=True):
        span = spans_by_id.get(judgment.evidence_span_id)
        # Spans outside a role (skills lists, summary) have no role to dedupe
        # against; retrieval-level dedup already removed exact repeats.
        role = span.experience_id if span is not None else None
        if role is None:
            kept.append((judgment, score))
            continue
        seen = kept_by_role.setdefault(role, [])
        if any(text_similarity(span.text, other) >= ratio for other in seen):
            continue
        seen.append(span.text)
        kept.append((judgment, score))
    return kept


def derive_status(
    score: Optional[float],
    relationships: list[Relationship],
    policy,
) -> EvidenceStatus:
    if Relationship.CONTRADICTORY in relationships:
        return EvidenceStatus.CONTRADICTORY_EVIDENCE
    if score is None:
        # Judgments existed but all UNDETERMINED → UNDETERMINED.
        # No judgments at all (nothing retrieved) → NO_EVIDENCE_FOUND.
        return (EvidenceStatus.UNDETERMINED if relationships
                else EvidenceStatus.NO_EVIDENCE_FOUND)
    t = policy.status_thresholds
    if score >= t["strong"]:
        return EvidenceStatus.STRONG_EVIDENCE
    if score >= t["moderate"]:
        return EvidenceStatus.MODERATE_EVIDENCE
    if score >= t["weak"]:
        return EvidenceStatus.WEAK_EVIDENCE
    return EvidenceStatus.NO_EVIDENCE_FOUND


def _recency_factor(req: Requirement, judgments: list[Judgment], spans_by_id,
                    policy, now_index: int) -> float:
    if req.type.value not in policy.recency["time_sensitive_types"]:
        return 1.0
    from evidence_engine.parsing.dates import month_index, parse_month
    end_indices = []
    for j in judgments:
        span = spans_by_id.get(j.evidence_span_id)
        if span is None:
            continue
        if span.is_current:
            end_indices.append(now_index)
        elif span.end_date:
            parsed = parse_month(span.end_date)
            if parsed:
                end_indices.append(month_index(*parsed))
    if not end_indices:
        return 1.0
    years_since = (now_index - max(end_indices)) / 12.0
    r = policy.recency
    if years_since <= r["full_within_years"]:
        return r["full"]
    if years_since <= r["medium_within_years"]:
        return r["medium"]
    return r["old"]


def contributing_evidence(
    judgments: list[Judgment],
    policy,
    spans_by_id: dict | None = None,
) -> list[tuple[Judgment, float]]:
    """The judgments that actually produce the requirement's score.

    Scoring drops uninterpretable judgments, collapses restatements inside a
    role, and combines only the strongest few. Displayed evidence is drawn from
    this same list so a user never sees a citation that carried no weight —
    "every point is backed by evidence" has to mean the evidence shown is the
    evidence scored.
    """
    scored = [(j, s) for j in judgments
              if (s := calculate_span_evidence_score(j, policy)) is not None]
    if spans_by_id is not None:
        scored = deduplicate_within_roles(
            scored, spans_by_id, policy.same_role_dedup_ratio)
    scored.sort(key=lambda pair: pair[1], reverse=True)
    return [(j, s) for j, s in scored[: len(policy.evidence_combine_alphas)]
            if s > 0]


def calculate_requirement_score(
    req: Requirement,
    judgments: list[Judgment],
    strict_years: float,
    policy,
    now_index: int,
    spans_by_id: dict | None = None,
) -> Optional[float]:
    scored = [(j, s) for j in judgments
              if (s := calculate_span_evidence_score(j, policy)) is not None]
    if not scored:
        return None

    if spans_by_id is not None:
        scored = deduplicate_within_roles(
            scored, spans_by_id, policy.same_role_dedup_ratio)

    score = combine_independent_evidence(
        [s for _, s in scored], policy.evidence_combine_alphas)

    if req.minimum_years:
        score *= min(1.0, strict_years / req.minimum_years)

    if spans_by_id is not None:
        score *= _recency_factor(req, judgments, spans_by_id, policy, now_index)

    return max(0.0, min(1.0, score))
