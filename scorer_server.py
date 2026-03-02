"""
Scorer API Server v3.0 — FastAPI service for ATS and HR resume scoring.

Supports multiple input modes:
1. Raw text:    { "resume_text": "...", "jd_text": "..." }
2. Base64 file: { "resume_file": "<base64>", "resume_filename": "resume.pdf", "jd_text": "..." }
3. File path:   { "resume_path": "...", "jd_path": "..." }  (local mode only)

Features:
- JWT + API key authentication with SQLite-backed user management
- Freemium tier enforcement (5 free scores, then Stripe subscription)
- Rate limiting (configurable per-key and global)
- Response caching (hash-based, 24h TTL)
- CORS support for web frontends
- Batch scoring endpoint
- Score explanation engine
- Stripe billing integration (checkout, webhooks, portal)

Usage:
    python scorer_server.py [--port 8100] [--host 0.0.0.0] [--require-auth] [--cors-origins "*"]
"""

import time
import argparse
import os
import sys
import hashlib

# Load .env file from project root (simple loader, no python-dotenv required)
_env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
if os.path.isfile(_env_path):
    with open(_env_path, "r", encoding="utf-8") as _ef:
        for _line in _ef:
            _line = _line.strip()
            if _line and not _line.startswith("#") and "=" in _line:
                _k, _, _v = _line.partition("=")
                os.environ[_k.strip()] = _v.strip()
import base64
import tempfile
import json
from pathlib import Path
from typing import Optional, List, Dict, Any
from collections import defaultdict
from datetime import datetime

from fastapi import FastAPI, HTTPException, Request, Depends
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

# Ensure project root is on sys.path
PROJECT_ROOT = str(Path(__file__).parent)
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

# ─── Startup: import scorers (triggers model loading) ───
print("Loading scoring models... this takes ~30 seconds on first run.")
_start = time.time()

import ats_scorer
import hr_scorer

_elapsed = time.time() - _start
print(f"Models loaded in {_elapsed:.1f}s")

# ─── Cloud modules (graceful import — works locally without cloud deps) ───
try:
    from cloud.config import settings as cloud_settings
    from cloud.auth import (
        create_user, authenticate_user, get_user_by_id,
        create_api_key as create_user_api_key, validate_api_key as validate_user_api_key,
        log_usage, check_usage_allowed, get_usage_stats,
        create_jwt_token, decode_jwt_token, update_user_tier,
        get_or_create_anonymous_user, get_user_by_stripe_customer_id,
        save_resume as _save_resume_db,
        get_resume as _get_resume_db,
        delete_resume as _delete_resume_db,
    )
    from cloud.billing import (
        is_billing_configured, create_checkout_session,
        handle_webhook_event, create_portal_session,
    )
    CLOUD_AVAILABLE = True
except ImportError:
    CLOUD_AVAILABLE = False

# ─── App ───
app = FastAPI(
    title="Resume Scorer API",
    description="Dual ATS + HR resume scoring with ML models. Supports text, file upload, batch scoring, JWT auth, and Stripe billing.",
    version="3.0.0",
)

_server_start_time = time.time()

# ─── Configuration ───
if CLOUD_AVAILABLE:
    _config = {
        "require_auth": cloud_settings.REQUIRE_AUTH,
        "rate_limit_per_minute": cloud_settings.RATE_LIMIT_PER_MINUTE,
        "rate_limit_per_day": cloud_settings.RATE_LIMIT_PER_DAY,
        "cache_ttl_seconds": cloud_settings.CACHE_TTL_SECONDS,
        "free_tier_total_limit": cloud_settings.FREE_TIER_TOTAL_LIMIT,
    }
else:
    _config = {
        "require_auth": False,
        "rate_limit_per_minute": 60,
        "rate_limit_per_day": 1000,
        "cache_ttl_seconds": 86400,  # 24 hours
        "free_tier_total_limit": 10,
    }

# ─── In-memory stores ───
_api_keys: Dict[str, Dict[str, Any]] = {}  # key -> {tier, daily_count, last_reset}
_rate_limits: Dict[str, List[float]] = defaultdict(list)  # key -> [timestamps]
_score_cache: Dict[str, Dict[str, Any]] = {}  # hash -> {result, timestamp}


# =============================================================================
# REQUEST / RESPONSE MODELS
# =============================================================================

class ScoreRequest(BaseModel):
    """Flexible scoring request — supports text, base64 files, or local file paths."""
    # Text input (preferred for API/SaaS)
    resume_text: Optional[str] = Field(None, description="Raw resume text content")
    jd_text: Optional[str] = Field(None, description="Raw job description text content")

    # Base64 file input (for file uploads via web)
    resume_file: Optional[str] = Field(None, description="Base64-encoded resume file (PDF/DOCX)")
    resume_filename: Optional[str] = Field(None, description="Original filename for format detection")
    jd_file: Optional[str] = Field(None, description="Base64-encoded JD file (PDF/DOCX)")
    jd_filename: Optional[str] = Field(None, description="Original JD filename")

    # File path input (local mode only — backward compatible)
    resume_path: Optional[str] = Field(None, description="Local file path to resume (local mode)")
    jd_path: Optional[str] = Field(None, description="Local file path to JD (local mode)")

    # Options
    include_explanation: bool = Field(False, description="Include score explanation with improvement suggestions")
    domain_hint: Optional[str] = Field(None, description="Force domain: technology, finance, consulting, clinical_research, healthcare, pharma_biotech")


class BatchScoreRequest(BaseModel):
    """Score multiple resumes against one JD, or one resume against multiple JDs."""
    mode: str = Field("many_resumes", description="'many_resumes' or 'many_jds'")

    # Many resumes, one JD
    resumes: Optional[List[str]] = Field(None, description="List of resume texts")
    jd_text: Optional[str] = Field(None, description="Single JD text (for many_resumes mode)")

    # One resume, many JDs
    resume_text: Optional[str] = Field(None, description="Single resume text (for many_jds mode)")
    jds: Optional[List[str]] = Field(None, description="List of JD texts")

    include_ranking: bool = Field(True, description="Include comparative ranking")


class CoverLetterRequest(BaseModel):
    """Generate a cover letter from resume + JD."""
    resume_text: str = Field("", description="Full text of the resume (blank = use saved resume)")
    jd_text: str = Field(..., description="Full text of the job description")
    company_name: str = Field("", description="Company name (auto-detected if empty)")
    job_title: str = Field("", description="Job title (auto-detected if empty)")


class JobDiscoverRequest(BaseModel):
    """Search for jobs and score them against a resume."""
    resume_text: str = Field("", description="Full text of the resume (blank = use saved resume)")
    job_title: str = Field(..., description="Target job title to search for")
    location: str = Field("", description="Geographic location filter")
    remote_only: bool = Field(False, description="Also search remote job boards")
    max_results: int = Field(10, ge=1, le=20, description="Number of top-scored jobs to return")


class FetchJDRequest(BaseModel):
    """Fetch full job description from a listing URL."""
    url: str = Field(..., description="Job listing URL to scrape")
    job_title: str = Field("", description="Job title hint for AI extraction")
    use_ai: bool = Field(True, description="Use Claude Haiku to clean/extract JD from raw page text")


class ResumeUploadRequest(BaseModel):
    """Upload or replace the authenticated user's saved resume."""
    resume_text: Optional[str] = Field(None, description="Plain text resume content")
    resume_file: Optional[str] = Field(None, description="Base64-encoded file (PDF/DOCX/TXT)")
    resume_filename: Optional[str] = Field(None, description="Original filename for format detection")


class APIKeyRequest(BaseModel):
    """Request to create an API key."""
    tier: str = Field("free", description="Tier: free, pro, team")
    label: Optional[str] = Field(None, description="Human-readable label for the key")


# =============================================================================
# INPUT RESOLUTION — Extract text from any input mode
# =============================================================================

def _decode_base64_file(b64_content: str, filename: str) -> str:
    """Decode base64 file, write to temp, extract text, clean up."""
    file_bytes = base64.b64decode(b64_content)
    ext = Path(filename).suffix.lower() if filename else ".txt"

    with tempfile.NamedTemporaryFile(suffix=ext, delete=False) as tmp:
        tmp.write(file_bytes)
        tmp_path = tmp.name

    try:
        if ext == ".pdf":
            text = ats_scorer.extract_text_from_pdf(tmp_path)
        elif ext == ".docx":
            from docx import Document
            doc = Document(tmp_path)
            text = "\n".join(p.text for p in doc.paragraphs)
        else:
            text = file_bytes.decode("utf-8", errors="replace")
        return text
    finally:
        os.unlink(tmp_path)


