# evidence_engine/requirements/extractor.py
"""Requirement extraction with fail-closed source-span verification."""
from __future__ import annotations

import re

from evidence_engine.llm import LLMClient, LLMError
from evidence_engine.models import Importance, Requirement, RequirementType
from evidence_engine.requirements.prompts import (
    REQUIREMENT_EXTRACTION_SYSTEM, build_user_prompt,
)

_GATE_TYPES = {
    RequirementType.LICENSE, RequirementType.CERTIFICATION,
    RequirementType.WORK_AUTHORIZATION, RequirementType.LOCATION,
    RequirementType.MINIMUM_YEARS, RequirementType.DEGREE,
}
_PREFERRED_RE = re.compile(r"preferr|desired|nice to have|\bplus\b", re.I)
_REQUIRED_RE = re.compile(r"required|must|minimum of|mandatory", re.I)

# "MD or PhD required" is one requirement with two routes, not two gates that
# a PhD holder half-fails. Alternation is the signal that a span must stay
# whole (extraction rule 5) even though rule 4 asks for atomic requirements.
_ALTERNATION_RE = re.compile(r"\bor\b|\beither\b|(?<=\w)/(?=\w)", re.I)

_STRICTNESS = [Importance.HARD_GATE, Importance.MUST_HAVE, Importance.CORE,
               Importance.PREFERRED, Importance.OPTIONAL]


class ExtractionError(Exception):
    pass


_WS_RUN = re.compile(r"\s+")


def locate_source_span(jd_text: str, quoted: str) -> tuple[int, int] | None:
    """Find a quoted requirement in the job text and return its real offsets.

    Offsets are derived here rather than taken from the model. Language models
    do not count characters reliably, so asking one for exact character indices
    fails on almost every real posting. What a model *is* reliable at is
    quoting, and a quote is the part that actually matters: it must appear in
    the posting verbatim or the requirement was invented.

    Falls back to a whitespace-tolerant search, because models routinely
    normalise runs of spaces and line breaks when echoing a line back.
    """
    if not quoted:
        return None
    index = jd_text.find(quoted)
    if index != -1:
        return index, index + len(quoted)

    # Whitespace-tolerant: match on collapsed text, then map back to the
    # original offsets so the recorded span still cuts real characters.
    target = _WS_RUN.sub(" ", quoted).strip()
    if not target:
        return None
    collapsed_chars: list[str] = []
    positions: list[int] = []
    previous_was_space = False
    for position, char in enumerate(jd_text):
        if char.isspace():
            if not previous_was_space:
                collapsed_chars.append(" ")
                positions.append(position)
            previous_was_space = True
        else:
            collapsed_chars.append(char)
            positions.append(position)
            previous_was_space = False
    collapsed = "".join(collapsed_chars)
    found = collapsed.find(target)
    if found == -1:
        return None
    start = positions[found]
    end_index = found + len(target) - 1
    return start, positions[end_index] + 1


def merge_alternative_requirements(
    reqs: list[Requirement], jd_text: str
) -> list[Requirement]:
    """Rejoin requirements the model split across one alternation.

    Requirements whose source spans overlap came from the same sentence. When
    that shared text offers alternatives, satisfying any one of them satisfies
    the requirement, so they must be scored as a single criterion.
    """
    ordered = sorted(reqs, key=lambda r: (r.source_start_char, r.source_end_char))
    groups: list[list[Requirement]] = []
    group_end = -1
    for req in ordered:
        if groups and req.source_start_char < group_end:
            groups[-1].append(req)
            group_end = max(group_end, req.source_end_char)
        else:
            groups.append([req])
            group_end = req.source_end_char

    merged: list[Requirement] = []
    for group in groups:
        if len(group) == 1:
            merged.append(group[0])
            continue
        start = min(r.source_start_char for r in group)
        end = max(r.source_end_char for r in group)
        span = jd_text[start:end]
        if not _ALTERNATION_RE.search(span):
            merged.extend(group)
            continue

        seen: list[str] = []
        for r in group:
            if r.normalized_text not in seen:
                seen.append(r.normalized_text)
        concepts: list[str] = []
        for r in group:
            for c in r.concepts:
                if c not in concepts:
                    concepts.append(c)
        head = group[0]
        merged.append(head.model_copy(update={
            "source_text": span,
            "source_start_char": start,
            "source_end_char": end,
            "normalized_text": " or ".join(seen),
            "importance": min(
                (r.importance for r in group), key=_STRICTNESS.index),
            "hard_gate": any(r.hard_gate for r in group),
            "concepts": concepts,
            "minimum_years": max(
                (r.minimum_years for r in group if r.minimum_years is not None),
                default=None),
            "extraction_confidence": min(r.extraction_confidence for r in group),
        }))
    return merged


