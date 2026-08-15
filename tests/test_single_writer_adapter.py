"""Behavioral coverage for the direct synchronous web rewrite service."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

import agent.web_rewrite as web_rewrite
import human_voice_audit
from agent.host_anthropic import AnthropicHost
from candidate_fit_preflight import (
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

from multi_agent_team import (
    AUTHORIZATION_VERSION,
    PUBLICATION_VERSION,
    RUN_CLAIM_VERSION,
    SOURCE_ATTESTATION_VERSION,
    VOTE_VERSION,
    canonical_digest,
)

_TRACED_RESUME = """ALEX DOE, MD
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

_PASSING_JD = """Director, Drug Safety Physician
About the job
Lead safety surveillance for oncology products.
Qualifications
5+ years' experience in Drug Safety/Pharmacovigilance in a biotech company.
Medical Degree (MD) with medical practice experience.
Strong knowledge of ICH and CIOMS guidelines.
Experience reviewing ICSRs and preparing DSUR and PBRER reports.
"""

_LOW_FIT_JD = """Regional Retail Sales Manager
About the job
Lead consumer retail sales operations across regional stores.
Qualifications
Experience leading retail sales teams, store merchandising, customer acquisition, regional account planning, consumer promotions, and sales forecasting. Clear communication and practical team leadership are important for this role.
"""

_SAFE_SOURCE = (
    "• Reviewed DSUR and PBRER reports and assessed safety signals under ICH "
    "and CIOMS guidance."
)
_SAFE_REWRITE = (
    "• Checked DSUR and PBRER reports and assessed safety signals under ICH "
    "and CIOMS guidance."
)
_RUN_ID = "run-single-writer"
_CASE_ID = "case-single-writer"


def make_semantic_response(
    replacements: list[dict[str, str]],
    supported: list[bool] | None = None,
):
    flags = supported if supported is not None else [True] * len(replacements)

    def response(call: dict) -> SimpleNamespace:
        payload = json.loads(call["messages"][0]["content"])
        assert [item["source_span_text"] for item in payload["proposals"]] == [
            item["source_span_text"] for item in replacements
        ]
        return make_response(
            {
                key: payload[key]
                for key in (
                    "run_id",
                    "case_id",
                    "source_digest",
                    "proposal_set_digest",
                    "lens",
                    "invocation_id",
                )
            }
            | {
                "schema_version": web_rewrite.SEMANTIC_REVIEW_VERSION,
                "decisions": [
                    {
                        "proposal_id": proposal["proposal_id"],
                        "supported": allowed,
                        "code": "PASS" if allowed else "UNSUPPORTED_CLAIM",
                        "evidence_lines": [item["source_span_text"]]
                        if allowed
                        else [],
                    }
                    for item, proposal, allowed in zip(
                        replacements, payload["proposals"], flags
                    )
                ],
            }
        )

    return response


class _Messages:
    def __init__(self, responses):
        self._responses = list(responses)
        self.calls: list[dict] = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        response = self._responses.pop(0)
        return response(kwargs) if callable(response) else response


class FakeClient:
    def __init__(self, responses):
        self.messages = _Messages(responses)

    @property
    def calls(self):
        return self.messages.calls


def make_response(payload: dict | str) -> SimpleNamespace:
    text = payload if isinstance(payload, str) else json.dumps(payload)
    return SimpleNamespace(
        content=[SimpleNamespace(type="text", text=text)],
        stop_reason="end_turn",
        usage=SimpleNamespace(input_tokens=100, output_tokens=25),
    )