def resolve_inputs(req: ScoreRequest) -> tuple:
    """
    Resolve resume_text and jd_text from any input mode.
    Priority: text > base64 file > file path.
    Returns (resume_text, jd_text, resume_file_path_or_none).
    """
    resume_text = None
    jd_text = None
    resume_file_path = None  # for format analysis (optional)

    # ── Resume text ──
    if req.resume_text:
        resume_text = req.resume_text
    elif req.resume_file:
        fname = req.resume_filename or "resume.pdf"
        resume_text = _decode_base64_file(req.resume_file, fname)
    elif req.resume_path:
        if not os.path.isfile(req.resume_path):
            raise HTTPException(status_code=400, detail=f"Resume file not found: {req.resume_path}")
        resume_text = _extract_text(req.resume_path)
        resume_file_path = req.resume_path

    # ── JD text ──
    if req.jd_text:
        jd_text = req.jd_text
    elif req.jd_file:
        fname = req.jd_filename or "jd.txt"
        jd_text = _decode_base64_file(req.jd_file, fname)
    elif req.jd_path:
        if not os.path.isfile(req.jd_path):
            raise HTTPException(status_code=400, detail=f"JD file not found: {req.jd_path}")
        jd_text = _extract_text(req.jd_path)

    # ── Validate ──
    if not resume_text:
        raise HTTPException(status_code=400, detail="Resume content required: provide resume_text, resume_file, or resume_path")
    if not jd_text:
        raise HTTPException(status_code=400, detail="JD content required: provide jd_text, jd_file, or jd_path")

    if len(resume_text.strip()) < 50:
        raise HTTPException(status_code=400, detail="Resume text too short (minimum 50 characters)")
    if len(jd_text.strip()) < 50:
        raise HTTPException(status_code=400, detail="JD text too short (minimum 50 characters)")

    return resume_text, jd_text, resume_file_path


def _extract_text(file_path: str) -> str:
    """Extract text from any supported file format."""
    from text_extractor import extract_text
    try:
        return extract_text(file_path)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


def _maybe_autofill_resume(req: ScoreRequest, api_key) -> ScoreRequest:
    """Return updated request with resume from DB if no resume was provided."""
    if req.resume_text or req.resume_file or req.resume_path:
        return req
    if not CLOUD_AVAILABLE or not isinstance(api_key, dict):
        return req
    record = _get_resume_db(api_key["user_id"])
    if record:
        return req.model_copy(update={"resume_text": record["resume_text"]})
    return req


# =============================================================================
# CACHING
# =============================================================================

def _cache_key(resume_text: str, jd_text: str, score_type: str) -> str:
    """Generate cache key from content hash."""
    content = f"{score_type}:{resume_text}:{jd_text}"
    return hashlib.sha256(content.encode()).hexdigest()[:32]


def _get_cached(key: str) -> Optional[Dict]:
    """Get cached result if not expired."""
    if key in _score_cache:
        entry = _score_cache[key]
        age = time.time() - entry["timestamp"]
        if age < _config["cache_ttl_seconds"]:
            return entry["result"]
        else:
            del _score_cache[key]
    return None


def _set_cached(key: str, result: Dict):
    """Cache a scoring result."""
    _score_cache[key] = {"result": result, "timestamp": time.time()}
    # Evict old entries if cache gets large
    if len(_score_cache) > 10000:
        oldest_keys = sorted(_score_cache, key=lambda k: _score_cache[k]["timestamp"])[:5000]
        for k in oldest_keys:
            del _score_cache[k]


# =============================================================================
# AUTHENTICATION & RATE LIMITING
# =============================================================================

def _check_rate_limit(api_key: str) -> bool:
    """Check if request is within rate limits. Returns True if allowed."""
    now = time.time()
    minute_ago = now - 60

    # Clean old entries
    _rate_limits[api_key] = [t for t in _rate_limits[api_key] if t > minute_ago]

    if len(_rate_limits[api_key]) >= _config["rate_limit_per_minute"]:
        return False

    _rate_limits[api_key].append(now)
    return True


async def verify_api_key(request: Request):
    """
    Dependency to verify API key or JWT token (auth only, no usage limit check).

    Use this for non-scoring endpoints (auth, usage, billing) that should always
    be accessible to authenticated users regardless of free tier limits.

    Supports:
    1. JWT Bearer token (Authorization: Bearer <token>)
    2. API key (X-API-Key header or api_key query param)
    3. Legacy in-memory keys (backward compatible)

    Returns auth context dict: {"user_id": int, "tier": str, "email": str} or "anonymous".
    """
    if not _config["require_auth"]:
        return "anonymous"

    # Try JWT Bearer token first
    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("Bearer ") and CLOUD_AVAILABLE:
        token = auth_header[7:]
        payload = decode_jwt_token(token)
        if not payload:
            raise HTTPException(status_code=401, detail="Invalid or expired JWT token.")

        user_id = payload["sub"]
        tier = payload.get("tier", "free")

        if not _check_rate_limit(str(user_id)):
            raise HTTPException(status_code=429, detail="Rate limit exceeded. Try again in 60 seconds.")

        return {"user_id": user_id, "tier": tier, "email": payload.get("email", "")}

    # Try API key (cloud SQLite-backed)
    api_key = request.headers.get("X-API-Key") or request.query_params.get("api_key")

    # Try cloud-backed API key validation
    if api_key and CLOUD_AVAILABLE:
        key_info = validate_user_api_key(api_key)
        if key_info:
            user_id = key_info["user_id"]
            tier = key_info["tier"]

            if not _check_rate_limit(str(user_id)):
                raise HTTPException(status_code=429, detail="Rate limit exceeded. Try again in 60 seconds.")

            return {"user_id": user_id, "tier": tier, "email": key_info.get("email", "")}

    # Fall back to legacy in-memory keys
    if api_key and api_key in _api_keys:
        if not _check_rate_limit(api_key):
            raise HTTPException(status_code=429, detail="Rate limit exceeded. Try again in 60 seconds.")

        key_info = _api_keys[api_key]
        today = datetime.now().strftime("%Y-%m-%d")
        if key_info.get("last_reset") != today:
            key_info["daily_count"] = 0
            key_info["last_reset"] = today

        key_info["daily_count"] = key_info.get("daily_count", 0) + 1
        return api_key

    # Anonymous access — track by session fingerprint (or IP fallback) for free tier enforcement
    if CLOUD_AVAILABLE:
        client_fingerprint = request.headers.get("X-Client-Fingerprint", "")
        if client_fingerprint:
            fingerprint = hashlib.sha256(client_fingerprint.encode()).hexdigest()[:16]
        else:
            client_ip = request.headers.get("X-Forwarded-For", request.client.host if request.client else "unknown")
            if "," in client_ip:
                client_ip = client_ip.split(",")[0].strip()
            fingerprint = hashlib.sha256(client_ip.encode()).hexdigest()[:16]
        anon_user = get_or_create_anonymous_user(fingerprint)

        if not _check_rate_limit(f"anon:{fingerprint}"):
            raise HTTPException(status_code=429, detail="Rate limit exceeded. Try again in 60 seconds.")

        return {"user_id": anon_user["id"], "tier": anon_user["tier"], "email": anon_user["email"], "anonymous": True}

    if api_key:
        raise HTTPException(status_code=403, detail="Invalid API key.")
    raise HTTPException(status_code=401, detail="Authentication required. Pass JWT via Authorization: Bearer <token> or API key via X-API-Key header.")


