"""Direct, fail-closed pipeline for synchronous web resume rewrites.

The web route has one untrusted language boundary: ``AnthropicHost.run_role``
for Writer replacements.  Requirement selection, replacement compilation,
candidate-fit policy, the three authoritative audits, publication, and durable
readback are ordinary deterministic service calls.  Native and asynchronous
Resume Team execution continue to use ``multi_agent_team.run_team``.
"""

from __future__ import annotations

import re
from collections import Counter
from typing import Any

import multi_agent_team
from agent.host_anthropic import BudgetExceeded, HostRefusal
from claim_provenance_audit import claim_supported_without_semantic_review

WEB_REWRITE_RESULT_VERSION = "web-rewrite-result/v1"
WEB_REWRITE_PUBLICATION_VERSION = "web-rewrite-publication/v1"
WEB_REWRITE_RECEIPT_VERSION = "web-rewrite-final-receipt/v1"

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


def _filter_mechanically_supported_replacements(
    replacements: Any,
) -> tuple[Any, int]:
    """Remove wording changes that would require a semantic reviewer.

    Malformed items stay in the list so the shared salvage compiler owns their
    existing diagnostics. Only well-shaped, non-empty changes that exceed the
    no-semantic-review closure are removed here and counted as strict compiler
    rejections.
    """

    if not isinstance(replacements, list):
        return replacements, 0
    admitted: list[Any] = []
    rejected = 0
    for item in replacements:
        if (
            isinstance(item, dict)
            and set(item) == {"source_span_text", "replacement_text"}
            and isinstance(item.get("source_span_text"), str)
            and isinstance(item.get("replacement_text"), str)
            and item["replacement_text"]
            and not claim_supported_without_semantic_review(
                item["replacement_text"], item["source_span_text"]
            )
        ):
            rejected += 1
            continue
        admitted.append(item)
    return admitted, rejected


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
    ) or not callable(getattr(host, "run_role", None)):
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
        and score >= 70
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
        raw_writer = host.run_role(
            "writer",
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
    filtered_replacements, semantic_rejections = (
        _filter_mechanically_supported_replacements(proposed_replacements)
    )
    if isinstance(proposed_replacements, list) and proposed_replacements and not filtered_replacements:
        writer_stats = multi_agent_team._admit_writer_stats(
            {
                "proposed_count": len(proposed_replacements),
                "accepted_count": 0,
                "rejected_count": len(proposed_replacements),
                "rejection_codes": {
                    "STRICT_COMPILER": len(proposed_replacements)
                },
            }
        )
        return fail("REJECTED:NO_SAFE_CHANGES", writer_stats)
    try:
        compiled, raw_writer_stats = (
            multi_agent_team.compile_writer_replacements_salvage(
                master_resume, filtered_replacements
            )
        )
    except multi_agent_team.NoSafeWriterChanges as error:
        raw_stats = dict(error.stats)
        raw_codes = dict(raw_stats["rejection_codes"])
        raw_codes["STRICT_COMPILER"] = (
            raw_codes.get("STRICT_COMPILER", 0) + semantic_rejections
        )
        raw_stats.update(
            {
                "proposed_count": len(proposed_replacements),
                "rejected_count": len(proposed_replacements),
                "rejection_codes": raw_codes,
            }
        )
        writer_stats = multi_agent_team._admit_writer_stats(raw_stats)
        return fail("REJECTED:NO_SAFE_CHANGES", writer_stats)
    except ValueError:
        return fail("FAILED:WRITER_SCHEMA")
    except Exception:
        return fail("FAILED:AGENT_CRASH")
    raw_codes = dict(raw_writer_stats["rejection_codes"])
    if semantic_rejections:
        raw_codes["STRICT_COMPILER"] = (
            raw_codes.get("STRICT_COMPILER", 0) + semantic_rejections
        )
    raw_writer_stats.update(
        {
            "proposed_count": len(proposed_replacements),
            "rejected_count": (
                len(proposed_replacements) - raw_writer_stats["accepted_count"]
            ),
            "rejection_codes": raw_codes,
        }
    )
    writer_stats = multi_agent_team._admit_writer_stats(raw_writer_stats)
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
    if readback != expected_readback:
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
    "derive_requirement_rubric",
    "run_web_rewrite",
]
