#!/usr/bin/env python3
"""Capability-isolated native runtime for the four-role resume team.

The runtime uses the installed Codex or Claude Code subscription CLI.  It does
not use a vendor SDK and removes API-key/provider credentials from every child
environment.  Models return proposals only; this process owns provenance,
three deterministic authorization votes, replay prevention, and publication.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import uuid
from dataclasses import dataclass
from datetime import date
from io import BytesIO
from pathlib import Path
from typing import Any, Callable, Sequence

from human_voice_audit import audit_text as _trusted_human_voice_audit
from evidence_audit import audit_text as _trusted_evidence_audit
from candidate_fit_preflight import assess_candidate_fit as _trusted_candidate_fit_assessment
from resume_integrity_audit import audit_resume_text as _trusted_resume_integrity_audit

try:
    import fcntl
except ModuleNotFoundError:  # pragma: no cover - runtime is POSIX-only today
    fcntl = None  # type: ignore[assignment]

try:  # Python 3.11+
    import tomllib
except ModuleNotFoundError:  # Python 3.10
    import tomli as tomllib

from multi_agent_team import (
    AgentInvocationFailure,
    AUTHORIZATION_VERSION,
    FINAL_RECEIPT_VERSION,
    PROTOCOL_VERSION,
    PUBLICATION_VERIFICATION_VERSION,
    PUBLICATION_VERSION,
    RESULT_VERSION,
    ROLE_ORDER,
    RUN_CLAIM_VERSION,
    SOURCE_ATTESTATION_VERSION,
    VOTE_VERSION,
    build_handoff,
    canonical_digest,
    normalize_native_payload,
    run_team,
    validate_recomputed_candidate_fit_report,
    validate_handoff,
)


HOST_CHECK_VERSION = "resume-team-host-check/v1"
MAX_CAPTURE_BYTES = 8 * 1024 * 1024
MAX_JOB_BYTES = 2 * 1024 * 1024
MAX_MASTER_BYTES = 20 * 1024 * 1024
_VOTE_NAMES = ("evidence", "human_voice", "canonical_integrity")
_CANDIDATE_FIT_REQUIRED_KEYS = {
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
_SAFE_ID = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_HUMAN_VOICE_CODE = re.compile(r"^[a-z][a-z0-9_]{0,127}$")
_HUMAN_VOICE_ACTIONABLE_CODES = frozenset(
    {
        "banned_lexicon",
        "cliche_openers",
        "formulaic_summary",
        "hard_bullet_cap",
        "phrase_stuffing",
        "repeated_skeletons",
        "summary_too_long",
        "summary_too_many_sentences",
        "synonym_padding",
    }
)

_CREDENTIAL_ENV_KEYS = {
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "OPENAI_API_KEY",
    "AZURE_OPENAI_API_KEY",
    "AWS_ACCESS_KEY_ID",
    "AWS_SECRET_ACCESS_KEY",
    "AWS_SESSION_TOKEN",
    "GOOGLE_APPLICATION_CREDENTIALS",
    "CLAUDE_CODE_USE_BEDROCK",
    "CLAUDE_CODE_USE_VERTEX",
    "CLAUDE_CODE_USE_FOUNDRY",
    "ANTHROPIC_BASE_URL",
    "OPENAI_BASE_URL",
    "OPENAI_API_BASE",
}
_CREDENTIAL_ENV_PREFIXES = (
    "ANTHROPIC_",
    "OPENAI_",
    "AZURE_OPENAI_",
    "AWS_",
    "VERTEX_",
    "FOUNDRY_",
    "CLAUDE_CODE_USE_",
)

# These are present in the bundled Codex build used by the app.  Preflight
# fails if a future/older build cannot prove that each switch exists.
CODEX_DISABLED_FEATURES = (
    "shell_tool",
    "unified_exec",
    "apps",
    "browser_use",
    "browser_use_external",
    "browser_use_full_cdp_access",
    "computer_use",
    "image_generation",
    "in_app_browser",
    "multi_agent",
    "enable_mcp_apps",
    "tool_call_mcp_elicitation",
    "plugins",
    "skill_search",
    "workspace_dependencies",
    "hooks",
    "goals",
    "memories",
    "remote_plugin",
    "skill_mcp_dependency_install",
    "network_proxy",
    "code_mode",
    "code_mode_host",
    "tool_suggest",
    "request_permissions_tool",
    "auth_elicitation",
    "guardian_approval",
)


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    )


def _native_output_schema(role: str) -> dict[str, Any]:
    """Return the exact model-facing payload schema for one native role.

    The coordinator still performs all semantic, provenance, and digest checks.
    This schema only constrains the host model's response to the keys and basic
    types that :func:`normalize_native_payload` accepts, preventing prose or a
    different role's payload from reaching that boundary.
    """

    # Keep this to the structured-output subset shared by the supported CLIs.
    # Non-emptiness and every cross-field relationship remain coordinator gates.
    text_value = {"type": "string"}
    claim_evidence = {
        "type": "array",
        "items": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "claim_text": text_value,
                "source_span_text": text_value,
            },
            "required": ["claim_text", "source_span_text"],
        },
    }
    if role == "researcher":
        return {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "rubric": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "hard_requirements": {
                            "type": "array",
                            "items": text_value,
                        },
                        "soft_requirements": {
                            "type": "array",
                            "items": text_value,
                        },
                    },
                    "required": ["hard_requirements", "soft_requirements"],
                },
                "jd_evidence_spans": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "properties": {"evidence_text": text_value},
                        "required": ["evidence_text"],
                    },
                },
            },
            "required": ["rubric", "jd_evidence_spans"],
        }
    if role == "writer":
        return {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "draft": text_value,
                "claim_evidence": claim_evidence,
            },
            "required": ["draft", "claim_evidence"],
        }
    if role == "auditor":
        return {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "verdict": {"type": "string", "enum": ["PASS", "FAIL"]},
                "findings": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "properties": {
                            "id": text_value,
                            "code": text_value,
                            "evidence_text": text_value,
                        },
                        "required": ["id", "code", "evidence_text"],
                    },
                },
                "audited_draft": text_value,
            },
            "required": ["verdict", "findings", "audited_draft"],
        }
    if role == "editor":
        return {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "draft": text_value,
                "addressed_finding_ids": {
                    "type": "array",
                    "items": text_value,
                },
                "claim_evidence": claim_evidence,
            },
            "required": [
                "draft",
                "addressed_finding_ids",
                "claim_evidence",
            ],
        }
    raise ValueError("unsupported role")


def _write_private_json(path: Path, value: Any) -> None:
    """Create one owner-only, non-symlink JSON file and durably close it."""

    encoded = _canonical_json(value).encode("utf-8")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags, 0o600)
    try:
        offset = 0
        while offset < len(encoded):
            written = os.write(descriptor, encoded[offset:])
            if written <= 0:
                raise OSError("short schema write")
            offset += written
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    info = os.lstat(path)
    if (
        stat.S_ISLNK(info.st_mode)
        or not stat.S_ISREG(info.st_mode)
        or info.st_size != len(encoded)
        or stat.S_IMODE(info.st_mode) & 0o077
    ):
        raise ValueError("unsafe native output schema")


def _sha_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sanitized_environment() -> dict[str, str]:
    environment = dict(os.environ)
    for key in list(environment):
        upper = key.upper()
        if (
            key in _CREDENTIAL_ENV_KEYS
            or upper.endswith("_API_KEY")
            or upper.startswith(_CREDENTIAL_ENV_PREFIXES)
        ):
            environment.pop(key, None)
    environment["PYTHONIOENCODING"] = "utf-8"
    return environment


@dataclass(frozen=True)
class ProcessResult:
    returncode: int
    stdout: str
    stderr: str
    pid: int
    timed_out: bool = False


ProcessRunner = Callable[..., ProcessResult]


def _run_process(
    command: Sequence[str],
    *,
    input_text: str,
    timeout: float,
    cwd: Path,
    env: dict[str, str],
) -> ProcessResult:
    """Run one child without a shell and kill its process group on timeout."""

    process = subprocess.Popen(
        list(command),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        cwd=str(cwd),
        env=env,
        start_new_session=True,
    )
    timed_out = False
    try:
        stdout, stderr = process.communicate(input=input_text, timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        try:
            if hasattr(os, "killpg"):
                os.killpg(process.pid, signal.SIGKILL)
            else:  # pragma: no cover - Windows fallback is best effort
                process.kill()
        except ProcessLookupError:
            pass
        stdout, stderr = process.communicate()
    if len(stdout.encode("utf-8")) + len(stderr.encode("utf-8")) > MAX_CAPTURE_BYTES:
        return ProcessResult(125, "", "", process.pid, timed_out)
    return ProcessResult(process.returncode, stdout, stderr, process.pid, timed_out)


def _resolve_executable(host: str, explicit: str | None = None) -> Path:
    candidates: list[str] = []
    if explicit:
        candidates.append(explicit)
    elif host == "codex":
        # Prefer the signed app-bundled binary over a potentially stale npm shim.
        candidates.extend(
            [
                "/Applications/ChatGPT.app/Contents/Resources/codex",
                shutil.which("codex") or "",
            ]
        )
    else:
        candidates.append(shutil.which("claude") or "")
    for candidate in candidates:
        if not candidate:
            continue
        resolved = Path(candidate).expanduser().resolve()
        try:
            mode = resolved.stat().st_mode
        except OSError:
            continue
        if stat.S_ISREG(mode) and os.access(resolved, os.X_OK):
            return resolved
    raise FileNotFoundError(f"{host} executable unavailable")


def _read_regular_bytes(path: Path, maximum: int) -> bytes:
    resolved = path.expanduser().resolve(strict=True)
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(resolved, flags)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_size > maximum:
            raise ValueError("invalid input artifact")
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(descriptor, min(1024 * 1024, maximum + 1 - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > maximum:
                raise ValueError("input artifact too large")
        return b"".join(chunks)
    finally:
        os.close(descriptor)


def _read_regular_nofollow_bytes(path: Path, maximum: int) -> bytes:
    """Read one stable regular inode without following the final component."""

    before = os.lstat(path)
    if stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode):
        raise ValueError("invalid input artifact")
    if before.st_size > maximum:
        raise ValueError("input artifact too large")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino)
            or opened.st_size > maximum
        ):
            raise ValueError("invalid input artifact")
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(descriptor, min(1024 * 1024, maximum + 1 - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > maximum:
                raise ValueError("input artifact too large")
        after = os.fstat(descriptor)
        if (
            (after.st_dev, after.st_ino) != (opened.st_dev, opened.st_ino)
            or after.st_size != opened.st_size
            or after.st_mtime_ns != opened.st_mtime_ns
        ):
            raise ValueError("input artifact changed")
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


@dataclass(frozen=True)
class MasterSnapshot:
    config_path: Path
    config_digest: str
    master_path: Path
    master_bytes_digest: str
    text: str
    text_digest: str


def load_master_snapshot(
    config_path: str | Path,
    *,
    require_canonical_layout: bool = True,
) -> MasterSnapshot:
    config = Path(config_path).expanduser().resolve(strict=True)
    config_bytes = _read_regular_bytes(config, 1024 * 1024)
    parsed = json.loads(config_bytes.decode("utf-8"))
    configured = parsed.get("master_resume_path") if isinstance(parsed, dict) else None
    if not isinstance(configured, str) or not configured:
        raise ValueError("invalid master resume configuration")
    candidate = Path(configured).expanduser()
    master = (
        candidate.resolve(strict=True)
        if candidate.is_absolute()
        else (config.parent / candidate).resolve(strict=True)
    )
    suffix = master.suffix.lower()
    if suffix not in {".md", ".txt", ".docx"}:
        raise ValueError("unsupported master resume format")
    master_bytes = _read_regular_bytes(master, MAX_MASTER_BYTES)
    if suffix == ".docx":
        text = _extract_docx(master_bytes)
    else:
        text = master_bytes.decode("utf-8")
    if not text.strip():
        raise ValueError("empty master resume")
    if require_canonical_layout:
        integrity = _trusted_resume_integrity_audit(text, text)
        role_count = (
            integrity.get("counts", {})
            .get("master", {})
            .get("roles")
            if isinstance(integrity, dict)
            else None
        )
        if (
            not isinstance(integrity, dict)
            or integrity.get("passed") is not True
            or type(role_count) is not int
            or role_count <= 0
        ):
            raise ValueError("master resume canonical layout is invalid")
    return MasterSnapshot(
        config_path=config,
        config_digest=_sha_bytes(config_bytes),
        master_path=master,
        master_bytes_digest=_sha_bytes(master_bytes),
        text=text,
        text_digest=canonical_digest(text),
    )


def _load_codex_role(project_root: Path, role: str) -> str:
    role_path = project_root / ".codex" / "agents" / f"resume-{role}.toml"
    raw = _read_regular_bytes(role_path, 256 * 1024)
    data = tomllib.loads(raw.decode("utf-8"))
    if (
        data.get("name") != f"resume-{role}"
        or data.get("sandbox_mode") != "read-only"
        or "model" in data
        or "model_reasoning_effort" in data
        or not isinstance(data.get("developer_instructions"), str)
        or not data["developer_instructions"].strip()
        or "\x00" in data["developer_instructions"]
    ):
        raise ValueError("invalid Codex role definition")
    return data["developer_instructions"]


def _load_claude_role(project_root: Path, role: str) -> dict[str, Any]:
    role_path = project_root / ".claude" / "agents" / f"resume-{role}.md"
    text = _read_regular_bytes(role_path, 256 * 1024).decode("utf-8")
    if not text.startswith("---\n") or "\n---\n" not in text[4:]:
        raise ValueError("invalid Claude role definition")
    frontmatter, prompt = text.split("\n---\n", 1)
    frontmatter = frontmatter[4:]
    fields: dict[str, str] = {}
    for line in frontmatter.splitlines():
        if ":" in line:
            key, value = line.split(":", 1)
            fields[key.strip()] = value.strip()
    if (
        fields.get("name") != f"resume-{role}"
        or "model" in fields
        or fields.get("tools") != "[]"
        or not fields.get("description")
        or not prompt.strip()
    ):
        raise ValueError("invalid Claude role definition")
    return {
        f"resume-{role}": {
            "description": fields["description"],
            "prompt": prompt.strip(),
            "tools": [],
        }
    }


def _strict_json_object(value: str) -> dict[str, Any]:
    parsed = json.loads(value)
    if not isinstance(parsed, dict):
        raise ValueError("native output is not an object")
    return parsed


def _unbound_audit_finding(
    vote_name: str,
    code: str,
    index: int,
) -> dict[str, Any]:
    """Return a deterministic finding that grants no Editor authority."""

    prefixes = {
        "evidence": "EV",
        "human_voice": "HV",
        "canonical_integrity": "CI",
    }
    return {
        "id": f"D-{prefixes[vote_name]}-{index:02d}",
        "vote_name": vote_name,
        "code": code,
        "actionable": False,
        "locations": [],
    }


def _parse_evidence_result(
    result: ProcessResult,
    candidate: Path,
) -> tuple[bool, list[str], list[dict[str, Any]]]:
    """Recompute evidence support and bind unsupported competency rows.

    The subprocess remains an independent vote. The trusted coordinator
    recomputes its decision from the exact candidate and grants edit authority
    only when every unsupported item maps to one complete Core Competencies
    line. Structural or ambiguous failures remain visible but uneditable.
    """

    if result.timed_out:
        code = "EVIDENCE_AUDIT_TIMEOUT"
        return False, [code], [_unbound_audit_finding("evidence", code, 1)]
    if result.returncode not in {0, 1}:
        code = "EVIDENCE_AUDIT_ERROR"
        return False, [code], [_unbound_audit_finding("evidence", code, 1)]
    if result.returncode == 0:
        return True, [], []

    try:
        draft = candidate.read_text(encoding="utf-8")
        report = _trusted_evidence_audit(draft)
    except Exception:
        code = "EVIDENCE_AUDIT_MALFORMED"
        return False, [code], [_unbound_audit_finding("evidence", code, 1)]

    passed = report.get("passed")
    unsupported = report.get("unsupported")
    structural_errors = report.get("structural_errors")
    if (
        type(passed) is not bool
        or not isinstance(unsupported, list)
        or not all(isinstance(item, str) and item for item in unsupported)
        or not isinstance(structural_errors, list)
        or not all(isinstance(item, str) and item for item in structural_errors)
        or passed is not False
    ):
        code = "EVIDENCE_AUDIT_MALFORMED"
        return False, [code], [_unbound_audit_finding("evidence", code, 1)]
    code = "EVIDENCE_AUDIT_FAILED"
    if structural_errors or not unsupported:
        return False, [code], [_unbound_audit_finding("evidence", code, 1)]

    matches: dict[str, list[dict[str, Any]]] = {item: [] for item in unsupported}
    in_core = False
    for line_number, raw_line in enumerate(draft.splitlines(), start=1):
        stripped = raw_line.strip()
        is_heading = (
            stripped
            and 3 < len(stripped) < 60
            and stripped == stripped.upper()
            and any(character.isalpha() for character in stripped)
            and not stripped.startswith(("•", "-", "*", "_", "─", "═"))
        )
        if is_heading:
            in_core = "CORE COMPETENC" in stripped
            continue
        if not in_core or not stripped:
            continue
        cleaned = re.sub(r"^[•\-*]\s*", "", stripped)
        items = [item.strip() for item in cleaned.split("|") if item.strip()]
        for unsupported_item in unsupported:
            if unsupported_item in items:
                matches[unsupported_item].append(
                    {
                        "line_number": line_number,
                        "source_line": raw_line,
                        "line_digest": _sha_bytes(raw_line.encode("utf-8")),
                    }
                )

    if any(len(locations) != 1 for locations in matches.values()):
        return False, [code], [_unbound_audit_finding("evidence", code, 1)]
    unique_locations = {
        location["line_number"]: location
        for locations in matches.values()
        for location in locations
    }
    locations = [unique_locations[key] for key in sorted(unique_locations)]
    return (
        False,
        [code],
        [
            {
                "id": "D-EV-01",
                "vote_name": "evidence",
                "code": code,
                "actionable": True,
                "locations": locations,
            }
        ],
    )


def _parse_human_voice_result(
    result: ProcessResult,
    candidate: Path,
) -> tuple[bool, list[str], list[dict[str, Any]]]:
    """Validate the JSON audit contract and bind findings to exact draft lines.

    Exit 1 with a well-formed failing report is a content failure.  Timeouts,
    non-audit exits, malformed JSON, exit/report disagreement, or an invalid
    line binding are infrastructure failures and never grant edit authority.
    """

    if result.timed_out:
        code = "HUMAN_VOICE_AUDIT_TIMEOUT"
        return False, [code], [_unbound_audit_finding("human_voice", code, 1)]
    if result.returncode not in {0, 1}:
        code = "HUMAN_VOICE_AUDIT_ERROR"
        return False, [code], [_unbound_audit_finding("human_voice", code, 1)]

    malformed_code = "HUMAN_VOICE_AUDIT_MALFORMED"

    def malformed() -> tuple[bool, list[str], list[dict[str, Any]]]:
        return (
            False,
            [malformed_code],
            [_unbound_audit_finding("human_voice", malformed_code, 1)],
        )

    try:
        report = _strict_json_object(result.stdout)
    except Exception:
        return malformed()

    # The subprocess is an independent vote, but its claimed edit scope is not
    # itself authority. Recompute the exact report inside the trusted runtime
    # and require byte-source-equivalent diagnostics before accepting any line
    # binding. This closes forged aggregate-to-line mappings even when the
    # supplied line number and digest are individually valid.
    try:
        trusted_report = _trusted_human_voice_audit(
            candidate.read_text(encoding="utf-8"),
            mode="resume",
        )
        trusted_report["path"] = str(candidate)
    except Exception:
        return malformed()
    if report != trusted_report:
        return malformed()

    expected_report_keys = {
        "passed",
        "mode",
        "failures",
        "warnings",
        "stats",
        "human_voice_score",
        "bullet_count",
        "path",
    }
    failures = report.get("failures")
    warnings = report.get("warnings")
    score = report.get("human_voice_score")
    bullet_count = report.get("bullet_count")
    if (
        set(report) != expected_report_keys
        or type(report.get("passed")) is not bool
        or report.get("mode") != "resume"
        or not isinstance(failures, list)
        or not isinstance(warnings, list)
        or not all(isinstance(warning, str) for warning in warnings)
        or not isinstance(report.get("stats"), dict)
        or type(score) not in {int, float}
        or type(bullet_count) is not int
        or bullet_count < 0
        or report.get("path") != str(candidate)
        or (result.returncode == 0) != report["passed"]
        or report["passed"] != (not failures)
    ):
        return malformed()

    if report["passed"]:
        return True, [], []

    draft_lines = candidate.read_text(encoding="utf-8").splitlines()
    codes: list[str] = []
    findings: list[dict[str, Any]] = []
    for failure_index, failure in enumerate(failures, start=1):
        if not isinstance(failure, dict):
            return malformed()
        required_keys = {"code", "message", "actionable", "locations"}
        if not required_keys <= set(failure) or not set(failure) <= (
            required_keys | {"fix"}
        ):
            return malformed()
        code = failure.get("code")
        message = failure.get("message")
        actionable = failure.get("actionable")
        locations = failure.get("locations")
        if (
            not isinstance(code, str)
            or not _HUMAN_VOICE_CODE.fullmatch(code)
            or code in codes
            or not isinstance(message, str)
            or not message
            or type(actionable) is not bool
            or not isinstance(locations, list)
            or actionable != bool(locations)
            or (actionable and code not in _HUMAN_VOICE_ACTIONABLE_CODES)
        ):
            return malformed()

        checked_locations: list[dict[str, Any]] = []
        previous_line_number = 0
        for location in locations:
            if not isinstance(location, dict) or set(location) != {
                "line_number",
                "source_line",
                "line_digest",
            }:
                return malformed()
            line_number = location.get("line_number")
            source_line = location.get("source_line")
            line_digest = location.get("line_digest")
            if (
                type(line_number) is not int
                or line_number <= previous_line_number
                or line_number > len(draft_lines)
                or not isinstance(source_line, str)
                or not source_line
                or draft_lines[line_number - 1] != source_line
                or not isinstance(line_digest, str)
                or not _SHA256.fullmatch(line_digest)
                or _sha_bytes(source_line.encode("utf-8")) != line_digest
            ):
                return malformed()
            checked_locations.append(
                {
                    "line_number": line_number,
                    "source_line": source_line,
                    "line_digest": line_digest,
                }
            )
            previous_line_number = line_number

        codes.append(code)
        findings.append(
            {
                "id": f"D-HV-{failure_index:02d}",
                "vote_name": "human_voice",
                "code": code,
                "actionable": actionable,
                "locations": checked_locations,
            }
        )

    return False, codes, findings


class NativeRoleAdapter:
    """Invoke one capability-isolated native role and wrap its raw payload."""

    def __init__(
        self,
        *,
        host: str,
        project_root: str | Path,
        cli_path: str | None = None,
        runner: ProcessRunner = _run_process,
        model: str | None = None,
        reasoning_effort: str | None = None,
    ) -> None:
        if host not in {"codex", "claude"}:
            raise ValueError("unsupported host")
        if host != "codex" and (model or reasoning_effort):
            raise ValueError("model and reasoning effort overrides require Codex")
        if reasoning_effort and reasoning_effort != "ultra":
            raise ValueError("invalid reasoning effort")
        self.host = host
        self.project_root = Path(project_root).expanduser().resolve(strict=True)
        self.executable = _resolve_executable(host, cli_path)
        self.runner = runner
        self.model = model
        self.reasoning_effort = reasoning_effort
        self._seen_invocations: set[str] = set()

    def _codex_command(
        self,
        isolated_root: Path,
        role: str,
        output_schema_path: Path,
    ) -> list[str]:
        instructions = _load_codex_role(self.project_root, role)
        command = [
            str(self.executable),
            "exec",
            "--ignore-user-config",
            "--ephemeral",
            "--sandbox",
            "read-only",
            "--skip-git-repo-check",
            "--ignore-rules",
            "--json",
            "--output-schema",
            str(output_schema_path),
            "--color",
            "never",
            "-C",
            str(isolated_root),
            "-c",
            'approval_policy="never"',
            "-c",
            "mcp_servers={}",
            "-c",
            'web_search="disabled"',
            "-c",
            "notify=[]",
            "-c",
            'forced_login_method="chatgpt"',
            "-c",
            f"developer_instructions={json.dumps(instructions, ensure_ascii=True)}",
        ]
        for feature in CODEX_DISABLED_FEATURES:
            command.extend(["--disable", feature])
        if self.model:
            command.extend(["--model", self.model])
        if self.reasoning_effort:
            command.extend(
                ["-c", f"model_reasoning_effort={json.dumps(self.reasoning_effort)}"]
            )
        command.append("-")
        return command

    def _parse_codex(self, result: ProcessResult) -> tuple[str, dict[str, Any]]:
        if result.timed_out:
            raise TimeoutError("native role timed out")
        if result.returncode != 0:
            raise ConnectionError("Codex role unavailable")
        events: list[dict[str, Any]] = []
        for line in result.stdout.splitlines():
            if not line.strip():
                continue
            try:
                event = _strict_json_object(line)
            except Exception as error:
                raise AgentInvocationFailure("AGENT_EVENT_MALFORMED") from error
            events.append(event)
        thread_ids = {
            event.get("thread_id")
            for event in events
            if event.get("type") == "thread.started"
            and isinstance(event.get("thread_id"), str)
        }
        messages: list[str] = []
        forbidden = False
        completed = False
        for event in events:
            if event.get("type") == "turn.completed":
                completed = True
            item = event.get("item")
            if not isinstance(item, dict):
                continue
            item_type = str(item.get("type", ""))
            if item_type not in {"agent_message", "reasoning"}:
                forbidden = True
            if item_type == "agent_message" and isinstance(item.get("text"), str):
                messages.append(item["text"])
        if len(thread_ids) != 1 or len(messages) != 1 or not completed or forbidden:
            raise LookupError("Codex role identity or isolation unavailable")
        thread_id = next(iter(thread_ids))
        if not _SAFE_ID.fullmatch(thread_id):
            raise LookupError("invalid Codex invocation identity")
        try:
            raw_payload = _strict_json_object(messages[0])
        except Exception as error:
            raise AgentInvocationFailure("AGENT_OUTPUT_MALFORMED") from error
        return f"codex:{thread_id}", raw_payload

    def _claude_command(self, role: str) -> list[str]:
        inline_agent = _load_claude_role(self.project_root, role)
        return [
            str(self.executable),
            "--safe-mode",
            "--setting-sources",
            "",
            "--print",
            "--agents",
            _canonical_json(inline_agent),
            "--agent",
            f"resume-{role}",
            "--tools",
            "",
            "--output-format",
            "json",
            "--json-schema",
            _canonical_json(_native_output_schema(role)),
            "--permission-mode",
            "dontAsk",
            "--no-session-persistence",
            "--disable-slash-commands",
            "--no-chrome",
            "--strict-mcp-config",
            "--mcp-config",
            '{"mcpServers":{}}',
        ]

    def _parse_claude(self, result: ProcessResult) -> tuple[str, dict[str, Any]]:
        if result.timed_out:
            raise TimeoutError("native role timed out")
        if result.returncode != 0:
            raise ConnectionError("Claude role unavailable")
        try:
            outer = _strict_json_object(result.stdout)
        except Exception as error:
            raise AgentInvocationFailure("AGENT_EVENT_MALFORMED") from error
        session_id = outer.get("session_id")
        raw = outer.get("result")
        structured_output = outer.get("structured_output")
        usage = outer.get("usage")
        server_tool_use = usage.get("server_tool_use") if isinstance(usage, dict) else None

        def all_zero(value: Any) -> bool:
            if isinstance(value, dict):
                return all(all_zero(item) for item in value.values())
            if isinstance(value, list):
                return all(all_zero(item) for item in value)
            return type(value) in {int, float} and value == 0

        if (
            outer.get("type") != "result"
            or outer.get("is_error") is not False
            or not isinstance(session_id, str)
            or not _SAFE_ID.fullmatch(session_id)
            or (
                structured_output is None
                and not isinstance(raw, str)
            )
            or (
                structured_output is not None
                and not isinstance(structured_output, dict)
            )
            or ("permission_denials" in outer and outer["permission_denials"] != [])
            or ("num_turns" in outer and outer["num_turns"] != 1)
            or (server_tool_use is not None and not all_zero(server_tool_use))
        ):
            raise LookupError("Claude role identity or isolation unavailable")
        if isinstance(structured_output, dict):
            raw_payload = structured_output
        else:
            try:
                raw_payload = _strict_json_object(raw)
            except Exception as error:
                raise AgentInvocationFailure("AGENT_OUTPUT_MALFORMED") from error
        return f"claude:{session_id}", raw_payload

    def invoke(
        self,
        role: str,
        context: dict[str, Any],
        timeout_seconds: float,
    ) -> dict[str, Any]:
        if role not in ROLE_ORDER or context.get("role") != role:
            raise ValueError("invalid role context")
        input_text = _canonical_json(context)
        environment = _sanitized_environment()
        if self.host == "codex":
            with tempfile.TemporaryDirectory(prefix=f"resume-team-{role}-") as directory:
                isolated_root = Path(directory)
                output_schema_path = isolated_root / "native-output.schema.json"
                _write_private_json(output_schema_path, _native_output_schema(role))
                result = self.runner(
                    self._codex_command(
                        isolated_root,
                        role,
                        output_schema_path,
                    ),
                    input_text=input_text,
                    timeout=timeout_seconds,
                    cwd=isolated_root,
                    env=environment,
                )
            agent_id, raw_payload = self._parse_codex(result)
        else:
            with tempfile.TemporaryDirectory(prefix=f"resume-team-{role}-") as directory:
                result = self.runner(
                    self._claude_command(role),
                    input_text=input_text,
                    timeout=timeout_seconds,
                    cwd=Path(directory),
                    env=environment,
                )
            agent_id, raw_payload = self._parse_claude(result)
        if agent_id in self._seen_invocations:
            raise LookupError("reused native invocation identity")
        try:
            normalized = normalize_native_payload(role, raw_payload, context)
            envelope = build_handoff(
                context=context,
                role=role,
                agent_id=agent_id,
                payload=normalized,
            )
            if validate_handoff(role, envelope, context).get("valid") is not True:
                raise ValueError("invalid normalized native handoff")
        except AgentInvocationFailure:
            raise
        except Exception as error:
            raise AgentInvocationFailure("AGENT_PAYLOAD_SCHEMA") from error
        self._seen_invocations.add(agent_id)
        return envelope


def _ensure_private_directory(path: Path) -> Path:
    target = path.expanduser().absolute()
    if target.is_symlink():
        raise ValueError("unsafe state directory")
    target.mkdir(parents=True, exist_ok=True, mode=0o700)
    info = os.lstat(target)
    if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):
        raise ValueError("unsafe state directory")
    resolved = target.resolve(strict=True)
    try:
        os.chmod(resolved, 0o700)
    except OSError:
        pass
    return resolved


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


class LocalTrustedServices:
    """Trusted local boundary required by :func:`multi_agent_team.run_team`."""

    _EVENT_KEYS = {
        "research_complete": {"agent_id", "artifact_digest"},
        "writer_complete": {"agent_id", "artifact_digest", "draft_digest"},
        "auditor_complete": {"agent_id", "artifact_digest", "draft_digest"},
        "editor_complete": {"agent_id", "artifact_digest", "draft_digest"},
        "audit_complete": {
            "auditor_agent_id",
            "auditor_artifact_digest",
            "authorization_digest",
            "authorization_codes",
            "draft_digest",
        },
        "pre_publish": {
            "draft_digest",
            "auditor_artifact_digest",
            "authorization_digest",
        },
    }

    def __init__(
        self,
        *,
        project_root: str | Path,
        config_path: str | Path,
        master: MasterSnapshot,
        output_dir: str | Path,
        state_dir: str | Path,
        run_id: str,
        case_id: str,
        job_description: str,
        candidate_fit_report: dict[str, Any],
        runner: ProcessRunner = _run_process,
        audit_timeout: float = 120.0,
    ) -> None:
        self.project_root = Path(project_root).expanduser().resolve(strict=True)
        self.config_path = Path(config_path).expanduser().resolve(strict=True)
        self.master = master
        fit_valid, _ = validate_recomputed_candidate_fit_report(
            candidate_fit_report,
            run_id=run_id,
            case_id=case_id,
            master_resume=master.text,
            job_description=job_description,
        )
        if not fit_valid:
            raise ValueError("invalid trusted candidate-fit assessment")
        self._job_description = job_description
        self._candidate_fit_report = json.loads(
            _canonical_json(candidate_fit_report)
        )
        self.candidate_fit_report_digest = canonical_digest(
            self._candidate_fit_report
        )
        output = Path(output_dir).expanduser().absolute()
        if output.is_symlink():
            raise ValueError("unsafe output directory")
        output.mkdir(parents=True, exist_ok=True)
        output_info = os.lstat(output)
        if not stat.S_ISDIR(output_info.st_mode) or stat.S_ISLNK(output_info.st_mode):
            raise ValueError("unsafe output directory")
        self.output_dir = output.resolve(strict=True)
        if not self.output_dir.is_dir():
            raise ValueError("invalid output directory")
        self.state_dir = _ensure_private_directory(Path(state_dir).expanduser())
        self.claim_dir = _ensure_private_directory(self.state_dir / "claims")
        self.audit_temp_dir = _ensure_private_directory(self.state_dir / "audit-tmp")
        self.event_path = self.state_dir / "events.jsonl"
        self.run_id = run_id
        self.case_id = case_id
        self.runner = runner
        self.audit_timeout = audit_timeout
        self._publications: dict[
            str, tuple[Path, str, dict[str, Any], tuple[int, int]]
        ] = {}
        self.job_description_digest: str | None = None
        self._owned_job_description: tuple[Path, tuple[int, int]] | None = None

    def assess_candidate_fit(
        self,
        master_resume: str,
        job_description: str,
        run_id: str,
        case_id: str,
    ) -> dict[str, Any]:
        """Return the exact precomputed trusted report for the bound inputs."""

        valid, _ = validate_recomputed_candidate_fit_report(
            self._candidate_fit_report,
            run_id=run_id,
            case_id=case_id,
            master_resume=master_resume,
            job_description=job_description,
        )
        if (
            not valid
            or run_id != self.run_id
            or case_id != self.case_id
            or master_resume != self.master.text
            or canonical_digest(self._candidate_fit_report)
            != self.candidate_fit_report_digest
        ):
            raise ValueError("candidate-fit assessment binding mismatch")
        return json.loads(_canonical_json(self._candidate_fit_report))

    def _rollback_owned_target(
        self, target: Path, identity: tuple[int, int]
    ) -> None:
        try:
            info = os.lstat(target)
        except FileNotFoundError:
            return
        if (
            stat.S_ISREG(info.st_mode)
            and not stat.S_ISLNK(info.st_mode)
            and (info.st_dev, info.st_ino) == identity
        ):
            target.unlink()
            _fsync_directory(self.output_dir)

    def ensure_job_description(self, job_description: str) -> Path:
        """Bind the exact request JD to the fixed output sibling without clobbering."""

        if not isinstance(job_description, str) or not job_description.strip():
            raise ValueError("invalid job description")
        if job_description != self._job_description:
            raise FileExistsError("job description differs from fit assessment")
        payload = job_description.encode("utf-8")
        if len(payload) > MAX_JOB_BYTES:
            raise ValueError("job description too large")
        expected = canonical_digest(job_description)
        target = self.output_dir / "job_description.txt"
        try:
            target_info = os.lstat(target)
            if stat.S_ISLNK(target_info.st_mode) or not stat.S_ISREG(
                target_info.st_mode
            ):
                raise ValueError("unsafe job_description.txt")
            existing = _read_regular_nofollow_bytes(target, MAX_JOB_BYTES)
        except FileNotFoundError:
            existing = None
        if existing is not None:
            if existing != payload:
                raise FileExistsError("job_description.txt differs from request")
            self.job_description_digest = expected
            return target

        temporary = self.output_dir / f".job_description.{uuid.uuid4().hex}.tmp"
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(temporary, flags, 0o600)
        linked = False
        identity: tuple[int, int] | None = None
        try:
            os.write(descriptor, payload)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        try:
            temporary_info = os.lstat(temporary)
            identity = (temporary_info.st_dev, temporary_info.st_ino)
            os.link(temporary, target)
            linked = True
            target_info = os.lstat(target)
            if (target_info.st_dev, target_info.st_ino) != identity:
                raise RuntimeError("job description identity mismatch")
            temporary.unlink()
            _fsync_directory(self.output_dir)
            if _read_regular_nofollow_bytes(target, MAX_JOB_BYTES) != payload:
                raise RuntimeError("job description readback failed")
        except Exception:
            if linked and identity is not None:
                self._rollback_owned_target(target, identity)
            raise
        finally:
            if temporary.exists():
                temporary.unlink()
        assert identity is not None
        self._owned_job_description = (target, identity)
        self.job_description_digest = expected
        return target

    def rollback_unpublished_job_description(self) -> None:
        """Remove only the exact JD inode created by this service instance."""

        owned = self._owned_job_description
        if owned is not None:
            self._rollback_owned_target(*owned)
            self._owned_job_description = None

    def claim_run(self, run_id: str, case_id: str) -> dict[str, Any]:
        if run_id != self.run_id or case_id != self.case_id:
            raise ValueError("run identity mismatch")
        claim_key = canonical_digest({"run_id": run_id, "case_id": case_id})
        claim_path = self.claim_dir / f"{claim_key}.json"
        claim_id = f"claim:{uuid.uuid4()}"
        payload = _canonical_json(
            {
                "schema_version": RUN_CLAIM_VERSION,
                "claim_id": claim_id,
                "run_case_digest": claim_key,
            }
        ).encode("utf-8")
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(claim_path, flags, 0o600)
        except FileExistsError:
            return {
                "schema_version": RUN_CLAIM_VERSION,
                "run_id": run_id,
                "case_id": case_id,
                "claimed": False,
                "claim_id": "",
            }
        try:
            os.write(descriptor, payload)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        _fsync_directory(self.claim_dir)
        return {
            "schema_version": RUN_CLAIM_VERSION,
            "run_id": run_id,
            "case_id": case_id,
            "claimed": True,
            "claim_id": claim_id,
        }

    def attest_source(self, master_resume: str) -> dict[str, Any]:
        current = load_master_snapshot(self.config_path)
        trusted = (
            master_resume == self.master.text
            and current.config_path == self.master.config_path
            and current.master_path == self.master.master_path
            and current.config_digest == self.master.config_digest
            and current.master_bytes_digest == self.master.master_bytes_digest
            and current.text_digest == self.master.text_digest
        )
        return {
            "schema_version": SOURCE_ATTESTATION_VERSION,
            "trusted": trusted,
            "source_id": f"source:{self.master.master_bytes_digest}",
            "source_digest": self.master.text_digest,
        }

    def _audit_vote(
        self,
        name: str,
        command: list[str],
        candidate: Path,
        expected_digest: str,
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        if _sha_bytes(_read_regular_bytes(candidate, MAX_MASTER_BYTES)) != expected_digest:
            raise RuntimeError("candidate changed before audit")
        result = self.runner(
            command,
            input_text="",
            timeout=self.audit_timeout,
            cwd=self.project_root,
            env=_sanitized_environment(),
        )
        if _sha_bytes(_read_regular_bytes(candidate, MAX_MASTER_BYTES)) != expected_digest:
            raise RuntimeError("candidate changed during audit")
        invocation_id = f"local:{name}:{result.pid}:{uuid.uuid4()}"
        codes: list[str] = []
        findings: list[dict[str, Any]] = []
        passed = result.returncode == 0 and not result.timed_out
        if name == "evidence":
            passed, codes, findings = _parse_evidence_result(result, candidate)
        elif name == "canonical_integrity":
            try:
                report = json.loads(result.stdout)
                reported_pass = report.get("passed") if isinstance(report, dict) else None
                reported_codes = (
                    report.get("difference_codes") if isinstance(report, dict) else None
                )
                if type(reported_pass) is not bool or not isinstance(reported_codes, list):
                    raise ValueError
                if not all(isinstance(code, str) and code for code in reported_codes):
                    raise ValueError
                passed = passed and reported_pass and not reported_codes
                if not passed:
                    codes = reported_codes or ["CANONICAL_INTEGRITY_FAILED"]
            except Exception:
                passed = False
                codes = ["CANONICAL_AUDIT_MALFORMED"]
            findings = [
                _unbound_audit_finding(name, code, index)
                for index, code in enumerate(codes, start=1)
            ]
        elif name == "human_voice":
            passed, codes, findings = _parse_human_voice_result(result, candidate)
        elif not passed:
            codes = [
                f"HUMAN_VOICE_AUDIT_{'TIMEOUT' if result.timed_out else 'FAILED'}"
            ]
            findings = [_unbound_audit_finding(name, codes[0], 1)]
        return (
            {
                "schema_version": VOTE_VERSION,
                "name": name,
                "invocation_id": invocation_id,
                "passed": passed,
                "draft_digest": canonical_digest(
                    candidate.read_text(encoding="utf-8")
                ),
                "codes": codes,
            },
            findings,
        )

    def audit_draft(self, draft: str) -> dict[str, Any]:
        draft_bytes = draft.encode("utf-8")
        byte_digest = _sha_bytes(draft_bytes)
        if byte_digest != canonical_digest(draft):
            raise RuntimeError("candidate encoding mismatch")
        with tempfile.TemporaryDirectory(
            prefix="candidate-", dir=str(self.audit_temp_dir)
        ) as directory:
            candidate = Path(directory) / "resume.md"
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
            descriptor = os.open(candidate, flags, 0o600)
            try:
                os.write(descriptor, draft_bytes)
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            python = sys.executable
            commands = [
                (
                    "evidence",
                    [python, str(self.project_root / "evidence_audit.py"), str(candidate)],
                ),
                (
                    "human_voice",
                    [
                        python,
                        str(self.project_root / "human_voice_audit.py"),
                        str(candidate),
                        "--mode",
                        "resume",
                        "--json",
                    ],
                ),
                (
                    "canonical_integrity",
                    [
                        python,
                        str(self.project_root / "resume_integrity_audit.py"),
                        str(candidate),
                        "--config",
                        str(self.config_path),
                    ],
                ),
            ]
            vote_results = [
                self._audit_vote(name, command, candidate, byte_digest)
                for name, command in commands
            ]
            votes = [vote for vote, _ in vote_results]
            findings = [
                finding
                for _, vote_findings in vote_results
                for finding in vote_findings
            ]
            if _sha_bytes(_read_regular_bytes(candidate, MAX_MASTER_BYTES)) != byte_digest:
                raise RuntimeError("candidate changed after audits")
        invocation_ids = {vote["invocation_id"] for vote in votes}
        if len(invocation_ids) != 3:
            raise RuntimeError("audit invocations were not independent")
        codes = [code for vote in votes for code in vote["codes"]]
        passed = all(vote["passed"] for vote in votes)
        return {
            "schema_version": AUTHORIZATION_VERSION,
            "draft_digest": canonical_digest(draft),
            "passed": passed,
            "codes": codes,
            "votes": votes,
            "findings": findings,
        }

    def record_event(self, event: str, payload: dict[str, Any]) -> None:
        if fcntl is None:
            raise RuntimeError("durable event locking requires POSIX")
        expected = self._EVENT_KEYS.get(event)
        if expected is None or not isinstance(payload, dict) or set(payload) != expected:
            raise ValueError("invalid event metadata")
        for key, value in payload.items():
            if not isinstance(value, str) or not value:
                raise ValueError("invalid event value")
            if "digest" in key and not _SHA256.fullmatch(value):
                raise ValueError("invalid event digest")
            if "agent_id" in key and not _SAFE_ID.fullmatch(value):
                raise ValueError("invalid event agent identity")
            if key == "authorization_codes" and (
                len(value) > 1024
                or re.fullmatch(r"[A-Za-z0-9_.,:|+-]+", value) is None
            ):
                raise ValueError("invalid authorization diagnostics")
        row = _canonical_json(
            {
                "event": event,
                "run_case_digest": canonical_digest(
                    {"run_id": self.run_id, "case_id": self.case_id}
                ),
                "metadata": payload,
            }
        ).encode("utf-8") + b"\n"
        flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(self.event_path, flags, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            os.write(descriptor, row)
            os.fsync(descriptor)
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)

    def publish(self, draft: str, metadata: dict[str, Any]) -> dict[str, Any]:
        expected_keys = {
            "schema_version",
            "run_id",
            "case_id",
            "draft_digest",
            "source_digest",
            "job_description_digest",
            "candidate_fit_report",
            "candidate_fit_report_digest",
            "researcher_agent_id",
            "researcher_artifact_digest",
            "auditor_attestation",
            "authorization_digest",
            "authorization_report",
            "vote_invocation_ids",
        }
        vote_ids = metadata.get("vote_invocation_ids") if isinstance(metadata, dict) else None
        attestation = metadata.get("auditor_attestation") if isinstance(metadata, dict) else None
        authorization = metadata.get("authorization_report") if isinstance(metadata, dict) else None
        fit_report = metadata.get("candidate_fit_report") if isinstance(metadata, dict) else None
        draft_digest = canonical_digest(draft)
        fit_valid, fit_passed = validate_recomputed_candidate_fit_report(
            fit_report,
            run_id=self.run_id,
            case_id=self.case_id,
            master_resume=self.master.text,
            job_description=self._job_description,
        )
        if (
            not isinstance(metadata, dict)
            or set(metadata) != expected_keys
            or metadata.get("schema_version") != AUTHORIZATION_VERSION
            or metadata.get("run_id") != self.run_id
            or metadata.get("case_id") != self.case_id
            or metadata.get("draft_digest") != draft_digest
            or metadata.get("source_digest") != self.master.text_digest
            or self.job_description_digest is None
            or metadata.get("job_description_digest")
            != self.job_description_digest
            or not fit_valid
            or not fit_passed
            or fit_report != self._candidate_fit_report
            or metadata.get("candidate_fit_report_digest")
            != self.candidate_fit_report_digest
            or metadata.get("candidate_fit_report_digest")
            != canonical_digest(fit_report)
            or not isinstance(metadata.get("researcher_agent_id"), str)
            or not re.fullmatch(
                r"(?:codex|claude):[A-Za-z0-9._:-]{1,128}",
                metadata["researcher_agent_id"],
            )
            or not _SHA256.fullmatch(
                str(metadata.get("researcher_artifact_digest", ""))
            )
            or not isinstance(attestation, dict)
            or set(attestation)
            != {"agent_id", "artifact_digest", "verdict", "draft_digest"}
            or not isinstance(attestation.get("agent_id"), str)
            or not re.fullmatch(
                r"(?:codex|claude):[A-Za-z0-9._:-]{1,128}",
                attestation["agent_id"],
            )
            or metadata["researcher_agent_id"].split(":", 1)[0]
            != attestation["agent_id"].split(":", 1)[0]
            or metadata["researcher_agent_id"] == attestation["agent_id"]
            or not _SHA256.fullmatch(str(attestation.get("artifact_digest", "")))
            or attestation.get("verdict") != "PASS"
            or attestation.get("draft_digest") != draft_digest
            or not isinstance(authorization, dict)
            or set(authorization)
            != {
                "schema_version",
                "draft_digest",
                "passed",
                "codes",
                "votes",
                "findings",
            }
            or authorization.get("schema_version") != AUTHORIZATION_VERSION
            or authorization.get("draft_digest") != draft_digest
            or authorization.get("passed") is not True
            or authorization.get("codes") != []
            or authorization.get("findings") != []
            or not isinstance(authorization.get("votes"), list)
            or len(authorization["votes"]) != 3
            or not _SHA256.fullmatch(str(metadata.get("authorization_digest", "")))
            or metadata.get("authorization_digest") != canonical_digest(authorization)
            or not isinstance(vote_ids, list)
            or len(vote_ids) != 3
            or len(set(vote_ids)) != 3
            or not all(isinstance(value, str) and value for value in vote_ids)
        ):
            raise ValueError("invalid publication authorization")
        for expected_name, vote in zip(_VOTE_NAMES, authorization["votes"]):
            if (
                not isinstance(vote, dict)
                or set(vote)
                != {
                    "schema_version",
                    "name",
                    "invocation_id",
                    "passed",
                    "draft_digest",
                    "codes",
                }
                or vote.get("schema_version") != VOTE_VERSION
                or vote.get("name") != expected_name
                or vote.get("passed") is not True
                or vote.get("draft_digest") != draft_digest
                or vote.get("codes") != []
                or not isinstance(vote.get("invocation_id"), str)
                or not vote["invocation_id"]
            ):
                raise ValueError("invalid publication authorization vote")
        if vote_ids != [vote["invocation_id"] for vote in authorization["votes"]]:
            raise ValueError("publication vote order mismatch")
        target = self.output_dir / "resume.md"
        if target.is_symlink():
            raise ValueError("unsafe publication target")
        if target.exists():
            raise FileExistsError("resume.md already exists")
        draft_bytes = draft.encode("utf-8")
        temporary = self.output_dir / f".resume.md.{uuid.uuid4().hex}.tmp"
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(temporary, flags, 0o600)
        try:
            os.write(descriptor, draft_bytes)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        temporary_info = os.lstat(temporary)
        target_identity = (temporary_info.st_dev, temporary_info.st_ino)
        linked = False
        digest = canonical_digest(draft)
        try:
            os.link(temporary, target)
            linked = True
            target_info = os.lstat(target)
            if (target_info.st_dev, target_info.st_ino) != target_identity:
                raise RuntimeError("publication identity mismatch")
            temporary.unlink()
            _fsync_directory(self.output_dir)
            disk_bytes = _read_regular_bytes(target, MAX_MASTER_BYTES)
            if disk_bytes != draft_bytes or _sha_bytes(disk_bytes) != digest:
                raise RuntimeError("publication verification failed")
        except Exception:
            if linked:
                self._rollback_owned_target(target, target_identity)
            raise
        finally:
            if temporary.exists():
                temporary.unlink()
        publication_id = f"publication:{uuid.uuid4()}"
        self._publications[publication_id] = (
            target,
            digest,
            dict(metadata),
            target_identity,
        )
        return {
            "schema_version": PUBLICATION_VERSION,
            "committed": True,
            "draft_digest": digest,
            "target_digest": digest,
            "publication_id": publication_id,
        }

    def verify_publication(self, publication_id: str) -> dict[str, Any]:
        record = self._publications.get(publication_id)
        if record is None:
            raise LookupError("unknown publication")
        target, expected, metadata, target_identity = record
        authorization_receipt = {
            "schema_version": FINAL_RECEIPT_VERSION,
            "run_id": metadata["run_id"],
            "case_id": metadata["case_id"],
            "draft_digest": expected,
            "source_digest": metadata["source_digest"],
            "job_description_digest": metadata["job_description_digest"],
            "candidate_fit_report": metadata["candidate_fit_report"],
            "candidate_fit_report_digest": metadata[
                "candidate_fit_report_digest"
            ],
            "researcher_agent_id": metadata["researcher_agent_id"],
            "researcher_artifact_digest": metadata[
                "researcher_artifact_digest"
            ],
            "auditor_attestation": metadata["auditor_attestation"],
            "authorization_digest": metadata["authorization_digest"],
            "authorization_report": metadata["authorization_report"],
            "vote_invocation_ids": metadata["vote_invocation_ids"],
            "publication_id": publication_id,
            "verified_target_digest": expected,
        }
        authorization_receipt_digest = canonical_digest(authorization_receipt)
        authorization_receipt_path = ""
        receipt_path: Path | None = None
        receipt_identity: tuple[int, int] | None = None
        try:
            disk_bytes = _read_regular_bytes(target, MAX_MASTER_BYTES)
            verified = _sha_bytes(disk_bytes) == expected
            if verified:
                receipt_path = self.output_dir / (
                    "resume-team-receipt."
                    f"{canonical_digest({'run_id': metadata['run_id'], 'case_id': metadata['case_id']})}.json"
                )
                if receipt_path.is_symlink() or receipt_path.exists():
                    raise FileExistsError("authorization receipt already exists")
                receipt_bytes = _canonical_json(authorization_receipt).encode("utf-8")
                temporary = self.output_dir / (
                    f".resume-team-receipt.{uuid.uuid4().hex}.tmp"
                )
                flags = (
                    os.O_WRONLY
                    | os.O_CREAT
                    | os.O_EXCL
                    | getattr(os, "O_NOFOLLOW", 0)
                )
                descriptor = os.open(temporary, flags, 0o600)
                try:
                    os.write(descriptor, receipt_bytes)
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
                temporary_info = os.lstat(temporary)
                candidate_receipt_identity = (
                    temporary_info.st_dev,
                    temporary_info.st_ino,
                )
                try:
                    os.link(temporary, receipt_path)
                    receipt_identity = candidate_receipt_identity
                    receipt_info = os.lstat(receipt_path)
                    if (
                        receipt_info.st_dev,
                        receipt_info.st_ino,
                    ) != receipt_identity:
                        raise RuntimeError("authorization receipt identity mismatch")
                    temporary.unlink()
                    _fsync_directory(self.output_dir)
                finally:
                    if temporary.exists():
                        temporary.unlink()
                readback = _read_regular_bytes(receipt_path, 64 * 1024)
                if (
                    readback != receipt_bytes
                    or _sha_bytes(readback) != authorization_receipt_digest
                    or json.loads(readback) != authorization_receipt
                ):
                    self._rollback_owned_target(receipt_path, receipt_identity)
                    raise RuntimeError("authorization receipt readback failed")
                authorization_receipt_path = receipt_path.name
            else:
                self._rollback_owned_target(target, target_identity)
        except Exception:
            if receipt_path is not None and receipt_identity is not None:
                self._rollback_owned_target(receipt_path, receipt_identity)
            self._rollback_owned_target(target, target_identity)
            raise
        return {
            "schema_version": PUBLICATION_VERIFICATION_VERSION,
            "verified": verified,
            "publication_id": publication_id,
            "target_digest": _sha_bytes(disk_bytes),
            "authorization_receipt": authorization_receipt,
            "authorization_receipt_digest": authorization_receipt_digest,
            "authorization_receipt_path": authorization_receipt_path,
        }


def _check(
    name: str, passed: bool, failure_code: str
) -> dict[str, str | bool]:
    return {"name": name, "passed": passed, "code": "OK" if passed else failure_code}


def check_host(
    *,
    host: str,
    project_root: str | Path,
    config_path: str | Path,
    cli_path: str | None = None,
    runner: ProcessRunner = _run_process,
) -> dict[str, Any]:
    """Non-model preflight.  It never writes a resume or calls a role."""

    root = Path(project_root).expanduser().resolve(strict=True)
    checks: list[dict[str, str | bool]] = []
    checks.append(_check("posix_runtime", os.name == "posix", "POSIX_RUNTIME_REQUIRED"))
    try:
        executable = _resolve_executable(host, cli_path)
        checks.append(_check("executable", True, "HOST_EXECUTABLE_UNAVAILABLE"))
    except Exception:
        executable = Path("/unavailable")
        checks.append(_check("executable", False, "HOST_EXECUTABLE_UNAVAILABLE"))

    definitions_ok = True
    try:
        for role in ROLE_ORDER:
            if host == "codex":
                _load_codex_role(root, role)
            else:
                _load_claude_role(root, role)
    except Exception:
        definitions_ok = False
    checks.append(_check("role_definitions", definitions_ok, "ROLE_DEFINITIONS_INVALID"))

    try:
        load_master_snapshot(config_path)
        master_ok = True
    except Exception:
        master_ok = False
    checks.append(_check("config_master", master_ok, "MASTER_SOURCE_INVALID"))

    scripts_ok = all(
        (root / name).is_file()
        for name in (
            "evidence_audit.py",
            "human_voice_audit.py",
            "resume_integrity_audit.py",
            "candidate_fit_preflight.py",
        )
    )
    checks.append(_check("audit_scripts", scripts_ok, "AUDIT_SCRIPTS_MISSING"))

    schemas_ok = True
    try:
        from jsonschema import Draft202012Validator

        expected = {
            "resume-team-handoff.schema.json": "resume-team-handoff/v1",
            "resume-team-authorization.schema.json": AUTHORIZATION_VERSION,
            "resume-team-final-receipt.schema.json": FINAL_RECEIPT_VERSION,
            "resume-team-result.schema.json": RESULT_VERSION,
            "candidate-fit-report.schema.json": "candidate-fit-report/v1",
        }
        for filename, identifier in expected.items():
            schema = json.loads(
                _read_regular_bytes(root / "schemas" / filename, 512 * 1024).decode("utf-8")
            )
            if not isinstance(schema, dict) or schema.get("$id") != identifier:
                raise ValueError
            Draft202012Validator.check_schema(schema)
            if filename == "candidate-fit-report.schema.json" and (
                schema.get("additionalProperties") is not False
                or set(schema.get("required", []))
                != _CANDIDATE_FIT_REQUIRED_KEYS
            ):
                raise ValueError
    except Exception:
        schemas_ok = False
    checks.append(_check("schemas", schemas_ok, "RUNTIME_SCHEMAS_INVALID"))

    feature_ok = True
    auth_ok = False
    if checks[1]["passed"]:
        environment = _sanitized_environment()
        if host == "codex":
            version = runner(
                [str(executable), "--version"],
                input_text="",
                timeout=10,
                cwd=root,
                env=environment,
            )
            features = runner(
                [str(executable), "features", "list"],
                input_text="",
                timeout=10,
                cwd=root,
                env=environment,
            )
            available = {
                line.split()[0] for line in features.stdout.splitlines() if line.split()
            }
            isolation_command = [
                str(executable),
                "exec",
                "--ignore-user-config",
                "--ephemeral",
                "--sandbox",
                "read-only",
                "--ignore-rules",
                "--json",
                "-c",
                'web_search="disabled"',
                "-c",
                "notify=[]",
                "-c",
                'forced_login_method="chatgpt"',
            ]
            for feature in CODEX_DISABLED_FEATURES:
                isolation_command.extend(["--disable", feature])
            isolation_command.append("--help")
            isolation = runner(
                isolation_command,
                input_text="",
                timeout=10,
                cwd=root,
                env=environment,
            )
            feature_ok = (
                version.returncode == 0
                and not version.timed_out
                and features.returncode == 0
                and not features.timed_out
                and set(CODEX_DISABLED_FEATURES).issubset(available)
                and isolation.returncode == 0
                and not isolation.timed_out
                and all(
                    flag in isolation.stdout
                    for flag in (
                        "--ignore-user-config",
                        "--ephemeral",
                        "--ignore-rules",
                        "--sandbox",
                        "--json",
                        "--output-schema",
                    )
                )
            )
            auth = runner(
                [str(executable), "login", "status"],
                input_text="",
                timeout=10,
                cwd=root,
                env=environment,
            )
            auth_ok = (
                auth.returncode == 0
                and not auth.timed_out
                and "Logged in using ChatGPT" in (auth.stdout + auth.stderr)
            )
        else:
            inline_agent = _canonical_json(_load_claude_role(root, "researcher"))
            help_result = runner(
                [
                    str(executable),
                    "--safe-mode",
                    "--setting-sources",
                    "",
                    "--agents",
                    inline_agent,
                    "--agent",
                    "resume-researcher",
                    "--tools",
                    "",
                    "--strict-mcp-config",
                    "--mcp-config",
                    '{"mcpServers":{}}',
                    "--no-session-persistence",
                    "--help",
                ],
                input_text="",
                timeout=10,
                cwd=root,
                env=environment,
            )
            feature_ok = (
                help_result.returncode == 0
                and not help_result.timed_out
                and re.search(
                    r'Use\s+""\s+to\s+disable\s+all\s+tools',
                    help_result.stdout,
                    re.IGNORECASE,
                )
                is not None
                and all(
                    flag in help_result.stdout
                    for flag in (
                        "--safe-mode",
                        "--agents",
                        "--tools",
                        "--json-schema",
                        "--no-session-persistence",
                        "--strict-mcp-config",
                    )
                )
            )
            auth = runner(
                [str(executable), "auth", "status", "--json"],
                input_text="",
                timeout=10,
                cwd=root,
                env=environment,
            )
            try:
                status = json.loads(auth.stdout)
                auth_ok = (
                    auth.returncode == 0
                    and not auth.timed_out
                    and status.get("loggedIn") is True
                    and status.get("authMethod") == "claude.ai"
                    and status.get("apiProvider") == "firstParty"
                )
            except Exception:
                auth_ok = False
    else:
        feature_ok = False
    checks.append(_check("tool_isolation", feature_ok, "TOOL_ISOLATION_UNAVAILABLE"))
    checks.append(
        _check(
            "subscription_configured",
            auth_ok,
            "SUBSCRIPTION_CONFIGURATION_UNAVAILABLE",
        )
    )
    ready = all(bool(row["passed"]) for row in checks)
    return {
        "schema_version": HOST_CHECK_VERSION,
        "host": host,
        "ready": ready,
        "effective_model_mode": "isolated_cli_default_unknown",
        "publication_scope": "authorized_markdown_draft",
        "readiness_scope": "static_configuration_no_inference",
        "live_inference_verified": False,
        "checks": checks,
    }


def _failure_result(
    run_id: str,
    case_id: str,
    terminal: str,
    *,
    candidate_fit_report: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "schema_version": RESULT_VERSION,
        "run_id": run_id if run_id else "invalid-run",
        "case_id": case_id if case_id else "invalid-case",
        "terminal_class": terminal,
        "published": False,
        "success_reported": False,
        "diagnostics": [terminal],
        "final_draft_digest": "",
        "authorization_receipt": None,
        "authorization_receipt_digest": "",
        "authorization_receipt_path": "",
        "candidate_fit_report": candidate_fit_report,
        "candidate_fit_report_digest": (
            canonical_digest(candidate_fit_report)
            if candidate_fit_report is not None
            else ""
        ),
    }


def _load_job_description(args: argparse.Namespace) -> str:
    choices = sum(
        value is not None
        for value in (args.job_description_file, args.job_description)
    )
    if choices != 1:
        raise ValueError("provide exactly one job description source")
    if args.job_description_file is not None:
        raw = _read_regular_bytes(Path(args.job_description_file), MAX_JOB_BYTES)
        text = raw.decode("utf-8")
    else:
        text = args.job_description
        if len(text.encode("utf-8")) > MAX_JOB_BYTES:
            raise ValueError("job description too large")
    if not text.strip():
        raise ValueError("empty job description")
    return text


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Publish an authorized Markdown resume draft with the native resume team"
    )
    parser.add_argument("--host", required=True, choices=("codex", "claude"))
    parser.add_argument("--check-host", action="store_true")
    parser.add_argument("--config", default="config.json")
    parser.add_argument("--job-description-file")
    parser.add_argument("--job-description")
    parser.add_argument("--output-dir")
    parser.add_argument("--run-id")
    parser.add_argument("--case-id")
    parser.add_argument(
        "--as-of-date",
        help="Candidate-fit policy date (YYYY-MM-DD); defaults to today's local date",
    )
    parser.add_argument("--max-editor-attempts", type=int, default=2)
    parser.add_argument("--role-timeout-seconds", type=float, default=180.0)
    parser.add_argument("--audit-timeout-seconds", type=float, default=120.0)
    parser.add_argument("--cli-path")
    parser.add_argument("--model")
    parser.add_argument("--reasoning-effort", choices=("ultra",))
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    project_root = Path(__file__).resolve().parent
    config_path = Path(args.config).expanduser().resolve()
    if args.check_host:
        report = check_host(
            host=args.host,
            project_root=project_root,
            config_path=config_path,
            cli_path=args.cli_path,
        )
        print(_canonical_json(report))
        return 0 if report["ready"] else 1

    run_id = args.run_id or str(uuid.uuid4())
    case_id = args.case_id or f"job:{uuid.uuid4()}"
    services: LocalTrustedServices | None = None
    candidate_fit_report: dict[str, Any] | None = None
    try:
        if not _SAFE_ID.fullmatch(run_id) or not _SAFE_ID.fullmatch(case_id):
            raise ValueError("invalid run identity")
        if args.host != "codex" and any(
            (args.model, args.reasoning_effort)
        ):
            raise ValueError("Codex options require the Codex host")
        job_description = _load_job_description(args)
        master = load_master_snapshot(config_path)
        as_of_date = args.as_of_date or date.today().isoformat()
        if date.fromisoformat(as_of_date).isoformat() != as_of_date:
            raise ValueError("invalid as-of date")
        try:
            candidate_fit_report = _trusted_candidate_fit_assessment(
                master.text,
                job_description,
                run_id=run_id,
                case_id=case_id,
                as_of_date=as_of_date,
                resume_digest=master.text_digest,
                job_description_digest=canonical_digest(job_description),
                extraction_trustworthy=True,
            )
        except Exception:
            result = _failure_result(
                run_id,
                case_id,
                "FAILED:CANDIDATE_FIT_PREFLIGHT",
            )
            print(_canonical_json(result))
            return 1
        fit_valid, fit_passed = validate_recomputed_candidate_fit_report(
            candidate_fit_report,
            run_id=run_id,
            case_id=case_id,
            master_resume=master.text,
            job_description=job_description,
        )
        if not fit_valid:
            result = _failure_result(
                run_id,
                case_id,
                "FAILED:CANDIDATE_FIT_PREFLIGHT",
            )
            print(_canonical_json(result))
            return 1
        if not fit_passed:
            result = _failure_result(
                run_id,
                case_id,
                "REJECTED:CANDIDATE_FIT",
                candidate_fit_report=candidate_fit_report,
            )
            print(_canonical_json(result))
            return 1
        if not args.output_dir:
            raise ValueError("output directory required")
        # Preserve the final path component so LocalTrustedServices can reject
        # a caller-supplied symlink before any state or output write.
        output_dir = Path(args.output_dir).expanduser().absolute()
        state_dir = project_root / ".resume-team-state"
        adapter = NativeRoleAdapter(
            host=args.host,
            project_root=project_root,
            cli_path=args.cli_path,
            model=args.model,
            reasoning_effort=args.reasoning_effort,
        )
        services = LocalTrustedServices(
            project_root=project_root,
            config_path=config_path,
            master=master,
            output_dir=output_dir,
            state_dir=state_dir,
            run_id=run_id,
            case_id=case_id,
            job_description=job_description,
            candidate_fit_report=candidate_fit_report,
            audit_timeout=args.audit_timeout_seconds,
        )
        services.ensure_job_description(job_description)
        request = {
            "schema_version": PROTOCOL_VERSION,
            "run_id": run_id,
            "case_id": case_id,
            "job_description": job_description,
            "master_resume": master.text,
            "output_dir": str(output_dir),
            "max_editor_attempts": args.max_editor_attempts,
            "role_timeout_seconds": args.role_timeout_seconds,
        }
        result = run_team(request, adapter, services)
        if result.get("published") is not True:
            services.rollback_unpublished_job_description()
    except Exception:
        if services is not None:
            try:
                services.rollback_unpublished_job_description()
            except Exception:
                pass
        result = _failure_result(
            run_id,
            case_id,
            "FAILED:RUNTIME_INPUT",
            candidate_fit_report=candidate_fit_report,
        )
    print(_canonical_json(result))
    return 0 if result.get("published") is True else 1


if __name__ == "__main__":
    raise SystemExit(main())
