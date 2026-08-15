# evidence_engine/audit.py
"""Audit records, content-addressed caching, and deterministic replay.

The spec's auditability requirement is that a score be reconstructable: given
identical inputs and the same scoring policy, the deterministic layer must
return the same number, and a change in any version must produce a new record
rather than silently rewriting an old one.

Three pieces implement that:

* ``input_fingerprint`` hashes the inputs *and* every version string, so a new
  prompt, policy, relation map or model yields a different identity.
* ``append_record`` writes an append-only JSONL trail. Nothing is ever updated
  in place.
* ``replay_scoring`` recomputes the deterministic score from the stored
  judgments and reports any drift. It never calls an LLM.
"""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from evidence_engine.models import MatchResult

DEFAULT_AUDIT_PATH = Path("audit_log") / "evidence_match.jsonl"

# Version fields that must participate in the cache identity. Adding a field
# here invalidates every cached result, which is the intended behaviour.
VERSION_FIELDS = (
    "scoring_policy_version", "parser_version", "prompt_version",
    "extractor_model", "judge_model", "embedding_model",
    "relationships_version",
)


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def canonical_json(payload: dict) -> str:
    """Stable serialisation so equal content always hashes equally."""
    return json.dumps(payload, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, default=str)


def input_fingerprint(
    resume_sha256: str,
    job_sha256: str,
    versions: dict,
    evaluated_month_index: int,
) -> str:
    """Identity of one evaluation: inputs plus every version that shaped it."""
    return sha256_text(canonical_json({
        "resume_sha256": resume_sha256,
        "job_sha256": job_sha256,
        "evaluated_month_index": evaluated_month_index,
        "versions": {k: versions.get(k) for k in VERSION_FIELDS},
    }))


def match_id_for(fingerprint: str, when: Optional[datetime] = None) -> str:
    when = when or datetime.now(timezone.utc)
    return f"MATCH-{when.strftime('%Y%m%d')}-{fingerprint[:12]}"


def audit_record(result: MatchResult) -> dict:
    """The complete, self-contained record for one match."""
    payload = result.model_dump(mode="json")
    payload["audit_schema"] = "evidence-match-audit/v1"
    payload["record_digest"] = sha256_text(canonical_json(payload))
    return payload


def append_record(
    result: MatchResult, path: Optional[str | Path] = None
) -> Path:
    """Append one audit record. Never rewrites history."""
    target = Path(path) if path else DEFAULT_AUDIT_PATH
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("a", encoding="utf-8") as handle:
        handle.write(canonical_json(audit_record(result)) + "\n")
    return target


def read_records(path: Optional[str | Path] = None) -> list[dict]:
    target = Path(path) if path else DEFAULT_AUDIT_PATH
    if not target.exists():
        return []
    return [json.loads(line) for line in
            target.read_text(encoding="utf-8").splitlines() if line.strip()]


class ResultCache:
    """Content-addressed cache. A version change is a cache miss, by design."""

    #: Overridable so tests and deployments do not write into the repo. Callers
    #: that construct ``ResultCache()`` with no argument (the tools, the HTTP
    #: endpoints) pick this up without threading a path through every layer.
    DIRECTORY_ENV = "EVIDENCE_CACHE_DIR"
    DEFAULT_DIRECTORY = "audit_log/cache"

    def __init__(self, directory: str | Path | None = None):
        self.directory = Path(
            directory
            or os.getenv(self.DIRECTORY_ENV)
            or self.DEFAULT_DIRECTORY
        )

    def _path(self, fingerprint: str) -> Path:
        return self.directory / f"{fingerprint}.json"

    def get(self, fingerprint: str) -> Optional[MatchResult]:
        path = self._path(fingerprint)
        if not path.exists():
            return None
        try:
            return MatchResult(**json.loads(path.read_text(encoding="utf-8")))
        except Exception:
            return None  # unreadable or stale shape → recompute

    def put(self, fingerprint: str, result: MatchResult) -> Path:
        self.directory.mkdir(parents=True, exist_ok=True)
        path = self._path(fingerprint)
        # Atomic replace so a crashed write never leaves a half-written result.
        fd, tmp = tempfile.mkstemp(dir=self.directory, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(canonical_json(result.model_dump(mode="json")))
            os.replace(tmp, path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise
        return path


def replay_scoring(result: MatchResult, policy=None) -> list[str]:
    """Recompute the deterministic score from stored judgments.

    Returns a list of drift descriptions; empty means the record reproduces
    exactly. No LLM is involved — that is the point of keeping the LLM out of
    the arithmetic in the first place.
    """
    from evidence_engine.policy import load_policy
    from evidence_engine.scoring.aggregate import calculate_overall_score
    from evidence_engine.scoring.requirement import calculate_requirement_score

    if result.status != "OK":
        return []
    policy = policy or load_policy()
    if policy.version != result.scoring_policy_version:
        return [f"policy version {policy.version} does not match recorded "
                f"{result.scoring_policy_version}; replay is not comparable"]

    spans_by_id = {s.evidence_span_id: s for s in result.evidence_spans}
    by_requirement: dict[str, list] = {}
    for judgment in result.judgments:
        by_requirement.setdefault(judgment.requirement_id, []).append(judgment)

    drift: list[str] = []
    matches = {m.requirement_id: m for m in result.matches}
    for req in result.requirements:
        recorded = matches.get(req.requirement_id)
        if recorded is None:
            drift.append(f"{req.requirement_id}: no recorded match")
            continue
        recomputed = calculate_requirement_score(
            req, by_requirement.get(req.requirement_id, []),
            result.durations.strict_relevant_years, policy,
            result.evaluated_month_index, spans_by_id)
        recomputed = round(recomputed, 4) if recomputed is not None else None
        if recomputed != recorded.score:
            drift.append(
                f"{req.requirement_id}: recorded score {recorded.score} "
                f"but judgments replay to {recomputed}")

    replayed = calculate_overall_score(
        result.requirements, matches, result.score_breakdown.role_similarity,
        result.durations, result.eligibility, result.parse_quality, policy)
    recorded_fit = result.score_breakdown.qualification_evidence_score
    if replayed.qualification_evidence_score != recorded_fit:
        drift.append(
            f"qualification_evidence_score recorded {recorded_fit} but "
            f"replays to {replayed.qualification_evidence_score}")
    return drift
