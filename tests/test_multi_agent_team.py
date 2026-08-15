"""Fast, repository-local tests for the native Resume Team controller."""

from __future__ import annotations

import ast
import json
from collections import Counter
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from candidate_fit_preflight import (
    CANDIDATE_FIT_THRESHOLD,
    assess_candidate_fit as _real_assess_candidate_fit,
)

from evidence_engine.testing import DeterministicJudge as _DeterministicJudge


def assess_candidate_fit(*args, **kwargs):
    """Run the fit gate offline with the package's deterministic judge.

    The gate calls a hosted model in production and fails closed without one.
    Tests inject the rule-based judge so pipeline wiring stays testable
    without an API key; production never falls back this way.
    """
    kwargs.setdefault("llm", _DeterministicJudge())
    return _real_assess_candidate_fit(*args, **kwargs)


try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10
    import tomli as tomllib

from multi_agent_team import (
    AUTHORIZATION_VERSION,
    CONTEXT_VERSION,
    FINAL_RECEIPT_VERSION,
    HANDOFF_VERSION,
    PROTOCOL_VERSION,
    PUBLICATION_VERIFICATION_VERSION,
    PUBLICATION_VERSION,
    RESULT_VERSION,
    RUN_CLAIM_VERSION,
    SOURCE_ATTESTATION_VERSION,
    VOTE_VERSION,
    AgentInvocationFailure,
    NoSafeWriterChanges,
    _native_agent_host,
    build_context,
    build_handoff,
    canonical_digest,
    compile_semantically_reviewed_writer_replacements_salvage,
    compile_writer_replacements_salvage,
    normalize_native_payload,
    run_team,
    validate_handoff,
)

ROOT = Path(__file__).resolve().parents[1]


PASSING_RESUME = """ALEX DOE, MD
PROFESSIONAL SUMMARY
Drug safety physician with eight years in pharmacovigilance.
CORE COMPETENCIES
• Pharmacovigilance | Drug Safety | ICSR Review | Signal Detection | Risk Management | DSUR | PBRER | CIOMS | ICH
PROFESSIONAL EXPERIENCE
DIRECTOR, DRUG SAFETY PHYSICIAN | Acme Biotech | Boston, MA
Jan 2018 - Present
• Led medical review of ICSRs, expectedness and causality assessments, and aggregate safety reports for oncology trials.
• Reviewed DSUR and PBRER reports and assessed safety signals under ICH and CIOMS guidance.
EDUCATION
Doctor of Medicine (M.D.)
Example Medical School
"""

PASSING_JD = """Director, Drug Safety Physician
About the job
Lead safety surveillance for oncology products.
Qualifications
5+ years' experience in Drug Safety/Pharmacovigilance in a biotech company.
Medical Degree (MD) with medical practice experience.
Strong knowledge of ICH and CIOMS guidelines.
Experience reviewing ICSRs and preparing DSUR and PBRER reports.
"""

LOW_FIT_JD = """Regional Retail Sales Manager
About the job
Lead consumer retail sales operations across regional stores. Build store plans, coach sales teams, oversee merchandising, and improve customer conversion through local promotions and account planning.
Qualifications
Experience leading retail sales teams, store merchandising, customer acquisition, regional account planning, consumer promotions, and sales forecasting. Clear communication and practical team leadership are important for this role.
"""

KNOCKOUT_JD = """Director, Drug Safety Physician
About the job
Lead safety surveillance for oncology products and provide medical oversight for aggregate reporting and signal management across a global biotech portfolio.
Qualifications
20+ years' experience in Drug Safety/Pharmacovigilance in a biotech company.
Medical Degree (MD) with medical practice experience.
Strong knowledge of ICH and CIOMS guidelines.
Experience reviewing ICSRs and preparing DSUR and PBRER reports.
"""


def eligible_draft(body: str) -> str:
    if "PROFESSIONAL EXPERIENCE" in body:
        return body
    return PASSING_RESUME.rstrip() + "\n" + body


def candidate_fit_report(
    master_resume,
    job_description,
    run_id,
    case_id,
    *,
    extraction_trustworthy=True,
):
    return assess_candidate_fit(
        master_resume,
        job_description,
        run_id=run_id,
        case_id=case_id,
        as_of_date="2026-07-19",
        extraction_trustworthy=extraction_trustworthy,
    )


def request(**updates):
    value = {
        "schema_version": PROTOCOL_VERSION,
        "run_id": "run-local-1",
        "case_id": "LOCAL-1",
        "job_description": PASSING_JD,
        "master_resume": eligible_draft(
            "Safe draft\nBAD draft\nSafe corrected draft\n"
            "Checked draft\nReviewed draft\nVOICE draft"
        ),
        "output_dir": "/synthetic/output",
        "max_editor_attempts": 2,
        "role_timeout_seconds": 30.0,
    }
    if "master_resume" in updates:
        updates["master_resume"] = eligible_draft(updates["master_resume"])
    value.update(updates)
    return value


def envelope(role, context, payload, *, agent_id=None):
    return {
        "schema_version": HANDOFF_VERSION,
        "run_id": context["run_id"],
        "case_id": context["case_id"],
        "role": role,
        "agent_id": agent_id or f"codex:test-{role}-{context['attempt']}",
        "attempt": context["attempt"],
        "parent_artifact_digest": context["parent_artifact_digest"],
        "artifact_digest": canonical_digest(payload),
        "status": "complete",
        "payload": payload,
    }


class ScriptedAdapter:
    def __init__(
        self,
        *,
        draft="Safe draft",
        edited="Safe corrected draft",
        shared_id=False,
        reuse_auditor_id=False,
        writer_evidence=None,
        editor_evidence=None,
    ):
        self.draft = draft
        self.edited = edited
        self.shared_id = shared_id
        self.reuse_auditor_id = reuse_auditor_id
        self.writer_evidence = [] if writer_evidence is None else writer_evidence
        self.editor_evidence = [] if editor_evidence is None else editor_evidence
        self.calls = []
        self.counts = Counter()

    def invoke(self, role, context, timeout_seconds):
        assert set(context) == {
            "schema_version",
            "run_id",
            "case_id",
            "role",
            "attempt",
            "parent_artifact_digest",
            "payload",
        }
        assert context["schema_version"] == CONTEXT_VERSION
        assert context["parent_artifact_digest"] == canonical_digest(context["payload"])
        assert timeout_seconds == 30.0
        self.calls.append(role)
        self.counts[role] += 1
        if role == "researcher":
            source = context["payload"]["job_description"]
            requirement = next(line for line in source.splitlines() if line.strip())
            start = source.index(requirement)
            payload = {
                "rubric": {
                    "hard_requirements": [requirement],
                    "soft_requirements": [],
                },
                "jd_evidence_spans": [
                    {
                        "start": start,
                        "end": start + len(requirement),
                        "digest": canonical_digest(requirement),
                    },
                ],
            }
        elif role == "writer":
            payload = {
                "draft": eligible_draft(self.draft),
                "claim_evidence": self.writer_evidence,
            }
        elif role == "auditor":
            audited = context["payload"]["writer_draft"]
            failed = "BAD" in audited or "Checked draft" in audited
            evidence_line = next(
                (
                    line.strip()
                    for line in audited.splitlines()
                    if "BAD" in line or "Checked draft" in line
                ),
                audited,
            )
            payload = {
                "verdict": "FAIL" if failed else "PASS",
                "findings": (
                    [
                        {
                            "id": "F-VOICE",
                            "code": "HUMAN_VOICE",
                            "evidence_digest": canonical_digest(evidence_line),
                        }
                    ]
                    if failed
                    else []
                ),
                "draft_digest": canonical_digest(audited),
            }
        else:
            finding_ids = [
                finding["id"]
                for finding in context["payload"]["audit_findings"]
            ]
            payload = {
                "draft": eligible_draft(self.edited),
                "addressed_finding_ids": finding_ids,
                "claim_evidence": self.editor_evidence,
            }
        if self.shared_id and role in {"writer", "auditor"}:
            agent_id = "codex:shared-writer-auditor"
        elif self.reuse_auditor_id and role == "auditor":
            agent_id = "codex:reused-auditor"
        else:
            agent_id = f"codex:test-{role}-{self.counts[role]}"
        return envelope(role, context, payload, agent_id=agent_id)


