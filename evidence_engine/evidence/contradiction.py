# evidence_engine/evidence/contradiction.py
"""Deterministic floor on what a judgment is allowed to claim.

Replay proves a score follows from its judgments, but a forger who edits a
judgment *and* the numbers it produces would still replay cleanly. The residual
trust sat on the model's semantic call.

This module removes part of that trust by checking, in code, claims that are
self-contradictory on the face of the cited text. A span saying a licence
expired cannot be strong evidence of holding that licence, whatever the
judgment says. The checks are deliberately lexical and narrow — they cover the
cases the specification names outright (expired credentials, explicit
negation), not general semantics.

The floor only ever makes a verdict weaker. A model may still be conservative;
it may not be more generous than the text allows.
"""
from __future__ import annotations

import re
from typing import Optional

from evidence_engine.models import Relationship, RequirementType

# Requirements where currency is the whole point.
_CREDENTIAL_TYPES = {
    RequirementType.LICENSE,
    RequirementType.CERTIFICATION,
    RequirementType.DEGREE,
}

# The credential is stated as not currently held.
_INVALIDATED_RE = re.compile(
    r"\b(?:expired|lapsed|revoked|suspended|rescinded|surrendered"
    r"|not\s+renewed|non-?renewed"
    r"|no\s+longer\s+(?:active|valid|current|licen[sc]ed|certified)"
    r"|inactive\s+(?:licen[sc]e|status|certification))\b",
    re.I,
)

# The candidate explicitly disclaims the work. "Did not lead regulatory
# submissions" must never count as leadership evidence.
_NEGATED_OWNERSHIP_RE = re.compile(
    r"\b(?:did\s+not|does\s+not|have\s+not|has\s+not|was\s+not|were\s+not"
    r"|never)\s+(?:\w+\s+){0,2}"
    r"(?:led|lead|manage[d]?|author(?:ed)?|own(?:ed)?|design(?:ed)?"
    r"|develop(?:ed)?|perform(?:ed)?|conduct(?:ed)?|administer(?:ed)?"
    r"|hold|held|possess(?:ed)?|complete[d]?|obtain(?:ed)?"
    r"|responsible\s+for|involved\s+in|part\s+of)\b",
    re.I,
)

# An explicit statement of no experience with the subject.
_NO_EXPERIENCE_RE = re.compile(
    r"\b(?:no|zero|without)\s+(?:direct\s+|hands-?on\s+|prior\s+|formal\s+)?"
    r"(?:experience|exposure|involvement|background|training)\b",
    re.I,
)

# Relationships that assert the requirement is demonstrated.
_POSITIVE = {
    Relationship.EXACT_EXPLICIT,
    Relationship.EQUIVALENT_SYNONYM,
    Relationship.ABBREVIATION_EQUIVALENT,
    Relationship.CHILD_SUPPORTS_PARENT,
    Relationship.ADJACENT_TRANSFERABLE,
    Relationship.CONTEXTUAL_IMPLICATION,
}


def deterministic_relationship_floor(
    requirement, span_text: str, relationship: Relationship
) -> tuple[Relationship, Optional[str]]:
    """Cap a claimed relationship at what the cited text can support.

    Returns the enforced relationship and a note when it was changed.
    """
    if relationship not in _POSITIVE:
        return relationship, None

    text = span_text or ""
    requirement_type = getattr(requirement, "type", None)

    if requirement_type in _CREDENTIAL_TYPES and _INVALIDATED_RE.search(text):
        return (Relationship.CONTRADICTORY,
                "the cited text states the credential is not current, which "
                "cannot evidence holding it")

    if _NEGATED_OWNERSHIP_RE.search(text):
        return (Relationship.NONE,
                "the cited text explicitly denies performing this work")

    if _NO_EXPERIENCE_RE.search(text):
        return (Relationship.NONE,
                "the cited text explicitly states an absence of experience")

    return relationship, None


def detect_contradiction(requirement, span_text: str) -> Optional[str]:
    """Reason string when the cited text contradicts the requirement outright.

    Used to cover spans the model's own verdict never reached — a dropped or
    malformed judgment must not be able to hide an expired credential. This is
    the same lexical rule the floor applies, asked directly rather than as a
    cap on a claimed relationship.
    """
    enforced, note = deterministic_relationship_floor(
        requirement, span_text, Relationship.EXACT_EXPLICIT)
    return note if enforced == Relationship.CONTRADICTORY else None
