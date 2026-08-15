"""Direct, fail-closed pipeline for synchronous web resume rewrites.

The web route uses one Writer call plus two independent factual-support reviews.
Requirement selection, structural compilation, candidate-fit policy, the three
code-owned audits, publication, and durable readback remain deterministic.
Native and asynchronous Resume Team execution continue to use
``multi_agent_team.run_team``.
"""

from __future__ import annotations

import re
import secrets
from collections import Counter
from typing import Any

import human_voice_audit
import multi_agent_team
from candidate_fit_preflight import WEB_REWRITE_FIT_FLOOR
import resume_integrity_audit
from agent.host_anthropic import BudgetExceeded, HostRefusal

WEB_REWRITE_RESULT_VERSION = "web-rewrite-result/v1"
WEB_REWRITE_PUBLICATION_VERSION = "web-rewrite-publication/v1"
WEB_REWRITE_RECEIPT_VERSION = "web-rewrite-final-receipt/v1"
SEMANTIC_REVIEW_VERSION = "web-semantic-review/v1"
_SEMANTIC_REVIEW_CODES = {
    "PASS",
    "UNSUPPORTED_CLAIM",
    "STRONGER_SCOPE",
    "FABRICATED_METRIC",
    "CANONICAL_FACT_CHANGED",
}

_MANDATORY_REQUIREMENT_RE = re.compile(
    r"\b(?:must|required|minimum|at\s+least|\d+\+?\s+years?|"
    r"degree|certification|license|mandatory)\b",
    re.IGNORECASE,
)
_JD_HEADING_RE = re.compile(
    r"^(?:requirements?|qualifications?|responsibilities|about(?:\s+the\s+job)?|benefits)\s*:?$",
    re.IGNORECASE,
)


def derive_requirement_rubric(job_description: str) -> dict[str, list[str]]:
    """Select exact, unique JD lines for the Writer without an agent handoff."""

    if not isinstance(job_description, str):
        raise ValueError("job_description must be text")
    lines = job_description.split("\n")
    stripped_counts = Counter(line.strip() for line in lines)
    retained = [
        line
        for line in lines
        if line.strip()
        and re.search(r"[A-Za-z0-9]", line) is not None
        and _JD_HEADING_RE.fullmatch(line.strip()) is None
        and stripped_counts[line.strip()] == 1
    ]
    if not retained:
        raise ValueError("job description has no unique substantive line")
    hard = [line for line in retained if _MANDATORY_REQUIREMENT_RE.search(line)]
    soft = [line for line in retained if not _MANDATORY_REQUIREMENT_RE.search(line)]
    return {"hard_requirements": hard, "soft_requirements": soft}


def _outcome(
    run_id: str,
    case_id: str,
    terminal_class: str,
    *,
    published: bool = False,
    final_draft: str = "",
    authorization_receipt: dict[str, Any] | None = None,
    candidate_fit_report: dict[str, Any] | None = None,
    candidate_fit_report_digest: str = "",
    candidate_fit_judge_report: dict[str, Any] | None = None,
    candidate_fit_judge_report_digest: str = "",
    writer_stats: dict[str, Any] | None = None,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "schema_version": WEB_REWRITE_RESULT_VERSION,
        "run_id": run_id,
        "case_id": case_id,
        "terminal_class": terminal_class,
        "published": published,
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
    if writer_stats is not None:
        result["writer_stats"] = writer_stats
    if published:
        assert authorization_receipt is not None
        result.update(
            {
                "final_draft": final_draft,
                "final_draft_digest": multi_agent_team.canonical_digest(final_draft),
                "authorization_receipt": authorization_receipt,
                "authorization_receipt_digest": multi_agent_team.canonical_digest(
                    authorization_receipt
                ),
            }
        )
    return result


def _valid_publication_receipt(receipt: Any, draft: str) -> bool:
    digest = multi_agent_team.canonical_digest(draft)
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
        and receipt.get("schema_version") == multi_agent_team.PUBLICATION_VERSION
        and receipt.get("committed") is True
        and receipt.get("draft_digest") == digest
        and receipt.get("target_digest") == digest
        and isinstance(receipt.get("publication_id"), str)
        and bool(receipt["publication_id"])
    )


def _authorization_summary(report: dict[str, Any]) -> str:
    parts: list[str] = []
    for vote in report["votes"]:
        outcome = "PASS" if vote["passed"] is True else ",".join(vote["codes"])
        parts.append(f"{vote['name']}:{outcome}")
    return "|".join(parts)


