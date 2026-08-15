# evidence_engine/scoring/eligibility.py
"""Hard-gate evaluation. Absence is UNVERIFIED, never failure."""
from __future__ import annotations

from evidence_engine.models import (
    EligibilityResult, EligibilityStatus, EvidenceStatus, GateResult,
    Relationship, Requirement, RequirementMatch,
)

_PASS_STATUSES = {EvidenceStatus.STRONG_EVIDENCE, EvidenceStatus.MODERATE_EVIDENCE}

# "A hard requirement needs explicit evidence." An inference -- however
# reasonable -- must never satisfy a gate. A nursing job title strongly
# suggests licensure, but suggesting is not stating, and a gate that accepts
# suggestion would certify candidates nobody actually verified.
_EXPLICIT_RELATIONSHIPS = {
    Relationship.EXACT_EXPLICIT,
    Relationship.EQUIVALENT_SYNONYM,
    Relationship.ABBREVIATION_EQUIVALENT,
}


def apply_hard_constraints(
    requirements: list[Requirement],
    matches: dict[str, RequirementMatch],
    policy,
) -> EligibilityResult:
    gates: list[GateResult] = []
    for req in requirements:
        if not req.hard_gate:
            continue
        match = matches.get(req.requirement_id)
        if match is not None and match.status == EvidenceStatus.CONTRADICTORY_EVIDENCE:
            status = EligibilityStatus.FAIL
            reason = "Explicit contradictory evidence found."
        elif (match is not None and match.status in _PASS_STATUSES
              and match.relationship in _EXPLICIT_RELATIONSHIPS):
            status = EligibilityStatus.PASS
            reason = "Gate supported by explicit evidence."
        elif match is not None and match.status in _PASS_STATUSES:
            status = EligibilityStatus.UNVERIFIED
            reason = ("The resume implies this requirement but does not state "
                      "it explicitly.")
        else:
            status = EligibilityStatus.UNVERIFIED
            reason = "The resume does not establish this requirement."
        gates.append(GateResult(requirement_id=req.requirement_id,
                                requirement=req.normalized_text,
                                status=status, reason=reason))

    if any(g.status == EligibilityStatus.FAIL for g in gates):
        overall = EligibilityStatus.FAIL
    elif not gates or any(g.status == EligibilityStatus.UNVERIFIED for g in gates):
        overall = EligibilityStatus.UNVERIFIED
    else:
        overall = EligibilityStatus.PASS
    return EligibilityResult(status=overall, gates=gates)
