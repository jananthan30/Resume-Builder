"""Anthropic API host adapter for the four-role Resume Team.

This lets the Resume Team roles (researcher, writer, auditor, editor) run
against the Anthropic Messages API directly, with no Claude Code or Codex
subscription CLI installed -- the critical-path unlock for running the
audited pipeline server-side.

Scope and how this maps onto the existing host seam
-----------------------------------------------------
The existing CLI-based host, ``NativeRoleAdapter`` in
``native_resume_team.py``, exposes one method -- ``invoke(role, context,
timeout_seconds) -> dict`` -- that ``multi_agent_team.run_team()`` calls once
per role. Internally, ``NativeRoleAdapter.invoke`` does two very different
things: (1) spawn a CLI process and get back the *model's own untrusted
payload* for that role, and (2) hand that payload to shared, coordinator-owned
functions (``normalize_native_payload``, ``build_handoff``,
``validate_handoff``) that compute provenance, digests, and schema
validation. Part (2) never touches the model and is identical regardless of
which host produced the payload.

``AnthropicHost`` in this module implements the equivalent of part (1) only:
given a role and the payload the controller supplies for it, call the
Anthropic API, parse the JSON reply, and return that raw dict -- mirroring
the same one-call-per-role shape (``run_role(role, payload, *, case_id,
run_id) -> dict``) without requiring a pre-built ``context`` envelope,
digests, or the CLI-specific ``codex:``/``claude:`` agent-identity
conventions the coordinator's role-separation check inspects. Building the
full envelope (``build_handoff`` + ``normalize_native_payload`` +
``validate_handoff``) and wiring the result into something that satisfies
``run_team``'s exact ``adapter.invoke(...)`` contract is coordinator-side
integration work for a follow-up task -- doing it here, without an explicit
mandate for the agent-identity scheme a cloud host should use, would risk
inventing a contract the controller was never asked to accept.

Import safety
-------------
The ``anthropic`` SDK is never imported at module scope, and no network
client is ever constructed except lazily inside :meth:`AnthropicHost._call_once`
(only when the caller did not inject one). ``import agent.host_anthropic``
must succeed with no ``anthropic`` package installed and no
``ANTHROPIC_API_KEY`` set.

Tolerant JSON parsing
---------------------
``native_resume_team.py`` has a ``_strict_json_object`` helper, but it is
exactly what its name says -- ``json.loads`` plus a dict check, with no
tolerance for a model wrapping its reply in a markdown code fence or adding
a sentence of surrounding prose. That is a different (and heavier,
POSIX/subprocess-coupled) module besides, so this file defines its own
minimal tolerant extractor, :func:`_extract_json_object`, instead of
importing that one.
"""

from __future__ import annotations

import json
import re
import time
from typing import Any

from multi_agent_team import ROLE_ORDER

__all__ = [
    "ROLE_ORDER",
    "DEFAULT_MODEL_MAP",
    "DEFAULT_MAX_INPUT_TOKENS",
    "DEFAULT_MAX_OUTPUT_TOKENS",
    "HostRefusal",
    "BudgetExceeded",
    "TokenBudget",
    "AnthropicHost",
]

# Default per-role model routing. These are real, active Anthropic model IDs
# (verified against the current model catalog) -- not placeholders.
# Per-role model routing. Matched to what each role actually has to do, not
# assigned uniformly, because the roles differ enormously in difficulty.
#
# The Writer and Editor carry the whole precision burden: one evidence entry
# per changed line and no others, every anchor copied byte-exactly, each source
# line used once and gone from the draft, claim and source in the same section,
# ordered containment, numeric identity. Measured across eleven production
# runs on Sonnet 4.6, they satisfied any individual rule once told and still
# missed intermittently -- run 665925eb repaired and completed all four roles,
# run fcc6dfaa failed both its first attempt and its repair on identical
# input. That is a capability gap, and a retry only papers over it.
#
# The Researcher extracts and quotes lines from a job description and has been
# reliable on Haiku since it was given a real contract. The Auditor reproduces
# a draft verbatim and reports what it finds -- exacting but far narrower than
# writing. Sonnet 5 is the current Sonnet and no more expensive than the 4.6 it
# replaces.
#
# Cost: about $0.17 -> $0.20 per run at measured token volumes, against $12/mo
# for ten Pro runs. Roughly 83% margin instead of 86% -- worth paying for a
# feature that otherwise fails intermittently.
DEFAULT_MODEL_MAP: dict[str, str] = {
    "researcher": "claude-haiku-4-5",
    "writer": "claude-opus-5",
    "auditor": "claude-sonnet-5",
    "editor": "claude-opus-5",
}