async def verify_api_key_with_usage(request: Request):
    """
    Dependency for scoring endpoints — authenticates AND enforces free tier limits.
    """
    auth = await verify_api_key(request)

    # Check usage limits for free tier (cloud-backed users only)
    if CLOUD_AVAILABLE and isinstance(auth, dict):
        user_id = auth["user_id"]
        tier = auth.get("tier", "free")
        if tier == "free" and not check_usage_allowed(user_id, tier):
            is_anon = auth.get("anonymous", False)
            if is_anon:
                detail = (
                    f"Free tier limit reached ({_config['free_tier_total_limit']} scores). "
                    f"Sign up at https://resume-scorer-web.streamlit.app for more scores, "
                    f"or upgrade to Pro ($12/month) for unlimited scoring."
                )
            else:
                detail = (
                    f"Free tier limit reached ({_config['free_tier_total_limit']} scores). "
                    f"Upgrade to Pro for unlimited scoring."
                )
            raise HTTPException(status_code=402, detail=detail)

    # Check legacy in-memory key limits
    if isinstance(auth, str) and auth in _api_keys:
        key_info = _api_keys[auth]
        if key_info["tier"] == "free" and key_info.get("daily_count", 0) >= _config["free_tier_total_limit"]:
            raise HTTPException(status_code=402, detail="Free tier limit reached. Upgrade for unlimited scoring.")

    return auth


# =============================================================================
# USAGE LOGGING HELPER
# =============================================================================

def _log_score_usage(auth_context, endpoint: str):
    """Log a scoring request if cloud auth is active."""
    if CLOUD_AVAILABLE and isinstance(auth_context, dict) and "user_id" in auth_context:
        try:
            log_usage(auth_context["user_id"], endpoint)
        except Exception:
            pass  # Don't fail scoring if logging fails


# =============================================================================
# SCORE EXPLANATION ENGINE
# =============================================================================

def generate_ats_explanation(resume_text: str, jd_text: str, ats_result: Dict) -> Dict:
    """
    Generate actionable improvement suggestions from ATS scoring results.

    Returns:
        - top_missing_keywords: Ranked by weight, with suggested placement
        - score_delta_predictions: Estimated score improvement per change
        - bullet_improvement_suggestions: Specific bullet rewrite hints
        - keyword_placement_map: Where to add each keyword (Summary/Skills/Bullets)
    """
    explanation = {
        "top_missing_keywords": [],
        "score_delta_predictions": [],
        "keyword_placement_map": {},
        "quick_wins": [],
        "section_scores": {},
    }

    # Extract missing keywords from the ATS result
    missing_kw = ats_result.get("missing_keywords", [])
    missing_weighted = ats_result.get("missing_weighted_terms", [])
    missing_phrases = ats_result.get("missing_phrases", [])

    # Combine and deduplicate missing terms
    all_missing = []
    seen = set()

    # Weighted terms first (highest impact)
    for term in missing_weighted:
        term_lower = term.lower()
        if term_lower not in seen:
            weight = ats_scorer.PV_KEYWORDS.get(term_lower, 1)
            all_missing.append({"keyword": term, "weight": weight, "source": "industry_term"})
            seen.add(term_lower)

    # Then regular missing keywords
    for term in missing_kw:
        term_lower = term.lower()
        if term_lower not in seen:
            all_missing.append({"keyword": term, "weight": 2, "source": "jd_keyword"})
            seen.add(term_lower)

    # Then missing phrases
    for phrase in missing_phrases:
        phrase_lower = phrase.lower()
        if phrase_lower not in seen:
            all_missing.append({"keyword": phrase, "weight": 2, "source": "industry_phrase"})
            seen.add(phrase_lower)

    # Sort by weight descending, take top 10
    all_missing.sort(key=lambda x: x["weight"], reverse=True)
    top_missing = all_missing[:10]

    # Generate placement suggestions for each missing keyword
    resume_lower = resume_text.lower()
    resume_sections = _identify_resume_sections(resume_text)

    for item in top_missing:
        kw = item["keyword"]
        kw_lower = kw.lower()

        # Determine best placement
        placement = "Core Competencies"  # Default — safest for keyword insertion
        reasoning = "Add to skills section for ATS keyword match"

        # Check if it's an action/verb term — better in bullets
        action_terms = {"managed", "led", "developed", "implemented", "designed", "analyzed", "created", "built"}
        if any(word in kw_lower for word in action_terms):
            placement = "Professional Experience (bullet points)"
            reasoning = "Use as action verb in experience bullets for contextual matching"

        # Check if it's a high-level concept — better in summary
        summary_terms = {"strategy", "leadership", "transformation", "oversight", "vision", "direction"}
        if any(word in kw_lower for word in summary_terms):
            placement = "Professional Summary"
            reasoning = "Incorporate into summary narrative for top-of-resume visibility"

        # Estimate score delta (rough: each keyword is ~1-3% depending on total)
        total_jd_terms = len(ats_result.get("matched_keywords", [])) + len(missing_kw)
        if total_jd_terms > 0:
            delta_estimate = round((item["weight"] / total_jd_terms) * 100 * 0.25, 1)  # 25% weight for keyword match
        else:
            delta_estimate = 1.0

        delta_estimate = max(0.5, min(5.0, delta_estimate))

        explanation["top_missing_keywords"].append({
            "keyword": kw,
            "weight": item["weight"],
            "suggested_placement": placement,
            "reasoning": reasoning,
            "estimated_score_increase": f"+{delta_estimate:.1f}%",
        })

        explanation["score_delta_predictions"].append({
            "action": f"Add '{kw}' to {placement}",
            "estimated_delta": f"+{delta_estimate:.1f}%",
        })

        explanation["keyword_placement_map"][kw] = placement

    # Generate quick wins (easy changes with high impact)
    current_score = ats_result.get("total_score", 0)

    if current_score < 60:
        explanation["quick_wins"].append(
            f"Add top 5 missing keywords to Core Competencies section (estimated +8-12% ATS score)"
        )
    if current_score < 75:
        explanation["quick_wins"].append(
            "Rewrite 3 weakest bullet points to incorporate JD terminology naturally"
        )

    # Check for format-based quick wins
    format_risk = ats_result.get("format_risk_score", 0)
    if format_risk > 30:
        explanation["quick_wins"].append(
            "Fix formatting issues (tables, text boxes, headers) — format risk is high"
        )

    readability = ats_result.get("readability", {})
    if isinstance(readability, dict) and readability.get("flesch_kincaid_grade", 12) > 14:
        explanation["quick_wins"].append(
            "Simplify language — readability is too complex for ATS (target Grade 10-12)"
        )

    # Section-level score breakdown
    explanation["section_scores"] = {
        "keyword_match": ats_result.get("keyword_score", 0),
        "semantic_similarity": ats_result.get("semantic_score", 0),
        "industry_terms": ats_result.get("weighted_score", 0),
        "phrase_match": ats_result.get("phrase_score", 0),
        "bm25": ats_result.get("bm25_score", 0),
        "format_risk": ats_result.get("format_risk_score", 0),
    }

    return explanation


def _identify_resume_sections(text: str) -> Dict[str, str]:
    """Identify sections in resume text for keyword placement suggestions."""
    sections = {}
    current_section = "header"
    current_content = []

    section_patterns = {
        "summary": ["professional summary", "summary", "profile", "objective"],
        "competencies": ["core competencies", "skills", "technical skills", "competencies"],
        "experience": ["professional experience", "work experience", "experience", "employment"],
        "education": ["education", "academic"],
        "certifications": ["certifications", "licensure", "credentials"],
    }

    for line in text.split("\n"):
        line_stripped = line.strip().lower()

        matched_section = None
        for section_name, keywords in section_patterns.items():
            if any(kw in line_stripped for kw in keywords) and len(line_stripped) < 40:
                matched_section = section_name
                break

        if matched_section:
            if current_content:
                sections[current_section] = "\n".join(current_content)
            current_section = matched_section
            current_content = []
        else:
            current_content.append(line)

    if current_content:
        sections[current_section] = "\n".join(current_content)

    return sections


def generate_hr_explanation(hr_result: Dict) -> Dict:
    """Generate actionable HR improvement suggestions."""
    explanation = {
        "priority_improvements": [],
        "strengths_to_emphasize": [],
        "risk_mitigations": [],
    }

    breakdown = hr_result.get("factor_breakdown", {})

    # Identify weakest factors
    factor_labels = {
        "experience": "Experience Fit",
        "skills": "Skills Match",
        "trajectory": "Career Trajectory",
        "impact": "Impact Signals",
        "competitive": "Competitive Edge",
        "job_fit": "Job Fit",
    }

    scored_factors = [(k, v, factor_labels.get(k, k)) for k, v in breakdown.items() if isinstance(v, (int, float))]
    scored_factors.sort(key=lambda x: x[1])

    # Bottom 2 factors = priority improvements
    for key, score, label in scored_factors[:2]:
        suggestion = _get_hr_improvement_suggestion(key, score)
        explanation["priority_improvements"].append({
            "factor": label,
            "current_score": score,
            "suggestion": suggestion,
        })

    # Top 2 factors = strengths to emphasize
    for key, score, label in scored_factors[-2:]:
        explanation["strengths_to_emphasize"].append({
            "factor": label,
            "current_score": score,
            "advice": f"Highlight this strength prominently — it's your competitive advantage",
        })

    # Risk mitigations from penalties
    penalties = hr_result.get("penalties_applied", {})
    for penalty_name, penalty_value in penalties.items():
        if penalty_value > 0:
            mitigation = _get_penalty_mitigation(penalty_name)
            explanation["risk_mitigations"].append({
                "risk": penalty_name.replace("_", " ").title(),
                "penalty": f"-{penalty_value:.1f} points",
                "mitigation": mitigation,
            })

    return explanation