class Services:
    def __init__(
        self,
        *,
        fail_publish=False,
        publication_receipt="valid",
        publication_verification="valid",
        opaque_report=False,
        stale_vote_ids=False,
        deterministic_fail_token="BAD",
        deterministic_actionable=True,
        stale_source_before_publish=False,
        fit_exception=False,
        fit_mutator=None,
    ):
        self.events = []
        self.audits = []
        self.published = []
        self.fail_publish = fail_publish
        self.publication_receipt = publication_receipt
        self.publication_verification = publication_verification
        self.opaque_report = opaque_report
        self.stale_vote_ids = stale_vote_ids
        self.deterministic_fail_token = deterministic_fail_token
        self.deterministic_actionable = deterministic_actionable
        self.stale_source_before_publish = stale_source_before_publish
        self.source_attestations = 0
        self.fit_exception = fit_exception
        self.fit_mutator = fit_mutator
        self.fit_calls = 0
        self.publication_digests = {}
        self.publication_metadata = {}

    def claim_run(self, run_id, case_id):
        return {
            "schema_version": RUN_CLAIM_VERSION,
            "run_id": run_id,
            "case_id": case_id,
            "claimed": True,
            "claim_id": f"claim-{run_id}-{case_id}",
        }

    def attest_source(self, master_resume):
        self.source_attestations += 1
        return {
            "schema_version": SOURCE_ATTESTATION_VERSION,
            "trusted": not (
                self.stale_source_before_publish and self.source_attestations > 1
            ),
            "source_id": "fixture-master",
            "source_digest": canonical_digest(master_resume),
        }

    def assess_candidate_fit(
        self, master_resume, job_description, run_id, case_id
    ):
        self.fit_calls += 1
        if self.fit_exception:
            raise RuntimeError("synthetic scorer failure")
        report = candidate_fit_report(
            master_resume,
            job_description,
            run_id,
            case_id,
        )
        if self.fit_mutator is not None:
            self.fit_mutator(report)
        return report

    def audit_draft(self, draft):
        if self.opaque_report:
            return {"passed": True}
        audit_number = len(self.audits) + 1
        vote_number = 1 if self.stale_vote_ids else audit_number
        failed = bool(
            self.deterministic_fail_token
            and self.deterministic_fail_token in draft
        )
        draft_digest = canonical_digest(draft)
        vote_rows = []
        for name in ("evidence", "human_voice", "canonical_integrity"):
            vote_failed = failed and name == "human_voice"
            vote_rows.append(
                {
                    "schema_version": VOTE_VERSION,
                    "name": name,
                    "invocation_id": f"vote-{vote_number}-{name}",
                    "passed": not vote_failed,
                    "draft_digest": draft_digest,
                    "codes": ["HUMAN_VOICE"] if vote_failed else [],
                }
            )
        report = {
            "schema_version": AUTHORIZATION_VERSION,
            "draft_digest": draft_digest,
            "passed": not failed,
            "codes": ["HUMAN_VOICE"] if failed else [],
            "votes": vote_rows,
            "findings": [],
        }
        if failed:
            locations = []
            if self.deterministic_actionable:
                matched = next(
                    (
                        (line_number, line)
                        for line_number, line in enumerate(
                            draft.splitlines(), start=1
                        )
                        if self.deterministic_fail_token in line
                    ),
                    None,
                )
                if matched is not None:
                    line_number, source_line = matched
                    locations = [
                        {
                            "line_number": line_number,
                            "source_line": source_line,
                            "line_digest": canonical_digest(source_line),
                        }
                    ]
            report["findings"] = [
                {
                    "id": f"human-voice-{audit_number}",
                    "vote_name": "human_voice",
                    "code": "HUMAN_VOICE",
                    "actionable": bool(locations),
                    "locations": locations,
                }
            ]
        self.audits.append(report)
        return report

    def record_event(self, event, payload):
        self.events.append(event)

    def publish(self, draft, metadata):
        if self.fail_publish:
            raise OSError("synthetic publication failure")
        self.published.append((draft, metadata))
        if self.publication_receipt == "none":
            return None
        draft_digest = canonical_digest(draft)
        publication_id = f"publication-{len(self.published)}"
        receipt = {
            "schema_version": PUBLICATION_VERSION,
            "committed": True,
            "draft_digest": draft_digest,
            "target_digest": draft_digest,
            "publication_id": publication_id,
        }
        if self.publication_receipt == "bad":
            receipt["target_digest"] = "f" * 64
        self.publication_digests[publication_id] = draft_digest
        self.publication_metadata[publication_id] = metadata
        return receipt

    def verify_publication(self, publication_id):
        digest = self.publication_digests.get(publication_id, "")
        metadata = self.publication_metadata.get(publication_id, {})
        authorization_receipt = {
            "schema_version": FINAL_RECEIPT_VERSION,
            "run_id": metadata.get("run_id", ""),
            "case_id": metadata.get("case_id", ""),
            "draft_digest": digest,
            "source_digest": metadata.get("source_digest", ""),
            "job_description_digest": metadata.get(
                "job_description_digest", ""
            ),
            "candidate_fit_report": metadata.get("candidate_fit_report"),
            "candidate_fit_report_digest": metadata.get(
                "candidate_fit_report_digest", ""
            ),
            "candidate_fit_judge_report": metadata.get(
                "candidate_fit_judge_report"
            ),
            "candidate_fit_judge_report_digest": metadata.get(
                "candidate_fit_judge_report_digest", ""
            ),
            "researcher_agent_id": metadata.get("researcher_agent_id", ""),
            "researcher_artifact_digest": metadata.get(
                "researcher_artifact_digest", ""
            ),
            "auditor_attestation": metadata.get("auditor_attestation", {}),
            "authorization_digest": metadata.get("authorization_digest", ""),
            "authorization_report": metadata.get("authorization_report", {}),
            "vote_invocation_ids": metadata.get("vote_invocation_ids", []),
            "publication_id": publication_id,
            "verified_target_digest": digest,
        }
        verification = {
            "schema_version": PUBLICATION_VERIFICATION_VERSION,
            "verified": True,
            "publication_id": publication_id,
            "target_digest": digest,
            "authorization_receipt": authorization_receipt,
            "authorization_receipt_digest": canonical_digest(authorization_receipt),
            "authorization_receipt_path": (
                f"resume-team-receipt.{canonical_digest(publication_id)}.json"
            ),
        }
        if self.publication_verification == "bad":
            verification["verified"] = False
        elif self.publication_verification == "bad_receipt":
            authorization_receipt["auditor_attestation"] = dict(
                authorization_receipt["auditor_attestation"],
                artifact_digest="f" * 64,
            )
            verification["authorization_receipt_digest"] = canonical_digest(
                authorization_receipt
            )
        elif self.publication_verification == "bad_receipt_digest":
            verification["authorization_receipt_digest"] = "f" * 64
        return verification


def test_build_context_and_strict_handoff_validation():
    payload = {"job_description": "Offline review"}
    context = build_context(
        run_id="run-1",
        case_id="case-1",
        role="researcher",
        attempt=0,
        payload=payload,
    )
    response_payload = {
        "rubric": {
            "hard_requirements": ["Offline review"],
            "soft_requirements": [],
        },
        "jd_evidence_spans": [
            {
                "start": 0,
                "end": len("Offline review"),
                "digest": canonical_digest("Offline review"),
            }
        ],
    }
    response = envelope("researcher", context, response_payload)
    assert validate_handoff("researcher", response, context) == {
        "valid": True,
        "code": "OK",
    }

    stale = dict(response)
    stale["run_id"] = "stale"
    assert validate_handoff("researcher", stale, context)["code"] == "HANDOFF_PROVENANCE"

    forged = dict(response)
    forged["artifact_digest"] = "f" * 64
    assert validate_handoff("researcher", forged, context)["code"] == "RESEARCH_SCHEMA"


def test_researcher_rubric_must_match_one_to_one_jd_evidence():
    context = build_context(
        run_id="run-research",
        case_id="case-research",
        role="researcher",
        attempt=0,
        payload={"job_description": "Requires offline review."},
    )
    with pytest.raises(ValueError, match="not evidence-bound"):
        normalize_native_payload(
            "researcher",
            {
                "rubric": {
                    "hard_requirements": ["Requires invented oncology work."],
                    "soft_requirements": [],
                },
                "jd_evidence_spans": [{"evidence_text": "Requires offline review."}],
            },
            context,
        )

    forged_payload = {
        "rubric": {
            "hard_requirements": ["Invented oncology work"],
            "soft_requirements": [],
        },
        "jd_evidence_spans": [
            {
                "start": 0,
                "end": len("Requires offline review."),
                "digest": canonical_digest("Requires offline review."),
            }
        ],
    }
    forged = envelope("researcher", context, forged_payload)
    assert validate_handoff("researcher", forged, context)["code"] == "RESEARCH_SCHEMA"

    empty_payload = {
        "rubric": {"hard_requirements": [], "soft_requirements": []},
        "jd_evidence_spans": [],
    }
    with pytest.raises(ValueError, match="not evidence-bound"):
        normalize_native_payload("researcher", empty_payload, context)
    empty = envelope("researcher", context, empty_payload)
    assert validate_handoff("researcher", empty, context)["code"] == "RESEARCH_SCHEMA"
    handoff_schema = json.loads(
        (ROOT / "schemas" / "resume-team-handoff.schema.json").read_text(
            encoding="utf-8"
        )
    )
    assert not Draft202012Validator(handoff_schema).is_valid(empty)

    punctuation_payload = {
        "rubric": {"hard_requirements": ["."], "soft_requirements": []},
        "jd_evidence_spans": [{"evidence_text": "."}],
    }
    punctuation_context = build_context(
        run_id="run-research-punctuation",
        case_id="case-research-punctuation",
        role="researcher",
        attempt=0,
        payload={"job_description": "Must lead clinical operations."},
    )
    with pytest.raises(ValueError, match="invalid researcher evidence"):
        normalize_native_payload(
            "researcher", punctuation_payload, punctuation_context
        )

    negated_context = build_context(
        run_id="run-research-negation",
        case_id="case-research-negation",
        role="researcher",
        attempt=0,
        payload={"job_description": "No experience with Python is required."},
    )
    sliced = {
        "rubric": {
            "hard_requirements": ["experience with Python"],
            "soft_requirements": [],
        },
        "jd_evidence_spans": [{"evidence_text": "experience with Python"}],
    }
    with pytest.raises(ValueError, match="complete JD line"):
        normalize_native_payload("researcher", sliced, negated_context)
    sliced_start = len("No ")
    sliced_payload = {
        "rubric": sliced["rubric"],
        "jd_evidence_spans": [
            {
                "start": sliced_start,
                "end": sliced_start + len("experience with Python"),
                "digest": canonical_digest("experience with Python"),
            }
        ],
    }
    sliced_handoff = envelope("researcher", negated_context, sliced_payload)
    assert validate_handoff(
        "researcher", sliced_handoff, negated_context
    )["code"] == "RESEARCH_SCHEMA"


def test_coordinator_normalizes_text_anchors_and_owns_hashes():
    context = build_context(
        run_id="run-native",
        case_id="case-native",
        role="writer",
        attempt=0,
        payload={
            "master_resume": "Reviewed 12 cases.",
            "researcher_artifact": {},
        },
    )
    normalized = normalize_native_payload(
        "writer",
        {
            "replacements": [
                {
                    "source_span_text": "Reviewed 12 cases.",
                    "replacement_text": "Checked 12 cases.",
                }
            ],
        },
        context,
    )
    handoff = build_handoff(
        context=context,
        role="writer",
        agent_id="native-writer-1",
        payload=normalized,
    )
    assert normalized["claim_evidence"] == [
        {
            "claim_digest": canonical_digest("Checked 12 cases."),
            "source_span_digest": canonical_digest("Reviewed 12 cases."),
            "source_start": 0,
            "source_end": 18,
        }
    ]
    assert validate_handoff("writer", handoff, context)["valid"]

    with pytest.raises(ValueError):
        normalize_native_payload(
            "writer",
            {
                "replacements": [
                    {
                        "source_span_text": "Reviewed 12 cases.",
                        "replacement_text": "Invented 99 cases.",
                    }
                ],
            },
            context,
        )


def writer_context(master: str):
    return build_context(
        run_id="run-writer-replacements",
        case_id="case-writer-replacements",
        role="writer",
        attempt=0,
        payload={"master_resume": master, "researcher_artifact": {}},
    )


SAFE_SOURCE_BULLET = (
    "• Reviewed DSUR and PBRER reports and assessed safety signals under ICH "
    "and CIOMS guidance."
)
OTHER_SOURCE_BULLET = (
    "• Reviewed  DSUR and PBRER reports and assessed safety signals under ICH "
    "and CIOMS guidance."
)
SAFE_SUPPORTED_REWRITE = (
    "• Reviewed DSUR and PBRER reports and assessed safety signals under ICH "
    "and CIOMS guidance for oncology programs."
)


def master_resume():
    return """PROFESSIONAL SUMMARY
Safety scientist with pharmacovigilance experience.
CORE COMPETENCIES
• Drug Safety | Signal Detection
PROFESSIONAL EXPERIENCE
Senior Safety Scientist | Acme
Jan 2020 - Present
• Reviewed DSUR and PBRER reports and assessed safety signals under ICH and CIOMS guidance.
• Reviewed  DSUR and PBRER reports and assessed safety signals under ICH and CIOMS guidance.
Safety Scientist | Other
Jan 2015 - Dec 2019
• Reviewed safety case reports under ICH guidance.
EDUCATION
Doctor of Medicine (M.D.)
Example Medical School
"""


