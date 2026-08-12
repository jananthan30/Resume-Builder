"""Regression coverage for the web-only single-writer rewrite path.

These tests deliberately exercise the public tool dispatch and synchronous
route functions.  The hosted model and persistent cloud database are replaced
at their outer boundaries so no request leaves the test process.
"""

from __future__ import annotations

import contextlib
import json
import sys
from types import ModuleType, SimpleNamespace

import pytest
from fastapi import HTTPException

import agent.tools as tools
import scorer_server

_JD = "Qualifications\nExperience reviewing safety signals."
_RESUME = "Candidate\nExperience reviewing safety signals."


def _install_quota_module(monkeypatch, check_quota=lambda *_: None):
    """Provide the lazy cloud quota import used by the tool under test."""
    module = ModuleType("cloud.quotas")
    module.check_quota = check_quota
    monkeypatch.setitem(sys.modules, "cloud.quotas", module)


def _tool_context() -> tools.ToolContext:
    return tools.ToolContext(user_id=7, tier="pro", conn=object(), run_id="run-web")


def _failed_pipeline_result():
    return {"terminal_class": "FAILED:WRITER_SCHEMA"}


def test_single_writer_mode_selects_direct_service_not_team_coordinator(monkeypatch):
    """The synchronous mode has no role-envelope coordinator boundary."""
    _install_quota_module(monkeypatch)
    budget = SimpleNamespace(input_tokens=0, output_tokens=0)
    host = SimpleNamespace(budget=budget, run_role=lambda *args, **kwargs: None)
    captured = {}

    monkeypatch.setattr(tools, "_default_writer_host", lambda: host)
    monkeypatch.setattr(
        tools,
        "_default_team_adapter",
        lambda: pytest.fail("four-role factory used in direct web mode"),
    )
    monkeypatch.setattr(tools, "reserve_run_slot", lambda *args, **kwargs: None)
    monkeypatch.setattr(tools, "_finish_agent_run", lambda *args, **kwargs: None)
    monkeypatch.setattr(tools, "_record_event", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        tools.multi_agent_team,
        "run_team",
        lambda *args, **kwargs: pytest.fail("role coordinator used in web mode"),
    )

    def run_web_rewrite(**kwargs):
        captured.update(kwargs)
        return _failed_pipeline_result()

    monkeypatch.setattr(tools.web_rewrite, "run_web_rewrite", run_web_rewrite)

    result = tools.dispatch(
        "run_resume_team",
        _tool_context(),
        jd_text=_JD,
        resume_text=_RESUME,
        pipeline_mode="single_writer",
    )

    assert result["status"] == "failed"
    assert captured["host"] is host
    assert captured["master_resume"] == _RESUME
    assert captured["job_description"] == _JD
    assert set(captured) == {
        "run_id",
        "case_id",
        "master_resume",
        "job_description",
        "host",
        "services",
    }


def test_no_safe_failure_retains_count_only_writer_diagnostics(monkeypatch):
    """A failed compile remains diagnosable without storing applicant text."""
    _install_quota_module(monkeypatch)
    stats = {
        "proposed_count": 2,
        "accepted_count": 0,
        "rejected_count": 2,
        "rejection_codes": {"STRICT_COMPILER": 2},
    }
    host = SimpleNamespace(
        budget=SimpleNamespace(input_tokens=17, output_tokens=9),
        run_role=lambda *args, **kwargs: None,
    )
    events = []

    monkeypatch.setattr(tools, "_default_writer_host", lambda: host)
    monkeypatch.setattr(tools, "reserve_run_slot", lambda *args, **kwargs: None)
    monkeypatch.setattr(tools, "_finish_agent_run", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        tools,
        "_record_event",
        lambda conn, user_id, *, kind, payload: events.append((kind, payload)),
    )
    monkeypatch.setattr(
        tools.web_rewrite,
        "run_web_rewrite",
        lambda **kwargs: {
            "terminal_class": "REJECTED:NO_SAFE_CHANGES",
            "writer_stats": stats,
        },
    )

    result = tools.dispatch(
        "run_resume_team",
        _tool_context(),
        jd_text=_JD,
        resume_text=_RESUME,
        pipeline_mode="single_writer",
    )

    assert result == {
        "run_id": "run-web",
        "status": "failed",
        "error": "REJECTED:NO_SAFE_CHANGES",
        "candidate_fit_report": None,
        "candidate_fit_judge_report": None,
        "writer_stats": stats,
    }
    failed_payload = next(
        payload for kind, payload in events if kind == "tailor_run_failed"
    )
    assert failed_payload["writer_stats"] == stats
    serialized = json.dumps(failed_payload)
    assert _RESUME not in serialized
    assert _JD not in serialized
    assert "source_span_text" not in serialized
    assert "replacement_text" not in serialized


