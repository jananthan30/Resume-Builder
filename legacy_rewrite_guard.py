"""Fail-closed policy for legacy resume-authoring entry points.

Resume drafts may only be produced by ``native_resume_team.py`` because that
runtime enforces the candidate-fit, provenance, audit, and publication gates.
Legacy MCP, REST, coaching, and standalone helpers deliberately remain
callable only so older clients receive one stable migration response.
"""

from __future__ import annotations

from typing import Any


NATIVE_RESUME_TEAM_REQUIRED = "NATIVE_RESUME_TEAM_REQUIRED"
NATIVE_RESUME_TEAM_REQUIRED_MESSAGE = (
    "Direct resume rewriting is disabled. Use `python native_resume_team.py "
    "--host codex` or `python native_resume_team.py --host claude`; the native "
    "Resume Team enforces the mandatory candidate-fit and publication-authorization gates."
)


class NativeResumeTeamRequiredError(RuntimeError):
    """Raised before a legacy standalone helper can author a resume."""

    code = NATIVE_RESUME_TEAM_REQUIRED

    def __init__(self) -> None:
        super().__init__(f"{NATIVE_RESUME_TEAM_REQUIRED}: {NATIVE_RESUME_TEAM_REQUIRED_MESSAGE}")


def native_resume_team_required_response() -> dict[str, Any]:
    """Return a fresh, non-authoring rejection payload for legacy clients."""

    return {
        "status": "REJECTED",
        "error": NATIVE_RESUME_TEAM_REQUIRED,
        "code": NATIVE_RESUME_TEAM_REQUIRED,
        "message": NATIVE_RESUME_TEAM_REQUIRED_MESSAGE,
        "rewritten_resume": None,
        "changes_made": [],
        "explanation": NATIVE_RESUME_TEAM_REQUIRED_MESSAGE,
    }
