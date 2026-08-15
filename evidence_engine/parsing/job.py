# evidence_engine/parsing/job.py
"""Job-description metadata extraction.

Deliberately deterministic and conservative. Metadata is display and
diagnostic context only — nothing here feeds Qualification Evidence Fit — so a
wrong guess must never be preferable to returning None.
"""
from __future__ import annotations

import re

from pydantic import BaseModel

_NOISE_PREFIX = re.compile(
    r"^(?:job\s+(?:title|posting|description)|position|role|title|req(?:uisition)?"
    r"(?:\s*(?:id|no|number|#))?)\s*[:\-–—]\s*", re.I)

_COMPANY_LINE = re.compile(
    r"^(?:company|employer|organi[sz]ation)\s*[:\-–—]\s*(.+)$", re.I)
_LOCATION_LINE = re.compile(
    r"^(?:location|based in|office|work location)\s*[:\-–—]\s*(.+)$", re.I)
_TITLE_LINE = re.compile(
    r"^(?:job\s+title|position|role|title)\s*[:\-–—]\s*(.+)$", re.I)

# "About Acme" / "Acme is hiring" / "Join Acme as a ..." / "at Acme"
_ABOUT_COMPANY = re.compile(r"^about\s+(?!us\b|the\s+(?:role|team|job)\b)(.+)$", re.I)
_IS_HIRING = re.compile(r"^(.+?)\s+is\s+(?:hiring|seeking|looking for)\b", re.I)

# "San Francisco, CA" / "London, UK" / "Boston, MA" — case matters here, so
# this pattern is deliberately not case-insensitive.
_CITY_REGION = re.compile(
    r"\b[A-Z][A-Za-z.\-']+(?:\s+[A-Z][A-Za-z.\-']+){0,2},\s*"
    r"(?:[A-Z]{2}|[A-Z][a-z]+)\b")

# Work mode is only a location when the line is about the job. "Remote
# monitoring of clinical trial sites" is a responsibility, not an office.
_WORK_MODE = re.compile(r"\b(remote|hybrid|on-?site|in-office)\b", re.I)
_WORK_MODE_CUE = re.compile(
    r"\b(?:role|position|job|work|working|located|location|based|schedule|"
    r"arrangement|setting|office)\b", re.I)
_WORK_MODE_ALONE = re.compile(r"^\(?(remote|hybrid|on-?site|in-office)\)?[.,;]?$",
                              re.I)

_BULLET = re.compile(r"^\s*[-*•‣▪]\s")
_MAX_TITLE_WORDS = 12
_SCAN_LINES = 25


class JobMetadata(BaseModel):
    title: str | None = None
    company: str | None = None
    location: str | None = None


def _clean(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip(" \t|-–—:*#").strip()


def _looks_like_title(line: str) -> bool:
    """A heading-ish line: short, not a sentence, has letters."""
    if not line or len(line.split()) > _MAX_TITLE_WORDS:
        return False
    if line.endswith((".", "!", "?", ":", ";")):
        return False
    return bool(re.search(r"[A-Za-z]", line))


def _location_in(line: str) -> str | None:
    if m := _CITY_REGION.search(line):
        return _clean(m.group(0))
    if m := _WORK_MODE_ALONE.match(line):
        return _clean(m.group(1)).title()
    if _WORK_MODE_CUE.search(line) and (m := _WORK_MODE.search(line)):
        return _clean(m.group(1)).title()
    return None


def parse_job_metadata(jd_text: str) -> JobMetadata:
    """Best-effort title/company/location from the head of a job description."""
    # Bullet status is decided on the raw line; _clean strips the marker.
    lines = [(bool(_BULLET.match(raw)), _clean(raw)) for raw in jd_text.splitlines()]
    head = [(is_bullet, text) for is_bullet, text in lines if text][:_SCAN_LINES]

    title = company = location = None

    # Labelled fields win outright — they are unambiguous.
    for _, line in head:
        if title is None and (m := _TITLE_LINE.match(line)):
            title = _clean(m.group(1))
        if company is None and (m := _COMPANY_LINE.match(line)):
            company = _clean(m.group(1))
        if location is None and (m := _LOCATION_LINE.match(line)):
            location = _clean(m.group(1))

    if title is None:
        for is_bullet, line in head:
            if is_bullet:
                continue
            candidate = _clean(_NOISE_PREFIX.sub("", line))
            if _looks_like_title(candidate):
                title = candidate
                break

    if company is None:
        for _, line in head:
            if m := _ABOUT_COMPANY.match(line):
                company = _clean(m.group(1))
                break
            if m := _IS_HIRING.match(line):
                company = _clean(m.group(1))
                break

    if location is None:
        for _, line in head:
            if found := _location_in(line):
                location = found
                break

    return JobMetadata(title=title or None, company=company or None,
                       location=location or None)
