# evidence_engine/scoring/aggregate.py
"""Overall Qualification Evidence Fit + Evidence Quality (deterministic)."""
from __future__ import annotations

from evidence_engine.models import (
    EligibilityResult, Importance, ParseQuality, Requirement,
    RequirementMatch, ScoreBreakdown,
)


def calculate_overall_score(
    requirements: list[Requirement],
    matches: dict[str, RequirementMatch],
    role_similarity: float,
    durations,
    eligibility: EligibilityResult,
    parse_quality,
    policy,
) -> ScoreBreakdown:
    nongates = [r for r in requirements if not r.hard_gate]
    weights = {r.requirement_id: policy.importance_weights[r.importance.value]
               for r in nongates}

    def safe_score(rid: str) -> float:
        m = matches.get(rid)
        return m.score if (m and m.score is not None) else 0.0

    total_w = sum(weights.values()) or 1.0
    coverage = sum(weights[r.requirement_id] * safe_score(r.requirement_id)
                   for r in nongates) / total_w

    def coverage_for(importance) -> float:
        """Weighted coverage within one importance band."""
        group = [r for r in nongates if r.importance == importance]
        if not group:
            return 0.0
        group_w = sum(weights[r.requirement_id] for r in group)
        return sum(weights[r.requirement_id] * safe_score(r.requirement_id)
                   for r in group) / group_w

    must = [r for r in nongates if r.importance == Importance.MUST_HAVE]
    # No must-haves means nothing to protect, so the blend must not penalise.
    must_coverage = coverage_for(Importance.MUST_HAVE) if must else 1.0
    core_coverage = coverage_for(Importance.CORE)
    preferred_coverage = coverage_for(Importance.PREFERRED)

    blend = policy.must_have_blend
    adjusted = coverage * (blend["coverage"] + blend["must_coverage"] * must_coverage)

    absent = [r.requirement_id for r in must
              if safe_score(r.requirement_id) < policy.must_have_absent_below]
    if len(absent) >= 2:
        adjusted = min(adjusted, policy.must_have_caps["two_or_more_missing"])
    elif len(absent) == 1:
        adjusted = min(adjusted, policy.must_have_caps["one_missing"])

    # Evidence quality: provenance clarity per requirement
    def q(rid: str) -> float:
        m = matches.get(rid)
        if m is None:
            return 0.0
        if m.citations_verified:
            return m.judge_confidence
        return min(m.judge_confidence, policy.unverified_citation_confidence_cap)

    evidence_quality = (sum(weights[r.requirement_id] * q(r.requirement_id)
                            for r in nongates) / total_w) if nongates else 0.0
    if parse_quality == ParseQuality.LOW or str(parse_quality) == "ParseQuality.LOW":
        evidence_quality *= policy.low_parse_quality_evidence_cap

    return ScoreBreakdown(
        eligibility_status=eligibility.status,
        qualification_evidence_score=round(max(0.0, min(1.0, adjusted)), 4),
        evidence_quality=round(max(0.0, min(1.0, evidence_quality)), 4),
        must_have_score=round(must_coverage, 4),
        core_score=round(core_coverage, 4),
        preferred_score=round(preferred_coverage, 4),
        role_similarity=role_similarity,
        missing_must_have_ids=absent,
        unverified_gate_ids=[g.requirement_id for g in eligibility.gates
                             if g.status.value == "UNVERIFIED"],
        failed_gate_ids=[g.requirement_id for g in eligibility.gates
                         if g.status.value == "FAIL"],
        scoring_policy_version=policy.version,
    )
