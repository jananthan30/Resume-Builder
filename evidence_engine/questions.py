# evidence_engine/questions.py
"""Turning gaps into questions, and answers into permissions.

``NO_EVIDENCE_FOUND`` is the engine's most honest output and, on its own, a
dead end: it tells someone their resume did not demonstrate something, without
saying whether they can. Only the candidate knows. So the engine asks.

The rule that governs everything here:

    **An answer authorises a rewrite. An answer is never evidence.**

Saying "yes, I did that" does not raise a score. It permits the writer to look
for and phrase experience the candidate actually has; the rewritten resume is
then matched again, and the *new text* is what scores — through the same
judge, the same offsets and the same replay as everything else. Without that
rule this becomes self-certification and the engine's guarantee is worthless.

Answering also makes ``TRUE_QUALIFICATION_GAP`` usable for the first time. The
specification permits that label only when the candidate confirms the
qualification is absent, and until now nothing ever asked, so it could never
be applied correctly.
"""
from __future__ import annotations

import re

from evidence_engine.models import (
    AnswerChoice, EvidenceStatus, GapAnswer, Importance, MatchResult,
    OpenQuestion, RewriteRecommendation, RewriteType,
)

# Interrupting someone is expensive. The engine already knows which gaps carry
# weight, so it asks about very few and only where the answer changes what may
# be written.
MAX_QUESTIONS = 3

_ASKABLE_STATUSES = {
    EvidenceStatus.NO_EVIDENCE_FOUND,
    EvidenceStatus.WEAK_EVIDENCE,
}
# Preferred and optional gaps are not worth a question: knowing the answer
# would not change whether the application is worth making.
_ASKABLE_IMPORTANCE = {Importance.MUST_HAVE, Importance.HARD_GATE}


_BOILERPLATE_RE = re.compile(
    r"^(?:the\s+role\s+will\s+|the\s+role\s+|you\s+will\s+|will\s+"
    r"|must\s+have\s+|must\s+|should\s+have\s+|candidates?\s+(?:must|should)\s+)"
    r"|\s*(?:is\s+|are\s+)?(?:required|mandatory|preferred|desired|essential)"
    r"(?:\s+for\s+this\s+(?:role|position))?\s*$",
    re.I)

# Requirement text does not always arrive as a noun phrase. The extractor may
# return it already phrased as an action -- "hold a valid RN licence",
# "possess a PMP certification" -- and wrapping that in "Do you have ..." built
# "Do you have a hold a valid RN license in Ontario?". Stripping the verb lets
# the phrasing below pick its own, which is the whole point of this function.
#
# Only possession verbs belong here. "maintain" or "obtain" describe a duty of
# the role rather than something the candidate holds, so removing them would
# change what is being asked.
_LEADING_POSSESSION_RE = re.compile(
    r"^(?:hold|holds|holding|possess|possesses|possessing|have|has|having)\s+",
    re.I)

_TRAILING_EXPERIENCE_RE = re.compile(r"\s+experience$", re.I)
_CREDENTIAL_RE = re.compile(
    r"\b(?:degree|licen[sc]e|certification|diploma|doctorate|phd|md|bsc|msc"
    r"|bachelor|master)\b", re.I)
_CURRENCY_RE = re.compile(r"^(?:an?\s+)?(?:active|current|valid)\b", re.I)


def _phrase(requirement: str) -> str:
    """Turn requirement wording into something a person can answer.

    Requirement text carries posting boilerplate — "... is required", "the role
    will ..." — which reads badly inside a question. Strip it, then pick the
    phrasing that fits what is left, so nobody is asked whether they "have
    experience with experience".
    """
    text = requirement.strip().rstrip(".")
    previous = None
    while previous != text:
        previous = text
        text = _BOILERPLATE_RE.sub("", text).strip()
        text = _LEADING_POSSESSION_RE.sub("", text).strip()
    if not text:
        return "Do you have this?"

    text = text[0].lower() + text[1:]
    # "5+ years' experience in X" -> ask about X; the years are not the question.
    text = re.sub(r"^\d+\+?\s*(?:or\s+more\s+)?years?'?\s+experience\s+"
                  r"(?:in|with|of)\s+", "", text, flags=re.I).strip()

    # "... dose-escalation experience" -> ask about the subject, not the noun.
    trailing = _TRAILING_EXPERIENCE_RE.search(text)
    if trailing and not text.startswith("experience"):
        return f"Do you have experience with {text[:trailing.start()]}?"

    if text.startswith("experience"):
        return f"Do you have {text}?"
    # A credential is something you hold, not something you have experience with.
    if _CURRENCY_RE.match(text) or _CREDENTIAL_RE.search(text):
        verb = "hold" if _CURRENCY_RE.match(text) else "have"
        if not re.match(r"^(?:an?|the|your)\s+", text):
            text = ("an " if text[0] in "aeiou" else "a ") + text
        return f"Do you {verb} {text}?"
    return f"Do you have experience with {text}?"