DEFAULT_MAX_INPUT_TOKENS = 120_000
DEFAULT_MAX_OUTPUT_TOKENS = 25_000

_MAX_TOKENS_PER_CALL = 8_000
_MAX_API_ATTEMPTS = 3  # one initial attempt plus two retries
_API_RETRY_BACKOFF_SECONDS = (0.05, 0.1)

_REPAIR_INSTRUCTION = (
    "Your previous reply could not be parsed as JSON. Return only a single "
    "valid JSON object matching the same schema -- no prose, no markdown "
    "code fences, and no text before or after the JSON."
)

def _rejection_instruction(reason: str) -> str:
    """Hand a role the coordinator's verdict on its own last reply.

    The JSON repair above exists because a model occasionally returns
    unparseable text. This is the same idea one layer up: the reply parsed
    fine but broke a rule the coordinator enforces -- an anchor that was not a
    complete line, evidence that missed a changed line, a claim paired with a
    source from another section.

    A role has to satisfy eight exact-text constraints at once, first try,
    with no feedback. Measured across nine production runs, it lands any
    individual rule once told and still misses on a first attempt. The local
    CLI flow never had this problem because an interactive agent simply reads
    the error and fixes it; that feedback was doing invisible work the hosted
    path had no equivalent for.

    Nothing is loosened by showing the model why it failed: the same validator
    judges the retry, and a second failure is still a hard rejection.
    """
    return (
        "Your previous reply was REJECTED by the coordinator's validator.\n\n"
        f"Reason: {reason}\n\n"
        "The task and its input are unchanged. Return the same kind of JSON "
        "object again, corrected so that this specific rule is satisfied. Copy "
        "anchor text character-for-character from the source you were given -- "
        "including leading bullets or markers -- rather than retyping it from "
        "memory. Change nothing else about your answer. Return only the JSON "
        "object."
    )


_JSON_FENCE_RE = re.compile(r"^```(?:json)?\s*\n?(.*?)\n?```$", re.DOTALL)


class HostRefusal(Exception):
    """Raised when this host cannot produce a usable reply for a role call.

    Covers both an API call that keeps failing past its retry budget and a
    model reply that never resolves to valid JSON even after one repair
    attempt. The controller treats either as fail-closed.
    """


class BudgetExceeded(Exception):
    """Raised when a call would push accumulated token usage past its cap."""


class TokenBudget:
    """Tracks input/output token usage accumulated across role calls.

    Meant to be shared across every role invocation in one Resume Team run
    (one instance passed to every :class:`AnthropicHost` used in that run),
    so the run fails closed once it would spend more than the configured
    caps -- defaults match the standalone-agent Phase 1 global constraints
    (120,000 input tokens, 25,000 output tokens).

    :meth:`add` is atomic: an addition that would push either running total
    past its cap raises :class:`BudgetExceeded` and leaves the totals
    unchanged, so a caller can safely retry after handling the error.
    """

    def __init__(
        self,
        max_input_tokens: int = DEFAULT_MAX_INPUT_TOKENS,
        max_output_tokens: int = DEFAULT_MAX_OUTPUT_TOKENS,
    ) -> None:
        if max_input_tokens <= 0 or max_output_tokens <= 0:
            raise ValueError("token caps must be positive")
        self.max_input_tokens = max_input_tokens
        self.max_output_tokens = max_output_tokens
        self.input_tokens = 0
        self.output_tokens = 0

    def exhausted(self) -> bool:
        """Return whether either running total has already reached its cap.

        Once a total is at its cap, any further usage would exceed it, so
        callers should refuse to spend more (skip the API call outright)
        rather than call in and fail only after the fact.
        """
        return (
            self.input_tokens >= self.max_input_tokens
            or self.output_tokens >= self.max_output_tokens
        )

    def add(self, in_tokens: int, out_tokens: int) -> None:
        """Record usage, or raise :class:`BudgetExceeded` and record nothing.

        Missing/negative counts (e.g. a usage field the API omitted) are
        treated as zero rather than allowed to reduce the running total.
        """
        added_input = max(0, int(in_tokens or 0))
        added_output = max(0, int(out_tokens or 0))
        new_input = self.input_tokens + added_input
        new_output = self.output_tokens + added_output
        if new_input > self.max_input_tokens or new_output > self.max_output_tokens:
            raise BudgetExceeded(
                "token budget exceeded: "
                f"input {new_input}/{self.max_input_tokens}, "
                f"output {new_output}/{self.max_output_tokens}"
            )
        self.input_tokens = new_input
        self.output_tokens = new_output


