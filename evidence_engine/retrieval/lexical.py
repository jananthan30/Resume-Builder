# evidence_engine/retrieval/lexical.py
"""BM25Plus lexical retrieval over evidence spans."""
from __future__ import annotations

import re

from rank_bm25 import BM25Plus

from evidence_engine.models import EvidenceSpan

_TOKEN_RE = re.compile(r"[a-z0-9]+")


def tokenize(text: str) -> list[str]:
    return _TOKEN_RE.findall(text.lower())


class LexicalIndex:
    def __init__(self, spans: list[EvidenceSpan]):
        self._ids = [s.evidence_span_id for s in spans]
        self._bm25 = BM25Plus([tokenize(s.text) for s in spans]) if spans else None

    def search(self, query: str, top_k: int = 8) -> list[str]:
        if self._bm25 is None:
            return []
        scores = self._bm25.get_scores(tokenize(query))
        ranked = sorted(zip(self._ids, scores), key=lambda t: t[1], reverse=True)
        return [sid for sid, sc in ranked[:top_k] if sc > 0]
