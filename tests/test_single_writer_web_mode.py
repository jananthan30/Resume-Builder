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


def test_single_writer_mode_selects_single_adapter_and_disables_editor(monkeypatch):
    """A `/rewrite`-style mode cannot accidentally invoke the four-role host."""
    _install_quota_module(monkeypatch)
    budget = SimpleNamespace(input_tokens=0, output_tokens=0)
    single_adapter = SimpleNamespace(host=SimpleNamespace(budget=budget))
    captured = {}

    monkeypatch.setattr(tools, "_default_single_writer_adapter", lambda: single_adapter)
    monkeypatch.setattr(
        tools,
        "_default_team_adapter",
        lambda: pytest.fail("four-role factory used in single-writer mode"),
    )
    monkeypatch.setattr(tools, "reserve_run_slot", lambda *args, **kwargs: None)
    monkeypatch.setattr(tools, "_finish_agent_run", lambda *args, **kwargs: None)
    monkeypatch.setattr(tools, "_record_event", lambda *args, **kwargs: None)

    def run_team(request, adapter, services):
        captured.update(request)
        assert adapter is single_adapter
        return _failed_pipeline_result()

    monkeypatch.setattr(tools.multi_agent_team, "run_team", run_team)

    result = tools.dispatch(
        "run_resume_team",
        _tool_context(),
        jd_text=_JD,
        resume_text=_RESUME,
        pipeline_mode="single_writer",
    )

    assert result["status"] == "failed"
    assert captured["max_editor_attempts"] == 0


def test_default_resume_tool_keeps_the_four_role_adapter(monkeypatch):
    """Omitting the internal mode remains the established four-role behavior."""
    _install_quota_module(monkeypatch)
    calls = {"team": 0, "single": 0}
    budget = SimpleNamespace(input_tokens=0, output_tokens=0)
    team_adapter = SimpleNamespace(host=SimpleNamespace(budget=budget))

    def team_factory():
        calls["team"] += 1
        return team_adapter

    def single_factory():
        calls["single"] += 1
        return team_adapter

    monkeypatch.setattr(tools, "_default_team_adapter", team_factory)
    monkeypatch.setattr(tools, "_default_single_writer_adapter", single_factory)
    monkeypatch.setattr(tools, "reserve_run_slot", lambda *args, **kwargs: None)
    monkeypatch.setattr(tools, "_finish_agent_run", lambda *args, **kwargs: None)
    monkeypatch.setattr(tools, "_record_event", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        tools.multi_agent_team, "run_team", lambda *args: _failed_pipeline_result()
    )

    tools.dispatch("run_resume_team", _tool_context(), jd_text=_JD, resume_text=_RESUME)

    assert calls == {"team": 1, "single": 0}


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