def _proposal_id(
    item: dict[str, str],
    *,
    run_id: str,
    case_id: str,
    source_digest: str,
) -> str:
    """Digest-bind one pair to its exact run, case, and immutable source."""

    return multi_agent_team.canonical_digest(
        {
            "run_id": run_id,
            "case_id": case_id,
            "source_digest": source_digest,
            "source_span_text": item["source_span_text"],
            "replacement_text": item["replacement_text"],
        }
    )


def _experience_role_for_line(
    master_resume: str, line: str
) -> dict[str, Any] | None:
    """Return exact experience-role ownership for one unique complete line."""

    start, end, _ = multi_agent_team._unique_span(master_resume, line)
    return multi_agent_team._role_at(
        multi_agent_team._experience_roles(master_resume), start, end
    )


def _role_bullet_prefix(line: str) -> str | None:
    """Return the exact structural prefix of a recognized experience bullet."""

    matched = re.match(r"^(\s*[•*-]\s+)", line)
    return matched.group(1) if matched is not None else None


def _is_section_heading(line: str) -> bool:
    """Use the real voice parser plus all canonical section names."""

    canonical_heading = multi_agent_team._section_heading_name(line)
    return (
        human_voice_audit._is_section_header(line)
        or re.match(r"^\s*#{1,6}\s+\S", line) is not None
        or canonical_heading
        in (
            set(resume_integrity_audit.PROTECTED_SECTIONS)
            | resume_integrity_audit.UNPROTECTED_SECTIONS
        )
    )


def _anchor_is_web_editable(master_resume: str, source: str) -> bool:
    """Freeze canonical identity rows and every section heading byte-for-byte."""

    if _is_section_heading(source) or (
        multi_agent_team._is_role_bullet(source)
        and _role_bullet_prefix(source) is None
    ):
        return False
    role = _experience_role_for_line(master_resume, source)
    if role is not None:
        return _role_bullet_prefix(source) is not None

    current_heading = ""
    for line in master_resume.split("\n"):
        heading = multi_agent_team._section_heading_name(line)
        if (
            heading in resume_integrity_audit.PROTECTED_SECTIONS
            or heading in resume_integrity_audit.UNPROTECTED_SECTIONS
        ):
            current_heading = heading
        if line == source:
            return (
                heading not in resume_integrity_audit.PROTECTED_SECTIONS
                and heading not in resume_integrity_audit.UNPROTECTED_SECTIONS
                and current_heading in resume_integrity_audit.UNPROTECTED_SECTIONS
            )
    return False


def _review_candidates(
    replacements: Any,
    master_resume: str,
) -> tuple[list[dict[str, str]], Counter[str]]:
    """Admit only well-shaped, uniquely anchored, non-empty proposals for review."""

    if not isinstance(replacements, list):
        raise ValueError("replacements must be a list")
    admitted: list[dict[str, str]] = []
    rejected: Counter[str] = Counter()
    seen_sources: set[str] = set()
    for item in replacements:
        if (
            not isinstance(item, dict)
            or set(item) != {"source_span_text", "replacement_text"}
            or not isinstance(item.get("source_span_text"), str)
            or not isinstance(item.get("replacement_text"), str)
        ):
            rejected["INVALID_ITEM"] += 1
            continue
        source = item["source_span_text"]
        replacement = item["replacement_text"]
        if (
            master_resume.count(source) != 1
            or source in seen_sources
            or not multi_agent_team._covers_complete_source_line(
                master_resume,
                master_resume.find(source),
                master_resume.find(source) + len(source),
            )
            or not _anchor_is_web_editable(master_resume, source)
        ):
            rejected["INVALID_ANCHOR"] += 1
            continue
        source_prefix = _role_bullet_prefix(source)
        replacement_prefix = _role_bullet_prefix(replacement)
        if (
            not replacement
            or replacement == source
            or not replacement.strip()
            or re.search(r"[A-Za-z0-9]", replacement) is None
            or "\n" in replacement
            or "\r" in replacement
            or source_prefix != replacement_prefix
            or (
                source_prefix is not None
                and not multi_agent_team._is_genuine_role_bullet(replacement)
            )
            or _is_section_heading(replacement)
            or not multi_agent_team._candidate_text_format_valid(replacement)
        ):
            rejected["STRICT_COMPILER"] += 1
            continue
        seen_sources.add(source)
        admitted.append(item)
    return admitted, rejected


