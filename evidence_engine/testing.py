# evidence_engine/testing.py
"""A deterministic judge for running the engine without a hosted model.

The engine splits work so the model classifies evidence and Python computes
every number. That split is what makes this possible: swap in a rule-based
classifier and the whole pipeline runs offline, deterministically, and fast.

Two audiences:

* Tests that need the engine to run but are not testing model behaviour —
  gate wiring, receipts, pipeline plumbing.
* Anyone integrating the engine who wants a CI run without an API key.

It is explicitly NOT a model substitute for judging real resumes. It applies
only the rules the specification states plainly — reward stated ownership,
discount participation, ignore repetition, respect explicit contradiction and
distinguish held credentials from planned ones. Anything subtler needs a real
judge.
"""
from __future__ import annotations

import re

__all__ = ["DeterministicJudge", "use_deterministic_judge"]

_WORD_RE = re.compile(r"[a-z0-9+#.]+")

# Verbs that assert the candidate did the thing themselves.
_OWNERSHIP = (
    "authored", "led", "owned", "built", "designed", "developed", "created",
    "managed", "directed", "ran", "delivered", "trained", "administered",
    "implemented", "established", "launched", "drove", "wrote", "reviewed",
    "conducted", "performed", "maintained", "oversaw", "shipped", "held",
)
# Verbs that place the candidate near the work without claiming it.
_PARTICIPATION = (
    "supported", "assisted", "contributed", "participated", "helped",
    "observed", "shadowed", "exposed", "familiar", "tracked", "coordinated",
    "attended", "aware",
)
# Explicit contradiction of a current credential.
_CONTRADICTS = ("expired", "lapsed", "revoked", "suspended", "no longer",
                "not renewed", "inactive")
# A credential that is planned or in progress is not a credential held.
_NOT_YET = ("pending", "applied for", "eligible for", "pursuing", "planning",
            "in progress", "scheduled to", "will obtain", "studying for",
            "enrolled in", "candidate for")

_STOPWORDS = {
    "a", "an", "and", "are", "as", "at", "be", "by", "for", "from", "has",
    "have", "in", "is", "it", "of", "on", "or", "our", "that", "the", "this",
    "to", "we", "will", "with", "you", "your", "must", "should", "can",
    "experience", "experienced", "years", "year", "required", "require",
    "requires", "preferred", "minimum", "strong", "excellent", "ability",
    "including", "such", "other", "work", "working", "role", "team",
}

_PREFERRED_RE = re.compile(r"preferr|desired|nice to have|\bplus\b|\bideal\b", re.I)
_REQUIRED_RE = re.compile(r"required|must|minimum of|mandatory|\bneed\b", re.I)
_LICENSE_RE = re.compile(r"licen[sc]", re.I)
_CERT_RE = re.compile(r"certif|credential", re.I)
_DEGREE_RE = re.compile(
    r"\b(bachelor|master|doctorate|doctoral|phd|md|pharmd|mph|mba|bsn|msn|"
    r"degree)\b", re.I)
_AUTH_RE = re.compile(r"work authoriz|authoriz\w+ to work|visa|sponsorship", re.I)
_LOCATION_RE = re.compile(r"\bon-?site\b|\brelocat|\bbased in\b|\bhybrid\b", re.I)

# A line worth testing the resume against.
_REQUIREMENT_LINE_RE = re.compile(
    r"(?i)(?:"
    r"\b(?:required|must|minimum|mandatory|preferred|desired|need)\b"
    r"|\b\d+\+?\s*(?:or more\s*)?years?\b"
    r"|\bexperience\b"
    r"|\b(?:bachelor|master|doctorate|phd|md|degree|licen[sc]|certif)\w*\b"
    r"|\bability to\b|\bproficien|\bknowledge of\b"
    r")")

_MIN_YEARS_RE = re.compile(
    r"\b(\d{1,2})\s*\+?\s*(?:or\s+more\s+)?years?\b", re.I)

_MAX_REQUIREMENTS = 12
# Sharing this share of a requirement's content words is a strong topical
# signal for a lexical matcher. Tuned so a resume that plainly names the
# requirement's subject is not under-credited into a borderline score.
_STRONG_OVERLAP = 0.45
_WEAK_OVERLAP = 0.25


def _content_words(text: str) -> set[str]:
    return {w for w in _WORD_RE.findall(text.lower())
            if w not in _STOPWORDS and len(w) > 2}


def _requirement_type(text: str) -> str:
    if _LICENSE_RE.search(text):
        return "license"
    if _CERT_RE.search(text):
        return "certification"
    if _DEGREE_RE.search(text):
        return "degree"
    if _AUTH_RE.search(text):
        return "work_authorization"
    if _LOCATION_RE.search(text):
        return "location"
    return "experience"


