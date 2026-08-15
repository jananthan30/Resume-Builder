# tests/conftest.py
"""Keep the suite hermetic.

Once a real ANTHROPIC_API_KEY is present in the environment, any test that
reaches an unstubbed code path will quietly start calling the live API. That
is bad in three separate ways: it makes results depend on ambient credentials,
it makes the suite slow and flaky, and it costs money on every run.

So real client construction is blocked by default. Tests that genuinely want
the live model opt in with RUN_LIVE_LLM=1, which is the same switch the live
benchmark and the calibration tests already use.
"""
from __future__ import annotations

import os

import pytest


class LiveModelCallBlocked(RuntimeError):
    """A test tried to reach the hosted model without opting in."""


@pytest.fixture(autouse=True)
def _block_live_model_calls(monkeypatch, request):
    """Fail fast instead of silently calling the API.

    The engine treats a client it cannot construct as "LLM unavailable" and
    fails closed, so raising here reproduces exactly the no-credentials
    behaviour a test would previously have got for free.
    """
    if os.getenv("RUN_LIVE_LLM") == "1":
        return
    if request.node.get_closest_marker("live_llm"):
        return

    def _blocked(*args, **kwargs):
        raise LiveModelCallBlocked(
            "the hosted model is not available in tests; inject a stub or "
            "evidence_engine.testing.DeterministicJudge, or set RUN_LIVE_LLM=1"
        )

    # Every constructor that can reach a network endpoint, not just Anthropic.
    # The engine resolves its client through ``build_client``, so blocking only
    # AnthropicClient would let a configured third-party provider make live
    # calls from CI — the exact failure this fixture exists to prevent.
    targets = (
        ("evidence_engine.engine", "AnthropicClient"),
        ("evidence_engine.engine", "build_client"),
        ("evidence_engine.llm", "AnthropicClient"),
        ("evidence_engine.llm", "OpenAICompatibleClient"),
        ("evidence_engine.llm", "build_client"),
    )
    for module, attribute in targets:
        try:
            monkeypatch.setattr(f"{module}.{attribute}", _blocked)
        except (AttributeError, ModuleNotFoundError):
            pass


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "live_llm: test genuinely needs the hosted model (costs money)",
    )
