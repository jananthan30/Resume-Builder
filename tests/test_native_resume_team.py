import hashlib
import json
import stat
import sys
from pathlib import Path

import pytest

from candidate_fit_preflight import assess_candidate_fit
from human_voice_audit import audit_text
from multi_agent_team import (
    AUTHORIZATION_VERSION,
    PROTOCOL_VERSION,
    PUBLICATION_VERSION,
    VOTE_VERSION,
    AgentInvocationFailure,
    build_context,
    canonical_digest,
    run_team,
)
from native_resume_team import (
    CODEX_DISABLED_FEATURES,
    LocalTrustedServices,
    NativeRoleAdapter,
    ProcessResult,
    check_host,
    load_master_snapshot,
    main,
)

ROOT = Path(__file__).resolve().parents[1]
PASSING_RESUME = """ALEX DOE, MD
PROFESSIONAL SUMMARY
Drug safety physician with eight years in pharmacovigilance.
CORE COMPETENCIES
• Pharmacovigilance | Drug Safety | ICSR Review | DSUR | PBRER | CIOMS | ICH
PROFESSIONAL EXPERIENCE
DIRECTOR, DRUG SAFETY PHYSICIAN | Acme Biotech | Boston, MA
Jan 2018 - Present
• Led medical review of ICSRs, expectedness and causality assessments, and aggregate safety reports for oncology trials.
• Reviewed DSUR and PBRER reports and assessed safety signals under ICH and CIOMS guidance.
EDUCATION
Doctor of Medicine (M.D.)
Example Medical School
"""

TEST_JOB_DESCRIPTION = """Director, Drug Safety Physician
About the job
Lead safety surveillance for oncology products.
Qualifications
5+ years' experience in Drug Safety/Pharmacovigilance in a biotech company.
Medical Degree (MD) with medical practice experience.
Strong knowledge of ICH and CIOMS guidelines.
Experience reviewing ICSRs and preparing DSUR and PBRER reports.
"""

LOW_FIT_JOB_DESCRIPTION = """Regional Retail Sales Manager
About the job
Lead consumer retail sales operations across regional stores. Build store plans, coach sales teams, oversee merchandising, and improve customer conversion through local promotions and account planning.
Qualifications
Experience leading retail sales teams, store merchandising, customer acquisition, regional account planning, consumer promotions, and sales forecasting. Clear communication and practical team leadership are important for this role.
"""

KNOCKOUT_JOB_DESCRIPTION = """Director, Drug Safety Physician
About the job
Lead safety surveillance for oncology products and provide medical oversight for aggregate reporting and signal management across a global biotech portfolio.
Qualifications
20+ years' experience in Drug Safety/Pharmacovigilance in a biotech company.
Medical Degree (MD) with medical practice experience.
Strong knowledge of ICH and CIOMS guidelines.
Experience reviewing ICSRs and preparing DSUR and PBRER reports.
"""


def _candidate_fit_report(
    master_text,
    job_description,
    run_id,
    case_id,
):
    return assess_candidate_fit(
        master_text,
        job_description,
        run_id=run_id,
        case_id=case_id,
        as_of_date="2026-07-19",
    )


def _passing_candidate_fit_assessor(
    resume_text,
    job_description_text,
    *,
    run_id,
    case_id,
    as_of_date,
    **_kwargs,
):
    return assess_candidate_fit(
        resume_text,
        job_description_text,
        run_id=run_id,
        case_id=case_id,
        as_of_date=as_of_date,
        **_kwargs,
    )


def researcher_context(run_id="run-1"):
    return build_context(
        run_id=run_id,
        case_id="case-1",
        role="researcher",
        attempt=0,
        payload={"job_description": "Role requires offline review."},
    )


def researcher_raw():
    requirement = "Role requires offline review."
    return {
        "rubric": {
            "hard_requirements": [requirement],
            "soft_requirements": [],
        },
        "jd_evidence_spans": [{"evidence_text": requirement}],
    }


def _codex_output_schema_cases():
    """Return model-facing raw payloads and exact contexts for all four roles."""

    researcher = researcher_raw()
    writer = {"draft": PASSING_RESUME, "claim_evidence": []}
    auditor = {
        "verdict": "PASS",
        "findings": [],
        "audited_draft": PASSING_RESUME,
    }
    editor = {
        "draft": PASSING_RESUME,
        "addressed_finding_ids": ["finding-1"],
        "claim_evidence": [],
    }
    return {
        "researcher": (
            researcher_context(run_id="run-schema-researcher"),
            researcher,
        ),
        "writer": (
            build_context(
                run_id="run-schema-writer",
                case_id="case-schema",
                role="writer",
                attempt=0,
                payload={"master_resume": PASSING_RESUME},
            ),
            writer,
        ),
        "auditor": (
            build_context(
                run_id="run-schema-auditor",
                case_id="case-schema",
                role="auditor",
                attempt=0,
                payload={
                    "master_resume": PASSING_RESUME,
                    "writer_draft": PASSING_RESUME,
                },
            ),
            auditor,
        ),
        "editor": (
            build_context(
                run_id="run-schema-editor",
                case_id="case-schema",
                role="editor",
                attempt=1,
                payload={
                    "master_resume": PASSING_RESUME,
                    "audit_findings": [{"id": "finding-1"}],
                },
            ),
            editor,
        ),
    }


@pytest.mark.parametrize("role", ("researcher", "writer", "auditor", "editor"))
def test_codex_adapter_supplies_valid_role_specific_output_schema(role):
    from jsonschema import Draft202012Validator

    cases = _codex_output_schema_cases()
    context, raw_payload = cases[role]
    observed = {}

    def runner(command, *, input_text, timeout, cwd, env):
        assert command.count("--output-schema") == 1
        schema_index = command.index("--output-schema")
        assert schema_index + 1 < len(command)
        schema_argument = Path(command[schema_index + 1])
        schema_path = (
            schema_argument
            if schema_argument.is_absolute()
            else cwd / schema_argument
        )
        assert schema_path.is_file()
        assert not schema_path.is_symlink()
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        Draft202012Validator.check_schema(schema)
        validator = Draft202012Validator(schema)
        validator.validate(raw_payload)
        with_unexpected_key = dict(raw_payload)
        with_unexpected_key["unexpected"] = True
        assert not validator.is_valid(with_unexpected_key)
        for other_role, (_, other_payload) in cases.items():
            if other_role != role:
                assert not validator.is_valid(other_payload), (
                    role,
                    other_role,
                )
        observed.update(
            role=role,
            schema_path=schema_path,
            schema=schema,
            command=command,
        )
        events = [
            {
                "type": "thread.started",
                "thread_id": f"schema-{role}-session",
            },
            {
                "type": "item.completed",
                "item": {
                    "id": f"schema-{role}-item",
                    "type": "agent_message",
                    "text": json.dumps(raw_payload),
                },
            },
            {"type": "turn.completed", "usage": {}},
        ]
        return ProcessResult(
            0,
            "\n".join(json.dumps(event) for event in events),
            "",
            100,
        )

    adapter = NativeRoleAdapter(
        host="codex",
        project_root=ROOT,
        cli_path=sys.executable,
        runner=runner,
    )
    packet = adapter.invoke(role, context, 5)

    assert observed["role"] == role
    assert observed["schema_path"].parent != ROOT
    assert packet["role"] == role
    assert packet["agent_id"] == f"codex:schema-{role}-session"


