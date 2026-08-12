"""LLM candidate-fit judge — the reasoning gate for the Resume Team pipeline.

The deterministic preflight (``candidate_fit_preflight``) reads job
descriptions with regexes, and regexes cannot read the infinite ways
employers write postings: confirmed failure classes include heading-vocabulary
misses, title extraction faults, specialized-years undercounting, and — worst
— reporting its own extraction failure as a hard requirement the candidate
lacks. Those are formatting artifacts, not fit signals, and each one blocks a
real user from a role they may genuinely fit.

This module gives the final refusal decision to a reasoning model instead.
The deterministic report is demoted to advisory evidence: it still runs, its
output is still recorded (it is free telemetry and a growing disagreement
corpus), but it no longer holds veto power when a judge verdict is available.

Trust model
-----------
A judge verdict is model output and therefore untrusted until it survives
:func:`validate_candidate_fit_judge_report`, which binds it to one run/case
and the exact resume/JD texts by digest, and — the anti-hallucination
anchor — requires every cited requirement to appear verbatim (whitespace-
normalized) in the actual job description. A verdict that cites a
requirement the posting never stated is discarded, not argued with.

Failure posture: any judge failure (no SDK, no key, malformed reply,
invalid citations) raises :exc:`JudgeUnavailable`. Callers treat that as
"no override exists" and fall back to the deterministic verdict — the
pipeline degrades to its old behavior, never to an open gate.

Import safety follows ``agent.host_anthropic``: the ``anthropic`` SDK is
imported lazily inside the default transport only. ``import
candidate_fit_judge`` succeeds with no SDK installed and no key set.

Verdict stability: verdicts may be cached by ``(resume digest, JD digest,
model, prompt version)``, so one resume/posting pair keeps one verdict
across retries. (Temperature pinning is not available on current models —
non-default sampling parameters are rejected — so the cache is the
stability mechanism.)
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import date
from pathlib import Path
from typing import Any, Callable

__all__ = [
    "DEFAULT_JUDGE_MODEL",
    "JUDGE_PROMPT_VERSION",
    "JUDGE_SCHEMA_VERSION",
    "JudgeUnavailable",
    "judge_candidate_fit",
    "validate_candidate_fit_judge_report",
]

JUDGE_SCHEMA_VERSION = "candidate-fit-judge/v1"
JUDGE_PROMPT_VERSION = "fit-judge-prompt/v1"
DEFAULT_JUDGE_MODEL = "claude-sonnet-5"
_MODEL_ENV_VAR = "RESUMEHQ_FIT_JUDGE_MODEL"

_MAX_INPUT_CHARS = 120_000
_MAX_GAPS = 12
_MAX_QUOTE_CHARS = 300
_MAX_CANDIDATE_HAS_CHARS = 400
_MAX_RATIONALE_CHARS = 2_000
_MAX_MODEL_NAME_CHARS = 128
# Room for the model's JSON verdict. Adaptive thinking is disabled because
# max_tokens caps thinking and visible output together.
_MAX_OUTPUT_TOKENS = 8_000

_JUDGE_REPORT_KEYS = frozenset(
    {
        "schema_version",
        "prompt_version",
        "judge_model",
        "run_id",
        "case_id",
        "as_of_date",
        "resume_digest",
        "job_description_digest",
        "verdict",
        "fit_score",
        "hard_gaps",
        "rationale",
    }
)

_GAP_KEYS = frozenset({"requirement_quote", "candidate_has"})

_WS_RE = re.compile(r"\s+")


class JudgeUnavailable(Exception):
    """No trustworthy judge verdict could be produced for this run."""


def _canonical_json(value: Any) -> str:
    # Byte-identical to multi_agent_team._canonical_json: the runtime digests
    # this report with its own helper, so both must agree exactly.
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


def _normalize_ws(text: str) -> str:
    return _WS_RE.sub(" ", text).strip().lower()


def _finite_score(value: Any) -> bool:
    return (
        type(value) in (int, float)
        and value == value  # NaN check without math import
        and value not in (float("inf"), float("-inf"))
        and 0.0 <= float(value) <= 100.0
    )


def validate_candidate_fit_judge_report(
    report: Any,
    *,
    run_id: str,
    case_id: str,
    master_resume: str,
    job_description: str,
) -> tuple[bool, bool]:
    """Return ``(valid, proceed)`` for an untrusted judge verdict.

    Binds the report to one run/case and the exact resume/JD digests, and
    requires each cited requirement to appear verbatim (whitespace-normalized,
    case-insensitive) in the job description. Anything malformed fails closed
    to ``(False, False)``.
    """

    if not isinstance(report, dict) or set(report) != _JUDGE_REPORT_KEYS:
        return False, False
    if (
        report.get("schema_version") != JUDGE_SCHEMA_VERSION
        or report.get("prompt_version") != JUDGE_PROMPT_VERSION
        or not isinstance(report.get("judge_model"), str)
        or not (1 <= len(report["judge_model"]) <= _MAX_MODEL_NAME_CHARS)
        or report.get("run_id") != run_id
        or report.get("case_id") != case_id
        or not isinstance(run_id, str)
        or not isinstance(case_id, str)
        or not (1 <= len(run_id) <= 256)
        or not (1 <= len(case_id) <= 256)
        or report.get("resume_digest") != _digest(master_resume)
        or report.get("job_description_digest") != _digest(job_description)
        or report.get("verdict") not in {"PROCEED", "DECLINE"}
        or not _finite_score(report.get("fit_score"))
        or not isinstance(report.get("rationale"), str)
        or not (1 <= len(report["rationale"]) <= _MAX_RATIONALE_CHARS)
    ):
        return False, False

    as_of = report.get("as_of_date")
    if not isinstance(as_of, str):
        return False, False
    try:
        if date.fromisoformat(as_of).isoformat() != as_of:
            return False, False
    except ValueError:
        return False, False

    gaps = report.get("hard_gaps")
    if not isinstance(gaps, list) or len(gaps) > _MAX_GAPS:
        return False, False
    normalized_jd = _normalize_ws(job_description)
    seen_quotes: set[str] = set()
    for gap in gaps:
        if not isinstance(gap, dict) or set(gap) != _GAP_KEYS:
            return False, False
        quote = gap.get("requirement_quote")
        has = gap.get("candidate_has")
        if (
            not isinstance(quote, str)
            or not (1 <= len(quote) <= _MAX_QUOTE_CHARS)
            or not isinstance(has, str)
            or not (1 <= len(has) <= _MAX_CANDIDATE_HAS_CHARS)
        ):
            return False, False
        normalized_quote = _normalize_ws(quote)
        # The anchor: a requirement the posting never stated is a
        # hallucination, and a verdict built on one is untrustworthy.
        if not normalized_quote or normalized_quote not in normalized_jd:
            return False, False
        if normalized_quote in seen_quotes:
            return False, False
        seen_quotes.add(normalized_quote)

    # A refusal must explain itself with at least one cited requirement;
    # an unexplained DECLINE is exactly the opacity this judge replaces.
    if report["verdict"] == "DECLINE" and not gaps:
        return False, False
    return True, report["verdict"] == "PROCEED"


_SYSTEM_PROMPT = """You are the candidate-fit judge for a resume-tailoring \
pipeline whose hard rule is that it never invents experience. Your decision \
is whether tailoring should PROCEED (the candidate could honestly present \
themselves for this role and credibly reach an interview) or DECLINE (the \
gap is so large that any tailored resume would either misrepresent the \
candidate or waste their application).

