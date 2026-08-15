# benchmarks/evidence/baselines.py
"""Ablation systems A-F from the spec's benchmark plan.

The comparison exists to answer one question: does requirement-aware evidence
matching actually rank resumes better than keyword counting, or does it just
read better? Each system returns a 0..1 score for one (resume, job) pair; the
runner turns those into ranking metrics.

Systems A-D are offline and deterministic. E and F call an LLM, so the runner
skips them unless one is configured.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable, Optional, Sequence

_TOKEN_RE = re.compile(r"[a-z0-9]+")
_STOPWORDS = {
    "a", "an", "and", "are", "as", "at", "be", "by", "for", "from", "has",
    "have", "in", "is", "it", "of", "on", "or", "our", "that", "the", "this",
    "to", "we", "will", "with", "you", "your", "must", "should", "can",
    "experience", "work", "role", "team", "job", "position", "candidate",
}


def tokenize(text: str) -> list[str]:
    return _TOKEN_RE.findall(text.lower())


def content_terms(text: str) -> set[str]:
    return {t for t in tokenize(text) if t not in _STOPWORDS and len(t) > 2}


@dataclass
class Corpus:
    """All resumes competing for one job. BM25 needs the competing set."""
    resume_texts: Sequence[str]


# --- A: exact keyword overlap ----------------------------------------------

def score_keyword_overlap(resume_text: str, jd_text: str,
                          corpus: Optional[Corpus] = None) -> float:
    """Share of job-description content terms appearing verbatim in the resume.

    This is the baseline the product is replacing, and the one that keyword
    stuffing trivially defeats.
    """
    jd_terms = content_terms(jd_text)
    if not jd_terms:
        return 0.0
    resume_terms = content_terms(resume_text)
    return len(jd_terms & resume_terms) / len(jd_terms)


# --- B: BM25 ----------------------------------------------------------------

def score_bm25(resume_text: str, jd_text: str,
               corpus: Optional[Corpus] = None) -> float:
    """BM25 of the JD as a query against this resume, within its corpus."""
    documents = list(corpus.resume_texts) if corpus else [resume_text]
    if resume_text not in documents:
        documents = documents + [resume_text]
    try:
        from rank_bm25 import BM25Plus
    except ImportError:  # pragma: no cover - dependency is declared
        return score_keyword_overlap(resume_text, jd_text)
    index = BM25Plus([tokenize(doc) for doc in documents])
    scores = index.get_scores(tokenize(jd_text))
    mine = scores[documents.index(resume_text)]
    top = max(scores) if len(scores) else 0.0
    return float(mine / top) if top > 0 else 0.0


# --- C: embedding similarity only -------------------------------------------

def score_embedding(resume_text: str, jd_text: str,
                    corpus: Optional[Corpus] = None) -> float:
    """Whole-document cosine similarity. Blunt, but a fair semantic baseline."""
    from evidence_engine.retrieval.dense import embed, sbert_available
    if not sbert_available():
        return 0.0
    jd_vec, resume_vec = embed(jd_text), embed(resume_text)
    if jd_vec is None or resume_vec is None:
        return 0.0
    return float(max(0.0, min(1.0, float(jd_vec @ resume_vec))))


# --- D: hybrid lexical + dense ----------------------------------------------

def score_hybrid(resume_text: str, jd_text: str,
                 corpus: Optional[Corpus] = None) -> float:
    lexical = score_bm25(resume_text, jd_text, corpus)
    dense = score_embedding(resume_text, jd_text, corpus)
    if dense == 0.0:
        return lexical
    return 0.5 * lexical + 0.5 * dense


# --- E: hybrid retrieval + LLM judge, without requirement decomposition ------

def make_judge_only_scorer(llm) -> Callable[..., float]:
    """Judge the whole JD against retrieved spans, with no requirement rubric.

    This isolates the contribution of requirement decomposition: E and F share
    retrieval and the same judge, and differ only in whether the job is broken
    into independently testable requirements first.
    """
    def score(resume_text: str, jd_text: str,
              corpus: Optional[Corpus] = None) -> float:
        from evidence_engine.evidence.judge import judge_requirement
        from evidence_engine.evidence.relationships import load_relation_map
        from evidence_engine.models import Importance, Requirement, RequirementType
        from evidence_engine.parsing.resume import parse_resume
        from evidence_engine.policy import load_policy
        from evidence_engine.retrieval.fusion import retrieve_candidates
        from evidence_engine.scoring.requirement import (
            calculate_span_evidence_score, combine_independent_evidence,
        )

        policy = load_policy()
        resume = parse_resume(resume_text)
        whole_job = Requirement(
            requirement_id="R0", source_text=jd_text[:200],
            source_start_char=0, source_end_char=min(200, len(jd_text)),
            normalized_text=" ".join(jd_text.split())[:400],
            type=RequirementType.OTHER, importance=Importance.MUST_HAVE)
        candidates = retrieve_candidates(
            whole_job.normalized_text, resume.evidence_spans, policy)
        try:
            judgments = judge_requirement(
                whole_job, candidates, load_relation_map(), llm,
                review_threshold=policy.judge_review_threshold)
        except Exception:
            return 0.0
        scores = [s for s in (calculate_span_evidence_score(j, policy)
                              for j in judgments) if s is not None]
        if not scores:
            return 0.0
        return combine_independent_evidence(scores, policy.evidence_combine_alphas)

    return score


# --- F: the full evidence matching engine -----------------------------------

def ranking_score(result) -> float:
    """Collapse a three-part result into one ordering key, for ranking only.

    The product keeps Eligibility, Qualification Evidence Fit and Evidence
    Quality separate, and the spec is explicit that Fit must not be multiplied
    by Eligibility for display. Ranking is a different question: to place
    candidates in an order you need a total ordering, and a candidate whose
    resume explicitly contradicts a mandatory gate does not belong above one
    who satisfies it.

    FAIL therefore demotes to zero *for ordering*, while the reported Fit is
    untouched. UNVERIFIED never demotes — absence of evidence is not failure.
    """
    if result.status != "OK":
        return 0.0
    if result.eligibility.status.value == "FAIL":
        return 0.0
    return result.score_breakdown.qualification_evidence_score


def make_engine_scorer(llm, now_index: Optional[int] = None,
                       cache=None) -> Callable[..., float]:
    """Rank by the full requirement-aware engine's evidence result."""
    def score(resume_text: str, jd_text: str,
              corpus: Optional[Corpus] = None) -> float:
        import tempfile
        from pathlib import Path

        from evidence_engine.engine import match_resume_to_job
        from evidence_engine.parsing.dates import NOW_INDEX

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "resume.md"
            path.write_text(resume_text, encoding="utf-8")
            result = match_resume_to_job(
                str(path), jd_text, llm=llm,
                now_index=now_index if now_index is not None else NOW_INDEX,
                cache=cache)
        return ranking_score(result)

    return score


OFFLINE_SYSTEMS: dict[str, Callable[..., float]] = {
    "A_keyword_overlap": score_keyword_overlap,
    "B_bm25": score_bm25,
    "C_embedding": score_embedding,
    "D_hybrid": score_hybrid,
}

# The keyword baseline the spec asks the engine to beat.
REFERENCE_SYSTEM = "A_keyword_overlap"


def build_systems(llm=None, now_index: Optional[int] = None,
                  cache=None) -> dict[str, Callable[..., float]]:
    systems = dict(OFFLINE_SYSTEMS)
    if llm is not None:
        systems["E_hybrid_plus_judge"] = make_judge_only_scorer(llm)
        systems["F_evidence_engine"] = make_engine_scorer(llm, now_index, cache)
    return systems