def test_codex_adapter_isolated_session_and_actual_thread_id(monkeypatch):
    observed = {}

    def runner(command, *, input_text, timeout, cwd, env):
        observed.update(command=command, input=input_text, cwd=cwd, env=env)
        assert cwd != ROOT
        assert cwd.is_dir()
        events = [
            {"type": "thread.started", "thread_id": "codex-session-1"},
            {"type": "turn.started"},
            {
                "type": "item.completed",
                "item": {
                    "id": "item-1",
                    "type": "agent_message",
                    "text": json.dumps(researcher_raw()),
                },
            },
            {"type": "turn.completed", "usage": {}},
        ]
        return ProcessResult(0, "\n".join(json.dumps(row) for row in events), "", 111)

    monkeypatch.setenv("OPENAI_API_KEY", "must-not-leak")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://untrusted.invalid")
    adapter = NativeRoleAdapter(
        host="codex",
        project_root=ROOT,
        cli_path=sys.executable,
        runner=runner,
    )
    packet = adapter.invoke("researcher", researcher_context(), 5)
    assert packet["agent_id"] == "codex:codex-session-1"
    assert packet["role"] == "researcher"
    assert "OPENAI_API_KEY" not in observed["env"]
    assert "OPENAI_BASE_URL" not in observed["env"]
    assert "--ephemeral" in observed["command"]
    assert "--ignore-user-config" in observed["command"]
    assert ["--sandbox", "read-only"] == observed["command"][
        observed["command"].index("--sandbox") : observed["command"].index("--sandbox") + 2
    ]
    for feature in CODEX_DISABLED_FEATURES:
        assert feature in observed["command"]
    assert "--model" not in observed["command"]
    assert all("model_reasoning_effort" not in value for value in observed["command"])


def test_codex_adapter_rejects_any_non_message_item_including_file_change():
    def runner(command, *, input_text, timeout, cwd, env):
        events = [
            {"type": "thread.started", "thread_id": "codex-session-2"},
            {
                "type": "item.completed",
                "item": {"id": "item-1", "type": "file_change"},
            },
            {
                "type": "item.completed",
                "item": {
                    "id": "item-2",
                    "type": "agent_message",
                    "text": json.dumps(researcher_raw()),
                },
            },
            {"type": "turn.completed", "usage": {}},
        ]
        return ProcessResult(0, "\n".join(json.dumps(row) for row in events), "", 112)

    adapter = NativeRoleAdapter(
        host="codex", project_root=ROOT, cli_path=sys.executable, runner=runner
    )
    with pytest.raises(LookupError):
        adapter.invoke("researcher", researcher_context(), 5)


def test_codex_adapter_classifies_malformed_event_json():
    def runner(command, *, input_text, timeout, cwd, env):
        return ProcessResult(0, "not-json", "", 113)

    adapter = NativeRoleAdapter(
        host="codex", project_root=ROOT, cli_path=sys.executable, runner=runner
    )
    with pytest.raises(AgentInvocationFailure) as caught:
        adapter.invoke("researcher", researcher_context(), 5)
    assert caught.value.code == "AGENT_EVENT_MALFORMED"


def test_codex_adapter_classifies_malformed_model_json():
    def runner(command, *, input_text, timeout, cwd, env):
        events = [
            {"type": "thread.started", "thread_id": "codex-session-json"},
            {
                "type": "item.completed",
                "item": {
                    "id": "item-json",
                    "type": "agent_message",
                    "text": "not-json",
                },
            },
            {"type": "turn.completed", "usage": {}},
        ]
        return ProcessResult(
            0, "\n".join(json.dumps(row) for row in events), "", 114
        )

    adapter = NativeRoleAdapter(
        host="codex", project_root=ROOT, cli_path=sys.executable, runner=runner
    )
    with pytest.raises(AgentInvocationFailure) as caught:
        adapter.invoke("researcher", researcher_context(), 5)
    assert caught.value.code == "AGENT_OUTPUT_MALFORMED"


def test_codex_adapter_classifies_model_payload_schema():
    def runner(command, *, input_text, timeout, cwd, env):
        events = [
            {"type": "thread.started", "thread_id": "codex-session-schema"},
            {
                "type": "item.completed",
                "item": {
                    "id": "item-schema",
                    "type": "agent_message",
                    "text": "{}",
                },
            },
            {"type": "turn.completed", "usage": {}},
        ]
        return ProcessResult(
            0, "\n".join(json.dumps(row) for row in events), "", 115
        )

    adapter = NativeRoleAdapter(
        host="codex", project_root=ROOT, cli_path=sys.executable, runner=runner
    )
    with pytest.raises(AgentInvocationFailure) as caught:
        adapter.invoke("researcher", researcher_context(), 5)
    assert caught.value.code == "AGENT_PAYLOAD_SCHEMA"


@pytest.mark.parametrize(
    ("result", "exception_type"),
    [
        (ProcessResult(1, "", "unavailable", 116), ConnectionError),
        (ProcessResult(1, "", "timeout", 117, timed_out=True), TimeoutError),
    ],
)
def test_codex_adapter_preserves_nonzero_and_timeout_categories(
    result,
    exception_type,
):
    def runner(command, *, input_text, timeout, cwd, env):
        return result

    adapter = NativeRoleAdapter(
        host="codex", project_root=ROOT, cli_path=sys.executable, runner=runner
    )
    with pytest.raises(exception_type):
        adapter.invoke("researcher", researcher_context(), 5)