def _get_hr_improvement_suggestion(factor: str, score: float) -> str:
    """Get specific improvement suggestion for an HR factor."""
    suggestions = {
        "experience": "Emphasize total years of relevant experience. Reframe earlier roles to show relevance to the target position.",
        "skills": "Add missing required skills to Core Competencies. Demonstrate skills in bullet points with context (not just listing them).",
        "trajectory": "Highlight career progression with clear title escalation. If lateral moves exist, explain the strategic reasoning.",
        "impact": "Add quantified metrics to 40%+ of bullets (%, $, numbers). Start bullets with strong action verbs (Led, Achieved, Generated).",
        "competitive": "Highlight prestigious companies, certifications, and education. Industry certifications add significant competitive edge.",
        "job_fit": "Tailor resume language to match the specific role and domain. Add domain-specific terminology to Summary and Experience sections.",
    }
    return suggestions.get(factor, "Review this area for improvement opportunities.")


def _get_penalty_mitigation(penalty_name: str) -> str:
    """Get mitigation advice for a specific penalty."""
    mitigations = {
        "job_hopping": "Frame short tenures positively: contract roles, rapid promotions, or startup environment. Add context to resume.",
        "unexplained_gap": "Address gaps proactively: add a brief note (sabbatical, education, consulting). Even one line helps.",
        "recent_instability": "Emphasize the stability and commitment you bring to your next role. Highlight longest tenures prominently.",
        "overqualified": "Tailor your resume to the role level. De-emphasize senior titles if applying to a lateral or lower position.",
    }
    return mitigations.get(penalty_name, "Address this concern proactively in your cover letter or interview preparation.")


# =============================================================================
# ENDPOINTS — v2.0 (text input, caching, explanations)
# =============================================================================

@app.get("/health")
def health():
    """Server status, model availability, and usage stats."""
    return {
        "status": "ok",
        "version": "3.0.0",
        "uptime_seconds": round(time.time() - _server_start_time, 1),
        "models": {
            "spacy": getattr(ats_scorer, "SPACY_AVAILABLE", False),
            "sbert": getattr(ats_scorer, "SBERT_AVAILABLE", False),
            "textstat": getattr(ats_scorer, "TEXTSTAT_AVAILABLE", False),
        },
        "cloud": {
            "available": CLOUD_AVAILABLE,
            "billing_configured": is_billing_configured() if CLOUD_AVAILABLE else False,
        },
        "job_discovery": {
            "adzuna_configured": bool(os.getenv("ADZUNA_APP_ID")) and bool(os.getenv("ADZUNA_APP_KEY")),
        },
        "cache_size": len(_score_cache),
        "auth_required": _config["require_auth"],
    }