def test_single_writer_public_tool_normalizes_crlf_before_direct_service(monkeypatch):
    """CRLF input reaches direct requirement derivation as normalized text."""
    _install_quota_module(monkeypatch)
    host = SimpleNamespace(
        budget=SimpleNamespace(input_tokens=0, output_tokens=0),
        run_role=lambda *args, **kwargs: None,
    )
    captured = {}
    monkeypatch.setattr(tools, "_default_writer_host", lambda: host)
    monkeypatch.setattr(tools, "reserve_run_slot", lambda *args, **kwargs: None)
    monkeypatch.setattr(tools, "_finish_agent_run", lambda *args, **kwargs: None)
    monkeypatch.setattr(tools, "_record_event", lambda *args, **kwargs: None)

    def run_web_rewrite(**kwargs):
        captured["job_description"] = kwargs["job_description"]
        captured["rubric"] = tools.web_rewrite.derive_requirement_rubric(
            kwargs["job_description"]
        )
        return {"terminal_class": "FAILED:SYNTHETIC"}

    monkeypatch.setattr(tools.web_rewrite, "run_web_rewrite", run_web_rewrite)
    crlf_jd = (
        "Qualifications\r\n"
        "Must hold an MD.\r\n"
        "Review ICSRs.\r\n"
        "Review ICSRs.\r\n"
        "Knowledge of CIOMS.\r\n"
    )

    result = tools.dispatch(
        "run_resume_team",
        _tool_context(),
        jd_text=crlf_jd,
        resume_text=_RESUME,
        pipeline_mode="single_writer",
    )

    assert result["error"] == "FAILED:SYNTHETIC"
    assert "\r" not in captured["job_description"]
    assert captured["rubric"] == {
        "hard_requirements": ["Must hold an MD."],
        "soft_requirements": ["Knowledge of CIOMS."],
    }


def test_default_resume_tool_keeps_the_four_role_adapter(monkeypatch):
    """Omitting the internal mode remains the established four-role behavior."""
    _install_quota_module(monkeypatch)
    calls = {"team": 0, "writer": 0}
    budget = SimpleNamespace(input_tokens=0, output_tokens=0)
    team_adapter = SimpleNamespace(host=SimpleNamespace(budget=budget))

    def team_factory():
        calls["team"] += 1
        return team_adapter

    def writer_factory():
        calls["writer"] += 1
        return SimpleNamespace(budget=budget)

    monkeypatch.setattr(tools, "_default_team_adapter", team_factory)
    monkeypatch.setattr(tools, "_default_writer_host", writer_factory)
    monkeypatch.setattr(tools, "reserve_run_slot", lambda *args, **kwargs: None)
    monkeypatch.setattr(tools, "_finish_agent_run", lambda *args, **kwargs: None)
    monkeypatch.setattr(tools, "_record_event", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        tools.multi_agent_team, "run_team", lambda *args: _failed_pipeline_result()
    )

    tools.dispatch("run_resume_team", _tool_context(), jd_text=_JD, resume_text=_RESUME)

    assert calls == {"team": 1, "writer": 0}


@pytest.mark.parametrize("mode", ["", "single-writer", "unknown", None])
def test_invalid_pipeline_mode_is_rejected_before_quota_or_resume_loading(
    monkeypatch, mode
):
    """Bad internal mode input must cost neither quota nor a run-row mutation."""
    quota_checks = []
    _install_quota_module(monkeypatch, lambda *args: quota_checks.append(args))
    monkeypatch.setattr(
        tools,
        "reserve_run_slot",
        lambda *args, **kwargs: pytest.fail("invalid mode claimed a run slot"),
    )

    with pytest.raises(ValueError, match="pipeline_mode"):
        tools.dispatch("run_resume_team", _tool_context(), jd_text=_JD, pipeline_mode=mode)

    assert quota_checks == []


