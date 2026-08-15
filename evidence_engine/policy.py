# evidence_engine/policy.py
"""Versioned scoring policy loading (spec §7: weights live in config)."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

from pydantic import BaseModel

_CONFIG_DIR = Path(__file__).parent / "config"


class ScoringPolicy(BaseModel):
    version: str
    parser_version: str
    prompt_version: str
    importance_weights: dict[str, float]
    relationship_values: dict[str, float]
    relationship_ceilings: dict[str, float]
    span_base_weights: dict[str, float]
    evidence_combine_alphas: list[float]
    same_role_dedup_ratio: float
    status_thresholds: dict[str, float]
    must_have_caps: dict[str, float]
    must_have_absent_below: float
    must_have_blend: dict[str, float]
    recency: dict
    recent_window_years: int
    judge_review_threshold: float
    role_similarity_weights: dict[str, float]
    retrieval: dict
    low_parse_quality_evidence_cap: float
    unverified_citation_confidence_cap: float


def load_policy(path: Optional[str] = None) -> ScoringPolicy:
    p = Path(path) if path else _CONFIG_DIR / "scoring_v0_1.json"
    return ScoringPolicy(**json.loads(p.read_text()))


def load_relationships(path: Optional[str] = None) -> dict:
    p = Path(path) if path else _CONFIG_DIR / "relationships_v0_1.json"
    return json.loads(p.read_text())