def test_salvage_keeps_safe_replacement_when_sibling_breaks_ownership():
    safe = {
        "source_span_text": SAFE_SOURCE_BULLET,
        "replacement_text": SAFE_SUPPORTED_REWRITE,
    }
    unsafe = {
        "source_span_text": OTHER_SOURCE_BULLET,
        "replacement_text": SAFE_SUPPORTED_REWRITE,
    }

    payload, stats = compile_writer_replacements_salvage(
        master_resume(), [unsafe, safe]
    )

    assert SAFE_SUPPORTED_REWRITE in payload["draft"]
    assert OTHER_SOURCE_BULLET in payload["draft"]
    assert stats == {
        "proposed_count": 2,
        "accepted_count": 1,
        "rejected_count": 1,
        "rejection_codes": {"STRICT_COMPILER": 1},
    }



def test_semantically_reviewed_salvage_allows_truthful_reordering():
    paraphrase = {
        "source_span_text": SAFE_SOURCE_BULLET,
        "replacement_text": (
            "• Assessed safety signals and reviewed DSUR and PBRER reports "
            "under CIOMS and ICH guidance."
        ),
    }

    with pytest.raises(NoSafeWriterChanges):
        compile_writer_replacements_salvage(master_resume(), [paraphrase])

    payload, stats = compile_semantically_reviewed_writer_replacements_salvage(
        master_resume(), [paraphrase]
    )

    assert paraphrase["replacement_text"] in payload["draft"]
    assert stats["accepted_count"] == 1


def test_semantically_reviewed_salvage_still_blocks_canonical_fact_changes():
    with pytest.raises(NoSafeWriterChanges) as caught:
        compile_semantically_reviewed_writer_replacements_salvage(
            master_resume(),
            [
                {
                    "source_span_text": "Senior Safety Scientist | Acme",
                    "replacement_text": "Director of Safety | Acme",
                }
            ],
        )

    assert caught.value.stats["rejection_codes"] == {"CANONICAL_INTEGRITY": 1}

def test_salvage_rejects_protected_fact_change_before_publication():
    with pytest.raises(NoSafeWriterChanges) as caught:
        compile_writer_replacements_salvage(
            master_resume(),
            [
                {
                    "source_span_text": "Senior Safety Scientist | Acme",
                    "replacement_text": "Director of Safety | Acme",
                }
            ],
        )

    assert caught.value.stats["rejection_codes"] == {"CANONICAL_INTEGRITY": 1}


def test_salvage_distinguishes_explicit_empty_list_from_all_rejected():
    payload, stats = compile_writer_replacements_salvage(master_resume(), [])

    assert payload == {"draft": master_resume(), "claim_evidence": []}
    assert stats == {
        "proposed_count": 0,
        "accepted_count": 0,
        "rejected_count": 0,
        "rejection_codes": {},
    }

    with pytest.raises(NoSafeWriterChanges) as caught:
        compile_writer_replacements_salvage(master_resume(), [{"bad": "shape"}])

    assert caught.value.stats == {
        "proposed_count": 1,
        "accepted_count": 0,
        "rejected_count": 1,
        "rejection_codes": {"INVALID_ITEM": 1},
    }


def test_salvage_applies_independent_replacements_in_source_order():
    first = {
        "source_span_text": SAFE_SOURCE_BULLET,
        "replacement_text": SAFE_SUPPORTED_REWRITE,
    }
    second = {
        "source_span_text": "• Reviewed safety case reports under ICH guidance.",
        "replacement_text": (
            "• Reviewed safety case reports under ICH guidance for global teams."
        ),
    }

    first_payload, first_stats = compile_writer_replacements_salvage(
        master_resume(), [first, second]
    )
    second_payload, second_stats = compile_writer_replacements_salvage(
        master_resume(), [second, first]
    )

    assert first_payload == second_payload
    assert first_stats == second_stats == {
        "proposed_count": 2,
        "accepted_count": 2,
        "rejected_count": 0,
        "rejection_codes": {},
    }


def test_writer_replacement_is_applied_in_place_without_move_or_duplicate():
    master = """PROFESSIONAL SUMMARY
Reviewed confidential records.

PROFESSIONAL EXPERIENCE
Analyst | Acme
2020 - 2021
• Managed routine work.
"""
    source = "Reviewed confidential records."
    replacement = "Reviewed confidential safety records."
    context = build_context(
        run_id="run-source-anchored",
        case_id="case-source-anchored",
        role="writer",
        attempt=0,
        payload={"master_resume": master, "researcher_artifact": {}},
    )

    normalized = normalize_native_payload(
        "writer",
        {
            "replacements": [
                {
                    "source_span_text": source,
                    "replacement_text": replacement,
                }
            ]
        },
        context,
    )

    expected = master.replace(source, replacement)
    start = master.index(source)
    assert normalized["draft"] == expected
    assert normalized["draft"].count(replacement) == 1
    assert normalized["draft"].index(replacement) < normalized["draft"].index(
        "PROFESSIONAL EXPERIENCE"
    )
    assert normalized["claim_evidence"] == [
        {
            "claim_digest": canonical_digest(replacement),
            "source_span_digest": canonical_digest(source),
            "source_start": start,
            "source_end": start + len(source),
        }
    ]


@pytest.mark.parametrize(
    "master",
    [
        pytest.param("Reviewed.\n", id="safe-master"),
        pytest.param("Reviewed.\u2028", id="unsafe-format-master"),
        pytest.param(
            """PROFESSIONAL EXPERIENCE
An intentionally unstructured role without canonical dates.

EDUCATION
Example University
""",
            id="unrecognized-experience-layout",
        ),
    ],
)
def test_writer_empty_replacements_pass_master_through_byte_identically(master):
    normalized = normalize_native_payload(
        "writer", {"replacements": []}, writer_context(master)
    )
    assert normalized == {"draft": master, "claim_evidence": []}


def test_writer_deletion_blanks_only_the_source_body_and_preserves_lf():
    master = """SUMMARY
Optional summary.
PROFESSIONAL EXPERIENCE
Analyst | Acme
2020 - 2021
• Reviewed records.
EDUCATION
Example University
"""
    normalized = normalize_native_payload(
        "writer",
        {
            "replacements": [
                {
                    "source_span_text": "Optional summary.",
                    "replacement_text": "",
                }
            ]
        },
        writer_context(master),
    )
    assert normalized == {
        "draft": """SUMMARY

PROFESSIONAL EXPERIENCE
Analyst | Acme
2020 - 2021
• Reviewed records.
EDUCATION
Example University
""",
        "claim_evidence": [],
    }


@pytest.mark.parametrize(
    "proposal_order",
    [
        pytest.param(("Checked.", "Reviewed."), id="later-anchor-first"),
        pytest.param(("Reviewed.", "Checked."), id="earlier-anchor-first"),
    ],
)
def test_writer_resolves_every_anchor_before_splicing(proposal_order):
    master = "Reviewed.\nChecked.\n"
    replacements = {
        "Checked.": "Checked records.",
        "Reviewed.": "Reviewed Checked.",
    }
    normalized = normalize_native_payload(
        "writer",
        {
            "replacements": [
                {
                    "source_span_text": source,
                    "replacement_text": replacements[source],
                }
                for source in proposal_order
            ]
        },
        writer_context(master),
    )
    assert normalized["draft"] == "Reviewed Checked.\nChecked records.\n"
    assert normalized["claim_evidence"] == [
        {
            "claim_digest": canonical_digest("Reviewed Checked."),
            "source_span_digest": canonical_digest("Reviewed."),
            "source_start": 0,
            "source_end": 9,
        },
        {
            "claim_digest": canonical_digest("Checked records."),
            "source_span_digest": canonical_digest("Checked."),
            "source_start": 10,
            "source_end": 18,
        },
    ]


def test_writer_cannot_delete_the_only_core_competencies_line():
    master = """CORE COMPETENCIES
• Drug Safety | Signal Detection
PROFESSIONAL EXPERIENCE
Analyst | Acme
2020 - 2021
• Reviewed safety records.
EDUCATION
Example University
"""

    with pytest.raises(ValueError, match="Core Competencies"):
        normalize_native_payload(
            "writer",
            {
                "replacements": [
                    {
                        "source_span_text": "• Drug Safety | Signal Detection",
                        "replacement_text": "",
                    }
                ]
            },
            writer_context(master),
        )


def test_writer_nonempty_replacement_rejects_crlf_master():
    master = "Reviewed 12 cases.\r\n"

    with pytest.raises(ValueError, match="unsafe draft text format"):
        normalize_native_payload(
            "writer",
            {
                "replacements": [
                    {
                        "source_span_text": "Reviewed 12 cases.",
                        "replacement_text": "Checked 12 cases.",
                    }
                ]
            },
            writer_context(master),
        )


def test_writer_empty_replacements_preserve_crlf_but_handoff_fails_closed():
    master = "Reviewed 12 cases.\r\n"
    context = writer_context(master)
    normalized = normalize_native_payload(
        "writer", {"replacements": []}, context
    )

    assert normalized == {"draft": master, "claim_evidence": []}
    handoff = build_handoff(
        context=context,
        role="writer",
        agent_id="native-writer-crlf",
        payload=normalized,
    )
    validation = validate_handoff("writer", handoff, context)
    assert not validation["valid"]
    assert validation["code"] == "WRITER_SCHEMA"


@pytest.mark.parametrize(
    "raw_payload",
    [
        {},
        {"replacements": None},
        {"replacements": {}},
        {"replacements": [None]},
        {"replacements": [], "draft": "legacy"},
        {"draft": "legacy", "claim_evidence": []},
        {"replacements": [{"source_span_text": "Reviewed."}]},
        {
            "replacements": [
                {
                    "source_span_text": "Reviewed.",
                    "replacement_text": "Reviewed.",
                    "extra": True,
                }
            ]
        },
        {
            "replacements": [
                {"source_span_text": 7, "replacement_text": "Reviewed safely."}
            ]
        },
        {
            "replacements": [
                {"source_span_text": "Reviewed.", "replacement_text": 7}
            ]
        },
        {
            "replacements": [
                {"source_span_text": "Reviewed.", "replacement_text": "   "}
            ]
        },
        {
            "replacements": [
                {
                    "source_span_text": "Reviewed.",
                    "replacement_text": "Reviewed.\nAdded.",
                }
            ]
        },
        {
            "replacements": [
                {
                    "source_span_text": "Reviewed.",
                    "replacement_text": "Reviewed.\u2028Added.",
                }
            ]
        },
        {
            "replacements": [
                {
                    "source_span_text": "Reviewed.",
                    "replacement_text": "Reviewed.",
                }
            ]
        },
    ],
)
def test_writer_rejects_malformed_replacement_payloads(raw_payload):
    with pytest.raises(ValueError):
        normalize_native_payload(
            "writer", raw_payload, writer_context("Reviewed.\n")
        )


