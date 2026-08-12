"""Fail-closed orchestration kernel for the native resume agent team.

The kernel is deliberately vendor-neutral.  Codex and Claude host integrations
provide an ``adapter`` that invokes one named role, while the application
provides ``services`` for handoff validation, deterministic auditing, event
recording, and atomic publication.  This module performs no model, network,
filesystem, subprocess, or credential access of its own.

Public entrypoint::

    run_team(request: dict, adapter: object, services: object) -> dict

The controller is the sole authority allowed to publish.  A Writer or Editor
artifact is never sufficient on its own: every candidate must pass a distinct
Auditor and three independent deterministic service votes, with exact digest
lineage, before verified publication.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import unicodedata
from collections import Counter
from datetime import date
from typing import Any, Callable

import resume_integrity_audit
from candidate_fit_judge import validate_candidate_fit_judge_report
from candidate_fit_preflight import (
    CANDIDATE_FIT_POLICY_VERSION,
    CANDIDATE_FIT_SCHEMA_VERSION,
    CANDIDATE_FIT_SCORER_VERSION,
    CANDIDATE_FIT_THRESHOLD,
)
from candidate_fit_preflight import (
    assess_candidate_fit as _deterministic_candidate_fit_assessment,
)
from claim_provenance_audit import claim_supported_by_source

PROTOCOL_VERSION = "resume-team/v2"
CONTEXT_VERSION = "resume-team-context/v1"
HANDOFF_VERSION = "resume-team-handoff/v1"
RESULT_VERSION = "resume-team-result/v2"
AUTHORIZATION_VERSION = "resume-team-authorization/v1"
VOTE_VERSION = "resume-team-vote/v1"
FINAL_RECEIPT_VERSION = "resume-team-final-receipt/v2"
PUBLICATION_VERSION = "resume-team-publication/v1"
PUBLICATION_VERIFICATION_VERSION = "resume-team-publication-verification/v1"
RUN_CLAIM_VERSION = "resume-team-run-claim/v1"
SOURCE_ATTESTATION_VERSION = "resume-team-source-attestation/v1"

ROLE_ORDER = ("researcher", "writer", "auditor", "editor")
MAX_EDITOR_ATTEMPTS = 2

_REQUEST_KEYS = {
    "schema_version",
    "run_id",
    "case_id",
    "job_description",
    "master_resume",
    "output_dir",
    "max_editor_attempts",
    "role_timeout_seconds",
}
_SCHEMA_CODES = {
    "researcher": "RESEARCH_SCHEMA",
    "writer": "WRITER_SCHEMA",
    "auditor": "AUDITOR_SCHEMA",
    "editor": "EDITOR_SCHEMA",
}
_CONTEXT_KEYS = {
    "schema_version",
    "run_id",
    "case_id",
    "role",
    "attempt",
    "parent_artifact_digest",
    "payload",
}
_HANDOFF_KEYS = {
    "schema_version",
    "run_id",
    "case_id",
    "role",
    "agent_id",
    "attempt",
    "parent_artifact_digest",
    "artifact_digest",
    "status",
    "payload",
}
_PAYLOAD_KEYS = {
    "researcher": {"rubric", "jd_evidence_spans"},
    "writer": {"draft", "claim_evidence"},
    "auditor": {"verdict", "findings", "draft_digest"},
    "editor": {"draft", "addressed_finding_ids", "claim_evidence"},
}

_VOTE_NAMES = ("evidence", "human_voice", "canonical_integrity")
_WRITER_REJECTION_CODES = frozenset(
    {"INVALID_ITEM", "INVALID_ANCHOR", "STRICT_COMPILER", "CANONICAL_INTEGRITY"}
)
_CANDIDATE_FIT_REPORT_KEYS = {
    "schema_version",
    "policy_version",
    "scorer_version",
    "run_id",
    "case_id",
    "as_of_date",
    "threshold",
    "semantic_mode",
    "resume_digest",
    "job_description_digest",
    "score",
    "component_scores",
    "recommendation",
    "hard_knockouts",
    "extraction_trustworthy",
    "passed",
    "codes",
}
_CANDIDATE_FIT_COMPONENT_KEYS = {
    "experience_match",
    "skills_match",
    "title_alignment",
    "domain_match",
    "education_match",
    "certification_match",
    "seniority_match",
}
_CANDIDATE_FIT_KNOCKOUT_KEYS = {
    "code",
    "category",
    "requirement",
    "candidate_has",
}
_AUTHORIZATION_REPORT_KEYS = {
    "schema_version",
    "draft_digest",
    "passed",
    "codes",
    "votes",
    "findings",
}
_DETERMINISTIC_FINDING_KEYS = {
    "id",
    "vote_name",
    "code",
    "actionable",
    "locations",
}
_DETERMINISTIC_LOCATION_KEYS = {
    "line_number",
    "source_line",
    "line_digest",
}
_CLAIM_EVIDENCE_KEYS = {
    "claim_digest",
    "source_span_digest",
    "source_start",
    "source_end",
}
_RECEIPT_BASENAME_RE = re.compile(
    r"resume-team-receipt\.[0-9a-f]{64}\.json"
)
# hosts: api = hosted product runtime; codex/claude = legacy local CLI hosts
_NATIVE_AGENT_ID_RE = re.compile(
    r"^(?P<host>codex|claude|api):[A-Za-z0-9._:-]{1,128}$"
)
_AGENT_INVOCATION_FAILURE_CODES = frozenset(
    {
        "AGENT_EVENT_MALFORMED",
        "AGENT_OUTPUT_MALFORMED",
        "AGENT_PAYLOAD_SCHEMA",
    }
)


class AgentInvocationFailure(RuntimeError):
    """Typed, non-sensitive failure raised by a trusted native host adapter."""

    def __init__(self, code: str):
        if code not in _AGENT_INVOCATION_FAILURE_CODES:
            raise ValueError("unknown agent invocation failure code")
        self.code = code
        super().__init__(code)


class NoSafeWriterChanges(RuntimeError):
    """Writer proposals contained no individually safe replacement."""

    def __init__(self, stats: dict[str, Any]):
        self.stats = json.loads(_canonical_json(stats))
        super().__init__("writer proposed no safe source-supported changes")


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    )


def _digest(value: Any) -> str:
    if isinstance(value, bytes):
        encoded = value
    elif isinstance(value, str):
        encoded = value.encode("utf-8")
    else:
        encoded = _canonical_json(value).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def canonical_digest(value: Any) -> str:
    """Return the protocol's lowercase SHA-256 digest for text or JSON data."""

    return _digest(value)


