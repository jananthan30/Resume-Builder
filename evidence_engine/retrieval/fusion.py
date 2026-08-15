# evidence_engine/retrieval/fusion.py
"""Reciprocal rank fusion + near-duplicate removal + candidate assembly."""
from __future__ import annotations

import difflib

from evidence_engine.models import EvidenceSpan
from evidence_engine.retrieval.lexical import LexicalIndex
from evidence_engine.retrieval.dense import VectorIndex, sbert_available


def reciprocal_rank_fusion(rankings: list[list[str]], k: int = 60) -> list[str]:
    scores: dict[str, float] = {}
    for ranking in rankings:
        for rank, sid in enumerate(ranking):
            scores[sid] = scores.get(sid, 0.0) + 1.0 / (k + rank + 1)
    return sorted(scores, key=lambda s: scores[s], reverse=True)


def _norm(text: str) -> str:
    return " ".join(text.lower().split()).rstrip(".")


def text_similarity(a: str, b: str) -> float:
    """Normalized similarity of two evidence texts, 0..1."""
    return difflib.SequenceMatcher(None, _norm(a), _norm(b)).ratio()


def deduplicate(
    span_ids: list[str],
    spans_by_id: dict[str, EvidenceSpan],
    ratio: float = 0.90,
) -> list[str]:
    kept: list[str] = []
    kept_texts: list[str] = []
    for sid in span_ids:
        text = _norm(spans_by_id[sid].text)
        if any(difflib.SequenceMatcher(None, text, t).ratio() >= ratio
               for t in kept_texts):
            continue
        kept.append(sid)
        kept_texts.append(text)
    return kept


def retrieve_candidates(
    query: str,
    spans: list[EvidenceSpan],
    policy,
    use_dense: bool = True,
) -> list[EvidenceSpan]:
    cfg = policy.retrieval
    lex = LexicalIndex(spans).search(query, top_k=cfg["lexical_top_k"])
    dense: list[str] = []
    if use_dense and sbert_available():
        dense = VectorIndex(spans).search(query, top_k=cfg["dense_top_k"])
    fused = reciprocal_rank_fusion([lex, dense], k=cfg["rrf_k"])
    by_id = {s.evidence_span_id: s for s in spans}
    kept = deduplicate(fused, by_id, ratio=cfg["dedup_ratio"])
    return [by_id[sid] for sid in kept[: cfg["final_top_k"]]]