class Services:
    """Code-owned boundaries are real shapes; only persistence is in memory."""

    def __init__(self):
        self.events: list[tuple[str, dict]] = []
        self.published: list[tuple[str, dict]] = []
        self._publications: dict[str, dict] = {}

    def claim_run(self, run_id, case_id):
        return {
            "schema_version": RUN_CLAIM_VERSION,
            "run_id": run_id,
            "case_id": case_id,
            "claimed": True,
            "claim_id": f"claim-{run_id}-{case_id}",
        }

    def attest_source(self, master_resume):
        return {
            "schema_version": SOURCE_ATTESTATION_VERSION,
            "trusted": True,
            "source_id": "fixture-master",
            "source_digest": canonical_digest(master_resume),
        }

    def assess_candidate_fit(self, master_resume, job_description, run_id, case_id):
        return assess_candidate_fit(
            master_resume,
            job_description,
            run_id=run_id,
            case_id=case_id,
            as_of_date="2026-07-19",
            extraction_trustworthy=True,
        )

    def audit_draft(self, draft):
        digest = canonical_digest(draft)
        return {
            "schema_version": AUTHORIZATION_VERSION,
            "draft_digest": digest,
            "passed": True,
            "codes": [],
            "votes": [
                {
                    "schema_version": VOTE_VERSION,
                    "name": name,
                    "invocation_id": f"vote-1-{name}",
                    "passed": True,
                    "draft_digest": digest,
                    "codes": [],
                }
                for name in ("evidence", "human_voice", "canonical_integrity")
            ],
            "findings": [],
        }

    def record_event(self, event, payload):
        self.events.append((event, payload))

    def publish(self, draft, metadata):
        digest = canonical_digest(draft)
        publication_id = "publication-1"
        stored = {
            "publication_id": publication_id,
            "draft": draft,
            "metadata": metadata,
        }
        self.published.append((draft, metadata))
        self._publications[publication_id] = json.loads(json.dumps(stored))
        return {
            "schema_version": PUBLICATION_VERSION,
            "committed": True,
            "draft_digest": digest,
            "target_digest": digest,
            "publication_id": publication_id,
        }

    def read_publication(self, publication_id):
        return self._publications[publication_id]


def run(responses, services=None):
    client = FakeClient(responses)
    result = web_rewrite.run_web_rewrite(
        run_id=_RUN_ID,
        case_id=_CASE_ID,
        master_resume=_TRACED_RESUME,
        job_description=_PASSING_JD,
        host=AnthropicHost(client=client),
        services=services or Services(),
    )
    return result, client


def test_removed_adapter_export_fails_explicitly_instead_of_importing_unsafely():
    from agent.adapter import SingleWriterTeamAdapter

    with pytest.raises(RuntimeError, match="agent.web_rewrite.run_web_rewrite"):
        SingleWriterTeamAdapter(AnthropicHost(client=FakeClient([])))


def test_direct_web_rewrite_calls_only_writer_and_publishes_verified_readback():
    services = Services()
    result, client = run([make_response({"replacements": []})], services)

    assert result["terminal_class"] == "PUBLISHED"
    assert result["final_draft"] == _TRACED_RESUME
    assert [call["model"] for call in client.calls] == ["claude-sonnet-5"]
    assert len(client.calls) == 1
    receipt = result["authorization_receipt"]
    assert receipt["schema_version"] == web_rewrite.WEB_REWRITE_RECEIPT_VERSION
    assert receipt["verified_target_digest"] == canonical_digest(_TRACED_RESUME)
    assert "researcher_agent_id" not in receipt
    assert "auditor_attestation" not in receipt
    assert [name for name, _ in services.events] == [
        "rewrite_requirements_ready",
        "rewrite_draft_compiled",
        "rewrite_authorized",
        "rewrite_pre_publish",
    ]


def test_requirement_rubric_keeps_only_unique_complete_jd_lines():
    job_description = """Requirements
Must hold an MD.
Collaborate with safety teams.
Collaborate with safety teams.
---
Experience reviewing ICSRs is required.
"""

    assert web_rewrite.derive_requirement_rubric(job_description) == {
        "hard_requirements": [
            "Must hold an MD.",
            "Experience reviewing ICSRs is required.",
        ],
        "soft_requirements": [],
    }


