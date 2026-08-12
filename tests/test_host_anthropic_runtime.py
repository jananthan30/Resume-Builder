"""Regression tests for production Anthropic role-call behavior."""

from __future__ import annotations

import json
import logging
import sys
from types import SimpleNamespace

import pytest

import candidate_fit_judge
import llm_scorer
from agent.adapter import AnthropicTeamAdapter
from agent.host_anthropic import AnthropicHost, HostRefusal, TokenBudget
from candidate_fit_judge import JudgeUnavailable, judge_candidate_fit
from llm_scorer import generate_cover_letter, score_with_llm
from multi_agent_team import build_context


class _Messages:
    def __init__(self, responses):
        self._responses = list(responses)
        self.calls: list[dict] = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        response = self._responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response


class _Client:
    def __init__(self, responses):
        self.messages = _Messages(responses)


def _text_response(payload: dict) -> SimpleNamespace:
    return SimpleNamespace(
        content=[SimpleNamespace(type="text", text=json.dumps(payload))],
        stop_reason="end_turn",
        usage=SimpleNamespace(input_tokens=100, output_tokens=25),
    )


def _thinking_only_response() -> SimpleNamespace:
    return SimpleNamespace(
        content=[
            SimpleNamespace(type="thinking", thinking="private model reasoning")
        ],
        stop_reason="max_tokens",
        usage=SimpleNamespace(input_tokens=4_000, output_tokens=8_000),
    )


ROLE_CASES = [
        (
            "researcher",
            {"job_description": "A complete job-description line."},
            {
                "rubric": {
                    "hard_requirements": ["A complete job-description line."],
                    "soft_requirements": [],
                },
                "jd_evidence_spans": [
                    {"evidence_text": "A complete job-description line."}
                ],
            },
        ),
        ("writer", {"master_resume": "Resume"}, {"replacements": []}),
        (
            "auditor",
            {"writer_draft": "Resume"},
            {"verdict": "PASS", "findings": [], "audited_draft": "Resume"},
        ),
        (
            "editor",
            {"master_resume": "Resume"},
            {
                "draft": "Resume",
                "addressed_finding_ids": [],
                "claim_evidence": [],
            },
        ),
    ]


@pytest.mark.parametrize(("role", "payload", "reply"), ROLE_CASES)
def test_strict_json_role_calls_disable_adaptive_thinking(role, payload, reply):
    """Removing the explicit flag would let Claude 5 spend all output on thought."""

    client = _Client([_text_response(reply)])
    host = AnthropicHost(client=client)

    host.run_role(role, payload, case_id="CASE", run_id="RUN")

    assert client.messages.calls[0]["thinking"] == {"type": "disabled"}


@pytest.mark.parametrize(("role", "payload", "reply"), ROLE_CASES)
def test_default_resume_team_role_calls_sonnet_5(role, payload, reply):
    """Changing any default route away from Sonnet 5 violates production policy."""

    client = _Client([_text_response(reply)])
    host = AnthropicHost(client=client)

    host.run_role(role, payload, case_id="CASE", run_id="RUN")

    assert client.messages.calls[0]["model"] == "claude-sonnet-5"


def test_host_rejects_non_sonnet_model_override():
    """Accepting a custom map could silently bypass the production policy."""

    client = _Client([_text_response({"replacements": []})])
    with pytest.raises(ValueError, match="Sonnet 5"):
        AnthropicHost(
            model_map={"writer": "legacy-model"},
            client=client,
        )

    assert client.messages.calls == []


def test_resume_team_model_map_cannot_be_mutated_after_construction():
    """A valid host must not become a different-model host after validation."""

    client = _Client([_text_response({"replacements": []})])
    host = AnthropicHost(client=client)

    with pytest.raises(TypeError):
        host.model_map["writer"] = "legacy-model"
    with pytest.raises(AttributeError):
        host.model_map = {"writer": "legacy-model"}

    host._model_map = {"writer": "legacy-model"}
    with pytest.raises(HostRefusal, match="model policy"):
        host.run_role(
            "writer", {"master_resume": "Resume"}, case_id="CASE", run_id="RUN"
        )

    assert client.messages.calls == []


def test_resume_team_transport_rejects_non_sonnet_model():
    """The final SDK boundary must enforce policy even when called directly."""

    client = _Client([_text_response({"replacements": []})])
    host = AnthropicHost(client=client)

    with pytest.raises(HostRefusal, match="model policy"):
        host._call_once(
            model="legacy-model", system="System", messages=[{"role": "user", "content": "x"}]
        )

    assert client.messages.calls == []


def test_candidate_fit_judge_rejects_non_sonnet_override():
    """A model argument must not bypass the hosted Sonnet 5 policy."""

    with pytest.raises(JudgeUnavailable, match="Sonnet 5"):
        judge_candidate_fit(
            "Resume evidence line",
            "Job requirement line",
            run_id="RUN",
            case_id="CASE",
            as_of_date="2026-08-11",
            model="legacy-model",
            llm_call=lambda _system, _user, _model: "{}",
        )