def test_claude_adapter_zero_tools_and_actual_session(monkeypatch):
    observed = {}

    def runner(command, *, input_text, timeout, cwd, env):
        observed.update(command=command, cwd=cwd, env=env)
        outer = {
            "type": "result",
            "is_error": False,
            "session_id": "claude-session-1",
            "permission_denials": [],
            "num_turns": 1,
            "usage": {"server_tool_use": {"web_search_requests": 0}},
            "result": "",
            "structured_output": researcher_raw(),
        }
        return ProcessResult(0, json.dumps(outer), "", 211)

    monkeypatch.setenv("ANTHROPIC_API_KEY", "must-not-leak")
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://untrusted.invalid")
    monkeypatch.setenv("ANTHROPIC_MODEL", "untrusted-model")
    monkeypatch.setenv("AWS_BEARER_TOKEN_BEDROCK", "must-not-leak")
    adapter = NativeRoleAdapter(
        host="claude",
        project_root=ROOT,
        cli_path=sys.executable,
        runner=runner,
    )
    packet = adapter.invoke("researcher", researcher_context(), 5)
    assert packet["agent_id"] == "claude:claude-session-1"
    assert "ANTHROPIC_API_KEY" not in observed["env"]
    assert "ANTHROPIC_BASE_URL" not in observed["env"]
    assert "ANTHROPIC_MODEL" not in observed["env"]
    assert "AWS_BEARER_TOKEN_BEDROCK" not in observed["env"]
    tools_index = observed["command"].index("--tools")
    assert observed["command"][tools_index + 1] == ""
    schema_index = observed["command"].index("--json-schema")
    schema = json.loads(observed["command"][schema_index + 1])
    from jsonschema import Draft202012Validator

    Draft202012Validator.check_schema(schema)
    Draft202012Validator(schema).validate(researcher_raw())
    assert "--safe-mode" in observed["command"]
    assert "--agents" in observed["command"]
    assert observed["cwd"] != ROOT
    assert "--model" not in observed["command"]
    assert "--effort" not in observed["command"]
    with pytest.raises(ValueError):
        NativeRoleAdapter(
            host="claude",
            project_root=ROOT,
            cli_path=sys.executable,
            runner=runner,
            reasoning_effort="ultra",
        )


def _master_fixture(tmp_path):
    text = PASSING_RESUME
    master = tmp_path / "master.md"
    master.write_text(text, encoding="utf-8")
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"master_resume_path": "master.md"}), encoding="utf-8")
    return config, load_master_snapshot(config)


def test_master_snapshot_rejects_decorated_role_date_before_agents(tmp_path):
    malformed = PASSING_RESUME.replace(
        "Jan 2018 - Present",
        "Jan 2018 - Present | Non-continuous roles; exact dates below",
    )
    master = tmp_path / "malformed-master.md"
    master.write_text(malformed, encoding="utf-8")
    config = tmp_path / "config.json"
    config.write_text(
        json.dumps({"master_resume_path": master.name}),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="canonical layout"):
        load_master_snapshot(config)


def _publication_metadata(run_id, case_id, draft, report=None):
    draft_digest = canonical_digest(draft)
    if report is None:
        votes = [
            {
                "schema_version": VOTE_VERSION,
                "name": name,
                "invocation_id": f"vote-{index}",
                "passed": True,
                "draft_digest": draft_digest,
                "codes": [],
            }
            for index, name in enumerate(
                ("evidence", "human_voice", "canonical_integrity"), start=1
            )
        ]
        report = {
            "schema_version": AUTHORIZATION_VERSION,
            "draft_digest": draft_digest,
            "passed": True,
            "codes": [],
            "votes": votes,
            "findings": [],
        }
    fit_report = _candidate_fit_report(
        draft, TEST_JOB_DESCRIPTION, run_id, case_id
    )
    return {
        "schema_version": AUTHORIZATION_VERSION,
        "run_id": run_id,
        "case_id": case_id,
        "draft_digest": draft_digest,
        "source_digest": canonical_digest(draft),
        "job_description_digest": canonical_digest(TEST_JOB_DESCRIPTION),
        "candidate_fit_report": fit_report,
        "candidate_fit_report_digest": canonical_digest(fit_report),
        "researcher_agent_id": "codex:researcher-test",
        "researcher_artifact_digest": "b" * 64,
        "auditor_attestation": {
            "agent_id": "codex:auditor-test",
            "artifact_digest": "a" * 64,
            "verdict": "PASS",
            "draft_digest": draft_digest,
        },
        "authorization_digest": canonical_digest(report),
        "authorization_report": report,
        "vote_invocation_ids": [vote["invocation_id"] for vote in report["votes"]],
    }


def test_local_services_three_votes_replay_and_atomic_publication(tmp_path):
    config, master = _master_fixture(tmp_path)
    output = tmp_path / "output"
    services = LocalTrustedServices(
        project_root=ROOT,
        config_path=config,
        master=master,
        output_dir=output,
        state_dir=tmp_path / "state",
        run_id="run-1",
        case_id="case-1",
        job_description=TEST_JOB_DESCRIPTION,
        candidate_fit_report=_candidate_fit_report(
            master.text, TEST_JOB_DESCRIPTION, "run-1", "case-1"
        ),
    )
    services.ensure_job_description(TEST_JOB_DESCRIPTION)
    first = services.claim_run("run-1", "case-1")
    second = services.claim_run("run-1", "case-1")
    assert first["claimed"] is True
    assert second["claimed"] is False
    assert services.attest_source(master.text)["trusted"] is True

    report = services.audit_draft(master.text)
    assert report["passed"] is True
    assert report["findings"] == []
    assert [vote["name"] for vote in report["votes"]] == [
        "evidence",
        "human_voice",
        "canonical_integrity",
    ]
    assert len({vote["invocation_id"] for vote in report["votes"]}) == 3
    assert all(vote["draft_digest"] == canonical_digest(master.text) for vote in report["votes"])

    metadata = _publication_metadata("run-1", "case-1", master.text, report)
    receipt = services.publish(master.text, metadata)
    assert receipt["schema_version"] == PUBLICATION_VERSION
    assert (output / "resume.md").read_text(encoding="utf-8") == master.text
    verification = services.verify_publication(receipt["publication_id"])
    assert verification["verified"] is True
    durable_path = output / verification["authorization_receipt_path"]
    assert durable_path.is_file() and not durable_path.is_symlink()
    assert stat.S_IMODE(durable_path.stat().st_mode) == 0o600
    durable = json.loads(durable_path.read_text(encoding="utf-8"))
    assert durable["publication_id"] == receipt["publication_id"]
    assert durable["source_digest"] == master.text_digest
    assert durable["job_description_digest"] == canonical_digest(
        TEST_JOB_DESCRIPTION
    )
    assert durable["candidate_fit_report"] == metadata["candidate_fit_report"]
    assert durable["candidate_fit_report_digest"] == canonical_digest(
        durable["candidate_fit_report"]
    )
    assert durable["researcher_agent_id"] == "codex:researcher-test"
    assert durable["researcher_artifact_digest"] == "b" * 64
    assert durable["vote_invocation_ids"] == metadata["vote_invocation_ids"]
    assert durable["authorization_report"] == report
    assert durable["auditor_attestation"]["verdict"] == "PASS"
    assert durable["auditor_attestation"]["draft_digest"] == canonical_digest(
        master.text
    )
    assert durable["authorization_digest"] == canonical_digest(report)
    assert durable["vote_invocation_ids"] == [
        vote["invocation_id"] for vote in durable["authorization_report"]["votes"]
    ]
    assert durable == verification["authorization_receipt"]
    assert canonical_digest(durable) == verification["authorization_receipt_digest"]
    assert master.text not in durable_path.read_text(encoding="utf-8")
    assert TEST_JOB_DESCRIPTION not in durable_path.read_text(encoding="utf-8")

    bad_metadata = json.loads(json.dumps(metadata))
    bad_metadata["authorization_report"]["votes"][0]["passed"] = False
    bad_metadata["authorization_digest"] = canonical_digest(
        bad_metadata["authorization_report"]
    )
    with pytest.raises(ValueError):
        services.publish(master.text, bad_metadata)