def test_malformed_truthy_audit_flags_fail_closed(monkeypatch):
    """A string such as ``"false"`` can never be minted into a PASS vote."""
    services = tools.CloudTrustedServices(
        conn=object(),
        user_id=7,
        run_id="run-audit",
        case_id="case-audit",
        master_resume=_RESUME,
    )
    monkeypatch.setattr(tools.evidence_audit, "audit_text", lambda draft: {"passed": "false"})
    monkeypatch.setattr(
        tools.human_voice_audit,
        "audit_text",
        lambda draft, mode: {"passed": "false", "failures": []},
    )
    monkeypatch.setattr(
        tools.resume_integrity_audit,
        "audit_resume_text",
        lambda master, draft: {"passed": "false"},
    )

    report = services.audit_draft(_RESUME)

    assert report["passed"] is False
    assert [vote["passed"] for vote in report["votes"]] == [False, False, False]
    assert report["codes"]


def test_direct_published_without_verified_draft_is_downgraded(monkeypatch):
    """A malformed success can never escape as a successful tool result."""
    _install_quota_module(monkeypatch)
    host = SimpleNamespace(
        budget=SimpleNamespace(input_tokens=0, output_tokens=0),
        run_role=lambda *args, **kwargs: None,
    )
    finished = []
    monkeypatch.setattr(tools, "_default_writer_host", lambda: host)
    monkeypatch.setattr(tools, "reserve_run_slot", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        tools,
        "_finish_agent_run",
        lambda *args, **kwargs: finished.append(kwargs),
    )
    monkeypatch.setattr(tools, "_record_event", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        tools.web_rewrite,
        "run_web_rewrite",
        lambda **kwargs: {
            "terminal_class": "PUBLISHED",
            "published": True,
            "final_draft": "",
        },
    )

    result = tools.dispatch(
        "run_resume_team",
        _tool_context(),
        jd_text=_JD,
        resume_text=_RESUME,
        pipeline_mode="single_writer",
    )

    assert result["status"] == "failed"
    assert result["error"] == "FAILED:PUBLICATION_VERIFICATION"
    assert finished[-1]["status"] == "failed"


