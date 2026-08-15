# evidence_engine/retrieval/dense.py
"""SBERT dense retrieval with SHA-256 disk cache (mirrors ats_scorer pattern)."""
from __future__ import annotations

import hashlib
from pathlib import Path

import numpy as np

from evidence_engine.models import EvidenceSpan

_CACHE_DIR = Path("embed_cache")
_MODEL_NAME = "all-MiniLM-L6-v2"
_model = None
_load_failed = False

EMBEDDING_MODEL_ID = f"sbert-{_MODEL_NAME}"


def sbert_available() -> bool:
    if _load_failed:
        return False
    try:
        import sentence_transformers  # noqa: F401
        return True
    except ImportError:
        return False


def _get_model():
    global _model, _load_failed
    if _model is None and not _load_failed:
        try:
            from sentence_transformers import SentenceTransformer
            _model = SentenceTransformer(_MODEL_NAME)
        except Exception:
            _load_failed = True
    return _model


def embed(text: str) -> np.ndarray | None:
    model = _get_model()
    if model is None:
        return None
    _CACHE_DIR.mkdir(exist_ok=True)
    key = hashlib.sha256(text.encode("utf-8")).hexdigest()
    path = _CACHE_DIR / f"{key}.npy"
    if path.exists():
        return np.load(path)
    vec = model.encode([text], normalize_embeddings=True)[0]
    np.save(path, vec)
    return vec


class VectorIndex:
    def __init__(self, spans: list[EvidenceSpan]):
        self._ids: list[str] = []
        self._matrix: np.ndarray | None = None
        vecs = []
        for s in spans:
            v = embed(s.text)
            if v is not None:
                self._ids.append(s.evidence_span_id)
                vecs.append(v)
        if vecs:
            self._matrix = np.vstack(vecs)

    def search(self, query: str, top_k: int = 8) -> list[str]:
        if self._matrix is None:
            return []
        q = embed(query)
        if q is None:
            return []
        sims = self._matrix @ q  # normalized embeddings → cosine
        order = np.argsort(-sims)[:top_k]
        return [self._ids[i] for i in order if sims[i] > 0]