def test_local_services_recomputes_forged_candidate_fit_before_construction(
    tmp_path,
):
    config, master = _master_fixture(tmp_path)
    forged_report = _candidate_fit_report(
        master.text,
        KNOCKOUT_JOB_DESCRIPTION,
        "run-forged-fit",
        "case-forged-fit",
    )
    assert forged_report["passed"] is False
    assert forged_report["hard_knockouts"]
    forged_report["score"] = 100.0
    forged_report["component_scores"] = {
        key: 100.0 for key in forged_report["component_scores"]
    }
    forged_report["hard_knockouts"] = []
    forged_report["extraction_trustworthy"] = True
    forged_report["passed"] = True
    forged_report["recommendation"] = "PROCEED"
    forged_report["codes"] = []
    output = tmp_path / "must-not-exist"

    with pytest.raises(ValueError, match="invalid trusted candidate-fit"):
        LocalTrustedServices(
            project_root=ROOT,
            config_path=config,
            master=master,
            output_dir=output,
            state_dir=tmp_path / "state",
            run_id="run-forged-fit",
            case_id="case-forged-fit",
            job_description=KNOCKOUT_JOB_DESCRIPTION,
            candidate_fit_report=forged_report,
        )
    assert not output.exists()


def _structured_audit_services(tmp_path, runner):
    config, master = _master_fixture(tmp_path)
    return LocalTrustedServices(
        project_root=ROOT,
        config_path=config,
        master=master,
        output_dir=tmp_path / "output",
        state_dir=tmp_path / "state",
        run_id="run-structured-audit",
        case_id="case-structured-audit",
        job_description=TEST_JOB_DESCRIPTION,
        candidate_fit_report=_candidate_fit_report(
            master.text,
            TEST_JOB_DESCRIPTION,
            "run-structured-audit",
            "case-structured-audit",
        ),
        runner=runner,
    )


def _human_voice_report(path):
    report = audit_text(path.read_text(encoding="utf-8"), mode="resume")
    report["path"] = str(path)
    return report


def test_local_services_binds_unsupported_competency_to_exact_line(tmp_path):
    from evidence_audit import audit_text as audit_evidence_text

    supported_core = (
        "• Pharmacovigilance | Drug Safety | ICSR Review | DSUR | PBRER | "
        "CIOMS | ICH"
    )
    draft = PASSING_RESUME.replace(
        supported_core,
        "• Pharmacovigilance\n• Quantum Portfolio Strategy",
    )
    unsupported_line_number = draft.splitlines().index(
        "• Quantum Portfolio Strategy"
    ) + 1
    next_pid = 650

    def runner(command, *, input_text, timeout, cwd, env):
        nonlocal next_pid
        next_pid += 1
        script = Path(command[1]).name
        candidate = Path(command[2])
        if script == "evidence_audit.py":
            report = audit_evidence_text(candidate.read_text(encoding="utf-8"))
            assert report["unsupported"] == ["Quantum Portfolio Strategy"]
            return ProcessResult(1, "evidence failed", "", next_pid)
        if script == "human_voice_audit.py":
            report = _human_voice_report(candidate)
            return ProcessResult(
                0 if report["passed"] else 1,
                json.dumps(report),
                "",
                next_pid,
            )
        return ProcessResult(
            0,
            json.dumps({"passed": True, "difference_codes": []}),
            "",
            next_pid,
        )

    services = _structured_audit_services(tmp_path, runner)
    report = services.audit_draft(draft)

    assert report["passed"] is False
    assert report["codes"] == ["EVIDENCE_AUDIT_FAILED"]
    assert report["findings"] == [
        {
            "id": "D-EV-01",
            "vote_name": "evidence",
            "code": "EVIDENCE_AUDIT_FAILED",
            "actionable": True,
            "locations": [
                {
                    "line_number": unsupported_line_number,
                    "source_line": "• Quantum Portfolio Strategy",
                    "line_digest": hashlib.sha256(
                        "• Quantum Portfolio Strategy".encode("utf-8")
                    ).hexdigest(),
                }
            ],
        }
    ]


def test_local_services_preserves_line_bound_human_voice_findings(tmp_path):
    draft = """PROFESSIONAL SUMMARY
Clinical and scientific safety physician.

PROFESSIONAL EXPERIENCE
- Reviewed safety cases.
"""
    calls = []
    next_pid = 700

    def runner(command, *, input_text, timeout, cwd, env):
        nonlocal next_pid
        next_pid += 1
        calls.append(command)
        script = Path(command[1]).name
        if script == "human_voice_audit.py":
            candidate = Path(command[2])
            report = _human_voice_report(candidate)
            return ProcessResult(
                1,
                json.dumps(report),
                "",
                next_pid,
            )
        if script == "resume_integrity_audit.py":
            return ProcessResult(
                0,
                json.dumps({"passed": True, "difference_codes": []}),
                "",
                next_pid,
            )
        return ProcessResult(0, "evidence passed", "", next_pid)

    services = _structured_audit_services(tmp_path, runner)
    first = services.audit_draft(draft)
    second = services.audit_draft(draft)

    expected_location = {
        "line_number": 2,
        "source_line": draft.splitlines()[1],
        "line_digest": hashlib.sha256(
            draft.splitlines()[1].encode("utf-8")
        ).hexdigest(),
    }
    assert first["passed"] is False
    assert first["codes"] == ["synonym_padding"]
    assert first["findings"] == [
        {
            "id": "D-HV-01",
            "vote_name": "human_voice",
            "code": "synonym_padding",
            "actionable": True,
            "locations": [expected_location],
        }
    ]
    assert second["findings"] == first["findings"]
    assert second["codes"] == first["codes"]
    assert [vote["invocation_id"] for vote in second["votes"]] != [
        vote["invocation_id"] for vote in first["votes"]
    ]
    human_commands = [
        command for command in calls if Path(command[1]).name == "human_voice_audit.py"
    ]
    assert human_commands
    assert all("--json" in command for command in human_commands)