def test_candidate_fit_rejection_stops_before_writer_and_publication():
    class RejectedFitServices(Services):
        def __init__(self):
            super().__init__()
            self.judge_calls = 0

        def assess_candidate_fit(
            self, master_resume, job_description, run_id, case_id
        ):
            return assess_candidate_fit(
                master_resume,
                """Retail Sales Manager
Qualifications
20+ years of retail merchandising experience required.
""",
                run_id=run_id,
                case_id=case_id,
                as_of_date="2026-07-19",
                extraction_trustworthy=True,
            )

        def judge_candidate_fit(self, *args):
            self.judge_calls += 1
            raise AssertionError("hard knockout reached reasoning judge")

    services = RejectedFitServices()
    client = FakeClient([])
    rejection_jd = """Retail Sales Manager
Qualifications
20+ years of retail merchandising experience required.
"""
    result = web_rewrite.run_web_rewrite(
        run_id=_RUN_ID,
        case_id=_CASE_ID,
        master_resume=_TRACED_RESUME,
        job_description=rejection_jd,
        host=AnthropicHost(client=client),
        services=services,
    )

    assert result["terminal_class"] == "REJECTED:CANDIDATE_FIT"
    assert result["candidate_fit_report"] is not None
    assert services.events == []
    assert services.published == []
    assert services.judge_calls == 0
    assert client.calls == []


def test_low_score_cannot_be_overridden_by_reasoning_judge():
    class LowFitServices(Services):
        def __init__(self):
            super().__init__()
            self.judge_calls = 0

        def judge_candidate_fit(self, *args):
            self.judge_calls += 1
            raise AssertionError("score rejection reached reasoning judge")

    services = LowFitServices()
    client = FakeClient([])
    result = web_rewrite.run_web_rewrite(
        run_id=_RUN_ID,
        case_id=_CASE_ID,
        master_resume=_TRACED_RESUME,
        job_description=_LOW_FIT_JD,
        host=AnthropicHost(client=client),
        services=services,
    )

    assert result["terminal_class"] == "REJECTED:CANDIDATE_FIT"
    assert result["candidate_fit_report"]["score"] < 70
    assert services.judge_calls == 0
    assert services.events == []
    assert services.published == []
    assert client.calls == []


def test_malformed_candidate_fit_report_fails_closed_before_writer():
    class MalformedFitServices(Services):
        def assess_candidate_fit(self, *args):
            return {"passed": True, "score": 100}

    services = MalformedFitServices()
    result, client = run([], services)

    assert result["terminal_class"] == "FAILED:CANDIDATE_FIT_PREFLIGHT"
    assert services.events == []
    assert services.published == []
    assert client.calls == []


def test_failed_authoritative_vote_cannot_publish_or_trigger_another_model_call():
    class FailingVoteServices(Services):
        def audit_draft(self, draft):
            report = super().audit_draft(draft)
            report["passed"] = False
            report["codes"] = ["HUMAN_VOICE"]
            report["votes"][1]["passed"] = False
            report["votes"][1]["codes"] = ["HUMAN_VOICE"]
            report["findings"] = [
                {
                    "id": "human-voice-1",
                    "vote_name": "human_voice",
                    "code": "HUMAN_VOICE",
                    "actionable": False,
                    "locations": [],
                }
            ]
            return report

    services = FailingVoteServices()
    result, client = run([make_response({"replacements": []})], services)

    assert result["published"] is False
    assert result["terminal_class"] == "REJECTED:NONACTIONABLE_HUMAN_VOICE"
    assert services.published == []
    assert len(client.calls) == 1


def test_writer_salvages_safe_subset_without_a_second_model_call():
    unsafe = {
        "source_span_text": (
            "DIRECTOR, DRUG SAFETY PHYSICIAN | Acme Biotech | Boston, MA"
        ),
        "replacement_text": "Vice President | Acme Biotech | Boston, MA",
    }
    safe = {"source_span_text": _SAFE_SOURCE, "replacement_text": _SAFE_REWRITE}
    services = Services()

    result, client = run(
        [
            make_response({"replacements": [unsafe, safe]}),
            make_semantic_response([safe], [True]),
            make_semantic_response([safe], [True]),
        ],
        services,
    )

    assert result["terminal_class"] == "PUBLISHED"
    assert services.published[0][0].count(_SAFE_REWRITE) == 1
    assert len(client.calls) == 3
    assert result["writer_stats"] == {
        "proposed_count": 2,
        "accepted_count": 1,
        "rejected_count": 1,
        "rejection_codes": {"INVALID_ANCHOR": 1},
    }