@app.post("/score/ats")
def score_ats(req: ScoreRequest, api_key: str = Depends(verify_api_key_with_usage)):
    """ATS score a resume against a job description. Accepts text, base64 files, or file paths."""
    req = _maybe_autofill_resume(req, api_key)
    resume_text, jd_text, file_path = resolve_inputs(req)
    _log_score_usage(api_key, "/score/ats")

    # Check cache
    cache_key = _cache_key(resume_text, jd_text, "ats")
    cached = _get_cached(cache_key)
    if cached and not req.include_explanation:
        cached["_cached"] = True
        return JSONResponse(content=cached)

    try:
        result = ats_scorer.calculate_ats_score(resume_text, jd_text, file_path)
        rating, likelihood, _color = ats_scorer.get_likelihood_rating(result["total_score"])
        result["rating"] = rating
        result["likelihood"] = likelihood

        # Add explanation if requested
        if req.include_explanation:
            result["explanation"] = generate_ats_explanation(resume_text, jd_text, result)

        _set_cached(cache_key, result)
        return JSONResponse(content=result)

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/score/hr")
def score_hr(req: ScoreRequest, api_key: str = Depends(verify_api_key_with_usage)):
    """HR score a resume against a job description. Accepts text, base64 files, or file paths."""
    req = _maybe_autofill_resume(req, api_key)
    resume_text, jd_text, file_path = resolve_inputs(req)
    _log_score_usage(api_key, "/score/hr")

    # Check cache
    cache_key = _cache_key(resume_text, jd_text, "hr")
    cached = _get_cached(cache_key)
    if cached and not req.include_explanation:
        cached["_cached"] = True
        return JSONResponse(content=cached)

    try:
        # Use text-based HR scoring
        candidate = hr_scorer.parse_resume(resume_text)
        jd = hr_scorer.parse_job_description(jd_text)

        # Enhanced JD parsing for job fit
        jd_fit = hr_scorer.extract_job_fit_requirements(jd_text, jd.title)
        jd.therapeutic_areas = jd_fit.therapeutic_areas
        jd.experience_types = jd_fit.experience_types
        jd.required_phases = jd_fit.required_phases
        jd.required_degrees = jd_fit.required_degrees
        jd.preferred_specializations = jd_fit.preferred_specializations
        jd.is_industry_role = jd_fit.is_industry_role

        # Knockout check
        if candidate.total_years_experience < 0.3 * jd.required_years:
            result_obj = hr_scorer.HRScoreResult(
                overall_score=0,
                recommendation="AUTO-REJECT",
                rating_label="Knockout - Insufficient Experience",
                confidence="High (95%)",
                factor_breakdown=hr_scorer.ScoreBreakdown(),
                penalties_applied={},
                strengths=[],
                concerns=[f"Experience knockout: {candidate.total_years_experience:.1f} years vs {jd.required_years:.1f} required"],
                suggested_questions=[],
                candidate_tags=["Knockout"],
                weights_used={},
            )
        else:
            # Component scoring
            scores = hr_scorer.ScoreBreakdown()
            strengths = []
            concerns = []

            exp_score, exp_narrative = hr_scorer.score_experience_trapezoidal(candidate.total_years_experience, jd.required_years)
            scores.experience = exp_score
            if exp_score >= 80: strengths.append(exp_narrative)
            elif exp_score < 60: concerns.append(exp_narrative)

            skills_score, matched, missing = hr_scorer.score_skills_contextual(candidate.skills, candidate.all_bullets, jd.required_skills, jd.raw_text)
            scores.skills = skills_score
            if matched: strengths.append(f"Skills Match: {len(matched)} of {len(matched) + len(missing)} required skills")
            if missing: concerns.append(f"Missing Skills: {', '.join(missing[:3])}")

            traj_score, traj_narrative = hr_scorer.calculate_career_slope(candidate.jobs)
            scores.trajectory = traj_score
            if traj_score >= 90: strengths.append(traj_narrative)
            elif traj_score < 60: concerns.append(traj_narrative)

            impact_score, impact_stats = hr_scorer.score_impact_density(candidate.all_bullets)
            scores.impact = impact_score
            density = impact_stats.get("density", 0)
            if density >= 30: strengths.append(f"High Impact Density: {density:.0f}% of bullets contain metrics/strong verbs")
            elif density < 15: concerns.append(f"Low Impact Quantification: Only {density:.0f}% of bullets contain metrics")

            companies = [j.company for j in candidate.jobs]
            comp_score, prestige_signals = hr_scorer.score_competitive(candidate.education, companies, candidate.certifications)
            scores.competitive = comp_score
            if prestige_signals: strengths.extend(prestige_signals[:2])

            job_fit_score, job_fit_components, job_fit_strengths, job_fit_concerns = hr_scorer.score_job_fit(candidate, jd)
            scores.job_fit = job_fit_score
            strengths.extend(job_fit_strengths[:2])
            concerns.extend(job_fit_concerns[:2])

            f_pattern_score, f_pattern_details = hr_scorer.score_f_pattern_compliance(resume_text, candidate.all_bullets)
            f_pattern_adjustment = 0
            if f_pattern_score >= 80:
                f_pattern_adjustment = 5
                strengths.append(f"Excellent visual format: F-Pattern score {f_pattern_score:.0f}/100")
            elif f_pattern_score >= 60:
                f_pattern_adjustment = 2
            elif f_pattern_score < 40:
                f_pattern_adjustment = -3
                concerns.append(f"Poor visual format: F-Pattern score {f_pattern_score:.0f}/100")

            mode, tags = hr_scorer.detect_edge_cases(candidate, scores.experience, scores.skills)
            candidate.tags.extend(tags)

            weights = hr_scorer.WEIGHT_PROFILES.get("pivot" if mode == "pivot" else jd.seniority_level, hr_scorer.WEIGHT_PROFILES["mid"])

            raw_score = (
                scores.experience * weights["experience"]
                + scores.skills * weights["skills"]
                + scores.trajectory * weights["trajectory"]
                + scores.impact * weights["impact"]
                + scores.competitive * weights["competitive"]
                + scores.job_fit * weights["job_fit"]
            )

            penalty_total, penalty_breakdown, penalty_concerns = hr_scorer.calculate_penalties(candidate.jobs, resume_text, jd)
            concerns.extend(penalty_concerns)

            final_score = max(0, min(100, raw_score - penalty_total + f_pattern_adjustment))

            job_fit_is_low = scores.job_fit < 60
            job_fit_is_marginal = 60 <= scores.job_fit < 75

            if final_score >= 85 and not job_fit_is_low:
                recommendation, rating_label = "STRONG INTERVIEW", "Strong Candidate"
            elif final_score >= 70 and not job_fit_is_low:
                recommendation = "INTERVIEW"
                rating_label = "Competitive (but role may be a stretch)" if job_fit_is_marginal else "Competitive"
            elif final_score >= 55 or (final_score >= 50 and not job_fit_is_low):
                recommendation, rating_label = "MAYBE", "Marginal - Screening Call Recommended"
            elif job_fit_is_low and final_score >= 60:
                recommendation, rating_label = "MAYBE", "Skills strong but Job Fit is weak - STRETCH candidate"
            else:
                recommendation, rating_label = "PASS", "Weak Match"

            avg_other = (scores.experience + scores.skills + scores.trajectory + scores.impact + scores.competitive) / 5
            if scores.job_fit < avg_other - 20:
                candidate.tags.append("STRETCH: Strong general profile but weak fit for THIS specific role")

            questions = hr_scorer.generate_interview_questions(scores, penalty_breakdown, candidate, impact_stats)
            data_completeness = min(100, len(candidate.jobs) * 15 + len(candidate.all_bullets) * 2)
            confidence = f"{'High' if data_completeness > 70 else 'Medium' if data_completeness > 40 else 'Low'} ({data_completeness:.0f}%)"

            result_obj = hr_scorer.HRScoreResult(
                overall_score=round(final_score, 1),
                recommendation=recommendation,
                rating_label=rating_label,
                confidence=confidence,
                factor_breakdown=scores,
                penalties_applied=penalty_breakdown,
                strengths=strengths[:5],
                concerns=concerns[:5],
                suggested_questions=questions[:4],
                candidate_tags=candidate.tags,
                weights_used=weights,
            )

        result = hr_scorer.result_to_dict(result_obj)

        if req.include_explanation:
            result["explanation"] = generate_hr_explanation(result)

        _set_cached(cache_key, result)
        return JSONResponse(content=result)

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/score/both")
def score_both(req: ScoreRequest, api_key: str = Depends(verify_api_key_with_usage)):
    """Combined ATS + HR scoring in a single call. Most efficient for full analysis."""
    req = _maybe_autofill_resume(req, api_key)
    resume_text, jd_text, file_path = resolve_inputs(req)
    _log_score_usage(api_key, "/score/both")

    # Check cache
    cache_key = _cache_key(resume_text, jd_text, "both")
    cached = _get_cached(cache_key)
    if cached and not req.include_explanation:
        cached["_cached"] = True
        return JSONResponse(content=cached)

    try:
        # ATS scoring
        ats_result = ats_scorer.calculate_ats_score(resume_text, jd_text, file_path)
        rating, likelihood, _color = ats_scorer.get_likelihood_rating(ats_result["total_score"])
        ats_result["rating"] = rating
        ats_result["likelihood"] = likelihood

        # HR scoring (reuse parsed text)
        # Use file-based if available for full HR analysis, otherwise text-based
        if file_path:
            hr_result_obj = hr_scorer.calculate_hr_score(file_path, req.jd_path or "")
            # If jd_path not available, fall back to temp file approach
            if not req.jd_path:
                # Write jd_text to temp for HR scorer
                with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False, encoding="utf-8") as tmp:
                    tmp.write(jd_text)
                    jd_tmp = tmp.name
                try:
                    hr_result_obj = hr_scorer.calculate_hr_score(file_path, jd_tmp)
                finally:
                    os.unlink(jd_tmp)
            hr_result = hr_scorer.result_to_dict(hr_result_obj)
        else:
            # Text-based: write both to temp files for HR scorer
            with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False, encoding="utf-8") as tmp_r:
                tmp_r.write(resume_text)
                resume_tmp = tmp_r.name
            with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False, encoding="utf-8") as tmp_j:
                tmp_j.write(jd_text)
                jd_tmp = tmp_j.name
            try:
                hr_result_obj = hr_scorer.calculate_hr_score(resume_tmp, jd_tmp)
                hr_result = hr_scorer.result_to_dict(hr_result_obj)
            finally:
                os.unlink(resume_tmp)
                os.unlink(jd_tmp)

        combined = {
            "ats": ats_result,
            "hr": hr_result,
            "summary": {
                "ats_score": ats_result.get("total_score", 0),
                "hr_score": hr_result.get("overall_score", 0),
                "ats_rating": rating,
                "hr_recommendation": hr_result.get("recommendation", ""),
                "overall_assessment": _overall_assessment(
                    ats_result.get("total_score", 0),
                    hr_result.get("overall_score", 0),
                ),
            },
        }

        if req.include_explanation:
            combined["explanation"] = {
                "ats": generate_ats_explanation(resume_text, jd_text, ats_result),
                "hr": generate_hr_explanation(hr_result),
            }

        _set_cached(cache_key, combined)
        return JSONResponse(content=combined)

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/score/batch")
def score_batch(req: BatchScoreRequest, api_key: str = Depends(verify_api_key_with_usage)):
    """Batch scoring: multiple resumes vs one JD, or one resume vs multiple JDs."""
    results = []

    if req.mode == "many_resumes" and req.resumes and req.jd_text:
        for i, resume in enumerate(req.resumes[:50]):  # Cap at 50
            try:
                ats_result = ats_scorer.calculate_ats_score(resume, req.jd_text)
                rating, likelihood, _ = ats_scorer.get_likelihood_rating(ats_result["total_score"])
                results.append({
                    "index": i,
                    "ats_score": ats_result["total_score"],
                    "rating": rating,
                    "matched_keywords": len(ats_result.get("matched_keywords", [])),
                    "missing_keywords": len(ats_result.get("missing_keywords", [])),
                })
            except Exception as e:
                results.append({"index": i, "error": str(e)})

    elif req.mode == "many_jds" and req.jds and req.resume_text:
        for i, jd in enumerate(req.jds[:50]):  # Cap at 50
            try:
                ats_result = ats_scorer.calculate_ats_score(req.resume_text, jd)
                rating, likelihood, _ = ats_scorer.get_likelihood_rating(ats_result["total_score"])
                results.append({
                    "index": i,
                    "ats_score": ats_result["total_score"],
                    "rating": rating,
                    "matched_keywords": len(ats_result.get("matched_keywords", [])),
                    "missing_keywords": len(ats_result.get("missing_keywords", [])),
                })
            except Exception as e:
                results.append({"index": i, "error": str(e)})
    else:
        raise HTTPException(status_code=400, detail="Invalid batch request. Provide resumes+jd_text or resume_text+jds.")

    # Add ranking if requested
    if req.include_ranking:
        valid = [r for r in results if "ats_score" in r]
        valid.sort(key=lambda x: x["ats_score"], reverse=True)
        for rank, entry in enumerate(valid, 1):
            entry["rank"] = rank

    return JSONResponse(content={
        "mode": req.mode,
        "total_scored": len(results),
        "results": results,
    })