Judgment rules:
- Judge only from the resume and job description texts given. Never assume \
unstated experience, and never penalize the candidate for the posting's \
formatting.
- Only explicit, non-negotiable screening requirements justify DECLINE: a \
stated minimum of years the candidate clearly lacks, a legally required \
license or degree they do not hold, or a required core competency with no \
adjacent evidence at all. Preferred/nice-to-have items never justify DECLINE.
- Transferable and adjacent experience counts toward PROCEED. Stretch roles \
are allowed; fantasy roles are not.
- A heuristic scanner's findings may be provided. They are frequently wrong. \
Verify each against the actual texts and silently discard any you cannot \
confirm.

Output exactly one JSON object, no prose, no code fences, with keys:
  "verdict": "PROCEED" or "DECLINE"
  "fit_score": integer 0-100, your calibrated estimate of candidate-role fit
  "hard_gaps": array (empty if none) of at most {max_gaps} objects, each
      {{"requirement_quote": "<verbatim quote from the job description>",
        "candidate_has": "<what the resume actually shows, one short line>"}}
  "rationale": one short paragraph a job seeker can read, plain language

"requirement_quote" MUST be copied verbatim from the job description text — \
it is checked mechanically against the posting, and any quote that does not \
appear there voids your entire verdict. DECLINE requires at least one \
hard gap."""


def _build_user_prompt(
    master_resume: str,
    job_description: str,
    deterministic_report: dict[str, Any] | None,
) -> str:
    scanner_note = "(none available)"
    if isinstance(deterministic_report, dict):
        findings = {
            "score": deterministic_report.get("score"),
            "passed": deterministic_report.get("passed"),
            "hard_knockouts": [
                {
                    "requirement": knockout.get("requirement"),
                    "candidate_has": knockout.get("candidate_has"),
                }
                for knockout in deterministic_report.get("hard_knockouts", [])
                if isinstance(knockout, dict)
            ],
        }
        scanner_note = json.dumps(findings, ensure_ascii=True)
    return (
        "JOB DESCRIPTION:\n<<<JD\n"
        + job_description
        + "\nJD>>>\n\nRESUME:\n<<<RESUME\n"
        + master_resume
        + "\nRESUME>>>\n\nHEURISTIC SCANNER FINDINGS (verify before "
        + "trusting; discard anything you cannot confirm in the texts "
        + "above):\n"
        + scanner_note
        + "\n\nReturn the JSON verdict now."
    )


def _default_llm_call(system: str, user: str, model: str) -> str:
    if model != "claude-sonnet-5":
        raise JudgeUnavailable("candidate-fit judge must use Claude Sonnet 5")
    try:
        import anthropic  # Deferred: import safety mirrors agent.host_anthropic.
    except Exception as exc:  # pragma: no cover - environment-specific
        raise JudgeUnavailable("anthropic SDK unavailable") from exc
    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise JudgeUnavailable("no API key configured")
    try:
        client = anthropic.Anthropic()
        # No sampling parameters: current models reject non-default
        # temperature/top_p with a 400. Verdict stability comes from the
        # (resume, JD, model, prompt) cache, not from temperature pinning.
        response = client.messages.create(
            model=model,
            max_tokens=_MAX_OUTPUT_TOKENS,
            thinking={"type": "disabled"},
            system=system,
            messages=[{"role": "user", "content": user}],
        )
    except Exception as exc:
        raise JudgeUnavailable("judge model call failed") from exc
    parts = [
        block.text
        for block in getattr(response, "content", [])
        if getattr(block, "type", "") == "text"
    ]
    if not parts:
        raise JudgeUnavailable("judge model returned no text")
    return "".join(parts)


_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.DOTALL)


def _extract_json_object(text: str) -> dict[str, Any]:
    candidates = [text.strip()]
    fenced = _FENCE_RE.search(text)
    if fenced:
        candidates.insert(0, fenced.group(1).strip())
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end > start:
        candidates.append(text[start : end + 1])
    for candidate in candidates:
        try:
            parsed = json.loads(candidate)
        except ValueError:
            continue
        if isinstance(parsed, dict):
            return parsed
    raise JudgeUnavailable("judge reply is not a JSON object")


def _cache_key(resume_digest: str, jd_digest: str, model: str) -> str:
    material = "|".join(
        (resume_digest, jd_digest, model, JUDGE_PROMPT_VERSION)
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


_CACHED_FIELDS = ("verdict", "fit_score", "hard_gaps", "rationale")


def _read_cache(cache_dir: Path, key: str) -> dict[str, Any] | None:
    path = cache_dir / f"{key}.json"
    try:
        raw = path.read_text(encoding="utf-8")
        payload = json.loads(raw)
    except (OSError, ValueError):
        return None
    if not isinstance(payload, dict) or set(payload) != set(_CACHED_FIELDS):
        return None
    return payload


def _write_cache(cache_dir: Path, key: str, payload: dict[str, Any]) -> None:
    try:
        cache_dir.mkdir(parents=True, exist_ok=True)
        tmp = cache_dir / f".{key}.tmp"
        tmp.write_text(_canonical_json(payload), encoding="utf-8")
        tmp.replace(cache_dir / f"{key}.json")
    except OSError:
        # Cache is an optimization; a cache write failure must never fail
        # a judged run.
        pass


def judge_candidate_fit(
    master_resume: str,
    job_description: str,
    *,
    run_id: str,
    case_id: str,
    as_of_date: str,
    deterministic_report: dict[str, Any] | None = None,
    model: str | None = None,
    llm_call: Callable[[str, str, str], str] | None = None,
    cache_dir: str | Path | None = None,
) -> dict[str, Any]:
    """Produce a validated ``candidate-fit-judge/v1`` report or raise.

    Raises :exc:`JudgeUnavailable` on any failure — callers must treat that
    as "no override exists" and let the deterministic verdict stand.
    """

    if (
        not isinstance(master_resume, str)
        or not isinstance(job_description, str)
        or not master_resume.strip()
        or not job_description.strip()
        or len(master_resume) > _MAX_INPUT_CHARS
        or len(job_description) > _MAX_INPUT_CHARS
    ):
        raise JudgeUnavailable("judge inputs out of bounds")
    try:
        if date.fromisoformat(as_of_date).isoformat() != as_of_date:
            raise ValueError
    except (TypeError, ValueError) as exc:
        raise JudgeUnavailable("invalid as-of date") from exc

    resolved_model = (
        model or os.environ.get(_MODEL_ENV_VAR) or DEFAULT_JUDGE_MODEL
    )
    if resolved_model != "claude-sonnet-5":
        raise JudgeUnavailable("candidate-fit judge must use Claude Sonnet 5")
    resume_digest = _digest(master_resume)
    jd_digest = _digest(job_description)

    def _assemble(payload: dict[str, Any]) -> dict[str, Any]:
        return {
            "schema_version": JUDGE_SCHEMA_VERSION,
            "prompt_version": JUDGE_PROMPT_VERSION,
            "judge_model": resolved_model,
            "run_id": run_id,
            "case_id": case_id,
            "as_of_date": as_of_date,
            "resume_digest": resume_digest,
            "job_description_digest": jd_digest,
            "verdict": payload.get("verdict"),
            "fit_score": payload.get("fit_score"),
            "hard_gaps": payload.get("hard_gaps"),
            "rationale": payload.get("rationale"),
        }

    cache_path = Path(cache_dir) if cache_dir is not None else None
    key = _cache_key(resume_digest, jd_digest, resolved_model)
    if cache_path is not None:
        cached = _read_cache(cache_path, key)
        if cached is not None:
            report = _assemble(cached)
            valid, _ = validate_candidate_fit_judge_report(
                report,
                run_id=run_id,
                case_id=case_id,
                master_resume=master_resume,
                job_description=job_description,
            )
            if valid:
                return report
            # A stale or corrupt cache entry falls through to a live call.

    call = llm_call or _default_llm_call
    system = _SYSTEM_PROMPT.replace("{max_gaps}", str(_MAX_GAPS)).replace(
        "{{", "{"
    ).replace("}}", "}")
    user = _build_user_prompt(master_resume, job_description, deterministic_report)
    try:
        reply_text = call(system, user, resolved_model)
    except JudgeUnavailable:
        raise
    except Exception as exc:
        raise JudgeUnavailable("judge transport failed") from exc
    if not isinstance(reply_text, str):
        raise JudgeUnavailable("judge reply is not text")

    raw = _extract_json_object(reply_text)
    if isinstance(raw.get("fit_score"), bool):
        raise JudgeUnavailable("judge verdict malformed")
    report = _assemble(
        {
            "verdict": raw.get("verdict"),
            "fit_score": raw.get("fit_score"),
            "hard_gaps": raw.get("hard_gaps"),
            "rationale": raw.get("rationale"),
        }
    )
    valid, _ = validate_candidate_fit_judge_report(
        report,
        run_id=run_id,
        case_id=case_id,
        master_resume=master_resume,
        job_description=job_description,
    )
    if not valid:
        raise JudgeUnavailable("judge verdict failed validation")
    if cache_path is not None:
        _write_cache(
            cache_path,
            key,
            {field: report[field] for field in _CACHED_FIELDS},
        )
    return report
