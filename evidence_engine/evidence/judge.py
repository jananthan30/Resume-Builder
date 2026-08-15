# evidence_engine/evidence/judge.py
"""Evidence judge with fail-closed citation and relationship verification."""
from __future__ import annotations

from pydantic import BaseModel

from evidence_engine.evidence.prompts import (
    EVIDENCE_JUDGE_SYSTEM, build_user_prompt,
)
from evidence_engine.evidence.contradiction import (
    detect_contradiction, deterministic_relationship_floor,
)
from evidence_engine.evidence.provenance import quote_is_grounded
from evidence_engine.evidence.relationships import (
    RelationMap, verify_relationship,
)
from evidence_engine.llm import LLMClient, LLMError
from evidence_engine.models import (
    EvidenceSpan, EvidenceStatus, Judgment, Relationship, Requirement,
)


class JudgeError(Exception):
    pass


class _RawJudgment(BaseModel):
    requirement_id: str
    evidence_span_id: str
    relationship: Relationship
    status: EvidenceStatus
    specificity: float = 0.0
    scope: float = 0.0
    context: float = 0.0
    temporal_relevance: float = 1.0
    suggested_strength: float | None = None
    confidence: float = 0.0
    quoted_text: str | None = None
    reason: str = ""


def _as_relation_map(source) -> RelationMap:
    return source if isinstance(source, RelationMap) else RelationMap(source)


def judge_requirement(
    requirement: Requirement,
    candidates: list[EvidenceSpan],
    relationships: dict | RelationMap,
    llm: LLMClient,
    review_threshold: float = 0.5,
) -> list[Judgment]:
    if not candidates:
        return []

    rmap = _as_relation_map(relationships)

    try:
        payload = llm.complete_json(
            EVIDENCE_JUDGE_SYSTEM,
            build_user_prompt(requirement, candidates, relationships),
        )
    except LLMError as exc:
        raise JudgeError(f"judge failed for {requirement.requirement_id}: {exc}") from exc

    raw = payload.get("judgments", [])
    if not isinstance(raw, list):
        raise JudgeError("judge payload missing 'judgments' list")

    spans_by_id = {s.evidence_span_id: s for s in candidates}
    verified: list[Judgment] = []
    for item in raw:
        try:
            rj = _RawJudgment(**item)
        except Exception:
            continue  # malformed judgment → drop
        span = spans_by_id.get(rj.evidence_span_id)
        if span is None:
            continue  # cites unknown span → drop
        if not quote_is_grounded(rj.quoted_text, span.text):
            continue  # fabricated quote → drop

        status, relationship = rj.status, rj.relationship
        if rj.confidence < review_threshold:
            status = EvidenceStatus.UNDETERMINED
            relationship = Relationship.UNDETERMINED

        # The curated map, not the model, authorises high-credit relationships.
        relationship, note = verify_relationship(
            relationship, requirement, span.text, rmap)
        # And the cited text itself caps what any relationship may claim.
        relationship, floor_note = deterministic_relationship_floor(
            requirement, span.text, relationship)
        note = floor_note or note
        if relationship == Relationship.CONTRADICTORY and floor_note:
            status = EvidenceStatus.CONTRADICTORY_EVIDENCE
        elif relationship == Relationship.NONE and floor_note:
            status = EvidenceStatus.NO_EVIDENCE_FOUND
        if relationship == Relationship.UNSUPPORTED_INFERENCE and note:
            status = EvidenceStatus.NO_EVIDENCE_FOUND

        verified.append(Judgment(
            requirement_id=requirement.requirement_id,
            evidence_span_id=span.evidence_span_id,
            relationship=relationship,
            status=status,
            specificity=rj.specificity, scope=rj.scope, context=rj.context,
            temporal_relevance=rj.temporal_relevance,
            confidence=rj.confidence, reason=rj.reason,
            suggested_strength=rj.suggested_strength,
            enforcement_note=note,
        ))

    # A judgment can be dropped above for citing an unknown span or quoting
    # text that is not in it. Dropping it silently would discard the span too,
    # and for an expired credential that turns a FAIL into an UNVERIFIED --
    # the exact failure a hard gate exists to prevent. So every retrieved span
    # the model did not successfully judge is still checked deterministically.
    judged = {j.evidence_span_id for j in verified}
    for span in candidates:
        if span.evidence_span_id in judged:
            continue
        contradiction = detect_contradiction(requirement, span.text)
        if contradiction is None:
            continue
        verified.append(Judgment(
            requirement_id=requirement.requirement_id,
            evidence_span_id=span.evidence_span_id,
            relationship=Relationship.CONTRADICTORY,
            status=EvidenceStatus.CONTRADICTORY_EVIDENCE,
            specificity=1.0, scope=1.0, context=1.0, temporal_relevance=1.0,
            confidence=1.0, reason=contradiction,
            enforcement_note="raised by the deterministic contradiction check",
        ))
    return verified