@app.post("/explain")
def explain_score(req: ScoreRequest, api_key: str = Depends(verify_api_key_with_usage)):
    """Get detailed explanation and improvement suggestions without re-scoring (uses cache)."""
    req = _maybe_autofill_resume(req, api_key)
    resume_text, jd_text, file_path = resolve_inputs(req)

    # Try to get cached ATS result
    ats_cache_key = _cache_key(resume_text, jd_text, "ats")
    ats_result = _get_cached(ats_cache_key)

    if not ats_result:
        # Score first if not cached
        ats_result = ats_scorer.calculate_ats_score(resume_text, jd_text, file_path)

    explanation = generate_ats_explanation(resume_text, jd_text, ats_result)

    return JSONResponse(content={
        "current_score": ats_result.get("total_score", 0),
        "explanation": explanation,
    })


# ─── Helper ───

def _overall_assessment(ats_score: float, hr_score: float) -> str:
    """Generate overall assessment from dual scores."""
    if ats_score >= 75 and hr_score >= 70:
        return "STRONG: Resume passes both ATS and HR evaluation. Ready to submit."
    elif ats_score >= 75 and hr_score < 70:
        return "ATS READY, HR WEAK: Resume will pass ATS filters but may not impress recruiters. Strengthen impact signals and career narrative."
    elif ats_score < 75 and hr_score >= 70:
        return "HR STRONG, ATS WEAK: Resume reads well to humans but may be filtered by ATS. Add more JD keywords to Core Competencies."
    elif ats_score >= 60 and hr_score >= 55:
        return "COMPETITIVE: Decent match but room for improvement on both ATS keywords and HR impact signals."
    else:
        return "NEEDS WORK: Significant gaps in keyword matching and/or recruiter appeal. Consider major revision."


# =============================================================================
# API KEY MANAGEMENT (admin endpoints)
# =============================================================================

def _verify_admin_secret(request: Request):
    """Verify admin secret from header. Blocks all access if not configured."""
    from cloud.config import settings
    secret = settings.ADMIN_SECRET
    if not secret:
        raise HTTPException(status_code=503, detail="Admin secret not configured.")
    provided = request.headers.get("X-Admin-Secret", "") or request.query_params.get("admin_secret", "")
    if not provided or provided != secret:
        raise HTTPException(status_code=403, detail="Forbidden.")


@app.post("/admin/create-key")
def create_api_key(req: APIKeyRequest, request: Request):
    """Create a new API key. Protected by admin secret."""
    _verify_admin_secret(request)
    import secrets
    key = f"rb_{secrets.token_hex(24)}"
    _api_keys[key] = {
        "tier": req.tier,
        "label": req.label or "",
        "created": datetime.now().isoformat(),
        "daily_count": 0,
        "last_reset": datetime.now().strftime("%Y-%m-%d"),
    }
    return {"api_key": key, "tier": req.tier, "label": req.label}


@app.get("/admin/stats")
def admin_stats(request: Request):
    """Server statistics. Protected by admin secret."""
    _verify_admin_secret(request)
    return {
        "uptime_seconds": round(time.time() - _server_start_time, 1),
        "cache_entries": len(_score_cache),
        "api_keys_issued": len(_api_keys),
        "active_rate_limits": len(_rate_limits),
    }


class SetTierRequest(BaseModel):
    email: str
    tier: str = Field(..., pattern="^(free|pro|ultra)$")


@app.post("/admin/set-tier")
def admin_set_tier(req: SetTierRequest, request: Request):
    """Update a user's tier. Protected by admin secret."""
    _verify_admin_secret(request)
    from cloud.auth import get_db
    db = get_db()
    row = db.execute("SELECT id, email, tier FROM users WHERE email = ?", (req.email.lower().strip(),)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail=f"User {req.email} not found.")
    old_tier = row["tier"]
    db.execute("UPDATE users SET tier = ?, updated_at = datetime('now') WHERE id = ?", (req.tier, row["id"]))
    db.commit()
    return {"email": row["email"], "old_tier": old_tier, "new_tier": req.tier}


# =============================================================================
# AUTH & BILLING ENDPOINTS (cloud mode)
# =============================================================================

class RegisterRequest(BaseModel):
    email: str
    password: str

class LoginRequest(BaseModel):
    email: str
    password: str

class CreateKeyRequest(BaseModel):
    label: str = ""


@app.post("/auth/register")
def register(req: RegisterRequest):
    """Register a new user account (free tier)."""
    if not CLOUD_AVAILABLE:
        raise HTTPException(status_code=501, detail="Cloud auth not available in local mode.")
    try:
        user = create_user(req.email, req.password)
        token = create_jwt_token(user["id"], user["email"], user["tier"])
        return {"user": user, "token": token}
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))


@app.post("/auth/login")
def login(req: LoginRequest):
    """Login and receive a JWT token."""
    if not CLOUD_AVAILABLE:
        raise HTTPException(status_code=501, detail="Cloud auth not available in local mode.")
    user = authenticate_user(req.email, req.password)
    if not user:
        raise HTTPException(status_code=401, detail="Invalid email or password.")
    token = create_jwt_token(user["id"], user["email"], user["tier"])
    return {"user": user, "token": token}


@app.post("/auth/api-key")
def create_key(req: CreateKeyRequest, auth=Depends(verify_api_key)):
    """Generate a new API key for the authenticated user."""
    if not CLOUD_AVAILABLE or not isinstance(auth, dict):
        raise HTTPException(status_code=501, detail="Cloud auth not available.")
    raw_key = create_user_api_key(auth["user_id"], req.label)
    return {"api_key": raw_key, "label": req.label, "note": "Save this key — it won't be shown again."}


@app.get("/auth/usage")
def usage_stats(auth=Depends(verify_api_key)):
    """Get usage stats for the authenticated user."""
    if not CLOUD_AVAILABLE or not isinstance(auth, dict):
        return {"total_scores": 0, "tier": "anonymous"}
    stats = get_usage_stats(auth["user_id"])
    stats["tier"] = auth["tier"]
    return stats


class CheckoutRequest(BaseModel):
    tier: str = Field("pro", description="Tier to upgrade to: 'pro' or 'ultra'")


@app.post("/billing/checkout")
def billing_checkout(req: CheckoutRequest = None, auth=Depends(verify_api_key)):
    """Create a Stripe checkout session to upgrade to Pro or Ultra."""
    if not CLOUD_AVAILABLE or not isinstance(auth, dict):
        raise HTTPException(status_code=501, detail="Billing not available.")
    if not is_billing_configured():
        raise HTTPException(status_code=503, detail="Stripe billing not configured. Contact admin.")

    target_tier = req.tier if req else "pro"
    current_tier = auth.get("tier", "free")

    # Already on same or higher tier
    tier_rank = {"free": 0, "pro": 1, "ultra": 2}
    if tier_rank.get(current_tier, 0) >= tier_rank.get(target_tier, 0):
        return {"message": f"Already on {current_tier.title()} tier."}

    result = create_checkout_session(auth["user_id"], auth["email"], tier=target_tier)
    if not result or "error" in result:
        raise HTTPException(status_code=500, detail=result.get("error", "Failed to create checkout session."))
    return result


@app.post("/billing/webhook")
async def billing_webhook(request: Request):
    """Handle Stripe webhook events (subscription lifecycle)."""
    if not CLOUD_AVAILABLE:
        raise HTTPException(status_code=501, detail="Billing not available.")

    payload = await request.body()
    sig = request.headers.get("stripe-signature", "")

    result = handle_webhook_event(payload, sig)

    if result["action"] == "upgrade":
        update_user_tier(
            result["user_id"], result["tier"],
            result.get("stripe_customer_id"), result.get("stripe_subscription_id"),
        )
        return {"status": "upgraded", "user_id": result["user_id"]}

    elif result["action"] == "downgrade":
        customer_id = result.get("stripe_customer_id", "")
        user = get_user_by_stripe_customer_id(customer_id) if customer_id else None
        if user:
            update_user_tier(user["id"], "free", customer_id, None)
            return {"status": "downgraded", "user_id": user["id"], "reason": result.get("reason")}
        return {"status": "downgrade_failed", "reason": "User not found for customer"}

    return {"status": "ignored", "event_type": result.get("event_type")}