@pytest.mark.parametrize(
    ("master", "source_span_text", "expected_error"),
    [
        ("Reviewed records.\n", "Reviewed", "exact complete line"),
        ("  Reviewed.\n", "Reviewed.", "exact complete line"),
        (
            "Reviewed.\nReviewed.\n",
            "Reviewed.",
            "one unique source span",
        ),
        ("---\nReviewed.\n", "---", "exact complete line"),
    ],
)
def test_writer_rejects_non_exact_or_ambiguous_source_anchors(
    master,
    source_span_text,
    expected_error,
):
    with pytest.raises(ValueError, match=expected_error):
        normalize_native_payload(
            "writer",
            {
                "replacements": [
                    {
                        "source_span_text": source_span_text,
                        "replacement_text": "Reviewed safely.",
                    }
                ]
            },
            writer_context(master),
        )


def test_writer_rejects_duplicate_source_anchors():
    with pytest.raises(ValueError, match="reuses one source line"):
        normalize_native_payload(
            "writer",
            {
                "replacements": [
                    {
                        "source_span_text": "Reviewed.",
                        "replacement_text": "Reviewed records.",
                    },
                    {
                        "source_span_text": "Reviewed.",
                        "replacement_text": "Reviewed safely.",
                    },
                ]
            },
            writer_context("Reviewed.\n"),
        )


def test_writer_rejects_unsupported_metric_replacement():
    with pytest.raises(ValueError, match="UNSUPPORTED_METRIC"):
        normalize_native_payload(
            "writer",
            {
                "replacements": [
                    {
                        "source_span_text": "Reviewed 12 cases.",
                        "replacement_text": "Reviewed 99 cases.",
                    }
                ]
            },
            writer_context("Reviewed 12 cases.\n"),
        )


def test_writer_rejects_non_additive_rewrite():
    with pytest.raises(ValueError, match="UNSUPPORTED_CLAIM"):
        normalize_native_payload(
            "writer",
            {
                "replacements": [
                    {
                        "source_span_text": "Reviewed confidential records.",
                        "replacement_text": "Reviewed records.",
                    }
                ]
            },
            writer_context("Reviewed confidential records.\n"),
        )


@pytest.mark.parametrize(
    ("draft", "evidence_text"),
    [
        ("Present draft line.", "Master-only omitted line."),
        (
            "Repeated draft line.\nRepeated draft line.",
            "Repeated draft line.",
        ),
    ],
)
def test_native_auditor_finding_must_bind_one_unique_draft_line(
    draft,
    evidence_text,
):
    context = build_context(
        run_id="run-auditor-draft-binding",
        case_id="case-auditor-draft-binding",
        role="auditor",
        attempt=0,
        payload={
            "master_resume": "Master-only omitted line.",
            "researcher_artifact": {},
            "writer_draft": draft,
        },
    )

    with pytest.raises(ValueError, match="auditor (finding|evidence)"):
        normalize_native_payload(
            "auditor",
            {
                "verdict": "FAIL",
                "findings": [
                    {
                        "id": "A-1",
                        "code": "AUDIT_FINDING",
                        "evidence_text": evidence_text,
                    }
                ],
                "audited_draft": draft,
            },
            context,
        )


def test_writer_allows_byte_identical_tabs_in_unchanged_trusted_source_lines():
    master = """PROFESSIONAL SUMMARY
Wrote SOPs.
CONTACT
Boston	private@example.com
CORE COMPETENCIES
• SOP Development
"""
    context = build_context(
        run_id="run-trusted-tab",
        case_id="case-trusted-tab",
        role="writer",
        attempt=0,
        payload={"master_resume": master, "researcher_artifact": {}},
    )

    normalized = normalize_native_payload(
        "writer",
        {
            "replacements": [
                {
                    "source_span_text": "Wrote SOPs.",
                    "replacement_text": "Authored SOPs.",
                }
            ]
        },
        context,
    )

    assert "Boston	private@example.com" in normalized["draft"]
    assert "Authored SOPs." in normalized["draft"]


def test_writer_handoff_cannot_move_a_trusted_tab_line():
    master = """PROFESSIONAL SUMMARY
Wrote SOPs.
Boston	private@example.com
CORE COMPETENCIES
• SOP Development
"""
    moved = """PROFESSIONAL SUMMARY
Boston	private@example.com
Wrote SOPs.
CORE COMPETENCIES
• SOP Development
"""
    context = build_context(
        run_id="run-moved-tab",
        case_id="case-moved-tab",
        role="writer",
        attempt=0,
        payload={"master_resume": master, "researcher_artifact": {}},
    )
    envelope = build_handoff(
        context=context,
        role="writer",
        agent_id="api:writer.run-moved-tab.0",
        payload={"draft": moved, "claim_evidence": []},
    )

    assert validate_handoff("writer", envelope, context) == {
        "valid": False,
        "code": "WRITER_SCHEMA",
    }


@pytest.mark.parametrize(
    "unsafe_separator",
    [
        "\r\n",
        "\r",
        "\v",
        "\f",
        "\x00",
        "\x1c",
        "\x1d",
        "\x1e",
        "\x7f",
        "\x85",
        "\u2028",
        "\u2029",
        "\t",
        "\u200b",
        "\u2060",
    ],
)
def test_writer_rejects_non_lf_control_and_format_separators(
    unsafe_separator,
):
    master = "Safe draft\nReviewed draft"
    context = build_context(
        run_id="run-unsafe-lines",
        case_id="case-unsafe-lines",
        role="writer",
        attempt=0,
        payload={"master_resume": master, "researcher_artifact": {}},
    )

    with pytest.raises(ValueError, match="one safe line"):
        normalize_native_payload(
            "writer",
            {
                "replacements": [
                    {
                        "source_span_text": "Safe draft",
                        "replacement_text": f"Safe{unsafe_separator}draft",
                    }
                ]
            },
            context,
        )


def test_writer_handoff_rejects_all_lf_replaced_with_unicode_line_separator():
    master = "Safe draft\nReviewed draft"
    adapter = ScriptedAdapter(draft=master.replace("\n", "\u2028"))
    services = Services(deterministic_fail_token="")

    result = run_team(request(master_resume=master), adapter, services)

    assert result["terminal_class"] == "FAILED:WRITER_SCHEMA"
    assert adapter.calls == ["researcher", "writer"]
    assert not services.audits
    assert not services.published


@pytest.mark.parametrize(
    "unsafe_separator",
    ["\r\n", "\x1e", "\x85", "\u2028", "\u2029", "\u2060"],
)
def test_editor_rejects_unsafe_separator_before_edit_scope_check(
    unsafe_separator,
):
    adapter = ScriptedAdapter(
        draft="Checked draft\nSafe draft",
        edited=f"Reviewed draft{unsafe_separator}Safe draft",
    )
    services = Services(deterministic_fail_token="")

    result = run_team(request(), adapter, services)

    assert result["terminal_class"] == "FAILED:EDITOR_SCHEMA"
    assert adapter.calls == ["researcher", "writer", "auditor", "editor"]
    assert len(services.audits) == 1
    assert not services.published


def test_editor_cannot_change_terminal_lf_outside_authorized_line_edit():
    adapter = ScriptedAdapter(
        draft="Checked draft\nSafe draft\n",
        edited="Reviewed draft\nSafe draft",
    )
    services = Services(deterministic_fail_token="")

    result = run_team(request(), adapter, services)

    assert result["terminal_class"] == "REJECTED:EDITOR_OVERREACH"
    assert adapter.calls == ["researcher", "writer", "auditor", "editor"]
    assert not services.published


def test_clean_run_skips_editor_and_publishes_once():
    adapter = ScriptedAdapter()
    services = Services()
    result = run_team(request(), adapter, services)
    assert result["terminal_class"] == "PUBLISHED"
    assert result["published"] is result["success_reported"] is True
    assert result["schema_version"] == RESULT_VERSION
    assert adapter.calls == ["researcher", "writer", "auditor"]
    assert len(services.published) == 1
    assert services.fit_calls == 1
    assert result["final_draft_digest"] == canonical_digest(
        eligible_draft("Safe draft")
    )
    receipt = result["authorization_receipt"]
    assert receipt["schema_version"] == FINAL_RECEIPT_VERSION
    assert receipt["draft_digest"] == result["final_draft_digest"]
    assert receipt["source_digest"] == canonical_digest(request()["master_resume"])
    assert receipt["job_description_digest"] == canonical_digest(
        request()["job_description"]
    )
    assert receipt["candidate_fit_report"] == result["candidate_fit_report"]
    assert receipt["candidate_fit_report_digest"] == canonical_digest(
        receipt["candidate_fit_report"]
    )
    assert result["candidate_fit_report_digest"] == receipt[
        "candidate_fit_report_digest"
    ]
    assert receipt["researcher_agent_id"] == "codex:test-researcher-1"
    assert receipt["researcher_artifact_digest"]
    assert receipt["auditor_attestation"]["verdict"] == "PASS"
    assert (
        receipt["auditor_attestation"]["draft_digest"]
        == result["final_draft_digest"]
    )
    assert receipt["authorization_report"]["passed"] is True
    assert receipt["authorization_report"]["codes"] == []
    votes = receipt["authorization_report"]["votes"]
    assert [vote["name"] for vote in votes] == [
        "evidence",
        "human_voice",
        "canonical_integrity",
    ]
    assert all(
        vote["passed"] is True
        and vote["codes"] == []
        and vote["draft_digest"] == result["final_draft_digest"]
        for vote in votes
    )
    assert receipt["authorization_digest"] == canonical_digest(
        receipt["authorization_report"]
    )
    assert receipt["vote_invocation_ids"] == [
        vote["invocation_id"] for vote in votes
    ]
    assert len(receipt["vote_invocation_ids"]) == 3
    result_schema = json.loads(
        (ROOT / "schemas" / "resume-team-result.schema.json").read_text(
            encoding="utf-8"
        )
    )
    Draft202012Validator(result_schema).validate(result)
    assert result["authorization_receipt_digest"] == canonical_digest(receipt)
    assert result["authorization_receipt_path"]


