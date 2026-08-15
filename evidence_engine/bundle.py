# evidence_engine/bundle.py
"""A self-contained, replayable evidence record for authorization.

The candidate-fit gate is authorization evidence, not a trusted input. The
original deterministic gate proved that by re-running itself and demanding a
byte-identical result — a forged report could not survive an independent
recomputation.

An LLM-based gate cannot offer that guarantee: you cannot re-run a model and
get identical bytes. This module restores an equivalent one by moving the
proof from *re-running* to *replaying*:

    a bundle carries the requirements, the evidence spans and the judgments;
    replay recomputes the score from them with no model call at all.

What that forecloses:

* forging a score      — it must fall out of the stored judgments
* forging a requirement— its offsets must resolve verbatim in the job text
* forging evidence     — its offsets must resolve verbatim in the resume
* forging a citation   — every judgment must name a span the bundle carries
* forging a relation   — the curated map is re-applied during replay, so an
                         invented synonym is downgraded exactly as it was
                         during the original run
* forging a judgment   — the deterministic contradiction floor is re-applied,
                         so an expired credential cited as strong evidence
                         collapses back to CONTRADICTORY no matter what the
                         stored judgment claims

What remains trusted is the model's semantic judgement in cases the floor
cannot decide lexically. The floor only ever weakens a claim, never
strengthens one.

Producer and validator both call :func:`replay_bundle`, so a report is
replayable by construction rather than by hope.
"""
from __future__ import annotations

from typing import Any

from evidence_engine.models import (
    EvidenceSpan, EvidenceStatus, Judgment, ParseQuality, Relationship,
    Requirement,
)

BUNDLE_VERSION = "evidence-bundle/v1"


class BundleError(Exception):
    """The bundle is structurally unusable and must fail closed."""


def _round(value: float) -> float:
    """One rounding rule everywhere, so canonical JSON compares equal."""
    return round(float(value), 4)


def build_bundle(result: Any) -> dict:
    """Reduce a MatchResult to exactly what replay needs.

    Deliberately not the whole result: recommendations, warnings and headline
    text are presentation, and including them would let cosmetic changes
    invalidate an authorization.
    """
    if result.status != "OK":
        raise BundleError(f"cannot bundle a failed match: {result.error}")
    return {
        "bundle_version": BUNDLE_VERSION,
        "match_id": result.match_id,
        "evaluated_month_index": int(result.evaluated_month_index),
        "parse_quality": result.parse_quality.value,
        "role_similarity": _round(result.score_breakdown.role_similarity),
        "strict_relevant_years": _round(result.durations.strict_relevant_years),
        "total_career_years": _round(result.durations.total_career_years),
        "scoring_policy_version": result.scoring_policy_version,
        "relationships_version": result.relationships_version,
        "requirements": [
            {
                "requirement_id": r.requirement_id,
                "source_text": r.source_text,
                "source_start_char": r.source_start_char,
                "source_end_char": r.source_end_char,
                "normalized_text": r.normalized_text,
                "type": r.type.value,
                "importance": r.importance.value,
                "hard_gate": bool(r.hard_gate),
                "minimum_years": r.minimum_years,
                "concepts": list(r.concepts),
            }
            for r in result.requirements
        ],
        "evidence_spans": [
            {
                "evidence_span_id": s.evidence_span_id,
                "text": s.text,
                "start_char": s.start_char,
                "end_char": s.end_char,
                "section": s.section,
                "experience_id": s.experience_id,
                "start_date": s.start_date,
                "end_date": s.end_date,
                "is_current": bool(s.is_current),
            }
            for s in result.evidence_spans
            # Only spans a judgment actually cites; the rest are noise that
            # would make the authorization sensitive to unrelated resume edits.
            if s.evidence_span_id in {j.evidence_span_id for j in result.judgments}
        ],
        "judgments": [
            {
                "requirement_id": j.requirement_id,
                "evidence_span_id": j.evidence_span_id,
                "relationship": j.relationship.value,
                "status": j.status.value,
                "specificity": _round(j.specificity),
                "scope": _round(j.scope),
                "context": _round(j.context),
                "temporal_relevance": _round(j.temporal_relevance),
                "confidence": _round(j.confidence),
            }
            for j in result.judgments
        ],
    }