@pytest.mark.parametrize(
    ("returncode", "stdout", "timed_out", "expected_code"),
    [
        (1, "not-json", False, "HUMAN_VOICE_AUDIT_MALFORMED"),
        (2, "", False, "HUMAN_VOICE_AUDIT_ERROR"),
        (1, "", True, "HUMAN_VOICE_AUDIT_TIMEOUT"),
    ],
)
def test_local_services_distinguishes_human_voice_infrastructure_failures(
    tmp_path,
    returncode,
    stdout,
    timed_out,
    expected_code,
):
    next_pid = 800

    def runner(command, *, input_text, timeout, cwd, env):
        nonlocal next_pid
        next_pid += 1
        script = Path(command[1]).name
        if script == "human_voice_audit.py":
            return ProcessResult(
                returncode,
                stdout,
                "synthetic diagnostic",
                next_pid,
                timed_out=timed_out,
            )
        if script == "resume_integrity_audit.py":
            return ProcessResult(
                0,
                json.dumps({"passed": True, "difference_codes": []}),
                "",
                next_pid,
            )
        return ProcessResult(0, "evidence passed", "", next_pid)

    services = _structured_audit_services(tmp_path, runner)
    report = services.audit_draft("PROFESSIONAL EXPERIENCE\n- Reviewed cases.\n")
    assert report["passed"] is False
    assert report["codes"] == [expected_code]
    assert report["findings"] == [
        {
            "id": "D-HV-01",
            "vote_name": "human_voice",
            "code": expected_code,
            "actionable": False,
            "locations": [],
        }
    ]


def test_local_services_rejects_forged_human_voice_line_binding(tmp_path):
    summary = " ".join(["Clinical"] * 71) + "."
    draft = f"PROFESSIONAL SUMMARY\n{summary}\n"
    next_pid = 900

    def runner(command, *, input_text, timeout, cwd, env):
        nonlocal next_pid
        next_pid += 1
        script = Path(command[1]).name
        if script == "human_voice_audit.py":
            candidate = Path(command[2])
            report = _human_voice_report(candidate)
            failure = next(
                row
                for row in report["failures"]
                if row["code"] == "summary_too_long"
            )
            failure["locations"][0]["source_line"] = "A different line."
            failure["locations"][0]["line_digest"] = hashlib.sha256(
                b"A different line."
            ).hexdigest()
            return ProcessResult(
                1,
                json.dumps(report),
                "",
                next_pid,
            )
        if script == "resume_integrity_audit.py":
            return ProcessResult(
                0,
                json.dumps({"passed": True, "difference_codes": []}),
                "",
                next_pid,
            )
        return ProcessResult(0, "evidence passed", "", next_pid)

    services = _structured_audit_services(tmp_path, runner)
    report = services.audit_draft(draft)
    assert report["codes"] == ["HUMAN_VOICE_AUDIT_MALFORMED"]
    assert report["findings"][0]["actionable"] is False
    assert report["findings"][0]["locations"] == []


def test_local_services_rejects_forged_actionable_aggregate_failure(tmp_path):
    draft = """PROFESSIONAL EXPERIENCE
- Reviewed clinical safety cases.
- Assessed clinical trial data.
- Prepared medical review notes.
"""
    next_pid = 950

    def runner(command, *, input_text, timeout, cwd, env):
        nonlocal next_pid
        next_pid += 1
        script = Path(command[1]).name
        if script == "human_voice_audit.py":
            candidate = Path(command[2])
            report = _human_voice_report(candidate)
            failure = next(
                row
                for row in report["failures"]
                if row["code"] == "low_burstiness"
            )
            source_line = draft.splitlines()[1]
            failure["actionable"] = True
            failure["locations"] = [
                {
                    "line_number": 2,
                    "source_line": source_line,
                    "line_digest": hashlib.sha256(
                        source_line.encode("utf-8")
                    ).hexdigest(),
                }
            ]
            return ProcessResult(1, json.dumps(report), "", next_pid)
        if script == "resume_integrity_audit.py":
            return ProcessResult(
                0,
                json.dumps({"passed": True, "difference_codes": []}),
                "",
                next_pid,
            )
        return ProcessResult(0, "evidence passed", "", next_pid)

    services = _structured_audit_services(tmp_path, runner)
    report = services.audit_draft(draft)
    assert report["codes"] == ["HUMAN_VOICE_AUDIT_MALFORMED"]
    assert report["findings"] == [
        {
            "id": "D-HV-01",
            "vote_name": "human_voice",
            "code": "HUMAN_VOICE_AUDIT_MALFORMED",
            "actionable": False,
            "locations": [],
        }
    ]


def test_local_services_closed_policy_rejects_new_actionable_code(
    tmp_path,
    monkeypatch,
):
    import native_resume_team

    draft = """PROFESSIONAL EXPERIENCE
- Reviewed clinical safety cases.
- Assessed clinical trial data.
- Prepared medical review notes.
"""
    next_pid = 975

    def future_audit(text, mode="resume"):
        report = audit_text(text, mode=mode)
        source_line = text.splitlines()[1]
        report["failures"].append(
            {
                "code": "future_aggregate_metric",
                "message": "A future aggregate policy failed.",
                "actionable": True,
                "locations": [
                    {
                        "line_number": 2,
                        "source_line": source_line,
                        "line_digest": hashlib.sha256(
                            source_line.encode("utf-8")
                        ).hexdigest(),
                    }
                ],
            }
        )
        report["passed"] = False
        return report

    monkeypatch.setattr(
        native_resume_team,
        "_trusted_human_voice_audit",
        future_audit,
    )

    def runner(command, *, input_text, timeout, cwd, env):
        nonlocal next_pid
        next_pid += 1
        script = Path(command[1]).name
        if script == "human_voice_audit.py":
            candidate = Path(command[2])
            report = future_audit(candidate.read_text(encoding="utf-8"))
            report["path"] = str(candidate)
            return ProcessResult(1, json.dumps(report), "", next_pid)
        if script == "resume_integrity_audit.py":
            return ProcessResult(
                0,
                json.dumps({"passed": True, "difference_codes": []}),
                "",
                next_pid,
            )
        return ProcessResult(0, "evidence passed", "", next_pid)

    services = _structured_audit_services(tmp_path, runner)
    report = services.audit_draft(draft)
    assert report["codes"] == ["HUMAN_VOICE_AUDIT_MALFORMED"]
    assert report["findings"][0]["actionable"] is False


