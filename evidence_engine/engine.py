# evidence_engine/engine.py
"""Pipeline orchestration (spec §5.7). LLM extracts/judges; code calculates."""
from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from typing import Optional

from evidence_engine.audit import (
    ResultCache, append_record, input_fingerprint, match_id_for,
)
from evidence_engine.bias import exclusion_warnings, screen_requirements
from evidence_engine.evidence.judge import judge_requirement
from evidence_engine.evidence.relationships import load_relation_map
from evidence_engine.llm import (
    _MAX_OUTPUT_TOKENS_EXTRACTOR, AnthropicClient, HOSTED_MODEL, LLMClient,
    build_client, model_id_for, resolve_model_specs,
)
from evidence_engine.models import (
    EligibilityResult, EligibilityStatus, EvidenceStatus, MatchResult,
    Relationship, RequirementMatch, ScoreBreakdown,
)
from evidence_engine.parsing.dates import NOW_INDEX
from evidence_engine.parsing.job import parse_job_metadata
from evidence_engine.parsing.resume import parse_resume
from evidence_engine.policy import load_policy
from evidence_engine.recommendations.rewrite import generate_recommendations
from evidence_engine.requirements.extractor import (
    ExtractionError, extract_requirements,
)
from evidence_engine.retrieval.dense import EMBEDDING_MODEL_ID
from evidence_engine.retrieval.fusion import retrieve_candidates
from evidence_engine.scoring.aggregate import calculate_overall_score
from evidence_engine.scoring.eligibility import apply_hard_constraints
from evidence_engine.scoring.experience import calculate_durations
from evidence_engine.scoring.requirement import (
    calculate_requirement_score, calculate_span_evidence_score,
    contributing_evidence, derive_status,
)
from evidence_engine.scoring.role_similarity import calculate_role_similarity
from text_extractor import extract_text

_MANDATORY_WARNING = (
    "No conclusion about an unstated qualification should be interpreted "
    "as proof that the candidate lacks it."
)


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _versions(
    policy,
    relationships_version: str,
    extractor_model: str = HOSTED_MODEL,
    judge_model: str = HOSTED_MODEL,
) -> dict:
    """Version strings that define this evaluation's identity.

    The two model fields must name the models that actually ran. They feed
    ``input_fingerprint``, so recording a constant here would give two
    different models the same cache key and let a cheap-model score be served
    as a frontier-model one.
    """
    return {
        "scoring_policy_version": policy.version,
        "parser_version": policy.parser_version,
        "prompt_version": policy.prompt_version,
        "extractor_model": extractor_model,
        "judge_model": judge_model,
        "embedding_model": EMBEDDING_MODEL_ID,
        "relationships_version": relationships_version,
    }


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _error_result(
    error: str, policy, jd_text: str, resume_text: str,
    versions: Optional[dict] = None, now_index: int = NOW_INDEX,
) -> MatchResult:
    versions = versions or _versions(policy, load_relation_map().version)
    elig = EligibilityResult(status=EligibilityStatus.UNVERIFIED, gates=[])
    resume_sha, job_sha = _sha256(resume_text), _sha256(jd_text)
    fingerprint = input_fingerprint(resume_sha, job_sha, versions, now_index)
    return MatchResult(
        match_id=match_id_for(fingerprint), status="ERROR", error=error,
        resume_sha256=resume_sha, job_sha256=job_sha,
        created_at=_now_iso(), evaluated_month_index=now_index,
        eligibility=elig,
        score_breakdown=ScoreBreakdown(
            eligibility_status=elig.status, qualification_evidence_score=0.0,
            evidence_quality=0.0, must_have_score=0.0,
            scoring_policy_version=policy.version),
        warnings=[_MANDATORY_WARNING],
        **versions,
    )


def match_resume_to_job(
    resume_path: str,
    jd_text: str,
    llm: LLMClient | None = None,
    policy=None,
    use_dense: bool = True,
    now_index: int = NOW_INDEX,
    cache: ResultCache | None = None,
    audit_path=None,
) -> MatchResult:
    """Match a resume file against a job description.

    Thin wrapper over :func:`match_texts` that handles DOCX/PDF extraction.
    """
    return match_texts(extract_text(resume_path), jd_text, llm=llm,
                       policy=policy, use_dense=use_dense, now_index=now_index,
                       cache=cache, audit_path=audit_path)