def test_candidate_fit_score_below_fixed_threshold_rejects_before_roles():
    adapter = ScriptedAdapter()
    services = Services()

    result = run_team(request(job_description=LOW_FIT_JD), adapter, services)

    assert result["terminal_class"] == "REJECTED:CANDIDATE_FIT"
    assert result["published"] is False
    assert adapter.calls == []
    assert services.fit_calls == 1
    assert services.audits == []
    assert services.published == []
    assert result["candidate_fit_report"]["score"] < CANDIDATE_FIT_THRESHOLD
    assert result["candidate_fit_report"]["hard_knockouts"] == []
    assert result["candidate_fit_report"]["codes"] == [
        "SCORE_BELOW_THRESHOLD"
    ]
    assert result["candidate_fit_report_digest"] == canonical_digest(
        result["candidate_fit_report"]
    )
    result_schema = json.loads(
        (ROOT / "schemas" / "resume-team-result.schema.json").read_text(
            encoding="utf-8"
        )
    )
    Draft202012Validator(result_schema).validate(result)


def test_candidate_fit_high_score_with_hard_knockout_rejects_before_roles():
    adapter = ScriptedAdapter()
    services = Services()

    result = run_team(request(job_description=KNOCKOUT_JD), adapter, services)

    assert result["terminal_class"] == "REJECTED:CANDIDATE_FIT"
    assert adapter.calls == []
    assert services.audits == []
    report = result["candidate_fit_report"]
    # A stated minimum the candidate's whole career cannot reach is
    # contradicted by the resume, so it fails eligibility outright. The
    # evidence score itself is left alone: the spec keeps eligibility and
    # qualification evidence separate rather than multiplying them.
    assert report["eligibility"] == "FAIL"
    assert report["codes"] == ["ELIGIBILITY_FAILED"]
    knockouts = report["hard_knockouts"]
    assert len(knockouts) == 1
    assert knockouts[0]["code"] == "MINIMUM_EXPERIENCE_NOT_MET"
    # The refusal cites the computed duration, not a bare verdict.
    assert "years of total career experience" in knockouts[0]["candidate_has"]


def test_kernel_recomputes_structurally_consistent_forged_candidate_fit_pass():
    def forge_rejection_as_pass(report):
        assert report["passed"] is False
        assert report["hard_knockouts"]
        report["score"] = 100.0
        report["component_scores"] = {
            key: 100.0 for key in report["component_scores"]
        }
        report["hard_knockouts"] = []
        report["extraction_trustworthy"] = True
        report["passed"] = True
        report["recommendation"] = "PROCEED"
        report["codes"] = []

    adapter = ScriptedAdapter()
    services = Services(fit_mutator=forge_rejection_as_pass)

    result = run_team(
        request(job_description=KNOCKOUT_JD), adapter, services
    )

    assert result["terminal_class"] == "FAILED:CANDIDATE_FIT_PREFLIGHT"
    assert adapter.calls == []
    assert services.audits == []
    assert services.published == []


def test_candidate_fit_assessor_exception_fails_before_roles():
    adapter = ScriptedAdapter()
    services = Services(fit_exception=True)

    result = run_team(request(), adapter, services)

    assert result["terminal_class"] == "FAILED:CANDIDATE_FIT_PREFLIGHT"
    assert adapter.calls == []
    assert services.audits == []
    assert result["candidate_fit_report"] is None
    assert result["candidate_fit_report_digest"] == ""


@pytest.mark.parametrize(
    "mutator",
    [
        lambda report: report.update(extra=True),
        lambda report: report.__setitem__("score", float("nan")),
        lambda report: report.__setitem__("score", True),
        lambda report: report.__setitem__("score", 101.0),
        lambda report: report.__setitem__("resume_digest", "f" * 64),
        lambda report: report.__setitem__("job_description_digest", "f" * 64),
        lambda report: report.__setitem__("run_id", "other-run"),
        lambda report: report.__setitem__("case_id", "other-case"),
        lambda report: report.__setitem__("policy_version", "forged-policy"),
        lambda report: report.__setitem__("scorer_version", "forged-scorer"),
        lambda report: report.__setitem__("threshold", 69.0),
        lambda report: report.__setitem__("as_of_date", "2099-07-19"),
        lambda report: report["component_scores"].__setitem__(
            "skills_match", float("inf")
        ),
        lambda report: report.__setitem__("passed", False),
    ],
    ids=[
        "extra-key",
        "nan",
        "bool-score",
        "out-of-range",
        "resume-digest",
        "jd-digest",
        "run",
        "case",
        "policy",
        "scorer",
        "lowered-threshold",
        "future-as-of-date",
        "infinite-component",
        "passed-semantics",
    ],
)
def test_malformed_or_misbound_candidate_fit_report_fails_closed(mutator):
    adapter = ScriptedAdapter()
    services = Services(fit_mutator=mutator)

    result = run_team(request(), adapter, services)

    assert result["terminal_class"] == "FAILED:CANDIDATE_FIT_PREFLIGHT"
    assert adapter.calls == []
    assert services.audits == []
    assert services.published == []
    assert result["candidate_fit_report"] is None
    assert result["candidate_fit_report_digest"] == ""


def test_failed_audit_uses_one_editor_then_reaudits():
    adapter = ScriptedAdapter(draft="Checked draft", edited="Reviewed draft")
    services = Services(deterministic_fail_token="Checked")
    result = run_team(request(), adapter, services)
    assert result["terminal_class"] == "PUBLISHED"
    assert adapter.calls == ["researcher", "writer", "auditor", "editor", "auditor"]
    assert len(services.audits) == 2
    assert [report["passed"] for report in services.audits] == [False, True]
    assert [report["codes"] for report in services.audits] == [
        ["HUMAN_VOICE"],
        [],
    ]
    assert all(len(report["votes"]) == 3 for report in services.audits)
    first_vote_ids = {
        vote["invocation_id"] for vote in services.audits[0]["votes"]
    }
    second_vote_ids = {
        vote["invocation_id"] for vote in services.audits[1]["votes"]
    }
    assert len(first_vote_ids | second_vote_ids) == 6
    assert first_vote_ids.isdisjoint(second_vote_ids)
    receipt_vote_ids = set(
        result["authorization_receipt"]["vote_invocation_ids"]
    )
    assert receipt_vote_ids == second_vote_ids
    assert receipt_vote_ids.isdisjoint(first_vote_ids)


def test_actionable_deterministic_only_failure_runs_editor_and_fresh_auditor():
    adapter = ScriptedAdapter(
        draft="Safe draft\nVOICE draft",
        edited="Safe draft",
    )
    services = Services(deterministic_fail_token="VOICE")

    result = run_team(request(), adapter, services)

    assert result["terminal_class"] == "PUBLISHED"
    assert adapter.calls == [
        "researcher",
        "writer",
        "auditor",
        "editor",
        "auditor",
    ]
    assert [report["passed"] for report in services.audits] == [False, True]
    assert services.audits[0]["findings"][0]["actionable"] is True
    assert services.audits[0]["findings"][0]["locations"] == [
        {
            "line_number": eligible_draft("Safe draft\nVOICE draft")
            .splitlines()
            .index("VOICE draft")
            + 1,
            "source_line": "VOICE draft",
            "line_digest": canonical_digest("VOICE draft"),
        }
    ]
    assert result["final_draft_digest"] == canonical_digest(
        eligible_draft("Safe draft")
    )


def test_two_actionable_deterministic_corrections_each_get_fresh_reaudit():
    class DeterministicOnlyTwoEditAdapter(ScriptedAdapter):
        def invoke(self, role, context, timeout_seconds):
            response = super().invoke(role, context, timeout_seconds)
            if role == "auditor":
                draft = context["payload"]["writer_draft"]
                response["payload"] = {
                    "verdict": "PASS",
                    "findings": [],
                    "draft_digest": canonical_digest(draft),
                }
            elif role == "editor":
                response["payload"]["draft"] = (
                    eligible_draft("Safe draft\nBAD draft")
                    if context["attempt"] == 1
                    else eligible_draft("Safe draft")
                )
            response["artifact_digest"] = canonical_digest(response["payload"])
            return response

    class TwoStageServices(Services):
        def audit_draft(self, draft):
            original = self.deterministic_fail_token
            self.deterministic_fail_token = (
                "Checked" if "Checked" in draft else "BAD" if "BAD" in draft else ""
            )
            try:
                return super().audit_draft(draft)
            finally:
                self.deterministic_fail_token = original

    adapter = DeterministicOnlyTwoEditAdapter(
        draft="Safe draft\nChecked draft\nBAD draft"
    )
    services = TwoStageServices()

    result = run_team(request(), adapter, services)

    assert result["terminal_class"] == "PUBLISHED"
    assert adapter.calls == [
        "researcher",
        "writer",
        "auditor",
        "editor",
        "auditor",
        "editor",
        "auditor",
    ]
    assert [report["passed"] for report in services.audits] == [
        False,
        False,
        True,
    ]
    all_vote_ids = [
        vote["invocation_id"]
        for report in services.audits
        for vote in report["votes"]
    ]
    assert len(all_vote_ids) == len(set(all_vote_ids)) == 9
    final_vote_ids = {
        vote["invocation_id"] for vote in services.audits[-1]["votes"]
    }
    assert set(result["authorization_receipt"]["vote_invocation_ids"]) == (
        final_vote_ids
    )


@pytest.mark.parametrize(
    ("field", "forged_value"),
    [
        ("line_number", 99),
        ("source_line", "Safe draft"),
        ("line_digest", "f" * 64),
    ],
)
def test_forged_deterministic_finding_never_reaches_editor(
    field, forged_value
):
    class ForgedFindingServices(Services):
        def audit_draft(self, draft):
            report = super().audit_draft(draft)
            report["findings"][0]["locations"][0][field] = forged_value
            return report

    adapter = ScriptedAdapter(
        draft="Safe draft\nVOICE draft",
        edited="Safe draft",
    )
    services = ForgedFindingServices(deterministic_fail_token="VOICE")

    result = run_team(request(), adapter, services)

    assert result["terminal_class"] == "FAILED:AUTHORIZATION_REPORT"
    assert adapter.calls == ["researcher", "writer", "auditor"]
    assert not services.published


def test_unanchored_deterministic_only_failure_cannot_authorize_editor():
    adapter = ScriptedAdapter(draft="VOICE draft", edited="Safe corrected draft")
    services = Services(
        deterministic_fail_token="VOICE",
        deterministic_actionable=False,
    )
    result = run_team(request(), adapter, services)
    assert result["terminal_class"] == "REJECTED:NONACTIONABLE_HUMAN_VOICE"
    assert adapter.calls == ["researcher", "writer", "auditor"]
    assert [report["passed"] for report in services.audits] == [False]
    assert not services.published