def test_publication_directory_sync_failure_rolls_back_linked_resume(
    tmp_path, monkeypatch
):
    import native_resume_team

    config, master = _master_fixture(tmp_path)
    output = tmp_path / "output"
    services = LocalTrustedServices(
        project_root=ROOT,
        config_path=config,
        master=master,
        output_dir=output,
        state_dir=tmp_path / "state",
        run_id="run-sync-failure",
        case_id="case-sync-failure",
        job_description=TEST_JOB_DESCRIPTION,
        candidate_fit_report=_candidate_fit_report(
            master.text,
            TEST_JOB_DESCRIPTION,
            "run-sync-failure",
            "case-sync-failure",
        ),
    )
    services.ensure_job_description(TEST_JOB_DESCRIPTION)
    metadata = _publication_metadata(
        "run-sync-failure", "case-sync-failure", master.text
    )
    real_sync = native_resume_team._fsync_directory
    calls = 0

    def fail_first_sync(path):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OSError("synthetic directory sync failure")
        return real_sync(path)

    monkeypatch.setattr(native_resume_team, "_fsync_directory", fail_first_sync)
    with pytest.raises(OSError, match="synthetic directory sync failure"):
        services.publish(master.text, metadata)
    assert not (output / "resume.md").exists()
    assert not list(output.glob(".resume.md.*.tmp"))


def test_local_services_reject_symlink_state_and_output(tmp_path):
    config, master = _master_fixture(tmp_path)
    real_state = tmp_path / "real-state"
    real_state.mkdir()
    state_link = tmp_path / "state-link"
    state_link.symlink_to(real_state, target_is_directory=True)
    with pytest.raises(ValueError):
        LocalTrustedServices(
            project_root=ROOT,
            config_path=config,
            master=master,
            output_dir=tmp_path / "output",
            state_dir=state_link,
            run_id="run-1",
            case_id="case-1",
            job_description=TEST_JOB_DESCRIPTION,
            candidate_fit_report=_candidate_fit_report(
                master.text, TEST_JOB_DESCRIPTION, "run-1", "case-1"
            ),
        )


def test_job_description_binding_is_atomic_and_rolls_back_only_owned_file(tmp_path):
    config, master = _master_fixture(tmp_path)
    created_output = tmp_path / "created-output"
    created = LocalTrustedServices(
        project_root=ROOT,
        config_path=config,
        master=master,
        output_dir=created_output,
        state_dir=tmp_path / "created-state",
        run_id="run-created-jd",
        case_id="case-created-jd",
        job_description=TEST_JOB_DESCRIPTION,
        candidate_fit_report=_candidate_fit_report(
            master.text,
            TEST_JOB_DESCRIPTION,
            "run-created-jd",
            "case-created-jd",
        ),
    )
    created_path = created.ensure_job_description(TEST_JOB_DESCRIPTION)
    assert created_path.read_text(encoding="utf-8") == TEST_JOB_DESCRIPTION
    assert stat.S_IMODE(created_path.stat().st_mode) == 0o600
    assert created.job_description_digest == canonical_digest(TEST_JOB_DESCRIPTION)
    created.rollback_unpublished_job_description()
    assert not created_path.exists()

    existing_output = tmp_path / "existing-output"
    existing_output.mkdir()
    existing_path = existing_output / "job_description.txt"
    existing_path.write_text(TEST_JOB_DESCRIPTION, encoding="utf-8")
    existing = LocalTrustedServices(
        project_root=ROOT,
        config_path=config,
        master=master,
        output_dir=existing_output,
        state_dir=tmp_path / "existing-state",
        run_id="run-existing-jd",
        case_id="case-existing-jd",
        job_description=TEST_JOB_DESCRIPTION,
        candidate_fit_report=_candidate_fit_report(
            master.text,
            TEST_JOB_DESCRIPTION,
            "run-existing-jd",
            "case-existing-jd",
        ),
    )
    existing.ensure_job_description(TEST_JOB_DESCRIPTION)
    existing.rollback_unpublished_job_description()
    assert existing_path.read_text(encoding="utf-8") == TEST_JOB_DESCRIPTION

    with pytest.raises(FileExistsError):
        existing.ensure_job_description("Different request text.")
    assert existing_path.read_text(encoding="utf-8") == TEST_JOB_DESCRIPTION

    symlink_output = tmp_path / "symlink-output"
    symlink_output.mkdir()
    linked_target = tmp_path / "linked-job.txt"
    linked_target.write_text(TEST_JOB_DESCRIPTION, encoding="utf-8")
    (symlink_output / "job_description.txt").symlink_to(linked_target)
    symlinked = LocalTrustedServices(
        project_root=ROOT,
        config_path=config,
        master=master,
        output_dir=symlink_output,
        state_dir=tmp_path / "symlink-state",
        run_id="run-symlink-jd",
        case_id="case-symlink-jd",
        job_description=TEST_JOB_DESCRIPTION,
        candidate_fit_report=_candidate_fit_report(
            master.text,
            TEST_JOB_DESCRIPTION,
            "run-symlink-jd",
            "case-symlink-jd",
        ),
    )
    with pytest.raises(ValueError):
        symlinked.ensure_job_description(TEST_JOB_DESCRIPTION)
    assert linked_target.read_text(encoding="utf-8") == TEST_JOB_DESCRIPTION


def test_failed_publication_readback_rolls_back_new_target(tmp_path):
    config, master = _master_fixture(tmp_path)
    output = tmp_path / "output"
    services = LocalTrustedServices(
        project_root=ROOT,
        config_path=config,
        master=master,
        output_dir=output,
        state_dir=tmp_path / "state",
        run_id="run-rollback",
        case_id="case-rollback",
        job_description=TEST_JOB_DESCRIPTION,
        candidate_fit_report=_candidate_fit_report(
            master.text,
            TEST_JOB_DESCRIPTION,
            "run-rollback",
            "case-rollback",
        ),
    )
    services.ensure_job_description(TEST_JOB_DESCRIPTION)
    metadata = _publication_metadata(
        "run-rollback", "case-rollback", master.text
    )
    receipt = services.publish(master.text, metadata)
    (output / "resume.md").write_text("corrupt", encoding="utf-8")
    verification = services.verify_publication(receipt["publication_id"])
    assert verification["verified"] is False
    assert not (output / "resume.md").exists()

    real_output = tmp_path / "real-output"
    real_output.mkdir()
    output_link = tmp_path / "output-link"
    output_link.symlink_to(real_output, target_is_directory=True)
    with pytest.raises(ValueError):
        LocalTrustedServices(
            project_root=ROOT,
            config_path=config,
            master=master,
            output_dir=output_link,
            state_dir=tmp_path / "other-state",
            run_id="run-2",
            case_id="case-2",
            job_description=TEST_JOB_DESCRIPTION,
            candidate_fit_report=_candidate_fit_report(
                master.text, TEST_JOB_DESCRIPTION, "run-2", "case-2"
            ),
        )