@app.post("/billing/portal")
def billing_portal(auth=Depends(verify_api_key)):
    """Get a Stripe Customer Portal URL for managing subscription."""
    if not CLOUD_AVAILABLE or not isinstance(auth, dict):
        raise HTTPException(status_code=501, detail="Billing not available.")

    user = get_user_by_id(auth["user_id"])
    if not user or not user.get("stripe_customer_id"):
        raise HTTPException(status_code=400, detail="No active subscription found.")

    portal_url = create_portal_session(user["stripe_customer_id"])
    if not portal_url:
        raise HTTPException(status_code=500, detail="Failed to create portal session.")
    return {"portal_url": portal_url}


# =============================================================================
# LLM SCORING ENDPOINTS
# =============================================================================

@app.post("/score/llm")
async def score_llm(req: ScoreRequest, api_key: str = Depends(verify_api_key_with_usage)):
    """Score resume using LLM-augmented scorer (Claude)."""
    try:
        from llm_scorer import score_with_llm, ANTHROPIC_AVAILABLE
    except ImportError:
        raise HTTPException(status_code=500, detail="llm_scorer module not found")

    if not ANTHROPIC_AVAILABLE:
        raise HTTPException(status_code=503, detail="anthropic package not installed")

    req = _maybe_autofill_resume(req, api_key)
    resume_text, jd_text, _ = resolve_inputs(req)
    result = score_with_llm(resume_text, jd_text, domain_hint=req.domain_hint)
    return result


@app.post("/score/combined")
async def score_combined(req: ScoreRequest, api_key: str = Depends(verify_api_key_with_usage)):
    """Score resume using all three scorers (ATS + HR + LLM) and return blended result."""
    req = _maybe_autofill_resume(req, api_key)
    resume_text, jd_text, resume_file_path = resolve_inputs(req)

    # Rules-based scores
    ats_result = ats_scorer.calculate_ats_score(resume_text, jd_text, resume_file_path)
    try:
        hr_result = hr_scorer.calculate_hr_score_from_text(resume_text, jd_text)
    except Exception as _hr_err:
        hr_result = None

    rules_ats = ats_result.get('total_score', 0)
    rules_hr = hr_result.overall_score if hr_result else 0

    # LLM score (optional — graceful degradation)
    llm_result = {'ats_score': None, 'hr_score': None, 'error': 'skipped'}
    try:
        from llm_scorer import score_with_llm, combine_scores, ANTHROPIC_AVAILABLE
        if ANTHROPIC_AVAILABLE:
            llm_result = score_with_llm(resume_text, jd_text, domain_hint=req.domain_hint)
            combined_ats, combined_hr, blend_details = combine_scores(rules_ats, rules_hr, llm_result)
        else:
            combined_ats, combined_hr, blend_details = rules_ats, rules_hr, {'method': 'rules_only'}
    except Exception as e:
        combined_ats, combined_hr, blend_details = rules_ats, rules_hr, {'method': 'rules_only', 'error': str(e)}

    return {
        'combined_ats': combined_ats,
        'combined_hr': combined_hr,
        'blend_details': blend_details,
        'rules_ats': ats_result,
        'rules_hr': hr_scorer.result_to_dict(hr_result) if hr_result else None,
        'llm': llm_result
    }


# =============================================================================
# ULTRA — RESUME REWRITING
# =============================================================================

@app.post("/rewrite")
async def rewrite_resume_endpoint(req: ScoreRequest, auth=Depends(verify_api_key_with_usage)):
    """
    Rewrite resume to match JD, return before/after scores.
    Pro tier: 10 rewrites/month. Ultra tier: unlimited.
    """
    # Enforce tier + rewrite limits
    if CLOUD_AVAILABLE and isinstance(auth, dict):
        tier = auth.get("tier", "free")
        user_id = auth.get("user_id")
        if tier not in ("pro", "ultra"):
            raise HTTPException(
                status_code=403,
                detail="Resume rewriting requires a Pro ($12/month) or Ultra ($29/month) subscription.",
            )
        # Pro users: check monthly rewrite limit
        if tier == "pro" and user_id:
            try:
                from cloud.auth import check_rewrite_allowed
                rewrite_status = check_rewrite_allowed(user_id, tier)
                if not rewrite_status["allowed"]:
                    raise HTTPException(
                        status_code=429,
                        detail=f"Monthly rewrite limit reached ({rewrite_status['limit']}/month for Pro). Upgrade to Ultra for unlimited rewrites.",
                    )
            except ImportError:
                pass  # Local mode — no limits

    req = _maybe_autofill_resume(req, auth)
    resume_text, jd_text, resume_file_path = resolve_inputs(req)
    _log_score_usage(auth, "/rewrite")

    try:
        # 1. Score the original resume
        original_ats = ats_scorer.calculate_ats_score(resume_text, jd_text, resume_file_path)
        try:
            original_hr_obj = hr_scorer.calculate_hr_score_from_text(resume_text, jd_text)
            original_hr = hr_scorer.result_to_dict(original_hr_obj)
        except Exception:
            original_hr = {"overall_score": 0}

        # 2. Rewrite via LLM
        from llm_scorer import rewrite_resume, ANTHROPIC_AVAILABLE
        if not ANTHROPIC_AVAILABLE:
            raise HTTPException(status_code=503, detail="LLM rewriting unavailable — anthropic package not installed.")

        rewrite_result = rewrite_resume(resume_text, jd_text, domain_hint=req.domain_hint)

        if rewrite_result.get("error") or not rewrite_result.get("rewritten_resume"):
            raise HTTPException(
                status_code=500,
                detail=rewrite_result.get("error", "Rewrite failed — no output returned."),
            )

        rewritten_text = rewrite_result["rewritten_resume"]

        # 3. Score the rewritten resume
        rewritten_ats = ats_scorer.calculate_ats_score(rewritten_text, jd_text)
        try:
            rewritten_hr_obj = hr_scorer.calculate_hr_score_from_text(rewritten_text, jd_text)
            rewritten_hr = hr_scorer.result_to_dict(rewritten_hr_obj)
        except Exception:
            rewritten_hr = {"overall_score": 0}

        return {
            "rewritten_resume": rewritten_text,
            "changes_made": rewrite_result.get("changes_made", []),
            "explanation": rewrite_result.get("explanation", ""),
            "original_scores": {
                "ats": original_ats.get("total_score", 0),
                "hr": original_hr.get("overall_score", 0),
            },
            "rewritten_scores": {
                "ats": rewritten_ats.get("total_score", 0),
                "hr": rewritten_hr.get("overall_score", 0),
            },
            "model_used": rewrite_result.get("model_used", "claude-sonnet-4-6"),
        }

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Rewrite failed: {str(e)}")


# =============================================================================
# COVER LETTER ENDPOINT
# =============================================================================


@app.post("/cover-letter")
async def cover_letter_endpoint(req: CoverLetterRequest, auth=Depends(verify_api_key_with_usage)):
    """Generate a tailored cover letter. Requires Pro or Ultra tier."""
    # Enforce tier
    if CLOUD_AVAILABLE and isinstance(auth, dict):
        tier = auth.get("tier", "free")
        if tier not in ("pro", "ultra"):
            raise HTTPException(
                status_code=403,
                detail="Cover letter generation requires a Pro ($12/month) or Ultra ($29/month) subscription.",
            )

    # Auto-fill saved resume if none provided
    if not req.resume_text and CLOUD_AVAILABLE and isinstance(auth, dict):
        record = _get_resume_db(auth["user_id"])
        if record:
            req = req.model_copy(update={"resume_text": record["resume_text"]})

    if not req.resume_text:
        raise HTTPException(status_code=400, detail="Provide resume_text or upload a resume via POST /resume/upload.")

    _log_score_usage(auth, "/cover-letter")

    try:
        from llm_scorer import generate_cover_letter, ANTHROPIC_AVAILABLE
        if not ANTHROPIC_AVAILABLE:
            raise HTTPException(status_code=503, detail="Cover letter generation unavailable — anthropic package not installed.")

        result = generate_cover_letter(
            resume_text=req.resume_text,
            jd_text=req.jd_text,
            company_name=req.company_name,
            job_title=req.job_title,
        )

        if result.get("error"):
            raise HTTPException(status_code=500, detail=result["error"])

        return JSONResponse(content=result)

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Cover letter generation failed: {str(e)}")


