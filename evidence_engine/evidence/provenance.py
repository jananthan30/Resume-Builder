# evidence_engine/evidence/provenance.py
"""Provenance verification: every displayed excerpt maps back to exact chars.

The product claim is that each point is backed by evidence a human can find in
the source document. That only holds if offsets are re-checkable, so these
functions re-derive every span and citation from the original text rather than
trusting what the pipeline recorded.
"""
from __future__ import annotations

from typing import Iterable, Optional


def verify_requirement_offsets(requirements: Iterable, jd_text: str) -> list[str]:
    """Each requirement's source offsets must reproduce its source_text."""
    errors: list[str] = []
    for req in requirements:
        actual = jd_text[req.source_start_char:req.source_end_char]
        if actual != req.source_text:
            errors.append(
                f"{req.requirement_id}: JD offsets "
                f"[{req.source_start_char}:{req.source_end_char}] yield "
                f"{actual!r}, expected {req.source_text!r}")
    return errors


def verify_span_offsets(spans: Iterable, resume_text: str) -> list[str]:
    """Each evidence span's offsets must reproduce its text."""
    errors: list[str] = []
    for span in spans:
        actual = resume_text[span.start_char:span.end_char]
        if actual != span.text:
            errors.append(
                f"{span.evidence_span_id}: resume offsets "
                f"[{span.start_char}:{span.end_char}] yield {actual!r}, "
                f"expected {span.text!r}")
    return errors


def verify_citations(matches: Iterable, spans: Iterable) -> list[str]:
    """Every cited span id must exist in the resume's evidence spans."""
    known = {s.evidence_span_id for s in spans}
    errors: list[str] = []
    for match in matches:
        for sid in match.evidence_span_ids:
            if sid not in known:
                errors.append(
                    f"{match.requirement_id}: cites unknown span {sid}")
    return errors


def quote_is_grounded(quoted_text: Optional[str], span_text: str) -> bool:
    """A judge quote must be a literal substring of the span it cites."""
    if quoted_text is None:
        return True
    return " ".join(quoted_text.split()) in " ".join(span_text.split())


def verify_match_result(result, resume_text: str, jd_text: str) -> list[str]:
    """Full provenance check over a MatchResult. Empty list means clean."""
    return (
        verify_requirement_offsets(result.requirements, jd_text)
        + verify_span_offsets(result.evidence_spans, resume_text)
        + verify_citations(result.matches, result.evidence_spans)
    )