def test_preexisting_receipt_is_not_clobbered_and_rolls_back_resume(tmp_path):
    config, master = _master_fixture(tmp_path)
    output = tmp_path / "output"
    services = LocalTrustedServices(
        project_root=ROOT,
        config_path=config,
        master=master,
        output_dir=output,
        state_dir=tmp_path / "state",
        run_id="run-receipt",
        case_id="case-receipt",
        job_description=TEST_JOB_DESCRIPTION,
        candidate_fit_report=_candidate_fit_report(
            master.text,
            TEST_JOB_DESCRIPTION,
            "run-receipt",
            "case-receipt",
        ),
    )
    services.ensure_job_description(TEST_JOB_DESCRIPTION)
    metadata = _publication_metadata(
        "run-receipt", "case-receipt", master.text
    )
    publication = services.publish(master.text, metadata)
    pair_digest = canonical_digest(
        {"run_id": "run-receipt", "case_id": "case-receipt"}
    )
    receipt_path = output / f"resume-team-receipt.{pair_digest}.json"
    receipt_path.write_text("sentinel", encoding="utf-8")
    with pytest.raises(FileExistsError):
        services.verify_publication(publication["publication_id"])
    assert receipt_path.read_text(encoding="utf-8") == "sentinel"
    assert not (output / "resume.md").exists()


def test_main_does_not_resolve_away_output_symlink(
    tmp_path, capsys, monkeypatch
):
    import native_resume_team

    monkeypatch.setattr(
        native_resume_team,
        "_trusted_candidate_fit_assessment",
        _passing_candidate_fit_assessor,
    )
    config, _ = _master_fixture(tmp_path)
    real_output = tmp_path / "real-output"
    real_output.mkdir()
    output_link = tmp_path / "output-link"
    output_link.symlink_to(real_output, target_is_directory=True)
    exit_code = main(
        [
            "--host",
            "codex",
            "--config",
            str(config),
            "--job-description",
            TEST_JOB_DESCRIPTION,
            "--output-dir",
            str(output_link),
            "--cli-path",
            sys.executable,
            "--run-id",
            "run-main-symlink",
            "--case-id",
            "case-main-symlink",
        ]
    )
    result = json.loads(capsys.readouterr().out)
    assert exit_code == 1
    assert result["terminal_class"] == "FAILED:RUNTIME_INPUT"
    assert list(real_output.iterdir()) == []


def test_main_rolls_back_only_new_job_description_after_agent_failure(
    tmp_path, capsys, monkeypatch
):
    import native_resume_team

    monkeypatch.setattr(
        native_resume_team,
        "_trusted_candidate_fit_assessment",
        _passing_candidate_fit_assessor,
    )
    config, _ = _master_fixture(tmp_path)
    output = tmp_path / "output"
    exit_code = main(
        [
            "--host",
            "codex",
            "--config",
            str(config),
            "--job-description",
            TEST_JOB_DESCRIPTION,
            "--output-dir",
            str(output),
            "--cli-path",
            sys.executable,
            "--run-id",
            "run-main-jd-rollback-v1",
            "--case-id",
            "case-main-jd-rollback-v1",
        ]
    )
    result = json.loads(capsys.readouterr().out)
    assert exit_code == 1
    assert result["published"] is False
    assert not (output / "job_description.txt").exists()


@pytest.mark.parametrize("mode", ["low-score", "hard-knockout"])
def test_native_candidate_fit_rejection_precedes_all_output_state_and_adapter_work(
    tmp_path, capsys, monkeypatch, mode
):
    import native_resume_team

    config, _ = _master_fixture(tmp_path)
    output = tmp_path / "must-not-exist"
    constructions = []

    def forbidden_construction(*_args, **_kwargs):
        constructions.append(True)
        raise AssertionError("post-fit runtime boundary must not be constructed")

    monkeypatch.setattr(native_resume_team, "NativeRoleAdapter", forbidden_construction)
    monkeypatch.setattr(native_resume_team, "LocalTrustedServices", forbidden_construction)

    rejecting_job = (
        KNOCKOUT_JOB_DESCRIPTION
        if mode == "hard-knockout"
        else LOW_FIT_JOB_DESCRIPTION
    )

    exit_code = main(
        [
            "--host",
            "codex",
            "--config",
            str(config),
            "--job-description",
            rejecting_job,
            "--output-dir",
            str(output),
            "--cli-path",
            sys.executable,
            "--run-id",
            f"run-native-{mode}",
            "--case-id",
            f"case-native-{mode}",
            "--as-of-date",
            "2026-07-19",
        ]
    )
    result = json.loads(capsys.readouterr().out)
    assert exit_code == 1
    assert result["terminal_class"] == "REJECTED:CANDIDATE_FIT"
    assert result["candidate_fit_report"]["passed"] is False
    assert result["candidate_fit_report_digest"] == canonical_digest(
        result["candidate_fit_report"]
    )
    assert constructions == []
    assert not output.exists()
    assert not (output / "job_description.txt").exists()


@pytest.mark.parametrize("mode", ["exception", "malformed"])
def test_native_candidate_fit_unavailable_or_malformed_fails_before_artifacts(
    tmp_path, capsys, monkeypatch, mode
):
    import native_resume_team

    config, _ = _master_fixture(tmp_path)
    output = tmp_path / "must-not-exist"

    def broken_assessor(*_args, **_kwargs):
        if mode == "exception":
            raise RuntimeError("synthetic scorer failure")
        return {"passed": True}

    monkeypatch.setattr(
        native_resume_team,
        "_trusted_candidate_fit_assessment",
        broken_assessor,
    )
    exit_code = main(
        [
            "--host",
            "codex",
            "--config",
            str(config),
            "--job-description",
            TEST_JOB_DESCRIPTION,
            "--output-dir",
            str(output),
            "--cli-path",
            sys.executable,
            "--run-id",
            f"run-native-{mode}",
            "--case-id",
            f"case-native-{mode}",
            "--as-of-date",
            "2026-07-19",
        ]
    )
    result = json.loads(capsys.readouterr().out)
    assert exit_code == 1
    assert result["terminal_class"] == "FAILED:CANDIDATE_FIT_PREFLIGHT"
    assert result["candidate_fit_report"] is None
    assert result["candidate_fit_report_digest"] == ""
    assert not output.exists()