# =============================================================================
# JOB DISCOVERY ENDPOINT
# =============================================================================


@app.post("/resume/upload")
def upload_resume_endpoint(req: ResumeUploadRequest, auth=Depends(verify_api_key)):
    """Save or replace the authenticated user's resume. Available to all registered users."""
    if not CLOUD_AVAILABLE or not isinstance(auth, dict):
        raise HTTPException(status_code=501, detail="Resume storage requires cloud authentication.")

    # Resolve text from request
    if req.resume_text and req.resume_text.strip():
        text = req.resume_text.strip()
        filename = req.resume_filename or "resume.txt"
    elif req.resume_file:
        filename = req.resume_filename or "resume.pdf"
        try:
            text = _decode_base64_file(req.resume_file, filename)
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"Could not decode file: {e}")
    else:
        raise HTTPException(status_code=400, detail="Provide resume_text or resume_file.")

    if len(text.strip()) < 100:
        raise HTTPException(status_code=400, detail="Resume too short (minimum 100 characters).")

    meta = _save_resume_db(auth["user_id"], text, filename)
    return {"saved": True, **meta}


@app.get("/resume")
def get_resume_endpoint(auth=Depends(verify_api_key)):
    """Retrieve the authenticated user's saved resume."""
    if not CLOUD_AVAILABLE or not isinstance(auth, dict):
        raise HTTPException(status_code=501, detail="Resume storage requires cloud authentication.")

    record = _get_resume_db(auth["user_id"])
    if not record:
        raise HTTPException(status_code=404, detail="No resume on file.")
    return {
        "resume_on_file": True,
        "resume_text": record["resume_text"],
        "filename": record["filename"],
        "content_hash": record["content_hash"],
        "uploaded_at": record["uploaded_at"],
        "updated_at": record["updated_at"],
        "char_count": len(record["resume_text"]),
    }


@app.delete("/resume")
def delete_resume_endpoint(auth=Depends(verify_api_key)):
    """Delete the authenticated user's saved resume."""
    if not CLOUD_AVAILABLE or not isinstance(auth, dict):
        raise HTTPException(status_code=501, detail="Resume storage requires cloud authentication.")

    deleted = _delete_resume_db(auth["user_id"])
    if not deleted:
        raise HTTPException(status_code=404, detail="No resume on file to delete.")
    return {"deleted": True}


@app.post("/jobs/discover")
def discover_jobs_endpoint(req: JobDiscoverRequest, api_key=Depends(verify_api_key_with_usage)):
    """Search for jobs and score them against a resume. Counts as 1 score usage."""
    _log_score_usage(api_key, "/jobs/discover")

    # Auto-fill saved resume if none provided
    if not req.resume_text and CLOUD_AVAILABLE and isinstance(api_key, dict):
        record = _get_resume_db(api_key["user_id"])
        if record:
            req = req.model_copy(update={"resume_text": record["resume_text"]})

    if not req.resume_text:
        raise HTTPException(status_code=400, detail="Provide resume_text or upload a resume via POST /resume/upload.")

    # Cache check (1-hour TTL for job discovery)
    cache_content = req.resume_text[:500] + req.job_title + req.location + str(req.remote_only)
    cache_key = hashlib.sha256(cache_content.encode()).hexdigest()[:32]
    cached = _get_cached(cache_key)
    if cached:
        cached["_cached"] = True
        return JSONResponse(content=cached)

    try:
        import job_discovery
        result = job_discovery.discover_jobs(
            resume_text=req.resume_text,
            job_title=req.job_title,
            location=req.location,
            remote_only=req.remote_only,
            max_results=req.max_results,
        )
        _set_cached(cache_key, result)
        return JSONResponse(content=result)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Job discovery failed: {str(e)}")


@app.post("/jobs/fetch-jd")
def fetch_jd_endpoint(req: FetchJDRequest, api_key=Depends(verify_api_key)):
    """Scrape full job description from a listing URL. Uses trafilatura + Claude Haiku."""
    try:
        import jd_fetcher
    except ImportError:
        raise HTTPException(status_code=503, detail="JD fetcher module unavailable on this server.")

    try:
        raw_text = jd_fetcher.fetch_jd_from_url(req.url)
    except Exception as e:
        raise HTTPException(status_code=422, detail=f"Scraping failed: {str(e)}")

    if not raw_text:
        raise HTTPException(
            status_code=422,
            detail="Could not extract text from this URL. The site may block scrapers (LinkedIn, Indeed). Please paste the JD manually.",
        )

    jd_text = raw_text
    if req.use_ai and len(raw_text) > 400:
        api_key_str = os.getenv("ANTHROPIC_API_KEY", "")
        if api_key_str:
            try:
                jd_text = jd_fetcher.extract_jd_with_ai(raw_text, req.job_title, api_key_str)
            except Exception:
                jd_text = raw_text  # Fall back to raw if AI cleaning fails

    return {
        "jd_text": jd_text,
        "char_count": len(jd_text),
        "raw_char_count": len(raw_text),
    }


# =============================================================================
# CLI ENTRY POINT
# =============================================================================

if __name__ == "__main__":
    import uvicorn

    parser = argparse.ArgumentParser(description="Resume Scorer API Server v3.0")
    parser.add_argument("--port", type=int, default=8100, help="Port (default: 8100)")
    parser.add_argument("--host", type=str, default="127.0.0.1", help="Host (default: 127.0.0.1)")
    parser.add_argument("--require-auth", action="store_true", help="Require API key authentication")
    parser.add_argument("--cors-origins", type=str, default="*", help="CORS allowed origins (comma-separated)")
    parser.add_argument("--rate-limit", type=int, default=60, help="Requests per minute per key (default: 60)")
    args = parser.parse_args()

    # Apply config from CLI args (override env/cloud settings)
    _config["require_auth"] = args.require_auth or _config["require_auth"]
    _config["rate_limit_per_minute"] = args.rate_limit

    # Add CORS middleware
    origins = [o.strip() for o in args.cors_origins.split(",")]
    app.add_middleware(
        CORSMiddleware,
        allow_origins=origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    print(f"\n{'='*60}")
    print(f"  Resume Scorer API v3.0")
    print(f"{'='*60}")
    print(f"  Server:  http://{args.host}:{args.port}")
    print(f"  Auth:    {'Required (JWT + API Key)' if _config['require_auth'] else 'Disabled'}")
    print(f"  Cloud:   {'Enabled' if CLOUD_AVAILABLE else 'Disabled (local mode)'}")
    print(f"  Billing: {'Configured' if CLOUD_AVAILABLE and is_billing_configured() else 'Not configured'}")
    print(f"  CORS:    {args.cors_origins}")
    print(f"  Rate:    {args.rate_limit}/min per key")
    print(f"\n  Scoring Endpoints:")
    print(f"  GET  /health         — Server status and model info")
    print(f"  POST /score/ats      — ATS score")
    print(f"  POST /score/hr       — HR score")
    print(f"  POST /score/both     — Combined ATS + HR score")
    print(f"  POST /score/llm      — LLM-augmented score (Claude)")
    print(f"  POST /score/combined — Blended ATS + HR + LLM score")
    print(f"  POST /score/batch    — Batch scoring")
    print(f"  POST /explain        — Score explanation")
    print(f"  POST /cover-letter   — Cover letter generation (Pro/Ultra)")
    print(f"  POST /jobs/discover  — Job discovery + scoring")
    print(f"\n  Auth & Billing Endpoints:")
    print(f"  POST /auth/register  — Create account")
    print(f"  POST /auth/login     — Login (returns JWT)")
    print(f"  POST /auth/api-key   — Generate API key")
    print(f"  GET  /auth/usage     — Usage stats")
    print(f"  POST /billing/checkout — Upgrade to Pro (Stripe)")
    print(f"  POST /billing/webhook  — Stripe webhooks")
    print(f"  POST /billing/portal   — Manage subscription")
    print(f"\n  Admin Endpoints:")
    print(f"  POST /admin/create-key — Legacy key generation")
    print(f"  GET  /admin/stats      — Server statistics")
    print(f"{'='*60}\n")

    uvicorn.run(app, host=args.host, port=args.port, log_level="info")
