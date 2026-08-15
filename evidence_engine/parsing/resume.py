# evidence_engine/parsing/resume.py
"""Line-based resume parser producing evidence spans with exact char offsets."""
from __future__ import annotations

import re

from evidence_engine.models import (
    EvidenceSpan, Experience, ParseQuality, Resume,
)
from evidence_engine.parsing.dates import parse_month

_SECTION_ALIASES = {
    "experience": "experience", "work experience": "experience",
    "professional experience": "experience", "employment history": "experience",
    "education": "education", "academic background": "education",
    "skills": "skills", "core competencies": "skills",
    "technical skills": "skills", "key skills": "skills",
    "summary": "summary", "professional summary": "summary",
    "profile": "summary", "objective": "summary",
    "publications": "publications", "selected publications": "publications",
    "certifications": "certifications", "licenses": "certifications",
    "certifications & licenses": "certifications",
    "licenses and certifications": "certifications",
}

_DATE_RANGE_RE = re.compile(
    r"([A-Za-z]{3,9}\s+\d{4}|\d{1,2}/\d{4}|\d{4}-\d{2}|\d{4})"
    r"\s*[–—-]\s*"
    r"([A-Za-z]{3,9}\s+\d{4}|\d{1,2}/\d{4}|\d{4}-\d{2}|\d{4}|Present|Current|present|current)",
)
_BULLET_RE = re.compile(r"^\s*[-*•]\s+")


def _canonical_section(line: str) -> str | None:
    key = re.sub(r"[#*:_]", "", line.strip().lower()).strip()
    return _SECTION_ALIASES.get(key)


def _ymd(parsed: tuple[int, int] | None) -> str | None:
    return f"{parsed[0]:04d}-{parsed[1]:02d}" if parsed else None


def _parse_role_header(line: str):
    """Best-effort (title, company, start, end, is_current) from a role line."""
    dates = _DATE_RANGE_RE.search(line)
    start = end = None
    is_current = False
    text_part = line.strip()
    if dates:
        start_p = parse_month(dates.group(1))
        raw_end = dates.group(2)
        is_current = raw_end.strip().lower() in {"present", "current"}
        end_p = None if is_current else parse_month(raw_end)
        start, end = _ymd(start_p), _ymd(end_p)
        text_part = (line[: dates.start()] + " " + line[dates.end():]).strip(" |–—-,")
    title, company = text_part, ""
    for sep in ("|", " at ", " — ", " – ", ", "):
        if sep in text_part:
            left, right = text_part.split(sep, 1)
            title, company = left.strip(), right.strip()
            break
    return title, company, start, end, is_current


def parse_resume(text: str) -> Resume:
    lines = text.splitlines(keepends=True)
    spans: list[EvidenceSpan] = []
    experiences: list[Experience] = []
    warnings: list[str] = []

    section = "header"
    saw_section = False
    current_exp: Experience | None = None
    offset = 0
    span_n = 0

    def add_span(content: str, line_start: int, sec: str, exp: Experience | None):
        nonlocal span_n
        span_n += 1
        start = text.index(content, line_start)  # exact offset of stripped content
        spans.append(EvidenceSpan(
            evidence_span_id=f"E{span_n}",
            text=content,
            start_char=start,
            end_char=start + len(content),
            section=sec,
            experience_id=exp.experience_id if exp else None,
            start_date=exp.start_date if exp else None,
            end_date=exp.end_date if exp else None,
            is_current=exp.is_current if exp else False,
        ))
        if exp is not None:
            exp.description_span_ids.append(spans[-1].evidence_span_id)

    for raw_line in lines:
        line_start = offset
        offset += len(raw_line)
        stripped = raw_line.strip()
        if not stripped:
            continue

        canonical = _canonical_section(stripped)
        if canonical is not None:
            section = canonical
            saw_section = True
            current_exp = None if section != "experience" else current_exp
            continue

        if section == "experience" and not _BULLET_RE.match(raw_line):
            title, company, start, end, is_current = _parse_role_header(stripped)
            current_exp = Experience(
                experience_id=f"EXP{len(experiences) + 1}",
                title=title, company=company,
                start_date=start, end_date=end, is_current=is_current,
            )
            experiences.append(current_exp)
            add_span(stripped, line_start, section, current_exp)
            continue

        content = _BULLET_RE.sub("", stripped) if _BULLET_RE.match(raw_line) else stripped
        add_span(content, line_start, section, current_exp if section == "experience" else None)

    roles_with_dates = sum(1 for e in experiences if e.start_date)
    if not saw_section:
        quality = ParseQuality.LOW
        warnings.append("PARSE_QUALITY_LOW")
    elif experiences and roles_with_dates == len(experiences):
        quality = ParseQuality.HIGH
    else:
        quality = ParseQuality.MEDIUM

    return Resume(raw_text=text, parse_quality=quality,
                  experiences=experiences, evidence_spans=spans, warnings=warnings)
