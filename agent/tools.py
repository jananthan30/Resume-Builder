"""Public, cloud-hostable tool registry for the ResumeHQ standalone agent.

This module is the only surface an orchestrating agent (an LLM choosing
tool calls) touches. It must import cleanly with no ``cloud/`` package
present and no vendor SDK installed -- ``cloud.*`` is therefore imported
LAZILY, inside each function body, never at module scope. Everything else
imported at module scope here (``ats_scorer``, ``hr_scorer``,
``candidate_fit_preflight``, ``evidence_audit``, ``human_voice_audit``,
``resume_integrity_audit``, ``multi_agent_team``, ``llm_scorer``,
``jd_fetcher``, ``agent.adapter``, ``agent.host_anthropic``) is a plain,
cloud-free, public module already imported the same way elsewhere in this
repository.

Every tool wraps EXISTING code -- it never reimplements scoring, auditing,
or pipeline logic. All user-data SQL goes through
``cloud.db.scoped(conn, user_id)`` (see that module for the guard); every
value, not only ``user_id``, is bound as a named parameter -- no query in
this file interpolates a quoted literal, only bare, whitelisted column
identifiers where a column name (never a value) must vary.

Registry invariant (see tests/test_agent_tools.py): no tool named
``writer``/``editor``/``auditor``/``researcher`` exists, and
``run_resume_team`` is the only tool whose result can carry resume draft
text. Default and asynchronous tailoring use the shared four-role coordinator.
The synchronous web ``/rewrite`` route instead uses a direct compiler-style
service: derive exact JD requirements, call one hosted Writer, run the same
three authoritative audits, then commit and read back the verified draft. It
has no synthetic role envelopes, protocol Auditor, or Editor.
``run_cover_letter`` also returns generated prose, but cover letters are not
resumes and were never gated behind the resume coordinator (see CLAUDE.md).
"""

from __future__ import annotations

import json
import logging
import re
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Callable

import agent.skills_loader as skills_loader
import agent.web_rewrite as web_rewrite
import ats_scorer
import candidate_fit_judge
import candidate_fit_preflight
import evidence_audit
import hr_scorer
import human_voice_audit
import jd_fetcher
import llm_scorer
import multi_agent_team
import resume_integrity_audit
from agent.adapter import AnthropicTeamAdapter
from agent.host_anthropic import AnthropicHost

__all__ = ["ToolContext", "TOOLS", "dispatch", "CloudTrustedServices"]


# =============================================================================
# Tool context
# =============================================================================


@dataclass
class ToolContext:
    """Carries the caller identity and DB handle every tool needs.

    ``run_id`` identifies this whole agent invocation (assigned by the
    caller -- e.g. the future ``/agent/*`` runner) and doubles as the
    ``agent_runs.id`` primary key for tools that record a run (currently
    ``run_resume_team`` and ``run_cover_letter``); tools that do not create
    a run simply ignore it.
    """

    user_id: int
    tier: str
    conn: Any
    run_id: str
    # True when the caller already claimed this run's quota slot and wrote its
    # agent_runs row (the async /agent/* enqueue path does this atomically, so
    # a burst of concurrent requests cannot all read the same stale count).
    # Re-checking here would then count the run against itself and refuse the
    # very last slot the caller was legitimately granted.
    slot_reserved: bool = False


# =============================================================================
# Small shared helpers (SQL timestamps, agent_runs/events bookkeeping)
# =============================================================================


