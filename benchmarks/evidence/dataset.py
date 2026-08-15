# benchmarks/evidence/dataset.py
"""Benchmark dataset schema and loader.

The spec's annotation design is deliberately richer than a single fit label:
whole-resume ranking alone cannot tell you whether the engine found the *right
evidence* for the *right requirement*. Requirement-level and span-level gold
annotations are therefore optional per pair, and metrics that need them are
reported only over the annotated subset rather than silently imputed.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

from pydantic import BaseModel, Field

DATA_DIR = Path(__file__).resolve().parent / "data"

# 0 = no fit, 1 = weak, 2 = partial, 3 = good, 4 = strong
LABEL_SCALE = {0: "no fit", 1: "weak", 2: "partial", 3: "good", 4: "strong"}


class GoldRequirement(BaseModel):
    """Expert annotation of one atomic requirement in a job description."""
    normalized_text: str
    importance: str  # HARD_GATE | MUST_HAVE | CORE | PREFERRED | OPTIONAL
    hard_gate: bool = False


class GoldEvidence(BaseModel):
    """Expected evidence for one requirement in one resume."""
    requirement_text: str
    # Verbatim resume excerpts a reviewer accepted as evidence. Empty means
    # the reviewer found none — which is a gold label, not missing data.
    span_texts: list[str] = Field(default_factory=list)
    status: Optional[str] = None       # STRONG_EVIDENCE | ... | NO_EVIDENCE_FOUND
    relationship: Optional[str] = None


class BenchmarkJob(BaseModel):
    job_id: str
    text: str
    domain: str = "unspecified"
    gold_requirements: list[GoldRequirement] = Field(default_factory=list)
    gold_eligibility: Optional[str] = None  # PASS | FAIL | UNVERIFIED


class BenchmarkResume(BaseModel):
    resume_id: str
    text: str


class BenchmarkPair(BaseModel):
    job_id: str
    resume_id: str
    label: int  # 0..4, expert fit judgement
    gold_eligibility: Optional[str] = None
    gold_evidence: list[GoldEvidence] = Field(default_factory=list)
    notes: str = ""


class BenchmarkDataset(BaseModel):
    name: str
    version: str
    description: str = ""
    jobs: list[BenchmarkJob]
    resumes: list[BenchmarkResume]
    pairs: list[BenchmarkPair]

    def job(self, job_id: str) -> BenchmarkJob:
        return next(j for j in self.jobs if j.job_id == job_id)

    def resume(self, resume_id: str) -> BenchmarkResume:
        return next(r for r in self.resumes if r.resume_id == resume_id)

    def pairs_for(self, job_id: str) -> list[BenchmarkPair]:
        return [p for p in self.pairs if p.job_id == job_id]

    def validate_integrity(self) -> list[str]:
        """Structural problems that would make results meaningless."""
        errors: list[str] = []
        job_ids = {j.job_id for j in self.jobs}
        resume_ids = {r.resume_id for r in self.resumes}
        if len(job_ids) != len(self.jobs):
            errors.append("duplicate job_id")
        if len(resume_ids) != len(self.resumes):
            errors.append("duplicate resume_id")
        seen: set[tuple[str, str]] = set()
        for pair in self.pairs:
            if pair.job_id not in job_ids:
                errors.append(f"pair references unknown job {pair.job_id}")
            if pair.resume_id not in resume_ids:
                errors.append(f"pair references unknown resume {pair.resume_id}")
            if not 0 <= pair.label <= 4:
                errors.append(
                    f"{pair.job_id}/{pair.resume_id}: label {pair.label} "
                    "outside the 0-4 scale")
            key = (pair.job_id, pair.resume_id)
            if key in seen:
                errors.append(f"duplicate pair {key}")
            seen.add(key)
            if pair.resume_id not in resume_ids:
                continue  # already reported; excerpt checks need the resume
            for evidence in pair.gold_evidence:
                resume_text = self.resume(pair.resume_id).text
                for excerpt in evidence.span_texts:
                    if excerpt not in resume_text:
                        errors.append(
                            f"{pair.resume_id}: gold excerpt not present "
                            f"verbatim: {excerpt!r}")
        for job in self.jobs:
            if len(self.pairs_for(job.job_id)) < 2:
                errors.append(
                    f"{job.job_id}: needs at least 2 resumes to rank")
        return errors


def load_dataset(path: str | Path | None = None) -> BenchmarkDataset:
    target = Path(path) if path else DATA_DIR / "seed_v1.json"
    return BenchmarkDataset(**json.loads(target.read_text(encoding="utf-8")))