def test_codex_preflight_is_non_model_and_reports_unknown_effective_model(tmp_path):
    # Hermetic config: the repo's real config.json is operator-local and
    # gitignored, so CI checkouts don't have it — pointing at it made this
    # test pass only on the maintainer's machine.
    config, _snapshot = _master_fixture(tmp_path)
    commands = []

    def runner(command, *, input_text, timeout, cwd, env):
        commands.append(command)
        if command[-2:] == ["features", "list"]:
            stdout = "\n".join(f"{name} stable true" for name in CODEX_DISABLED_FEATURES)
        elif command[-2:] == ["login", "status"]:
            stdout = "Logged in using ChatGPT"
        elif "--ignore-user-config" in command:
            stdout = " ".join(
                (
                    "--ignore-user-config",
                    "--ephemeral",
                    "--ignore-rules",
                    "--sandbox",
                    "--json",
                    "--output-schema",
                )
            )
        else:
            stdout = "codex-cli test"
        return ProcessResult(0, stdout, "", 300 + len(commands))

    report = check_host(
        host="codex",
        project_root=ROOT,
        config_path=config,
        cli_path=sys.executable,
        runner=runner,
    )
    assert report["ready"] is True
    assert report["effective_model_mode"] == "isolated_cli_default_unknown"
    assert report["readiness_scope"] == "static_configuration_no_inference"
    assert report["live_inference_verified"] is False
    assert len(commands) == 4
    exec_commands = [command for command in commands if "exec" in command]
    assert len(exec_commands) == 1
    assert "--help" in exec_commands[0]


@pytest.mark.parametrize("mode", ["missing", "corrupt"])
def test_host_preflight_fails_when_candidate_fit_schema_is_unavailable(
    monkeypatch, mode
):
    import native_resume_team

    original_read = native_resume_team._read_regular_bytes

    def guarded_read(path, maximum):
        if Path(path).name == "candidate-fit-report.schema.json":
            if mode == "missing":
                raise FileNotFoundError(path)
            return b'{"$id":"candidate-fit-report/v1","type":"object"}'
        return original_read(path, maximum)

    def runner(command, *, input_text, timeout, cwd, env):
        if command[-2:] == ["features", "list"]:
            stdout = "\n".join(
                f"{name} stable true" for name in CODEX_DISABLED_FEATURES
            )
        elif command[-2:] == ["login", "status"]:
            stdout = "Logged in using ChatGPT"
        elif "--ignore-user-config" in command:
            stdout = (
                "--ignore-user-config --ephemeral --ignore-rules --sandbox "
                "--json --output-schema"
            )
        else:
            stdout = "codex-cli test"
        return ProcessResult(0, stdout, "", 901)

    monkeypatch.setattr(native_resume_team, "_read_regular_bytes", guarded_read)
    report = check_host(
        host="codex",
        project_root=ROOT,
        config_path=ROOT / "config.json",
        cli_path=sys.executable,
        runner=runner,
    )
    schema_check = next(row for row in report["checks"] if row["name"] == "schemas")
    assert schema_check == {
        "name": "schemas",
        "passed": False,
        "code": "RUNTIME_SCHEMAS_INVALID",
    }
    assert report["ready"] is False


def test_mocked_codex_native_team_end_to_end_publishes_verifiable_receipt(tmp_path):
    from jsonschema import Draft202012Validator

    from final_receipt_verifier import verify_final_receipt

    config, master = _master_fixture(tmp_path)
    calls = []

    def runner(command, *, input_text, timeout, cwd, env):
        context = json.loads(input_text)
        role = context["role"]
        calls.append(role)
        if role == "researcher":
            requirement = next(
                line
                for line in context["payload"]["job_description"].splitlines()
                if line.strip()
            )
            raw = {
                "rubric": {
                    "hard_requirements": [requirement],
                    "soft_requirements": [],
                },
                "jd_evidence_spans": [{"evidence_text": requirement}],
            }
        elif role == "writer":
            raw = {"draft": master.text, "claim_evidence": []}
        elif role == "auditor":
            raw = {
                "verdict": "PASS",
                "findings": [],
                "audited_draft": context["payload"]["writer_draft"],
            }
        else:
            raise AssertionError("clean run must not invoke editor")
        session_id = f"e2e-{role}-{len(calls)}"
        events = [
            {"type": "thread.started", "thread_id": session_id},
            {
                "type": "item.completed",
                "item": {
                    "id": f"item-{len(calls)}",
                    "type": "agent_message",
                    "text": json.dumps(raw),
                },
            },
            {"type": "turn.completed", "usage": {}},
        ]
        return ProcessResult(
            0,
            "\n".join(json.dumps(event) for event in events),
            "",
            500 + len(calls),
        )

    output = tmp_path / "application"
    output.mkdir()
    (output / "job_description.txt").write_text(
        TEST_JOB_DESCRIPTION, encoding="utf-8"
    )
    adapter = NativeRoleAdapter(
        host="codex",
        project_root=ROOT,
        cli_path=sys.executable,
        runner=runner,
    )
    services = LocalTrustedServices(
        project_root=ROOT,
        config_path=config,
        master=master,
        output_dir=output,
        state_dir=tmp_path / "state",
        run_id="run-e2e",
        case_id="case-e2e",
        job_description=TEST_JOB_DESCRIPTION,
        candidate_fit_report=_candidate_fit_report(
            master.text,
            TEST_JOB_DESCRIPTION,
            "run-e2e",
            "case-e2e",
        ),
    )
    services.ensure_job_description(TEST_JOB_DESCRIPTION)
    request = {
        "schema_version": PROTOCOL_VERSION,
        "run_id": "run-e2e",
        "case_id": "case-e2e",
        "job_description": TEST_JOB_DESCRIPTION,
        "master_resume": master.text,
        "output_dir": str(output),
        "max_editor_attempts": 2,
        "role_timeout_seconds": 30,
    }
    result = run_team(request, adapter, services)
    assert calls == ["researcher", "writer", "auditor"]
    assert result["published"] is True
    assert (output / "resume.md").read_text(encoding="utf-8") == master.text

    result_schema = json.loads(
        (ROOT / "schemas" / "resume-team-result.schema.json").read_text(
            encoding="utf-8"
        )
    )
    receipt_schema = json.loads(
        (ROOT / "schemas" / "resume-team-final-receipt.schema.json").read_text(
            encoding="utf-8"
        )
    )
    Draft202012Validator(result_schema).validate(result)
    receipt_path = output / result["authorization_receipt_path"]
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    Draft202012Validator(receipt_schema).validate(receipt)
    verified = verify_final_receipt(
        resume_path=output / "resume.md",
        receipt_path=receipt_path,
        expected_receipt_digest=result["authorization_receipt_digest"],
        config_path=config,
    )
    assert verified == result["authorization_receipt"] == receipt
    assert canonical_digest(receipt) == result["authorization_receipt_digest"]
    assert receipt["authorization_report"]["draft_digest"] == result[
        "final_draft_digest"
    ]
    assert receipt["source_digest"] == master.text_digest
    assert receipt["job_description_digest"] == canonical_digest(
        TEST_JOB_DESCRIPTION
    )
    assert receipt["researcher_agent_id"].startswith("codex:")
    assert receipt["researcher_artifact_digest"]
