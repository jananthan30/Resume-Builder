"""Deterministic, fail-closed candidate-fit gate for resume development.

This module intentionally does not calculate ATS or HR scores.  Those scores
describe a document after tailoring; they cannot establish whether the master
resume demonstrates the job's minimum qualifications.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
from dataclasses import dataclass
from datetime import date
from io import BytesIO
from pathlib import Path
from typing import Any, Iterable

# Calibrated 2026-08-06 against 61 real job descriptions the owner had
# actually applied to -- a self-selected set, not random postings.
#
#   15 carried hard knockouts and fail at any bar, correctly.
#   Of the 46 clean-floor jobs: median 68.5, mean 68.3, range 43.5-78.8.
#
# The previous default of 70 sat ABOVE that median, so by construction more
# than half of deliberately-chosen applications were refused -- 41% passed. A
# gate that rejects the typical job its user picked is measuring the wrong
# thing. 65 sits just under the median, admits about two thirds, and still
# refuses the bottom of the distribution, which is where the genuine
# mismatches are.
#
# This is a starting bar, not a verdict: applicants set their own (see
# resolve_threshold), and the authenticity guarantees live downstream in the
# claim validator, the Auditor, and the evidence audit -- not here.
#
# 2026-08-10 (owner decision): default lowered 65 -> 50. On the measured
# distribution above, 50 admits nearly every clean-floor job (range starts
# 43.5) and turns the score bar into a floor against the truly hopeless
# rather than a median filter. The real gate remains the hard knockouts,
# which no score or bar setting can override, plus the downstream evidence
# and voice audits.
CANDIDATE_FIT_THRESHOLD = 50.0

# A user-chosen score bar is allowed inside these bounds. The floor exists so
# "any job at all" cannot be selected: below it the tailoring has nothing
# genuine to work with and the result would embarrass the applicant. The
# ceiling exists so the bar cannot be set somewhere nothing ever qualifies.
#
# Only the SCORE is selectable. Hard knockouts stay non-negotiable at every
# setting -- a missing licence or degree is a fact about the applicant, not a
# risk preference, and no slider should let one through.
CANDIDATE_FIT_THRESHOLD_MIN = 40.0
CANDIDATE_FIT_THRESHOLD_MAX = 95.0


def resolve_threshold(requested: object) -> float:
    """Return the score bar to apply, falling back to the default policy.

    Anything unusable -- None, a bool, a non-number, NaN, out of bounds --
    yields the default rather than raising. A malformed preference is a
    reason to apply the standard bar, not to fail someone's run.
    """
    if requested is None or isinstance(requested, bool):
        return CANDIDATE_FIT_THRESHOLD
    try:
        value = float(requested)
    except (TypeError, ValueError):
        return CANDIDATE_FIT_THRESHOLD
    if value != value:  # NaN
        return CANDIDATE_FIT_THRESHOLD
    if not (CANDIDATE_FIT_THRESHOLD_MIN <= value <= CANDIDATE_FIT_THRESHOLD_MAX):
        return CANDIDATE_FIT_THRESHOLD
    return value
CANDIDATE_FIT_SCHEMA_VERSION = "1.0.0"
CANDIDATE_FIT_POLICY_VERSION = "candidate-fit-policy-v2"
CANDIDATE_FIT_SCORER_VERSION = "deterministic-job-fit-v1"
SEMANTIC_MODE = "disabled"

MAX_RESUME_BYTES = 10 * 1024 * 1024
MAX_JOB_DESCRIPTION_BYTES = 2 * 1024 * 1024
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_UNSAFE_TEXT_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f\u0085\u2028\u2029\u200b\u200c\u200d\u2060\ufeff]")

# Whether a posting states parseable requirements is a question about its
# content, not its heading vocabulary. Employers write "Qualifications",
# "Minimum Qualifications", "Must-Haves", "Ideal Candidate", "Desired",
# "Skills", "What You'll Need" and more; enumerating those names rejected
# half of all real postings as unparseable, which is a formatting artifact
# rather than a fit or safety signal. Requirement-shaped statements are the
# durable signal, so a posting qualifies on either evidence.
_REQUIREMENTS_HEADING_RE = re.compile(
    r"(?im)^\s*(?:your|the|our)?\s*(?:"
    r"(?:basic|minimum|min\.?|required|preferred|core|key|essential|desired|ideal)"
    r"\s*(?:qualifications?|requirements?|experience|candidate"
    r"|skills?(?:\s+(?:and|&)\s+experience)?)?"
    r"|qualifications?(?:\s+(?:and|&)\s+skills?)?"
    r"|(?:job|position|role)?\s*requirements?"
    r"|skills?(?:\s+(?:and|&)\s+experience)?"
    r"|experience\s+required"
    r"|must[-\s]?haves?"
    r"|what\s+you(?:['\u2019]?ll)?\s+(?:need|bring|will\s+bring)"
    r"|what\s+we(?:['\u2019]?re)?\s+looking\s+for"
    r"|who\s+you\s+are"
    r"|about\s+you"
    r")\s*:?\s*$"
)

# A requirement-shaped statement: a degree, a years-of-experience floor, or an
# explicit capability/proficiency claim.
_REQUIREMENT_STATEMENT_RE = re.compile(
    r"(?im)^\s*(?:[-\u2022*\u00b7\u2013\u2014o]|\(?\d+[.)])?\s*(?=.{6,})(?:"
    r".*\b(?:bachelor|master|doctorate|md|do|phd|pharmd|mph|mba|rn|bsn|degree)\b"
    r"|.*\b\d+\s*\+?\s*(?:or\s+more\s+)?years?\b"
    r"|.*\bexperience\s+(?:in|with|working|as|leading|supporting)\b"
    r"|.*\b(?:ability|able)\s+to\b"
    r"|.*\b(?:proficien|familiarity|familiar\s+with|knowledge\s+of|expertise"
    r"|skilled\s+in|competen)\w*\b"
    r"|.*\b(?:is\s+)?(?:required|must\s+have|must\s+possess|preferred)\b"
    r"|.*\b(?:strong|excellent|demonstrated|proven)\b.*\bskills?\b"
    r")"
)

_MIN_REQUIREMENT_STATEMENTS = 3


def _states_parseable_requirements(job_description_text: str) -> bool:
    """True when the posting declares requirements in any recognizable form."""
    if _REQUIREMENTS_HEADING_RE.search(job_description_text):
        return True
    matches = _REQUIREMENT_STATEMENT_RE.findall(job_description_text)
    return len(matches) >= _MIN_REQUIREMENT_STATEMENTS

_COMPONENT_KEYS = (
    "experience_match",
    "skills_match",
    "title_alignment",
    "domain_match",
    "education_match",
    "certification_match",
    "seniority_match",
)

# A specialized minimum is credited only when the canonical role title itself
# establishes that function. A keyword in a skill list or one bullet cannot
# turn an unrelated multi-year job into years of specialized experience.
_GATE_TITLE_PATTERNS: tuple[tuple[str, tuple[str, ...]], ...] = (
    # Clinical-development credit uses a stricter title/evidence helper below;
    # its empty pattern tuple reserves deterministic output ordering here.
    ("clinical_development", ()),
    (
        "pharmacovigilance",
        (
            r"\bpharmacovigilance\b",
            r"\bdrug\s+safety\b",
            r"\bsafety\s+(?:physician|scientist|associate|officer|specialist)\b",
            r"\bpv\s+(?:physician|scientist|associate|officer|specialist)\b",
        ),
    ),
    (
        "monitoring",
        (
            r"\bcra\b",
            r"\bclinical\s+research\s+associate\b",
            r"\b(?:clinical|site)\s+monitor\b",
        ),
    ),
    (
        "clinical_research",
        (
            r"\b(?:clinical\s+)?(?:study\s+)?investigator\b",
            r"\b(?:clinical\s+)?research(?:er|\s+(?:associate|assistant|coordinator|scientist))\b",
            r"\bclinical\s+(?:scientist|trial\s+manager)\b",
            r"\bcrf\s+specialist\b",
        ),
    ),
    (
        "medical_affairs",
        (
            r"\bmedical\s+(?:science\s+liaison|affairs|advisor)\b",
            r"\bmsl\b",
        ),
    ),
    ("oncology", (r"\boncolog",)),
    ("hematology", (r"\bhematolog", r"\bheme[/ -]?onc\b")),
    ("regulatory", (r"\bregulatory\s+(?:affairs|specialist|scientist|manager|director)\b",)),
    ("data_management", (r"\bdata\s+(?:manager|management|coordinator)\b",)),
    ("medical_writing", (r"\bmedical\s+(?:writer|writing)\b",)),
    ("medical_practice", (r"\bphysician\b", r"\bmedical\s+officer\b", r"\bresident\b")),
)

_MONTHS = {
    "jan": 1,
    "january": 1,
    "feb": 2,
    "february": 2,
    "mar": 3,
    "march": 3,
    "apr": 4,
    "april": 4,
    "may": 5,
    "jun": 6,
    "june": 6,
    "jul": 7,
    "july": 7,
    "aug": 8,
    "august": 8,
    "sep": 9,
    "sept": 9,
    "september": 9,
    "oct": 10,
    "october": 10,
    "nov": 11,
    "november": 11,
    "dec": 12,
    "december": 12,
}
_DATE_TOKEN = (
    r"(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|"
    r"Jul(?:y)?|Aug(?:ust)?|Sep(?:t(?:ember)?)?|Oct(?:ober)?|Nov(?:ember)?|"
    r"Dec(?:ember)?)\s+(?:19|20)\d{2}|(?:0?[1-9]|1[0-2])/(?:19|20)\d{2}|"
    r"(?:19|20)\d{2}|Present|Current"
)
_DATE_RANGE_RE = re.compile(
    rf"(?P<start>{_DATE_TOKEN})\s*(?:-|–|—|to)\s*(?P<end>{_DATE_TOKEN})",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class _LoadedArtifact:
    text: str
    digest: str
    trustworthy: bool


@dataclass(frozen=True)
class _GenericLadderExtraction:
    """A narrowly parsed generic degree/total-experience OR ladder."""

    paths: tuple[tuple[str, float], ...]
    remaining_text: str
    detected: bool
    trustworthy: bool


_GENERIC_LADDER_DEGREE_LABEL = (
    r"(?:doctorate\s+degree|doctoral\s+degree|ph\.?d\.?(?:\s+degree)?|"
    r"m\.?d\.?(?:\s+degree)?|pharm\.?d\.?(?:\s+degree)?|"
    r"master(?:['’]s|s['’])?\s+degree|"
    r"bachelor(?:['’]s|s['’])?\s+degree|"
    r"associate(?:['’]s|s['’])?\s+degree|"
    r"high\s+school\s+(?:diploma|degree)(?:\s*/\s*ged)?|ged)"
)
_GENERIC_LADDER_NUMBER_WORDS = {
    "one": 1.0,
    "two": 2.0,
    "three": 3.0,
    "four": 4.0,
    "five": 5.0,
    "six": 6.0,
    "seven": 7.0,
    "eight": 8.0,
    "nine": 9.0,
    "ten": 10.0,
    "eleven": 11.0,
    "twelve": 12.0,
    "thirteen": 13.0,
    "fourteen": 14.0,
    "fifteen": 15.0,
    "sixteen": 16.0,
    "seventeen": 17.0,
    "eighteen": 18.0,
    "nineteen": 19.0,
    "twenty": 20.0,
}
_GENERIC_LADDER_QUANTITY = (
    r"(?:\d+(?:\.\d+)?|"
    + "|".join(_GENERIC_LADDER_NUMBER_WORDS)
    + r")"
)
_GENERIC_LADDER_ROUTE_RE = re.compile(
    rf"^\s*[•*\-]?\s*(?P<degree>{_GENERIC_LADDER_DEGREE_LABEL})\s+"
    rf"(?:and|with|\+)\s+(?P<years>{_GENERIC_LADDER_QUANTITY})\+?\s*"
    rf"(?:years?|yrs?\.?)"
    rf"(?:\s*['’]\s*|\s+of\s+|\s+)experience\s*[.;]?\s*$",
    re.IGNORECASE,
)
_GENERIC_LADDER_ROUTE_LIKE_RE = re.compile(
    rf"^\s*[•*\-]?\s*{_GENERIC_LADDER_DEGREE_LABEL}\s+"
    rf"(?:and|with|\+)\s+[^\n.;]{{1,48}}\s+"
    rf"(?:years?|yrs?\.?)"
    rf"(?:\s*['’]\s*|\s+of\s+|\s+)experience\s*[.;]?\s*$",
    re.IGNORECASE,
)


def _canonical_generic_ladder_degree(value: str) -> str:
    normalized = re.sub(r"[^a-z0-9]+", " ", value.lower()).strip()
    if normalized.startswith(("doctorate", "doctoral")):
        return "doctorate"
    if re.search(r"\bph\s*d\b", normalized):
        return "phd"
    if re.search(r"\bm\s*d\b", normalized):
        return "md"
    if re.search(r"\bpharm\s*d\b", normalized):
        return "pharmd"
    if normalized.startswith("master"):
        return "master"
    if normalized.startswith("bachelor"):
        return "bachelor"
    if normalized.startswith("associate"):
        return "associate"
    if "high school" in normalized or normalized == "ged":
        return "high_school"
    return ""


def _parse_generic_ladder_route(value: str) -> tuple[str, float] | None:
    match = _GENERIC_LADDER_ROUTE_RE.fullmatch(value)
    if not match:
        return None
    degree = _canonical_generic_ladder_degree(match.group("degree"))
    raw_years = match.group("years").lower()
    if raw_years in _GENERIC_LADDER_NUMBER_WORDS:
        years = _GENERIC_LADDER_NUMBER_WORDS[raw_years]
    else:
        try:
            years = float(raw_years)
        except ValueError:
            return None
    if not degree or not 0 < years <= 80:
        return None
    return degree, years


def _extract_generic_degree_experience_ladder(
    required_text: str,
) -> _GenericLadderExtraction:
    """Extract one exact OR ladder whose experience scope is generic total.

    The shared scorer already handles typed ladders such as "three years of
    clinical development experience."  This boundary parser covers the
    distinct employer-boilerplate form "Six Years' Experience," including
    number words.  It accepts one complete alternating ladder only.  A block
    that resembles that form but is incomplete, duplicated, or ambiguous is
    removed and marked untrustworthy so fragments cannot become scalar gates.
    """
    lines = required_text.splitlines()
    removed: set[int] = set()
    candidate_blocks: list[tuple[list[tuple[str, float]], bool]] = []
    inline_indices: set[int] = set()

    for line_index, line in enumerate(lines):
        parts = re.split(r"\s+\bOR\b\s+", line.strip(), flags=re.IGNORECASE)
        if len(parts) < 2:
            continue
        parsed = [_parse_generic_ladder_route(part) for part in parts]
        resembles = any(
            route is not None or _GENERIC_LADDER_ROUTE_LIKE_RE.fullmatch(part)
            for route, part in zip(parsed, parts)
        )
        if not resembles:
            continue
        valid = all(route is not None for route in parsed)
        routes = [route for route in parsed if route is not None]
        candidate_blocks.append((routes, valid))
        removed.add(line_index)
        inline_indices.add(line_index)

    logical = [
        (line_index, line)
        for line_index, line in enumerate(lines)
        if line.strip() and line_index not in inline_indices
    ]
    kinds: list[str] = []
    parsed_routes: list[tuple[str, float] | None] = []
    for _, line in logical:
        route = _parse_generic_ladder_route(line)
        parsed_routes.append(route)
        if route is not None:
            kinds.append("path")
        elif _GENERIC_LADDER_ROUTE_LIKE_RE.fullmatch(line):
            kinds.append("like")
        elif re.fullmatch(r"\s*OR\s*", line, re.IGNORECASE):
            kinds.append("or")
        else:
            kinds.append("other")

    index = 0
    while index < len(logical):
        if kinds[index] == "other":
            index += 1
            continue
        end = index
        while end + 1 < len(logical) and kinds[end + 1] != "other":
            end += 1
        block_kinds = kinds[index : end + 1]
        if "or" in block_kinds and ({"path", "like"} & set(block_kinds)):
            alternating = (
                len(block_kinds) >= 3
                and all(
                    kind == ("path" if offset % 2 == 0 else "or")
                    for offset, kind in enumerate(block_kinds)
                )
            )
            routes = [
                parsed_routes[position]
                for position in range(index, end + 1, 2)
                if parsed_routes[position] is not None
            ]
            candidate_blocks.append((routes, alternating))
            removed.update(
                logical[position][0] for position in range(index, end + 1)
            )
        index = end + 1

    remaining_text = "\n".join(
        line for line_index, line in enumerate(lines) if line_index not in removed
    )
    if not candidate_blocks:
        return _GenericLadderExtraction((), required_text, False, True)

    routes, block_valid = candidate_blocks[0]
    unique_degrees = len({degree for degree, _ in routes}) == len(routes)
    trustworthy = (
        len(candidate_blocks) == 1
        and block_valid
        and len(routes) >= 2
        and unique_degrees
    )
    return _GenericLadderExtraction(
        tuple(routes) if trustworthy else (),
        remaining_text,
        True,
        trustworthy,
    )


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _validate_digest(value: str | None, text: str, label: str) -> str:
    if value is None:
        return _sha256(text.encode("utf-8"))
    if not isinstance(value, str) or not _SHA256_RE.fullmatch(value):
        raise ValueError(f"{label} must be a full lowercase SHA-256 digest")
    return value


def _parse_as_of_date(value: str | date) -> date:
    if type(value) is date:
        parsed = value
    else:
        if not isinstance(value, str):
            raise ValueError("as_of_date must be an ISO date")
        try:
            parsed = date.fromisoformat(value)
        except ValueError as exc:
            raise ValueError("as_of_date must be YYYY-MM-DD") from exc
        if parsed.isoformat() != value:
            raise ValueError("as_of_date must be canonical YYYY-MM-DD")
    if parsed > date.today():
        raise ValueError("as_of_date must not be in the future")
    return parsed


def _validate_identity(value: str, label: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 256:
        raise ValueError(f"{label} must be a non-empty string of at most 256 characters")
    if _UNSAFE_TEXT_RE.search(value) or "\r" in value or "\n" in value:
        raise ValueError(f"{label} contains unsafe characters")
    return value


def _read_regular_bytes(path: Path, maximum: int) -> bytes:
    path = path.expanduser()
    before = os.lstat(path)
    if stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode):
        raise ValueError(f"input is not a regular non-symlink file: {path}")
    if before.st_size > maximum:
        raise ValueError(f"input exceeds {maximum} bytes: {path}")

    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino)
            or opened.st_size > maximum
        ):
            raise ValueError(f"input changed or is invalid: {path}")
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(descriptor, min(1024 * 1024, maximum + 1 - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > maximum:
                raise ValueError(f"input exceeds {maximum} bytes: {path}")
        after = os.fstat(descriptor)
        if (
            (after.st_dev, after.st_ino) != (opened.st_dev, opened.st_ino)
            or after.st_size != opened.st_size
            or after.st_mtime_ns != opened.st_mtime_ns
        ):
            raise ValueError(f"input changed while being read: {path}")
        return b"".join(chunks)
    finally:
        os.close(descriptor)


def _extract_docx(data: bytes) -> str:
    from docx import Document

    document = Document(BytesIO(data))
    lines: list[str] = []
    for paragraph in document.paragraphs:
        style_name = getattr(getattr(paragraph, "style", None), "name", "") or ""
        prefix = ""
        if "List Bullet" in style_name:
            prefix = "• "
        elif "List Number" in style_name:
            prefix = "1. "
        text = paragraph.text
        runs = list(paragraph.runs)
        if runs and runs[0].bold:
            split_at = 0
            while split_at < len(runs) and runs[split_at].bold:
                split_at += 1
            if split_at < len(runs):
                left = "".join(run.text for run in runs[:split_at]).rstrip()
                right = "".join(run.text for run in runs[split_at:]).lstrip(" \t,|")
                if left:
                    text = f"{left} | {right}" if right else left
        lines.append(prefix + text)
    return "\n".join(lines)


def _load_artifact(path_value: str | Path, *, resume: bool) -> _LoadedArtifact:
    path = Path(path_value)
    suffix = path.suffix.lower()
    if suffix not in {".md", ".txt", ".docx"}:
        raise ValueError(f"unsupported input format: {suffix or '<none>'}")
    data = _read_regular_bytes(
        path, MAX_RESUME_BYTES if resume else MAX_JOB_DESCRIPTION_BYTES
    )
    try:
        text = _extract_docx(data) if suffix == ".docx" else data.decode("utf-8")
    except Exception as exc:
        raise ValueError(f"could not extract {path}") from exc
    trustworthy = bool(text.strip()) and not _UNSAFE_TEXT_RE.search(text) and "\r" not in text
    digest_payload = text.encode("utf-8") if suffix == ".docx" else data
    return _LoadedArtifact(
        text=text,
        digest=_sha256(digest_payload),
        trustworthy=trustworthy,
    )


def _text_structure_is_trustworthy(
    resume_text: str,
    job_description_text: str,
    profile: Any,
    requirements: Any,
) -> bool:
    if (
        len(resume_text.strip()) < 200
        or len(job_description_text.strip()) < 150
        or _UNSAFE_TEXT_RE.search(resume_text)
        or _UNSAFE_TEXT_RE.search(job_description_text)
        or "\r" in resume_text
        or "\r" in job_description_text
    ):
        return False
    if not re.search(r"(?im)^\s*(?:professional\s+)?experience\s*$", resume_text):
        return False
    if not re.search(r"(?im)^\s*education\s*$", resume_text):
        return False
    if not _states_parseable_requirements(job_description_text):
        return False
    if not getattr(getattr(profile, "base", None), "jobs", None):
        return False
    if not getattr(getattr(profile, "base", None), "education", None):
        return False
    if getattr(requirements, "extraction_trustworthy", True) is not True:
        return False
    # An unextractable title is not a parsing failure. Title alignment is 15%
    # of the score and already falls back to a neutral 50 when absent, so a
    # posting whose title sits in page furniture must not fail closed.
    title = getattr(requirements, "title", "")
    if not isinstance(title, str) or len(title) > 200:
        return False
    return True


def _month_index(value: str, frozen_date: date) -> int | None:
    normalized = value.strip().lower()
    if normalized in {"present", "current"}:
        return frozen_date.year * 12 + frozen_date.month - 1
    match = re.fullmatch(r"(0?[1-9]|1[0-2])/(19|20)(\d{2})", normalized)
    if match:
        year = int(match.group(2) + match.group(3))
        return year * 12 + int(match.group(1)) - 1
    match = re.fullmatch(r"([a-z]+)\s+((?:19|20)\d{2})", normalized)
    if match and match.group(1) in _MONTHS:
        return int(match.group(2)) * 12 + _MONTHS[match.group(1)] - 1
    if re.fullmatch(r"(?:19|20)\d{2}", normalized):
        # Match hr_scorer's established convention for a year-only date.
        return int(normalized) * 12 + 5
    return None


def _job_intervals(job: Any, frozen_date: date) -> list[tuple[int, int]]:
    frozen_month = frozen_date.year * 12 + frozen_date.month - 1
    context = getattr(job, "date_context", "") or ""
    if ";" in context:
        parsed: list[tuple[int, int]] = []
        for match in _DATE_RANGE_RE.finditer(context):
            start = _month_index(match.group("start"), frozen_date)
            end = _month_index(match.group("end"), frozen_date)
            if end is not None:
                # Closed roles sometimes contain an accidentally or
                # maliciously future-dated end. Never credit months beyond the
                # report's trusted as-of date.
                end = min(end, frozen_month)
            if start is not None and end is not None and end > start:
                parsed.append((start, end))
        # A compound range is trustworthy only if every segment parsed.
        if parsed and len(parsed) == len([part for part in context.split(";") if part.strip()]):
            return parsed
        return []

    start_date = getattr(job, "start_date", None)
    end_date = getattr(job, "end_date", None)
    is_current = bool(getattr(job, "is_current", False))
    if start_date is None or (end_date is None and not is_current):
        return []
    start = start_date.year * 12 + start_date.month - 1
    effective_end = frozen_date if is_current else end_date
    if effective_end is None:
        return []
    end = min(
        effective_end.year * 12 + effective_end.month - 1,
        frozen_month,
    )
    return [(start, end)] if end > start else []


def _union_years(intervals: Iterable[tuple[int, int]]) -> float:
    covered: set[int] = set()
    for start, end in intervals:
        if end > start and end - start <= 200 * 12:
            covered.update(range(start, end))
    return len(covered) / 12.0


def _apply_conservative_experience(profile: Any, frozen_date: date) -> None:
    """Replace additive/bullet-derived years with title-bound interval unions."""
    from job_fit_scorer import _job_qualifies_for_clinical_development

    all_intervals: list[tuple[int, int]] = []
    intervals_by_type: dict[str, list[tuple[int, int]]] = {
        name: [] for name, _ in _GATE_TITLE_PATTERNS
    }
    cro_intervals: list[tuple[int, int]] = []
    for job in getattr(getattr(profile, "base", None), "jobs", []):
        intervals = _job_intervals(job, frozen_date)
        if not intervals:
            continue
        all_intervals.extend(intervals)
        title = (getattr(job, "title", "") or "").lower()
        company = (getattr(job, "company", "") or "").lower()
        for experience_type, patterns in _GATE_TITLE_PATTERNS:
            qualifies = (
                _job_qualifies_for_clinical_development(job)
                if experience_type == "clinical_development"
                else any(re.search(pattern, title) for pattern in patterns)
            )
            if qualifies:
                intervals_by_type[experience_type].extend(intervals)
        if any(
            cro in company
            for cro in (
                "covance",
                "labcorp",
                "icon",
                "iqvia",
                "parexel",
                "ppd",
                "syneos",
                "medpace",
                "pra health",
            )
        ):
            cro_intervals.extend(intervals)

    strict_years = {
        key: _union_years(value)
        for key, value in intervals_by_type.items()
        if value
    }
    if cro_intervals:
        strict_years["cro_experience"] = _union_years(cro_intervals)
    if "monitoring" in strict_years and cro_intervals:
        # Intersect monitoring/CRO months for independent-monitoring credit.
        monitoring_months: set[int] = set()
        cro_months: set[int] = set()
        for start, end in intervals_by_type["monitoring"]:
            monitoring_months.update(range(start, end))
        for start, end in cro_intervals:
            cro_months.update(range(start, end))
        strict_years["independent_monitoring"] = len(
            monitoring_months & cro_months
        ) / 12.0

    profile.years_by_type = strict_years
    profile.base.total_years_experience = _union_years(all_intervals)


def _knockout_code(category: str) -> str:
    return {
        "experience": "MINIMUM_EXPERIENCE_SHORTFALL",
        "education": "REQUIRED_EDUCATION_MISSING",
        "certification": "REQUIRED_CERTIFICATION_MISSING",
        "tools": "REQUIRED_TOOL_EXPERIENCE_MISSING",
        "language": "REQUIRED_LANGUAGE_MISSING",
        "visa": "WORK_AUTHORIZATION_CONFLICT",
        "location": "LOCATION_REQUIREMENT_CONFLICT",
        "extraction": "UNPARSEABLE_HARD_REQUIREMENT",
    }.get(category, "HARD_REQUIREMENT_MISSING")


def _ordered_unique(values: Iterable[str]) -> list[str]:
    result: list[str] = []
    for value in values:
        if value not in result:
            result.append(value)
    return result


def assess_candidate_fit(
    resume_text: str,
    job_description_text: str,
    *,
    run_id: str,
    case_id: str,
    as_of_date: str | date,
    resume_digest: str | None = None,
    job_description_digest: str | None = None,
    extraction_trustworthy: bool = True,
) -> dict[str, Any]:
    """Assess unmodified master-resume fit against a job description.

    This audited report keeps a FIXED policy bar on purpose.
    The report is digest-bound into the authorization receipt, and every
    receipt for one run must agree on the bar that was applied, so this is not
    the place for a per-request preference.

    An applicant's own bar is applied one layer up, by
    ``agent.tools.candidate_fit``, which evaluates this report's score against
    resolve_threshold(...). The provable standard therefore stays fixed while
    the advice adapts to the person asking.

    Semantic scoring, embedding caches, network access, and mutable profile
    caches remain disabled.
    """
    if not isinstance(resume_text, str) or not isinstance(job_description_text, str):
        raise ValueError("resume_text and job_description_text must be strings")
    run_id = _validate_identity(run_id, "run_id")
    case_id = _validate_identity(case_id, "case_id")
    frozen_date = _parse_as_of_date(as_of_date)
    bound_resume_digest = _validate_digest(resume_digest, resume_text, "resume_digest")
    bound_jd_digest = _validate_digest(
        job_description_digest, job_description_text, "job_description_digest"
    )

    component_scores = {key: 0.0 for key in _COMPONENT_KEYS}
    hard_knockouts: list[dict[str, str]] = []
    score = 0.0
    structural_trust = False

    try:
        # Lazy import keeps importing this gate itself free of scorer/model
        # initialization. The invoked path explicitly disables all semantics.
        from job_fit_scorer import (
            DEGREE_RANK,
            _split_required_vs_preferred,
            build_candidate_profile,
            check_knockouts,
            extract_requirements,
            score_fit_dimensions,
        )

        required_text, preferred_text = _split_required_vs_preferred(
            job_description_text
        )
        generic_ladder = _extract_generic_degree_experience_ladder(required_text)
        extraction_text = job_description_text
        if generic_ladder.detected:
            extraction_text = "\n".join(
                part
                for part in (generic_ladder.remaining_text, preferred_text)
                if part
            )
        requirements = extract_requirements(extraction_text, semantic_enabled=False)
        if generic_ladder.detected:
            requirements.extraction_trustworthy = bool(
                requirements.extraction_trustworthy
                and generic_ladder.trustworthy
            )
        if generic_ladder.trustworthy and generic_ladder.paths:
            requirements.degree_experience_paths = [
                {"degree": degree, "years": years}
                for degree, years in generic_ladder.paths
            ]
            requirements.degree_experience_type = "total"
            route_degrees = [degree for degree, _ in generic_ladder.paths]
            requirements.required_degree = max(
                route_degrees,
                key=lambda degree: DEGREE_RANK.get(degree, 0),
            )
            requirements.acceptable_degrees = route_degrees
            requirements.min_years_total = 0.0
            requirements.min_years_specific = {}
            requirements.extraction_confidence = min(
                100.0, requirements.extraction_confidence + 15.0
            )
        profile = build_candidate_profile(
            resume_text, as_of_date=frozen_date, use_cache=False
        )
        _apply_conservative_experience(profile, frozen_date)
        if requirements.degree_experience_type == "total":
            profile.years_by_type["total"] = profile.base.total_years_experience
        structural_trust = _text_structure_is_trustworthy(
            resume_text, job_description_text, profile, requirements
        )
        knockout_result = check_knockouts(profile, requirements)
        dimensions = score_fit_dimensions(
            profile,
            requirements,
            resume_text,
            job_description_text,
            semantic_enabled=False,
        )
        dimension_dict = dimensions.to_dict()
        component_scores = {
            key: round(float(dimension_dict[key]), 1) for key in _COMPONENT_KEYS
        }
        score = round(float(dimensions.weighted_score()), 1)

        for knockout in knockout_result.knockouts:
            if knockout.severity != "hard":
                continue
            hard_knockouts.append(
                {
                    "code": _knockout_code(knockout.category),
                    "category": knockout.category,
                    "requirement": knockout.requirement,
                    "candidate_has": knockout.candidate_has,
                }
            )
        if hard_knockouts:
            # Informative degradation: each hard knockout lowers the ceiling
            # (1 -> 55, 2 -> 45, 3+ -> 35) instead of flattening every
            # rejection to 35, so the report keeps signaling component
            # strength beneath the knockout.
            cap = max(35.0, 65.0 - 10.0 * len(hard_knockouts))
            score = min(score, cap)
    except Exception:
        # Parsing/scoring ambiguity is a policy rejection, never a permissive
        # fallback. Details are deliberately not serialized into the stable
        # authorization surface.
        structural_trust = False
        component_scores = {key: 0.0 for key in _COMPONENT_KEYS}
        hard_knockouts = []
        score = 0.0

    trustworthy = bool(extraction_trustworthy) and structural_trust
    passed = trustworthy and not hard_knockouts and score >= CANDIDATE_FIT_THRESHOLD
    codes = _ordered_unique(
        code
        for condition, code in (
            (not trustworthy, "UNTRUSTWORTHY_EXTRACTION"),
            (bool(hard_knockouts), "HARD_KNOCKOUT"),
            (score < CANDIDATE_FIT_THRESHOLD, "SCORE_BELOW_THRESHOLD"),
        )
        if condition
    )

    return {
        "schema_version": CANDIDATE_FIT_SCHEMA_VERSION,
        "policy_version": CANDIDATE_FIT_POLICY_VERSION,
        "scorer_version": CANDIDATE_FIT_SCORER_VERSION,
        "run_id": run_id,
        "case_id": case_id,
        "as_of_date": frozen_date.isoformat(),
        "threshold": CANDIDATE_FIT_THRESHOLD,
        "semantic_mode": SEMANTIC_MODE,
        "resume_digest": bound_resume_digest,
        "job_description_digest": bound_jd_digest,
        "score": score,
        "component_scores": component_scores,
        "recommendation": "PROCEED" if passed else "REJECT",
        "hard_knockouts": hard_knockouts,
        "extraction_trustworthy": trustworthy,
        "passed": passed,
        "codes": codes,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Deterministic master-resume candidate-fit preflight"
    )
    parser.add_argument("--resume", required=True)
    parser.add_argument("--job-description", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--case-id", required=True)
    parser.add_argument("--as-of-date", required=True)
    parser.add_argument("--json", action="store_true", help="emit the JSON report")
    args = parser.parse_args(argv)

    try:
        resume = _load_artifact(args.resume, resume=True)
        job_description = _load_artifact(args.job_description, resume=False)
        report = assess_candidate_fit(
            resume.text,
            job_description.text,
            run_id=args.run_id,
            case_id=args.case_id,
            as_of_date=args.as_of_date,
            resume_digest=resume.digest,
            job_description_digest=job_description.digest,
            extraction_trustworthy=resume.trustworthy and job_description.trustworthy,
        )
    except (OSError, ValueError) as exc:
        print(json.dumps({"error": str(exc)}, sort_keys=True))
        return 2

    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