def test_unbound_non_voice_vote_is_explicitly_uneditable():
    class UnboundEvidenceServices(Services):
        def audit_draft(self, draft):
            report = super().audit_draft(draft)
            draft_digest = canonical_digest(draft)
            report["codes"] = ["EVIDENCE_AUDIT_FAILED"]
            report["votes"] = [
                {
                    "schema_version": VOTE_VERSION,
                    "name": name,
                    "invocation_id": f"vote-1-{name}",
                    "passed": name != "evidence",
                    "draft_digest": draft_digest,
                    "codes": (
                        ["EVIDENCE_AUDIT_FAILED"] if name == "evidence" else []
                    ),
                }
                for name in ("evidence", "human_voice", "canonical_integrity")
            ]
            report["findings"] = [
                {
                    "id": "evidence-unbound-1",
                    "vote_name": "evidence",
                    "code": "EVIDENCE_AUDIT_FAILED",
                    "actionable": False,
                    "locations": [],
                }
            ]
            return report

    adapter = ScriptedAdapter(draft="VOICE draft", edited="Safe draft")
    result = run_team(
        request(),
        adapter,
        UnboundEvidenceServices(deterministic_fail_token="VOICE"),
    )

    assert result["terminal_class"] == (
        "REJECTED:UNEDITABLE_DETERMINISTIC_AUDIT"
    )
    assert adapter.calls == ["researcher", "writer", "auditor"]


def test_deterministic_code_does_not_allow_unrelated_editor_addition():
    adapter = ScriptedAdapter(
        draft="Checked draft",
        edited="Reviewed draft\nSafe draft",
    )
    services = Services()
    result = run_team(request(), adapter, services)
    assert result["terminal_class"] == "REJECTED:EDITOR_OVERREACH"
    assert adapter.calls == ["researcher", "writer", "auditor", "editor"]
    assert not services.published


def test_editor_cannot_reorder_unchanged_unrelated_lines():
    adapter = ScriptedAdapter(
        draft="Checked draft\nSafe draft",
        edited="Safe draft\nChecked draft",
    )
    services = Services(deterministic_fail_token="")
    result = run_team(request(), adapter, services)
    assert result["terminal_class"] == "REJECTED:EDITOR_OVERREACH"
    assert adapter.calls == ["researcher", "writer", "auditor", "editor"]
    assert not services.published


def test_editor_must_address_exact_finding_id_set():
    class ExtraFindingAdapter(ScriptedAdapter):
        def invoke(self, role, context, timeout_seconds):
            response = super().invoke(role, context, timeout_seconds)
            if role == "editor":
                response["payload"]["addressed_finding_ids"].append("EXTRA-ID")
                response["artifact_digest"] = canonical_digest(response["payload"])
            return response

    result = run_team(
        request(),
        ExtraFindingAdapter(draft="Checked draft", edited="Reviewed draft"),
        Services(deterministic_fail_token=""),
    )
    assert result["terminal_class"] == "FAILED:EDITOR_SCHEMA"


def test_editor_must_actually_change_every_model_finding_line():
    class TwoFindingAdapter(ScriptedAdapter):
        def invoke(self, role, context, timeout_seconds):
            response = super().invoke(role, context, timeout_seconds)
            if role == "auditor" and context["attempt"] == 0:
                response["payload"] = {
                    "verdict": "FAIL",
                    "findings": [
                        {
                            "id": "F-ONE",
                            "code": "VOICE_ONE",
                            "evidence_digest": canonical_digest("Checked draft"),
                        },
                        {
                            "id": "F-TWO",
                            "code": "VOICE_TWO",
                            "evidence_digest": canonical_digest("BAD draft"),
                        },
                    ],
                    "draft_digest": canonical_digest(
                        context["payload"]["writer_draft"]
                    ),
                }
                response["artifact_digest"] = canonical_digest(response["payload"])
            return response

    result = run_team(
        request(),
        TwoFindingAdapter(
            draft="Checked draft\nBAD draft",
            edited="Reviewed draft\nBAD draft",
        ),
        Services(deterministic_fail_token=""),
    )
    assert result["terminal_class"] == "REJECTED:EDITOR_OVERREACH"


def test_verbatim_nonexperience_line_cannot_move_into_a_role_without_evidence():
    master = """PROFESSIONAL SUMMARY
Reviewed confidential records.

PROFESSIONAL EXPERIENCE
Analyst | Acme
2020 - 2021
Managed routine work.

EDUCATION
Example University
"""
    context = build_context(
        run_id="run-cross-section",
        case_id="case-cross-section",
        role="writer",
        attempt=0,
        payload={"master_resume": master, "researcher_artifact": {}},
    )
    with pytest.raises(ValueError, match="moves source claims") as caught:
        normalize_native_payload(
            "writer",
            {
                "replacements": [
                    {
                        "source_span_text": "Reviewed confidential records.",
                        "replacement_text": "",
                    },
                    {
                        "source_span_text": "Managed routine work.",
                        "replacement_text": "Reviewed confidential records.",
                    },
                ]
            },
            context,
        )
    assert "Reviewed confidential records." in str(caught.value)


def test_writer_cannot_delete_the_only_bullet_from_a_single_role():
    master = """PROFESSIONAL EXPERIENCE
Analyst | Acme
2020 - 2021
• Reviewed records.

EDUCATION
Example University
"""
    with pytest.raises(ValueError, match="moves source claims"):
        normalize_native_payload(
            "writer",
            {
                "replacements": [
                    {
                        "source_span_text": "• Reviewed records.",
                        "replacement_text": "",
                    }
                ]
            },
            writer_context(master),
        )


def test_editor_cannot_delete_the_only_bullet_from_a_single_role():
    source_bullets = """• Led medical review of ICSRs, expectedness and causality assessments, and aggregate safety reports for oncology trials.
• Reviewed DSUR and PBRER reports and assessed safety signals under ICH and CIOMS guidance."""
    master = PASSING_RESUME.replace(source_bullets, "• Checked draft documents")
    empty_role = master.replace("• Checked draft documents\n", "")
    adapter = ScriptedAdapter(draft=master, edited=empty_role)
    services = Services(deterministic_fail_token="")

    # The one-bullet master is deliberately threadbare, so it needs a job
    # description it can actually evidence: the fit gate now scores real
    # evidence and would otherwise reject at Phase 0, before the editor this
    # test exists to exercise is ever invoked.
    thin_jd = (
        "Draft Checker\n"
        "Qualifications\n"
        "Experience checking draft documents is required.\n"
    )
    result = run_team(
        request(master_resume=master, job_description=thin_jd), adapter, services
    )

    assert result["terminal_class"] == "FAILED:EDITOR_SCHEMA"
    assert adapter.calls == ["researcher", "writer", "auditor", "editor"]
    assert not services.published


def test_writer_may_omit_optional_summary_when_each_role_keeps_a_bullet():
    master = """PROFESSIONAL SUMMARY
Optional summary.

PROFESSIONAL EXPERIENCE
Analyst | Acme
2020 - 2021
• Reviewed records.

EDUCATION
Example University
"""
    context = build_context(
        run_id="run-optional-summary",
        case_id="case-optional-summary",
        role="writer",
        attempt=0,
        payload={"master_resume": master, "researcher_artifact": {}},
    )

    normalized = normalize_native_payload(
        "writer",
        {
            "replacements": [
                {
                    "source_span_text": "Optional summary.",
                    "replacement_text": "",
                }
            ]
        },
        context,
    )

    assert normalized == {
        "draft": """PROFESSIONAL SUMMARY


PROFESSIONAL EXPERIENCE
Analyst | Acme
2020 - 2021
• Reviewed records.

EDUCATION
Example University
""",
        "claim_evidence": [],
    }


def test_cross_role_agent_identity_is_rejected_without_publication():
    adapter = ScriptedAdapter(shared_id=True)
    services = Services()
    result = run_team(request(), adapter, services)
    assert result["terminal_class"] == "FAILED:ROLE_SEPARATION"
    assert result["published"] is result["success_reported"] is False
    assert result["final_draft_digest"] == ""
    assert not services.published


@pytest.mark.parametrize(
    ("code", "expected_terminal"),
    [
        ("AGENT_EVENT_MALFORMED", "FAILED:AGENT_EVENT_MALFORMED"),
        ("AGENT_OUTPUT_MALFORMED", "FAILED:AGENT_OUTPUT_MALFORMED"),
        ("AGENT_PAYLOAD_SCHEMA", "FAILED:RESEARCH_SCHEMA"),
    ],
)
def test_typed_native_invocation_failures_keep_specific_terminal_class(
    code,
    expected_terminal,
):
    class BrokenNativeAdapter(ScriptedAdapter):
        def invoke(self, role, context, timeout_seconds):
            raise AgentInvocationFailure(code)

    result = run_team(request(), BrokenNativeAdapter(), Services())

    assert result["terminal_class"] == expected_terminal
    assert result["published"] is result["success_reported"] is False


def test_writer_payload_failure_uses_writer_terminal_class():
    class BrokenWriterAdapter(ScriptedAdapter):
        def invoke(self, role, context, timeout_seconds):
            if role == "writer":
                raise AgentInvocationFailure("AGENT_PAYLOAD_SCHEMA")
            return super().invoke(role, context, timeout_seconds)

    result = run_team(request(), BrokenWriterAdapter(), Services())

    assert result["terminal_class"] == "FAILED:WRITER_SCHEMA"
    assert result["published"] is result["success_reported"] is False


def test_no_safe_writer_changes_rejects_only_the_writer_role():
    class NoSafeWriterAdapter(ScriptedAdapter):
        def invoke(self, role, context, timeout_seconds):
            if role == "writer":
                raise NoSafeWriterChanges(
                    {
                        "proposed_count": 1,
                        "accepted_count": 0,
                        "rejected_count": 1,
                        "rejection_codes": {"STRICT_COMPILER": 1},
                    }
                )
            return super().invoke(role, context, timeout_seconds)

    result = run_team(request(), NoSafeWriterAdapter(), Services())

    assert result["terminal_class"] == "REJECTED:NO_SAFE_CHANGES"
    assert result["published"] is result["success_reported"] is False


def test_no_safe_writer_changes_from_non_writer_fails_as_agent_crash():
    class MisplacedNoSafeAdapter(ScriptedAdapter):
        def invoke(self, role, context, timeout_seconds):
            if role == "researcher":
                raise NoSafeWriterChanges(
                    {
                        "proposed_count": 1,
                        "accepted_count": 0,
                        "rejected_count": 1,
                        "rejection_codes": {"STRICT_COMPILER": 1},
                    }
                )
            return super().invoke(role, context, timeout_seconds)

    result = run_team(request(), MisplacedNoSafeAdapter(), Services())

    assert result["terminal_class"] == "FAILED:AGENT_CRASH"
    assert result["published"] is result["success_reported"] is False


def test_writer_complete_exposes_only_valid_adapter_stats():
    class PayloadServices(Services):
        def __init__(self):
            super().__init__()
            self.event_payloads = []

        def record_event(self, event, payload):
            self.event_payloads.append((event, payload))
            super().record_event(event, payload)

    adapter = ScriptedAdapter()
    adapter.writer_stats = {
        "proposed_count": 2,
        "accepted_count": 1,
        "rejected_count": 1,
        "rejection_codes": {"STRICT_COMPILER": 1},
    }
    services = PayloadServices()

    result = run_team(request(), adapter, services)

    assert result["terminal_class"] == "PUBLISHED"
    writer_complete = next(
        payload
        for event, payload in services.event_payloads
        if event == "writer_complete"
    )
    assert writer_complete["writer_stats"] == adapter.writer_stats