def match_texts(
    resume_text: str,
    jd_text: str,
    llm: LLMClient | None = None,
    policy=None,
    use_dense: bool = True,
    now_index: int = NOW_INDEX,
    cache: ResultCache | None = None,
    audit_path=None,
) -> MatchResult:
    """Match resume text against a job description.

    ``now_index`` is stamped into the result so recency decay and open-ended
    role durations replay identically later. ``cache``, when supplied, is keyed
    on the inputs and every version string, so a policy or prompt change is a
    miss rather than a stale hit.
    """
    policy = policy or load_policy()
    relationships = load_relation_map()  # built once; reused for every judgment

    # Resolve *which* models will run before fingerprinting. Reading the
    # configuration is free; constructing a client is not, so the cache can
    # still short-circuit without touching the network.
    extractor_spec = judge_spec = None
    if llm is not None:
        # An injected client serves both roles. Record what it actually is
        # rather than the default, so a stub or a cheaper model never files its
        # judgments under the frontier model's identity.
        extractor_model = judge_model = getattr(llm, "model_id", HOSTED_MODEL)
    else:
        extractor_spec, judge_spec = resolve_model_specs()
        try:
            extractor_model = model_id_for(extractor_spec)
            judge_model = model_id_for(judge_spec)
        except ValueError as exc:
            # Misconfiguration is unavailability, not a fit rejection.
            return _error_result(f"LLM unavailable: {exc}", policy, jd_text,
                                 resume_text, None, now_index)

    versions = _versions(policy, relationships.version, extractor_model,
                         judge_model)
    resume_sha, job_sha = _sha256(resume_text), _sha256(jd_text)
    fingerprint = input_fingerprint(resume_sha, job_sha, versions, now_index)

    if cache is not None:
        cached = cache.get(fingerprint)
        if cached is not None:
            return cached

    def fail(error: str) -> MatchResult:
        return _error_result(error, policy, jd_text, resume_text, versions,
                             now_index)

    if llm is None:
        try:
            extractor_llm = build_client(
                extractor_spec, max_output_tokens=_MAX_OUTPUT_TOKENS_EXTRACTOR)
            # One client when both roles share a spec, so the common case does
            # not open two connections. The shared client carries the larger
            # extractor budget, which is safe: max_tokens is a ceiling, and
            # judging emits one small verdict per requirement regardless. The
            # reverse would not be safe -- it is what capped extraction.
            judge_llm = (extractor_llm if judge_spec == extractor_spec
                         else build_client(judge_spec))
        except Exception as exc:
            return fail(f"LLM unavailable: {exc}")

        # The fingerprint above was computed from what the configuration said
        # would run. Re-derive it from what was actually built if they differ:
        # a client reporting a different model would otherwise file its
        # judgments under the predicted model's identity and share its cache
        # entry — the exact substitution the fingerprint exists to prevent.
        actually = (getattr(extractor_llm, "model_id", extractor_model),
                    getattr(judge_llm, "model_id", judge_model))
        if actually != (extractor_model, judge_model):
            extractor_model, judge_model = actually
            versions = _versions(policy, relationships.version,
                                 extractor_model, judge_model)
            fingerprint = input_fingerprint(resume_sha, job_sha, versions,
                                            now_index)
            if cache is not None:
                cached = cache.get(fingerprint)
                if cached is not None:
                    return cached
    else:
        extractor_llm = judge_llm = llm

    resume = parse_resume(resume_text)
    warnings = list(resume.warnings) + [_MANDATORY_WARNING]

    try:
        requirements = extract_requirements(jd_text, extractor_llm)
    except ExtractionError as exc:
        return fail(str(exc))
    except Exception as exc:  # LLM call failure (auth, network) — fail closed
        return fail(f"LLM unavailable: {exc}")

    # Protected attributes never reach the feature space; the refusal is logged.
    requirements, excluded = screen_requirements(requirements)
    warnings.extend(exclusion_warnings(excluded))
    if not requirements:
        return fail("no scoreable requirements remain after "
                    "protected-attribute screening")

    spans_by_id = {s.evidence_span_id: s for s in resume.evidence_spans}
    matches: dict[str, RequirementMatch] = {}
    all_judgments = []
    weak_evidence: dict[str, list[str]] = {}

    try:
        for req in requirements:
            candidates = retrieve_candidates(
                req.normalized_text, resume.evidence_spans, policy,
                use_dense=use_dense)
            judgments = judge_requirement(
                req, candidates, relationships, judge_llm,
                review_threshold=policy.judge_review_threshold)
            all_judgments.extend(judgments)

            req_durations = calculate_durations(
                resume, judgments, spans_by_id, policy, now_index)
            score = calculate_requirement_score(
                req, judgments, req_durations.strict_relevant_years,
                policy, now_index, spans_by_id)

            # Cite exactly the spans that produced the score, not every span
            # that scored above zero.
            contributing = contributing_evidence(judgments, policy, spans_by_id)
            scored = [(j, calculate_span_evidence_score(j, policy))
                      for j in judgments]
            best = contributing[0][0] if contributing else None

            weak_threshold = policy.status_thresholds["weak"]
            weak_evidence[req.requirement_id] = [
                j.evidence_span_id for j, s in scored
                if s is not None and 0 < s <= weak_threshold
                and spans_by_id.get(j.evidence_span_id) is not None
            ][:3]

            status = derive_status(score, [j.relationship for j in judgments], policy)

            # Contradictory evidence scores zero, so it never appears in the
            # contributing list — but it is the single most important thing to
            # show. A candidate told "rejected" is entitled to see the line
            # that did it.
            contradicting = [j for j in judgments
                             if j.relationship == Relationship.CONTRADICTORY]
            if status == EvidenceStatus.CONTRADICTORY_EVIDENCE and contradicting:
                cited = [j.evidence_span_id for j in contradicting]
                best = contradicting[0]
            else:
                cited = [j.evidence_span_id for j, _ in contributing]

            matches[req.requirement_id] = RequirementMatch(
                requirement_id=req.requirement_id,
                status=status,
                score=round(score, 4) if score is not None else None,
                relationship=best.relationship if best else None,
                evidence_span_ids=cited,
                reason=best.reason if best else
                       "No resume span establishes this requirement.",
                gap=None if status == EvidenceStatus.STRONG_EVIDENCE else
                    f"The resume does not demonstrate: {req.normalized_text}.",
                judge_confidence=best.confidence if best else 0.0,
                citations_verified=True,
            )
    except Exception as exc:  # JudgeError or unexpected — fail closed
        return fail(str(exc))

    eligibility = apply_hard_constraints(requirements, matches, policy)
    durations = calculate_durations(resume, all_judgments, spans_by_id,
                                    policy, now_index)
    metadata = parse_job_metadata(jd_text)
    core_texts = [r.normalized_text for r in requirements
                  if r.importance.value in {"MUST_HAVE", "CORE"}]
    role_sim = calculate_role_similarity(
        metadata.title or "", core_texts, resume, policy)
    breakdown = calculate_overall_score(
        requirements, matches, role_sim, durations, eligibility,
        resume.parse_quality, policy)

    strong = sorted(rid for rid, m in matches.items()
                    if m.status == EvidenceStatus.STRONG_EVIDENCE)
    partial = sorted(rid for rid, m in matches.items()
                     if m.status in {EvidenceStatus.MODERATE_EVIDENCE,
                                     EvidenceStatus.WEAK_EVIDENCE})
    missing = sorted(rid for rid, m in matches.items()
                     if m.status in {EvidenceStatus.NO_EVIDENCE_FOUND,
                                     EvidenceStatus.CONTRADICTORY_EVIDENCE})

    recommendations = generate_recommendations(
        requirements, matches, spans_by_id, weak_evidence)

    result = MatchResult(
        match_id=match_id_for(fingerprint), status="OK",
        resume_sha256=resume_sha, job_sha256=job_sha,
        created_at=_now_iso(), evaluated_month_index=now_index,
        parse_quality=resume.parse_quality,
        job_title=metadata.title, job_company=metadata.company,
        job_location=metadata.location,
        eligibility=eligibility,
        score_breakdown=breakdown,
        requirements=requirements,
        matches=[matches[r.requirement_id] for r in requirements],
        evidence_spans=resume.evidence_spans,
        judgments=all_judgments,
        durations=durations,
        strong_evidence=strong, partial_evidence=partial,
        missing_evidence=missing,
        rewrite_recommendations=recommendations,
        excluded_requirements=excluded,
        warnings=warnings,
        **versions,
    )

    if cache is not None:
        cache.put(fingerprint, result)
    if audit_path is not None:
        append_record(result, audit_path)
    return result