def test_truthful_paraphrase_is_accepted_after_independent_semantic_review():
    paraphrase = {
        "source_span_text": _SAFE_SOURCE,
        "replacement_text": (
            "• Assessed safety signals and reviewed DSUR and PBRER reports "
            "under CIOMS and ICH guidance."
        ),
    }
    services = Services()

    result, client = run(
        [
            make_response({"replacements": [paraphrase]}),
            make_semantic_response([paraphrase], [True]),
            make_semantic_response([paraphrase], [True]),
        ],
        services,
    )

    assert result["terminal_class"] == "PUBLISHED"
    assert paraphrase["replacement_text"] in result["final_draft"]
    assert len(client.calls) == 3
    assert "Freely rewrite, shorten, reorder, clarify" in client.calls[0]["system"]
    assert all(
        "independent skeptical factual reviewer" in call["system"]
        for call in client.calls[1:]
    )
    receipt = result["authorization_receipt"]
    assert receipt["semantic_review"]["passed_count"] == 1
    assert receipt["semantic_review_digest"] == canonical_digest(
        receipt["semantic_review"]
    )
    attestations = receipt["semantic_review"]["attestations"]
    assert len({item["lens"] for item in attestations}) == 2
    assert len({item["invocation_id"] for item in attestations}) == 2
    assert len({item["review_digest"] for item in attestations}) == 2
    assert all(
        item["decisions"][0]["evidence_line_digests"]
        == [canonical_digest(_SAFE_SOURCE)]
        for item in attestations
    )



def test_both_distinct_review_lenses_must_pass():
    paraphrase = {
        "source_span_text": _SAFE_SOURCE,
        "replacement_text": "• Reviewed oncology safety reports under ICH guidance.",
    }
    services = Services()

    result, client = run(
        [
            make_response({"replacements": [paraphrase]}),
            make_semantic_response([paraphrase], [True]),
            make_semantic_response([paraphrase], [False]),
        ],
        services,
    )

    assert result["terminal_class"] == "REJECTED:NO_SAFE_CHANGES"
    assert result["writer_stats"]["rejection_codes"] == {
        "SEMANTIC_SUPPORT": 1
    }
    assert len(client.calls) == 3
    first_payload = json.loads(client.calls[1]["messages"][0]["content"])
    second_payload = json.loads(client.calls[2]["messages"][0]["content"])
    assert {first_payload["lens"], second_payload["lens"]} == {
        "claim_entailment",
        "skeptical_recruiter",
    }
    assert first_payload["invocation_id"] != second_payload["invocation_id"]
    assert "job_description" not in first_payload
    assert "job_description" not in second_payload
    assert services.published == []

def test_malformed_semantic_review_fails_closed():
    paraphrase = {
        "source_span_text": _SAFE_SOURCE,
        "replacement_text": "• Reviewed oncology safety reports.",
    }
    services = Services()

    result, client = run(
        [
            make_response({"replacements": [paraphrase]}),
            make_response({"decisions": [{"supported": True, "code": "PASS"}]}),
        ],
        services,
    )

    assert result["terminal_class"] == "FAILED:SEMANTIC_REVIEW_SCHEMA"
    assert services.published == []
    assert len(client.calls) == 2