def test_writer_complete_omits_stats_with_unapproved_rejection_code():
    class PayloadServices(Services):
        def __init__(self):
            super().__init__()
            self.event_payloads = []

        def record_event(self, event, payload):
            self.event_payloads.append((event, payload))
            super().record_event(event, payload)

    adapter = ScriptedAdapter()
    adapter.writer_stats = {
        "proposed_count": 2,
        "accepted_count": 1,
        "rejected_count": 1,
        "rejection_codes": {"Candidate resume line: private@example.com": 1},
    }
    services = PayloadServices()

    result = run_team(request(), adapter, services)

    assert result["terminal_class"] == "PUBLISHED"
    writer_complete = next(
        payload
        for event, payload in services.event_payloads
        if event == "writer_complete"
    )
    assert "writer_stats" not in writer_complete


@pytest.mark.parametrize(
    ("role", "agent_id"),
    [
        ("researcher", "local-researcher-1"),
        ("auditor", "local-auditor-1"),
        ("auditor", "claude:cross-host-auditor-1"),
    ],
)
def test_receipt_identities_must_be_native_and_same_host(role, agent_id):
    class InvalidReceiptIdentityAdapter(ScriptedAdapter):
        def invoke(self, current_role, context, timeout_seconds):
            response = super().invoke(current_role, context, timeout_seconds)
            if current_role == role:
                response["agent_id"] = agent_id
            return response

    services = Services()
    result = run_team(request(), InvalidReceiptIdentityAdapter(), services)
    assert result["terminal_class"] == "FAILED:ROLE_SEPARATION"
    assert result["published"] is result["success_reported"] is False
    assert result["authorization_receipt"] is None
    assert not services.published


def test_native_agent_host_recognizes_api_host():
    """The hosted product runtime ("api") must be a first-class native host,
    exactly like the legacy local CLI hosts "codex" and "claude"."""
    assert _native_agent_host("api:researcher.run1.0") == "api"
    assert _native_agent_host("codex:test-researcher-0") == "codex"
    assert _native_agent_host("claude:test-researcher-0") == "claude"


@pytest.mark.parametrize("agent_id", ["openai:x", "x", ""])
def test_native_agent_host_rejects_unknown_or_absent_host(agent_id):
    """The host enum stays closed: adding "api" must not open the gate to
    an arbitrary or missing host prefix."""
    assert _native_agent_host(agent_id) is None


def test_api_host_researcher_and_auditor_with_distinct_ids_publishes():
    class ApiHostAdapter(ScriptedAdapter):
        def invoke(self, role, context, timeout_seconds):
            response = super().invoke(role, context, timeout_seconds)
            if role in {"researcher", "auditor"}:
                response["agent_id"] = f"api:{role}-1"
            return response

    services = Services()
    result = run_team(request(), ApiHostAdapter(), services)
    assert result["terminal_class"] == "PUBLISHED"
    assert result["published"] is result["success_reported"] is True
    receipt = result["authorization_receipt"]
    assert receipt["researcher_agent_id"] == "api:researcher-1"
    assert receipt["auditor_attestation"]["agent_id"] == "api:auditor-1"
    result_schema = json.loads(
        (ROOT / "schemas" / "resume-team-result.schema.json").read_text(
            encoding="utf-8"
        )
    )
    Draft202012Validator(result_schema).validate(result)


def test_api_researcher_with_claude_auditor_fails_role_separation():
    """Distinct hosts must still fail even when one of them is the new
    "api" host: role separation requires researcher and auditor to share
    the SAME host family, not merely a recognized one."""

    class MixedHostAdapter(ScriptedAdapter):
        def invoke(self, role, context, timeout_seconds):
            response = super().invoke(role, context, timeout_seconds)
            if role == "researcher":
                response["agent_id"] = "api:researcher-1"
            elif role == "auditor":
                response["agent_id"] = "claude:auditor-1"
            return response

    result = run_team(request(), MixedHostAdapter(), Services())
    assert result["terminal_class"] == "FAILED:ROLE_SEPARATION"
    assert result["published"] is result["success_reported"] is False


def test_identical_agent_id_for_researcher_and_auditor_fails_role_separation():
    """Regression guard: widening the host enum to include "api" must not
    weaken the independent distinct-identity check between researcher and
    auditor."""

    class SameIdentityAdapter(ScriptedAdapter):
        def invoke(self, role, context, timeout_seconds):
            response = super().invoke(role, context, timeout_seconds)
            if role in {"researcher", "auditor"}:
                response["agent_id"] = "api:same-identity"
            return response

    result = run_team(request(), SameIdentityAdapter(), Services())
    assert result["terminal_class"] == "FAILED:ROLE_SEPARATION"
    assert result["published"] is result["success_reported"] is False


def test_repeated_editor_draft_stops_correction_cycle():
    class CyclingAdapter(ScriptedAdapter):
        def invoke(self, role, context, timeout_seconds):
            response = super().invoke(role, context, timeout_seconds)
            if role == "auditor":
                line = context["payload"]["writer_draft"].splitlines()[-1]
                response["payload"] = {
                    "verdict": "FAIL",
                    "findings": [
                        {
                            "id": "F-CYCLE",
                            "code": "VOICE",
                            "evidence_digest": canonical_digest(line),
                        }
                    ],
                    "draft_digest": canonical_digest(
                        context["payload"]["writer_draft"]
                    ),
                }
                response["artifact_digest"] = canonical_digest(response["payload"])
            elif role == "editor":
                response["payload"]["draft"] = (
                    eligible_draft("Reviewed draft")
                    if context["attempt"] == 1
                    else eligible_draft("Checked draft")
                )
                response["artifact_digest"] = canonical_digest(response["payload"])
            return response

    adapter = CyclingAdapter(draft="Checked draft")
    services = Services()
    result = run_team(request(), adapter, services)
    assert result["terminal_class"] == "REJECTED:CORRECTION_CYCLE"
    assert adapter.calls == [
        "researcher",
        "writer",
        "auditor",
        "editor",
        "auditor",
        "editor",
    ]
    assert not services.published


@pytest.mark.parametrize("attempts", [True, -1, 3, "2"])
def test_caller_cannot_expand_or_corrupt_editor_budget(attempts):
    adapter = ScriptedAdapter()
    services = Services()
    result = run_team(request(max_editor_attempts=attempts), adapter, services)
    assert result["terminal_class"] == "FAILED:REQUEST_SCHEMA"
    assert not adapter.calls
    assert not services.published


def test_publication_exception_never_reports_success():
    adapter = ScriptedAdapter()
    services = Services(fail_publish=True)
    result = run_team(request(), adapter, services)
    assert result["terminal_class"] == "FAILED:PUBLICATION_ATOMICITY"
    assert result["published"] is result["success_reported"] is False
    assert result["final_draft_digest"] == ""


def test_source_is_reattested_immediately_before_publication():
    services = Services(stale_source_before_publish=True)
    result = run_team(request(), ScriptedAdapter(), services)
    assert result["terminal_class"] == "FAILED:SOURCE_ATTESTATION"
    assert services.source_attestations == 2
    assert not services.published


@pytest.mark.parametrize("receipt", ["none", "bad"])
def test_missing_or_invalid_publication_receipt_never_reports_success(receipt):
    services = Services(publication_receipt=receipt)
    result = run_team(request(), ScriptedAdapter(), services)
    assert result["terminal_class"] == "FAILED:PUBLICATION_RECEIPT"
    assert result["published"] is result["success_reported"] is False
    assert result["final_draft_digest"] == ""


@pytest.mark.parametrize(
    "verification_mode", ["bad", "bad_receipt", "bad_receipt_digest"]
)
def test_failed_publication_readback_never_reports_success(verification_mode):
    services = Services(publication_verification=verification_mode)
    result = run_team(request(), ScriptedAdapter(), services)
    assert result["terminal_class"] == "FAILED:PUBLICATION_VERIFICATION"
    assert result["published"] is result["success_reported"] is False


def test_opaque_boolean_authorization_report_is_rejected():
    services = Services(opaque_report=True)
    result = run_team(request(), ScriptedAdapter(), services)
    assert result["terminal_class"] == "FAILED:AUTHORIZATION_REPORT"
    assert not result["published"]
    assert not services.published


@pytest.mark.parametrize("draft", ["Safe draft", "BAD draft"])
def test_authorization_report_missing_findings_is_rejected(draft):
    class LegacyReportServices(Services):
        def audit_draft(self, candidate):
            report = super().audit_draft(candidate)
            del report["findings"]
            return report

    services = LegacyReportServices()
    result = run_team(request(), ScriptedAdapter(draft=draft), services)

    assert result["terminal_class"] == "FAILED:AUTHORIZATION_REPORT"
    assert not result["published"]
    assert not services.published


def test_empty_evidence_cannot_admit_a_synthetic_changed_line():
    result = run_team(
        request(master_resume="MASTER\nVerified source line"),
        ScriptedAdapter(draft="Synthetic invented line"),
        Services(),
    )
    assert result["terminal_class"] == "FAILED:WRITER_SCHEMA"
    assert not result["published"]


@pytest.mark.parametrize(
    "source,draft,expected_code",
    [
        ("Managed a $9B portfolio", "Managed a $9T portfolio", "UNSUPPORTED_METRIC"),
        ("Reviewed analyst reports", "Cured analyst reports", "UNSUPPORTED_CLAIM"),
        ("Reviewed 10+ cases", "Reviewed 10 cases", "UNSUPPORTED_METRIC"),
        (
            "Reviewed 10 cases and 20 reports",
            "Reviewed 10 cases",
            "UNSUPPORTED_METRIC",
        ),
        ("Did not review 12 cases", "Reviewed 12 cases", "UNSUPPORTED_CLAIM"),
        ("Audited by FDA", "Audited FDA", "UNSUPPORTED_CLAIM"),
        (
            "Reviewed confidential analyst reports",
            "Reviewed analyst reports",
            "UNSUPPORTED_CLAIM",
        ),
    ],
)
def test_semantic_or_metric_substitution_is_rejected_by_normalize_and_run(
    source, draft, expected_code
):
    context = build_context(
        run_id="run-unsafe",
        case_id="case-unsafe",
        role="writer",
        attempt=0,
        payload={"master_resume": source, "researcher_artifact": {}},
    )
    with pytest.raises(ValueError, match=expected_code):
        normalize_native_payload(
            "writer",
            {
                "replacements": [
                    {
                        "source_span_text": source,
                        "replacement_text": draft,
                    }
                ],
            },
            context,
        )

    forged_evidence = [
        {
            "claim_digest": canonical_digest(draft),
            "source_span_digest": canonical_digest(source),
            "source_start": 0,
            "source_end": len(source),
        }
    ]
    result = run_team(
        request(master_resume=source),
        ScriptedAdapter(draft=draft, writer_evidence=forged_evidence),
        Services(),
    )
    assert result["terminal_class"] == "FAILED:WRITER_SCHEMA"
    assert not result["published"]