def test_candidate_fit_model_constant_cannot_weaken_policy(monkeypatch):
    """Rebinding an exported default must not change the enforced model."""

    monkeypatch.setattr(candidate_fit_judge, "DEFAULT_JUDGE_MODEL", "legacy-model")
    with pytest.raises(JudgeUnavailable, match="Sonnet 5"):
        judge_candidate_fit(
            "Resume evidence line",
            "Job requirement line",
            run_id="RUN",
            case_id="CASE",
            as_of_date="2026-08-11",
            llm_call=lambda _system, _user, _model: "{}",
        )


def test_candidate_fit_transport_rejects_non_sonnet_model():
    """The judge's final SDK boundary must enforce the literal policy."""

    with pytest.raises(JudgeUnavailable, match="Sonnet 5"):
        candidate_fit_judge._default_llm_call(
            "System instructions", "User payload", "legacy-model"
        )


@pytest.mark.parametrize("call", [score_with_llm, generate_cover_letter])
def test_direct_hosted_llm_calls_reject_non_sonnet_override(call):
    """Library callers must not bypass the hosted Sonnet 5 policy."""

    with pytest.raises(ValueError, match="Sonnet 5"):
        call("Resume evidence line", "Job requirement line", model="legacy-model")


def test_direct_hosted_model_constant_cannot_weaken_policy(monkeypatch):
    """Rebinding the public default must not weaken scorer enforcement."""

    monkeypatch.setattr(llm_scorer, "HOSTED_MODEL", "legacy-model")
    with pytest.raises(ValueError, match="Sonnet 5"):
        score_with_llm(
            "Resume evidence line", "Job requirement line", model="legacy-model"
        )


def test_candidate_fit_default_call_disables_adaptive_thinking(monkeypatch):
    """The strict JSON judge must reserve its output budget for visible JSON."""

    client = _Client([_text_response({"verdict": "PASS"})])
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setitem(
        sys.modules, "anthropic", SimpleNamespace(Anthropic=lambda: client)
    )

    candidate_fit_judge._default_llm_call(
        "System instructions", "User payload", "claude-sonnet-5"
    )

    assert client.messages.calls[0]["thinking"] == {"type": "disabled"}
    assert "temperature" not in client.messages.calls[0]


def test_thinking_only_response_accounts_usage_and_reports_safe_metadata():
    """Moving accounting after text extraction would hide a billed failed response."""

    budget = TokenBudget()
    host = AnthropicHost(budget=budget, client=_Client([_thinking_only_response()]))

    with pytest.raises(HostRefusal) as caught:
        host.run_role(
            "writer", {"master_resume": "Resume"}, case_id="CASE", run_id="RUN"
        )

    assert budget.input_tokens == 4_000
    assert budget.output_tokens == 8_000
    message = str(caught.value)
    assert "stop_reason=max_tokens" in message
    assert "content_blocks=thinking" in message
    assert "private model reasoning" not in message


def test_adapter_logs_safe_writer_host_diagnostic(caplog):
    """Dropping the host reason would make the next production failure opaque."""

    host = AnthropicHost(client=_Client([_thinking_only_response()]))
    adapter = AnthropicTeamAdapter(host)
    context = build_context(
        run_id="RUN",
        case_id="CASE",
        role="writer",
        attempt=0,
        payload={"master_resume": "Resume", "researcher_artifact": {}},
    )

    with caplog.at_level(logging.WARNING, logger="agent.adapter"):
        with pytest.raises(ConnectionError):
            adapter.invoke("writer", context, timeout_seconds=120)

    assert "Anthropic role call failed" in caplog.text
    assert "role=writer" in caplog.text
    assert "stop_reason=max_tokens" in caplog.text
    assert "private model reasoning" not in caplog.text


def test_nonretryable_provider_error_is_fail_fast_and_secret_safe(caplog):
    """Logging the SDK exception text could leak credentials or request data."""

    AuthenticationError = type("AuthenticationError", (Exception,), {})
    provider_error = AuthenticationError("credential=sk-ant-private")
    provider_error.status_code = 401
    provider_error.request_id = "req_auth_123"
    client = _Client([provider_error])
    host = AnthropicHost(client=client)
    adapter = AnthropicTeamAdapter(host)
    context = build_context(
        run_id="RUN",
        case_id="CASE",
        role="writer",
        attempt=0,
        payload={"master_resume": "Resume", "researcher_artifact": {}},
    )

    with caplog.at_level(logging.WARNING, logger="agent.adapter"):
        with pytest.raises(ConnectionError) as caught:
            adapter.invoke("writer", context, timeout_seconds=120)

    assert isinstance(caught.value.__cause__, HostRefusal)
    diagnostic = str(caught.value.__cause__)
    assert "error_type=AuthenticationError" in diagnostic
    assert "status=401" in diagnostic
    assert "request_id=req_auth_123" in diagnostic
    assert "attempts=1" in diagnostic
    assert "sk-ant-private" not in diagnostic
    assert "sk-ant-private" not in caplog.text
    assert len(client.messages.calls) == 1
