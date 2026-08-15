# evidence_engine/scoring/role_similarity.py
"""Role similarity diagnostic (v0): responsibility/function/seniority/domain/scope.

No employer prestige. Never blended into qualification_evidence_score.
"""
from __future__ import annotations

import re

from evidence_engine.models import Resume

_TOKEN_RE = re.compile(r"[a-z0-9]+")

_SENIORITY = [
    ("intern", 1), ("trainee", 1), ("junior", 2), ("associate", 2),
    ("coordinator", 2), ("assistant", 2), ("scientist", 3), ("engineer", 3),
    ("analyst", 3), ("senior", 4), ("sr", 4), ("manager", 4), ("lead", 5),
    ("principal", 6), ("staff", 5), ("director", 7), ("head", 7),
    ("vp", 8), ("vice president", 8), ("chief", 9),
]
_LEADERSHIP = ("led", "managed", "directed", "supervised", "mentored", "oversaw")


def _tokens(text: str) -> set[str]:
    return set(_TOKEN_RE.findall(text.lower()))


def _jaccard(a: set[str], b: set[str]) -> float:
    return len(a & b) / len(a | b) if (a | b) else 0.0


def _seniority_of(title: str) -> int | None:
    t = title.lower()
    for key, level in _SENIORITY:
        if key in t:
            return level
    return None


def _similarity(query: str, texts: list[str]) -> float:
    """Max SBERT cosine when available, else max token-Jaccard."""
    from evidence_engine.retrieval.dense import embed
    import numpy as np

    q = embed(query) if query else None
    if q is not None:
        sims = []
        for t in texts:
            v = embed(t)
            if v is not None:
                sims.append(float(np.dot(q, v)))
        if sims:
            return max(0.0, min(1.0, max(sims)))
    q_tok = _tokens(query)
    return max((_jaccard(q_tok, _tokens(t)) for t in texts), default=0.0)


def calculate_role_similarity(
    jd_title: str,
    jd_core_texts: list[str],
    resume: Resume,
    policy,
) -> float:
    if not resume.experiences:
        return 0.0
    w = policy.role_similarity_weights
    spans_by_exp: dict[str, list[str]] = {}
    for s in resume.evidence_spans:
        if s.experience_id:
            spans_by_exp.setdefault(s.experience_id, []).append(s.text)

    exp_blobs = [" ".join(spans_by_exp.get(e.experience_id, [e.title]))
                 for e in resume.experiences]
    exp_titles = [e.title for e in resume.experiences if e.title]

    responsibility = _similarity(" ".join(jd_core_texts) or jd_title, exp_blobs)
    function = _similarity(jd_title, exp_titles) if jd_title and exp_titles else 0.0

    jd_level = _seniority_of(jd_title or "")
    levels = [l for l in (_seniority_of(t) for t in exp_titles) if l is not None]
    if jd_level is None or not levels:
        seniority = 0.6
    else:
        best = min(levels, key=lambda l: abs(l - jd_level))
        seniority = max(0.0, 1.0 - 0.25 * abs(jd_level - best))

    domain = _jaccard(_tokens(" ".join(jd_core_texts) or jd_title),
                      _tokens(resume.raw_text))

    senior_jd = bool(jd_level and jd_level >= 4)
    has_leadership = any(
        term in " ".join(sum(spans_by_exp.values(), [])).lower()
        for term in _LEADERSHIP)
    scope = 1.0 if (senior_jd and has_leadership) else 0.5

    return round(
        w["responsibility"] * responsibility + w["function"] * function
        + w["seniority"] * seniority + w["domain"] * domain + w["scope"] * scope,
        4)
