# evidence_engine/api.py
"""The single product-facing shape for an evidence match.

Every surface — the agent tool, the HTTP endpoint, the CLI — serialises through
here, so the contract cannot drift between them. The shape follows the spec's
API section: three separate headline outputs, per-requirement evidence with
exact provenance, and explicit warnings.

The headline deliberately reads "strong evidence for 17 of 21 requirements"
rather than a bare percentage. That sentence is defensible; "your ATS score is
73%" is not, because no such universal score exists.
"""
from __future__ import annotations

from evidence_engine.models import (
    EvidenceStatus, Importance, MatchResult,
)
from evidence_engine.questions import build_questions

# Requirements that carry real weight in the headline count. Optional extras
# would inflate the denominator and make a good match look worse than it is.
_IMPORTANT = {Importance.MUST_HAVE, Importance.CORE, Importance.PREFERRED}

_PARTIAL = {EvidenceStatus.MODERATE_EVIDENCE, EvidenceStatus.WEAK_EVIDENCE}


def build_headline(result: MatchResult) -> str:
    """The plain-language sentence that replaces the ATS-score claim."""
    if result.status != "OK":
        return f"No evidence match was produced: {result.error}"

    by_id = {m.requirement_id: m for m in result.matches}
    important = [r for r in result.requirements if r.importance in _IMPORTANT]
    total = len(important)
    strong = sum(1 for r in important
                 if by_id.get(r.requirement_id)
                 and by_id[r.requirement_id].status == EvidenceStatus.STRONG_EVIDENCE)
    partial = sum(1 for r in important
                  if by_id.get(r.requirement_id)
                  and by_id[r.requirement_id].status in _PARTIAL)
    missing = [r for r in important
               if by_id.get(r.requirement_id)
               and by_id[r.requirement_id].status
               == EvidenceStatus.NO_EVIDENCE_FOUND]
    missing_must = [r for r in missing if r.importance == Importance.MUST_HAVE]

    parts = [f"Your resume provides strong evidence for {strong} of {total} "
             f"important requirements."]
    if partial:
        parts.append(f"{partial} {'is' if partial == 1 else 'are'} "
                     "partially supported.")
    if missing_must:
        parts.append(f"{len(missing_must)} must-have "
                     f"{'has' if len(missing_must) == 1 else 'have'} "
                     "no evidence found.")
    # Requirements below must-have still deserve a mention; a silent zero
    # reads as a pass and hides the most actionable gaps.
    other_missing = len(missing) - len(missing_must)
    if other_missing:
        parts.append(f"{other_missing} other "
                     f"{'requirement has' if other_missing == 1 else 'requirements have'}"
                     " no evidence found.")
    if result.eligibility.status.value != "PASS" and result.eligibility.gates:
        parts.append(f"Eligibility is {result.eligibility.status.value}.")
    return " ".join(parts)