def test_reused_vote_ids_are_rejected_after_correction():
    services = Services(stale_vote_ids=True)
    result = run_team(
        request(),
        ScriptedAdapter(draft="Checked draft", edited="Reviewed draft"),
        services,
    )
    assert result["terminal_class"] == "FAILED:STALE_AUTHORIZATION"
    assert not result["published"]


def test_reused_agent_id_is_rejected_on_fresh_reaudit():
    result = run_team(
        request(),
        ScriptedAdapter(
            draft="Checked draft",
            edited="Reviewed draft",
            reuse_auditor_id=True,
        ),
        Services(),
    )
    assert result["terminal_class"] == "FAILED:ROLE_SEPARATION"
    assert not result["published"]


def test_native_host_files_are_parseable_unpinned_and_match():
    codex_paths = sorted((ROOT / ".codex" / "agents").glob("resume-*.toml"))
    claude_paths = sorted((ROOT / ".claude" / "agents").glob("resume-*.md"))
    plugin_agent_paths = sorted((ROOT / "agents").glob("resume-*.md"))
    assert len(codex_paths) == len(claude_paths) == len(plugin_agent_paths) == 4
    for path in codex_paths:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
        assert set(data) == {
            "name",
            "description",
            "sandbox_mode",
            "developer_instructions",
        }
        assert data["sandbox_mode"] == "read-only"
        assert "model" not in data and "reasoning" not in data
        assert HANDOFF_VERSION in data["developer_instructions"]
    for path, plugin_path in zip(claude_paths, plugin_agent_paths, strict=True):
        text = path.read_text(encoding="utf-8")
        assert text.startswith("---\n")
        frontmatter = text.split("---\n", 2)[1]
        assert "name:" in frontmatter and "description:" in frontmatter
        assert [
            line.strip()
            for line in frontmatter.splitlines()
            if line.strip().startswith("tools:")
        ] == ["tools: []"]
        assert "model:" not in frontmatter
        assert "effort:" not in frontmatter
        assert HANDOFF_VERSION in text
        assert path.read_bytes() == plugin_path.read_bytes()

    command = (ROOT / "commands" / "resume-team.md").read_bytes()
    mirror = (ROOT / ".claude" / "commands" / "resume-team.md").read_bytes()
    assert command == mirror
    lowered = command.decode("utf-8").lower()
    for marker in (
        "researcher",
        "writer",
        "auditor",
        "editor",
        "three independent authorization votes",
        "evidence_audit.py",
        "human_voice_audit.py",
        "resume_integrity_audit.py",
        "editor `draft`/`addressed_finding_ids`/`claim_evidence`",
        "source_start",
        "source_end",
        "globally unique real native `agent_id` for every invocation",
        "native_resume_team.py --host <codex|claude>",
        "one-for-one, byte-for-byte",
        "posix_runtime_required",
        "terminal_class: published",
        "draft-stage artifact",
        "not a completed application package",
        "authorization_receipt_path",
        "authorization_receipt_digest",
        "auditor_attestation",
        "authorization_report",
        "canonical_digest(authorization_report)",
        "verify",
    ):
        assert marker in lowered

    researcher = (ROOT / ".claude" / "agents" / "resume-researcher.md").read_text(
        encoding="utf-8"
    )
    assert "one-for-one, byte-for-byte, and in the same order" in researcher
    assert "missing, extra, reordered, paraphrased" in researcher

    for command_name in ("resume.md", "tailor-resume.md", "batch-resume.md"):
        source = (ROOT / "commands" / command_name).read_bytes()
        claude_mirror = (ROOT / ".claude" / "commands" / command_name).read_bytes()
        assert source == claude_mirror
        source_text = source.decode("utf-8").lower()
        assert "native_resume_team.py" in source_text
        assert "terminal_class: published" in source_text
        assert "final_draft_digest" in source_text
        assert "authorization_receipt_path" in source_text
        assert "authorization_receipt_digest" in source_text
        assert "auditor_attestation" in source_text
        assert "authorization_report" in source_text
        assert "authorization_digest" in source_text
        assert "vote_invocation_ids" in source_text
        assert "schemas/resume-team-final-receipt.schema.json" in source_text
        assert "non-symlink" in source_text
        assert "immediately before docx" in source_text
        assert "never delete" in source_text and "receipt" in source_text
        assert "draft-stage" in source_text
        assert "package completion" in source_text or "completed package" in source_text
        assert "verify_final_receipt" in source_text
        assert "create_resume_from_md_authorized" in source_text
        assert "add_application_authorized" in source_text
        assert "expected_receipt_digest" in source_text
        assert "if updated is not true" in source_text
        assert "trackerupdateerror" in source_text
        assert "create_resume_from_md(" not in source_text
        assert "add_application(" not in source_text
        assert "save as resume.md" not in source_text
        assert "save as `resume.md`" not in source_text
        if command_name in ("resume.md", "batch-resume.md"):
            assert "create_cover_letter_from_md_authorized" in source_text

    schema = json.loads(
        (ROOT / "schemas" / "resume-team-handoff.schema.json").read_text(
            encoding="utf-8"
        )
    )
    assert schema["$id"] == HANDOFF_VERSION
    assert schema["additionalProperties"] is False
    assert schema["properties"]["case_id"] == {"type": "string", "minLength": 1}
    assert schema["properties"]["attempt"]["maximum"] == 2
    assert schema["properties"]["status"] == {"const": "complete"}
    assert schema["$defs"]["claimEvidence"]["required"] == [
        "claim_digest",
        "source_span_digest",
        "source_start",
        "source_end",
    ]
    assert schema["$defs"]["editorPayload"]["required"] == [
        "draft",
        "addressed_finding_ids",
        "claim_evidence",
    ]
    assert "one-for-one" in schema["$defs"]["researcherPayload"]["description"]
    role_attempts = {
        branch["if"]["properties"]["role"]["const"]: branch["then"][
            "properties"
        ]["attempt"]
        for branch in schema["allOf"]
    }
    assert role_attempts == {
        "researcher": {"const": 0},
        "writer": {"const": 0},
        "auditor": {"type": "integer", "minimum": 0, "maximum": 2},
        "editor": {"type": "integer", "minimum": 1, "maximum": 2},
    }

    codex_manifest = json.loads(
        (ROOT / ".codex-plugin" / "plugin.json").read_text(encoding="utf-8")
    )
    assert codex_manifest["skills"] == "./skills/"
    skill_path = ROOT / codex_manifest["skills"] / "resume-team" / "SKILL.md"
    assert skill_path.is_file()
    skill_text = skill_path.read_text(encoding="utf-8")
    assert "python native_resume_team.py --host codex" in skill_text
    assert "Do not manually spawn role agents as a fallback" in skill_text
    assert "draft-stage artifact" in skill_text
    assert "--reasoning-effort ultra" in skill_text
    assert "POSIX_RUNTIME_REQUIRED" in skill_text
    assert "authorization_receipt_path" in skill_text
    assert "authorization_receipt_digest" in skill_text
    assert "auditor_attestation" in skill_text
    assert "authorization_report" in skill_text
    assert "canonical_digest(authorization_report)" in skill_text
    assert "schemas/resume-team-final-receipt.schema.json" in skill_text
    assert "final_receipt_verifier.py" in skill_text
    assert "create_resume_from_md_authorized" in skill_text
    assert "create_cover_letter_from_md_authorized" in skill_text
    assert "add_application_authorized" in skill_text
    assert "literally `true`" in skill_text.lower()
    assert "non-symlink" in skill_text
    assert "Never reuse, replace, or clobber" in skill_text
    assert "--replace-existing" not in skill_text
    assert "--codex-profile" not in skill_text
    assert "default subagent" not in skill_text
    assert (ROOT / "native_resume_team.py").is_file()

    result_schema = json.loads(
        (ROOT / "schemas" / "resume-team-result.schema.json").read_text(
            encoding="utf-8"
        )
    )
    assert "draft publication" in result_schema["description"]
    assert "package completion" in result_schema["description"]
    assert "authorization_receipt" in result_schema["required"]
    assert "authorization_receipt_digest" in result_schema["required"]
    assert "authorization_receipt_path" in result_schema["required"]
    receipt_schema = result_schema["$defs"]["authorizationReceipt"]
    assert "auditor_attestation" in receipt_schema["required"]
    assert "authorization_report" in receipt_schema["required"]
    assert "source_digest" in receipt_schema["required"]
    assert "job_description_digest" in receipt_schema["required"]
    assert "researcher_artifact_digest" in receipt_schema["required"]
    assert "canonical" in receipt_schema["description"]
    report_schema = result_schema["$defs"]["authorizationReport"]
    assert report_schema["properties"]["passed"] == {"const": True}
    assert report_schema["properties"]["codes"] == {
        "type": "array",
        "maxItems": 0,
    }

    final_receipt_schema = json.loads(
        (ROOT / "schemas" / "resume-team-final-receipt.schema.json").read_text(
            encoding="utf-8"
        )
    )
    assert final_receipt_schema["$id"] == FINAL_RECEIPT_VERSION
    assert final_receipt_schema["additionalProperties"] is False
    assert final_receipt_schema["required"] == receipt_schema["required"]
    assert "auditor_attestation" in final_receipt_schema["required"]
    assert "authorization_report" in final_receipt_schema["required"]
    assert "canonical SHA-256" in final_receipt_schema["description"]

    marketplace = json.loads(
        (ROOT / ".agents" / "plugins" / "marketplace.json").read_text(
            encoding="utf-8"
        )
    )
    plugin_source = marketplace["plugins"][0]["source"]["path"]
    assert plugin_source == "./"
    assert (ROOT / plugin_source / ".codex-plugin" / "plugin.json").is_file()

    codex_guidance = (ROOT / "AGENTS.md").read_text(encoding="utf-8")
    assert "$resume-team [job description]" in codex_guidance
    assert "Runtime `PUBLISHED` means only" in codex_guidance
    assert "POSIX_RUNTIME_REQUIRED" in codex_guidance
    assert "not installed Codex `/resume-builder:*` commands" in " ".join(
        codex_guidance.split()
    )


def test_controller_dependency_closure_is_offline_and_sdk_free():
    tree = ast.parse((ROOT / "multi_agent_team.py").read_text(encoding="utf-8"))
    imports = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imports.add(node.module.split(".")[0])
    assert not imports & {
        "anthropic",
        "openai",
        "autogen",
        "crewai",
        "langgraph",
        "subprocess",
        "socket",
    }
