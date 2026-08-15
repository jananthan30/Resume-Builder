# evidence_engine/models.py
"""Pydantic v2 models for the evidence matching engine (spec §5)."""
from __future__ import annotations

from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field


class RequirementType(str, Enum):
    EXPERIENCE = "experience"
    RESPONSIBILITY = "responsibility"
    SKILL = "skill"
    DOMAIN = "domain"
    DEGREE = "degree"
    LICENSE = "license"
    CERTIFICATION = "certification"
    MINIMUM_YEARS = "minimum_years"
    SENIORITY = "seniority"
    LEADERSHIP = "leadership"
    TOOL = "tool"
    THERAPEUTIC_AREA = "therapeutic_area"
    REGULATORY_KNOWLEDGE = "regulatory_knowledge"
    RESEARCH = "research"
    PUBLICATION = "publication"
    COMMUNICATION = "communication"
    LOCATION = "location"
    TRAVEL = "travel"
    WORK_AUTHORIZATION = "work_authorization"
    OTHER = "other"


class Importance(str, Enum):
    HARD_GATE = "HARD_GATE"
    MUST_HAVE = "MUST_HAVE"
    CORE = "CORE"
    PREFERRED = "PREFERRED"
    OPTIONAL = "OPTIONAL"


class ParseQuality(str, Enum):
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"


class Relationship(str, Enum):
    EXACT_EXPLICIT = "EXACT_EXPLICIT"
    EQUIVALENT_SYNONYM = "EQUIVALENT_SYNONYM"
    ABBREVIATION_EQUIVALENT = "ABBREVIATION_EQUIVALENT"
    CHILD_SUPPORTS_PARENT = "CHILD_SUPPORTS_PARENT"
    ADJACENT_TRANSFERABLE = "ADJACENT_TRANSFERABLE"
    CONTEXTUAL_IMPLICATION = "CONTEXTUAL_IMPLICATION"
    UNSUPPORTED_INFERENCE = "UNSUPPORTED_INFERENCE"
    CONTRADICTORY = "CONTRADICTORY"
    NONE = "NONE"
    UNDETERMINED = "UNDETERMINED"


class EvidenceStatus(str, Enum):
    STRONG_EVIDENCE = "STRONG_EVIDENCE"
    MODERATE_EVIDENCE = "MODERATE_EVIDENCE"
    WEAK_EVIDENCE = "WEAK_EVIDENCE"
    NO_EVIDENCE_FOUND = "NO_EVIDENCE_FOUND"
    CONTRADICTORY_EVIDENCE = "CONTRADICTORY_EVIDENCE"
    UNDETERMINED = "UNDETERMINED"


class EligibilityStatus(str, Enum):
    PASS = "PASS"
    FAIL = "FAIL"
    UNVERIFIED = "UNVERIFIED"


class RewriteType(str, Enum):
    CLARIFY_EXISTING_EVIDENCE = "CLARIFY_EXISTING_EVIDENCE"
    QUANTIFY_EXISTING_EVIDENCE = "QUANTIFY_EXISTING_EVIDENCE"
    SURFACE_HIDDEN_EVIDENCE = "SURFACE_HIDDEN_EVIDENCE"
    REORDER_EXISTING_EVIDENCE = "REORDER_EXISTING_EVIDENCE"
    ADD_MISSING_CONTEXT = "ADD_MISSING_CONTEXT"
    TRUE_QUALIFICATION_GAP = "TRUE_QUALIFICATION_GAP"


class Requirement(BaseModel):
    requirement_id: str
    source_text: str
    source_start_char: int
    source_end_char: int
    normalized_text: str
    type: RequirementType
    importance: Importance
    hard_gate: bool = False
    weight_raw: float = 1.0
    minimum_years: Optional[float] = None
    concepts: list[str] = Field(default_factory=list)
    extraction_confidence: float = 1.0


class EvidenceSpan(BaseModel):
    evidence_span_id: str
    text: str
    start_char: int
    end_char: int
    section: str
    experience_id: Optional[str] = None
    start_date: Optional[str] = None  # YYYY-MM
    end_date: Optional[str] = None    # YYYY-MM; None when is_current
    is_current: bool = False


class Experience(BaseModel):
    experience_id: str
    title: str
    company: str
    start_date: Optional[str] = None
    end_date: Optional[str] = None
    is_current: bool = False
    description_span_ids: list[str] = Field(default_factory=list)


class Resume(BaseModel):
    raw_text: str
    parse_quality: ParseQuality
    experiences: list[Experience] = Field(default_factory=list)
    evidence_spans: list[EvidenceSpan] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class Judgment(BaseModel):
    """One LLM evidence judgment for ONE (requirement, span) pair.

    suggested_strength is diagnostic only — deterministic scoring ignores it.
    """
    requirement_id: str
    evidence_span_id: str
    relationship: Relationship
    status: EvidenceStatus
    specificity: float = 0.0
    scope: float = 0.0
    context: float = 0.0
    temporal_relevance: float = 1.0
    confidence: float = 0.0
    reason: str = ""
    suggested_strength: Optional[float] = None
    # Set when the curated relation map overrode the model's claimed
    # relationship; carried into the audit record so downgrades are visible.
    enforcement_note: Optional[str] = None


