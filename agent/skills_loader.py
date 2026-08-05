"""Skill-content loader for the ResumeHQ standalone agent.

Reads the small, curated ``SKILL.md`` files under ``skills_server/<name>/``
-- ported, host-CLI-free versions of this repo's local
``.claude/commands/*.md`` guides (see that directory's own files) -- and
exposes them to an orchestrating agent as plain text plus lightweight
frontmatter metadata.

Public API:
    list_skills() -> list[{"name": str, "description": str}]
    read_skill(name: str) -> str  (raises KeyError for an unregistered name)

Both functions read the file fresh on every call (no caching) -- these
files change rarely and staleness would be a worse failure mode than a
cheap disk read on a low-traffic endpoint.

No dependency on ``cloud.*`` or any vendor SDK -- this module is pure
stdlib, importable in the same environments ``agent/tools.py`` already
guarantees clean imports for.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

__all__ = ["list_skills", "read_skill"]

_SKILLS_DIR = (Path(__file__).resolve().parent.parent / "skills_server").resolve()

_FRONTMATTER_RE = re.compile(r"\A---[ \t]*\n(.*?)\n---[ \t]*\n?", re.DOTALL)


def _parse_frontmatter(text: str) -> dict[str, str]:
    """Tiny line-based frontmatter reader -- the two fields these files use
    (``name``, ``description``) are flat scalars, so a full YAML parser
    would be a dependency for nothing this module needs."""
    match = _FRONTMATTER_RE.match(text)
    if not match:
        return {}
    fields: dict[str, str] = {}
    for line in match.group(1).splitlines():
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        fields[key.strip()] = value.strip()
    return fields


def _safe_skill_path(name: str) -> Path | None:
    """Resolve ``name`` to a ``skills_server/<name>/SKILL.md`` path, or
    ``None`` if ``name`` is not a plain directory-name slug or would
    resolve outside ``_SKILLS_DIR`` (defense in depth against a
    path-traversal ``name`` -- ``read_skill`` is registered as an
    agent-callable tool in ``agent/tools.py``, so ``name`` must be treated
    as untrusted input, not a trusted local file path).
    """
    if not isinstance(name, str) or not name or name in (".", ".."):
        return None
    if "/" in name or "\\" in name or "\x00" in name:
        return None
    candidate = (_SKILLS_DIR / name / "SKILL.md").resolve()
    try:
        candidate.relative_to(_SKILLS_DIR)
    except ValueError:
        return None
    return candidate


def _discovered_skill_names() -> list[str]:
    if not _SKILLS_DIR.is_dir():
        return []
    return sorted(
        child.name
        for child in _SKILLS_DIR.iterdir()
        if child.is_dir() and (child / "SKILL.md").is_file()
    )


def list_skills() -> list[dict[str, Any]]:
    """Return ``[{"name", "description"}]`` for every registered skill, in a
    fixed (alphabetical) order, for system-prompt injection."""
    skills = []
    for name in _discovered_skill_names():
        meta = _parse_frontmatter(read_skill(name))
        skills.append({
            "name": meta.get("name", name),
            "description": meta.get("description", ""),
        })
    return skills


def read_skill(name: str) -> str:
    """Return the full Markdown content (frontmatter + body) for one skill.

    Raises ``KeyError`` for any name that is not a currently registered
    skill directory -- this must never become an arbitrary-file-read
    primitive.
    """
    path = _safe_skill_path(name)
    if path is None or not path.is_file():
        raise KeyError(f"unknown skill: {name!r}")
    return path.read_text(encoding="utf-8")