def test_semantic_review_cannot_replay_across_source_or_run():
    paraphrase = {
        "source_span_text": _SAFE_SOURCE,
        "replacement_text": "• Reviewed oncology safety reports under ICH guidance.",
    }

    def stale_response(call):
        payload = json.loads(call["messages"][0]["content"])
        return make_response(
            {
                "schema_version": web_rewrite.SEMANTIC_REVIEW_VERSION,
                "run_id": "different-run",
                "case_id": payload["case_id"],
                "source_digest": "0" * 64,
                "proposal_set_digest": payload["proposal_set_digest"],
                "lens": payload["lens"],
                "invocation_id": payload["invocation_id"],
                "decisions": [
                    {
                        "proposal_id": payload["proposals"][0]["proposal_id"],
                        "supported": True,
                        "code": "PASS",
                        "evidence_lines": [_SAFE_SOURCE],
                    }
                ],
            }
        )

    result, client = run(
        [make_response({"replacements": [paraphrase]}), stale_response]
    )

    assert result["terminal_class"] == "FAILED:SEMANTIC_REVIEW_SCHEMA"
    assert len(client.calls) == 2

@pytest.mark.parametrize("bad_code", [[], {}, ["PASS"]])
def test_unhashable_semantic_code_returns_stable_schema_failure(bad_code):
    paraphrase = {
        "source_span_text": _SAFE_SOURCE,
        "replacement_text": "• Reviewed oncology safety reports under ICH guidance.",
    }

    def malformed_response(call):
        payload = json.loads(call["messages"][0]["content"])
        return make_response(
            {
                key: payload[key]
                for key in (
                    "run_id",
                    "case_id",
                    "source_digest",
                    "proposal_set_digest",
                    "lens",
                    "invocation_id",
                )
            }
            | {
                "schema_version": web_rewrite.SEMANTIC_REVIEW_VERSION,
                "decisions": [
                    {
                        "proposal_id": payload["proposals"][0]["proposal_id"],
                        "supported": True,
                        "code": bad_code,
                        "evidence_lines": [_SAFE_SOURCE],
                    }
                ],
            }
        )

    result, client = run(
        [
            make_response({"replacements": [paraphrase]}),
            malformed_response,
        ]
    )

    assert result["terminal_class"] == "FAILED:SEMANTIC_REVIEW_SCHEMA"
    assert len(client.calls) == 2

def test_semantic_review_decisions_are_digest_bound_and_exact_boolean():
    first = {
        "source_span_text": _SAFE_SOURCE,
        "replacement_text": "• Reviewed oncology safety reports under ICH guidance.",
    }
    second_source = (
        "• Led medical review of ICSRs, expectedness and causality assessments, "
        "and aggregate safety reports for oncology trials."
    )
    second = {
        "source_span_text": second_source,
        "replacement_text": "• Reviewed oncology ICSRs and aggregate safety reports.",
    }
    forged = {
        "decisions": [
            {
                "proposal_id": "b" * 64,
                "supported": "true",
                "code": "PASS",
            },
            {
                "proposal_id": "a" * 64,
                "supported": True,
                "code": "PASS",
            },
        ]
    }

    result, client = run(
        [make_response({"replacements": [first, second]}), make_response(forged)]
    )

    assert result["terminal_class"] == "FAILED:SEMANTIC_REVIEW_SCHEMA"
    assert len(client.calls) == 2


def test_semantic_pass_citation_cannot_cross_experience_roles():
    second_role = """
SAFETY SCIENTIST | Other Biotech | Boston, MA
Jan 2010 - Dec 2017
• Used Oracle Argus for case processing.
"""
    resume = _TRACED_RESUME.replace(
        "EDUCATION\n", second_role + "EDUCATION\n"
    )
    paraphrase = {
        "source_span_text": _SAFE_SOURCE,
        "replacement_text": "• Reviewed safety reports using Oracle Argus.",
    }

    def cross_role_response(call):
        payload = json.loads(call["messages"][0]["content"])
        return make_response(
            {
                key: payload[key]
                for key in (
                    "run_id",
                    "case_id",
                    "source_digest",
                    "proposal_set_digest",
                    "lens",
                    "invocation_id",
                )
            }
            | {
                "schema_version": web_rewrite.SEMANTIC_REVIEW_VERSION,
                "decisions": [
                    {
                        "proposal_id": payload["proposals"][0]["proposal_id"],
                        "supported": True,
                        "code": "PASS",
                        "evidence_lines": [
                            "• Used Oracle Argus for case processing."
                        ],
                    }
                ],
            }
        )

    services = Services()
    client = FakeClient(
        [make_response({"replacements": [paraphrase]}), cross_role_response]
    )
    result = web_rewrite.run_web_rewrite(
        run_id=_RUN_ID,
        case_id=_CASE_ID,
        master_resume=resume,
        job_description=_PASSING_JD,
        host=AnthropicHost(client=client),
        services=services,
    )

    assert result["terminal_class"] == "FAILED:SEMANTIC_REVIEW_SCHEMA"
    assert services.published == []