def _evidence_line_valid_for_candidate(
    *,
    master_resume: str,
    candidate: dict[str, str],
    evidence_line: str,
) -> bool:
    """Require one exact unique citation, local to an experience role when relevant."""

    try:
        evidence_start, evidence_end, _ = multi_agent_team._unique_span(
            master_resume, evidence_line
        )
        if not multi_agent_team._covers_complete_source_line(
            master_resume, evidence_start, evidence_end
        ):
            return False
        source_start, source_end, _ = multi_agent_team._unique_span(
            master_resume, candidate["source_span_text"]
        )
    except Exception:
        return False
    roles = multi_agent_team._experience_roles(master_resume)
    source_role = multi_agent_team._role_at(roles, source_start, source_end)
    evidence_role = multi_agent_team._role_at(
        roles, evidence_start, evidence_end
    )
    return source_role is None or (
        evidence_role is not None and evidence_role["key"] == source_role["key"]
    )


def _validate_semantic_review(
    report: Any,
    candidates: list[dict[str, str]],
    *,
    master_resume: str,
    run_id: str,
    case_id: str,
    source_digest: str,
    proposal_set_digest: str,
    lens: str,
    invocation_id: str,
) -> dict[str, Any]:
    """Validate exact bindings, citations, types, and decisions for one lens."""

    expected_ids = [
        _proposal_id(
            item,
            run_id=run_id,
            case_id=case_id,
            source_digest=source_digest,
        )
        for item in candidates
    ]
    if (
        not isinstance(report, dict)
        or set(report)
        != {
            "schema_version",
            "run_id",
            "case_id",
            "source_digest",
            "proposal_set_digest",
            "lens",
            "invocation_id",
            "decisions",
        }
        or report.get("schema_version") != SEMANTIC_REVIEW_VERSION
        or report.get("run_id") != run_id
        or report.get("case_id") != case_id
        or report.get("source_digest") != source_digest
        or report.get("proposal_set_digest") != proposal_set_digest
        or report.get("lens") != lens
        or report.get("invocation_id") != invocation_id
    ):
        raise ValueError("semantic review binding")
    decisions = report["decisions"]
    if not isinstance(decisions, list) or len(decisions) != len(candidates):
        raise ValueError("semantic review count")
    normalized: list[dict[str, Any]] = []
    for expected_id, candidate, decision in zip(
        expected_ids, candidates, decisions
    ):
        if (
            not isinstance(decision, dict)
            or set(decision)
            != {"proposal_id", "supported", "code", "evidence_lines"}
            or decision.get("proposal_id") != expected_id
            or type(decision.get("supported")) is not bool
            or type(decision.get("code")) is not str
            or decision["code"] not in _SEMANTIC_REVIEW_CODES
            or not isinstance(decision.get("evidence_lines"), list)
            or not all(
                type(line) is str and bool(line)
                for line in decision["evidence_lines"]
            )
            or len(decision["evidence_lines"])
            != len(set(decision["evidence_lines"]))
            or (decision["supported"] and decision["code"] != "PASS")
            or (not decision["supported"] and decision["code"] == "PASS")
            or (decision["supported"] and not decision["evidence_lines"])
            or any(
                not _evidence_line_valid_for_candidate(
                    master_resume=master_resume,
                    candidate=candidate,
                    evidence_line=line,
                )
                for line in decision["evidence_lines"]
            )
        ):
            raise ValueError("semantic review decision")
        normalized.append(
            {
                "proposal_id": expected_id,
                "supported": decision["supported"],
                "code": decision["code"],
                "evidence_line_digests": [
                    multi_agent_team.canonical_digest(line)
                    for line in decision["evidence_lines"]
                ],
            }
        )
    return {
        "schema_version": SEMANTIC_REVIEW_VERSION,
        "run_id": run_id,
        "case_id": case_id,
        "source_digest": source_digest,
        "proposal_set_digest": proposal_set_digest,
        "lens": lens,
        "invocation_id": invocation_id,
        "review_digest": multi_agent_team.canonical_digest(report),
        "decisions": normalized,
    }


