"""Run-team-compatible adapter around :class:`agent.host_anthropic.AnthropicHost`.

``multi_agent_team.run_team()`` expects an ``adapter`` object exposing one
method -- ``invoke(role, context, timeout_seconds) -> dict`` -- that returns a
*complete, already-validated* role handoff envelope (see
``multi_agent_team.build_handoff`` / ``validate_handoff``). The reference
implementation of that contract is ``NativeRoleAdapter.invoke`` in
``native_resume_team.py`` (CLI hosts: spawn Codex or Claude Code, parse its
stdout into ``(agent_id, raw_payload)``, then hand ``raw_payload`` to
coordinator-owned, host-agnostic functions).

``AnthropicTeamAdapter`` is the hosted-product equivalent of that same split:

1. host-specific plumbing -- call the Anthropic Messages API via
   :meth:`AnthropicHost.run_role` (see ``agent/host_anthropic.py``) instead of
   spawning a CLI process, to get back the model's own untrusted JSON payload
   for the role;
2. coordinator-owned, host-agnostic logic -- feed that payload through
   ``multi_agent_team.normalize_native_payload``, ``build_handoff``, and
   ``validate_handoff`` (imported, not reimplemented) to produce the exact
   envelope shape ``NativeRoleAdapter.invoke`` produces.

Agent identity
--------------
``multi_agent_team._NATIVE_AGENT_ID_RE`` accepts three host prefixes:
``codex``, ``claude`` (legacy local CLI hosts) and ``api`` (this hosted
product runtime). Every call this adapter makes is stamped
``api:<role>.<run_id>.<attempt>`` -- e.g. ``api:researcher.run-42.0``. Each
``(role, attempt)`` pair is unique within one ``run_team()`` call by
construction (researcher/writer are only ever invoked at attempt 0; auditor
at 0..2; editor at 1..2 -- see ``multi_agent_team._valid_role_attempt``), so
this scheme is distinct per call without needing a real invocation identity
(a CLI session/thread id) the way the CLI hosts have. Researcher and auditor
responses share the same ``"api"`` host prefix, satisfying ``run_team``'s
same-host role-separation check.

Failure mapping
----------------
``AnthropicHost.run_role`` raises exactly two typed exceptions when it cannot
produce a usable reply for a role: ``HostRefusal`` (the API call kept failing
past its retry budget, or the model's reply never resolved to valid JSON even
after one repair attempt) and ``BudgetExceeded`` (the shared token budget for
this run is already exhausted). Neither has an exact native-CLI equivalent,
but both mean the same thing from ``run_team``'s point of view: no usable
output arrived for this role call, and retrying within the same run will not
help. This adapter re-raises both as a bare ``ConnectionError`` --
``run_team``'s own dispatcher already treats that exactly like the CLI
adapter's "process exited non-zero" case, i.e. ``FAILED:AGENT_UNAVAILABLE``.
This is a deliberate simplification, not a silent one: a caller that needs to
tell "budget exhausted" apart from "the API is down" (for billing/ops
purposes) must inspect ``error.__cause__`` on that ``ConnectionError``, since
both collapse to the same terminal class today. See the Task 7 report for why
finer-grained mapping -- and real wall-clock enforcement of
``timeout_seconds`` against the Anthropic call -- were left out of this pass.

A malformed *envelope* (well-formed JSON that fails the strict wire-format
checks inside ``normalize_native_payload`` / ``build_handoff`` /
``validate_handoff`` -- e.g. an evidence span that is not a unique, complete
job-description line) becomes
``multi_agent_team.AgentInvocationFailure("AGENT_PAYLOAD_SCHEMA")``, mirroring
``NativeRoleAdapter.invoke``'s own try/except around the identical three
calls. ``run_team`` maps that to ``FAILED:AGENT_PAYLOAD_SCHEMA``.

Import safety
-------------
This module imports ``multi_agent_team`` and ``agent.host_anthropic`` at
module scope -- both plain, cloud-free, vendor-SDK-free modules already
imported the same way elsewhere in this repository (``native_resume_team.py``
imports ``multi_agent_team`` at module scope too). No network client is
constructed anywhere in this file; :class:`AnthropicHost` remains the only
place that ever touches the ``anthropic`` package, and only lazily.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from agent.host_anthropic import AnthropicHost, BudgetExceeded, HostRefusal
from multi_agent_team import (
    AgentInvocationFailure,
    ROLE_ORDER,
    build_handoff,
    normalize_native_payload,
    validate_handoff,
)

__all__ = ["AnthropicTeamAdapter"]

_log = logging.getLogger(__name__)

# Cap on the model's own reply replayed back to it during a repair. Long
# enough to carry a full draft plus its evidence, bounded so one malformed
# reply cannot blow the next request's context.
_MAX_REPLAY_CHARS = 60_000


def _compact_json(payload: Any) -> str | None:
    """Serialize a role's own previous reply for replay, or None if it cannot
    be represented. Best effort: a failure here just costs the repair its
    context, it never fails the run."""
    try:
        text = json.dumps(payload, sort_keys=True, ensure_ascii=True)
    except (TypeError, ValueError):
        return None
    return text if len(text) <= _MAX_REPLAY_CHARS else None


class AnthropicTeamAdapter:
    """Adapts :class:`AnthropicHost` to ``run_team``'s ``adapter`` contract.

    Thin by design: all model-calling behavior (retries, JSON repair, token
    budget, per-role model routing) stays owned by the injected
    :class:`AnthropicHost`; all envelope/provenance/validation behavior stays
    owned by the imported ``multi_agent_team`` helpers. This class only
    supplies the glue -- routing one role's context payload to the host,
    stamping the resulting hosted-product agent identity, and translating the
    host's typed failures into exceptions ``run_team`` already knows how to
    handle fail-closed.
    """

    def __init__(self, host: AnthropicHost) -> None:
        if not isinstance(host, AnthropicHost):
            raise ValueError("host must be an AnthropicHost")
        self.host = host
        self._seen_invocations: set[str] = set()

    def invoke(
        self,
        role: str,
        context: dict[str, Any],
        timeout_seconds: float,
    ) -> dict[str, Any]:
        """Call the Anthropic API for ``role`` and return a validated handoff.

        ``timeout_seconds`` is accepted for interface compatibility with
        ``run_team``'s call site (mirroring ``NativeRoleAdapter.invoke``'s
        signature exactly) but is not enforced against the underlying API
        call in this pass -- see the module docstring's "Failure mapping"
        section.
        """

        if (
            role not in ROLE_ORDER
            or not isinstance(context, dict)
            or context.get("role") != role
        ):
            raise ValueError("invalid role context")
        run_id = context.get("run_id")
        case_id = context.get("case_id")
        attempt = context.get("attempt")
        payload = context.get("payload")
        if not isinstance(run_id, str) or not run_id:
            raise ValueError("invalid role context")
        if not isinstance(case_id, str) or not case_id:
            raise ValueError("invalid role context")
        if type(attempt) is not int:
            raise ValueError("invalid role context")
        if not isinstance(payload, dict):
            raise ValueError("invalid role context")

        def call_host(**extra: Any) -> Any:
            try:
                return self.host.run_role(
                    role, payload, case_id=case_id, run_id=run_id, **extra
                )
            except (HostRefusal, BudgetExceeded) as error:
                raise ConnectionError(
                    f"Anthropic host unavailable for role {role!r}: {error}"
                ) from error

        def build_envelope(candidate: Any, identity: str) -> dict[str, Any]:
            normalized = normalize_native_payload(role, candidate, context)
            envelope = build_handoff(
                context=context, role=role, agent_id=identity, payload=normalized
            )
            if validate_handoff(role, envelope, context).get("valid") is not True:
                raise ValueError("invalid normalized api handoff")
            return envelope

        raw_payload = call_host()

        agent_id = f"api:{role}.{run_id}.{attempt}"
        if agent_id in self._seen_invocations:
            raise LookupError("reused api invocation identity")

        try:
            envelope = build_envelope(raw_payload, agent_id)
        except AgentInvocationFailure:
            raise
        except Exception as first_error:
            # One repair attempt, then fail closed. A role must satisfy eight
            # exact-text constraints simultaneously on its first try with no
            # feedback -- complete-line anchors, uniqueness, one-for-one
            # evidence coverage, source-line disappearance, one claim per
            # source line, same-section pairing, ordered containment, numeric
            # identity. Measured across nine production runs, it lands any
            # individual rule once told and still misses on a first pass. The
            # local CLI flow never hit this because an interactive agent reads
            # the error and fixes it; that feedback was doing invisible work.
            #
            # Nothing is loosened: the same validator judges the retry, the
            # coordinator's context and digest binding are untouched, and a
            # second failure raises exactly as before. The retry carries its
            # own identity so the reuse guard keeps working and the receipt
            # still shows one distinct id per invocation.
            _log.warning(
                "role payload rejected (retrying once): role=%s attempt=%s "
                "run_id=%s reason=%s: %s",
                role, attempt, run_id, type(first_error).__name__, first_error,
            )
            repair_id = f"{agent_id}.repair"
            if repair_id in self._seen_invocations:
                raise LookupError("reused api invocation identity")
            try:
                repaired = call_host(
                    rejection=str(first_error),
                    previous_reply=_compact_json(raw_payload),
                )
                envelope = build_envelope(repaired, repair_id)
            except (AgentInvocationFailure, ConnectionError, LookupError):
                raise
            except Exception as retry_error:
                _log.warning(
                    "role payload rejected after repair: role=%s attempt=%s "
                    "run_id=%s reason=%s: %s",
                    role, attempt, run_id, type(retry_error).__name__, retry_error,
                )
                raise AgentInvocationFailure("AGENT_PAYLOAD_SCHEMA") from retry_error
            agent_id = repair_id

        self._seen_invocations.add(agent_id)
        return envelope