def test_additive_fabrication_is_rejected_by_semantic_review():
    fabricated = {
        "source_span_text": _SAFE_SOURCE,
        "replacement_text": (
            _SAFE_SOURCE[:-1] + " and won a Nobel Prize."
        ),
    }
    services = Services()

    result, client = run(
        [
            make_response({"replacements": [fabricated]}),
            make_semantic_response([fabricated], [False]),
            make_semantic_response([fabricated], [False]),
        ],
        services,
    )

    assert result["terminal_class"] == "REJECTED:NO_SAFE_CHANGES"
    assert result["writer_stats"] == {
        "proposed_count": 1,
        "accepted_count": 0,
        "rejected_count": 1,
        "rejection_codes": {"SEMANTIC_SUPPORT": 1},
    }
    assert services.published == []
    assert len(client.calls) == 3


def test_fake_acronym_expansion_is_rejected_by_direct_service():
    source = "• Wrote SOPs for safety review and clinical operations."
    replacement = (
        "• Wrote secured outstanding prizes (SOPs) for safety review "
        "and clinical operations."
    )
    resume = _TRACED_RESUME.replace(_SAFE_SOURCE, source)
    services = Services()
    proposal = {
        "source_span_text": source,
        "replacement_text": replacement,
    }
    client = FakeClient(
        [
            make_response({"replacements": [proposal]}),
            make_semantic_response([proposal], [False]),
            make_semantic_response([proposal], [False]),
        ]
    )

    result = web_rewrite.run_web_rewrite(
        run_id=_RUN_ID,
        case_id=_CASE_ID,
        master_resume=resume,
        job_description=_PASSING_JD,
        host=AnthropicHost(client=client),
        services=services,
    )

    assert result["terminal_class"] == "REJECTED:NO_SAFE_CHANGES"
    assert result["writer_stats"]["rejection_codes"] == {"SEMANTIC_SUPPORT": 1}
    assert services.published == []
    assert len(client.calls) == 3





def test_summary_content_cannot_be_reclassified_as_unknown_heading():
    import human_voice_audit

    source = "Drug safety physician with eight years in pharmacovigilance."
    replacement = "ROBUST DRUG SAFETY PHYSICIAN"
    bypass_draft = _TRACED_RESUME.replace(source, replacement)
    voice_report = human_voice_audit.audit_text(bypass_draft, mode="resume")
    assert voice_report["passed"] is True
    assert voice_report["stats"]["summary_words"] == 0

    proposal = {
        "source_span_text": source,
        "replacement_text": replacement,
    }
    services = Services()
    result, client = run(
        [make_response({"replacements": [proposal]})], services
    )

    assert result["terminal_class"] == "REJECTED:NO_SAFE_CHANGES"
    assert result["writer_stats"]["rejection_codes"] == {
        "STRICT_COMPILER": 1
    }
    assert len(client.calls) == 1
    assert services.published == []

def test_experience_bullet_cannot_be_reclassified_to_evade_voice_audit():
    import human_voice_audit

    replacement = (
        "Leveraged robust review of DSUR and PBRER reports and assessed "
        "safety signals under ICH and CIOMS guidance."
    )
    bypass_draft = _TRACED_RESUME.replace(_SAFE_SOURCE, replacement)
    assert human_voice_audit.audit_text(bypass_draft, mode="resume")["passed"] is True

    proposal = {
        "source_span_text": _SAFE_SOURCE,
        "replacement_text": replacement,
    }
    services = Services()
    result, client = run(
        [make_response({"replacements": [proposal]})], services
    )

    assert result["terminal_class"] == "REJECTED:NO_SAFE_CHANGES"
    assert result["writer_stats"]["rejection_codes"] == {
        "STRICT_COMPILER": 1
    }
    assert len(client.calls) == 1
    assert services.published == []