def to_api_response(result: MatchResult, include_spans: bool = True) -> dict:
    """Serialise a MatchResult into the public response shape."""
    if result.status != "OK":
        return {
            "match_id": result.match_id,
            "status": "ERROR",
            "error": result.error,
            "headline": build_headline(result),
            "scoring_policy_version": result.scoring_policy_version,
            "warnings": result.warnings,
        }

    spans_by_id = {s.evidence_span_id: s for s in result.evidence_spans}
    reqs_by_id = {r.requirement_id: r for r in result.requirements}
    weights = _normalized_weights(result)
    breakdown = result.score_breakdown

    requirements = []
    for match in result.matches:
        req = reqs_by_id[match.requirement_id]
        evidence = []
        if include_spans:
            for span_id in match.evidence_span_ids:
                span = spans_by_id.get(span_id)
                if span is None:
                    continue
                evidence.append({
                    "evidence_span_id": span.evidence_span_id,
                    "text": span.text,
                    "section": span.section,
                    "experience_id": span.experience_id,
                    "start_date": span.start_date,
                    "end_date": "PRESENT" if span.is_current else span.end_date,
                    "start_char": span.start_char,
                    "end_char": span.end_char,
                })
        requirements.append({
            "requirement_id": req.requirement_id,
            "requirement": req.normalized_text,
            "source_text": req.source_text,
            "type": req.type.value,
            "importance": req.importance.value,
            "hard_gate": req.hard_gate,
            "weight": weights.get(req.requirement_id, 0.0),
            "score": match.score,
            "status": match.status.value,
            "relationship": match.relationship.value if match.relationship else None,
            "evidence": evidence,
            "reason": match.reason,
            "gap": match.gap,
        })

    return {
        "match_id": result.match_id,
        "status": "OK",
        "headline": build_headline(result),
        "job": {"title": result.job_title, "company": result.job_company,
                "location": result.job_location},
        "qualification_evidence_score": breakdown.qualification_evidence_score,
        "evidence_quality": breakdown.evidence_quality,
        "eligibility": {
            "status": result.eligibility.status.value,
            "gates": [{"requirement_id": g.requirement_id,
                       "requirement": g.requirement,
                       "status": g.status.value,
                       "reason": g.reason} for g in result.eligibility.gates],
        },
        "component_scores": {
            "must_have_evidence": breakdown.must_have_score,
            "core_responsibilities": breakdown.core_score,
            "preferred_qualifications": breakdown.preferred_score,
            "role_similarity": breakdown.role_similarity,
        },
        "relevant_experience_years": {
            "total_career": result.durations.total_career_years,
            "strict_relevant": result.durations.strict_relevant_years,
            "adjacent_relevant": result.durations.adjacent_relevant_years,
            "recent_relevant": result.durations.recent_relevant_years,
            "longest_continuous_relevant":
                result.durations.longest_continuous_relevant_years,
        },
        "requirements": requirements,
        "strong_evidence": result.strong_evidence,
        "partial_evidence": result.partial_evidence,
        "missing_evidence": result.missing_evidence,
        "missing_must_have_ids": breakdown.missing_must_have_ids,
        # The few gaps only the candidate can resolve. Answering authorises a
        # rewrite; it never changes the evidence or the score.
        "open_questions": [
            {"requirement_id": q.requirement_id,
             "requirement": q.requirement,
             "importance": q.importance,
             "weight": q.weight,
             "question": q.question,
             "why_it_matters": q.why_it_matters}
            for q in build_questions(result)],
        "rewrite_recommendations": [
            {"recommendation_id": rec.recommendation_id,
             "requirement_id": rec.requirement_id,
             "type": rec.type.value,
             "priority": rec.priority,
             "supporting_span_ids": rec.supporting_span_ids,
             "requires_truth_confirmation": rec.requires_truth_confirmation,
             "reason": rec.reason,
             "recommendation": rec.recommendation}
            for rec in result.rewrite_recommendations],
        "excluded_requirements": [
            {"requirement_id": e.requirement_id, "source_text": e.source_text,
             "category": e.category, "is_proxy": e.is_proxy,
             "reason": e.reason}
            for e in result.excluded_requirements],
        "parse_quality": result.parse_quality.value,
        "versions": {
            "scoring_policy": result.scoring_policy_version,
            "parser": result.parser_version,
            "prompts": result.prompt_version,
            "extractor_model": result.extractor_model,
            "judge_model": result.judge_model,
            "embedding_model": result.embedding_model,
            "relationships": result.relationships_version,
        },
        "resume_sha256": result.resume_sha256,
        "job_sha256": result.job_sha256,
        "created_at": result.created_at,
        "warnings": result.warnings,
    }


def _normalized_weights(result: MatchResult) -> dict[str, float]:
    """Requirement weights as shares of the scored total, for display."""
    from evidence_engine.policy import load_policy
    policy = load_policy()
    nongates = [r for r in result.requirements if not r.hard_gate]
    raw = {r.requirement_id: policy.importance_weights[r.importance.value]
           for r in nongates}
    total = sum(raw.values())
    if not total:
        return {}
    return {rid: round(w / total, 4) for rid, w in raw.items()}


def summarize(result: MatchResult) -> dict:
    """Compact form for logs, trackers and list views."""
    if result.status != "OK":
        return {"status": "ERROR", "error": result.error,
                "headline": build_headline(result)}
    breakdown = result.score_breakdown
    return {
        "status": "OK",
        "match_id": result.match_id,
        "headline": build_headline(result),
        "qualification_evidence_score": breakdown.qualification_evidence_score,
        "evidence_quality": breakdown.evidence_quality,
        "eligibility": result.eligibility.status.value,
        "must_have_evidence": breakdown.must_have_score,
        "missing_must_have_ids": breakdown.missing_must_have_ids,
        "requirements_total": len(result.requirements),
        "requirements_strong": len(result.strong_evidence),
        "requirements_partial": len(result.partial_evidence),
        "requirements_missing": len(result.missing_evidence),
        "scoring_policy_version": result.scoring_policy_version,
    }