def _now_utc_sql() -> str:
    """UTC timestamp in the same 'YYYY-MM-DD HH:MM:SS' shape SQLite's own
    ``datetime('now')`` produces, so lexicographic range comparisons in
    cloud.quotas stay correct regardless of which rows used the column
    default vs. an explicit value from this module."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _insert_agent_run(
    conn: Any,
    user_id: int,
    run_id: str,
    kind: str,
    *,
    input_ref: str = "",
    instruction: str | None = None,
) -> None:
    from cloud.db import scoped

    scoped(conn, user_id).q(
        """
        INSERT INTO agent_runs
            (id, user_id, kind, status, input_ref, instruction, created_at)
        VALUES
            (:id, :user_id, :kind, :status, :input_ref, :instruction, :created_at)
        """,
        {
            "id": run_id,
            "kind": kind,
            "status": "queued",
            "input_ref": input_ref,
            "instruction": instruction,
            "created_at": _now_utc_sql(),
        },
    )
    conn.commit()


def reserve_run_slot(
    conn: Any,
    user_id: int,
    tier: str,
    action: str,
    run_id: str,
    *,
    input_ref: str = "",
    instruction: str | None = None,
) -> None:
    """Atomically verify quota AND claim the slot, in one write transaction.

    Checking the quota and then inserting the row as two separate autocommitted
    steps is a double-spend: month_usage() counts agent_runs rows, so every
    concurrent request that arrives before the first one's INSERT lands reads
    the same stale count and is admitted. The async enqueue path measured 25
    accepted against a limit of 10 before it was fixed this way; the
    synchronous path had the identical hole, with a resume load sitting in the
    window to widen it.

    BEGIN IMMEDIATE takes SQLite's RESERVED lock up front, so concurrent
    callers serialize here and each one sees the rows its predecessors claimed.

    Raises QuotaExceeded without leaving a row behind.
    """
    from cloud.quotas import check_quota

    conn.commit()  # ensure no implicit transaction is already open
    conn.execute("BEGIN IMMEDIATE")
    try:
        check_quota(conn, user_id, tier, action)
        _insert_agent_run(
            conn, user_id, run_id, action,
            input_ref=input_ref, instruction=instruction,
        )
        conn.commit()
    except BaseException:
        conn.rollback()
        raise


def _finish_agent_run(
    conn: Any,
    user_id: int,
    run_id: str,
    *,
    status: str,
    result: dict | None = None,
    error: str | None = None,
    tokens_in: int = 0,
    tokens_out: int = 0,
) -> None:
    from cloud.db import scoped

    cursor = scoped(conn, user_id).q(
        """
        UPDATE agent_runs
        SET status = :status,
            result = :result,
            error = :error,
            tokens_in = :tokens_in,
            tokens_out = :tokens_out,
            finished_at = :finished_at
        WHERE id = :id AND user_id = :user_id
        """,
        {
            "id": run_id,
            "status": status,
            "result": json.dumps(result) if result is not None else None,
            "error": error,
            "tokens_in": int(tokens_in or 0),
            "tokens_out": int(tokens_out or 0),
            "finished_at": _now_utc_sql(),
        },
    )
    if cursor.rowcount != 1:
        conn.rollback()
        raise LookupError("run disappeared before terminal update")
    conn.commit()


def _record_event(
    conn: Any,
    user_id: int,
    *,
    kind: str,
    payload: dict,
    application_id: int | None = None,
) -> None:
    from cloud.db import scoped

    scoped(conn, user_id).q(
        """
        INSERT INTO events (user_id, application_id, kind, payload, created_at)
        VALUES (:user_id, :application_id, :kind, :payload, :created_at)
        """,
        {
            "application_id": application_id,
            "kind": kind,
            "payload": json.dumps(payload),
            "created_at": _now_utc_sql(),
        },
    )
    conn.commit()


def _schema(properties: dict, required: list[str]) -> dict:
    return {
        "type": "object",
        "properties": properties,
        "required": required,
        "additionalProperties": False,
    }


# =============================================================================
# score_resume / candidate_fit / fetch_jd -- pure wrappers, no quota, no run
# =============================================================================


def score_resume(ctx: ToolContext, resume_text: str, jd_text: str) -> dict:
    """Evidence match for a resume/JD pair. Wraps the evidence engine.

    This replaced ATS/HR keyword scoring. A universal ATS score does not exist
    across recruiting systems, so the answer is now requirement-level: which
    requirements the resume evidences, with the exact excerpt behind each, and
    which it does not. Absence of evidence is reported as NO_EVIDENCE_FOUND —
    never as proof the candidate lacks the qualification.

    The response carries ``open_questions``: the few gaps only the candidate
    can resolve. Answering authorises a rewrite -- it is never evidence and
    never moves the score.

    ``legacy_ats_hr`` remains available through the /score/ats and /score/hr
    endpoints for callers that still depend on the old numbers.
    """
    from evidence_engine.api import to_api_response
    from evidence_engine.audit import ResultCache
    from evidence_engine.engine import match_texts

    # Cached so a follow-up confirm_gaps on the same pair is a cache hit rather
    # than a second full match: the questions are only useful if answering them
    # is cheap.
    return to_api_response(match_texts(resume_text, jd_text, cache=ResultCache()))


def confirm_gaps(
    ctx: ToolContext, resume_text: str, jd_text: str, answers: list
) -> dict:
    """Answer the open questions from ``score_resume`` for this pair.

    ``NO_EVIDENCE_FOUND`` is the engine's most honest output and, alone, a dead
    end: it says the resume did not demonstrate something, not whether the
    candidate can. Only the candidate knows, so this is where they say.

    **An answer authorises a rewrite. An answer is never evidence.** "Yes I did
    that" does not move ``qualification_evidence_score`` and does not change
    eligibility; it permits the writer to look for and phrase experience the
    candidate actually has, and the rewritten resume is matched again before
    anything scores. Without that rule this is self-certification.

    A "no" is the only legitimate route to ``TRUE_QUALIFICATION_GAP`` — the
    engine may never infer absence from silence.

    Each answer is ``{requirement_id, answer: yes|no|unsure, detail,
    resume_sha256, job_sha256}``. The two digests come from the ``score_resume``
    response and bind an answer to one application, so a "yes" given about a
    different resume or job cannot be replayed here.
    """
    from evidence_engine.api import to_api_response
    from evidence_engine.audit import ResultCache
    from evidence_engine.engine import match_texts
    from evidence_engine.models import GapAnswer
    from evidence_engine.questions import apply_answers

    try:
        parsed = [GapAnswer(**item) for item in answers]
    except Exception as exc:
        return {"error": f"invalid answer: {exc}"}

    result = match_texts(resume_text, jd_text, cache=ResultCache())
    if result.status != "OK":
        return {"error": result.error, "status": result.status}

    updated, rejected = apply_answers(result, parsed)
    result.rewrite_recommendations = updated

    payload = to_api_response(result)
    # Named so a caller cannot mistake a silently dropped answer for an
    # accepted one.
    payload["rejected_answers"] = rejected
    return payload


def candidate_fit(
    ctx: ToolContext,
    resume_text: str,
    jd_text: str,
    threshold: float | None = None,
) -> dict:
    """Deterministic candidate-fit gate. Wraps candidate_fit_preflight.

    Advisory for the calling agent -- this does NOT gate run_resume_team;
    multi_agent_team.run_team() independently recomputes and enforces the
    same gate as a hard control before any role is invoked.

    ``threshold`` is the applicant's own score bar. 70 was never defensible as
    a universal number -- how much of a stretch a role may be is a risk
    preference, and it belongs to the person applying. Measured on a real
    corpus, the fixed bar rejected roughly two of every three genuine job
    descriptions, including a Clinical Research Associate II role scoring 68.1
    with zero knockouts against a clinical-research resume.

    The applicant's bar is applied HERE rather than inside the report, because
    candidate-fit-report/v1 pins "threshold" to const 70.0 and is digest-bound
    into the authorization receipt. Keeping the audited standard fixed and
    provable while letting the advice adapt is the point: this returns the
    unmodified report plus a ``user_bar`` block carrying the chosen bar, the
    verdict against it, and the shortfall, so a caller can say "this role
    scores 68 against your resume, 2 below your bar" instead of refusing
    without explanation.

    Only the score bar moves. Hard knockouts and untrusted extraction still
    refuse at every setting -- those are facts about the application, not
    preferences, and no bar should let a missing licence through.
    """
    run_id = f"fit:{uuid.uuid4()}"
    case_id = f"case:{uuid.uuid4()}"
    report = candidate_fit_preflight.assess_candidate_fit(
        resume_text,
        jd_text,
        run_id=run_id,
        case_id=case_id,
        as_of_date=date.today().isoformat(),
    )

    bar = candidate_fit_preflight.resolve_threshold(threshold)
    score = report.get("score") or 0.0
    blocked = bool(report.get("hard_knockouts")) or not report.get(
        "extraction_trustworthy", True
    )
    return {
        **report,
        "user_bar": {
            "threshold": bar,
            "is_default": bar == candidate_fit_preflight.CANDIDATE_FIT_THRESHOLD,
            "meets_bar": (not blocked) and score >= bar,
            "points_short": round(max(0.0, bar - score), 1),
            "blocked_by_hard_requirement": blocked,
        },
    }


def fetch_jd(ctx: ToolContext, url: str, use_ai: bool = True) -> dict:
    """Scrape a job description from a listing URL. Wraps jd_fetcher.

    Mirrors the /jobs/fetch-jd endpoint's own two-step shape: scrape, then
    (only if an API key is configured) let jd_fetcher's Sonnet 5 cleanup
    strip surrounding page noise -- that helper already no-ops safely (falls
    back to the raw text) when no key is present.
    """
    try:
        raw_text = jd_fetcher.fetch_jd_from_url(url)
    except jd_fetcher.BlockedURLError as exc:
        return {"ok": False, "jd_text": "", "error": str(exc)}
    if not raw_text:
        return {
            "ok": False,
            "jd_text": "",
            "error": (
                "Could not extract text from this URL. The site may block "
                "scrapers (LinkedIn, Indeed). Please paste the JD manually."
            ),
        }
    jd_text = raw_text
    if use_ai and len(raw_text) > 400:
        jd_text = jd_fetcher.extract_jd_with_ai(raw_text)
    return {"ok": True, "jd_text": jd_text, "char_count": len(jd_text)}


def read_skill(ctx: ToolContext, skill_name: str) -> dict:
    """Return the full guidance text for one server-side skill. Wraps
    agent.skills_loader.read_skill (Task 11); ``ctx`` is unused -- this is a
    pure, quota-free, DB-free read of a small bundled file.

    The parameter is ``skill_name``, not ``name`` -- ``dispatch(name, ctx,
    **kwargs)``'s own first parameter is called ``name`` (the TOOL's name),
    so a tool argument also called ``name`` cannot be passed as a keyword
    through ``dispatch()`` without colliding with it.

    Raises ``ValueError`` (not the loader's own ``KeyError``) for an unknown
    ``skill_name`` -- ``dispatch()`` itself already uses ``KeyError`` to mean
    "no such TOOL is registered", so a bad skill name (a normal, expected
    input a caller might get wrong) is translated to the input-validation
    exception class every other tool in this file already raises for bad
    arguments (see e.g. run_resume_team's "jd_text must be a non-empty
    string"), keeping the two failure modes distinguishable.
    """
    try:
        content = skills_loader.read_skill(skill_name)
    except KeyError:
        raise ValueError(f"unknown skill: {skill_name!r}") from None
    return {"name": skill_name, "content": content}


# =============================================================================
# CloudTrustedServices -- SQL-backed trust boundary for hosted resume execution
# =============================================================================


class CloudTrustedServices:
    """SQL-backed trust boundary shared by hosted resume execution paths.

    Both the four-role coordinator and direct web rewrite service use these
    code-owned candidate-fit, source-attestation, audit, event, publication,
    and durable-readback operations. The three authorization votes call the
    real evidence, human-voice, and canonical-integrity engines in-process;
    model output never supplies a vote or publication receipt.
    """

    def __init__(
        self,
        conn: Any,
        user_id: int,
        run_id: str,
        case_id: str,
        master_resume: str,
    ) -> None:
        self.conn = conn
        self.user_id = user_id
        self.run_id = run_id
        self.case_id = case_id
        self._master_resume = master_resume
        self.last_published_draft: str | None = None

    # --- claim_run ----------------------------------------------------
    def claim_run(self, run_id: str, case_id: str) -> dict[str, Any]:
        if run_id != self.run_id or case_id != self.case_id:
            raise ValueError("run identity mismatch")
        from cloud.db import scoped

        cursor = scoped(self.conn, self.user_id).q(
            """
            UPDATE agent_runs
            SET status = :new_status
            WHERE id = :id AND user_id = :user_id AND status = :old_status
            """,
            {"id": run_id, "new_status": "running", "old_status": "queued"},
        )
        self.conn.commit()
        claimed = cursor.rowcount == 1
        return {
            "schema_version": multi_agent_team.RUN_CLAIM_VERSION,
            "run_id": run_id,
            "case_id": case_id,
            "claimed": claimed,
            "claim_id": f"claim:{run_id}" if claimed else "",
        }

    # --- attest_source --------------------------------------------------
    def attest_source(self, master_resume: str) -> dict[str, Any]:
        return {
            "schema_version": multi_agent_team.SOURCE_ATTESTATION_VERSION,
            "trusted": True,
            "source_id": f"user-resume:{self.user_id}",
            "source_digest": multi_agent_team.canonical_digest(master_resume),
        }

    # --- assess_candidate_fit -------------------------------------------
    def assess_candidate_fit(
        self, master_resume: str, job_description: str, run_id: str, case_id: str
    ) -> dict[str, Any]:
        return candidate_fit_preflight.assess_candidate_fit(
            master_resume,
            job_description,
            run_id=run_id,
            case_id=case_id,
            as_of_date=date.today().isoformat(),
        )

    # --- judge_candidate_fit ---------------------------------------------
    def judge_candidate_fit(
        self,
        master_resume: str,
        job_description: str,
        run_id: str,
        case_id: str,
        deterministic_report: dict[str, Any],
    ) -> dict[str, Any]:
        """Live reasoning-judge verdict, consulted only on deterministic
        rejection.

        The runtime revalidates whatever this returns
        (validate_candidate_fit_judge_report digest-binds it and verifies
        every citation), so this seam only has to produce, cache, and log —
        never to be trusted. Raises on any failure; the runtime then lets
        the deterministic rejection stand.
        """
        from cloud.config import settings as cloud_settings

        cache_dir = (
            Path(cloud_settings.DB_PATH).resolve().parent / "fit_judge_cache"
        )
        try:
            report = candidate_fit_judge.judge_candidate_fit(
                master_resume,
                job_description,
                run_id=run_id,
                case_id=case_id,
                as_of_date=date.today().isoformat(),
                deterministic_report=deterministic_report,
                cache_dir=cache_dir,
            )
        except Exception:
            # The runtime treats any raise as "no override exists" and keeps
            # the deterministic rejection — correct, but it swallows the
            # exception, so this seam is the only place the operator can see
            # WHY the judge never ruled (bad model ID, 400, timeout, ...).
            logging.getLogger("scorer.fit_judge").exception(
                "fit-judge unavailable run=%s det_score=%s",
                run_id,
                (deterministic_report or {}).get("score"),
            )
            raise
        # Every consultation is, by construction, a deterministic rejection —
        # so each log line is one row of the scanner-vs-judge disagreement
        # corpus (judge PROCEED = scanner overruled).
        logging.getLogger("scorer.fit_judge").info(
            "fit-judge run=%s det_score=%s det_knockouts=%d judge=%s "
            "judge_score=%s",
            run_id,
            (deterministic_report or {}).get("score"),
            len((deterministic_report or {}).get("hard_knockouts") or []),
            report.get("verdict"),
            report.get("fit_score"),
        )
        return report

    # --- audit_draft: three real, in-process deterministic votes --------
    @staticmethod
    def _vote(name: str, passed: bool, codes: list[str], draft_digest: str) -> dict[str, Any]:
        return {
            "schema_version": multi_agent_team.VOTE_VERSION,
            "name": name,
            "invocation_id": f"cloud:{name}:{uuid.uuid4()}",
            "passed": passed,
            "draft_digest": draft_digest,
            "codes": codes,
        }

    def audit_draft(self, draft: str) -> dict[str, Any]:
        draft_digest = multi_agent_team.canonical_digest(draft)

        evidence_report = evidence_audit.audit_text(draft)
        evidence_passed = (
            isinstance(evidence_report, dict)
            and evidence_report.get("passed") is True
        )
        evidence_codes = [] if evidence_passed else ["EVIDENCE_UNSUPPORTED_ITEMS"]

        voice_report = human_voice_audit.audit_text(draft, mode="resume")
        voice_passed = (
            isinstance(voice_report, dict)
            and voice_report.get("passed") is True
        )
        voice_codes = [] if voice_passed else ["HUMAN_VOICE_AUDIT_FAILED"]

        integrity_report = resume_integrity_audit.audit_resume_text(
            self._master_resume, draft
        )
        integrity_passed = (
            isinstance(integrity_report, dict)
            and integrity_report.get("passed") is True
        )
        integrity_codes = (
            []
            if integrity_passed
            else (
                integrity_report.get("difference_codes")
                if isinstance(integrity_report, dict)
                else None
            )
            or ["CANONICAL_INTEGRITY_FAILED"]
        )

        votes = [
            self._vote("evidence", evidence_passed, evidence_codes, draft_digest),
            self._vote("human_voice", voice_passed, voice_codes, draft_digest),
            self._vote(
                "canonical_integrity", integrity_passed, integrity_codes, draft_digest
            ),
        ]
        all_codes = [code for vote in votes for code in vote["codes"]]
        passed = all(vote["passed"] for vote in votes)

        findings: list[dict[str, Any]] = []
        if not passed:
            # human_voice_audit already returns findings in exactly the shape
            # the coordinator's editor loop consumes: actionable=True plus
            # locations carrying line_number, the verbatim source_line, and a
            # line_digest that agrees with multi_agent_team._digest. Synthesizing
            # placeholders instead -- actionable=False, locations=[] -- made
            # every prose failure unfixable by construction, so a draft that
            # merely opened a bullet with "Spearheaded" terminated the whole run
            # as REJECTED:NONACTIONABLE_HUMAN_VOICE with nothing for the Editor
            # to act on.
            findings.extend(_actionable_voice_findings(voice_report))

            # The other two audits have no per-line data to give. evidence_audit
            # reports unsupported ITEMS and resume_integrity_audit reports
            # document-level difference codes; neither can point at a draft line,
            # so their failures stay non-actionable and still stop the run. That
            # is correct rather than lazy: a finding the Editor cannot locate
            # must not be presented as one it can fix.
            for vote in votes:
                if vote["name"] == "human_voice":
                    continue
                for code in vote["codes"]:
                    findings.append(
                        {
                            "id": f"{vote['name']}-{uuid.uuid4()}",
                            "vote_name": vote["name"],
                            "code": code,
                            "actionable": False,
                            "locations": [],
                        }
                    )

        return {
            "schema_version": multi_agent_team.AUTHORIZATION_VERSION,
            "draft_digest": draft_digest,
            "passed": passed,
            "codes": all_codes,
            "votes": votes,
            "findings": findings,
        }

    # --- record_event -----------------------------------------------------
    def record_event(self, event: str, payload: dict[str, Any]) -> None:
        _record_event(self.conn, self.user_id, kind=event, payload=payload)

    # --- publish / verify_publication -------------------------------------
    def publish(self, draft: str, metadata: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(metadata, dict):
            raise ValueError("invalid publication metadata")
        draft_digest = multi_agent_team.canonical_digest(draft)
        if metadata.get("draft_digest") != draft_digest:
            raise ValueError("draft digest mismatch at publish")

        publication_id = f"pub:{uuid.uuid4()}"

        # Durable readback proof: write into this run's own agent_runs row
        # (the SQL analogue of LocalTrustedServices writing resume.md to
        # disk) so read_publication()/verify_publication() read it from storage --
        # not from an in-memory attribute -- before the pipeline is allowed
        # to treat it as committed.
        from cloud.db import scoped

        cursor = scoped(self.conn, self.user_id).q(
            """
            UPDATE agent_runs SET result = :result
            WHERE id = :id AND user_id = :user_id AND status = :status
            """,
            {
                "id": self.run_id,
                "status": "running",
                "result": json.dumps(
                    {
                        "publication_id": publication_id,
                        "draft": draft,
                        "metadata": metadata,
                    }
                ),
            },
        )
        if cursor.rowcount != 1:
            self.conn.rollback()
            raise LookupError("run disappeared during publication")
        self.conn.commit()
        self.last_published_draft = draft

        return {
            "schema_version": multi_agent_team.PUBLICATION_VERSION,
            "committed": True,
            "draft_digest": draft_digest,
            "target_digest": draft_digest,
            "publication_id": publication_id,
        }

    def read_publication(self, publication_id: str) -> dict[str, Any]:
        """Read one committed draft from SQL, never from process memory."""

        from cloud.db import scoped

        row = scoped(self.conn, self.user_id).q(
            "SELECT result FROM agent_runs WHERE id = :id AND user_id = :user_id",
            {"id": self.run_id},
        ).fetchone()
        if row is None or row["result"] is None:
            raise LookupError("unknown publication")
        stored = json.loads(row["result"])
        if (
            not isinstance(stored, dict)
            or set(stored) != {"publication_id", "draft", "metadata"}
            or stored.get("publication_id") != publication_id
            or not isinstance(stored.get("draft"), str)
            or not isinstance(stored.get("metadata"), dict)
        ):
            raise LookupError("unknown publication")
        return stored

    def verify_publication(self, publication_id: str) -> dict[str, Any]:
        stored = self.read_publication(publication_id)
        draft = stored["draft"]
        metadata = stored["metadata"]
        digest = multi_agent_team.canonical_digest(draft)
        authorization_receipt = {
            "schema_version": multi_agent_team.FINAL_RECEIPT_VERSION,
            "run_id": metadata["run_id"],
            "case_id": metadata["case_id"],
            "draft_digest": digest,
            "source_digest": metadata["source_digest"],
            "job_description_digest": metadata["job_description_digest"],
            "candidate_fit_report": metadata["candidate_fit_report"],
            "candidate_fit_report_digest": metadata["candidate_fit_report_digest"],
            "candidate_fit_judge_report": metadata.get(
                "candidate_fit_judge_report"
            ),
            "candidate_fit_judge_report_digest": metadata.get(
                "candidate_fit_judge_report_digest", ""
            ),
            "researcher_agent_id": metadata["researcher_agent_id"],
            "researcher_artifact_digest": metadata["researcher_artifact_digest"],
            "auditor_attestation": metadata["auditor_attestation"],
            "authorization_digest": metadata["authorization_digest"],
            "authorization_report": metadata["authorization_report"],
            "vote_invocation_ids": metadata["vote_invocation_ids"],
            "publication_id": publication_id,
            "verified_target_digest": digest,
        }
        authorization_receipt_digest = multi_agent_team.canonical_digest(
            authorization_receipt
        )
        return {
            "schema_version": multi_agent_team.PUBLICATION_VERIFICATION_VERSION,
            "verified": True,
            "publication_id": publication_id,
            "target_digest": digest,
            "authorization_receipt": authorization_receipt,
            "authorization_receipt_digest": authorization_receipt_digest,
            "authorization_receipt_path": (
                f"resume-team-receipt.{multi_agent_team.canonical_digest(publication_id)}.json"
            ),
        }


# =============================================================================
# run_resume_team -- THE ONLY tool that may return resume draft text
# =============================================================================

_TAILOR_MAX_EDITOR_ATTEMPTS = 2
_TAILOR_ROLE_TIMEOUT_SECONDS = 120.0


def _default_team_adapter() -> AnthropicTeamAdapter:
    """Construct the production Anthropic-backed team adapter.

    A thin, monkeypatchable seam: tests replace this whole function to
    inject a fake-client-backed adapter, so run_resume_team's public
    signature never needs a test-only parameter, and production code always
    goes through the real AnthropicHost/AnthropicTeamAdapter pair.
    """
    return AnthropicTeamAdapter(host=AnthropicHost())


def _default_writer_host() -> AnthropicHost:
    """Construct the sole language-model boundary used by web rewrites."""
    return AnthropicHost()


def _admit_writer_stats(value: Any) -> dict[str, Any] | None:
    """Admit only the coordinator's fixed, count-only Writer diagnostics."""

    try:
        return multi_agent_team._admit_writer_stats(value)
    except Exception:
        return None


# Leading list markers: "- ", "* ", "• ", "1. ", "2) ", en/em dashes.
_LIST_MARKER_RE = re.compile(r"^[ \t]*(?:[-*•·–—]|\(?\d{1,2}[.)])[ \t]+")


def _actionable_voice_findings(voice_report: Any) -> list[dict[str, Any]]:
    """Convert human_voice_audit failures into editor-actionable findings.

    The audit already emits what the coordinator wants, so this is a filter and
    a rename rather than a translation: keep the failures that declare
    themselves actionable and carry at least one well-formed location, and give
    each a unique id.

    Anything malformed is dropped rather than repaired. A location whose
    line_number, source_line, or line_digest disagrees with the draft is
    rejected by the coordinator as UNEDITABLE_DETERMINISTIC_AUDIT, which fails
    the entire run -- so one bad location would cost more than the finding is
    worth. Dropping it leaves the vote failing and the run stopping, which is
    the same outcome as before this function existed.
    """
    if not isinstance(voice_report, dict):
        return []

    findings: list[dict[str, Any]] = []
    for failure in voice_report.get("failures") or []:
        if not isinstance(failure, dict) or failure.get("actionable") is not True:
            continue
        code = failure.get("code")
        if not isinstance(code, str) or not code:
            continue

        locations = [
            {
                "line_number": location["line_number"],
                "source_line": location["source_line"],
                "line_digest": location["line_digest"],
            }
            for location in (failure.get("locations") or [])
            if isinstance(location, dict)
            and type(location.get("line_number")) is int
            and location["line_number"] > 0
            and isinstance(location.get("source_line"), str)
            and isinstance(location.get("line_digest"), str)
        ]
        if not locations:
            continue

        findings.append(
            {
                "id": f"human_voice-{uuid.uuid4()}",
                "vote_name": "human_voice",
                "code": code,
                "actionable": True,
                "locations": locations,
            }
        )
    return findings


def _flatten_list_markers(jd_text: str) -> str:
    """Remove leading bullet/number markers from every job-description line.

    The pipeline anchors each requirement to a complete line of the source
    text, and the coordinator compares those anchors byte-for-byte. That makes
    the model responsible for reproducing a line exactly -- including a leading
    "- " that carries no meaning. Models tidy such markers away by reflex, and
    they do it inconsistently: the same job description would anchor cleanly on
    one run and fail with "researcher evidence must use one complete JD line"
    on the next, taking the whole run down with it.

    Prompting could not fix that reliably; an explicit rule plus a worked
    example still left it a coin flip. So the marker is removed before the
    pipeline ever sees the text. The model has nothing to strip, and its
    natural output matches. This removes a failure mode rather than asking the
    model to avoid one.

    Only leading markers go. Wording, order, blank lines, and internal
    punctuation are untouched, so the requirements a user reads are unchanged
    and every downstream digest stays computed over one consistent source.

    If flattening would make two previously distinct lines identical, the text
    is returned unchanged: the uniqueness rule protecting anchor resolution
    matters more than the convenience, and it fails closed either way.
    """
    lines = jd_text.split("\n")
    flattened = [_LIST_MARKER_RE.sub("", line).rstrip() for line in lines]

    significant = [line.strip() for line in flattened if line.strip()]
    if len(significant) != len(set(significant)):
        return jd_text
    return "\n".join(flattened)


# A sentence boundary strong enough to split on: terminal punctuation
# followed by whitespace and a capital/numeral start.
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?;])\s+(?=[A-Z0-9(\"'])")
_PROSE_LINE_MIN_CHARS = 200


def _split_prose_lines(jd_text: str) -> str:
    """Break paragraph-blob job descriptions into one sentence per line.

    Job-board scrapes (the descriptions stored on overnight matches) arrive
    as long prose paragraphs. The pipeline's evidence contract anchors each
    requirement to ONE COMPLETE line of the source, verified byte-for-byte —
    a requirement buried mid-paragraph therefore cannot be anchored at all,
    and every such run dies as FAILED:AGENT_PAYLOAD_SCHEMA ("evidence must
    use one complete JD line") no matter which model runs the Researcher.
    Observed live on 2026-08-11 across consecutive runs on two models.

    Same philosophy as _flatten_list_markers above: normalize the source
    once instead of asking a model to work around it. Only long lines with a
    real sentence boundary are split; wording and order are untouched, so
    the requirements a user reads are unchanged. If splitting would create
    duplicate lines, the text is returned unchanged — the anchor-uniqueness
    rule matters more, and it fails closed either way.
    """
    out: list = []
    changed = False
    for line in jd_text.split("\n"):
        if len(line) >= _PROSE_LINE_MIN_CHARS:
            parts = [p.strip() for p in _SENTENCE_SPLIT_RE.split(line) if p.strip()]
            if len(parts) > 1:
                out.extend(parts)
                changed = True
                continue
        out.append(line)
    if not changed:
        return jd_text
    significant = [line.strip() for line in out if line.strip()]
    if len(significant) != len(set(significant)):
        return jd_text
    return "\n".join(out)


def run_resume_team(
    ctx: ToolContext,
    jd_text: str,
    instruction: str | None = None,
    resume_text: str | None = None,
    pipeline_mode: str = "four_role",
) -> dict:
    """Run an audited tailoring path and return only a verified draft.

    THE ONLY tool in this registry whose result can contain resume draft
    text. Checks quota FIRST: a run that never starts (quota exhausted, no
    resume on file) never touches agent_runs, so there is nothing to
    "refund". A run that starts but does not publish is recorded as
    status='failed', which cloud.quotas.month_usage excludes from its count.

    ``pipeline_mode="four_role"`` is the default used by direct and async
    tailoring. The web-only ``"single_writer"`` mode bypasses role
    coordination entirely: it calls one hosted Writer, requires two read-only
    citation-bound factual reviews for proposed changes, compiles the unanimous
    anchored subset, runs three code-owned votes, and returns only committed SQL
    readback. Native and async execution remain unchanged.

    ``resume_text``, when a non-empty string, is a ONE-OFF source resume for
    THIS RUN ONLY -- it is used to tailor this draft and is never written to
    the account's saved resume. This is what lets a caller (e.g. POST
    /rewrite with a request-provided resume_text) honor a one-off variant
    without silently overwriting a carefully maintained saved resume. When
    ``resume_text`` is ``None`` (the default) or blank, the saved resume is
    loaded exactly as before.

    ``instruction`` is recorded on the agent_runs row for observability but
    is not injected into either execution path in this version.
    """
    if pipeline_mode not in {"four_role", "single_writer"}:
        raise ValueError("pipeline_mode must be 'four_role' or 'single_writer'")
    single_writer = pipeline_mode == "single_writer"

    from cloud.quotas import check_quota

    # Advisory fast fail: answer an out-of-allowance caller before doing any
    # work, with the message that actually explains their situation. The
    # AUTHORITATIVE check is inside reserve_run_slot below, which re-checks
    # under a write lock -- this one races and is not relied on.
    if not ctx.slot_reserved:
        check_quota(ctx.conn, ctx.user_id, ctx.tier, "tailor")

    # Validate and load the resume BEFORE claiming a slot, so a request that
    # was never runnable cannot consume the caller's allowance.
    if not isinstance(jd_text, str) or not jd_text.strip():
        raise ValueError("jd_text must be a non-empty string")
    jd_text = jd_text.replace("\r\n", "\n").replace("\r", "\n")
    jd_text = _split_prose_lines(_flatten_list_markers(jd_text))

    if isinstance(resume_text, str) and resume_text.strip():
        master_resume = resume_text
    else:
        import cloud.auth

        record = cloud.auth.get_resume(ctx.user_id)
        if not record or not record.get("resume_text"):
            raise ValueError(
                "No resume on file. Upload a resume before requesting a tailored draft."
            )
        master_resume = record["resume_text"]

    run_id = ctx.run_id
    case_id = f"case:{run_id}"

    if not ctx.slot_reserved:
        # One transaction for the check and the claim -- see reserve_run_slot.
        reserve_run_slot(
            ctx.conn, ctx.user_id, ctx.tier, "tailor", run_id,
            input_ref=multi_agent_team.canonical_digest(jd_text),
            instruction=instruction,
        )

    services = CloudTrustedServices(ctx.conn, ctx.user_id, run_id, case_id, master_resume)

    try:
        if single_writer:
            host = _default_writer_host()
            if (
                not callable(getattr(host, "run_web_writer", None))
                or not callable(getattr(host, "review_replacements", None))
                or not hasattr(host, "budget")
            ):
                raise TypeError("writer host factory returned an invalid host")
            result = web_rewrite.run_web_rewrite(
                run_id=run_id,
                case_id=case_id,
                master_resume=master_resume,
                job_description=jd_text,
                host=host,
                services=services,
            )
            budget = host.budget
        else:
            request = {
                "schema_version": multi_agent_team.PROTOCOL_VERSION,
                "run_id": run_id,
                "case_id": case_id,
                "job_description": jd_text,
                "master_resume": master_resume,
                "output_dir": f"agent://tailor/{ctx.user_id}/{run_id}",
                "max_editor_attempts": _TAILOR_MAX_EDITOR_ATTEMPTS,
                "role_timeout_seconds": _TAILOR_ROLE_TIMEOUT_SECONDS,
            }
            adapter = _default_team_adapter()
            result = multi_agent_team.run_team(request, adapter, services)
            budget = adapter.host.budget
        if not isinstance(result, dict):
            raise TypeError("resume execution returned a non-object")
    except Exception:  # noqa: BLE001 -- any crash here is a failed run
        try:
            _finish_agent_run(
                ctx.conn, ctx.user_id, run_id, status="failed", error="AGENT_CRASH"
            )
            _record_event(
                ctx.conn,
                ctx.user_id,
                kind="tailor_run_failed",
                payload={
                    "run_id": run_id,
                    "terminal_class": "AGENT_CRASH",
                    "pipeline_mode": pipeline_mode,
                },
            )
        except Exception:
            pass
        return {"run_id": run_id, "status": "failed", "error": "AGENT_CRASH"}

    writer_stats = (
        _admit_writer_stats(result.get("writer_stats")) if single_writer else None
    )
    tokens_in = budget.input_tokens
    tokens_out = budget.output_tokens

    draft_text = (
        result.get("final_draft")
        if single_writer
        else services.last_published_draft
    )
    if (
        result.get("terminal_class") == "PUBLISHED"
        and result.get("published") is True
        and isinstance(draft_text, str)
        and bool(draft_text)
    ):
        try:
            _finish_agent_run(
                ctx.conn,
                ctx.user_id,
                run_id,
                status="succeeded",
                result={
                    "draft": draft_text,
                    "authorization_receipt": result.get(
                        "authorization_receipt"
                    ),
                    "authorization_receipt_digest": result.get(
                        "authorization_receipt_digest"
                    ),
                    "candidate_fit_report_digest": result.get(
                        "candidate_fit_report_digest"
                    ),
                },
                tokens_in=tokens_in,
                tokens_out=tokens_out,
            )
            _record_event(
                ctx.conn,
                ctx.user_id,
                kind="tailor_run_succeeded",
                payload={
                    "run_id": run_id,
                    "terminal_class": result["terminal_class"],
                    "pipeline_mode": pipeline_mode,
                },
            )
        except Exception:
            try:
                _finish_agent_run(
                    ctx.conn,
                    ctx.user_id,
                    run_id,
                    status="failed",
                    error="FAILED:PUBLICATION_ATOMICITY",
                    tokens_in=tokens_in,
                    tokens_out=tokens_out,
                )
            except Exception:
                pass
            return {
                "run_id": run_id,
                "status": "failed",
                "error": "FAILED:PUBLICATION_ATOMICITY",
                "candidate_fit_report": result.get("candidate_fit_report"),
                "candidate_fit_judge_report": result.get(
                    "candidate_fit_judge_report"
                ),
            }
        return {
            "run_id": run_id,
            "status": "succeeded",
            "draft": draft_text,
            "candidate_fit_report": result.get("candidate_fit_report"),
            "authorization_receipt_digest": result.get("authorization_receipt_digest"),
        }

    terminal_class = result.get("terminal_class") or "FAILED:UNKNOWN"
    if terminal_class == "PUBLISHED":
        terminal_class = "FAILED:PUBLICATION_VERIFICATION"
    try:
        _finish_agent_run(
            ctx.conn,
            ctx.user_id,
            run_id,
            status="failed",
            error=terminal_class,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
        )
    except Exception:
        return {"run_id": run_id, "status": "failed", "error": "AGENT_CRASH"}
    failed_event = {
        "run_id": run_id,
        "terminal_class": terminal_class,
        "pipeline_mode": pipeline_mode,
    }
    if writer_stats is not None:
        failed_event["writer_stats"] = writer_stats
    try:
        _record_event(
            ctx.conn,
            ctx.user_id,
            kind="tailor_run_failed",
            payload=failed_event,
        )
    except Exception:
        pass
    failed_result = {
        "run_id": run_id,
        "status": "failed",
        "error": terminal_class,
        "candidate_fit_report": result.get("candidate_fit_report"),
        "candidate_fit_judge_report": result.get("candidate_fit_judge_report"),
    }
    if writer_stats is not None:
        failed_result["writer_stats"] = writer_stats
    return failed_result


# =============================================================================
# run_cover_letter -- single-shot LLM generation, not the four-role pipeline
# =============================================================================


def run_cover_letter(
    ctx: ToolContext,
    jd_text: str,
    company: str | None = None,
    title: str | None = None,
    resume_text: str | None = None,
) -> dict:
    """Generate a tailored cover letter. Wraps llm_scorer.generate_cover_letter
    plus the human_voice_audit hard gate, quota-checked, run/event-recorded.

    Cover letters are not resumes: they were never behind the audited
    four-role pipeline (see CLAUDE.md), so this tool -- unlike
    run_resume_team -- talks to the model directly through the existing
    single-shot generator rather than through AnthropicHost/run_team.

    ``resume_text``, when a non-empty string, is a ONE-OFF source resume for
    THIS RUN ONLY -- it is never written to the account's saved resume
    (mirrors run_resume_team's identical parameter). When ``None`` (the
    default) or blank, the saved resume is loaded exactly as before.
    """
    from cloud.quotas import check_quota

    # Advisory fast fail; reserve_run_slot below is authoritative. See
    # run_resume_team for why both exist.
    if not ctx.slot_reserved:
        check_quota(ctx.conn, ctx.user_id, ctx.tier, "cover_letter")

    # Validate and load before claiming a slot (mirrors run_resume_team).
    if not isinstance(jd_text, str) or not jd_text.strip():
        raise ValueError("jd_text must be a non-empty string")
    jd_text = _split_prose_lines(_flatten_list_markers(jd_text))

    if isinstance(resume_text, str) and resume_text.strip():
        source_resume_text = resume_text
    else:
        import cloud.auth

        record = cloud.auth.get_resume(ctx.user_id)
        if not record or not record.get("resume_text"):
            raise ValueError(
                "No resume on file. Upload a resume before requesting a cover letter."
            )
        source_resume_text = record["resume_text"]

    run_id = ctx.run_id
    if not ctx.slot_reserved:
        # One transaction for the check and the claim -- see reserve_run_slot.
        reserve_run_slot(
            ctx.conn, ctx.user_id, ctx.tier, "cover_letter", run_id,
            input_ref=multi_agent_team.canonical_digest(jd_text),
            instruction=json.dumps({"company": company, "title": title}),
        )

    try:
        generated = llm_scorer.generate_cover_letter(
            resume_text=source_resume_text,
            jd_text=jd_text,
            company_name=company or "",
            job_title=title or "",
        )
    except Exception as error:  # noqa: BLE001 -- any crash here is a failed run
        _finish_agent_run(
            ctx.conn, ctx.user_id, run_id, status="failed", error=f"GENERATION_CRASH:{error}"
        )
        _record_event(
            ctx.conn,
            ctx.user_id,
            kind="cover_letter_run_failed",
            payload={"run_id": run_id, "error": "GENERATION_CRASH"},
        )
        return {"run_id": run_id, "status": "failed", "error": "GENERATION_CRASH"}

    if generated.get("error") or not generated.get("full_text"):
        error_code = str(generated.get("error") or "EMPTY_GENERATION")
        _finish_agent_run(ctx.conn, ctx.user_id, run_id, status="failed", error=error_code)
        _record_event(
            ctx.conn,
            ctx.user_id,
            kind="cover_letter_run_failed",
            payload={"run_id": run_id, "error": error_code},
        )
        return {"run_id": run_id, "status": "failed", "error": error_code}

    voice_report = human_voice_audit.audit_text(generated["full_text"], mode="cover_letter")
    if not voice_report.get("passed"):
        _finish_agent_run(
            ctx.conn, ctx.user_id, run_id, status="failed", error="HUMAN_VOICE_AUDIT_FAILED"
        )
        _record_event(
            ctx.conn,
            ctx.user_id,
            kind="cover_letter_run_failed",
            payload={"run_id": run_id, "error": "HUMAN_VOICE_AUDIT_FAILED"},
        )
        return {
            "run_id": run_id,
            "status": "failed",
            "error": "HUMAN_VOICE_AUDIT_FAILED",
            "voice_failures": voice_report.get("failures", []),
        }

    _finish_agent_run(
        ctx.conn,
        ctx.user_id,
        run_id,
        status="succeeded",
        result={
            "full_text": generated["full_text"],
            "company": generated.get("company"),
            "job_title": generated.get("job_title"),
            "word_count": generated.get("word_count"),
        },
    )
    _record_event(
        ctx.conn, ctx.user_id, kind="cover_letter_run_succeeded", payload={"run_id": run_id}
    )
    return {
        "run_id": run_id,
        "status": "succeeded",
        "full_text": generated["full_text"],
        "paragraphs": generated.get("paragraphs", []),
        "company": generated.get("company"),
        "job_title": generated.get("job_title"),
        "word_count": generated.get("word_count"),
    }


# =============================================================================
# Tracker + preferences tools -- mirror the T4 endpoint SQL shapes via scoped()
# =============================================================================

_VALID_TARGET_TIERS = {"IC", "Sr", "Manager", "AD", "Director"}
_VALID_FIT_LABELS = {"MEETS", "STRETCH", "MISS"}

_LOG_APPLICATION_OPTIONAL_FIELDS = (
    "status",
    "resume_file",
    "cover_letter_file",
    "ats_score",
    "hr_score",
    "llm_score",
    "notes",
    "applied_at",
    "jd_ref",
    "jd_text",
    "target_tier",
    "fit_label",
    "hard_reqs_missed",
    "referral_source",
)

_UPDATE_APPLICATION_FIELDS = (
    "status",
    "notes",
    "resume_file",
    "cover_letter_file",
    "applied_at",
    "jd_ref",
    "jd_text",
    "target_tier",
    "fit_label",
    "hard_reqs_missed",
    "referral_source",
)

_BREAKDOWN_COLUMNS = ("target_tier", "fit_label", "referral_source")


def _validate_classification(target_tier: Any, fit_label: Any) -> None:
    if target_tier is not None and target_tier not in _VALID_TARGET_TIERS:
        raise ValueError(f"target_tier must be one of {sorted(_VALID_TARGET_TIERS)}")
    if fit_label is not None and fit_label not in _VALID_FIT_LABELS:
        raise ValueError(f"fit_label must be one of {sorted(_VALID_FIT_LABELS)}")


def log_application(ctx: ToolContext, company: str, title: str, **optional: Any) -> dict:
    """Add a job application row. Mirrors POST /tracker/add's SQL shape."""
    from cloud.db import scoped

    unknown = set(optional) - set(_LOG_APPLICATION_OPTIONAL_FIELDS)
    if unknown:
        raise ValueError(f"unsupported log_application fields: {sorted(unknown)}")
    _validate_classification(optional.get("target_tier"), optional.get("fit_label"))

    params = {
        "company": company,
        "job_title": title,
        "status": optional.get("status", "Applied"),
        "resume_file": optional.get("resume_file", ""),
        "cover_letter_file": optional.get("cover_letter_file", ""),
        "ats_score": optional.get("ats_score"),
        "hr_score": optional.get("hr_score"),
        "llm_score": optional.get("llm_score"),
        "notes": optional.get("notes", ""),
        "applied_at": optional.get("applied_at"),
        "jd_ref": optional.get("jd_ref"),
        "jd_text": optional.get("jd_text"),
        "target_tier": optional.get("target_tier"),
        "fit_label": optional.get("fit_label"),
        "hard_reqs_missed": optional.get("hard_reqs_missed"),
        "referral_source": optional.get("referral_source"),
    }
    cursor = scoped(ctx.conn, ctx.user_id).q(
        """
        INSERT INTO job_applications
            (user_id, company, job_title, status, resume_file, cover_letter_file,
             ats_score, hr_score, llm_score, notes, applied_at, jd_ref, jd_text,
             target_tier, fit_label, hard_reqs_missed, referral_source)
        VALUES
            (:user_id, :company, :job_title, :status, :resume_file, :cover_letter_file,
             :ats_score, :hr_score, :llm_score, :notes, :applied_at, :jd_ref, :jd_text,
             :target_tier, :fit_label, :hard_reqs_missed, :referral_source)
        """,
        params,
    )
    ctx.conn.commit()
    return {"id": cursor.lastrowid, "status": "added"}


def update_application(ctx: ToolContext, application_id: int, **fields: Any) -> dict:
    """Partial-update a job application row owned by this user."""
    from cloud.db import scoped

    unknown = set(fields) - set(_UPDATE_APPLICATION_FIELDS)
    if unknown:
        raise ValueError(f"unsupported update_application fields: {sorted(unknown)}")
    if not fields:
        return {"status": "no_change"}
    _validate_classification(fields.get("target_tier"), fields.get("fit_label"))

    # Field names come only from the fixed whitelist tuple above (never from
    # attacker-controlled text), so interpolating them as bare identifiers
    # here mirrors the existing, reviewed pattern in scorer_server.py's
    # _tracker_pipeline_breakdown -- every VALUE is still bound as a named
    # parameter below.
    set_clauses = [f"{field} = :{field}" for field in fields]
    set_clauses.append("updated_at = :updated_at")
    params = dict(fields)
    params["updated_at"] = _now_utc_sql()
    params["application_id"] = application_id
    sql = (
        f"UPDATE job_applications SET {', '.join(set_clauses)} "
        "WHERE id = :application_id AND user_id = :user_id"
    )
    scoped(ctx.conn, ctx.user_id).q(sql, params)
    ctx.conn.commit()
    return {"status": "updated"}


def _breakdown(conn: Any, user_id: int, column: str) -> dict[str, dict[str, Any]]:
    from cloud.db import scoped

    if column not in _BREAKDOWN_COLUMNS:
        raise ValueError(f"unsupported breakdown column: {column!r}")
    rows = scoped(conn, user_id).q(
        f"""
        SELECT {column} AS bucket,
               COUNT(*) AS cnt,
               SUM(CASE WHEN rejection_reason IN (:auto, :screen, :interview)
                        THEN 1 ELSE 0 END) AS rejected_cnt,
               SUM(CASE WHEN responded_at IS NOT NULL
                          OR COALESCE(interview_stage, 0) > 0
                          OR (rejection_reason IS NOT NULL
                              AND rejection_reason != :silence)
                        THEN 1 ELSE 0 END) AS responded_cnt
        FROM job_applications
        WHERE user_id = :user_id AND {column} IS NOT NULL
        GROUP BY {column}
        """,
        # Bound, not inlined: scoped() rejects any quoted literal in the SQL it
        # runs. Mirrors the definitions in scorer_server -- rejected keys on the
        # typed reason rather than free-text status, and a response means the
        # employer engaged at all, which the old responded_at-only rule could
        # not see (it was set solely alongside a rejection, capping the metric
        # at the rejection rate).
        {
            "auto": "auto_reject",
            "screen": "screen_reject",
            "interview": "interview_reject",
            "silence": "no_response",
        },
    ).fetchall()
    return {
        row["bucket"]: {
            "count": row["cnt"],
            "rejected": row["rejected_cnt"],
            "response_rate": round(row["responded_cnt"] / row["cnt"], 4) if row["cnt"] else 0.0,
        }
        for row in rows
    }


def pipeline_stats(ctx: ToolContext) -> dict:
    """Deterministic SQL pipeline-health aggregates. Mirrors GET /insights/pipeline."""
    import statistics

    from cloud.db import scoped

    conn, user_id = ctx.conn, ctx.user_id
    total = scoped(conn, user_id).q(
        "SELECT COUNT(*) AS c FROM job_applications WHERE user_id = :user_id"
    ).fetchone()["c"]
    rejected = scoped(conn, user_id).q(
        "SELECT COUNT(*) AS c FROM job_applications WHERE user_id = :user_id AND status = :status",
        {"status": "Rejected"},
    ).fetchone()["c"]
    active = scoped(conn, user_id).q(
        "SELECT COUNT(*) AS c FROM job_applications WHERE user_id = :user_id AND closed_at IS NULL"
    ).fetchone()["c"]

    by_tier = _breakdown(conn, user_id, "target_tier")
    by_fit = _breakdown(conn, user_id, "fit_label")
    by_source = _breakdown(conn, user_id, "referral_source")

    reason_rows = scoped(conn, user_id).q(
        """
        SELECT rejection_reason, COUNT(*) AS c
        FROM job_applications
        WHERE user_id = :user_id AND rejection_reason IS NOT NULL
        GROUP BY rejection_reason
        """
    ).fetchall()
    by_rejection_reason = {row["rejection_reason"]: row["c"] for row in reason_rows}

    day_rows = scoped(conn, user_id).q(
        """
        SELECT (julianday(responded_at) - julianday(applied_at)) AS days
        FROM job_applications
        WHERE user_id = :user_id AND applied_at IS NOT NULL AND responded_at IS NOT NULL
        """
    ).fetchall()
    day_values = [row["days"] for row in day_rows if row["days"] is not None]
    median_days = statistics.median(day_values) if day_values else None

    return {
        "totals": {"applications": total, "rejected": rejected, "active": active},
        "by_tier": by_tier,
        "by_fit": by_fit,
        "by_source": by_source,
        "median_days_to_response": median_days,
        "by_rejection_reason": by_rejection_reason,
    }


def get_profile(ctx: ToolContext) -> dict:
    """Return every stored preference row for this user."""
    from cloud.db import scoped

    rows = scoped(ctx.conn, ctx.user_id).q(
        """
        SELECT key, value, source, confidence, updated_at
        FROM preferences
        WHERE user_id = :user_id
        ORDER BY key
        """
    ).fetchall()
    return {"preferences": [dict(row) for row in rows]}


def update_preference(
    ctx: ToolContext,
    key: str,
    value: Any,
    source: str = "explicit",
    confidence: float = 1.0,
) -> dict:
    """Upsert one preference row (user_id, key) -> value."""
    from cloud.db import scoped

    if not isinstance(key, str) or not key.strip():
        raise ValueError("preference key must be a non-empty string")
    value_text = str(value)
    scoped(ctx.conn, ctx.user_id).q(
        """
        INSERT INTO preferences (user_id, key, value, source, confidence, updated_at)
        VALUES (:user_id, :key, :value, :source, :confidence, :updated_at)
        ON CONFLICT(user_id, key) DO UPDATE SET
            value = excluded.value,
            source = excluded.source,
            confidence = excluded.confidence,
            updated_at = excluded.updated_at
        """,
        {
            "key": key,
            "value": value_text,
            "source": source,
            "confidence": float(confidence),
            "updated_at": _now_utc_sql(),
        },
    )
    ctx.conn.commit()
    return {"key": key, "value": value_text, "status": "saved"}


# =============================================================================
# Registry + dispatch
# =============================================================================

TOOLS: dict[str, dict[str, Any]] = {
    "score_resume": {
        "schema": _schema(
            {
                "resume_text": {"type": "string"},
                "jd_text": {"type": "string"},
            },
            ["resume_text", "jd_text"],
        ),
        "fn": score_resume,
    },
    "confirm_gaps": {
        "schema": _schema(
            {
                "resume_text": {"type": "string"},
                "jd_text": {"type": "string"},
                "answers": {
                    "type": "array",
                    "description": (
                        "Answers to the open_questions from score_resume. Each "
                        "is {requirement_id, answer: yes|no|unsure, detail, "
                        "resume_sha256, job_sha256}. Copy both digests from the "
                        "score_resume response -- they bind an answer to this "
                        "exact resume and job. An answer authorises a rewrite; "
                        "it never changes the score."
                    ),
                    "items": {
                        "type": "object",
                        "properties": {
                            "requirement_id": {"type": "string"},
                            "answer": {
                                "type": "string",
                                "enum": ["yes", "no", "unsure"],
                            },
                            "detail": {
                                "type": "string",
                                "description": (
                                    "The candidate's own words on where they "
                                    "did this. Quoted back to the writer, never "
                                    "treated as evidence."
                                ),
                            },
                            "resume_sha256": {"type": "string"},
                            "job_sha256": {"type": "string"},
                        },
                        "required": [
                            "requirement_id", "answer",
                            "resume_sha256", "job_sha256",
                        ],
                    },
                },
            },
            ["resume_text", "jd_text", "answers"],
        ),
        "fn": confirm_gaps,
    },
    "candidate_fit": {
        "schema": _schema(
            {
                "resume_text": {"type": "string"},
                "jd_text": {"type": "string"},
                "threshold": {
                    "type": "number",
                    "description": (
                        "The applicant's own score bar, 40-95. Omit for the "
                        "default policy. Only the score bar moves -- hard "
                        "knockouts such as a missing licence stay "
                        "non-negotiable at every setting."
                    ),
                },
            },
            ["resume_text", "jd_text"],
        ),
        "fn": candidate_fit,
    },
    "fetch_jd": {
        "schema": _schema(
            {
                "url": {"type": "string"},
                "use_ai": {"type": "boolean"},
            },
            ["url"],
        ),
        "fn": fetch_jd,
    },
    "read_skill": {
        "schema": _schema(
            {
                "skill_name": {
                    "type": "string",
                    "description": (
                        "Skill slug to read, e.g. 'tailor-resume', "
                        "'cover-letter', 'job-fit', or 'writing-coach'."
                    ),
                },
            },
            ["skill_name"],
        ),
        "fn": read_skill,
    },
    "run_resume_team": {
        "schema": _schema(
            {
                "jd_text": {"type": "string"},
                "instruction": {"type": "string"},
                "resume_text": {
                    "type": "string",
                    "description": (
                        "Optional one-off resume text to tailor from for "
                        "this run only; does not change the saved resume."
                    ),
                },
            },
            ["jd_text"],
        ),
        "fn": run_resume_team,
    },
    "run_cover_letter": {
        "schema": _schema(
            {
                "jd_text": {"type": "string"},
                "company": {"type": "string"},
                "title": {"type": "string"},
                "resume_text": {
                    "type": "string",
                    "description": (
                        "Optional one-off resume text to write from for "
                        "this run only; does not change the saved resume."
                    ),
                },
            },
            ["jd_text"],
        ),
        "fn": run_cover_letter,
    },
    "log_application": {
        "schema": _schema(
            {
                "company": {"type": "string"},
                "title": {"type": "string"},
                "status": {"type": "string"},
                "resume_file": {"type": "string"},
                "cover_letter_file": {"type": "string"},
                "ats_score": {"type": "number"},
                "hr_score": {"type": "number"},
                "llm_score": {"type": "number"},
                "notes": {"type": "string"},
                "applied_at": {"type": "string"},
                "jd_ref": {"type": "string"},
                "jd_text": {
                    "type": "string",
                    "description": "Full job description text — required later by run-by-application_id tailoring",
                },
                "target_tier": {"type": "string", "enum": sorted(_VALID_TARGET_TIERS)},
                "fit_label": {"type": "string", "enum": sorted(_VALID_FIT_LABELS)},
                "hard_reqs_missed": {"type": "integer"},
                "referral_source": {"type": "string"},
            },
            ["company", "title"],
        ),
        "fn": log_application,
    },
    "update_application": {
        "schema": _schema(
            {
                "application_id": {"type": "integer"},
                "status": {"type": "string"},
                "notes": {"type": "string"},
                "resume_file": {"type": "string"},
                "cover_letter_file": {"type": "string"},
                "applied_at": {"type": "string"},
                "jd_ref": {"type": "string"},
                "jd_text": {
                    "type": "string",
                    "description": "Full job description text — backfill to enable run-by-application_id tailoring",
                },
                "target_tier": {"type": "string", "enum": sorted(_VALID_TARGET_TIERS)},
                "fit_label": {"type": "string", "enum": sorted(_VALID_FIT_LABELS)},
                "hard_reqs_missed": {"type": "integer"},
                "referral_source": {"type": "string"},
            },
            ["application_id"],
        ),
        "fn": update_application,
    },
    "pipeline_stats": {
        "schema": _schema({}, []),
        "fn": pipeline_stats,
    },
    "get_profile": {
        "schema": _schema({}, []),
        "fn": get_profile,
    },
    "update_preference": {
        "schema": _schema(
            {
                "key": {"type": "string"},
                "value": {"type": "string"},
                "source": {"type": "string"},
                "confidence": {"type": "number"},
            },
            ["key", "value"],
        ),
        "fn": update_preference,
    },
}


def dispatch(name: str, ctx: ToolContext, **kwargs: Any) -> Any:
    """Look up ``name`` in :data:`TOOLS` and call it with ``ctx``.

    Raises ``KeyError`` for an unknown tool name -- callers (an orchestrating
    agent choosing a tool by name) must not be able to invoke anything not
    explicitly published in this registry.
    """
    try:
        entry = TOOLS[name]
    except KeyError:
        raise KeyError(f"unknown tool: {name!r}") from None
    fn: Callable[..., Any] = entry["fn"]
    return fn(ctx, **kwargs)