@pytest.mark.parametrize("replacement", ["...", "!!!", "---"])
def test_any_replacement_must_retain_substantive_ascii_content(replacement):
    source = "Drug safety physician with eight years in pharmacovigilance."
    proposal = {
        "source_span_text": source,
        "replacement_text": replacement,
    }
    services = Services()

    result, client = run(
        [make_response({"replacements": [proposal]})], services
    )

    assert result["terminal_class"] == "REJECTED:NO_SAFE_CHANGES"
    assert result["writer_stats"]["rejection_codes"] == {
        "STRICT_COMPILER": 1
    }
    assert len(client.calls) == 1
    assert services.published == []


@pytest.mark.parametrize("marker", ["+ ", "1. "])
def test_web_rejects_bullet_markers_ignored_by_real_audits(marker):
    source_body = _SAFE_SOURCE.removeprefix("• ")
    source = marker + source_body
    replacement = marker + "Leveraged robust " + source_body[0].lower() + source_body[1:]
    custom_resume = _TRACED_RESUME.replace(_SAFE_SOURCE, source)
    assert human_voice_audit.audit_text(custom_resume, mode="resume")[
        "bullet_count"
    ] == 1  # Only the ordinary experience bullet; this source is skipped.
    proposal = {
        "source_span_text": source,
        "replacement_text": replacement,
    }
    services = Services()
    client = FakeClient([make_response({"replacements": [proposal]})])

    result = web_rewrite.run_web_rewrite(
        run_id=_RUN_ID,
        case_id=_CASE_ID,
        master_resume=custom_resume,
        job_description=_PASSING_JD,
        host=AnthropicHost(client=client),
        services=services,
    )

    assert result["terminal_class"] == "REJECTED:NO_SAFE_CHANGES"
    assert result["writer_stats"]["rejection_codes"] == {
        "INVALID_ANCHOR": 1
    }
    assert len(client.calls) == 1
    assert services.published == []

@pytest.mark.parametrize("replacement", ["• -", "• ---", "• ...", "• !!!"])
def test_bullet_replacement_must_retain_substantive_body(replacement):
    proposal = {
        "source_span_text": _SAFE_SOURCE,
        "replacement_text": replacement,
    }
    services = Services()

    result, client = run(
        [make_response({"replacements": [proposal]})], services
    )

    assert result["terminal_class"] == "REJECTED:NO_SAFE_CHANGES"
    assert result["writer_stats"]["rejection_codes"] == {
        "STRICT_COMPILER": 1
    }
    assert len(client.calls) == 1
    assert services.published == []

@pytest.mark.parametrize(
    ("source", "replacement"),
    [
        (
            "DIRECTOR, DRUG SAFETY PHYSICIAN | Acme Biotech | Boston, MA",
            "DIRECTOR, DRUG SAFETY PHYSICIAN | Acme Biotech | Boston, MA",
        ),
        ("Jan 2018 - Present", "Jan 2018 – Present"),
        ("Doctor of Medicine (M.D.)", "Doctor of Medicine (M.D．)"),
    ],
)
def test_canonical_identity_lines_are_frozen_before_semantic_review(
    source, replacement
):
    proposal = {
        "source_span_text": source,
        "replacement_text": replacement,
    }
    services = Services()

    result, client = run(
        [make_response({"replacements": [proposal]})], services
    )

    assert result["terminal_class"] == "REJECTED:NO_SAFE_CHANGES"
    assert result["writer_stats"]["rejection_codes"] == {"INVALID_ANCHOR": 1}
    assert len(client.calls) == 1
    assert services.published == []

