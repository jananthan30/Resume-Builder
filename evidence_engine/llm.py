# evidence_engine/llm.py
"""LLM client protocol and provider-agnostic implementations.

The engine's guarantee is that the model only ever *classifies* and Python
computes every number. That makes the model a swappable component: a weaker
judge produces measurably wrong labels, never quietly drifting scores. So the
provider is configuration rather than policy.

Two rules keep that safe, and both are enforced structurally here rather than
by convention:

1. **Redaction is not optional.** ``complete_json`` lives on the base class and
   is the only path to a hosted model, so a new provider cannot forget to call
   :func:`pii_redactor.redact_text`. This matters more, not less, once calls
   leave Anthropic: a resume sent to a third-party endpoint is a third-party
   data transfer.
2. **The model that ran is recorded, not assumed.** Every client exposes
   ``model_id``, which the engine writes into the audit record and feeds to
   ``input_fingerprint``. Two models therefore have two cache identities, and a
   cheap-model score can never be served as a frontier-model one.

Model selection uses a ``provider:model`` spec — ``anthropic:claude-sonnet-5``,
``deepseek:deepseek-chat``, ``xai:grok-4.6``. A bare name means Anthropic, so
existing callers and stored records keep working unchanged.
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Protocol, runtime_checkable

from pii_redactor import redact_text

# Unchanged default. Cost work happens by configuring a different model, never
# by silently shipping one.
HOSTED_MODEL = "claude-sonnet-5"
DEFAULT_MODEL_SPEC = f"anthropic:{HOSTED_MODEL}"

# Per-role overrides. Extraction must quote the posting verbatim and classify
# hard gates, and it runs once per match; judging is bounded classification and
# runs once per requirement. They are configured separately because that is the
# split that actually trades cost against risk.
EXTRACTOR_MODEL_ENV = "EVIDENCE_EXTRACTOR_MODEL"
JUDGE_MODEL_ENV = "EVIDENCE_JUDGE_MODEL"

_REQUEST_TIMEOUT_SECONDS = 120.0

# The two roles need different budgets because only one of them scales with the
# input. Judging emits a small fixed verdict per requirement no matter how long
# the posting is. Extraction emits an object per requirement -- id, normalised
# text, a verbatim source quote, type, importance, hard-gate flag -- so a real
# job ad, which yields 25-40 atomic requirements, runs to several thousand
# tokens of JSON.
#
# Sharing 4096 between them silently capped extraction: the object was cut
# mid-write, arrived with no closing brace, and _parse_json could only report
# "no JSON object in LLM response" -- which reads as a misbehaving model rather
# than a budget we set too low. Short postings fit and worked, real ones did
# not, so it looked intermittent.
_MAX_OUTPUT_TOKENS = 4096
_MAX_OUTPUT_TOKENS_EXTRACTOR = 16384

# The Anthropic SDK retries transient failures internally. The OpenAI-compatible
# path has no such layer, so without this a single overloaded response would
# fail a whole match — making a cheap provider look less accurate than it is in
# a benchmark, when it was only briefly busy. Backoff is short: the caller is
# usually a person waiting on a fit check, not a batch job.
_RETRY_STATUSES = frozenset({408, 409, 429, 500, 502, 503, 504, 529})
_MAX_ATTEMPTS = 3
_RETRY_BACKOFF_SECONDS = 1.0


class LLMError(Exception):
    pass


class LLMTruncationError(LLMError):
    """The model hit its output cap before finishing the answer.

    Deliberately distinct from malformed JSON. Re-asking a truncated response
    produces the same truncation at the same cost, and the honest fix is a
    larger budget for that role. Raised from the transport layer, which sits
    outside ``complete_json``'s repair retry, so it never burns a second call.

    It also has to *say* it was truncated: reported as "no JSON object" this
    looked like a model failure for as long as it took to read the token cap.
    """


class LLMTransportError(LLMError):
    """The provider could not be reached, or returned nothing usable.

    Kept distinct from a malformed-JSON failure because the two want opposite
    handling: re-asking for valid JSON can fix a fenced or chatty response, but
    it cannot fix a 401, a 503, or a completion that ran out of output budget.
    Retrying those just doubles the latency and reports the second failure's
    message instead of the first one's.
    """


@runtime_checkable
class LLMClient(Protocol):
    #: The exact model that produced a response. Part of the audit record and
    #: of the cache identity, so it must name what actually ran.
    model_id: str

    def complete_json(self, system: str, user: str) -> dict:
        ...


# --------------------------------------------------------------------------
# Providers
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Provider:
    """One hosted endpoint.

    ``base_url`` of ``None`` selects the native Anthropic SDK. Everything else
    speaks the OpenAI chat-completions shape, which is what xAI, Moonshot,
    DeepSeek, OpenRouter, Together, Groq, and local vLLM/Ollama servers all
    expose.
    """

    name: str
    base_url: Optional[str]
    api_key_env: str
    #: Ask for JSON directly where the provider honours it. The repair retry in
    #: ``complete_json`` still covers providers that ignore or reject it.
    json_mode: bool = False
    #: Anthropic's current models reject sampling parameters outright; the
    #: OpenAI-compatible providers accept temperature and are more reliable at 0.
    temperature: Optional[float] = None
    #: Local endpoints do not leave the machine, so a key is not required.
    requires_key: bool = True


PROVIDERS: dict[str, Provider] = {
    "anthropic": Provider("anthropic", None, "ANTHROPIC_API_KEY"),
    "xai": Provider("xai", "https://api.x.ai/v1", "XAI_API_KEY",
                    json_mode=True, temperature=0.0),
    "moonshot": Provider("moonshot", "https://api.moonshot.ai/v1",
                         "MOONSHOT_API_KEY", json_mode=True, temperature=0.0),
    "deepseek": Provider("deepseek", "https://api.deepseek.com/v1",
                         "DEEPSEEK_API_KEY", json_mode=True, temperature=0.0),
    "openrouter": Provider("openrouter", "https://openrouter.ai/api/v1",
                           "OPENROUTER_API_KEY", temperature=0.0),
    "together": Provider("together", "https://api.together.xyz/v1",
                         "TOGETHER_API_KEY", json_mode=True, temperature=0.0),
    "fireworks": Provider("fireworks", "https://api.fireworks.ai/inference/v1",
                          "FIREWORKS_API_KEY", json_mode=True, temperature=0.0),
    "groq": Provider("groq", "https://api.groq.com/openai/v1", "GROQ_API_KEY",
                     json_mode=True, temperature=0.0),
    # Gemini via Google's OpenAI-compatibility layer. The native Gemini API
    # lives at a different path and speaks a different shape; this endpoint is
    # the one that fits the client above.
    "google": Provider("google",
                       "https://generativelanguage.googleapis.com/v1beta/openai",
                       "GEMINI_API_KEY", json_mode=True, temperature=0.0),
    # Alibaba Model Studio (formerly DashScope). The two regions are separate
    # entries rather than a flag: the Beijing endpoint is materially cheaper,
    # and sending candidate data there is a residency decision that should be
    # written down in configuration, not toggled.
    "dashscope": Provider("dashscope",
                          "https://dashscope-intl.aliyuncs.com/compatible-mode/v1",
                          "DASHSCOPE_API_KEY", json_mode=True, temperature=0.0),
    "dashscope-cn": Provider("dashscope-cn",
                             "https://dashscope.aliyuncs.com/compatible-mode/v1",
                             "DASHSCOPE_API_KEY", json_mode=True,
                             temperature=0.0),
    # Self-hosted weights. Point EVIDENCE_LOCAL_BASE_URL at vLLM, Ollama, or
    # llama.cpp; nothing leaves the machine, which is the only configuration
    # where candidate data never reaches a third party at all.
    "local": Provider("local", None, "", temperature=0.0, requires_key=False),
}

LOCAL_BASE_URL_ENV = "EVIDENCE_LOCAL_BASE_URL"
_DEFAULT_LOCAL_BASE_URL = "http://localhost:11434/v1"

# An aggregator routes one model slug across several backends that may serve it
# at different quantizations. Two runs can therefore record the same
# ``judge_model`` and be materially different models — which is precisely the
# failure the model-identity fix exists to prevent, reintroduced one layer up.
# Pin the backend to make a run reproducible; when unpinned, the recorded model
# id says so out loud rather than implying a reproducibility it does not have.
OPENROUTER_PROVIDER_ENV = "EVIDENCE_OPENROUTER_PROVIDER"
OPENROUTER_QUANTIZATIONS_ENV = "EVIDENCE_OPENROUTER_QUANTIZATIONS"
UNPINNED_SUFFIX = "@unpinned"


def parse_model_spec(spec: str) -> tuple[Provider, str]:
    """Split ``provider:model`` into its provider and model id.

    A spec with no provider prefix is Anthropic, so every model string written
    before this module existed still resolves the same way.
    """
    spec = (spec or "").strip()
    if not spec:
        raise ValueError("empty model spec")
    # Key off the separator, not the model half: a spec like "deepseek:" has a
    # provider and no model, and must be an error rather than silently
    # re-reading "deepseek" as an Anthropic model name.
    provider_name, separator, model = spec.partition(":")
    if not separator:
        provider_name, model = "anthropic", provider_name
    provider = PROVIDERS.get(provider_name.lower())
    if provider is None:
        raise ValueError(
            f"unknown provider {provider_name!r}; expected one of "
            + ", ".join(sorted(PROVIDERS))
        )
    if not model.strip():
        raise ValueError(f"model spec {spec!r} names no model")
    return provider, model.strip()


# --------------------------------------------------------------------------
# JSON handling
# --------------------------------------------------------------------------

_JSON_RE = re.compile(r"\{.*\}", re.DOTALL)
_FENCE_RE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$", re.I)


def _parse_json(text: str) -> dict:
    # Reasoning-tuned open models very often wrap the payload in a code fence.
    text = _FENCE_RE.sub("", text or "")
    match = _JSON_RE.search(text)
    if not match:
        raise LLMError("no JSON object in LLM response")
    try:
        return json.loads(match.group(0))
    except json.JSONDecodeError as exc:
        raise LLMError(f"invalid JSON: {exc}") from exc


#: Resolved from this file rather than the working directory. ``find_dotenv``
#: walks up from the *caller's* stack frame, so it depends on where the process
#: was started and raises outright when there is no caller frame (a ``python -``
#: heredoc, some embedded interpreters). Both failures look like "no key".
_ENV_PATH = Path(__file__).resolve().parent.parent / ".env"


def _load_env_file() -> None:
    """Pick up API keys from .env if the shell did not export them.

    The gate and the scorer both fail closed without a key, so a key sitting
    in .env but never loaded looks identical to having no key at all — the
    user is told "could not assess" when the credential was right there.
    Never overrides a value already in the environment.
    """
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    try:
        load_dotenv(_ENV_PATH, override=False)
    except Exception:
        # A missing or unreadable .env is not an error — the credential may
        # legitimately come from the real environment. Failing here would
        # convert "your shell already has the key" into "we cannot assess you".
        return


# --------------------------------------------------------------------------
# Clients
# --------------------------------------------------------------------------


class _HostedClient:
    """Shared request path for every provider.

    Subclasses implement transport only. Redaction, the JSON contract, and the
    single repair retry live here so they cannot be bypassed by adding a
    provider.
    """

    provider: Provider
    model_id: str
    #: Class-level default so every construction path has a budget, including
    #: test doubles built with ``__new__``. Instances raise it per role.
    max_output_tokens: int = _MAX_OUTPUT_TOKENS

    def _send(self, system: str, user: str) -> str:  # pragma: no cover - ABC
        raise NotImplementedError

    def _call(self, system: str, user: str) -> str:
        return self._send(redact_text(system), redact_text(user))

    def complete_json(self, system: str, user: str) -> dict:
        # Transport failures propagate untouched: only a malformed payload is
        # worth re-asking for.
        raw = self._call(system, user)
        try:
            return _parse_json(raw)
        except LLMError:
            repair = (user + "\n\nReturn ONLY valid JSON matching the schema. "
                              "No commentary, no code fence.")
            return _parse_json(self._call(system, repair))  # raises on 2nd fail

    def __repr__(self) -> str:
        return f"<{type(self).__name__} {self.provider.name}:{self.model_id}>"


class AnthropicClient(_HostedClient):
    """Claude via the official Anthropic SDK."""

    def __init__(self, model: str = HOSTED_MODEL,
                 max_output_tokens: int = _MAX_OUTPUT_TOKENS):
        self.provider = PROVIDERS["anthropic"]
        self.model_id = model
        self.max_output_tokens = max_output_tokens
        _load_env_file()
        import anthropic
        self._client = anthropic.Anthropic()

    def _send(self, system: str, user: str) -> str:
        # No temperature: it is rejected outright by the current Claude models.
        # Reproducibility does not depend on it here — the model only
        # classifies evidence, and every number is recomputed deterministically
        # from those classifications.
        resp = self._client.messages.create(
            model=self.model_id, max_tokens=self.max_output_tokens,
            system=system,
            messages=[{"role": "user", "content": user}],
        )
        if resp.stop_reason == "max_tokens":
            raise LLMTruncationError(
                f"{self.model_id} hit the {self.max_output_tokens}-token output "
                "cap before finishing; the reply is incomplete"
            )
        return "".join(b.text for b in resp.content if b.type == "text")


class OpenAICompatibleClient(_HostedClient):
    """Any endpoint exposing OpenAI chat-completions.

    Covers xAI, Moonshot, DeepSeek, OpenRouter, Together, Fireworks, Groq and
    self-hosted vLLM/Ollama. Implemented directly on httpx: it is a single
    unauthenticated-shape POST, and adding an SDK for one request would be a
    dependency the project does not otherwise need.
    """

    def __init__(self, provider: Provider, model: str,
                 base_url: Optional[str] = None,
                 max_output_tokens: int = _MAX_OUTPUT_TOKENS):
        _load_env_file()
        self.provider = provider
        self.max_output_tokens = max_output_tokens
        # What goes on the wire, which is not always what gets recorded: an
        # aggregator's recorded identity also names the backend that served it.
        self._wire_model = model
        self.model_id = model
        self._routing: Optional[dict] = None
        if provider.name == "openrouter":
            self._routing, self.model_id = _openrouter_routing(model)
        resolved = base_url or provider.base_url
        if provider.name == "local":
            resolved = (os.getenv(LOCAL_BASE_URL_ENV) or resolved
                        or _DEFAULT_LOCAL_BASE_URL)
        if not resolved:
            raise LLMTransportError(f"provider {provider.name} has no base URL")
        self.base_url = resolved.rstrip("/")

        self._api_key = os.getenv(provider.api_key_env) if provider.api_key_env else None
        if provider.requires_key and not self._api_key:
            # Fail at construction, so the engine reports "LLM unavailable"
            # rather than a fit rejection. "We could not assess you" must never
            # be reported as "you do not qualify".
            raise LLMTransportError(
                f"{provider.api_key_env} is not set; cannot reach "
                f"{provider.name}"
            )

        import httpx
        self._http = httpx.Client(timeout=_REQUEST_TIMEOUT_SECONDS)

    def _post(self, body: dict, headers: dict):
        """POST once, retrying only genuinely transient failures.

        A 4xx that is not a rate limit is the caller's problem and is returned
        for the caller to interpret — notably 400, which drives the json-mode
        fallback above.
        """
        import time

        import httpx
        failure: Optional[LLMTransportError] = None
        for attempt in range(_MAX_ATTEMPTS):
            try:
                resp = self._http.post(f"{self.base_url}/chat/completions",
                                       json=body, headers=headers)
            except httpx.HTTPError as exc:
                failure = LLMTransportError(
                    f"{self.provider.name} request failed: {exc}")
            else:
                if resp.status_code not in _RETRY_STATUSES:
                    return resp
                failure = LLMTransportError(
                    f"{self.provider.name} returned {resp.status_code}: "
                    f"{resp.text[:300]}")
            if attempt + 1 < _MAX_ATTEMPTS:
                time.sleep(_RETRY_BACKOFF_SECONDS * (2 ** attempt))
        raise failure

    def _send(self, system: str, user: str) -> str:
        body: dict = {
            "model": self._wire_model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "max_tokens": self.max_output_tokens,
        }
        if self.provider.temperature is not None:
            body["temperature"] = self.provider.temperature
        if self.provider.json_mode:
            body["response_format"] = {"type": "json_object"}
        if self._routing:
            body["provider"] = self._routing

        headers = {"content-type": "application/json"}
        if self._api_key:
            headers["authorization"] = f"Bearer {self._api_key}"

        resp = self._post(body, headers)

        if resp.status_code == 400 and self.provider.json_mode:
            # Not every model behind an OpenAI-compatible endpoint supports
            # JSON mode. Retry without it rather than failing the whole match;
            # the parser and repair retry already handle free-form output.
            body.pop("response_format", None)
            resp = self._post(body, headers)

        if resp.status_code >= 400:
            raise LLMTransportError(
                f"{self.provider.name} returned {resp.status_code}: "
                f"{resp.text[:300]}"
            )

        try:
            payload = resp.json()
            message = payload["choices"][0]["message"]
        except (ValueError, KeyError, IndexError) as exc:
            raise LLMTransportError(
                f"{self.provider.name} returned an unexpected shape: {exc}"
            ) from exc

        finish = payload["choices"][0].get("finish_reason")
        if finish == "length":
            raise LLMTruncationError(
                f"{self.provider.name}:{self.model_id} hit the "
                f"{self.max_output_tokens}-token output cap before finishing; "
                "the reply is incomplete"
            )

        # Reasoning models put the chain of thought in `reasoning_content` and
        # the answer in `content`. Only the answer is wanted.
        content = message.get("content")
        if not content:
            raise LLMTransportError(
                f"{self.provider.name} returned no content "
                f"(finish_reason={finish})"
            )
        return content


def _openrouter_routing(model: str) -> tuple[Optional[dict], str]:
    """Backend pin for an aggregated model, and the identity to record.

    Returns ``(routing_body, model_id)``. With a pin, fallbacks are disabled —
    a silent failover to another backend is exactly the substitution being
    guarded against, so it is better to fail and be told. Without one, the
    recorded id carries ``@unpinned`` so a stored score never claims a
    reproducibility it does not have.
    """
    pin = (os.getenv(OPENROUTER_PROVIDER_ENV) or "").strip()
    if not pin:
        return None, f"{model}{UNPINNED_SUFFIX}"

    routing: dict = {"order": [pin], "allow_fallbacks": False}
    quantizations = [q.strip() for q
                     in (os.getenv(OPENROUTER_QUANTIZATIONS_ENV) or "").split(",")
                     if q.strip()]
    if quantizations:
        # Precision is the difference a model slug most easily hides.
        routing["quantizations"] = quantizations
        return routing, f"{model}@{pin}/{'+'.join(quantizations)}"
    return routing, f"{model}@{pin}"


def build_client(spec: str = DEFAULT_MODEL_SPEC,
                 max_output_tokens: int = _MAX_OUTPUT_TOKENS) -> LLMClient:
    """Construct the client for a ``provider:model`` spec.

    ``max_output_tokens`` is per-role: extraction needs far more headroom than
    judging because its output grows with the posting. See the constants above.

    Raises on an unknown provider or a missing credential, which the engine
    turns into a fail-closed "LLM unavailable" rather than a fit rejection.
    """
    provider, model = parse_model_spec(spec)
    if provider.base_url is None and provider.name == "anthropic":
        return AnthropicClient(model, max_output_tokens=max_output_tokens)
    return OpenAICompatibleClient(provider, model,
                                  max_output_tokens=max_output_tokens)


def resolve_model_specs() -> tuple[str, str]:
    """The configured (extractor, judge) specs.

    Judging defaults to whatever extraction uses, so setting only
    ``EVIDENCE_JUDGE_MODEL`` moves the expensive, high-volume role to a cheaper
    model while leaving verbatim extraction alone.
    """
    _load_env_file()
    extractor = os.getenv(EXTRACTOR_MODEL_ENV) or DEFAULT_MODEL_SPEC
    judge = os.getenv(JUDGE_MODEL_ENV) or extractor
    return extractor.strip(), judge.strip()


def model_id_for(spec: str) -> str:
    """The identity a spec resolves to, without constructing a client.

    The engine needs this to build the cache fingerprint *before* deciding
    whether a network call is required at all, so it must agree exactly with
    the ``model_id`` the resulting client would report — including an
    aggregator's backend pin, which is part of what makes a run reproducible.
    """
    provider, model = parse_model_spec(spec)
    if provider.name == "openrouter":
        _load_env_file()
        return _openrouter_routing(model)[1]
    return model