def _rebuild(bundle: dict) -> tuple[list, list, list]:
    """Reconstruct typed objects, failing closed on anything malformed."""
    try:
        requirements = [Requirement(**dict(r, extraction_confidence=1.0))
                        for r in bundle["requirements"]]
        spans = [EvidenceSpan(**s) for s in bundle["evidence_spans"]]
        judgments = [Judgment(**dict(j, reason="")) for j in bundle["judgments"]]
    except Exception as exc:
        raise BundleError(f"malformed bundle: {exc}") from exc
    if not requirements:
        raise BundleError("bundle carries no requirements")
    return requirements, spans, judgments


def verify_bundle_provenance(
    bundle: dict, resume_text: str, job_description_text: str
) -> None:
    """Every offset in the bundle must resolve verbatim in the source texts.

    This is what stops an invented requirement or an invented resume excerpt
    from reaching the scorer.
    """
    from evidence_engine.evidence.provenance import (
        verify_requirement_offsets, verify_span_offsets,
    )

    requirements, spans, judgments = _rebuild(bundle)
    errors = verify_requirement_offsets(requirements, job_description_text)
    errors += verify_span_offsets(spans, resume_text)

    known_spans = {s.evidence_span_id for s in spans}
    known_requirements = {r.requirement_id for r in requirements}
    for judgment in judgments:
        if judgment.evidence_span_id not in known_spans:
            errors.append(
                f"judgment cites unknown span {judgment.evidence_span_id}")
        if judgment.requirement_id not in known_requirements:
            errors.append(
                f"judgment cites unknown requirement {judgment.requirement_id}")
    if errors:
        raise BundleError("; ".join(errors[:5]))