def repair_narrowed_alternatives(req: Requirement) -> Requirement:
    """Restore alternatives a normalization dropped from a gate.

    A gate whose source offers routes but whose normalized text names only one
    would fail a candidate holding a different accepted qualification.
    """
    if not req.hard_gate:
        return req
    if not _ALTERNATION_RE.search(req.source_text):
        return req
    if _ALTERNATION_RE.search(req.normalized_text):
        return req
    return req.model_copy(update={
        "normalized_text": " ".join(req.source_text.split()).strip(" .;"),
    })


def _anchor_to_source(req: Requirement, jd_text: str) -> Requirement:
    """Replace model-supplied offsets with the real location of its quote."""
    located = locate_source_span(jd_text, req.source_text)
    if located is None:
        raise ExtractionError(
            f"{req.requirement_id}: quoted requirement does not appear in the "
            "job description")
    start, end = located
    return req.model_copy(update={
        "source_start_char": start,
        "source_end_char": end,
        # Carry the exact characters, so the recorded span and the recorded
        # text can never disagree downstream.
        "source_text": jd_text[start:end],
    })


def repair_classification(req: Requirement) -> Requirement:
    """Bring a requirement's importance back in line with its source wording.

    These rules exist to stop a model inflating importance, and repairing to
    the conservative value serves that better than failing the whole
    extraction: one mis-labelled line should not kill a run. The posting's own
    words are the authority, not the model's label.

    A requirement whose type is not gate-eligible loses its gate status rather
    than blocking the candidate. Only licences, certifications, degrees,
    work authorization, location and stated minimums may ever refuse someone.
    """
    updates: dict = {}
    importance = req.importance

    # Preferred wins when both appear ("preferred but not required").
    if _PREFERRED_RE.search(req.source_text):
        if importance not in (Importance.PREFERRED, Importance.OPTIONAL):
            importance = Importance.PREFERRED
    elif _REQUIRED_RE.search(req.source_text) and importance in (
            Importance.PREFERRED, Importance.OPTIONAL):
        importance = Importance.MUST_HAVE

    hard_gate = req.hard_gate
    if hard_gate and req.type not in _GATE_TYPES:
        # Not a gate-eligible subject: keep it important, but it may not
        # decide eligibility.
        hard_gate = False
        if importance == Importance.HARD_GATE:
            importance = Importance.MUST_HAVE
    elif hard_gate and importance != Importance.HARD_GATE:
        importance = Importance.HARD_GATE
    elif importance == Importance.HARD_GATE and not hard_gate:
        hard_gate = req.type in _GATE_TYPES
        if not hard_gate:
            importance = Importance.MUST_HAVE

    if importance != req.importance:
        updates["importance"] = importance
    if hard_gate != req.hard_gate:
        updates["hard_gate"] = hard_gate
    return req.model_copy(update=updates) if updates else req


def _validate(req: Requirement, jd_text: str) -> None:
    """Invariants that must hold after repair; a breach here is a real bug."""
    if jd_text[req.source_start_char:req.source_end_char] != req.source_text:
        raise ExtractionError(
            f"{req.requirement_id}: source offsets do not match JD text")
    if req.hard_gate and (req.importance != Importance.HARD_GATE
                          or req.type not in _GATE_TYPES):
        raise ExtractionError(
            f"{req.requirement_id}: invalid hard gate ({req.type}/{req.importance})")


def extract_requirements(jd_text: str, llm: LLMClient) -> list[Requirement]:
    try:
        payload = llm.complete_json(REQUIREMENT_EXTRACTION_SYSTEM,
                                    build_user_prompt(jd_text))
    except LLMError as exc:
        raise ExtractionError(f"LLM extraction failed: {exc}") from exc

    raw = payload.get("requirements")
    if not isinstance(raw, list) or not raw:
        raise ExtractionError("LLM returned no requirements")

    try:
        reqs = [Requirement(**item) for item in raw]
    except Exception as exc:
        raise ExtractionError(f"malformed requirement payload: {exc}") from exc

    reqs = [repair_classification(_anchor_to_source(req, jd_text))
            for req in reqs]
    for req in reqs:
        _validate(req, jd_text)

    # Rejoin split alternations before deduping, so "MD or PhD" is one gate.
    reqs = merge_alternative_requirements(reqs, jd_text)
    reqs = [repair_narrowed_alternatives(r) for r in reqs]

    # Merge duplicates by normalized text (keep first), renumber in source order.
    seen: set[str] = set()
    deduped: list[Requirement] = []
    for req in sorted(reqs, key=lambda r: r.source_start_char):
        key = req.normalized_text.strip().lower()
        if key in seen:
            continue
        seen.add(key)
        deduped.append(req)
    for i, req in enumerate(deduped, start=1):
        req.requirement_id = f"R{i}"
    return deduped