def _extract_json_object(text: str) -> dict[str, Any]:
    """Tolerantly parse a JSON object out of model output text.

    Handles the formatting noise a real model reply can carry even when
    explicitly asked for bare JSON: surrounding whitespace, a single
    markdown code fence wrapping the whole reply, or a little prose before
    or after one JSON object. Raises ``ValueError`` if no JSON object can be
    recovered, or if the recovered value isn't a JSON object.
    """
    if not isinstance(text, str):
        raise ValueError("model output is not text")
    candidate = text.strip()
    if not candidate:
        raise ValueError("model output is empty")

    fence_match = _JSON_FENCE_RE.match(candidate)
    if fence_match:
        candidate = fence_match.group(1).strip()

    try:
        parsed = json.loads(candidate)
    except json.JSONDecodeError:
        start = candidate.find("{")
        end = candidate.rfind("}")
        if start == -1 or end == -1 or end <= start:
            raise ValueError("model output does not contain a JSON object") from None
        parsed = json.loads(candidate[start : end + 1])

    if not isinstance(parsed, dict):
        raise ValueError("model output is not a JSON object")
    return parsed


def _first_text_block(response: Any) -> str:
    """Return the text of the first text-type content block in a response.

    Accepts either real SDK response objects (attribute access) or plain
    dicts (as a fake test client might use), checking both shapes so the
    same helper works against either.
    """
    content = getattr(response, "content", None)
    if content is None and isinstance(response, dict):
        content = response.get("content")
    if not isinstance(content, list):
        raise HostRefusal("Anthropic response has no content blocks")

    for block in content:
        block_type = getattr(block, "type", None)
        if block_type is None and isinstance(block, dict):
            block_type = block.get("type")
        if block_type != "text":
            continue
        text = getattr(block, "text", None)
        if text is None and isinstance(block, dict):
            text = block.get("text")
        if isinstance(text, str):
            return text

    raise HostRefusal("Anthropic response contained no text block")


def _usage_tokens(response: Any) -> tuple[int, int]:
    """Return ``(input_tokens, output_tokens)`` from a response, else zero."""
    usage = getattr(response, "usage", None)
    if usage is None and isinstance(response, dict):
        usage = response.get("usage")
    if usage is None:
        return 0, 0

    input_tokens = getattr(usage, "input_tokens", None)
    if input_tokens is None and isinstance(usage, dict):
        input_tokens = usage.get("input_tokens")
    output_tokens = getattr(usage, "output_tokens", None)
    if output_tokens is None and isinstance(usage, dict):
        output_tokens = usage.get("output_tokens")
    return int(input_tokens or 0), int(output_tokens or 0)


_JSON_ONLY = (
    "Respond with a single strict JSON object only -- no prose, no markdown "
    "code fences, and no text before or after the JSON."
)