def test_writer_with_no_safe_proposal_rejects_with_count_only_diagnostics():
    unsafe = {
        "source_span_text": (
            "DIRECTOR, DRUG SAFETY PHYSICIAN | Acme Biotech | Boston, MA"
        ),
        "replacement_text": "Vice President | Acme Biotech | Boston, MA",
    }
    services = Services()

    result, client = run(
        [make_response({"replacements": [unsafe]})], services
    )

    assert result["terminal_class"] == "REJECTED:NO_SAFE_CHANGES"
    assert result["writer_stats"] == {
        "proposed_count": 1,
        "accepted_count": 0,
        "rejected_count": 1,
        "rejection_codes": {"INVALID_ANCHOR": 1},
    }
    serialized = json.dumps(result["writer_stats"])
    assert _TRACED_RESUME not in serialized
    assert _PASSING_JD not in serialized
    assert _SAFE_SOURCE not in serialized
    assert services.published == []
    assert len(client.calls) == 1


def test_malformed_writer_json_uses_only_host_syntax_repair():
    result, client = run(
        [make_response("not json"), make_response({"replacements": []})]
    )

    assert result["terminal_class"] == "PUBLISHED"
    assert len(client.calls) == 2
    repair_messages = client.calls[1]["messages"]
    assert any(
        "previous reply could not be parsed as JSON" in row["content"]
        for row in repair_messages
    )
    assert all(
        "REJECTED by the coordinator" not in row["content"]
        for row in repair_messages
    )


def test_tampered_publication_readback_fails_closed():
    class TamperedReadbackServices(Services):
        def read_publication(self, publication_id):
            stored = super().read_publication(publication_id)
            return {**stored, "draft": stored["draft"] + "\nTAMPERED"}

    services = TamperedReadbackServices()
    result, client = run([make_response({"replacements": []})], services)

    assert result["terminal_class"] == "FAILED:PUBLICATION_VERIFICATION"
    assert result["published"] is False
    assert "final_draft" not in result
    assert len(client.calls) == 1



def test_type_tampered_semantic_readback_fails_closed():
    paraphrase = {
        "source_span_text": _SAFE_SOURCE,
        "replacement_text": "• Reviewed oncology safety reports under ICH guidance.",
    }

    class BooleanTamperedReadbackServices(Services):
        def read_publication(self, publication_id):
            stored = super().read_publication(publication_id)
            stored["metadata"]["semantic_review"]["attestations"][0][
                "decisions"
            ][0]["supported"] = 1
            return stored

    services = BooleanTamperedReadbackServices()
    result, client = run(
        [
            make_response({"replacements": [paraphrase]}),
            make_semantic_response([paraphrase], [True]),
            make_semantic_response([paraphrase], [True]),
        ],
        services,
    )

    assert result["terminal_class"] == "FAILED:PUBLICATION_VERIFICATION"
    assert result["published"] is False
    assert len(client.calls) == 3

def test_invalid_writer_shape_fails_without_publication():
    services = Services()
    result, client = run([make_response({"draft": _TRACED_RESUME})], services)

    assert result["terminal_class"] == "FAILED:WRITER_SCHEMA"
    assert services.published == []
    assert len(client.calls) == 1


def test_publication_receipt_binds_candidate_fit_authorization_and_votes():
    services = Services()
    result, _ = run([make_response({"replacements": []})], services)

    receipt = result["authorization_receipt"]
    metadata = services.published[0][1]
    assert receipt["candidate_fit_report_digest"] == canonical_digest(
        receipt["candidate_fit_report"]
    )
    assert receipt["authorization_digest"] == canonical_digest(
        receipt["authorization_report"]
    )
    assert receipt["vote_invocation_ids"] == [
        vote["invocation_id"] for vote in receipt["authorization_report"]["votes"]
    ]
    assert metadata["schema_version"] == web_rewrite.WEB_REWRITE_PUBLICATION_VERSION
    assert result["authorization_receipt_digest"] == canonical_digest(receipt)