class ExperienceDurations(BaseModel):
    total_career_years: float = 0.0
    strict_relevant_years: float = 0.0
    adjacent_relevant_years: float = 0.0
    recent_relevant_years: float = 0.0
    longest_continuous_relevant_years: float = 0.0


class RequirementMatch(BaseModel):
    requirement_id: str
    status: EvidenceStatus
    score: Optional[float]  # 0..1; None when UNDETERMINED
    relationship: Optional[Relationship]
    evidence_span_ids: list[str] = Field(default_factory=list)
    reason: str = ""
    gap: Optional[str] = None
    judge_confidence: float = 0.0
    citations_verified: bool = True


class GateResult(BaseModel):
    requirement_id: str
    requirement: str
    status: EligibilityStatus
    reason: str = ""


class EligibilityResult(BaseModel):
    status: EligibilityStatus
    gates: list[GateResult] = Field(default_factory=list)


class ScoreBreakdown(BaseModel):
    eligibility_status: EligibilityStatus
    qualification_evidence_score: float
    evidence_quality: float
    must_have_score: float
    core_score: float = 0.0
    preferred_score: float = 0.0
    role_similarity: float = 0.0
    missing_must_have_ids: list[str] = Field(default_factory=list)
    unverified_gate_ids: list[str] = Field(default_factory=list)
    failed_gate_ids: list[str] = Field(default_factory=list)
    scoring_policy_version: str


class AnswerChoice(str, Enum):
    YES = "yes"
    NO = "no"
    UNSURE = "unsure"


class OpenQuestion(BaseModel):
    """A gap worth interrupting the candidate for.

    NO_EVIDENCE_FOUND says the resume did not demonstrate something; it never
    says the candidate lacks it. Only the candidate can close that gap, so the
    engine asks rather than guessing in either direction.
    """
    requirement_id: str
    requirement: str
    importance: str
    weight: float
    question: str
    why_it_matters: str


class GapAnswer(BaseModel):
    """One answer, bound to the exact resume and job it was given about.

    Both digests are carried so a "yes" from one application cannot be
    replayed against a different one.
    """
    requirement_id: str
    answer: AnswerChoice
    detail: str = ""
    resume_sha256: str
    job_sha256: str


class ExcludedRequirement(BaseModel):
    """A JD line refused as a scoring criterion because it is protected.

    Recorded rather than silently dropped: regulators and candidates are both
    entitled to see what the engine declined to score.
    """
    requirement_id: str
    source_text: str
    category: str
    is_proxy: bool
    reason: str


class RewriteRecommendation(BaseModel):
    recommendation_id: str
    requirement_id: str
    type: RewriteType
    priority: str  # HIGH | MEDIUM | LOW
    supporting_span_ids: list[str] = Field(default_factory=list)
    reason: str
    recommendation: str
    requires_truth_confirmation: bool = True


class MatchResult(BaseModel):
    match_id: str
    status: str = "OK"  # OK | ERROR
    error: Optional[str] = None
    scoring_policy_version: str
    parser_version: str
    extractor_model: str
    judge_model: str
    prompt_version: str
    embedding_model: str
    relationships_version: str
    resume_sha256: str
    job_sha256: str
    created_at: str = ""
    job_title: Optional[str] = None
    job_company: Optional[str] = None
    job_location: Optional[str] = None
    # Month index the result was evaluated at. Recency decay and open-ended
    # role durations depend on "now", so replaying an audit record must use the
    # original evaluation month rather than the wall clock.
    evaluated_month_index: int = 0
    parse_quality: ParseQuality = ParseQuality.HIGH
    eligibility: EligibilityResult
    score_breakdown: ScoreBreakdown
    requirements: list[Requirement] = Field(default_factory=list)
    matches: list[RequirementMatch] = Field(default_factory=list)
    evidence_spans: list[EvidenceSpan] = Field(default_factory=list)
    # Retained so the deterministic score can be recomputed and checked
    # without another LLM call (spec: audits reconstruct every score).
    judgments: list[Judgment] = Field(default_factory=list)
    durations: ExperienceDurations = Field(default_factory=ExperienceDurations)
    strong_evidence: list[str] = Field(default_factory=list)
    partial_evidence: list[str] = Field(default_factory=list)
    missing_evidence: list[str] = Field(default_factory=list)
    rewrite_recommendations: list[RewriteRecommendation] = Field(default_factory=list)
    excluded_requirements: list[ExcludedRequirement] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