# Each role's exact output contract, mirroring what
# multi_agent_team.normalize_native_payload accepts. These are not style
# guidance: the coordinator recomputes every offset and digest itself and
# fails closed on any deviation, so a payload that is merely reasonable is
# still rejected. Without these, a role receives only "you are the
# <role>" and produces the generically sensible thing -- the researcher,
# for instance, returns a job-description field extraction (job_title,
# company, key_skills, ...) instead of an evidence-anchored rubric, and the
# whole run dies at FAILED:AGENT_PAYLOAD_SCHEMA before writing a word.
_ROLE_CONTRACTS: dict[str, str] = {
    "researcher": (
        "You are the Researcher. You receive only a job description. Convert "
        "it into a requirement rubric where every requirement is quoted "
        "verbatim from that job description.\n\n"
        "Return exactly these two top-level keys and no others:\n"
        '  "rubric": {"hard_requirements": [string, ...], '
        '"soft_requirements": [string, ...]}\n'
        '  "jd_evidence_spans": [{"evidence_text": string}, ...]\n\n'
        "Rules, all enforced:\n"
        "- Every requirement string must be ONE COMPLETE LINE copied "
        "character-for-character from the job description, from the first "
        "non-space character of that line to its last. Do not paraphrase, "
        "truncate, merge lines, or join with commas.\n"
        "- KEEP the line's leading marker. Bullets, dashes, asterisks, and "
        "numbering are part of the line. Removing them is the single most "
        "common way this fails.\n"
        "  If the job description contains this line:\n"
        "      - 3+ years of clinical trial experience required\n"
        "  then the requirement must be exactly:\n"
        '      "- 3+ years of clinical trial experience required"\n'
        '  Writing "3+ years of clinical trial experience required" without '
        'the leading "- " is REJECTED and the whole run fails.\n'
        "- That line must occur exactly ONCE in the job description. If a line "
        "appears more than once, choose a different requirement.\n"
        "- jd_evidence_spans must have exactly one entry per requirement, in "
        "the same order: all hard_requirements first, then all "
        "soft_requirements.\n"
        '- Each entry\'s "evidence_text" must EQUAL its requirement string '
        "exactly. They are the same string, repeated.\n"
        "- Hard requirements are stated as mandatory (required, must have, "
        "minimum). Soft requirements are stated as preferred, desired, or a "
        "plus. Include at least one requirement.\n"
        "- Emit no offsets, indexes, hashes, or scores. The coordinator "
        "computes those."
    ),
    "writer": (
        "You are the Writer. You receive a master resume and a validated "
        "requirement rubric. Produce one complete tailored resume draft that "
        "reuses only facts already present in the master resume.\n\n"
        "Return exactly these two top-level keys and no others:\n"
        '  "draft": string  (the complete resume, plain text)\n'
        '  "claim_evidence": [{"claim_text": string, '
        '"source_span_text": string}, ...]\n\n'
        "CHANGE AS FEW LINES AS POSSIBLE. Every line you change must be "
        "justified below, one entry each, so a small, surgical edit succeeds "
        "where a full rewrite fails. Concentrate changes in the professional "
        "summary and core-competencies lines; leave experience bullets alone "
        "unless a rubric requirement genuinely needs one reworded.\n\n"
        "Rules, all enforced:\n"
        "- Never invent employers, titles, dates, degrees, certifications, "
        "publications, or metrics. Reframe existing wording only.\n"
        "- Keep job titles, company names, and dates byte-identical to the "
        "master resume.\n"
        "- claim_evidence must cover the lines you changed EXACTLY: one entry "
        "for every changed line, and no entry for any line you left alone. "
        "Not a sample, not the interesting ones -- all of them and only them. "
        "A missing or extra entry fails the whole run.\n"
        '- "claim_text" is your new draft line, copied exactly and occurring '
        "exactly once in the draft.\n"
        '- "source_span_text" is the ONE COMPLETE LINE of the master resume '
        "that line replaces, copied exactly. Because you rewrote it, that "
        "original line must NOT still appear anywhere in your draft.\n"
        "- Each master-resume line may back at most one claim.\n"
        "- ADD, DO NOT REWRITE. Start from the original line and insert the "
        "job description's terms into it. Every word of the original must "
        "still be there, in its original order. Deleting or replacing any of "
        "the original wording is rejected, however much better the new "
        "sentence reads.\n"
        "  Original:  Combines clinical judgment with ICH-GCP knowledge\n"
        "  Good:      Combines clinical judgment with ICH-GCP and "
        "pharmacovigilance knowledge\n"
        "  REJECTED:  Brings early-phase oncology experience from a global "
        "CRO   (the original words are gone)\n"
        "- STAY IN THE SAME SECTION. A changed line must be paired with the "
        "line it replaces in that same part of the resume:\n"
        "    * a summary line's source is the ORIGINAL SUMMARY line;\n"
        "    * a core-competencies line's source is the ORIGINAL "
        "COMPETENCIES line;\n"
        "    * an experience bullet's source is a bullet under THE SAME job.\n"
        "  Never source a summary or competencies line from an experience "
        "bullet, and never source a bullet in one job from a bullet in "
        "another job. Both of those are rejected as UNSUPPORTED_CLAIM even "
        "when the facts are true.\n"
        "- The claim must be supported by its source line -- same facts, "
        "reworded. Do not pair a claim with an unrelated source line.\n"
        "- Copy both texts with any leading bullet or marker intact "
        '("• ", "- "); stripping them is rejected.\n'
        "- Do not reorder or duplicate experience between roles.\n"
        "- Before answering, count your changed lines and count your "
        "claim_evidence entries. They must be the same number."
    ),
    "auditor": (
        "You are the Auditor. You receive the exact writer draft and the "
        "rubric. Judge the draft. You have no authority to edit it.\n\n"
        "Return exactly these three top-level keys and no others:\n"
        '  "verdict": "PASS" or "FAIL"\n'
        '  "findings": [{"id": string, "code": string, '
        '"evidence_text": string}, ...]\n'
        '  "audited_draft": string\n\n'
        "Rules, all enforced:\n"
        '- "audited_draft" must be the draft you received, reproduced '
        "byte-for-byte. Do not fix, reformat, or improve it.\n"
        '- "verdict" is "PASS" if and only if findings is empty. "FAIL" '
        "requires at least one finding, and any finding requires FAIL.\n"
        '- Each finding\'s "evidence_text" must be ONE COMPLETE LINE copied '
        "exactly from the draft, occurring exactly once in it. Keep the "
        "line's leading bullet or marker -- a resume bullet line starts with "
        '"• " or "- " and that prefix is part of the line. Dropping it '
        "is rejected.\n"
        '- "id" is a short unique identifier you assign (e.g. "F1"). "code" '
        "names the problem class (e.g. UNSUPPORTED_CLAIM, TITLE_ALTERED, "
        "DATE_ALTERED, FABRICATED_METRIC, RUBRIC_UNMET).\n"
        "- Report a finding only where the draft contradicts the master "
        "resume or asserts something it does not support."
    ),
    "editor": (
        "You are the Editor. You receive the draft, the master resume, and "
        "the auditor's findings. Correct only what the findings name.\n\n"
        "Return exactly these three top-level keys and no others:\n"
        '  "draft": string  (the complete corrected resume)\n'
        '  "addressed_finding_ids": [string, ...]\n'
        '  "claim_evidence": [{"claim_text": string, '
        '"source_span_text": string}, ...]\n\n'
        "Rules, all enforced:\n"
        "- Change only the lines the findings identify. Leave every other "
        "line byte-identical.\n"
        "- Never invent facts; the same authenticity rules as the Writer "
        "apply.\n"
        '- "addressed_finding_ids" lists the finding ids you fixed.\n'
        "- claim_evidence must cover the lines that differ from the master "
        "resume EXACTLY: one entry per differing line, none for any line that "
        "matches the master. A missing or extra entry fails the whole run.\n"
        '- "claim_text" is your draft line copied exactly; '
        '"source_span_text" is the ONE COMPLETE LINE of the master resume it '
        "replaces, copied exactly, and that original line must not still "
        "appear in your draft. Each master line may back at most one claim. "
        "Keep leading bullets and markers intact.\n"
        "- STAY IN THE SAME SECTION: a summary line's source is the original "
        "summary line, a competencies line's source is the original "
        "competencies line, and an experience bullet's source is a bullet "
        "under the same job. Crossing between sections is rejected as "
        "UNSUPPORTED_CLAIM even when the facts are true.\n"
        "- Return the COMPLETE resume, not a diff or a fragment."
    ),
}