def _why(importance: str, is_gate: bool) -> str:
    if is_gate:
        return ("This is a hard requirement. Your resume does not mention it, "
                "which is not the same as you lacking it — but an employer "
                "cannot tell the difference.")
    if importance == Importance.MUST_HAVE.value:
        return ("This is a must-have and nothing in your resume demonstrates "
                "it. If you have done it, saying so is the single biggest "
                "improvement available.")
    return "Your resume does not currently demonstrate this."


def build_questions(result: MatchResult, limit: int = MAX_QUESTIONS
                    ) -> list[OpenQuestion]:
    """The few gaps worth asking about, heaviest first."""
    if result.status != "OK":
        return []

    from evidence_engine.api import _normalized_weights

    weights = _normalized_weights(result)
    matches = {m.requirement_id: m for m in result.matches}

    candidates: list[tuple[float, OpenQuestion]] = []
    for req in result.requirements:
        match = matches.get(req.requirement_id)
        if match is None or match.status not in _ASKABLE_STATUSES:
            continue
        if req.importance not in _ASKABLE_IMPORTANCE:
            continue
        # A gate carries no fit weight by design, so give it the top rank
        # explicitly rather than letting a zero weight bury it.
        weight = 1.0 if req.hard_gate else weights.get(req.requirement_id, 0.0)
        candidates.append((weight, OpenQuestion(
            requirement_id=req.requirement_id,
            requirement=req.normalized_text,
            importance=req.importance.value,
            weight=round(weight, 4),
            question=_phrase(req.normalized_text),
            why_it_matters=_why(req.importance.value, req.hard_gate),
        )))

    candidates.sort(key=lambda pair: (-pair[0], pair[1].requirement_id))
    return [question for _, question in candidates[:limit]]


def validate_answers(
    result: MatchResult, answers: list[GapAnswer]
) -> tuple[list[GapAnswer], list[str]]:
    """Keep only answers that belong to this exact resume and job.

    An answer carries both input digests, so one given about a different
    application cannot be replayed here to unlock a rewrite.
    """
    known = {r.requirement_id for r in result.requirements}
    accepted: list[GapAnswer] = []
    rejected: list[str] = []
    for answer in answers:
        if answer.resume_sha256 != result.resume_sha256:
            rejected.append(
                f"{answer.requirement_id}: answer was given about a different resume")
        elif answer.job_sha256 != result.job_sha256:
            rejected.append(
                f"{answer.requirement_id}: answer was given about a different job")
        elif answer.requirement_id not in known:
            rejected.append(
                f"{answer.requirement_id}: no such requirement in this match")
        else:
            accepted.append(answer)
    return accepted, rejected


def apply_answers(
    result: MatchResult, answers: list[GapAnswer]
) -> tuple[list[RewriteRecommendation], list[str]]:
    """Resolve recommendations in light of what the candidate confirmed.

    Returns the updated recommendations and any rejected answers. The score is
    deliberately untouched: an answer changes what the writer is *allowed to
    look for*, never what the evidence *says*.
    """
    accepted, rejected = validate_answers(result, answers)
    by_requirement = {a.requirement_id: a for a in accepted}
    requirements = {r.requirement_id: r for r in result.requirements}

    updated: list[RewriteRecommendation] = []
    for rec in result.rewrite_recommendations:
        answer = by_requirement.get(rec.requirement_id)
        if answer is None:
            updated.append(rec)
            continue
        name = requirements[rec.requirement_id].normalized_text \
            if rec.requirement_id in requirements else rec.requirement_id

        if answer.answer is AnswerChoice.YES:
            detail = answer.detail.strip()
            updated.append(rec.model_copy(update={
                "type": RewriteType.SURFACE_HIDDEN_EVIDENCE,
                "priority": "HIGH",
                # Confirmed by the person it describes, so the writer may now
                # surface it. It still has to be written truthfully, and the
                # rewritten resume is re-matched before it scores.
                "requires_truth_confirmation": False,
                "reason": f"{name}: confirmed by the candidate",
                "recommendation": (
                    f"You confirmed experience with {name}. Add it to the role "
                    "where it happened, in your own words."
                    + (f" You described it as: {detail}" if detail else "")),
            }))
        elif answer.answer is AnswerChoice.NO:
            updated.append(rec.model_copy(update={
                # Only reachable because the candidate said so. The engine may
                # never infer this from silence.
                "type": RewriteType.TRUE_QUALIFICATION_GAP,
                "priority": "LOW",
                "requires_truth_confirmation": False,
                "reason": f"{name}: confirmed absent by the candidate",
                "recommendation": (
                    f"You confirmed you do not have {name}. Do not add it. "
                    "Consider whether this role is the right target, or treat "
                    "it as something to build toward."),
            }))
        else:
            updated.append(rec.model_copy(update={
                "reason": f"{name}: the candidate was unsure",
                "recommendation": (
                    f"{name} is still unconfirmed. Nothing will be written "
                    "about it until you can say one way or the other."),
            }))
    return updated, rejected