def _is_sha256(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _native_agent_host(agent_id: Any) -> str | None:
    """Return the supported native host encoded in an invocation identity."""

    if not isinstance(agent_id, str):
        return None
    matched = _NATIVE_AGENT_ID_RE.fullmatch(agent_id)
    return matched.group("host") if matched else None


def _valid_role_attempt(role: str, attempt: Any) -> bool:
    if type(attempt) is not int:
        return False
    if role in {"researcher", "writer"}:
        return attempt == 0
    if role == "auditor":
        return 0 <= attempt <= MAX_EDITOR_ATTEMPTS
    if role == "editor":
        return 1 <= attempt <= MAX_EDITOR_ATTEMPTS
    return False


def build_context(
    *,
    run_id: str,
    case_id: str,
    role: str,
    attempt: int,
    payload: dict[str, Any],
) -> dict[str, Any]:
    """Build one exact, digest-bound role input context.

    The function is pure so native hosts can use it without an API key or
    vendor SDK.  It deliberately rejects booleans as attempts.
    """

    if role not in ROLE_ORDER:
        raise ValueError("unknown role")
    if not isinstance(run_id, str) or not run_id:
        raise ValueError("run_id must be non-empty")
    if not isinstance(case_id, str) or not case_id:
        raise ValueError("case_id must be non-empty")
    if not _valid_role_attempt(role, attempt):
        raise ValueError("attempt is outside the correction budget")
    if not isinstance(payload, dict):
        raise ValueError("payload must be an object")
    return {
        "schema_version": CONTEXT_VERSION,
        "run_id": run_id,
        "case_id": case_id,
        "role": role,
        "attempt": attempt,
        "parent_artifact_digest": _digest(payload),
        "payload": payload,
    }


def build_handoff(
    *,
    context: dict[str, Any],
    role: str,
    agent_id: str,
    payload: dict[str, Any],
    status: str = "complete",
) -> dict[str, Any]:
    """Wrap an untrusted native role payload in coordinator-owned provenance.

    Cryptographic metadata is control-plane work.  Role models should return
    only their strict payload; the host adapter supplies its real invocation ID
    and this function computes the envelope digest.
    """

    if not isinstance(context, dict) or set(context) != _CONTEXT_KEYS:
        raise ValueError("invalid context")
    if (
        context.get("schema_version") != CONTEXT_VERSION
        or context.get("role") != role
        or not _valid_role_attempt(role, context.get("attempt"))
        or not isinstance(context.get("run_id"), str)
        or not context["run_id"]
        or not isinstance(context.get("case_id"), str)
        or not context["case_id"]
        or context.get("parent_artifact_digest")
        != _digest(context.get("payload"))
    ):
        raise ValueError("context role mismatch")
    if not isinstance(agent_id, str) or not agent_id:
        raise ValueError("agent_id must be non-empty")
    if status not in {"complete", "failed"}:
        raise ValueError("invalid status")
    if not isinstance(payload, dict):
        raise ValueError("payload must be an object")
    return {
        "schema_version": HANDOFF_VERSION,
        "run_id": context["run_id"],
        "case_id": context["case_id"],
        "role": role,
        "agent_id": agent_id,
        "attempt": context["attempt"],
        "parent_artifact_digest": context["parent_artifact_digest"],
        "artifact_digest": _digest(payload),
        "status": status,
        "payload": payload,
    }


def _unique_span(source: str, evidence_text: Any) -> tuple[int, int, str]:
    if not isinstance(evidence_text, str) or not evidence_text:
        raise ValueError("evidence text must be non-empty")
    start = source.find(evidence_text)
    if start < 0 or source.find(evidence_text, start + 1) >= 0:
        raise ValueError("evidence text must identify one unique source span")
    return start, start + len(evidence_text), _digest(evidence_text)


def _covers_complete_source_line(source: str, start: int, end: int) -> bool:
    """Accept an evidence span only when it covers one full semantic line."""

    if start < 0 or end <= start or end > len(source):
        return False
    evidence = source[start:end]
    if not evidence.strip() or "\n" in evidence or "\r" in evidence:
        return False
    line_start = source.rfind("\n", 0, start) + 1
    next_newline = source.find("\n", end)
    line_end = len(source) if next_newline < 0 else next_newline
    full_line = source[line_start:line_end].rstrip("\r").strip()
    return (
        evidence.strip() == full_line
        and not re.fullmatch(r"[_\-=]{3,}", full_line)
    )


def _significant_lines(text: str) -> set[str]:
    return {
        line.strip()
        for line in text.splitlines()
        if line.strip() and not re.fullmatch(r"[_\-=]{3,}", line.strip())
    }


def _candidate_text_format_valid(text: Any) -> bool:
    """Accept untrusted text whose only control character is an ASCII LF.

    Python's ``splitlines`` treats several control and Unicode characters as
    line boundaries.  Allowing those in model-authored text lets an untrusted
    role alter the byte-level document layout while downstream line-oriented
    gates still see apparently unchanged rows.  Horizontal controls and
    invisible format/surrogate characters are therefore excluded here.
    """

    if not isinstance(text, str) or not text.strip():
        return False
    return all(
        character == "\n"
        or unicodedata.category(character) not in {"Cc", "Cf", "Cs", "Zl", "Zp"}
        for character in text
    )


def _candidate_draft_format_valid(master: Any, draft: Any) -> bool:
    """Allow tabs only on byte-identical lines inherited from the source.

    Uploaded DOCX resumes commonly contain trusted tab stops in title/date or
    contact lines.  Replacement text still passes the stricter validator above,
    so a model can never introduce a tab.  At the whole-draft boundary, admit a
    tab-bearing line only when the immutable master contains it byte-identically
    at the same line position.  All other controls and invisible separators
    remain forbidden.
    """

    if not isinstance(master, str) or not isinstance(draft, str):
        return False
    if _candidate_text_format_valid(draft):
        return True
    if "\t" not in draft or not _candidate_text_format_valid(
        draft.replace("\t", "")
    ):
        return False
    master_lines = master.split("\n")
    return all(
        index < len(master_lines) and line == master_lines[index]
        for index, line in enumerate(draft.split("\n"))
        if "\t" in line
    )


def _line_counts(text: str) -> Counter[str]:
    return Counter(
        line.strip()
        for line in text.splitlines()
        if line.strip() and not re.fullmatch(r"[_\-=]{3,}", line.strip())
    )


def _ownership_violation_hint(master: str, draft: str) -> str:
    """Best-effort description of WHICH lines broke the duplicate/move rules.

    Diagnostic only — the boolean validators above keep sole authority; this
    never loosens anything. The repair loop shows the model the rejection
    reason, and "duplicates or moves source claims" alone gives it nothing
    to act on: observed live (2026-08-11), the prior writer failed the initial
    attempt and the blind repair identically. Naming the offending line
    turns the second attempt into a targeted fix. Fails silent to "" — a
    diagnostic that crashes must never mask the real rejection.
    """

    try:
        hints: list = []
        draft_counts = _line_counts(draft)
        master_counts = _line_counts(master)
        for line, count in draft_counts.items():
            if line in master_counts and count > master_counts[line]:
                hints.append(
                    f"line appears {count}x in draft but {master_counts[line]}x "
                    f"in the master: {line[:90]!r}"
                )
            elif line not in master_counts and count > 1:
                hints.append(f"new line repeated {count}x: {line[:90]!r}")
        master_roles = _experience_roles(master)
        draft_roles = _experience_roles(draft)
        master_home: dict = {}
        for role in master_roles:
            for line in role["lines"]:
                master_home.setdefault(line, role["key"])
        for role in draft_roles:
            for line in role["lines"]:
                home = master_home.get(line)
                if home is not None and home != role["key"]:
                    hints.append(
                        f"bullet moved to a different role than the master: "
                        f"{line[:90]!r}"
                    )
                elif home is None and line in master_counts:
                    hints.append(
                        "master line moved from outside Professional Experience "
                        f"into a role: {line[:90]!r}"
                    )
        if not hints:
            return ""
        return " — " + "; ".join(hints[:3])
    except Exception:
        return ""


def _changed_lines(draft: str, master: str) -> set[str] | None:
    """Return novel lines, rejecting duplicated master or novel lines."""

    draft_counts = _line_counts(draft)
    master_counts = _line_counts(master)
    for line, count in draft_counts.items():
        if line in master_counts and count > master_counts[line]:
            return None
        if line not in master_counts and count > 1:
            return None
    return {line for line in draft_counts if line not in master_counts}


_EXPERIENCE_HEADINGS = {
    "PROFESSIONAL EXPERIENCE",
    "WORK EXPERIENCE",
    "EXPERIENCE",
}
_CORE_COMPETENCIES_HEADINGS = {
    "CORE COMPETENCIES",
    "COMPETENCIES",
}
_EXPERIENCE_END_HEADINGS = {
    "PROFESSIONAL SUMMARY",
    "SUMMARY",
    "CORE COMPETENCIES",
    "COMPETENCIES",
    "EDUCATION",
    "CERTIFICATIONS",
    "CERTIFICATIONS & LICENSURE",
    "LICENSURE",
    "PUBLICATIONS",
    "PROJECTS",
    "PROFESSIONAL MEMBERSHIPS",
    "MEMBERSHIPS",
    "SKILLS",
    "AWARDS",
    "HONORS",
    "LANGUAGES",
    "VOLUNTEER",
}
_DATE_LINE_RE = re.compile(
    r"\b(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|"
    r"Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|Nov(?:ember)?|"
    r"Dec(?:ember)?|Present|(?:19|20)\d{2})\b",
    flags=re.IGNORECASE,
)
_ROLE_DATE_COMPONENT = (
    r"(?:(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|"
    r"Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|"
    r"Nov(?:ember)?|Dec(?:ember)?)(?:\s+\d{1,2},?)?\s+)?"
    r"(?:19|20)\d{2}|(?:0?[1-9]|1[0-2])/(?:19|20)\d{2}|Present"
)
_ROLE_DATE_RANGE = (
    rf"(?:{_ROLE_DATE_COMPONENT})(?:\s*(?:-|to)\s*"
    rf"(?:{_ROLE_DATE_COMPONENT}))?"
)
_ROLE_DATE_ONLY_RE = re.compile(
    rf"^{_ROLE_DATE_RANGE}(?:\s*;\s*{_ROLE_DATE_RANGE})*$",
    flags=re.IGNORECASE,
)


def _line_records(text: str) -> list[tuple[str, int, int]]:
    records: list[tuple[str, int, int]] = []
    offset = 0
    for raw in text.splitlines(keepends=True):
        content = raw.rstrip("\r\n")
        records.append((content, offset, offset + len(content)))
        offset += len(raw)
    if not records and text:
        records.append((text, 0, len(text)))
    return records


def _role_identity_text(value: str, *, leading: bool = False) -> str:
    """Normalize role identity exactly like the canonical resume audit."""

    value = unicodedata.normalize("NFKC", value).replace("\u00a0", " ")
    value = value.replace("\t", " | ")
    value = re.sub(r"[‐‑‒–—―]", "-", value)
    if leading:
        value = re.sub(
            r"^\s*(?:#{1,6}\s+|[-*+•]\s+|\d+[.)]\s+)",
            "",
            value,
        )
    value = re.sub(r"(?<!\w)[*_`]+|[*_`]+(?!\w)", "", value)
    value = re.sub(r"\s*\|\s*", " | ", value)
    value = re.sub(r"(?<=\w)\s*-\s*(?=\w)", " - ", value)
    return re.sub(r"\s+", " ", value).strip()


def _section_heading_name(raw: str) -> str:
    return _role_identity_text(raw, leading=True).rstrip(":").strip().upper()


def _is_role_bullet(raw: str) -> bool:
    return bool(re.match(r"^\s*(?:[-*+•]|\d+[.)])\s+", raw))


def _is_genuine_role_bullet(raw: str) -> bool:
    """Return whether a role row is a marker plus substantive bullet text."""

    matched = re.match(r"^\s*(?:[-*+•]|\d+[.)])\s+(?P<body>.*)$", raw)
    return bool(
        matched
        and any(character.isalnum() for character in matched.group("body"))
    )


def _is_separator(raw: str) -> bool:
    return bool(re.fullmatch(r"\s*(?:_{3,}|-{3,}|={3,})\s*", raw))


def _has_nonempty_core_competencies(text: str) -> bool:
    """Return whether a recognized competencies section has real content."""

    in_section = False
    known_headings = _EXPERIENCE_HEADINGS | _EXPERIENCE_END_HEADINGS
    for raw, _, _ in _line_records(text):
        heading = _section_heading_name(raw)
        if heading in _CORE_COMPETENCIES_HEADINGS:
            in_section = True
            continue
        if in_section and heading in known_headings:
            return False
        if (
            in_section
            and raw.strip()
            and not _is_separator(raw)
            and any(character.isalnum() for character in raw)
        ):
            return True
    return False


def _is_role_date_line(value: str) -> bool:
    return bool(_ROLE_DATE_ONLY_RE.fullmatch(value.strip()))


def _has_experience_section(text: str) -> bool:
    return any(
        _section_heading_name(raw) in _EXPERIENCE_HEADINGS
        for raw, _, _ in _line_records(text)
    )


def _experience_roles(text: str) -> list[dict[str, Any]]:
    """Return span-aware role ownership for every supported resume layout.

    The parser intentionally mirrors ``resume_integrity_audit._parse_roles``
    while retaining source offsets and per-role line multiplicity.  This keeps
    provenance locality intact for source Markdown, rendered/DOCX text, and
    standalone title/company/date layouts.
    """

    records = _line_records(text)
    section_start: int | None = None
    section_end = len(text)
    for index, (raw, _, _) in enumerate(records):
        heading = _section_heading_name(raw)
        if heading in _EXPERIENCE_HEADINGS and section_start is None:
            section_start = index + 1
            continue
        if section_start is not None and (
            heading in _EXPERIENCE_END_HEADINGS
            or heading in _EXPERIENCE_HEADINGS
        ):
            section_end = records[index][1]
            break
    if section_start is None:
        return []

    rows = [
        (index, raw, start, end)
        for index, (raw, start, end) in enumerate(records[section_start:], section_start)
        if start < section_end and raw.strip() and not _is_separator(raw)
    ]
    headers: list[
        tuple[int, int, tuple[str, str, str], frozenset[int]]
    ] = []
    position = 0
    while position < len(rows):
        _, raw, start, _ = rows[position]
        if _is_role_bullet(raw):
            position += 1
            continue
        clean = _role_identity_text(raw, leading=True)
        parts = [
            part
            for raw_part in clean.split("|")
            if (part := _role_identity_text(raw_part))
        ]

        # Rendered/DOCX layout: title | dates, then company | location.
        if (
            len(parts) >= 2
            and _is_role_date_line(parts[-1])
            and position + 1 < len(rows)
            and not _is_role_bullet(rows[position + 1][1])
        ):
            company_clean = _role_identity_text(
                rows[position + 1][1], leading=True
            )
            company_parts = [
                part
                for raw_part in company_clean.split("|")
                if (part := _role_identity_text(raw_part))
            ]
            if company_parts and not _is_role_date_line(company_parts[0]):
                headers.append(
                    (
                        position,
                        start,
                        (parts[0], company_parts[0], parts[-1]),
                        frozenset({position + 1}),
                    )
                )
                position += 2
                continue

        # Source layout: title | company [| location ...], then dates.
        if len(parts) >= 2 and not _is_role_date_line(parts[-1]):
            date_position = position + 1
            while (
                date_position < len(rows)
                and _is_role_bullet(rows[date_position][1])
            ):
                date_position += 1
            if date_position < len(rows):
                date_text = _role_identity_text(
                    rows[date_position][1], leading=True
                )
            else:
                date_text = ""
            if date_text and _DATE_LINE_RE.search(date_text) and not _is_role_date_line(
                date_text
            ):
                # A rendered role header or date-bearing prose may not be
                # consumed as this source role's metadata.
                return []
            if (
                date_position < len(rows)
                and not _is_role_bullet(rows[date_position][1])
                and _is_role_date_line(date_text)
            ):
                headers.append(
                    (
                        position,
                        start,
                        (parts[0], parts[1], date_text),
                        frozenset({date_position}),
                    )
                )
                position = date_position + 1
                continue

        # Standalone layout: title, company [| location], then dates.
        if (
            len(parts) == 1
            and not _is_role_date_line(clean)
            and position + 2 < len(rows)
            and not _is_role_bullet(rows[position + 1][1])
            and not _is_role_bullet(rows[position + 2][1])
        ):
            company_clean = _role_identity_text(
                rows[position + 1][1], leading=True
            )
            date_text = _role_identity_text(
                rows[position + 2][1], leading=True
            )
            company_parts = [
                part
                for raw_part in company_clean.split("|")
                if (part := _role_identity_text(raw_part))
            ]
            if (
                company_parts
                and not _is_role_date_line(company_parts[0])
                and _is_role_date_line(date_text)
            ):
                headers.append(
                    (
                        position,
                        start,
                        (parts[0], company_parts[0], date_text),
                        frozenset({position + 1, position + 2}),
                    )
                )
                position += 3
                continue
        position += 1

    roles: list[dict[str, Any]] = []
    for header_position, (
        row_position,
        start,
        key,
        metadata_rows,
    ) in enumerate(headers):
        next_row_position = (
            headers[header_position + 1][0]
            if header_position + 1 < len(headers)
            else len(rows)
        )
        end = (
            headers[header_position + 1][1]
            if header_position + 1 < len(headers)
            else section_end
        )
        body_lines = Counter(
            rows[row][1].strip()
            for row in range(row_position + 1, next_row_position)
            if row not in metadata_rows
            and rows[row][1].strip()
            and not _is_separator(rows[row][1])
        )
        bullet_count = sum(
            1
            for row in range(row_position + 1, next_row_position)
            if row not in metadata_rows
            and _is_genuine_role_bullet(rows[row][1])
        )
        roles.append(
            {
                "key": key,
                "start": start,
                "end": end,
                "lines": body_lines,
                "bullet_count": bullet_count,
            }
        )
    return roles


def _role_at(
    roles: list[dict[str, Any]],
    start: int,
    end: int,
) -> dict[str, Any] | None:
    matches = [role for role in roles if role["start"] <= start and end <= role["end"]]
    return matches[0] if len(matches) == 1 else None


def _canonical_experience_keys(text: str) -> Counter[tuple[str, str, str]] | None:
    """Cross-check span parsing against the independent canonical parser."""

    from resume_integrity_audit import AuditInputError, parse_resume_identity

    try:
        return parse_resume_identity(text)["roles"]
    except (AuditInputError, KeyError, TypeError):
        return None


def _experience_ownership_valid(master: str, draft: str) -> bool:
    master_roles = _experience_roles(master)
    draft_roles = _experience_roles(draft)
    master_has_section = _has_experience_section(master)
    draft_has_section = _has_experience_section(draft)
    if master_has_section != draft_has_section:
        return False
    if master_has_section and (not master_roles or not draft_roles):
        # Unknown role layouts must fail closed instead of silently disabling
        # locality enforcement for the whole experience section.
        return False
    if not master_has_section:
        return True
    if any(role.get("bullet_count", 0) < 1 for role in draft_roles):
        # Role identity alone is not a useful or honest experience record.  A
        # deletion-only candidate must retain at least one substantive bullet
        # under every canonical role before it can reach deterministic votes.
        return False
    master_role_keys = Counter(role["key"] for role in master_roles)
    draft_role_keys = Counter(role["key"] for role in draft_roles)
    master_canonical_keys = _canonical_experience_keys(master)
    draft_canonical_keys = _canonical_experience_keys(draft)
    if (
        master_canonical_keys is None
        or draft_canonical_keys is None
        or master_role_keys != master_canonical_keys
        or draft_role_keys != draft_canonical_keys
        or master_role_keys != draft_role_keys
    ):
        return False
    master_lines = _line_counts(master)
    master_by_key: dict[tuple[str, str, str], Counter[str]] = {}
    master_role_counts: Counter[str] = Counter()
    for role in master_roles:
        master_by_key.setdefault(role["key"], Counter()).update(role["lines"])
        master_role_counts.update(role["lines"])
    draft_by_key: dict[tuple[str, str, str], Counter[str]] = {}
    draft_role_counts: Counter[str] = Counter()
    for role in draft_roles:
        if role["key"] not in master_by_key:
            return False
        draft_by_key.setdefault(role["key"], Counter()).update(role["lines"])
        draft_role_counts.update(role["lines"])
    for key, draft_counts in draft_by_key.items():
        source_counts = master_by_key[key]
        for line, count in draft_counts.items():
            # Any verbatim master line placed inside Professional Experience
            # may occur no more often than it did in that exact canonical
            # role. This count-aware check blocks moving a shared line from B
            # into a second copy under A while global multiplicity stays flat,
            # as well as moving summary/education text into a role.
            if line in master_lines and count > source_counts[line]:
                return False
    # Enforce the reverse direction too: a line attributed to an experience
    # role may be omitted, but cannot reappear in Summary/Core or another
    # unscoped section.  Preserve only the outside-role multiplicity that the
    # master itself already had for that exact line.
    master_outside_counts = master_lines - master_role_counts
    draft_outside_counts = _line_counts(draft) - draft_role_counts
    if any(
        draft_outside_counts[line] > master_outside_counts[line]
        for line in master_lines
    ):
        return False
    return True


def _same_experience_role(
    draft: str,
    master: str,
    claim_line: str,
    source_start: int,
    source_end: int,
) -> bool:
    claim_start = draft.find(claim_line)
    if claim_start < 0 or draft.find(claim_line, claim_start + 1) >= 0:
        return False
    claim_role = _role_at(
        _experience_roles(draft), claim_start, claim_start + len(claim_line)
    )
    source_role = _role_at(_experience_roles(master), source_start, source_end)
    if claim_role is None or source_role is None:
        return claim_role is None and source_role is None
    return source_role["key"] == claim_role["key"]


def _normalize_claim_evidence(
    draft: str,
    master: str,
    evidence: Any,
) -> list[dict[str, Any]]:
    if not _candidate_draft_format_valid(master, draft):
        raise ValueError("unsafe draft text format")
    if not isinstance(evidence, list):
        raise ValueError("invalid claim evidence")

    changed_lines = _changed_lines(draft, master)
    if changed_lines is None or not _experience_ownership_valid(master, draft):
        raise ValueError(
            "draft duplicates or moves source claims"
            + _ownership_violation_hint(master, draft)
        )
    admitted_claims: set[str] = set()
    admitted_source_spans: set[tuple[int, int]] = set()
    draft_line_counts = _line_counts(draft)
    normalized: list[dict[str, Any]] = []
    for item in evidence:
        if not isinstance(item, dict) or set(item) != {
            "claim_text",
            "source_span_text",
        }:
            raise ValueError("invalid claim evidence item")
        claim_text = item["claim_text"]
        source_text = item["source_span_text"]
        if not isinstance(claim_text, str) or not isinstance(source_text, str):
            raise ValueError("claim evidence must be text")
        claim_line = claim_text.strip()
        if (
            not claim_line
            or claim_text not in draft
            or claim_line not in changed_lines
            or not source_text
        ):
            raise ValueError("claim evidence must cover one complete changed line")
        source_start, source_end, source_digest = _unique_span(master, source_text)
        if not _covers_complete_source_line(master, source_start, source_end):
            raise ValueError("claim evidence must use one complete source line")
        source_bounds = (source_start, source_end)
        if (
            source_bounds in admitted_source_spans
            or source_text.strip() in draft_line_counts
        ):
            raise ValueError("claim evidence duplicates one source line")
        supported, code = claim_supported_by_source(claim_line, source_text)
        if not supported:
            raise ValueError(code)
        if not _same_experience_role(
            draft,
            master,
            claim_line,
            source_start,
            source_end,
        ):
            raise ValueError("UNSUPPORTED_CLAIM")
        if claim_line in admitted_claims:
            raise ValueError("duplicate claim evidence")
        admitted_claims.add(claim_line)
        admitted_source_spans.add(source_bounds)
        normalized.append(
            {
                "claim_digest": _digest(claim_line),
                "source_span_digest": source_digest,
                "source_start": source_start,
                "source_end": source_end,
            }
        )

    if admitted_claims != changed_lines:
        raise ValueError("claim evidence does not exactly cover every changed line")
    return normalized


def _compile_writer_replacements(
    master: str,
    replacements: Any,
) -> dict[str, Any]:
    if not isinstance(master, str) or not isinstance(replacements, list):
        raise ValueError("invalid writer replacements")
    if not replacements:
        return {"draft": master, "claim_evidence": []}
    records = {
        (start, end): body for body, start, end in _line_records(master)
    }
    anchored = []
    seen_spans = set()
    for item in replacements:
        if not isinstance(item, dict) or set(item) != {
            "source_span_text",
            "replacement_text",
        }:
            raise ValueError("invalid writer replacement item")
        source_text = item["source_span_text"]
        replacement_text = item["replacement_text"]
        if not isinstance(source_text, str) or not isinstance(
            replacement_text, str
        ):
            raise ValueError("writer replacement values must be text")
        start, end, _ = _unique_span(master, source_text)
        if (
            not _covers_complete_source_line(master, start, end)
            or records.get((start, end)) != source_text
        ):
            raise ValueError("writer source must be one exact complete line")
        if (start, end) in seen_spans:
            raise ValueError("writer replacement reuses one source line")
        if replacement_text == source_text:
            raise ValueError("writer replacement is a no-op")
        if replacement_text != "" and (
            not replacement_text.strip()
            or "\n" in replacement_text
            or "\r" in replacement_text
            or not _candidate_text_format_valid(replacement_text)
        ):
            raise ValueError("writer replacement must be one safe line")
        seen_spans.add((start, end))
        anchored.append((start, end, source_text, replacement_text))

    anchored.sort(key=lambda row: row[0])
    chunks = []
    raw_evidence = []
    cursor = 0
    for start, end, source_text, replacement_text in anchored:
        if start < cursor:
            raise ValueError("writer replacements overlap")
        chunks.extend((master[cursor:start], replacement_text))
        cursor = end
        if replacement_text:
            raw_evidence.append(
                {
                    "claim_text": replacement_text,
                    "source_span_text": source_text,
                }
            )
    chunks.append(master[cursor:])
    draft = "".join(chunks)
    master_has_competencies = _has_nonempty_core_competencies(master)
    if master_has_competencies and not _has_nonempty_core_competencies(draft):
        raise ValueError("Writer must preserve non-empty Core Competencies")
    return {
        "draft": draft,
        "claim_evidence": _normalize_claim_evidence(
            draft, master, raw_evidence
        ),
    }


def _writer_salvage_rejection_code(
    item: Any,
    master: str,
) -> tuple[str | None, tuple[int, int] | None]:
    """Classify only malformed replacement items and unusable source anchors."""

    if not isinstance(item, dict) or set(item) != {
        "source_span_text",
        "replacement_text",
    }:
        return "INVALID_ITEM", None
    source = item["source_span_text"]
    replacement = item["replacement_text"]
    if not isinstance(source, str) or not isinstance(replacement, str):
        return "INVALID_ITEM", None
    try:
        start, end, _ = _unique_span(master, source)
    except Exception:
        return "INVALID_ANCHOR", None
    if not _covers_complete_source_line(master, start, end):
        return "INVALID_ANCHOR", None
    return None, (start, end)


def compile_writer_replacements_salvage(
    master: str,
    replacements: Any,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Keep source-ordered Writer replacements that remain safe in combination."""

    if not isinstance(master, str) or not isinstance(replacements, list):
        raise ValueError("invalid writer replacements")
    if not replacements:
        return _compile_writer_replacements(master, []), {
            "proposed_count": 0,
            "accepted_count": 0,
            "rejected_count": 0,
            "rejection_codes": {},
        }

    candidates: list[tuple[int, int, dict[str, str]]] = []
    rejected: Counter[str] = Counter()
    for ordinal, item in enumerate(replacements):
        category, bounds = _writer_salvage_rejection_code(item, master)
        if category is not None:
            rejected[category] += 1
            continue
        assert bounds is not None
        assert isinstance(item, dict)
        start, end = bounds
        try:
            _compile_writer_replacements(master, [item])
        except Exception:
            raw_draft = master[:start] + item["replacement_text"] + master[end:]
            integrity = resume_integrity_audit.audit_resume_text(
                master, raw_draft
            )
            rejected[
                "CANONICAL_INTEGRITY"
                if integrity.get("passed") is not True
                else "STRICT_COMPILER"
            ] += 1
            continue
        candidates.append((start, ordinal, item))

    accepted: list[dict[str, str]] = []
    accepted_starts: set[int] = set()
    compiled = _compile_writer_replacements(master, [])
    for start, _, item in sorted(candidates):
        if start in accepted_starts:
            rejected["INVALID_ANCHOR"] += 1
            continue
        try:
            proposed = _compile_writer_replacements(master, [*accepted, item])
        except Exception:
            rejected["STRICT_COMPILER"] += 1
            continue
        integrity = resume_integrity_audit.audit_resume_text(
            master, proposed["draft"]
        )
        if integrity.get("passed") is not True:
            rejected["CANONICAL_INTEGRITY"] += 1
            continue
        accepted.append(item)
        accepted_starts.add(start)
        compiled = proposed

    stats = {
        "proposed_count": len(replacements),
        "accepted_count": len(accepted),
        "rejected_count": len(replacements) - len(accepted),
        "rejection_codes": dict(sorted(rejected.items())),
    }
    if not accepted:
        raise NoSafeWriterChanges(stats)
    return compiled, stats


def _admit_writer_stats(value: Any) -> dict[str, Any] | None:
    """Return only well-formed, non-sensitive Writer salvage accounting."""

    expected_keys = {
        "proposed_count",
        "accepted_count",
        "rejected_count",
        "rejection_codes",
    }
    if type(value) is not dict or set(value) != expected_keys:
        return None
    proposed = value["proposed_count"]
    accepted = value["accepted_count"]
    rejected = value["rejected_count"]
    codes = value["rejection_codes"]
    if (
        type(proposed) is not int
        or type(accepted) is not int
        or type(rejected) is not int
        or type(codes) is not dict
        or min(proposed, accepted, rejected) < 0
        or proposed != accepted + rejected
    ):
        return None
    if any(
        not isinstance(code, str)
        or code not in _WRITER_REJECTION_CODES
        or type(count) is not int
        or count < 0
        for code, count in codes.items()
    ) or sum(codes.values()) != rejected:
        return None
    return json.loads(_canonical_json(value))


def _claim_evidence_valid(
    draft: Any,
    master: Any,
    evidence: Any,
) -> bool:
    if not _candidate_draft_format_valid(master, draft):
        return False
    if not isinstance(evidence, list):
        return False

    changed_lines = _changed_lines(draft, master)
    if changed_lines is None or not _experience_ownership_valid(master, draft):
        return False
    changed_by_digest = {_digest(line): line for line in changed_lines}
    admitted: set[str] = set()
    admitted_source_spans: set[tuple[int, int]] = set()
    draft_line_counts = _line_counts(draft)
    for item in evidence:
        if not isinstance(item, dict) or set(item) != _CLAIM_EVIDENCE_KEYS:
            return False
        claim_digest = item["claim_digest"]
        source_digest = item["source_span_digest"]
        source_start = item["source_start"]
        source_end = item["source_end"]
        if (
            not _is_sha256(claim_digest)
            or not _is_sha256(source_digest)
            or type(source_start) is not int
            or type(source_end) is not int
            or source_start < 0
            or source_end <= source_start
            or source_end > len(master)
            or claim_digest not in changed_by_digest
            or claim_digest in admitted
            or (source_start, source_end) in admitted_source_spans
        ):
            return False
        source_text = master[source_start:source_end]
        if _digest(source_text) != source_digest:
            return False
        if not _covers_complete_source_line(master, source_start, source_end):
            return False
        if source_text.strip() in draft_line_counts:
            return False
        if (
            master.find(source_text) != source_start
            or master.find(source_text, source_start + 1) >= 0
        ):
            return False
        supported, _ = claim_supported_by_source(
            changed_by_digest[claim_digest], source_text
        )
        if not supported or not _same_experience_role(
            draft,
            master,
            changed_by_digest[claim_digest],
            source_start,
            source_end,
        ):
            return False
        admitted.add(claim_digest)
        admitted_source_spans.add((source_start, source_end))
    return admitted == set(changed_by_digest)


def normalize_native_payload(
    role: str,
    raw_payload: Any,
    context: dict[str, Any],
) -> dict[str, Any]:
    """Convert a model-friendly role payload into the strict wire payload.

    Models provide exact source text rather than attempting cryptographic work.
    The coordinator verifies every text anchor against the role-scoped context
    and computes all offsets and hashes itself.  Any ambiguous or absent anchor
    fails closed.
    """

    if role not in ROLE_ORDER or not isinstance(raw_payload, dict):
        raise ValueError("invalid native payload")
    if (
        not isinstance(context, dict)
        or set(context) != _CONTEXT_KEYS
        or context.get("schema_version") != CONTEXT_VERSION
        or context.get("role") != role
        or not _valid_role_attempt(role, context.get("attempt"))
        or context.get("parent_artifact_digest")
        != _digest(context.get("payload"))
    ):
        raise ValueError("context role mismatch")
    scoped = context.get("payload")
    if not isinstance(scoped, dict):
        raise ValueError("invalid context payload")

    if role == "researcher":
        if set(raw_payload) != {"rubric", "jd_evidence_spans"}:
            raise ValueError("invalid researcher payload keys")
        source = scoped.get("job_description")
        rubric = raw_payload["rubric"]
        spans = raw_payload["jd_evidence_spans"]
        if (
            not isinstance(source, str)
            or not isinstance(rubric, dict)
            or set(rubric) != {"hard_requirements", "soft_requirements"}
            or not all(
                isinstance(rubric[key], list)
                and all(
                    isinstance(item, str)
                    and bool(item.strip())
                    and re.search(r"[A-Za-z0-9]", item) is not None
                    for item in rubric[key]
                )
                for key in ("hard_requirements", "soft_requirements")
            )
            or not isinstance(spans, list)
        ):
            raise ValueError("invalid researcher evidence")
        requirements = rubric["hard_requirements"] + rubric["soft_requirements"]
        if not requirements or len(requirements) != len(spans):
            raise ValueError("researcher rubric is not evidence-bound")
        normalized_spans = []
        for requirement, span in zip(requirements, spans):
            if not isinstance(span, dict) or set(span) != {"evidence_text"}:
                raise ValueError("invalid researcher span")
            if span["evidence_text"] != requirement:
                raise ValueError("researcher rubric is not evidence-bound")
            start, end, span_digest = _unique_span(source, span["evidence_text"])
            if not _covers_complete_source_line(source, start, end):
                raise ValueError("researcher evidence must use one complete JD line")
            normalized_spans.append(
                {"start": start, "end": end, "digest": span_digest}
            )
        return {
            "rubric": rubric,
            "jd_evidence_spans": normalized_spans,
        }

    if role == "writer":
        if set(raw_payload) != {"replacements"}:
            raise ValueError("invalid writer payload keys")
        master = scoped.get("master_resume")
        return _compile_writer_replacements(
            master, raw_payload["replacements"]
        )

    if role == "auditor":
        if set(raw_payload) != {"verdict", "findings", "audited_draft"}:
            raise ValueError("invalid auditor payload keys")
        draft = scoped.get("writer_draft")
        if raw_payload["audited_draft"] != draft or not isinstance(draft, str):
            raise ValueError("auditor did not bind the exact draft")
        findings = raw_payload["findings"]
        if not isinstance(findings, list):
            raise ValueError("invalid auditor findings")
        normalized_findings = []
        draft_significant_lines = _significant_lines(draft)
        draft_line_counts = _line_counts(draft)
        for finding in findings:
            if not isinstance(finding, dict) or set(finding) != {
                "id",
                "code",
                "evidence_text",
            }:
                raise ValueError("invalid auditor finding")
            evidence_text = finding["evidence_text"]
            if (
                not isinstance(evidence_text, str)
                or not evidence_text
                or evidence_text not in draft
            ):
                raise ValueError("auditor finding is not evidence-bound")
            evidence_line = evidence_text.strip()
            if (
                evidence_line not in draft_significant_lines
                or draft_line_counts[evidence_line] != 1
            ):
                raise ValueError(
                    "auditor evidence must cover one unique complete draft line"
                )
            normalized_findings.append(
                {
                    "id": finding["id"],
                    "code": finding["code"],
                    "evidence_digest": _digest(evidence_line),
                }
            )
        return {
            "verdict": raw_payload["verdict"],
            "findings": normalized_findings,
            "draft_digest": _digest(draft),
        }

    if set(raw_payload) != {"draft", "addressed_finding_ids", "claim_evidence"}:
        raise ValueError("invalid editor payload keys")
    draft = raw_payload["draft"]
    master = scoped.get("master_resume")
    if not isinstance(draft, str) or not isinstance(master, str):
        raise ValueError("invalid editor draft")
    return {
        "draft": draft,
        "addressed_finding_ids": raw_payload["addressed_finding_ids"],
        "claim_evidence": _normalize_claim_evidence(
            draft, master, raw_payload["claim_evidence"]
        ),
    }


def _valid_payload(role: str, payload: Any, context: dict[str, Any]) -> bool:
    if not isinstance(payload, dict) or set(payload) != _PAYLOAD_KEYS[role]:
        return False
    if role in {"writer", "editor"} and not _candidate_draft_format_valid(
        context.get("payload", {}).get("master_resume"), payload.get("draft")
    ):
        return False
    if role == "researcher":
        rubric = payload["rubric"]
        spans = payload["jd_evidence_spans"]
        if not isinstance(rubric, dict) or set(rubric) != {
            "hard_requirements",
            "soft_requirements",
        }:
            return False
        if not all(
            isinstance(rubric[key], list)
            and all(
                isinstance(item, str)
                and bool(item.strip())
                and re.search(r"[A-Za-z0-9]", item) is not None
                for item in rubric[key]
            )
            for key in ("hard_requirements", "soft_requirements")
        ):
            return False
        if not isinstance(spans, list):
            return False
        source = context.get("payload", {}).get("job_description")
        if not isinstance(source, str):
            return False
        requirements = rubric["hard_requirements"] + rubric["soft_requirements"]
        if not requirements or len(requirements) != len(spans):
            return False
        seen_spans: set[tuple[int, int]] = set()
        for requirement, span in zip(requirements, spans):
            if not isinstance(span, dict) or set(span) != {"start", "end", "digest"}:
                return False
            if (
                type(span["start"]) is not int
                or type(span["end"]) is not int
                or span["start"] < 0
                or span["end"] <= span["start"]
                or span["end"] > len(source)
                or not _is_sha256(span["digest"])
            ):
                return False
            bounds = (span["start"], span["end"])
            evidence_text = source[span["start"] : span["end"]]
            if (
                bounds in seen_spans
                or evidence_text != requirement
                or _digest(evidence_text) != span["digest"]
                or not _covers_complete_source_line(
                    source, span["start"], span["end"]
                )
                or source.find(evidence_text) != span["start"]
                or source.find(evidence_text, span["start"] + 1) >= 0
            ):
                return False
            seen_spans.add(bounds)
        return True
    if role == "writer":
        return _claim_evidence_valid(
            payload["draft"],
            context.get("payload", {}).get("master_resume"),
            payload["claim_evidence"],
        )
    if role == "auditor":
        verdict = payload["verdict"]
        findings = payload["findings"]
        if verdict not in {"PASS", "FAIL"} or not isinstance(findings, list):
            return False
        ids: list[str] = []
        for finding in findings:
            if not isinstance(finding, dict) or set(finding) != {
                "id",
                "code",
                "evidence_digest",
            }:
                return False
            if not all(
                isinstance(finding[key], str) and finding[key]
                for key in ("id", "code")
            ) or not _is_sha256(finding["evidence_digest"]):
                return False
            ids.append(finding["id"])
        if len(ids) != len(set(ids)):
            return False
        if (verdict == "PASS") != (not findings):
            return False
        writer_draft = context.get("payload", {}).get("writer_draft")
        master = context.get("payload", {}).get("master_resume")
        line_digests = {
            _digest(line)
            for line in (
                _significant_lines(writer_draft)
                | (_significant_lines(master) if isinstance(master, str) else set())
            )
        } if isinstance(writer_draft, str) else set()
        return (
            isinstance(writer_draft, str)
            and payload["draft_digest"] == _digest(writer_draft)
            and all(
                finding["evidence_digest"] in line_digests for finding in findings
            )
        )
    addressed = payload["addressed_finding_ids"]
    if not isinstance(addressed, list) or not all(
        isinstance(value, str) and value for value in addressed
    ):
        return False
    if len(addressed) != len(set(addressed)):
        return False
    active = context.get("payload", {}).get("audit_findings")
    if not isinstance(active, list) or not active:
        return False
    active_ids = {
        str(finding.get("id")) for finding in active if isinstance(finding, dict)
    }
    return (
        bool(active_ids)
        and set(addressed) == active_ids
        and _claim_evidence_valid(
            payload["draft"],
            context.get("payload", {}).get("master_resume"),
            payload["claim_evidence"],
        )
    )


def validate_handoff(
    role: str,
    envelope: Any,
    context: Any,
) -> dict[str, Any]:
    """Validate a native role packet without trusting any agent-supplied hash."""

    schema_code = _SCHEMA_CODES.get(role, "HANDOFF_SCHEMA")
    if role not in ROLE_ORDER:
        return {"valid": False, "code": schema_code}
    if not isinstance(context, dict) or set(context) != _CONTEXT_KEYS:
        return {"valid": False, "code": "HANDOFF_PROVENANCE"}
    if (
        context.get("schema_version") != CONTEXT_VERSION
        or context.get("role") != role
        or not _valid_role_attempt(role, context.get("attempt"))
        or context.get("parent_artifact_digest") != _digest(context.get("payload"))
    ):
        return {"valid": False, "code": "HANDOFF_PROVENANCE"}
    if not isinstance(envelope, dict) or set(envelope) != _HANDOFF_KEYS:
        return {"valid": False, "code": schema_code}
    if (
        envelope.get("schema_version") != HANDOFF_VERSION
        or envelope.get("role") != role
        or envelope.get("status") != "complete"
        or not isinstance(envelope.get("agent_id"), str)
        or not envelope["agent_id"]
        or type(envelope.get("attempt")) is not int
    ):
        return {"valid": False, "code": schema_code}
    for key in ("run_id", "case_id", "attempt", "parent_artifact_digest"):
        if envelope.get(key) != context.get(key):
            return {"valid": False, "code": "HANDOFF_PROVENANCE"}
    payload = envelope.get("payload")
    if envelope.get("artifact_digest") != _digest(payload):
        return {"valid": False, "code": schema_code}
    if not _valid_payload(role, payload, context):
        return {"valid": False, "code": schema_code}
    return {"valid": True, "code": "OK"}


def _valid_request(request: Any) -> bool:
    if not isinstance(request, dict) or set(request) != _REQUEST_KEYS:
        return False
    if request.get("schema_version") != PROTOCOL_VERSION:
        return False
    if not all(
        isinstance(request.get(key), str) and bool(request[key])
        for key in ("run_id", "case_id", "job_description", "master_resume", "output_dir")
    ):
        return False
    attempts = request.get("max_editor_attempts")
    timeout = request.get("role_timeout_seconds")
    return (
        type(attempts) is int
        and 0 <= attempts <= MAX_EDITOR_ATTEMPTS
        and type(timeout) in (int, float)
        and not isinstance(timeout, bool)
        and 0 < timeout <= 600
    )


def _result(
    request: dict[str, Any],
    terminal_class: str,
    *,
    published: bool = False,
    final_draft: str = "",
    authorization_receipt: dict[str, Any] | None = None,
    authorization_receipt_digest: str = "",
    authorization_receipt_path: str = "",
    candidate_fit_report: dict[str, Any] | None = None,
    candidate_fit_report_digest: str = "",
    candidate_fit_judge_report: dict[str, Any] | None = None,
    candidate_fit_judge_report_digest: str = "",
    digest_fn: Callable[[Any], str] = _digest,
) -> dict[str, Any]:
    return {
        "schema_version": RESULT_VERSION,
        "run_id": str(request.get("run_id", "invalid-run")),
        "case_id": str(request.get("case_id", "invalid-case")),
        "terminal_class": terminal_class,
        "published": published,
        "success_reported": published,
        "diagnostics": [] if published else [terminal_class],
        "final_draft_digest": digest_fn(final_draft) if published else "",
        "authorization_receipt": authorization_receipt if published else None,
        "authorization_receipt_digest": (
            authorization_receipt_digest if published else ""
        ),
        "authorization_receipt_path": authorization_receipt_path if published else "",
        "candidate_fit_report": candidate_fit_report,
        "candidate_fit_report_digest": (
            candidate_fit_report_digest if candidate_fit_report is not None else ""
        ),
        "candidate_fit_judge_report": candidate_fit_judge_report,
        "candidate_fit_judge_report_digest": (
            candidate_fit_judge_report_digest
            if candidate_fit_judge_report is not None
            else ""
        ),
    }


def _validate_run_claim(receipt: Any, request: dict[str, Any]) -> bool:
    return (
        isinstance(receipt, dict)
        and set(receipt)
        == {"schema_version", "run_id", "case_id", "claimed", "claim_id"}
        and receipt.get("schema_version") == RUN_CLAIM_VERSION
        and receipt.get("run_id") == request["run_id"]
        and receipt.get("case_id") == request["case_id"]
        and receipt.get("claimed") is True
        and isinstance(receipt.get("claim_id"), str)
        and bool(receipt["claim_id"])
    )


def _validate_source_attestation(attestation: Any, master_resume: str) -> bool:
    return (
        isinstance(attestation, dict)
        and set(attestation)
        == {"schema_version", "trusted", "source_id", "source_digest"}
        and attestation.get("schema_version") == SOURCE_ATTESTATION_VERSION
        and attestation.get("trusted") is True
        and isinstance(attestation.get("source_id"), str)
        and bool(attestation["source_id"])
        and attestation.get("source_digest") == _digest(master_resume)
    )


def _finite_fit_score(value: Any) -> bool:
    return (
        type(value) in (int, float)
        and math.isfinite(float(value))
        and 0.0 <= float(value) <= 100.0
    )


def validate_candidate_fit_report(
    report: Any,
    *,
    run_id: str,
    case_id: str,
    master_resume: str,
    job_description: str,
) -> tuple[bool, bool]:
    """Strictly validate a trusted deterministic candidate-fit assessment.

    Returns ``(structurally_and_semantically_valid, passed)``.  The validator
    binds the complete report to one run/case and the exact full master/JD
    strings.  Booleans, NaN/Infinity, out-of-range scores, reordered or forged
    policy codes, and permissive threshold changes all fail closed.
    """

    if not isinstance(report, dict) or set(report) != _CANDIDATE_FIT_REPORT_KEYS:
        return False, False
    if (
        report.get("schema_version") != CANDIDATE_FIT_SCHEMA_VERSION
        or report.get("policy_version") != CANDIDATE_FIT_POLICY_VERSION
        or report.get("scorer_version") != CANDIDATE_FIT_SCORER_VERSION
        or report.get("run_id") != run_id
        or report.get("case_id") != case_id
        or not isinstance(report.get("run_id"), str)
        or not isinstance(report.get("case_id"), str)
        or not (1 <= len(report["run_id"]) <= 256)
        or not (1 <= len(report["case_id"]) <= 256)
        or report.get("semantic_mode") != "disabled"
        or report.get("resume_digest") != _digest(master_resume)
        or report.get("job_description_digest") != _digest(job_description)
        or type(report.get("threshold")) not in (int, float)
        or not math.isfinite(float(report["threshold"]))
        or float(report["threshold"]) != float(CANDIDATE_FIT_THRESHOLD)
        or not _finite_fit_score(report.get("score"))
        or type(report.get("extraction_trustworthy")) is not bool
        or type(report.get("passed")) is not bool
        or report.get("recommendation") not in {"PROCEED", "REJECT"}
    ):
        return False, False

    as_of_date = report.get("as_of_date")
    if not isinstance(as_of_date, str):
        return False, False
    try:
        parsed_as_of_date = date.fromisoformat(as_of_date)
        if (
            parsed_as_of_date.isoformat() != as_of_date
            or parsed_as_of_date > date.today()
        ):
            return False, False
    except ValueError:
        return False, False

    components = report.get("component_scores")
    if (
        not isinstance(components, dict)
        or set(components) != _CANDIDATE_FIT_COMPONENT_KEYS
        or not all(_finite_fit_score(value) for value in components.values())
    ):
        return False, False

    hard_knockouts = report.get("hard_knockouts")
    if not isinstance(hard_knockouts, list):
        return False, False
    knockout_identities: set[tuple[str, str, str]] = set()
    for knockout in hard_knockouts:
        if (
            not isinstance(knockout, dict)
            or set(knockout) != _CANDIDATE_FIT_KNOCKOUT_KEYS
            or not all(
                isinstance(knockout.get(key), str) and bool(knockout[key])
                for key in ("code", "category", "requirement", "candidate_has")
            )
        ):
            return False, False
        identity = (
            knockout["code"],
            knockout["category"],
            knockout["requirement"],
        )
        if identity in knockout_identities:
            return False, False
        knockout_identities.add(identity)

    expected_codes: list[str] = []
    if report["extraction_trustworthy"] is False:
        expected_codes.append("UNTRUSTWORTHY_EXTRACTION")
    if hard_knockouts:
        expected_codes.append("HARD_KNOCKOUT")
    if float(report["score"]) < float(report["threshold"]):
        expected_codes.append("SCORE_BELOW_THRESHOLD")
    passed = not expected_codes
    if (
        report.get("codes") != expected_codes
        or report["passed"] is not passed
        or report["recommendation"] != ("PROCEED" if passed else "REJECT")
    ):
        return False, False
    return True, passed


def validate_recomputed_candidate_fit_report(
    report: Any,
    *,
    run_id: str,
    case_id: str,
    master_resume: str,
    job_description: str,
) -> tuple[bool, bool]:
    """Validate a report and require an exact deterministic recomputation.

    Candidate-fit reports are authorization evidence, not trusted inputs.  A
    structurally self-consistent report can still invent its score, component
    values, or knockout findings.  This boundary freezes every assessment
    input carried by the report and accepts it only when the canonical report
    exactly matches a fresh code-owned assessment.
    """

    valid, passed = validate_candidate_fit_report(
        report,
        run_id=run_id,
        case_id=case_id,
        master_resume=master_resume,
        job_description=job_description,
    )
    if not valid:
        return False, False
    try:
        recomputed = _deterministic_candidate_fit_assessment(
            master_resume,
            job_description,
            run_id=run_id,
            case_id=case_id,
            as_of_date=report["as_of_date"],
            resume_digest=report["resume_digest"],
            job_description_digest=report["job_description_digest"],
            extraction_trustworthy=report["extraction_trustworthy"],
        )
        exact = _canonical_json(report) == _canonical_json(recomputed)
    except Exception:
        return False, False
    if not exact:
        return False, False
    return True, passed


def _validate_authorization_report(
    report: Any,
    draft: str,
) -> tuple[bool, bool]:
    """Return ``(structurally_valid, unanimously_passed)`` for three votes."""

    if not isinstance(report, dict) or set(report) != _AUTHORIZATION_REPORT_KEYS:
        return False, False
    draft_digest = _digest(draft)
    if (
        report.get("schema_version") != AUTHORIZATION_VERSION
        or report.get("draft_digest") != draft_digest
        or type(report.get("passed")) is not bool
        or not isinstance(report.get("codes"), list)
        or not all(isinstance(code, str) and code for code in report["codes"])
        or not isinstance(report.get("votes"), list)
        or len(report["votes"]) != len(_VOTE_NAMES)
    ):
        return False, False

    invocation_ids: set[str] = set()
    flattened_codes: list[str] = []
    vote_passes: list[bool] = []
    for expected_name, vote in zip(_VOTE_NAMES, report["votes"]):
        if not isinstance(vote, dict) or set(vote) != {
            "schema_version",
            "name",
            "invocation_id",
            "passed",
            "draft_digest",
            "codes",
        }:
            return False, False
        invocation_id = vote.get("invocation_id")
        codes = vote.get("codes")
        if (
            vote.get("schema_version") != VOTE_VERSION
            or vote.get("name") != expected_name
            or not isinstance(invocation_id, str)
            or not invocation_id
            or invocation_id in invocation_ids
            or type(vote.get("passed")) is not bool
            or vote.get("draft_digest") != draft_digest
            or not isinstance(codes, list)
            or not all(isinstance(code, str) and code for code in codes)
            or (vote["passed"] and bool(codes))
            or (not vote["passed"] and not codes)
        ):
            return False, False
        invocation_ids.add(invocation_id)
        flattened_codes.extend(codes)
        vote_passes.append(vote["passed"])

    unanimous = all(vote_passes)
    structurally_valid = (
        report["codes"] == flattened_codes
        and report["passed"] is unanimous
        and (unanimous == (not flattened_codes))
    )
    if not structurally_valid:
        return False, False

    # Every failed vote/code pair is bound to one trusted finding.  Locations
    # are checked against the exact raw draft line so a forged line number,
    # line body, or digest fails the whole report.
    findings = report["findings"]
    if not isinstance(findings, list):
        return False, False
    if unanimous:
        return (not findings), True

    draft_lines = draft.splitlines()
    expected_pairs = {
        (vote["name"], code)
        for vote in report["votes"]
        for code in vote["codes"]
    }
    finding_pairs: list[tuple[str, str]] = []
    finding_ids: set[str] = set()
    for finding in findings:
        if (
            not isinstance(finding, dict)
            or set(finding) != _DETERMINISTIC_FINDING_KEYS
        ):
            return False, False
        finding_id = finding.get("id")
        vote_name = finding.get("vote_name")
        code = finding.get("code")
        actionable = finding.get("actionable")
        locations = finding.get("locations")
        if (
            not isinstance(finding_id, str)
            or not finding_id
            or finding_id in finding_ids
            or vote_name not in _VOTE_NAMES
            or not isinstance(code, str)
            or not code
            or type(actionable) is not bool
            or not isinstance(locations, list)
            or (actionable and not locations)
            or (not actionable and bool(locations))
        ):
            return False, False
        finding_ids.add(finding_id)
        finding_pairs.append((vote_name, code))
        seen_locations: set[int] = set()
        for location in locations:
            if (
                not isinstance(location, dict)
                or set(location) != _DETERMINISTIC_LOCATION_KEYS
            ):
                return False, False
            line_number = location.get("line_number")
            source_line = location.get("source_line")
            line_digest = location.get("line_digest")
            if (
                type(line_number) is not int
                or line_number <= 0
                or line_number > len(draft_lines)
                or line_number in seen_locations
                or not isinstance(source_line, str)
                or not source_line.strip()
                or draft_lines[line_number - 1] != source_line
                or not _is_sha256(line_digest)
                or line_digest != _digest(source_line)
            ):
                return False, False
            seen_locations.add(line_number)

    # One finding may contain several line locations, but each failed
    # vote/code pair has exactly one policy decision.  This also prevents an
    # unrelated finding from smuggling edit authority into the report.
    if len(finding_pairs) != len(set(finding_pairs)):
        return False, False
    if set(finding_pairs) != expected_pairs:
        return False, False
    return structurally_valid, unanimous


def _authorization_findings(
    report: dict[str, Any],
    draft: str,
) -> tuple[list[dict[str, Any]], str | None]:
    """Return trusted line-bound deterministic findings or a terminal code.

    The authorization report was already structurally validated.  This second
    check is deliberately repeated at the point where edit authority is
    minted.  Aggregate failures stay visible but can never authorize an
    arbitrary model rewrite.
    """

    if report.get("passed") is True:
        return [], None
    raw_findings = report.get("findings")
    failed_names = {
        vote.get("name")
        for vote in report.get("votes", [])
        if isinstance(vote, dict) and vote.get("passed") is False
    }
    nonactionable_code = (
        "NONACTIONABLE_HUMAN_VOICE"
        if failed_names == {"human_voice"}
        else "UNEDITABLE_DETERMINISTIC_AUDIT"
    )
    if not isinstance(raw_findings, list) or not raw_findings:
        return [], nonactionable_code

    draft_lines = draft.splitlines()
    normalized: list[dict[str, Any]] = []
    for finding in raw_findings:
        if not isinstance(finding, dict) or finding.get("actionable") is not True:
            return [], nonactionable_code
        locations = finding.get("locations")
        if not isinstance(locations, list) or not locations:
            return [], nonactionable_code
        for ordinal, location in enumerate(locations, start=1):
            if not isinstance(location, dict):
                return [], "UNEDITABLE_DETERMINISTIC_AUDIT"
            line_number = location.get("line_number")
            source_line = location.get("source_line")
            line_digest = location.get("line_digest")
            if (
                type(line_number) is not int
                or line_number <= 0
                or line_number > len(draft_lines)
                or not isinstance(source_line, str)
                or draft_lines[line_number - 1] != source_line
                or not _is_sha256(line_digest)
                or _digest(source_line) != line_digest
            ):
                return [], "UNEDITABLE_DETERMINISTIC_AUDIT"
            normalized.append(
                {
                    "id": f"D-{finding['id']}-L{ordinal}",
                    "code": finding["code"],
                    "evidence_text": source_line,
                    "line_number": line_number,
                    "line_digest": line_digest,
                    "authority": "deterministic",
                }
            )
    return normalized, None


def _validate_publication_receipt(receipt: Any, draft: str) -> bool:
    digest = _digest(draft)
    return (
        isinstance(receipt, dict)
        and set(receipt)
        == {
            "schema_version",
            "committed",
            "draft_digest",
            "target_digest",
            "publication_id",
        }
        and receipt.get("schema_version") == PUBLICATION_VERSION
        and receipt.get("committed") is True
        and receipt.get("draft_digest") == digest
        and receipt.get("target_digest") == digest
        and isinstance(receipt.get("publication_id"), str)
        and bool(receipt["publication_id"])
    )


def _final_receipt(
    request: dict[str, Any],
    draft: str,
    source_digest: str,
    researcher_packet: dict[str, Any],
    auditor_packet: dict[str, Any],
    authorization: dict[str, Any],
    publication_id: str,
    candidate_fit_report: dict[str, Any],
    candidate_fit_report_digest: str,
    candidate_fit_judge_report: dict[str, Any] | None = None,
    candidate_fit_judge_report_digest: str = "",
) -> dict[str, Any]:
    return {
        "schema_version": FINAL_RECEIPT_VERSION,
        "run_id": request["run_id"],
        "case_id": request["case_id"],
        "draft_digest": _digest(draft),
        "source_digest": source_digest,
        "job_description_digest": _digest(request["job_description"]),
        "candidate_fit_report": candidate_fit_report,
        "candidate_fit_report_digest": candidate_fit_report_digest,
        "candidate_fit_judge_report": candidate_fit_judge_report,
        "candidate_fit_judge_report_digest": (
            candidate_fit_judge_report_digest
            if candidate_fit_judge_report is not None
            else ""
        ),
        "researcher_agent_id": researcher_packet["agent_id"],
        "researcher_artifact_digest": researcher_packet["artifact_digest"],
        "auditor_attestation": {
            "agent_id": auditor_packet["agent_id"],
            "artifact_digest": auditor_packet["artifact_digest"],
            "verdict": auditor_packet["payload"]["verdict"],
            "draft_digest": auditor_packet["payload"]["draft_digest"],
        },
        "authorization_digest": _digest(authorization),
        "authorization_report": authorization,
        "vote_invocation_ids": [
            vote["invocation_id"] for vote in authorization["votes"]
        ],
        "publication_id": publication_id,
        "verified_target_digest": _digest(draft),
    }


def _validate_publication_verification(
    verification: Any,
    publication_receipt: dict[str, Any],
    expected_authorization_receipt: dict[str, Any],
) -> bool:
    return (
        isinstance(verification, dict)
        and set(verification)
        == {
            "schema_version",
            "verified",
            "publication_id",
            "target_digest",
            "authorization_receipt",
            "authorization_receipt_digest",
            "authorization_receipt_path",
        }
        and verification.get("schema_version")
        == PUBLICATION_VERIFICATION_VERSION
        and verification.get("verified") is True
        and verification.get("publication_id")
        == publication_receipt["publication_id"]
        and verification.get("target_digest")
        == expected_authorization_receipt["verified_target_digest"]
        and verification.get("authorization_receipt")
        == expected_authorization_receipt
        and verification.get("authorization_receipt_digest")
        == _digest(expected_authorization_receipt)
        and isinstance(verification.get("authorization_receipt_path"), str)
        and _RECEIPT_BASENAME_RE.fullmatch(
            verification["authorization_receipt_path"]
        )
        is not None
    )


def _editor_change_is_scoped(
    previous: str,
    edited: str,
    findings: list[dict[str, Any]],
    master: str,
    claim_evidence: Any,
) -> bool:
    """Allow only evidence-linked replacements/deletions of authorized lines.

    Findings reach this function only after the coordinator has resolved them
    to an exact 1-based line number, raw line body, and raw-line digest.  Using
    positions as well as content matters when identical bullets occur more
    than once: authority for one occurrence must not authorize changing a
    different occurrence.  Every replacement line must remain provenance
    valid against a source span that also supports the consumed finding line.
    """

    if (
        not isinstance(master, str)
        or not isinstance(claim_evidence, list)
        or not _candidate_draft_format_valid(master, previous)
        or not _candidate_draft_format_valid(master, edited)
    ):
        return False
    # Split on the only admitted separator and retain a trailing empty element.
    # That makes adding/removing a terminal LF a byte-level layout edit which
    # must itself be authorized rather than disappearing under ``splitlines``.
    previous_lines = previous.split("\n")
    edited_lines = edited.split("\n")
    previous_raw_lines = previous_lines
    authorized: dict[int, str] = {}
    for finding in findings:
        if not isinstance(finding, dict):
            return False
        line_number = finding.get("line_number")
        source_line = finding.get("evidence_text")
        line_digest = finding.get("line_digest")
        if (
            type(line_number) is not int
            or line_number <= 0
            or line_number > len(previous_raw_lines)
            or not isinstance(source_line, str)
            or not source_line.strip()
            or previous_raw_lines[line_number - 1] != source_line
            or not _is_sha256(line_digest)
            or _digest(source_line) != line_digest
        ):
            return False
        zero_based = line_number - 1
        existing = authorized.get(zero_based)
        if existing is not None and existing != source_line:
            return False
        authorized[zero_based] = source_line
    if not authorized:
        return False

    evidence_by_claim = {
        item.get("claim_digest"): item
        for item in claim_evidence
        if isinstance(item, dict) and set(item) == _CLAIM_EVIDENCE_KEYS
    }
    master_lines = _line_counts(master)
    replacement_cache: dict[tuple[str, str], bool] = {}

    def replacement_is_valid(old_raw: str, new_raw: str) -> bool:
        cache_key = (old_raw, new_raw)
        if cache_key in replacement_cache:
            return replacement_cache[cache_key]
        line = new_raw.strip()
        if not line:
            replacement_cache[cache_key] = False
            return False
        if line in master_lines:
            source_text = line
        else:
            item = evidence_by_claim.get(_digest(line))
            if not isinstance(item, dict):
                replacement_cache[cache_key] = False
                return False
            start = item.get("source_start")
            end = item.get("source_end")
            if (
                type(start) is not int
                or type(end) is not int
                or start < 0
                or end <= start
                or end > len(master)
            ):
                replacement_cache[cache_key] = False
                return False
            source_text = master[start:end]
        valid = claim_supported_by_source(old_raw.strip(), source_text)[0]
        replacement_cache[cache_key] = valid
        return valid

    # Parse the edited byte-level LF line sequence as a transformation of the
    # previous one.  An unauthorized line has exactly one transition: copy the
    # same raw body.  An authorized line must be deleted or replaced by at most
    # one provenance-valid raw body.  Dynamic programming avoids ambiguous LCS
    # alignment when duplicate bullets are present while granting no insertion
    # or unrelated newline authority.
    reachable: set[tuple[int, int]] = {(0, 0)}
    terminal = (len(previous_lines), len(edited_lines))
    while reachable:
        old_index, new_index = reachable.pop()
        if (old_index, new_index) == terminal:
            return True
        if old_index >= len(previous_lines):
            continue
        old_raw = previous_lines[old_index]
        if old_index not in authorized:
            if (
                new_index < len(edited_lines)
                and edited_lines[new_index] == old_raw
            ):
                reachable.add((old_index + 1, new_index + 1))
            continue

        # Deletion consumes only the exact authorized source occurrence.
        reachable.add((old_index + 1, new_index))
        # Replacement must actually differ; an unchanged line cannot satisfy a
        # claimed finding even if the Editor lists its ID as addressed.
        if new_index < len(edited_lines):
            new_raw = edited_lines[new_index]
            if new_raw != old_raw and replacement_is_valid(old_raw, new_raw):
                reachable.add((old_index + 1, new_index + 1))
    return False


def run_team(request: dict, adapter: object, services: object) -> dict:
    """Run one bounded Researcher -> Writer -> Auditor -> Editor workflow.

    ``adapter`` must expose ``invoke(role, context, timeout_seconds)``.
    ``services`` is the trusted application boundary and must expose
    ``claim_run``, ``attest_source``, ``audit_draft``, ``publish``,
    ``verify_publication``, and ``record_event``.  Role packets are validated by
    this module rather than by an injected callback.  Every authorization is
    three named, independently identified, digest-bound votes.  All failures
    become stable, non-sensitive terminal classes; no exception or partial
    success escapes as a successful result.  A failed ``audit_draft`` report
    may authorize correction only through its digest-bound ``findings`` field;
    aggregate failure reports remain terminal and uneditable.
    """

    if not _valid_request(request):
        safe_request = request if isinstance(request, dict) else {}
        return _result(safe_request, "FAILED:REQUEST_SCHEMA")

    try:
        claim_run = services.claim_run
        attest_source = services.attest_source
        audit_draft = services.audit_draft
        record_event = services.record_event
        publish = services.publish
        verify_publication = services.verify_publication
        invoke_adapter = adapter.invoke
    except Exception:
        return _result(request, "FAILED:SERVICE_UNAVAILABLE")
    try:
        assess_fit = services.assess_candidate_fit
    except Exception:
        return _result(request, "FAILED:CANDIDATE_FIT_PREFLIGHT")

    try:
        run_claim = claim_run(request["run_id"], request["case_id"])
    except Exception:
        return _result(request, "FAILED:RUN_CLAIM")
    if not _validate_run_claim(run_claim, request):
        if isinstance(run_claim, dict) and run_claim.get("claimed") is False:
            return _result(request, "FAILED:REPLAY")
        return _result(request, "FAILED:RUN_CLAIM")

    try:
        source_attestation = attest_source(request["master_resume"])
    except Exception:
        return _result(request, "FAILED:SOURCE_ATTESTATION")
    if not _validate_source_attestation(
        source_attestation, request["master_resume"]
    ):
        return _result(request, "FAILED:SOURCE_ATTESTATION")

    try:
        candidate_fit_report = assess_fit(
            request["master_resume"],
            request["job_description"],
            request["run_id"],
            request["case_id"],
        )
    except Exception:
        return _result(request, "FAILED:CANDIDATE_FIT_PREFLIGHT")
    fit_valid, fit_passed = validate_recomputed_candidate_fit_report(
        candidate_fit_report,
        run_id=request["run_id"],
        case_id=request["case_id"],
        master_resume=request["master_resume"],
        job_description=request["job_description"],
    )
    if not fit_valid:
        return _result(request, "FAILED:CANDIDATE_FIT_PREFLIGHT")
    candidate_fit_report_digest = _digest(candidate_fit_report)
    candidate_fit_judge_report: dict[str, Any] | None = None
    candidate_fit_judge_report_digest = ""
    if not fit_passed:
        # The deterministic scanner reads postings with regexes and is
        # demoted to advisory on rejection: a reasoning judge, when the
        # trusted services expose one, holds the final refusal decision.
        # Its verdict is untrusted model output until it survives
        # validate_candidate_fit_judge_report, which digest-binds it to this
        # exact run/resume/JD and mechanically verifies every cited
        # requirement against the posting text. No valid judge verdict —
        # service absent, errored, or citation check failed — means the
        # deterministic rejection stands unchanged: the gate degrades to
        # its old behavior, never to an open gate.
        judge_service = getattr(services, "judge_candidate_fit", None)
        judge_report: Any = None
        if callable(judge_service):
            try:
                judge_report = judge_service(
                    request["master_resume"],
                    request["job_description"],
                    request["run_id"],
                    request["case_id"],
                    candidate_fit_report,
                )
            except Exception:
                judge_report = None
        judge_valid = False
        judge_proceed = False
        if judge_report is not None:
            judge_valid, judge_proceed = validate_candidate_fit_judge_report(
                judge_report,
                run_id=request["run_id"],
                case_id=request["case_id"],
                master_resume=request["master_resume"],
                job_description=request["job_description"],
            )
        if judge_valid:
            candidate_fit_judge_report = json.loads(
                _canonical_json(judge_report)
            )
            candidate_fit_judge_report_digest = _digest(
                candidate_fit_judge_report
            )
        if not (judge_valid and judge_proceed):
            return _result(
                request,
                "REJECTED:CANDIDATE_FIT",
                candidate_fit_report=candidate_fit_report,
                candidate_fit_report_digest=candidate_fit_report_digest,
                candidate_fit_judge_report=candidate_fit_judge_report,
                candidate_fit_judge_report_digest=(
                    candidate_fit_judge_report_digest
                ),
            )

    seen_agent_ids: set[str] = set()
    seen_packets: set[str] = set()
    seen_vote_ids: set[str] = set()
    research_payload: dict[str, Any] = {}
    researcher_host: str | None = None

    def fail(terminal: str) -> dict[str, Any]:
        return _result(
            request,
            terminal,
            candidate_fit_report=candidate_fit_report,
            candidate_fit_report_digest=candidate_fit_report_digest,
            candidate_fit_judge_report=candidate_fit_judge_report,
            candidate_fit_judge_report_digest=candidate_fit_judge_report_digest,
        )

    def invoke_role(
        role: str,
        attempt: int,
        payload: dict[str, Any],
    ) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
        nonlocal researcher_host
        try:
            context = build_context(
                run_id=request["run_id"],
                case_id=request["case_id"],
                role=role,
                attempt=attempt,
                payload=payload,
            )
        except Exception:
            return None, fail("FAILED:HANDOFF_PROVENANCE")
        try:
            response = invoke_adapter(
                role,
                context,
                request["role_timeout_seconds"],
            )
        except TimeoutError:
            return None, fail("FAILED:AGENT_TIMEOUT")
        except PermissionError:
            code = "AUDITOR_SIDE_EFFECT" if role == "auditor" else "AGENT_CRASH"
            return None, fail(f"FAILED:{code}")
        except NoSafeWriterChanges:
            if role == "writer":
                return None, fail("REJECTED:NO_SAFE_CHANGES")
            return None, fail("FAILED:AGENT_CRASH")
        except AgentInvocationFailure as error:
            code = (
                _SCHEMA_CODES[role]
                if error.code == "AGENT_PAYLOAD_SCHEMA"
                else error.code
            )
            return None, fail(f"FAILED:{code}")
        except (LookupError, ConnectionError):
            return None, fail("FAILED:AGENT_UNAVAILABLE")
        except Exception:
            return None, fail("FAILED:AGENT_CRASH")

        if response is None:
            return None, fail("FAILED:AGENT_UNAVAILABLE")
        if not isinstance(response, dict):
            return None, fail(f"FAILED:{_SCHEMA_CODES[role]}")

        response_role = response.get("role")
        response_agent = response.get("agent_id")
        if response_role != role:
            return None, fail("FAILED:ROLE_SEPARATION")
        if not isinstance(response_agent, str) or not response_agent:
            return None, fail(f"FAILED:{_SCHEMA_CODES[role]}")
        if response_agent in seen_agent_ids:
            return None, fail("FAILED:ROLE_SEPARATION")
        if role in {"researcher", "auditor"}:
            response_host = _native_agent_host(response_agent)
            if response_host is None:
                return None, fail("FAILED:ROLE_SEPARATION")
            if role == "researcher":
                researcher_host = response_host
            elif researcher_host is None or response_host != researcher_host:
                return None, fail("FAILED:ROLE_SEPARATION")

        try:
            checked = validate_handoff(role, response, context)
        except Exception:
            return None, fail(f"FAILED:{_SCHEMA_CODES[role]}")
        if not isinstance(checked, dict) or checked.get("valid") is not True:
            code = (
                checked.get("code", _SCHEMA_CODES[role])
                if isinstance(checked, dict)
                else _SCHEMA_CODES[role]
            )
            return None, fail(f"FAILED:{code}")

        packet_digest = _digest(response)
        if packet_digest in seen_packets:
            return None, fail("FAILED:HANDOFF_REPLAY")
        seen_agent_ids.add(response_agent)
        seen_packets.add(packet_digest)
        return response, None

    def event(name: str, payload: dict[str, Any]) -> dict[str, Any] | None:
        try:
            record_event(name, payload)
            return None
        except Exception:
            return fail("FAILED:PREPUBLICATION")

    def invoke_auditor(
        current_draft: str,
        attempt: int,
    ) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
        response, stopped = invoke_role(
            "auditor",
            attempt,
            {
                "master_resume": request["master_resume"],
                "researcher_artifact": research_payload,
                "writer_draft": current_draft,
            },
        )
        if stopped:
            return None, stopped
        assert response is not None
        payload = response.get("payload")
        if not isinstance(payload, dict):
            return None, fail("FAILED:AUDITOR_SCHEMA")
        verdict = payload.get("verdict")
        findings = payload.get("findings")
        if (
            verdict not in {"PASS", "FAIL"}
            or not isinstance(findings, list)
            or (verdict == "PASS") != (not findings)
        ):
            return None, fail("FAILED:AUDIT_AMBIGUOUS")
        if payload.get("draft_digest") != _digest(current_draft):
            return None, fail("FAILED:AUDITOR_SCHEMA")
        return response, None

    def authorize_candidate(
        current_draft: str,
    ) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
        try:
            report = audit_draft(current_draft)
        except Exception:
            return None, fail("FAILED:DETERMINISTIC_AUDIT")
        structurally_valid, _ = _validate_authorization_report(
            report, current_draft
        )
        if not structurally_valid:
            return None, fail("FAILED:AUTHORIZATION_REPORT")
        vote_ids = {
            vote["invocation_id"] for vote in report["votes"]
        }
        if vote_ids & seen_vote_ids:
            return None, fail("FAILED:STALE_AUTHORIZATION")
        seen_vote_ids.update(vote_ids)
        return report, None

    def authorization_event_summary(report: dict[str, Any]) -> str:
        """Return only deterministic vote names/status codes for diagnostics."""

        parts: list[str] = []
        for vote in report["votes"]:
            codes = vote["codes"]
            outcome = "PASS" if vote["passed"] is True else ",".join(codes)
            parts.append(f"{vote['name']}:{outcome}")
        return "|".join(parts)

    def publish_candidate(
        current_draft: str,
        auditor_packet: dict[str, Any],
        authorization: dict[str, Any],
    ) -> dict[str, Any]:
        try:
            fresh_attestation = attest_source(request["master_resume"])
        except Exception:
            return fail("FAILED:SOURCE_ATTESTATION")
        if not _validate_source_attestation(
            fresh_attestation, request["master_resume"]
        ):
            return fail("FAILED:SOURCE_ATTESTATION")
        draft_digest = _digest(current_draft)
        terminal = event(
            "pre_publish",
            {
                "draft_digest": draft_digest,
                "auditor_artifact_digest": auditor_packet["artifact_digest"],
                "authorization_digest": _digest(authorization),
            },
        )
        if terminal:
            return terminal
        metadata = {
            "schema_version": AUTHORIZATION_VERSION,
            "run_id": request["run_id"],
            "case_id": request["case_id"],
            "draft_digest": draft_digest,
            "source_digest": fresh_attestation["source_digest"],
            "job_description_digest": _digest(request["job_description"]),
            "candidate_fit_report": candidate_fit_report,
            "candidate_fit_report_digest": candidate_fit_report_digest,
            "candidate_fit_judge_report": candidate_fit_judge_report,
            "candidate_fit_judge_report_digest": (
                candidate_fit_judge_report_digest
            ),
            "researcher_agent_id": researcher["agent_id"],
            "researcher_artifact_digest": researcher["artifact_digest"],
            "auditor_attestation": {
                "agent_id": auditor_packet["agent_id"],
                "artifact_digest": auditor_packet["artifact_digest"],
                "verdict": auditor_packet["payload"]["verdict"],
                "draft_digest": auditor_packet["payload"]["draft_digest"],
            },
            "authorization_digest": _digest(authorization),
            "authorization_report": authorization,
            "vote_invocation_ids": [
                vote["invocation_id"] for vote in authorization["votes"]
            ],
        }
        try:
            receipt = publish(current_draft, metadata)
        except Exception:
            return fail("FAILED:PUBLICATION_ATOMICITY")
        if not _validate_publication_receipt(receipt, current_draft):
            return fail("FAILED:PUBLICATION_RECEIPT")
        expected_authorization_receipt = _final_receipt(
            request,
            current_draft,
            fresh_attestation["source_digest"],
            researcher,
            auditor_packet,
            authorization,
            receipt["publication_id"],
            candidate_fit_report,
            candidate_fit_report_digest,
            candidate_fit_judge_report,
            candidate_fit_judge_report_digest,
        )
        try:
            verification = verify_publication(receipt["publication_id"])
        except Exception:
            return fail("FAILED:PUBLICATION_VERIFICATION")
        if not _validate_publication_verification(
            verification,
            receipt,
            expected_authorization_receipt,
        ):
            return fail("FAILED:PUBLICATION_VERIFICATION")
        return _result(
            request,
            "PUBLISHED",
            published=True,
            final_draft=current_draft,
            authorization_receipt=expected_authorization_receipt,
            authorization_receipt_digest=verification[
                "authorization_receipt_digest"
            ],
            authorization_receipt_path=verification[
                "authorization_receipt_path"
            ],
            candidate_fit_report=candidate_fit_report,
            candidate_fit_report_digest=candidate_fit_report_digest,
            candidate_fit_judge_report=candidate_fit_judge_report,
            candidate_fit_judge_report_digest=candidate_fit_judge_report_digest,
        )

    def combined_findings(
        auditor_packet: dict[str, Any],
        authorization: dict[str, Any],
        current_draft: str,
    ) -> tuple[list[dict[str, Any]], str | None]:
        draft_lines = current_draft.splitlines()
        findings: list[dict[str, Any]] = []
        for finding in auditor_packet["payload"]["findings"]:
            matches = [
                (index, line)
                for index, line in enumerate(draft_lines, start=1)
                if line.strip()
                and _digest(line.strip()) == finding["evidence_digest"]
            ]
            # The model protocol historically binds findings to a complete
            # line digest rather than a line number.  Only a unique occurrence
            # in the candidate can safely be converted into edit authority.
            if len(matches) != 1:
                return [], "AUDIT_AMBIGUOUS"
            line_number, source_line = matches[0]
            findings.append(
                {
                    "id": f"A-{finding['id']}",
                    "code": finding["code"],
                    "evidence_text": source_line,
                    "line_number": line_number,
                    "line_digest": _digest(source_line),
                    "authority": "auditor",
                }
            )

        deterministic, terminal_code = _authorization_findings(
            authorization,
            current_draft,
        )
        if terminal_code:
            return [], terminal_code
        findings.extend(deterministic)
        return findings, None

    def findings_terminal(code: str) -> dict[str, Any]:
        if code == "AUDIT_AMBIGUOUS":
            return fail("FAILED:AUDIT_AMBIGUOUS")
        return fail(f"REJECTED:{code}")

    researcher, terminal = invoke_role(
        "researcher",
        0,
        {"job_description": request["job_description"]},
    )
    if terminal:
        return terminal
    assert researcher is not None
    research_payload = researcher["payload"]
    terminal = event(
        "research_complete",
        {
            "agent_id": researcher["agent_id"],
            "artifact_digest": researcher["artifact_digest"],
        },
    )
    if terminal:
        return terminal

    writer, terminal = invoke_role(
        "writer",
        0,
        {
            "master_resume": request["master_resume"],
            "researcher_artifact": research_payload,
        },
    )
    if terminal:
        return terminal
    assert writer is not None
    draft = writer["payload"].get("draft")
    if not isinstance(draft, str):
        return fail("FAILED:WRITER_SCHEMA")
    try:
        writer_stats = _admit_writer_stats(
            getattr(adapter, "writer_stats", None)
        )
    except Exception:
        writer_stats = None
    writer_event = {
        "agent_id": writer["agent_id"],
        "artifact_digest": writer["artifact_digest"],
        "draft_digest": _digest(draft),
    }
    if writer_stats is not None:
        writer_event["writer_stats"] = writer_stats
    terminal = event(
        "writer_complete",
        writer_event,
    )
    if terminal:
        return terminal

    seen_drafts = {_digest(draft)}
    auditor, terminal = invoke_auditor(draft, 0)
    if terminal:
        return terminal
    assert auditor is not None
    authorization, terminal = authorize_candidate(draft)
    if terminal:
        return terminal
    assert authorization is not None
    terminal = event(
        "audit_complete",
        {
            "auditor_agent_id": auditor["agent_id"],
            "auditor_artifact_digest": auditor["artifact_digest"],
            "authorization_digest": _digest(authorization),
            "authorization_codes": authorization_event_summary(authorization),
            "draft_digest": _digest(draft),
        },
    )
    if terminal:
        return terminal

    auditor_payload = auditor["payload"]
    if authorization["passed"] is True and auditor_payload["verdict"] == "PASS":
        return publish_candidate(draft, auditor, authorization)
    findings, findings_error = combined_findings(auditor, authorization, draft)
    if findings_error:
        return findings_terminal(findings_error)
    if not findings:
        return fail("FAILED:AUDIT_AMBIGUOUS")
    last_codes = [str(row.get("code")) for row in findings if row.get("code")]

    for editor_attempt in range(1, request["max_editor_attempts"] + 1):
        editor, terminal = invoke_role(
            "editor",
            editor_attempt,
            {
                "master_resume": request["master_resume"],
                "writer_draft": draft,
                "audit_findings": findings,
            },
        )
        if terminal:
            return terminal
        assert editor is not None
        editor_payload = editor["payload"]
        edited = editor_payload.get("draft")
        addressed = editor_payload.get("addressed_finding_ids")
        expected_findings = {
            str(row.get("id")) for row in findings if isinstance(row, dict)
        }
        if (
            not isinstance(edited, str)
            or not isinstance(addressed, list)
            or expected_findings != {str(value) for value in addressed}
        ):
            return fail("FAILED:EDITOR_SCHEMA")
        if not _editor_change_is_scoped(
            draft,
            edited,
            findings,
            request["master_resume"],
            editor_payload.get("claim_evidence"),
        ):
            return fail("REJECTED:EDITOR_OVERREACH")

        edited_digest = _digest(edited)
        if edited_digest in seen_drafts:
            return fail("REJECTED:CORRECTION_CYCLE")
        seen_drafts.add(edited_digest)
        terminal = event(
            "editor_complete",
            {
                "agent_id": editor["agent_id"],
                "artifact_digest": editor["artifact_digest"],
                "draft_digest": edited_digest,
            },
        )
        if terminal:
            return terminal

        auditor, terminal = invoke_auditor(edited, editor_attempt)
        if terminal:
            return terminal
        assert auditor is not None
        edited_authorization, terminal = authorize_candidate(edited)
        if terminal:
            return terminal
        assert edited_authorization is not None
        terminal = event(
            "audit_complete",
            {
                "auditor_agent_id": auditor["agent_id"],
                "auditor_artifact_digest": auditor["artifact_digest"],
                "authorization_digest": _digest(edited_authorization),
                "authorization_codes": authorization_event_summary(
                    edited_authorization
                ),
                "draft_digest": edited_digest,
            },
        )
        if terminal:
            return terminal

        draft = edited
        auditor_payload = auditor["payload"]
        if (
            edited_authorization["passed"] is True
            and auditor_payload.get("verdict") == "PASS"
        ):
            return publish_candidate(draft, auditor, edited_authorization)
        findings, findings_error = combined_findings(
            auditor,
            edited_authorization,
            draft,
        )
        if findings_error:
            return findings_terminal(findings_error)
        if not findings:
            return fail("FAILED:AUDIT_AMBIGUOUS")
        last_codes = [str(row.get("code")) for row in findings if row.get("code")]

    terminal_code = last_codes[0] if last_codes else "CORRECTION_LIMIT"
    return fail(f"REJECTED:{terminal_code}")


__all__ = [
    "AgentInvocationFailure",
    "AUTHORIZATION_VERSION",
    "CONTEXT_VERSION",
    "FINAL_RECEIPT_VERSION",
    "HANDOFF_VERSION",
    "MAX_EDITOR_ATTEMPTS",
    "NoSafeWriterChanges",
    "PROTOCOL_VERSION",
    "PUBLICATION_VERIFICATION_VERSION",
    "PUBLICATION_VERSION",
    "RESULT_VERSION",
    "ROLE_ORDER",
    "RUN_CLAIM_VERSION",
    "SOURCE_ATTESTATION_VERSION",
    "VOTE_VERSION",
    "build_context",
    "build_handoff",
    "canonical_digest",
    "compile_writer_replacements_salvage",
    "normalize_native_payload",
    "run_team",
    "validate_candidate_fit_report",
    "validate_candidate_fit_judge_report",
    "validate_recomputed_candidate_fit_report",
    "validate_handoff",
]