def _unanimously_supported(
    candidates: list[dict[str, str]],
    attestations: list[dict[str, Any]],
) -> list[dict[str, str]]:
    """Return candidates receiving exact PASS from every distinct review lens."""

    if (
        len(attestations) != 2
        or len({attestation["lens"] for attestation in attestations}) != 2
        or len(
            {attestation["invocation_id"] for attestation in attestations}
        )
        != 2
        or len({attestation["review_digest"] for attestation in attestations})
        != 2
    ):
        raise ValueError("semantic review independence")
    supported: list[dict[str, str]] = []
    for index, candidate in enumerate(candidates):
        if all(
            attestation["decisions"][index]["supported"] is True
            for attestation in attestations
        ):
            supported.append(candidate)
    return supported


def run_web_rewrite(
    *,
    run_id: str,
    case_id: str,
    master_resume: str,
    job_description: str,
    host: Any,
    services: Any,
) -> dict[str, Any]:
    """Generate, authorize, commit, and read back one synchronous web draft."""

    if not all(
        isinstance(value, str) and bool(value)
        for value in (run_id, case_id, master_resume, job_description)
    ) or not callable(getattr(host, "run_web_writer", None)) or not callable(
        getattr(host, "review_replacements", None)
    ):
        return _outcome(run_id, case_id, "FAILED:REQUEST_SCHEMA")
    required_services = (
        "claim_run",
        "attest_source",
        "assess_candidate_fit",
        "record_event",
        "audit_draft",
        "publish",
        "read_publication",
    )
    if any(not callable(getattr(services, name, None)) for name in required_services):
        return _outcome(run_id, case_id, "FAILED:SERVICE_UNAVAILABLE")

    request_identity = {"run_id": run_id, "case_id": case_id}
    try:
        run_claim = services.claim_run(run_id, case_id)
    except Exception:
        return _outcome(run_id, case_id, "FAILED:RUN_CLAIM")
    if not multi_agent_team._validate_run_claim(run_claim, request_identity):
        terminal = (
            "FAILED:REPLAY"
            if isinstance(run_claim, dict) and run_claim.get("claimed") is False
            else "FAILED:RUN_CLAIM"
        )
        return _outcome(run_id, case_id, terminal)

    try:
        source_attestation = services.attest_source(master_resume)
    except Exception:
        return _outcome(run_id, case_id, "FAILED:SOURCE_ATTESTATION")
    if not multi_agent_team._validate_source_attestation(
        source_attestation, master_resume
    ):
        return _outcome(run_id, case_id, "FAILED:SOURCE_ATTESTATION")

    try:
        candidate_fit_report = services.assess_candidate_fit(
            master_resume, job_description, run_id, case_id
        )
    except Exception:
        return _outcome(run_id, case_id, "FAILED:CANDIDATE_FIT_PREFLIGHT")
    fit_valid, _ = multi_agent_team.validate_recomputed_candidate_fit_report(
        candidate_fit_report,
        run_id=run_id,
        case_id=case_id,
        master_resume=master_resume,
        job_description=job_description,
    )
    if not fit_valid:
        return _outcome(run_id, case_id, "FAILED:CANDIDATE_FIT_PREFLIGHT")

    candidate_fit_report_digest = multi_agent_team.canonical_digest(
        candidate_fit_report
    )
    candidate_fit_judge_report: dict[str, Any] | None = None
    candidate_fit_judge_report_digest = ""
    score = candidate_fit_report.get("score")
    authoritative_fit_passed = (
        candidate_fit_report.get("passed") is True
        and candidate_fit_report.get("extraction_trustworthy") is True
        and candidate_fit_report.get("hard_knockouts") == []
        and candidate_fit_report.get("codes") == []
        and type(score) in (int, float)
        and not isinstance(score, bool)
        and score >= WEB_REWRITE_FIT_FLOOR
    )
    if not authoritative_fit_passed:
        # Web rewriting requires the canonical deterministic policy itself to
        # pass. A reasoning judge cannot waive extraction ambiguity, a hard
        # requirement, rejection codes, or the authoritative score floor.
        return _outcome(
            run_id,
            case_id,
            "REJECTED:CANDIDATE_FIT",
            candidate_fit_report=candidate_fit_report,
            candidate_fit_report_digest=candidate_fit_report_digest,
        )

    def fail(
        terminal_class: str, writer_stats: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        return _outcome(
            run_id,
            case_id,
            terminal_class,
            candidate_fit_report=candidate_fit_report,
            candidate_fit_report_digest=candidate_fit_report_digest,
            candidate_fit_judge_report=candidate_fit_judge_report,
            candidate_fit_judge_report_digest=candidate_fit_judge_report_digest,
            writer_stats=writer_stats,
        )

    def record(event: str, payload: dict[str, Any]) -> bool:
        try:
            services.record_event(event, payload)
        except Exception:
            return False
        return True

    try:
        requirement_rubric = derive_requirement_rubric(job_description)
    except Exception:
        return fail("FAILED:RESEARCH_SCHEMA")
    if not record(
        "rewrite_requirements_ready",
        {
            "hard_requirement_count": len(
                requirement_rubric["hard_requirements"]
            ),
            "soft_requirement_count": len(
                requirement_rubric["soft_requirements"]
            ),
        },
    ):
        return fail("FAILED:PREPUBLICATION")

    try:
        raw_writer = host.run_web_writer(
            {
                "master_resume": master_resume,
                "requirement_rubric": requirement_rubric,
            },
            case_id=case_id,
            run_id=run_id,
        )
    except (HostRefusal, BudgetExceeded, ConnectionError, TimeoutError):
        return fail("FAILED:AGENT_UNAVAILABLE")
    except Exception:
        return fail("FAILED:AGENT_CRASH")
    if not isinstance(raw_writer, dict) or set(raw_writer) != {"replacements"}:
        return fail("FAILED:WRITER_SCHEMA")

    proposed_replacements = raw_writer["replacements"]
    if not isinstance(proposed_replacements, list):
        return fail("FAILED:WRITER_SCHEMA")
    if not proposed_replacements:
        compiled, raw_writer_stats = (
            multi_agent_team.compile_semantically_reviewed_writer_replacements_salvage(
                master_resume, []
            )
        )
        semantic_review = {
            "schema_version": SEMANTIC_REVIEW_VERSION,
            "run_id": run_id,
            "case_id": case_id,
            "source_digest": source_attestation["source_digest"],
            "proposal_set_digest": multi_agent_team.canonical_digest([]),
            "review_invocation_ids": [],
            "passed_count": 0,
            "rejected_count": 0,
            "attestations": [],
        }
        pre_rejections: Counter[str] = Counter()
        semantic_rejections = 0
    else:
        try:
            review_candidates, pre_rejections = _review_candidates(
                proposed_replacements, master_resume
            )
        except ValueError:
            return fail("FAILED:WRITER_SCHEMA")
        if not review_candidates:
            writer_stats = multi_agent_team._admit_writer_stats(
                {
                    "proposed_count": len(proposed_replacements),
                    "accepted_count": 0,
                    "rejected_count": len(proposed_replacements),
                    "rejection_codes": dict(sorted(pre_rejections.items())),
                }
            )
            return fail("REJECTED:NO_SAFE_CHANGES", writer_stats)
        source_digest = source_attestation["source_digest"]
        review_payload = [
            {
                "proposal_id": _proposal_id(
                    item,
                    run_id=run_id,
                    case_id=case_id,
                    source_digest=source_digest,
                ),
                "source_span_text": item["source_span_text"],
                "replacement_text": item["replacement_text"],
            }
            for item in review_candidates
        ]
        proposal_set_digest = multi_agent_team.canonical_digest(review_payload)
        semantic_attestations: list[dict[str, Any]] = []
        for lens in ("claim_entailment", "skeptical_recruiter"):
            invocation_id = (
                f"semantic:{lens}:{run_id}:{secrets.token_hex(16)}"
            )
            try:
                raw_semantic_review = host.review_replacements(
                    master_resume=master_resume,
                    proposals=review_payload,
                    case_id=case_id,
                    run_id=run_id,
                    source_digest=source_digest,
                    proposal_set_digest=proposal_set_digest,
                    lens=lens,
                    invocation_id=invocation_id,
                )
            except (HostRefusal, BudgetExceeded, ConnectionError, TimeoutError):
                return fail("FAILED:AGENT_UNAVAILABLE")
            except Exception:
                return fail("FAILED:AGENT_CRASH")
            try:
                semantic_attestations.append(
                    _validate_semantic_review(
                        raw_semantic_review,
                        review_candidates,
                        master_resume=master_resume,
                        run_id=run_id,
                        case_id=case_id,
                        source_digest=source_digest,
                        proposal_set_digest=proposal_set_digest,
                        lens=lens,
                        invocation_id=invocation_id,
                    )
                )
            except (TypeError, ValueError):
                return fail("FAILED:SEMANTIC_REVIEW_SCHEMA")
        try:
            supported_replacements = _unanimously_supported(
                review_candidates, semantic_attestations
            )
        except ValueError:
            return fail("FAILED:SEMANTIC_REVIEW_SCHEMA")
        semantic_rejections = len(review_candidates) - len(
            supported_replacements
        )
        semantic_review = {
            "schema_version": SEMANTIC_REVIEW_VERSION,
            "run_id": run_id,
            "case_id": case_id,
            "source_digest": source_digest,
            "proposal_set_digest": proposal_set_digest,
            "review_invocation_ids": [
                item["invocation_id"] for item in semantic_attestations
            ],
            "passed_count": len(supported_replacements),
            "rejected_count": semantic_rejections,
            "attestations": semantic_attestations,
        }
        if not supported_replacements:
            rejection_codes = Counter(pre_rejections)
            rejection_codes["SEMANTIC_SUPPORT"] += semantic_rejections
            writer_stats = multi_agent_team._admit_writer_stats(
                {
                    "proposed_count": len(proposed_replacements),
                    "accepted_count": 0,
                    "rejected_count": len(proposed_replacements),
                    "rejection_codes": dict(sorted(rejection_codes.items())),
                }
            )
            return fail("REJECTED:NO_SAFE_CHANGES", writer_stats)
        try:
            compiled, raw_writer_stats = (
                multi_agent_team.compile_semantically_reviewed_writer_replacements_salvage(
                    master_resume, supported_replacements
                )
            )
        except multi_agent_team.NoSafeWriterChanges as error:
            raw_writer_stats = dict(error.stats)
            compiled = None
        except ValueError:
            return fail("FAILED:WRITER_SCHEMA")
        except Exception:
            return fail("FAILED:AGENT_CRASH")
        if compiled is None:
            rejection_codes = Counter(pre_rejections)
            rejection_codes["SEMANTIC_SUPPORT"] += semantic_rejections
            rejection_codes.update(raw_writer_stats["rejection_codes"])
            writer_stats = multi_agent_team._admit_writer_stats(
                {
                    "proposed_count": len(proposed_replacements),
                    "accepted_count": 0,
                    "rejected_count": len(proposed_replacements),
                    "rejection_codes": dict(sorted(rejection_codes.items())),
                }
            )
            return fail("REJECTED:NO_SAFE_CHANGES", writer_stats)

    rejection_codes = Counter(pre_rejections)
    rejection_codes["SEMANTIC_SUPPORT"] += semantic_rejections
    rejection_codes.update(raw_writer_stats["rejection_codes"])
    if not rejection_codes.get("SEMANTIC_SUPPORT"):
        rejection_codes.pop("SEMANTIC_SUPPORT", None)
    accepted_count = raw_writer_stats["accepted_count"]
    writer_stats = multi_agent_team._admit_writer_stats(
        {
            "proposed_count": len(proposed_replacements),
            "accepted_count": accepted_count,
            "rejected_count": len(proposed_replacements) - accepted_count,
            "rejection_codes": dict(sorted(rejection_codes.items())),
        }
    )
    if writer_stats is None:
        return fail("FAILED:WRITER_SCHEMA")
    draft = compiled["draft"]
    draft_digest = multi_agent_team.canonical_digest(draft)
    if not record(
        "rewrite_draft_compiled",
        {"draft_digest": draft_digest, "writer_stats": writer_stats},
    ):
        return fail("FAILED:PREPUBLICATION", writer_stats)

    try:
        authorization = services.audit_draft(draft)
    except Exception:
        return fail("FAILED:DETERMINISTIC_AUDIT", writer_stats)
    structurally_valid, unanimously_passed = (
        multi_agent_team._validate_authorization_report(authorization, draft)
    )
    if not structurally_valid:
        return fail("FAILED:AUTHORIZATION_REPORT", writer_stats)
    if not record(
        "rewrite_authorized",
        {
            "authorization_digest": multi_agent_team.canonical_digest(
                authorization
            ),
            "authorization_codes": _authorization_summary(authorization),
            "draft_digest": draft_digest,
        },
    ):
        return fail("FAILED:PREPUBLICATION", writer_stats)
    if not unanimously_passed:
        _, terminal_code = multi_agent_team._authorization_findings(
            authorization, draft
        )
        terminal_code = terminal_code or (
            authorization["codes"][0]
            if authorization["codes"]
            else "DETERMINISTIC_AUDIT"
        )
        return fail(f"REJECTED:{terminal_code}", writer_stats)

    try:
        fresh_attestation = services.attest_source(master_resume)
    except Exception:
        return fail("FAILED:SOURCE_ATTESTATION", writer_stats)
    if not multi_agent_team._validate_source_attestation(
        fresh_attestation, master_resume
    ):
        return fail("FAILED:SOURCE_ATTESTATION", writer_stats)

    authorization_digest = multi_agent_team.canonical_digest(authorization)
    metadata = {
        "schema_version": WEB_REWRITE_PUBLICATION_VERSION,
        "run_id": run_id,
        "case_id": case_id,
        "draft_digest": draft_digest,
        "source_digest": fresh_attestation["source_digest"],
        "job_description_digest": multi_agent_team.canonical_digest(
            job_description
        ),
        "candidate_fit_report": candidate_fit_report,
        "candidate_fit_report_digest": candidate_fit_report_digest,
        "candidate_fit_judge_report": candidate_fit_judge_report,
        "candidate_fit_judge_report_digest": candidate_fit_judge_report_digest,
        "semantic_review": semantic_review,
        "semantic_review_digest": multi_agent_team.canonical_digest(
            semantic_review
        ),
        "authorization_report": authorization,
        "authorization_digest": authorization_digest,
        "vote_invocation_ids": [
            vote["invocation_id"] for vote in authorization["votes"]
        ],
    }
    if not record(
        "rewrite_pre_publish",
        {
            "draft_digest": draft_digest,
            "authorization_digest": authorization_digest,
        },
    ):
        return fail("FAILED:PREPUBLICATION", writer_stats)

    try:
        publication = services.publish(draft, metadata)
    except Exception:
        return fail("FAILED:PUBLICATION_ATOMICITY", writer_stats)
    if not _valid_publication_receipt(publication, draft):
        return fail("FAILED:PUBLICATION_RECEIPT", writer_stats)

    publication_id = publication["publication_id"]
    expected_readback = {
        "publication_id": publication_id,
        "draft": draft,
        "metadata": metadata,
    }
    try:
        readback = services.read_publication(publication_id)
    except Exception:
        return fail("FAILED:PUBLICATION_VERIFICATION", writer_stats)
    try:
        readback_matches = (
            type(readback) is dict
            and multi_agent_team._canonical_json(readback)
            == multi_agent_team._canonical_json(expected_readback)
        )
    except Exception:
        readback_matches = False
    if not readback_matches:
        return fail("FAILED:PUBLICATION_VERIFICATION", writer_stats)

    authorization_receipt = {
        "schema_version": WEB_REWRITE_RECEIPT_VERSION,
        "run_id": run_id,
        "case_id": case_id,
        "draft_digest": draft_digest,
        "source_digest": metadata["source_digest"],
        "job_description_digest": metadata["job_description_digest"],
        "candidate_fit_report": candidate_fit_report,
        "candidate_fit_report_digest": candidate_fit_report_digest,
        "candidate_fit_judge_report": candidate_fit_judge_report,
        "candidate_fit_judge_report_digest": candidate_fit_judge_report_digest,
        "semantic_review": semantic_review,
        "semantic_review_digest": metadata["semantic_review_digest"],
        "authorization_report": authorization,
        "authorization_digest": authorization_digest,
        "vote_invocation_ids": metadata["vote_invocation_ids"],
        "publication_id": publication_id,
        "verified_target_digest": draft_digest,
    }
    return _outcome(
        run_id,
        case_id,
        "PUBLISHED",
        published=True,
        final_draft=draft,
        authorization_receipt=authorization_receipt,
        candidate_fit_report=candidate_fit_report,
        candidate_fit_report_digest=candidate_fit_report_digest,
        candidate_fit_judge_report=candidate_fit_judge_report,
        candidate_fit_judge_report_digest=candidate_fit_judge_report_digest,
        writer_stats=writer_stats,
    )


__all__ = [
    "WEB_REWRITE_PUBLICATION_VERSION",
    "WEB_REWRITE_RECEIPT_VERSION",
    "WEB_REWRITE_RESULT_VERSION",
    "SEMANTIC_REVIEW_VERSION",
    "derive_requirement_rubric",
    "run_web_rewrite",
]
