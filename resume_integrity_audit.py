#!/usr/bin/env python3
"""Fail-closed canonical identity audit for tailored resumes.

Diagnostics intentionally contain only stable codes, counts, and digests.  The
canonical resume facts themselves are never included in a report.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import unicodedata
from collections import Counter
from pathlib import Path
from typing import Any


PROTECTED_SECTIONS = {
    "PROFESSIONAL EXPERIENCE": "roles",
    "WORK EXPERIENCE": "roles",
    "EXPERIENCE": "roles",
    "EDUCATION": "education",
    "CERTIFICATIONS & LICENSURE": "certifications",
    "CERTIFICATIONS & TRAINING": "certifications",
    "CERTIFICATIONS": "certifications",
    "LICENSURE": "certifications",
    "PUBLICATIONS": "publications",
    "PROFESSIONAL MEMBERSHIPS": "memberships",
    "MEMBERSHIPS": "memberships",
}
UNPROTECTED_SECTIONS = {
    "PROFESSIONAL SUMMARY", "SUMMARY", "CORE COMPETENCIES", "COMPETENCIES",
    "PROJECTS", "SKILLS", "AWARDS", "HONORS", "LANGUAGES", "VOLUNTEER",
}
DATE_RE = re.compile(
    r"\b(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|"
    r"Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|Nov(?:ember)?|"
    r"Dec(?:ember)?|Present|\d{4})\b",
    re.IGNORECASE,
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
    re.IGNORECASE,
)


class AuditInputError(Exception):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def _clean(value: str, *, leading: bool = False) -> str:
    value = unicodedata.normalize("NFKC", value).replace("\u00a0", " ")
    value = value.replace("\t", " | ")
    value = re.sub(r"[‐‑‒–—―]", "-", value)
    if leading:
        value = re.sub(r"^\s*(?:#{1,6}\s+|[-*+•]\s+|\d+[.)]\s+)", "", value)
    value = re.sub(r"(?<!\w)[*_`]+|[*_`]+(?!\w)", "", value)
    value = re.sub(r"\s*\|\s*", " | ", value)
    value = re.sub(r"(?<=\w)\s*-\s*(?=\w)", " - ", value)
    return re.sub(r"\s+", " ", value).strip()


def _heading(line: str) -> str | None:
    candidate = _clean(line, leading=True).rstrip(":").strip().upper()
    return PROTECTED_SECTIONS.get(candidate)


def _known_heading(line: str) -> bool:
    candidate = _clean(line, leading=True).rstrip(":").strip().upper()
    return candidate in PROTECTED_SECTIONS or candidate in UNPROTECTED_SECTIONS


def _is_separator(line: str) -> bool:
    return bool(re.fullmatch(r"\s*(?:_{3,}|-{3,}|={3,})\s*", line))


def _split_sections(text: str) -> dict[str, list[str]]:
    sections: dict[str, list[str]] = {}
    current: str | None = None
    seen: set[str] = set()
    for raw in text.splitlines():
        if _is_separator(raw):
            continue
        section = _heading(raw)
        if section:
            if section in seen:
                raise AuditInputError("DUPLICATE_PROTECTED_SECTION")
            seen.add(section)
            sections[section] = []
            current = section
        elif _known_heading(raw):
            current = None
        elif current is not None:
            sections[current].append(raw.rstrip())
    return sections


def _nonempty(lines: list[str]) -> list[str]:
    return [line for line in lines if line.strip()]


def _is_bullet(line: str) -> bool:
    return bool(re.match(r"^\s*(?:[-*+•]|\d+[.)])\s+", line))


def _is_role_date_line(value: str) -> bool:
    return bool(_ROLE_DATE_ONLY_RE.fullmatch(value.strip()))


def _parse_roles(lines: list[str]) -> Counter[tuple[str, str, str]]:
    rows = _nonempty(lines)
    roles: Counter[tuple[str, str, str]] = Counter()
    i = 0
    while i < len(rows):
        raw = rows[i]
        clean = _clean(raw, leading=True)
        if _is_bullet(raw):
            i += 1
            continue
        parts = [_clean(part) for part in clean.split("|")]
        parts = [part for part in parts if part]

        # Rendered layout: title<TAB>dates, followed by company | location.
        if (
            len(parts) >= 2
            and _is_role_date_line(parts[-1])
        ):
            if i + 1 < len(rows) and not _is_bullet(rows[i + 1]):
                company_parts = [_clean(p) for p in _clean(rows[i + 1]).split("|") if _clean(p)]
                if company_parts and not _is_role_date_line(company_parts[0]):
                    roles[(parts[0], company_parts[0], parts[-1])] += 1
                    i += 2
                    # The production renderer may preserve location on its own
                    # paragraph between company and the first experience bullet.
                    if i + 1 < len(rows) and not _is_bullet(rows[i]) and _is_bullet(rows[i + 1]):
                        i += 1
                    continue

        # Source layout: title | company [| location ...], then dates.
        if len(parts) >= 2 and not _is_role_date_line(parts[-1]):
            j = i + 1
            while j < len(rows) and _is_bullet(rows[j]):
                j += 1
            date_text = _clean(rows[j], leading=True) if j < len(rows) else ""
            if date_text and DATE_RE.search(date_text) and not _is_role_date_line(
                date_text
            ):
                raise AuditInputError("AMBIGUOUS_ROLE_LAYOUT")
            if j < len(rows) and _is_role_date_line(date_text) and not _is_bullet(rows[j]):
                roles[(parts[0], parts[1], date_text)] += 1
                i = j + 1
                continue

        # Standalone title + company/location + dates.
        if len(parts) == 1 and not _is_role_date_line(clean) and i + 2 < len(rows):
            company_line = _clean(rows[i + 1], leading=True)
            date_line = _clean(rows[i + 2], leading=True)
            if date_line and DATE_RE.search(date_line) and not _is_role_date_line(
                date_line
            ):
                raise AuditInputError("AMBIGUOUS_ROLE_LAYOUT")
            if not _is_bullet(rows[i + 1]) and _is_role_date_line(date_line):
                company_parts = [_clean(p) for p in company_line.split("|") if _clean(p)]
                if company_parts and not _is_role_date_line(company_parts[0]):
                    roles[(clean, company_parts[0], date_line)] += 1
                    i += 3
                    continue
        i += 1
    return roles


def _parse_education(lines: list[str]) -> Counter[tuple[str, str, str]]:
    rows = _nonempty(lines)
    result: Counter[tuple[str, str, str]] = Counter()
    i = 0
    while i < len(rows):
        first = _clean(rows[i], leading=True)
        if not first:
            i += 1
            continue
        # Rendered degree<TAB>dates.
        first_parts = [_clean(p) for p in first.split("|") if _clean(p)]
        degree, dates = first_parts[0], ""
        if len(first_parts) > 1 and DATE_RE.search(first_parts[-1]):
            dates = first_parts[-1]
        if i + 1 >= len(rows):
            result[(degree, "", dates)] += 1
            i += 1
            continue
        second = _clean(rows[i + 1], leading=True)
        second_parts = [_clean(p) for p in second.split("|") if _clean(p)]
        school = second_parts[0] if second_parts else second
        if not dates and len(second_parts) > 1 and DATE_RE.search(second_parts[-1]):
            dates = second_parts[-1]
        result[(degree, school, dates)] += 1
        i += 2
    return result


def _parse_simple(lines: list[str]) -> Counter[str]:
    return Counter(_clean(line, leading=True) for line in lines if _clean(line, leading=True))


def _parse_publications(lines: list[str]) -> Counter[str]:
    entries: list[str] = []
    current: str | None = None
    for raw in lines:
        if not raw.strip():
            if current:
                entries.append(current)
                current = None
            continue
        stripped = raw.strip()
        numbered = bool(re.match(r"^\d+[.)]\s+", stripped))
        bullet = bool(re.match(r"^[-*+•]\s+", stripped))
        cleaned = _clean(raw, leading=True)
        if cleaned.lower().startswith(("full publication", "full list")):
            if current:
                entries.append(current)
                current = None
            entries.append(cleaned)
        elif numbered or bullet:
            if current:
                entries.append(current)
            current = cleaned
        elif current is not None and (raw[:1].isspace() or not re.search(r"[.!?]$", current)):
            current = f"{current} {cleaned}"
        else:
            if current:
                entries.append(current)
            current = cleaned
    if current:
        entries.append(current)
    return Counter(entries)


def parse_resume_identity(text: str) -> dict[str, Counter[Any]]:
    if not isinstance(text, str):
        raise AuditInputError("UNREADABLE_ARTIFACT")
    if not text.strip():
        raise AuditInputError("EMPTY_ARTIFACT")
    sections = _split_sections(text)
    identity = {
        "roles": _parse_roles(sections.get("roles", [])),
        "education": _parse_education(sections.get("education", [])),
        "publications": _parse_publications(sections.get("publications", [])),
        "certifications": _parse_simple(sections.get("certifications", [])),
        "memberships": _parse_simple(sections.get("memberships", [])),
    }
    if not any(identity.values()):
        raise AuditInputError("NO_CANONICAL_CONTENT")
    return identity


def _digest(counter: Counter[Any]) -> str:
    encoded = sorted((repr(key), count) for key, count in counter.items())
    return hashlib.sha256(json.dumps(encoded, separators=(",", ":")).encode()).hexdigest()


def _role_code(master: Counter[Any], tailored: Counter[Any]) -> str:
    missing = list((master - tailored).elements())
    added = list((tailored - master).elements())
    if len(missing) >= 2 and added:
        return "ROLE_MERGED"
    if len(missing) == len(added) == 1:
        before, after = missing[0], added[0]
        differences = [i for i in range(3) if before[i] != after[i]]
        if differences == [0]:
            return "ROLE_TITLE_CHANGED"
        if differences == [1]:
            return "ROLE_COMPANY_CHANGED"
        if differences == [2]:
            return "ROLE_DATES_CHANGED"
    return "ROLE_MISSING"


def _section_code(label: str, master: Counter[Any], tailored: Counter[Any]) -> str:
    missing = master - tailored
    added = tailored - master
    if missing and not added:
        return f"{label}_MISSING"
    if added and not missing:
        return f"{label}_ADDED"
    return f"{label}_CHANGED"


def _error_report(code: str, *, internal: bool = False) -> dict[str, Any]:
    return {
        "passed": False,
        "exit_code": 2 if internal else 1,
        "difference_codes": [code],
        "counts": {},
        "digests": {},
    }


def audit_resume_text(master_text: str, tailored_text: str) -> dict[str, Any]:
    try:
        master = parse_resume_identity(master_text)
        tailored = parse_resume_identity(tailored_text)
    except AuditInputError as exc:
        return _error_report(exc.code)
    codes: list[str] = []
    if master["roles"] != tailored["roles"]:
        codes.append(_role_code(master["roles"], tailored["roles"]))
    for key, label in (
        ("education", "EDUCATION"),
        ("publications", "PUBLICATION"),
        ("certifications", "CERTIFICATION"),
        ("memberships", "MEMBERSHIP"),
    ):
        if master[key] != tailored[key]:
            codes.append(_section_code(label, master[key], tailored[key]))
    counts = {
        side: {key: sum(values[key].values()) for key in values}
        for side, values in (("master", master), ("tailored", tailored))
    }
    digests = {
        side: {key: _digest(values[key]) for key in values}
        for side, values in (("master", master), ("tailored", tailored))
    }
    return {
        "passed": not codes,
        "exit_code": 0 if not codes else 1,
        "difference_codes": codes,
        "counts": counts,
        "digests": digests,
    }


def resolve_master_path(config_path: str = "config.json") -> Path:
    path = Path(config_path).expanduser().resolve()
    try:
        config = json.loads(path.read_text(encoding="utf-8"))
        configured = config["master_resume_path"]
    except Exception as exc:
        raise AuditInputError("UNREADABLE_ARTIFACT") from exc
    master = Path(configured).expanduser()
    return master.resolve() if master.is_absolute() else (path.parent / master).resolve()


def _read_artifact(path_value: str | Path) -> str:
    path = Path(path_value)
    suffix = path.suffix.lower()
    if suffix not in {".md", ".txt", ".docx"}:
        raise AuditInputError("UNSUPPORTED_FORMAT")
    if suffix == ".docx":
        try:
            from docx import Document

            document = Document(str(path))
            lines = []
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
        except Exception as exc:
            raise AuditInputError("UNREADABLE_ARTIFACT") from exc
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise AuditInputError("INVALID_ENCODING") from exc
    except Exception as exc:
        raise AuditInputError("UNREADABLE_ARTIFACT") from exc


def audit_resume_files(
    *, tailored_path: str | Path, master_path: str | Path | None = None,
    config_path: str = "config.json"
) -> dict[str, Any]:
    try:
        resolved_master = Path(master_path) if master_path is not None else resolve_master_path(config_path)
        master_text = _read_artifact(resolved_master)
        tailored_text = _read_artifact(tailored_path)
    except AuditInputError as exc:
        return _error_report(exc.code)
    except Exception:
        return _error_report("UNREADABLE_ARTIFACT", internal=True)
    return audit_resume_text(master_text, tailored_text)


def format_audit_report(report: dict[str, Any]) -> str:
    safe = {
        "passed": bool(report.get("passed")),
        "exit_code": int(report.get("exit_code", 2)),
        "difference_codes": list(report.get("difference_codes", [])),
        "counts": report.get("counts", {}),
        "digests": report.get("digests", {}),
    }
    return json.dumps(safe, sort_keys=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Audit tailored resume canonical identity")
    parser.add_argument("path", nargs="?", help="tailored resume path")
    parser.add_argument("--tailored", help="tailored resume path")
    parser.add_argument("--config", default="config.json", help="configuration path")
    args = parser.parse_args(argv)
    tailored = args.tailored or args.path
    if not tailored or (args.tailored and args.path):
        parser.error("provide exactly one tailored path")
    report = audit_resume_files(tailored_path=tailored, config_path=args.config)
    print(format_audit_report(report))
    return int(report["exit_code"])


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        print(format_audit_report(_error_report("UNREADABLE_ARTIFACT", internal=True)))
        sys.exit(2)
