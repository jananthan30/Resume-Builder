"""Behavioral coverage for the deterministic, single-model-role adapter."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

import agent.adapter as adapter_module
from agent.adapter import SingleWriterTeamAdapter
from agent.host_anthropic import AnthropicHost
from candidate_fit_preflight import assess_candidate_fit
from multi_agent_team import (
    AUTHORIZATION_VERSION,
    FINAL_RECEIPT_VERSION,
    PROTOCOL_VERSION,
    PUBLICATION_VERIFICATION_VERSION,
    PUBLICATION_VERSION,
    RUN_CLAIM_VERSION,
    SOURCE_ATTESTATION_VERSION,
    VOTE_VERSION,
    build_context,
    canonical_digest,
    run_team,
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

_SAFE_SOURCE = "• Reviewed DSUR and PBRER reports and assessed safety signals under ICH and CIOMS guidance."
_SAFE_REWRITE = "• Reviewed DSUR and PBRER reports and assessed safety signals under ICH and CIOMS guidance for oncology programs."


class _Messages:
    def __init__(self, responses):
        self._responses = list(responses)
        self.calls: list[dict] = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return self._responses.pop(0)


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


def request(**updates):
    value = {
        "schema_version": PROTOCOL_VERSION,
        "run_id": "run-single-writer",
        "case_id": "SINGLE-WRITER-1",
        "job_description": _PASSING_JD,
        "master_resume": _TRACED_RESUME,
        "output_dir": "/synthetic/output",
        "max_editor_attempts": 0,
        "role_timeout_seconds": 30.0,
    }
    value.update(updates)
    return value


class Services:
    """Small real-controller service boundary; only the API transport is faked."""

    def __init__(self):
        self.events = []
        self.published = []
        self._publication_digests = {}
        self._publication_metadata = {}

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
        self.published.append((draft, metadata))
        self._publication_digests[publication_id] = digest
        self._publication_metadata[publication_id] = metadata
        return {
            "schema_version": PUBLICATION_VERSION,
            "committed": True,
            "draft_digest": digest,
            "target_digest": digest,
            "publication_id": publication_id,
        }

    def verify_publication(self, publication_id):
        digest = self._publication_digests[publication_id]
        metadata = self._publication_metadata[publication_id]
        receipt = {
            "schema_version": FINAL_RECEIPT_VERSION,
            "run_id": metadata["run_id"],
            "case_id": metadata["case_id"],
            "draft_digest": digest,
            "source_digest": metadata["source_digest"],
            "job_description_digest": metadata["job_description_digest"],
            "candidate_fit_report": metadata["candidate_fit_report"],
            "candidate_fit_report_digest": metadata["candidate_fit_report_digest"],
            "candidate_fit_judge_report": metadata["candidate_fit_judge_report"],
            "candidate_fit_judge_report_digest": metadata["candidate_fit_judge_report_digest"],
            "researcher_agent_id": metadata["researcher_agent_id"],
            "researcher_artifact_digest": metadata["researcher_artifact_digest"],
            "auditor_attestation": metadata["auditor_attestation"],
            "authorization_digest": metadata["authorization_digest"],
            "authorization_report": metadata["authorization_report"],
            "vote_invocation_ids": metadata["vote_invocation_ids"],
            "publication_id": publication_id,
            "verified_target_digest": digest,
        }
        return {
            "schema_version": PUBLICATION_VERIFICATION_VERSION,
            "verified": True,
            "publication_id": publication_id,
            "target_digest": digest,
            "authorization_receipt": receipt,
            "authorization_receipt_digest": canonical_digest(receipt),
            "authorization_receipt_path": "resume-team-receipt." + canonical_digest(publication_id) + ".json",
        }


def _adapter(responses):
    client = FakeClient(responses)
    return SingleWriterTeamAdapter(host=AnthropicHost(client=client)), client


def test_single_writer_adapter_calls_anthropic_only_for_writer_and_publishes():
    client = FakeClient([make_response({"replacements": []})])
    adapter = SingleWriterTeamAdapter(host=AnthropicHost(client=client))

    result = run_team(request(), adapter, Services())

    assert result["terminal_class"] == "PUBLISHED"
    assert [call["model"] for call in client.calls] == ["claude-sonnet-5"]
    assert len(client.calls) == 1
    receipt = result["authorization_receipt"]
    assert receipt["researcher_agent_id"].endswith(".det")
    assert receipt["auditor_attestation"]["agent_id"].endswith(".det")
    assert receipt["researcher_agent_id"].startswith("api:researcher.")
    assert receipt["auditor_attestation"]["agent_id"].startswith("api:auditor.")


def test_deterministic_researcher_keeps_only_unique_complete_jd_lines():
    adapter, client = _adapter([])
    job_description = (
        "Requirements\n"
        "Must hold an MD.\n"
        "Collaborate with safety teams.\n"
        "Collaborate with safety teams.\n"
        "---\n"
        "Experience reviewing ICSRs is required.\n"
    )
    context = build_context(
        run_id="run-research", case_id="case-research", role="researcher", attempt=0,
        payload={"job_description": job_description},
    )

    result = adapter.invoke("researcher", context, timeout_seconds=30)

    assert result["payload"]["rubric"] == {
        "hard_requirements": ["Must hold an MD.", "Experience reviewing ICSRs is required."],
        "soft_requirements": [],
    }
    assert client.calls == []


def test_deterministic_researcher_normalization_failure_is_typed(monkeypatch):
    adapter, client = _adapter([])
    context = build_context(
        run_id="run-research-failure",
        case_id="case-research-failure",
        role="researcher",
        attempt=0,
        payload={"job_description": "Must hold an MD.\nReview ICSRs."},
    )

    def reject_payload(*_args):
        raise ValueError("private candidate content")

    monkeypatch.setattr(adapter_module, "normalize_native_payload", reject_payload)

    with pytest.raises(adapter_module.AgentInvocationFailure) as error:
        adapter.invoke("researcher", context, timeout_seconds=30)

    assert error.value.code == "AGENT_PAYLOAD_SCHEMA"
    assert "private candidate content" not in str(error.value)
    assert error.value.__cause__ is None
    assert client.calls == []


def test_failed_authoritative_vote_cannot_publish_or_trigger_more_model_calls():
    class RecordingSingleWriterAdapter(SingleWriterTeamAdapter):
        def __init__(self, host):
            super().__init__(host)
            self.invoked_roles = []

        def invoke(self, role, context, timeout_seconds):
            self.invoked_roles.append(role)
            return super().invoke(role, context, timeout_seconds)

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

    client = FakeClient([make_response({"replacements": []})])
    adapter = RecordingSingleWriterAdapter(host=AnthropicHost(client=client))
    services = FailingVoteServices()

    result = run_team(request(), adapter, services)

    assert result["published"] is result["success_reported"] is False
    assert services.published == []
    assert adapter.invoked_roles == ["researcher", "writer", "auditor"]
    assert adapter.invoked_roles.count("writer") == 1
    assert "editor" not in adapter.invoked_roles
    assert len(client.calls) == 1


def test_writer_salvages_safe_subset_without_a_second_model_call():
    unsafe = {"source_span_text": "DIRECTOR, DRUG SAFETY PHYSICIAN | Acme Biotech | Boston, MA", "replacement_text": "Vice President | Acme Biotech | Boston, MA"}
    safe = {"source_span_text": _SAFE_SOURCE, "replacement_text": _SAFE_REWRITE}
    adapter, client = _adapter([make_response({"replacements": [unsafe, safe]})])
    services = Services()

    result = run_team(request(), adapter, services)

    assert result["terminal_class"] == "PUBLISHED"
    assert services.published[0][0].count(_SAFE_REWRITE) == 1
    assert len(client.calls) == 1
    assert adapter.writer_stats == {
        "proposed_count": 2,
        "accepted_count": 1,
        "rejected_count": 1,
        "rejection_codes": {"CANONICAL_INTEGRITY": 1},
    }


def test_writer_with_no_safe_proposal_rejects_without_semantic_repair():
    unsafe = {"source_span_text": "DIRECTOR, DRUG SAFETY PHYSICIAN | Acme Biotech | Boston, MA", "replacement_text": "Vice President | Acme Biotech | Boston, MA"}
    adapter, client = _adapter([make_response({"replacements": [unsafe]})])

    result = run_team(request(), adapter, Services())

    assert result["terminal_class"] == "REJECTED:NO_SAFE_CHANGES"
    assert len(client.calls) == 1


def test_editor_invocation_is_denied_before_host_call():
    adapter, client = _adapter([])
    context = build_context(
        run_id="run-editor", case_id="case-editor", role="editor", attempt=1,
        payload={"master_resume": _TRACED_RESUME, "writer_draft": _TRACED_RESUME, "audit_findings": [{"id": "A-1"}]},
    )

    with pytest.raises(PermissionError, match="does not permit Editor"):
        adapter.invoke("editor", context, timeout_seconds=30)

    assert client.calls == []


def test_malformed_writer_json_uses_only_host_syntax_repair():
    adapter, client = _adapter([make_response("not json"), make_response({"replacements": []})])
    context = build_context(
        run_id="run-syntax", case_id="case-syntax", role="writer", attempt=0,
        payload={"master_resume": _TRACED_RESUME, "researcher_artifact": {}},
    )

    handoff = adapter.invoke("writer", context, timeout_seconds=30)

    assert handoff["payload"]["draft"] == _TRACED_RESUME
    assert len(client.calls) == 2
    repair_messages = client.calls[1]["messages"]
    assert any("previous reply could not be parsed as JSON" in row["content"] for row in repair_messages)
    assert all("REJECTED by the coordinator" not in row["content"] for row in repair_messages)


def test_reused_deterministic_invocation_identity_fails_closed():
    adapter, client = _adapter([])
    context = build_context(
        run_id="run-reuse", case_id="case-reuse", role="researcher", attempt=0,
        payload={"job_description": "Must hold an MD."},
    )
    adapter.invoke("researcher", context, timeout_seconds=30)

    with pytest.raises(LookupError, match="reused api invocation identity"):
        adapter.invoke("researcher", context, timeout_seconds=30)

    assert client.calls == []


def test_writer_stats_exposes_only_counts_and_returns_a_copy():
    safe = {"source_span_text": _SAFE_SOURCE, "replacement_text": _SAFE_REWRITE}
    adapter, _ = _adapter([make_response({"replacements": [safe]})])
    context = build_context(
        run_id="run-stats", case_id="case-stats", role="writer", attempt=0,
        payload={"master_resume": _TRACED_RESUME, "researcher_artifact": {}},
    )

    adapter.invoke("writer", context, timeout_seconds=30)
    stats = adapter.writer_stats
    assert stats == {"proposed_count": 1, "accepted_count": 1, "rejected_count": 0, "rejection_codes": {}}
    assert _SAFE_SOURCE not in json.dumps(stats)
    assert _SAFE_REWRITE not in json.dumps(stats)
    stats["accepted_count"] = 99
    assert adapter.writer_stats["accepted_count"] == 1
