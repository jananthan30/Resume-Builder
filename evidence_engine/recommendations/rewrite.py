# evidence_engine/recommendations/rewrite.py
"""Gap analysis + rewrite recommendations. Never recommends fabricating."""
from __future__ import annotations

import re

from evidence_engine.models import (
    EvidenceSpan, EvidenceStatus, Importance, Requirement,
    RequirementMatch, RewriteRecommendation, RewriteType,
)

_GAP_STATUSES = {
    EvidenceStatus.NO_EVIDENCE_FOUND, EvidenceStatus.WEAK_EVIDENCE,
    EvidenceStatus.CONTRADICTORY_EVIDENCE, EvidenceStatus.UNDETERMINED,
}
_NUMBER_RE = re.compile(r"[\d%$]")


def generate_recommendations(
    requirements: list[Requirement],
    matches: dict[str, RequirementMatch],
    spans_by_id: dict[str, EvidenceSpan],
    weak_evidence: dict[str, list[str]],
) -> list[RewriteRecommendation]:
    recs: list[RewriteRecommendation] = []
    gaps = [r for r in requirements
            if matches.get(r.requirement_id)
            and matches[r.requirement_id].status in _GAP_STATUSES]
    order = {Importance.MUST_HAVE: 0, Importance.CORE: 1,
             Importance.PREFERRED: 2, Importance.OPTIONAL: 3,
             Importance.HARD_GATE: 4}
    gaps.sort(key=lambda r: order[r.importance])

    for req in gaps:
        match = matches[req.requirement_id]
        weak_ids = [sid for sid in weak_evidence.get(req.requirement_id, [])
                    if sid in spans_by_id]
        weak_texts = [spans_by_id[sid].text for sid in weak_ids]

        if match.status == EvidenceStatus.CONTRADICTORY_EVIDENCE:
            rtype, priority = RewriteType.TRUE_QUALIFICATION_GAP, "HIGH"
            body = (f"Evidence in the resume conflicts with: {req.normalized_text}. "
                    "Review the contradiction before applying.")
        elif match.status == EvidenceStatus.UNDETERMINED:
            rtype, priority = RewriteType.ADD_MISSING_CONTEXT, "MEDIUM"
            body = (f"The resume evidence for '{req.normalized_text}' could not be "
                    "interpreted reliably. If accurate, add explicit context (role, "
                    "dates, scope) so the work is attributable.")
        elif weak_ids and any(_NUMBER_RE.search(t) for t in weak_texts):
            rtype, priority = RewriteType.QUANTIFY_EXISTING_EVIDENCE, "HIGH"
            body = (f"The job asks for {req.normalized_text}. Your resume has related "
                    "quantified work that is not clearly connected to it. If accurate, "
                    "make the metric and your role explicit.")
        elif weak_ids:
            rtype = RewriteType.CLARIFY_EXISTING_EVIDENCE
            priority = "HIGH" if req.importance == Importance.MUST_HAVE else "MEDIUM"
            body = (f"The job asks for {req.normalized_text}. Your resume describes "
                    "related work but does not establish this requirement. If accurate, "
                    "state that responsibility explicitly.")
        else:
            rtype, priority = RewriteType.SURFACE_HIDDEN_EVIDENCE, "LOW"
            body = (f"No evidence found in the resume for: {req.normalized_text}. "
                    "If you have relevant experience, add it to the appropriate role. "
                    "Otherwise, do not add this claim.")

        recs.append(RewriteRecommendation(
            recommendation_id=f"REC-{len(recs) + 1}",
            requirement_id=req.requirement_id,
            type=rtype, priority=priority,
            supporting_span_ids=weak_ids,
            reason=f"{req.normalized_text}: {match.status.value}",
            recommendation=body,
            requires_truth_confirmation=True,
        ))
    return recs