def _importance(text: str, rtype: str) -> tuple[str, bool]:
    """Importance plus whether the line is a hard gate.

    Preferred language wins over required language, matching the extractor's
    own rule that "preferred but not required" is not a must-have.
    """
    if _PREFERRED_RE.search(text):
        return "PREFERRED", False
    if _REQUIRED_RE.search(text):
        if rtype in {"license", "certification", "work_authorization",
                     "location", "degree"}:
            return "HARD_GATE", True
        return "MUST_HAVE", False
    return "CORE", False


def _is_repetitive(text: str) -> bool:
    """Low variety means keyword stuffing rather than a claim about work."""
    tokens = _WORD_RE.findall(text.lower())
    if len(tokens) < 8:
        return False
    if len(set(tokens)) / len(tokens) < 0.65:
        return True
    content = [t for t in tokens if t not in _STOPWORDS]
    return any(content.count(t) >= 3 for t in set(content))


class DeterministicJudge:
    """Rule-based stand-in for the extractor and evidence judge roles.

    Two modes, because two different kinds of test need it:

    ``topical`` (default)
        Any genuine topical overlap is strong evidence; anything unrelated is
        none. Deliberately coarse and therefore *stable*: tests exercising the
        writer, publication or receipt pipeline should not flake because a
        lexical threshold moved. Unrelated work still scores zero, and the
        deterministic contradiction floor still applies, so a retail resume
        and an expired licence both behave correctly.

    ``calibrated``
        Graded scoring that distinguishes ownership from participation and
        strong overlap from weak. Use when the gradation itself is under test.
    """

    def __init__(self, max_requirements: int = _MAX_REQUIREMENTS,
                 mode: str = "topical"):
        if mode not in {"topical", "calibrated"}:
            raise ValueError(f"unknown judge mode: {mode!r}")
        self.max_requirements = max_requirements
        self.mode = mode
        self.calls = 0
        # Named honestly so offline results are never filed in the audit trail
        # or the result cache under a hosted model's identity. The mode is part
        # of it because the two modes grade differently.
        self.model_id = f"deterministic-judge-{mode}-v1"

    # -- LLMClient protocol --------------------------------------------
    def complete_json(self, system: str, user: str) -> dict:
        self.calls += 1
        if "extract job requirements" in system:
            return {"requirements": self._extract(user)}
        return {"judgments": self._judge(user)}

    # -- requirement extraction ----------------------------------------
    def _extract(self, user: str) -> list[dict]:
        marker = "offsets are 0-based into this exact text):\n\n"
        jd_text = user.split(marker, 1)[1] if marker in user else user

        requirements: list[dict] = []
        cursor = 0
        for raw_line in jd_text.split("\n"):
            start = jd_text.index(raw_line, cursor) if raw_line else cursor
            cursor = start + len(raw_line)
            line = raw_line.strip()
            if len(line) < 12 or not _REQUIREMENT_LINE_RE.search(line):
                continue
            # Offsets must reproduce source_text exactly or the extractor
            # rejects the payload, so anchor on the stripped span.
            offset = jd_text.index(line, start)
            rtype = _requirement_type(line)
            importance, hard_gate = _importance(line, rtype)
            years = _MIN_YEARS_RE.search(line)
            # A stated minimum qualifies a topical requirement; it does not
            # replace it. Marking it a gate would strip the requirement out of
            # the fit denominator, losing the candidate's strongest match.
            minimum_years = float(years.group(1)) if years else None
            requirements.append({
                "requirement_id": f"R{len(requirements) + 1}",
                "source_text": line,
                "source_start_char": offset,
                "source_end_char": offset + len(line),
                "normalized_text": line.lstrip("-•* ").rstrip(".").strip(),
                "type": rtype,
                "importance": importance,
                "hard_gate": hard_gate,
                "minimum_years": minimum_years,
                "concepts": sorted(_content_words(line))[:6],
                "extraction_confidence": 0.9,
            })
            if len(requirements) >= self.max_requirements:
                break
        return requirements

    # -- evidence judging ----------------------------------------------
    def _judge(self, user: str) -> list[dict]:
        header, _, body = user.partition("CANDIDATE EVIDENCE SPANS:")
        requirement_line = header.split(":", 1)[1].split("\n")[0] if ":" in header else ""
        requirement_words = _content_words(requirement_line)
        rtype = _requirement_type(requirement_line)

        judgments = []
        for line in body.splitlines():
            if not line.startswith("["):
                continue
            span_id = line[1:line.index("]")]
            text = line[line.index("]") + 1:].strip()
            judgments.append({
                "requirement_id": "R1",
                "evidence_span_id": span_id,
                "confidence": 0.9,
                **self._classify(text, requirement_words, rtype, self.mode),
            })
        return judgments

    @staticmethod
    def _classify(text: str, requirement_words: set[str], rtype: str,
                  mode: str = "topical") -> dict:
        lowered = text.lower()
        none = {"relationship": "NONE", "status": "NO_EVIDENCE_FOUND",
                "specificity": 0.0, "scope": 0.0, "context": 0.0,
                "temporal_relevance": 0.0, "reason": "no supporting claim"}

        if _is_repetitive(text):
            return dict(none, reason="repetition without a claim")

        # Coverage of the SMALLER set, not of the requirement. A compound
        # requirement listing six competencies can never be half-covered by
        # one short resume line, yet a line naming four of them verbatim is
        # strong evidence. At least two shared content words are needed so a
        # stray coincidence cannot score.
        span_words = _content_words(text)
        shared = requirement_words & span_words
        if len(shared) < 2 or not requirement_words or not span_words:
            overlap = 0.0
        else:
            overlap = len(shared) / min(len(requirement_words), len(span_words))
        if overlap < _WEAK_OVERLAP:
            return none

        if mode == "topical":
            shared = requirement_words & _content_words(text)
            credential = rtype in {"license", "certification", "degree"}
            if credential and any(term in lowered for term in _CONTRADICTS):
                return {"relationship": "CONTRADICTORY",
                        "status": "CONTRADICTORY_EVIDENCE", "specificity": 0.9,
                        "scope": 0.9, "context": 0.9, "temporal_relevance": 1.0,
                        "reason": "the credential is explicitly not current"}
            if credential and any(term in lowered for term in _NOT_YET):
                return dict(none, reason="the credential is planned, not held")
            if not shared:
                return none
            return {"relationship": "EXACT_EXPLICIT", "status": "STRONG_EVIDENCE",
                    "specificity": 0.9, "scope": 0.85, "context": 0.9,
                    "temporal_relevance": 1.0,
                    "reason": "the requirement's subject is stated in the resume"}

        credential = rtype in {"license", "certification", "degree"}
        if credential and any(term in lowered for term in _CONTRADICTS):
            return {"relationship": "CONTRADICTORY",
                    "status": "CONTRADICTORY_EVIDENCE", "specificity": 0.9,
                    "scope": 0.9, "context": 0.9, "temporal_relevance": 1.0,
                    "reason": "the credential is explicitly not current"}
        if credential and any(term in lowered for term in _NOT_YET):
            # Planned is not held — but it is also not a contradiction, so the
            # gate reports UNVERIFIED rather than refusing the candidate.
            return dict(none, reason="the credential is planned, not held")

        owns = any(verb in lowered for verb in _OWNERSHIP)
        if any(verb in lowered for verb in _PARTICIPATION):
            return {"relationship": "CONTEXTUAL_IMPLICATION",
                    "status": "WEAK_EVIDENCE", "specificity": 0.35,
                    "scope": 0.3, "context": 0.45, "temporal_relevance": 1.0,
                    "reason": "participation, not ownership"}
        if overlap >= _STRONG_OVERLAP and owns:
            return {"relationship": "EXACT_EXPLICIT", "status": "STRONG_EVIDENCE",
                    "specificity": 0.9, "scope": 0.85, "context": 0.9,
                    "temporal_relevance": 1.0, "reason": "stated ownership"}
        if overlap >= _STRONG_OVERLAP:
            # The requirement's own subject matter is stated in the resume.
            # A summary or competency line is weaker than a described
            # achievement, but it is still the candidate asserting the topic.
            return {"relationship": "EXACT_EXPLICIT", "status": "STRONG_EVIDENCE",
                    "specificity": 0.7, "scope": 0.65, "context": 0.75,
                    "temporal_relevance": 1.0, "reason": "topic stated directly"}
        if owns:
            return {"relationship": "CONTEXTUAL_IMPLICATION",
                    "status": "MODERATE_EVIDENCE", "specificity": 0.6,
                    "scope": 0.55, "context": 0.65, "temporal_relevance": 1.0,
                    "reason": "related work owned by the candidate"}
        return {"relationship": "CONTEXTUAL_IMPLICATION",
                "status": "WEAK_EVIDENCE", "specificity": 0.4,
                "scope": 0.4, "context": 0.5, "temporal_relevance": 1.0,
                "reason": "topic present without a clear claim"}


def use_deterministic_judge(monkeypatch, mode: str = "topical") -> None:
    """Route every unconfigured evidence match through the offline judge.

    Patches the point where the engine constructs its hosted client, so tests
    that exercise production entry points -- HTTP endpoints, agent tools, the
    background runner -- run end to end without an API key and without any
    production code path that could silently fall back to a rule-based judge.

        def test_something(monkeypatch):
            use_deterministic_judge(monkeypatch)
    """
    import evidence_engine.engine as engine

    def _offline(*args, **kwargs):
        return DeterministicJudge(mode=mode)

    # ``build_client`` is the engine's single construction point for any
    # provider; ``AnthropicClient`` is patched too so older call sites that
    # reach for it directly stay offline.
    monkeypatch.setattr(engine, "build_client", _offline)
    monkeypatch.setattr(engine, "AnthropicClient", _offline)