def replay_bundle(
    bundle: dict,
    resume_text: str,
    job_description_text: str,
    policy=None,
    relation_map=None,
) -> dict:
    """Recompute the fit outcome from stored judgments. No model call.

    Returns the derived score, component scores, eligibility and knockouts.
    Raises :class:`BundleError` when the bundle cannot be trusted at all.
    """
    from evidence_engine.evidence.contradiction import (
        deterministic_relationship_floor,
    )
    from evidence_engine.evidence.relationships import (
        load_relation_map, verify_relationship,
    )
    from evidence_engine.models import RequirementMatch
    from evidence_engine.policy import load_policy
    from evidence_engine.scoring.aggregate import calculate_overall_score
    from evidence_engine.scoring.eligibility import apply_hard_constraints
    from evidence_engine.scoring.requirement import (
        calculate_requirement_score, contributing_evidence, derive_status,
    )

    if not isinstance(bundle, dict) or bundle.get("bundle_version") != BUNDLE_VERSION:
        raise BundleError("unrecognised bundle version")
    policy = policy or load_policy()
    if policy.version != bundle.get("scoring_policy_version"):
        raise BundleError(
            f"bundle was scored under {bundle.get('scoring_policy_version')}, "
            f"not {policy.version}")

    verify_bundle_provenance(bundle, resume_text, job_description_text)
    requirements, spans, judgments = _rebuild(bundle)
    relation_map = relation_map or load_relation_map()
    if relation_map.version != bundle.get("relationships_version"):
        raise BundleError("relation map version does not match the bundle")

    spans_by_id = {s.evidence_span_id: s for s in spans}
    requirements_by_id = {r.requirement_id: r for r in requirements}
    by_requirement: dict[str, list[Judgment]] = {}
    for judgment in judgments:
        requirement = requirements_by_id[judgment.requirement_id]
        span = spans_by_id[judgment.evidence_span_id]
        # Re-apply relation enforcement: a relationship the curated map does
        # not support is downgraded now exactly as it was during the run, so a
        # tampered bundle cannot buy credit it never earned.
        enforced, _note = verify_relationship(
            judgment.relationship, requirement, span.text, relation_map)
        # The cited text caps the claim: a span saying a licence expired can
        # never replay as evidence of holding it, whatever the judgment says.
        enforced, _floor = deterministic_relationship_floor(
            requirement, span.text, enforced)
        by_requirement.setdefault(judgment.requirement_id, []).append(
            judgment.model_copy(update={"relationship": enforced}))

    now_index = int(bundle["evaluated_month_index"])
    strict_years = float(bundle["strict_relevant_years"])
    matches: dict[str, RequirementMatch] = {}
    for requirement in requirements:
        requirement_judgments = by_requirement.get(requirement.requirement_id, [])
        score = calculate_requirement_score(
            requirement, requirement_judgments, strict_years, policy,
            now_index, spans_by_id)
        status = derive_status(
            score, [j.relationship for j in requirement_judgments], policy)
        contradicting = [j for j in requirement_judgments
                         if j.relationship == Relationship.CONTRADICTORY]
        if status == EvidenceStatus.CONTRADICTORY_EVIDENCE and contradicting:
            cited = [j.evidence_span_id for j in contradicting]
            confidence = contradicting[0].confidence
        else:
            contributing = contributing_evidence(
                requirement_judgments, policy, spans_by_id)
            cited = [j.evidence_span_id for j, _ in contributing]
            confidence = contributing[0][0].confidence if contributing else 0.0
        matches[requirement.requirement_id] = RequirementMatch(
            requirement_id=requirement.requirement_id,
            status=status,
            score=round(score, 4) if score is not None else None,
            relationship=None,
            evidence_span_ids=cited,
            judge_confidence=confidence,
        )

    eligibility = apply_hard_constraints(requirements, matches, policy)
    breakdown = calculate_overall_score(
        requirements, matches, float(bundle["role_similarity"]), None,
        eligibility, ParseQuality(bundle["parse_quality"]), policy)

    # A stated minimum the candidate's ENTIRE career cannot reach is
    # contradicted by the resume, not merely unevidenced by it. Deliberately
    # measured against total career rather than relevant years: relevant years
    # can read zero because retrieval found nothing, which would fail everyone.
    total_career_years = float(bundle.get("total_career_years") or 0.0)
    duration_shortfalls: dict[str, str] = {}
    for requirement in requirements:
        minimum = requirement.minimum_years
        # Applies to any binding minimum, gate or not. A minimum attached to a
        # merely preferred qualification is a nice-to-have and never blocks.
        if not minimum or requirement.importance.value == "PREFERRED":
            continue
        if total_career_years and total_career_years < float(minimum):
            duration_shortfalls[requirement.requirement_id] = (
                f"{total_career_years:g} years of total career experience "
                f"against a stated minimum of {minimum:g}")

    knockouts = []
    for requirement_id, detail in duration_shortfalls.items():
        requirement = requirements_by_id[requirement_id]
        knockouts.append({
            "code": "MINIMUM_EXPERIENCE_NOT_MET",
            "category": requirement_id,
            "requirement": requirement.normalized_text,
            "candidate_has": detail,
        })
    for gate in eligibility.gates:
        if gate.requirement_id in duration_shortfalls:
            continue
        if gate.status.value != "FAIL":
            continue
        match = matches.get(gate.requirement_id)
        excerpt = ""
        if match is not None:
            for span_id in match.evidence_span_ids:
                span = spans_by_id.get(span_id)
                if span is not None:
                    excerpt = " ".join(span.text.split())
                    break
        knockouts.append({
            "code": "ELIGIBILITY_CONTRADICTED",
            "category": gate.requirement_id,
            "requirement": gate.requirement or gate.requirement_id,
            "candidate_has": excerpt or gate.reason or "contradictory evidence",
        })

    unverified = [
        {
            "requirement": gate.requirement or gate.requirement_id,
            "reason": "No evidence found in the resume. This is not a finding "
                      "that the candidate lacks the qualification.",
        }
        for gate in eligibility.gates if gate.status.value == "UNVERIFIED"
    ]

    # A duration shortfall is an eligibility failure in its own right, so the
    # headline status must reflect it and not just the knockout list.
    eligibility_status = (
        "FAIL" if duration_shortfalls else eligibility.status.value
    )

    return {
        "score": round(breakdown.qualification_evidence_score * 100, 1),
        "component_scores": {
            "must_have_evidence": round(breakdown.must_have_score * 100, 1),
            "core_responsibilities": round(breakdown.core_score * 100, 1),
            "preferred_qualifications": round(breakdown.preferred_score * 100, 1),
            "evidence_quality": round(breakdown.evidence_quality * 100, 1),
            "role_similarity": round(breakdown.role_similarity * 100, 1),
        },
        "eligibility": eligibility_status,
        "hard_knockouts": knockouts,
        "unverified_requirements": unverified,
        "requirement_count": len(requirements),
    }