def _default_system_prompt(role: str) -> str:
    contract = _ROLE_CONTRACTS.get(role)
    if contract is None:
        return (
            f"You are the {role} in an automated resume-writing team. "
            f"{_JSON_ONLY}"
        )
    return f"{contract}\n\n{_JSON_ONLY}"


class AnthropicHost:
    """Calls the Anthropic Messages API for one Resume Team role at a time.

    The standalone-agent counterpart to the CLI-based ``NativeRoleAdapter``
    in ``native_resume_team.py`` -- same idea (call a model with a role's
    input, get its JSON reply back) with no Claude Code or Codex
    subscription CLI involved. See the module docstring for exactly how far
    this class's responsibility goes versus the coordinator's.
    """

    def __init__(
        self,
        model_map: dict[str, str] | None = None,
        budget: TokenBudget | None = None,
        client: Any = None,
    ) -> None:
        resolved_map = dict(DEFAULT_MODEL_MAP)
        if model_map:
            resolved_map.update(model_map)
        missing = [role for role in ROLE_ORDER if role not in resolved_map]
        if missing:
            raise ValueError(f"model_map is missing roles: {missing}")
        self.model_map = resolved_map
        self.budget = budget if budget is not None else TokenBudget()
        self._client = client

    def _ensure_client(self) -> Any:
        """Return the injected client, or lazily construct the real one.

        ``anthropic`` is imported here -- never at module scope -- so this
        module stays importable with no SDK installed and no API key set.
        """
        if self._client is None:
            import anthropic  # local import: keep import-time SDK-free

            self._client = anthropic.Anthropic()
        return self._client

    def _call_once(
        self, *, model: str, system: str, messages: list[dict[str, Any]]
    ) -> Any:
        """Call the API with retry-on-exception; return the raw response.

        Retries any API exception up to ``_MAX_API_ATTEMPTS - 1`` times with
        a short backoff between attempts, then raises :class:`HostRefusal`.
        """
        client = self._ensure_client()
        last_error: Exception | None = None
        for attempt in range(_MAX_API_ATTEMPTS):
            try:
                return client.messages.create(
                    model=model,
                    max_tokens=_MAX_TOKENS_PER_CALL,
                    system=system,
                    messages=messages,
                )
            except Exception as error:  # noqa: BLE001 - any API failure retries the same way
                last_error = error
                if attempt < _MAX_API_ATTEMPTS - 1:
                    time.sleep(_API_RETRY_BACKOFF_SECONDS[attempt])
        raise HostRefusal(
            f"Anthropic API call failed after {_MAX_API_ATTEMPTS} attempts: {last_error}"
        )

    def run_role(
        self,
        role: str,
        payload: dict[str, Any],
        *,
        case_id: str,
        run_id: str,
        rejection: str | None = None,
        previous_reply: str | None = None,
    ) -> dict[str, Any]:
        """Call one role's model with ``payload`` and return its parsed reply.

        ``payload`` is the role-scoped input data the controller supplies for
        that role. ``case_id``/``run_id`` are accepted for tracing and
        observability; this host does not embed them into the request or the
        returned dict -- provenance and digest binding stay coordinator-owned,
        exactly as with the CLI adapters (see the module docstring).

        Raises:
            ValueError: unknown role, or a non-dict payload/blank
                case_id/run_id.
            BudgetExceeded: the shared budget is already exhausted; the
                client is not called for this attempt.
            HostRefusal: the API call failed past its retry budget, or the
                model's reply could not be parsed as JSON even after one
                repair retry.
        """
        if role not in ROLE_ORDER:
            raise ValueError(f"unsupported role: {role!r}")
        if not isinstance(payload, dict):
            raise ValueError("payload must be an object")
        if not isinstance(case_id, str) or not case_id:
            raise ValueError("case_id must be non-empty")
        if not isinstance(run_id, str) or not run_id:
            raise ValueError("run_id must be non-empty")

        if self.budget.exhausted():
            raise BudgetExceeded("token budget already exhausted; refusing to call")

        model = self.model_map[role]
        system = _default_system_prompt(role)
        payload_text = json.dumps(payload, sort_keys=True, ensure_ascii=True)
        messages: list[dict[str, Any]] = [{"role": "user", "content": payload_text}]

        # Coordinator-supplied rejection: replay the model's own reply and the
        # validator's verdict so it can correct one specific violation. The
        # payload above is unchanged, so this is a second look at the same
        # task, not a different one. See AnthropicTeamAdapter.invoke.
        if rejection:
            if previous_reply:
                messages.append({"role": "assistant", "content": previous_reply})
            messages.append(
                {"role": "user", "content": _rejection_instruction(rejection)}
            )

        response = self._call_once(model=model, system=system, messages=messages)
        text = _first_text_block(response)
        in_tokens, out_tokens = _usage_tokens(response)
        self.budget.add(in_tokens, out_tokens)

        try:
            return _extract_json_object(text)
        except ValueError:
            pass  # fall through to the single repair retry

        repair_messages = messages + [
            {"role": "assistant", "content": text},
            {"role": "user", "content": _REPAIR_INSTRUCTION},
        ]
        response = self._call_once(model=model, system=system, messages=repair_messages)
        text = _first_text_block(response)
        in_tokens, out_tokens = _usage_tokens(response)
        self.budget.add(in_tokens, out_tokens)

        try:
            return _extract_json_object(text)
        except ValueError as error:
            raise HostRefusal(
                f"{role} reply was not valid JSON after one repair retry"
            ) from error