def test_terminal_update_failure_never_returns_a_draft(monkeypatch):
    """A verified draft is not returned when durable succeeded state cannot commit."""
    _install_quota_module(monkeypatch)
    host = SimpleNamespace(
        budget=SimpleNamespace(input_tokens=0, output_tokens=0),
        run_role=lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(tools, "_default_writer_host", lambda: host)
    monkeypatch.setattr(tools, "reserve_run_slot", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        tools,
        "_finish_agent_run",
        lambda *args, **kwargs: (_ for _ in ()).throw(LookupError("missing run")),
    )
    monkeypatch.setattr(tools, "_record_event", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        tools.web_rewrite,
        "run_web_rewrite",
        lambda **kwargs: {
            "terminal_class": "PUBLISHED",
            "published": True,
            "final_draft": "Verified draft",
        },
    )

    result = tools.dispatch(
        "run_resume_team",
        _tool_context(),
        jd_text=_JD,
        resume_text=_RESUME,
        pipeline_mode="single_writer",
    )

    assert result["status"] == "failed"
    assert "draft" not in result
    assert result["error"] == "FAILED:PUBLICATION_ATOMICITY"


def _route_setup(monkeypatch, result):
    monkeypatch.setattr(scorer_server, "CLOUD_AVAILABLE", True)
    monkeypatch.setattr(scorer_server, "_agent_slot", contextlib.nullcontext)
    monkeypatch.setattr(scorer_server, "db_get_conn", lambda *_: SimpleNamespace(close=lambda: None))
    monkeypatch.setattr(scorer_server, "_safe_agent_scores", lambda *_: (71, 72))
    monkeypatch.setattr(scorer_server.agent_tools, "dispatch", lambda *args, **kwargs: result)


def test_rewrite_uses_single_writer_and_keeps_success_response_shape(monkeypatch):
    """The synchronous rewrite contract stays stable while selecting its new mode."""
    captured = {}
    _route_setup(monkeypatch, {"status": "succeeded", "draft": "Tailored resume"})

    def dispatch(name, ctx, **kwargs):
        captured.update(kwargs)
        return {"status": "succeeded", "draft": "Tailored resume"}

    monkeypatch.setattr(scorer_server.agent_tools, "dispatch", dispatch)

    response = scorer_server.rewrite_resume_endpoint(
        scorer_server.ScoreRequest(jd_text=_JD, resume_text=_RESUME),
        auth={"user_id": 7, "tier": "pro"},
    )

    assert captured["pipeline_mode"] == "single_writer"
    assert json.loads(response.body) == {
        "rewritten_resume": "Tailored resume",
        "ats_before": 71,
        "ats_after": 71,
        "hr_before": 72,
        "hr_after": 72,
    }


@pytest.mark.parametrize(
    ("terminal", "expected_detail"),
    [
        ("REJECTED:NO_SAFE_CHANGES", "no_safe"),
        ("REJECTED:HUMAN_VOICE_AUDIT_FAILED", "safety"),
    ],
)
def test_rewrite_safety_rejections_are_honest_422s(monkeypatch, terminal, expected_detail):
    """Deterministic rejections tell users what happened, never to retry blindly."""
    _route_setup(monkeypatch, {"status": "failed", "error": terminal})

    with pytest.raises(HTTPException) as error:
        scorer_server.rewrite_resume_endpoint(
            scorer_server.ScoreRequest(jd_text=_JD, resume_text=_RESUME),
            auth={"user_id": 7, "tier": "pro"},
        )

    assert error.value.status_code == 422
    detail = error.value.detail.lower()
    assert "few minutes" not in detail
    assert terminal.lower() not in detail
    if expected_detail == "no_safe":
        assert detail == scorer_server._NO_SAFE_TAILOR_DETAIL.lower()
    else:
        assert "deterministic safety checks" in detail
        assert "resume was left unchanged" in detail


def test_rewrite_no_safe_log_contains_only_admitted_count_diagnostics(
    monkeypatch, caplog
):
    stats = {
        "proposed_count": 2,
        "accepted_count": 0,
        "rejected_count": 2,
        "rejection_codes": {"STRICT_COMPILER": 2},
    }
    _route_setup(
        monkeypatch,
        {
            "run_id": "safe-run-id",
            "status": "failed",
            "error": "REJECTED:NO_SAFE_CHANGES",
            "writer_stats": stats,
        },
    )

    with caplog.at_level("ERROR", logger="scorer.rewrite"):
        with pytest.raises(HTTPException):
            scorer_server.rewrite_resume_endpoint(
                scorer_server.ScoreRequest(jd_text=_JD, resume_text=_RESUME),
                auth={"user_id": 7, "tier": "pro"},
            )

    assert "run=safe-run-id" in caplog.text
    assert '"STRICT_COMPILER":2' in caplog.text
    assert "proposed_count" in caplog.text
    assert _RESUME not in caplog.text
    assert _JD not in caplog.text
    assert "source_span_text" not in caplog.text
    assert "replacement_text" not in caplog.text


def test_friendly_agent_error_keeps_safety_rejections_non_transient():
    """Polling clients receive the same non-internal advice as `/rewrite`."""
    assert scorer_server._friendly_agent_error(
        "tailor", "REJECTED:NO_SAFE_CHANGES"
    ) == scorer_server._NO_SAFE_TAILOR_DETAIL
    generic = scorer_server._friendly_agent_error(
        "tailor", "REJECTED:HUMAN_VOICE_AUDIT_FAILED"
    )
    assert "few minutes" not in generic.lower()
    assert "human_voice" not in generic.lower()
    assert "deterministic safety checks" in generic.lower()


def test_async_tailor_background_omits_internal_pipeline_mode(monkeypatch):
    """The queue-and-poll endpoint continues to use the four-role default."""
    captured = {}
    monkeypatch.setattr(scorer_server, "db_get_conn", lambda *_: SimpleNamespace(close=lambda: None))

    def dispatch(name, ctx, **kwargs):
        captured.update(kwargs)
        return {"status": "failed"}

    monkeypatch.setattr(scorer_server.agent_tools, "dispatch", dispatch)

    scorer_server._run_tailor_in_background("run-async", 7, "pro", _JD, None, _RESUME)

    assert "pipeline_mode" not in captured
