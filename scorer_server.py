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

import argparse
import hashlib
import os
import sys
import time

# Load .env file from project root (simple loader, no python-dotenv required)
_env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
if os.path.isfile(_env_path):
    with open(_env_path, "r", encoding="utf-8") as _ef:
        for _line in _ef:
            _line = _line.strip()
            if _line and not _line.startswith("#") and "=" in _line:
                _k, _, _v = _line.partition("=")
                os.environ[_k.strip()] = _v.strip()
import asyncio
import base64
import json
import logging
import re
import statistics
import tempfile
import threading
import uuid
from collections import defaultdict
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from legacy_rewrite_guard import native_resume_team_required_response

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

# ─── Agent tool registry (cloud-hostable; imports cleanly with no cloud/
# package present and no vendor SDK installed -- see agent/tools.py's own
# module docstring). /rewrite and /cover-letter dispatch into this registry
# instead of talking to the model or the four-role pipeline directly, so
# every draft they can ever return is produced (and audited/recorded) by the
# exact same code path Task 8's tests already cover. ───
# ─── Async run bookkeeping for POST /agent/tailor + /agent/cover-letter
# (Task 10; see agent/runner.py's own module docstring). Imports cleanly
# without cloud/ present, same as agent.tools above. ───
import agent.runner as agent_runner
import agent.tools as agent_tools

# ─── Cloud modules (graceful import — works locally without cloud deps) ───
try:
    from cloud.auth import (
        authenticate_user,
        check_usage_allowed,
        create_jwt_token,
        create_password_reset_token,
        create_user,
        decode_jwt_token,
        get_or_create_anonymous_user,
        get_usage_stats,
        get_user_by_id,
        get_user_by_stripe_customer_id,
        log_usage,
        reset_password_with_token,
        update_user_tier,
    )
    from cloud.auth import (
        create_api_key as create_user_api_key,
    )
    from cloud.auth import (
        delete_resume as _delete_resume_db,
    )
    from cloud.auth import (
        get_resume as _get_resume_db,
    )
    from cloud.auth import (
        save_resume as _save_resume_db,
    )
    from cloud.auth import (
        validate_api_key as validate_user_api_key,
    )
    from cloud.billing import (
        create_checkout_session,
        create_portal_session,
        get_subscription_status,
        handle_webhook_event,
        is_billing_configured,
    )
    from cloud.config import settings as cloud_settings
    from cloud.db import get_conn as db_get_conn
    from cloud.db import run_migrations, scoped
    from cloud.emailer import send_password_reset_email
    from cloud.quotas import QuotaExceeded, check_quota  # noqa: F401 — availability probe
    CLOUD_AVAILABLE = True
except ImportError:
    CLOUD_AVAILABLE = False

# ─── Refuse to serve production traffic on development defaults ───
# Every default in cloud/config.py is chosen for local development, and each
# one fails INVISIBLY in production: the server boots and looks healthy while
# signing forgeable session tokens or writing accounts to a disk the next
# deploy discards. A smoke test passes either way, which is exactly why this
# has to be checked at boot rather than noticed later.
if CLOUD_AVAILABLE:
    from cloud.config import UnsafeProductionConfig, validate_production

    _config_problems = validate_production(cloud_settings)
    if _config_problems:
        print("\nREFUSING TO START — SCORER_ENV=production with unsafe configuration:")
        for _problem in _config_problems:
            print(f"  - {_problem}")
        print(
            "\nSet the named variables (fly secrets set ...) and redeploy. "
            "See the owner checklist in docs/superpowers/plans/private-layer-notes.md.\n"
        )
        raise UnsafeProductionConfig("; ".join(_config_problems))

# ─── Run database migrations at startup (graceful on failure) ───
if CLOUD_AVAILABLE:
    _mig_conn = None
    try:
        _mig_conn = db_get_conn(cloud_settings.DB_PATH)
        _applied = run_migrations(_mig_conn)
        if _applied:
            print(f"Applied {len(_applied)} database migrations: {', '.join(_applied)}")
    except Exception as e:
        print(f"Warning: Database migrations failed (non-critical): {e}")
        print("Server will continue, but cloud features may be degraded.")
    finally:
        if _mig_conn is not None:
            try:
                _mig_conn.close()
            except Exception:
                pass

# ─── Reap agent runs orphaned by the previous process ───
# Runs execute in-process via BackgroundTasks, so any row still 'queued' or
# 'running' at boot belonged to the process this one replaced and will never
# advance. Left alone it polls as "queued" forever AND keeps occupying a paid
# monthly slot, because quota counts every row that is not 'failed'. Separate
# from the migration block above: different concern, different failure mode.
# See agent.runner.reap_orphaned_runs for the single-owner assumption.
if CLOUD_AVAILABLE:
    _reap_conn = None
    try:
        _reap_conn = db_get_conn(cloud_settings.DB_PATH)
        _reaped = agent_runner.reap_orphaned_runs(_reap_conn)
        if _reaped:
            print(f"Failed {_reaped} agent run(s) orphaned by the previous process.")
    except Exception as e:
        print(f"Warning: could not reap orphaned agent runs: {e}")
    finally:
        if _reap_conn is not None:
            try:
                _reap_conn.close()
            except Exception:
                pass

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

# ─── Concurrency bound for the agent pipeline ───
# A tailoring run occupies one worker thread for minutes at a time. FastAPI
# serves every sync handler AND every background task from one threadpool of
# 40, so without a bound, ~40 simultaneous runs starve everything else --
# including /health, whose failure has Fly restart the machine and kill every
# run in flight. Admitting only a few at a time and refusing the rest with a
# retryable 429 keeps the server answering while it is busy.
#
# Sized for the deployed shape (1 shared CPU, 1GB): the work is network-bound
# on the Anthropic API, so the limit is about memory and leaving threads free,
# not parallelism.
MAX_CONCURRENT_AGENT_RUNS = int(os.getenv("SCORER_MAX_CONCURRENT_AGENT_RUNS", "8"))
_agent_run_slots = threading.BoundedSemaphore(MAX_CONCURRENT_AGENT_RUNS)

_AGENT_BUSY_DETAIL = (
    "We're finishing a lot of resumes right now. Please try again in a minute."
)


def _acquire_agent_slot() -> None:
    """Claim a run slot or refuse with 429. Never blocks: a caller made to wait
    would hold the very thread this bound exists to protect."""
    if not _agent_run_slots.acquire(blocking=False):
        raise HTTPException(status_code=429, detail=_AGENT_BUSY_DETAIL)


@contextmanager
def _agent_slot():
    """Bound one synchronous pipeline call (/rewrite, /cover-letter)."""
    _acquire_agent_slot()
    try:
        yield
    finally:
        _agent_run_slots.release()


def _in_agent_slot(worker, *args) -> None:
    """Run a background agent callable, then release its slot.

    The slot is released HERE rather than inside the worker so its lifetime
    belongs to the scheduling layer, not to one particular worker function.
    A worker that is replaced, returns early, or raises still gives the slot
    back; a leak here is permanent, since a BoundedSemaphore never refills.
    """
    try:
        worker(*args)
    finally:
        _agent_run_slots.release()


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
    format_style: Optional[str] = Field(None, description="Resume format: ats (default), harvard, modern, executive")
    include_llm_score: bool = Field(False, description="Legacy rewrite option (direct rewriting is disabled)")


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


class RedFlagCoachRequest(BaseModel):
    """Interactive LLM coach for fixing ATS/HR red flags."""
    resume_text: str = Field("", description="Full text of the resume (blank = use saved resume)")
    jd_text: str = Field(..., description="Full text of the job description")
    score_context: Optional[Dict[str, Any]] = Field(None, description="Latest ATS/HR/LLM score payload from the UI")
    chat_history: List[Dict[str, str]] = Field(default_factory=list, description="Ordered chat messages with roles and content")
    domain_hint: Optional[str] = Field(None, description="Optional domain override")


class JobDiscoverRequest(BaseModel):
    """Search for jobs and score them against a resume."""
    resume_text: str = Field("", description="Full text of the resume (blank = use saved resume)")
    job_title: str = Field(..., description="Target job title to search for")
    location: str = Field("", description="Geographic location filter")
    remote_only: bool = Field(False, description="Also search remote job boards")
    max_results: int = Field(10, ge=1, le=20, description="Number of top-scored jobs to return")


class TrackerAddRequest(BaseModel):
    """Add a job application to the tracker."""
    company: str = Field("", description="Company name")
    job_title: str = Field("", description="Job title")
    status: str = Field("Applied", description="Application status")
    resume_file: str = Field("", description="Resume filename")
    cover_letter_file: str = Field("", description="Cover letter filename")
    ats_score: float = Field(0.0, description="ATS score")
    hr_score: float = Field(0.0, description="HR score")
    llm_score: float = Field(0.0, description="LLM score")
    notes: str = Field("", description="Notes")
    applied_at: Optional[str] = Field(None, description="Date the application was submitted (ISO date/datetime)")
    jd_ref: Optional[str] = Field(None, description="Reference/filename for the job description used")
    jd_text: Optional[str] = Field(None, description="Full job description text — feeds /agent/tailor's application_id path")
    target_tier: Optional[str] = Field(None, description="IC | Sr | Manager | AD | Director")
    fit_label: Optional[str] = Field(None, description="MEETS | STRETCH | MISS (from job_fit_scorer)")
    hard_reqs_missed: Optional[int] = Field(None, description="Count of hard-requirement knockouts at apply time")
    referral_source: Optional[str] = Field(None, description="cold | alumni | recruiter | network | referral")


class TrackerUpdateRequest(BaseModel):
    """Update a job application entry."""
    status: Optional[str] = None
    notes: Optional[str] = None
    resume_file: Optional[str] = None
    cover_letter_file: Optional[str] = None
    applied_at: Optional[str] = None
    jd_ref: Optional[str] = None
    jd_text: Optional[str] = None
    target_tier: Optional[str] = None
    fit_label: Optional[str] = None
    hard_reqs_missed: Optional[int] = None
    referral_source: Optional[str] = None


_PROFILE_TEXT_FIELDS = (
    "full_name", "email", "phone", "city", "state", "postcode", "country",
    "linkedin_url", "portfolio_url", "work_authorization", "notice_period",
    "desired_salary",
)


class ProfileUpdateRequest(BaseModel):
    """Application-assist profile: what job forms ask for, stored once.

    PUT semantics are full-replace: omitted fields reset to their defaults.
    Never add government IDs, date of birth, or anything sensitive here.
    """
    full_name: str = Field("", max_length=1000)
    email: str = Field("", max_length=1000)
    phone: str = Field("", max_length=1000)
    city: str = Field("", max_length=1000)
    state: str = Field("", max_length=1000)
    postcode: str = Field("", max_length=1000)
    country: str = Field("", max_length=1000)
    linkedin_url: str = Field("", max_length=1000)
    portfolio_url: str = Field("", max_length=1000)
    work_authorization: str = Field("", max_length=1000)
    notice_period: str = Field("", max_length=1000)
    desired_salary: str = Field("", max_length=1000)
    references_on_request: bool = False


# Tracker classification whitelists. SQLite ALTER TABLE cannot add CHECK
# constraints (see cloud/db.py migrations), so valid values are enforced
# here, at the API boundary, instead.
_VALID_TARGET_TIERS = {"IC", "Sr", "Manager", "AD", "Director"}
_VALID_FIT_LABELS = {"MEETS", "STRETCH", "MISS"}
_VALID_REJECTION_REASONS = {
    "no_response", "auto_reject", "screen_reject",
    "interview_reject", "offer_declined", "withdrawn",
}


def _validate_tracker_classification(target_tier, fit_label, hard_reqs_missed) -> None:
    """Raise HTTP 422 with a clear detail if a tracker classification field is
    invalid. None always passes for every field here — they're all optional."""
    if target_tier is not None and target_tier not in _VALID_TARGET_TIERS:
        raise HTTPException(
            status_code=422,
            detail=f"target_tier must be one of {sorted(_VALID_TARGET_TIERS)} or null; got {target_tier!r}.",
        )
    if fit_label is not None and fit_label not in _VALID_FIT_LABELS:
        raise HTTPException(
            status_code=422,
            detail=f"fit_label must be one of {sorted(_VALID_FIT_LABELS)} or null; got {fit_label!r}.",
        )
    if hard_reqs_missed is not None and hard_reqs_missed < 0:
        raise HTTPException(
            status_code=422,
            detail=f"hard_reqs_missed must be an integer >= 0 or null; got {hard_reqs_missed!r}.",
        )


class TrackerOutcomeRequest(BaseModel):
    """Record a terminal (or interview-stage) outcome for a tracker entry."""
    rejection_reason: str = Field(..., description=(
        "no_response | auto_reject | screen_reject | interview_reject | "
        "offer_declined | withdrawn"
    ))
    interview_stage: Optional[int] = Field(
        None, description="0=applied, 1=phone screen, 2=hiring mgr, 3=panel, 4=onsite, 5=offer"
    )
    detail: Optional[str] = Field(None, description="Free-text note about the outcome")


class FetchJDRequest(BaseModel):
    """Fetch full job description from a listing URL."""
    url: str = Field(..., description="Job listing URL to scrape")
    job_title: str = Field("", description="Job title hint for AI extraction")
    use_ai: bool = Field(True, description="Use Claude Haiku to clean/extract JD from raw page text")


class JdExtractRequest(BaseModel):
    """JD-only extraction for ATS keywords and structured requirements (no resume required)."""

    jd_text: str = Field(..., description="Full job description plain text or markdown")
    domain_hint: Optional[str] = Field(
        None,
        description="Optional domain override for extract_jd_keywords (e.g. technology, clinical_research)",
    )


class ResumeUploadRequest(BaseModel):
    """Upload or replace the authenticated user's saved resume."""
    resume_text: Optional[str] = Field(None, description="Plain text resume content")
    resume_file: Optional[str] = Field(None, description="Base64-encoded file (PDF/DOCX/TXT)")
    resume_filename: Optional[str] = Field(None, description="Original filename for format detection")


class AgentTailorRequest(BaseModel):
    """Enqueue an async tailored-resume run (POST /agent/tailor)."""
    jd_text: Optional[str] = Field(None, description="Job description text")
    application_id: Optional[int] = Field(
        None, description="Tracker entry to read a stored job description from, if jd_text is omitted"
    )
    instruction: Optional[str] = Field(None, description="Optional free-text guidance for this run")
    resume_text: Optional[str] = Field(
        None, description="Optional one-off resume text for this run only; never saved to the account"
    )


class AgentCoverLetterRequest(BaseModel):
    """Enqueue an async cover-letter run (POST /agent/cover-letter)."""
    jd_text: Optional[str] = Field(None, description="Job description text")
    application_id: Optional[int] = Field(
        None, description="Tracker entry to read a stored job description from, if jd_text is omitted"
    )
    instruction: Optional[str] = Field(
        None, description="Accepted for shape parity with /agent/tailor; not yet wired into generation"
    )
    resume_text: Optional[str] = Field(
        None, description="Optional one-off resume text for this run only; never saved to the account"
    )
    company_name: str = Field("", description="Company name (auto-detected if empty)")
    job_title: str = Field("", description="Job title (auto-detected if empty)")


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
        if ext in (".pdf", ".docx"):
            # Use centralized extractor so PDF, DOCX (tables/text boxes/headers/
            # footers) parse identically to the local-file path. Previously
            # uploads used doc.paragraphs only and silently lost content.
            from text_extractor import extract_text
            text = extract_text(tmp_path)
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

        # The token's own "tier" claim is NOT trusted. Tokens live 30 days
        # (SCORER_JWT_EXPIRE_HOURS defaults to 720) and there is no refresh or
        # revocation, so a claim minted at login goes stale in both directions:
        # a canceled subscriber kept paid access for the rest of the month, and
        # someone who had just paid stayed locked out until they happened to log
        # in again -- and, because /billing/checkout read the same stale claim,
        # could be sold a second concurrent subscription. The users row is the
        # single source of truth for entitlement; this is one indexed primary-key
        # read against a local SQLite file.
        user = get_user_by_id(user_id)
        if user is None:
            raise HTTPException(status_code=401, detail="Invalid or expired JWT token.")
        tier = user["tier"]

        if not _check_rate_limit(str(user_id)):
            raise HTTPException(status_code=429, detail="Rate limit exceeded. Try again in 60 seconds.")

        return {"user_id": user_id, "tier": tier, "email": user["email"]}

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
                    f"Sign up at https://getresumehq.com for more scores, "
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


def _wants_event_stream(request: Request) -> bool:
    """Enable SSE explicitly via Accept header or ?stream=true."""
    accept = request.headers.get("Accept", "").lower()
    stream_param = request.query_params.get("stream", "").lower()
    return "text/event-stream" in accept or stream_param in {"1", "true", "yes"}


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
            "Add top 5 missing keywords to Core Competencies section (estimated +8-12% ATS score)"
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
            "advice": "Highlight this strength prominently — it's your competitive advantage",
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

_agent_key_state: Dict[str, Any] = {"checked": False, "status": "unchecked", "detail": None}


def _agent_key_status() -> Dict[str, Any]:
    """Report whether the Anthropic key is present AND actually accepted.

    'Configured' is not the same as 'working'. A revoked key is a non-empty
    string that passes every local check and fails only when the API is
    finally called -- which, for this deployment, meant a key died in March
    and nobody found out until August, because the endpoint that would have
    used it was a 409 stub and nothing else ever touched it.

    So this makes one real request (cheapest model, one output token,
    a fraction of a cent) the first time it is asked, then caches the answer
    for the life of the process. A deploy restarts the process, so every
    deploy re-checks. Health checks poll this endpoint every 30s and must
    stay fast, hence the cache.

    A dead key is REPORTED, never fatal: the free tier, billing, tracker,
    and job search do not touch Anthropic, and failing the health check
    would have Fly kill a machine that is serving all of them correctly.
    """
    if _agent_key_state["checked"]:
        return {"status": _agent_key_state["status"], "detail": _agent_key_state["detail"]}

    key = (os.getenv("ANTHROPIC_API_KEY") or "").strip()
    if not key:
        _agent_key_state.update(checked=True, status="missing",
                                detail="ANTHROPIC_API_KEY is not set.")
    else:
        try:
            import anthropic

            anthropic.Anthropic(api_key=key).messages.create(
                model="claude-haiku-4-5",
                max_tokens=1,
                messages=[{"role": "user", "content": "ping"}],
            )
            _agent_key_state.update(checked=True, status="ok", detail=None)
        except ImportError:
            _agent_key_state.update(checked=True, status="unavailable",
                                    detail="anthropic SDK is not installed.")
        except Exception as exc:  # noqa: BLE001 -- any failure is a report, not a crash
            name = type(exc).__name__
            if "Authentication" in name or "PermissionDenied" in name:
                detail = "ANTHROPIC_API_KEY is rejected by the API (invalid or revoked)."
            elif "RateLimit" in name:
                # Transient: don't cache, so the next poll re-checks.
                return {"status": "rate_limited",
                        "detail": "Rate limited while checking; will retry."}
            else:
                detail = f"Anthropic API unreachable ({name})."
            _agent_key_state.update(checked=True, status="invalid", detail=detail)

    return {"status": _agent_key_state["status"], "detail": _agent_key_state["detail"]}


@app.get("/health")
def health():
    """Server status, model availability, and usage stats."""
    agent_key = _agent_key_status()
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
        # Paid AI features (tailoring, cover letters). "ok" here means the key
        # was actually accepted by Anthropic, not merely that it is non-empty.
        "agent": {
            "api_key": agent_key["status"],
            "detail": agent_key["detail"],
            "features_available": agent_key["status"] == "ok",
        },
        "cache_size": len(_score_cache),
        "auth_required": _config["require_auth"],
    }


@app.post("/jd/extract")
def jd_extract(req: JdExtractRequest, api_key: str = Depends(verify_api_key_with_usage)):
    """Extract ATS keyword lists and rule-based JD requirements without scoring a resume.

    Community-contributed surface (fork: michalsteyn/fork-Resume-Builder):
    useful standalone as a keyword checker, and as the first step of a
    check-before-you-tailor flow.
    """
    from dataclasses import asdict

    from job_fit_scorer import extract_requirements

    jd_text = (req.jd_text or "").strip()
    if len(jd_text) < 50:
        raise HTTPException(status_code=400, detail="jd_text too short (minimum ~50 characters)")

    _log_score_usage(api_key, "/jd/extract")

    try:
        domain_override = req.domain_hint.strip() if req.domain_hint else None
        primary, confidence, domain_scores = ats_scorer.detect_domain(jd_text)
        domain_for_keywords = domain_override or primary
        jd_kw = ats_scorer.extract_jd_keywords(jd_text, domain=domain_for_keywords)
        req_fit = extract_requirements(jd_text)
        jd_hr = hr_scorer.parse_job_description(jd_text)

        return JSONResponse(
            content={
                "source": "resumehq",
                "domain": primary,
                "domain_confidence": float(confidence),
                "domain_scores": {k: float(v) for k, v in domain_scores.items()},
                "domain_used_for_keywords": domain_for_keywords,
                "jd_keywords": jd_kw,
                "extracted_requirements": asdict(req_fit),
                "job_requirements_hr": asdict(jd_hr),
            }
        )
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


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

            burstiness_score, burstiness_stats = hr_scorer.score_burstiness(candidate.all_bullets)
            bst_cv = burstiness_stats.get('coefficient_variation', 0)
            bst_penalty = -8 if bst_cv < 0.15 else (-4 if bst_cv < 0.25 else 0)

            final_score = max(0, min(100, raw_score - penalty_total + f_pattern_adjustment + bst_penalty))

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

            writing_quality = {
                **burstiness_stats,
                'burstiness_score': burstiness_score,
                'burstiness_penalty': bst_penalty,
                'quantification_rate': impact_stats.get('density', 0),
            }

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
                writing_quality=writing_quality,
            )

        result = hr_scorer.result_to_dict(result_obj)

        if req.include_explanation:
            result["explanation"] = generate_hr_explanation(result)

        _set_cached(cache_key, result)
        return JSONResponse(content=result)

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/score/both")
async def score_both(req: ScoreRequest, request: Request, api_key: str = Depends(verify_api_key_with_usage)):
    """
    ATS + HR scoring in one call.

    Default response is JSON for compatibility with existing clients.
    Set `Accept: text/event-stream` or `?stream=true` to receive SSE progress
    events on Fly.io for long-running requests.
    """
    import json as _json

    req = _maybe_autofill_resume(req, api_key)
    resume_text, jd_text, file_path = resolve_inputs(req)
    _log_score_usage(api_key, "/score/both")
    stream_response = _wants_event_stream(request)

    def _sse(obj: dict) -> str:
        return f"data: {_json.dumps(obj)}\n\n"

    def _run_ats():
        result = ats_scorer.calculate_ats_score(resume_text, jd_text, file_path)
        rating, likelihood, _ = ats_scorer.get_likelihood_rating(result["total_score"])
        result["rating"] = rating
        result["likelihood"] = likelihood
        return result

    def _run_hr():
        try:
            hr_obj = hr_scorer.calculate_hr_score_from_text(resume_text, jd_text)
            return hr_scorer.result_to_dict(hr_obj)
        except Exception:
            return {"overall_score": 0}

    def _build_combined_result(ats_result: dict, hr_result: dict) -> dict:
        combined = {
            "ats": ats_result,
            "hr": hr_result,
            "summary": {
                "ats_score": ats_result.get("total_score", 0),
                "hr_score": hr_result.get("overall_score", 0),
                "ats_rating": ats_result.get("rating", ""),
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
        return combined

    cache_key = _cache_key(resume_text, jd_text, "both")
    if not req.include_explanation:
        cached = _get_cached(cache_key)
        if cached:
            cached["_cached"] = True
            if not stream_response:
                return JSONResponse(content=cached)

            async def _cached_stream():
                yield _sse({"stage": "done", "pct": 100, "result": cached})

            return StreamingResponse(_cached_stream(), media_type="text/event-stream")

    if not stream_response:
        try:
            loop = asyncio.get_running_loop()
            ats_fut = loop.run_in_executor(None, _run_ats)
            hr_fut = loop.run_in_executor(None, _run_hr)
            ats_result, hr_result = await asyncio.gather(ats_fut, hr_fut)
            combined = _build_combined_result(ats_result, hr_result)
            _set_cached(cache_key, combined)
            return JSONResponse(content=combined)
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))

    async def event_stream():
        loop = asyncio.get_running_loop()
        yield _sse({"stage": "scoring", "pct": 8})

        # Submit both in parallel
        ats_fut = loop.run_in_executor(None, _run_ats)
        hr_fut  = loop.run_in_executor(None, _run_hr)

        pct = 18
        while not (ats_fut.done() and hr_fut.done()):
            yield _sse({"stage": "scoring", "pct": min(pct, 80)})
            await asyncio.sleep(5)
            pct += 15

        try:
            ats_result = ats_fut.result()
            hr_result  = hr_fut.result()
        except Exception as exc:
            yield _sse({"stage": "error", "detail": str(exc)})
            return

        combined = _build_combined_result(ats_result, hr_result)
        _set_cached(cache_key, combined)
        yield _sse({"stage": "done", "pct": 100, "result": combined})

    return StreamingResponse(event_stream(), media_type="text/event-stream")


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
    # get_db is a @contextmanager -- it must be entered. Called bare it returns
    # the context manager itself, whose .execute() raises AttributeError, so
    # this endpoint 500'd on every call and its remediation path did not exist.
    with get_db() as db:
        row = db.execute(
            "SELECT id, email, tier FROM users WHERE email = ?",
            (req.email.lower().strip(),),
        ).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail=f"User {req.email} not found.")
        old_tier = row["tier"]
        db.execute(
            "UPDATE users SET tier = ?, updated_at = datetime('now') WHERE id = ?",
            (req.tier, row["id"]),
        )
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


class ForgotPasswordRequest(BaseModel):
    email: str

class ResetPasswordRequest(BaseModel):
    token: str
    new_password: str


# Per-email cooldown for /auth/forgot. In-memory is enough: the goal is to
# stop one address being carpet-bombed with reset mail, not to survive
# restarts, and the single-machine deployment has no second process to sync.
_FORGOT_COOLDOWN: dict = {}
_FORGOT_COOLDOWN_SECONDS = 60

FRONTEND_URL = os.getenv("FRONTEND_URL", "https://getresumehq.com")


@app.post("/auth/forgot")
def forgot_password(req: ForgotPasswordRequest):
    """Email a password-reset link.

    Always answers {"sent": true} — for unknown addresses too — so the
    endpoint cannot be used to probe which emails have accounts.
    """
    if not CLOUD_AVAILABLE:
        raise HTTPException(status_code=501, detail="Cloud auth not available in local mode.")
    email = req.email.lower().strip()
    now = time.time()
    if now - _FORGOT_COOLDOWN.get(email, 0.0) >= _FORGOT_COOLDOWN_SECONDS:
        _FORGOT_COOLDOWN[email] = now
        token = create_password_reset_token(email)
        if token:
            send_password_reset_email(email, f"{FRONTEND_URL}/reset?token={token}")
    return {"sent": True}


@app.post("/auth/reset")
def reset_password(req: ResetPasswordRequest):
    """Set a new password using a token from the reset email."""
    if not CLOUD_AVAILABLE:
        raise HTTPException(status_code=501, detail="Cloud auth not available in local mode.")
    try:
        reset_password_with_token(req.token, req.new_password)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"reset": True}


@app.get("/digest/unsubscribe")
def digest_unsubscribe(token: str = ""):
    """Login-free one-click opt-out from the weekly job digest."""
    if not CLOUD_AVAILABLE:
        raise HTTPException(status_code=501, detail="Cloud features not available in local mode.")
    from cloud.digest import unsubscribe_with_token

    try:
        unsubscribe_with_token(token)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return HTMLResponse(
        "<div style='font-family:system-ui;max-width:28rem;margin:4rem auto;text-align:center'>"
        "<h2>You're unsubscribed</h2><p>No more weekly job digests. You can re-enable "
        "them anytime from your account at <a href='https://getresumehq.com'>getresumehq.com</a>.</p></div>"
    )


@app.post("/admin/send-digests")
def admin_send_digests(request: Request, user_id: Optional[int] = None, force: bool = False):
    """Trigger the weekly digest run (all eligible users, or one for testing)."""
    _verify_admin_secret(request)
    if not CLOUD_AVAILABLE:
        raise HTTPException(status_code=501, detail="Cloud features not available in local mode.")
    from cloud.digest import send_weekly_digests

    return send_weekly_digests(only_user_id=user_id, force=force)


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

    # Bind the session to the existing Stripe customer when there is one, so a
    # second checkout reuses that customer instead of creating a duplicate.
    # Stripe itself is the authority on whether they are already paying: if the
    # tier check above passed on a stale local row (an upgrade webhook that
    # never landed), selling another subscription would double-bill them.
    user = get_user_by_id(auth["user_id"])
    existing_customer = (user or {}).get("stripe_customer_id") or ""
    if existing_customer:
        status = get_subscription_status(existing_customer)
        if status and status.get("status") == "active":
            raise HTTPException(
                status_code=409,
                detail=(
                    "You already have an active subscription. Manage it from "
                    "your account instead of starting a new one."
                ),
            )

    result = create_checkout_session(
        auth["user_id"], auth["email"], tier=target_tier,
        stripe_customer_id=existing_customer,
    )
    if not result or "error" in result:
        raise HTTPException(status_code=500, detail=result.get("error", "Failed to create checkout session."))
    return result


@app.post("/billing/webhook")
async def billing_webhook(request: Request):
    """Handle Stripe webhook events (subscription lifecycle).

    Every failure path returns non-2xx ON PURPOSE. Stripe treats any 2xx as
    "handled" and never retries, so the previous blanket 200 meant a wrong or
    rotated signing secret silently discarded every entitlement while the
    Stripe dashboard stayed green: money in, no access granted, no signal.

    Status contract:
      503  Stripe/webhook secret not configured -- retryable once fixed
      400  bad signature or unusable metadata -- a retry cannot help, but the
           delivery must still be recorded as failed
      500  event is valid but we cannot map its customer to an account
      200  handled, duplicate, or an event type we deliberately ignore
    """
    if not CLOUD_AVAILABLE:
        raise HTTPException(status_code=501, detail="Billing not available.")

    payload = await request.body()
    sig = request.headers.get("stripe-signature", "")

    result = handle_webhook_event(payload, sig)
    action = result["action"]
    error = result.get("error")

    if error:
        retryable = "not configured" in error
        raise HTTPException(status_code=503 if retryable else 400, detail=error)

    # Idempotency. Stripe retries until it gets a 2xx and guarantees no
    # ordering, so the same event can arrive twice and a stale one can arrive
    # after a newer one. Claiming the id first means a replay is a no-op --
    # without it, a redelivered past_due could downgrade someone who has
    # since recovered. Claimed only for events that actually change state.
    event_id = result.get("event_id") or ""
    claimed = False
    if action != "none" and event_id:
        import sqlite3

        from cloud.auth import get_db
        try:
            with get_db() as conn:
                conn.execute(
                    "INSERT INTO stripe_events (event_id) VALUES (?)", (event_id,)
                )
            claimed = True
        except sqlite3.IntegrityError:
            return {"status": "ignored", "reason": "duplicate event"}

    def _release() -> None:
        """Un-claim the event so Stripe's retry is processed, not deduped.

        Without this, claim-then-fail burns the event id permanently: the
        retry hits the duplicate check above and gets a cheerful 200, so the
        change is lost exactly when the retry was the chance to apply it.
        """
        if not claimed:
            return
        from cloud.auth import get_db
        with get_db() as conn:
            conn.execute("DELETE FROM stripe_events WHERE event_id = ?", (event_id,))

    if action == "upgrade":
        update_user_tier(
            result["user_id"], result["tier"],
            result.get("stripe_customer_id"), result.get("stripe_subscription_id"),
        )
        return {"status": "upgraded", "user_id": result["user_id"]}

    elif action in ("restore", "downgrade"):
        # 'restore' is a recovered subscription (a retried card that cleared).
        customer_id = result.get("stripe_customer_id", "")
        user = get_user_by_stripe_customer_id(customer_id) if customer_id else None
        if not user:
            # We are billing a customer we cannot map to an account. Refusing
            # loudly keeps the event in Stripe's retry queue and on the
            # dashboard instead of dropping the change on the floor.
            _release()
            raise HTTPException(
                status_code=500,
                detail=f"No account for Stripe customer {customer_id or '(missing)'}",
            )
        if action == "restore":
            tier = result.get("tier", "pro")
            update_user_tier(user["id"], tier, customer_id, None)
            return {"status": "restored", "user_id": user["id"], "tier": tier}
        update_user_tier(user["id"], "free", customer_id, None)
        return {"status": "downgraded", "user_id": user["id"], "reason": result.get("reason")}

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
def score_llm(req: ScoreRequest, api_key: str = Depends(verify_api_key_with_usage)):
    """Score resume using LLM-augmented scorer (Claude).

    Deliberately a plain ``def``: score_with_llm below is a blocking network
    call. Declared ``async``, it would run on the event loop and stall every
    other request -- including /health, whose failure has Fly restart the
    machine -- for the length of the call. FastAPI runs a sync handler in its
    worker threadpool instead.
    """
    try:
        from llm_scorer import ANTHROPIC_AVAILABLE, score_with_llm
    except ImportError:
        raise HTTPException(status_code=500, detail="llm_scorer module not found")

    if not ANTHROPIC_AVAILABLE:
        raise HTTPException(status_code=503, detail="anthropic package not installed")

    req = _maybe_autofill_resume(req, api_key)
    resume_text, jd_text, _ = resolve_inputs(req)
    result = score_with_llm(resume_text, jd_text, domain_hint=req.domain_hint)
    return result


@app.post("/score/combined")
async def score_combined(req: ScoreRequest, request: Request, api_key: str = Depends(verify_api_key_with_usage)):
    """
    ATS + HR + LLM scoring in one call.

    Default response is JSON for existing web clients.
    Set `Accept: text/event-stream` or `?stream=true` to receive SSE progress
    events for long-running requests.
    """
    import json as _json

    req = _maybe_autofill_resume(req, api_key)
    resume_text, jd_text, resume_file_path = resolve_inputs(req)
    domain_hint = req.domain_hint
    stream_response = _wants_event_stream(request)

    def _sse(obj: dict) -> str:
        return f"data: {_json.dumps(obj)}\n\n"

    def _run_ats():
        return ats_scorer.calculate_ats_score(resume_text, jd_text, resume_file_path)

    def _run_hr():
        try:
            return hr_scorer.calculate_hr_score_from_text(resume_text, jd_text)
        except Exception:
            return None

    def _run_llm(rules_ats: float, rules_hr: float) -> dict:
        try:
            from llm_scorer import ANTHROPIC_AVAILABLE, combine_scores, score_with_llm
            if ANTHROPIC_AVAILABLE:
                llm_r = score_with_llm(resume_text, jd_text, domain_hint=domain_hint)
                c_ats, c_hr, blend = combine_scores(rules_ats, rules_hr, llm_r)
            else:
                llm_r = {"ats_score": None, "hr_score": None, "error": "skipped"}
                c_ats, c_hr, blend = rules_ats, rules_hr, {"method": "rules_only"}
        except Exception as exc:
            llm_r = {"error": str(exc)}
            c_ats, c_hr, blend = rules_ats, rules_hr, {"method": "rules_only", "error": str(exc)}
        return {"llm": llm_r, "combined_ats": c_ats, "combined_hr": c_hr, "blend": blend}

    def _build_final(ats_result: dict, hr_result_obj, llm_holder: dict) -> dict:
        rules_ats = ats_result.get("total_score", 0)
        rules_hr = hr_result_obj.overall_score if hr_result_obj else 0
        return {
            "combined_ats": llm_holder.get("combined_ats", rules_ats),
            "combined_hr": llm_holder.get("combined_hr", rules_hr),
            "blend_details": llm_holder.get("blend", {"method": "rules_only"}),
            "rules_ats": ats_result,
            "rules_hr": hr_scorer.result_to_dict(hr_result_obj) if hr_result_obj else None,
            "llm": llm_holder.get("llm", {"error": "skipped"}),
        }

    if not stream_response:
        loop = asyncio.get_running_loop()
        ats_fut = loop.run_in_executor(None, _run_ats)
        hr_fut = loop.run_in_executor(None, _run_hr)
        ats_result, hr_result_obj = await asyncio.gather(ats_fut, hr_fut)
        rules_ats = ats_result.get("total_score", 0)
        rules_hr = hr_result_obj.overall_score if hr_result_obj else 0
        llm_holder = await loop.run_in_executor(None, lambda: _run_llm(rules_ats, rules_hr))
        return JSONResponse(content=_build_final(ats_result, hr_result_obj, llm_holder))

    async def event_stream():
        loop = asyncio.get_running_loop()
        yield _sse({"stage": "scoring", "pct": 8})

        ats_fut = loop.run_in_executor(None, _run_ats)
        hr_fut  = loop.run_in_executor(None, _run_hr)

        pct = 15
        while not (ats_fut.done() and hr_fut.done()):
            yield _sse({"stage": "scoring", "pct": min(pct, 55)})
            await asyncio.sleep(5)
            pct += 12

        ats_result = ats_fut.result()
        hr_result_obj = hr_fut.result()
        rules_ats = ats_result.get("total_score", 0)
        rules_hr  = hr_result_obj.overall_score if hr_result_obj else 0

        yield _sse({"stage": "llm_scoring", "pct": 62})

        llm_fut = loop.run_in_executor(None, lambda: _run_llm(rules_ats, rules_hr))
        pct = 65
        while not llm_fut.done():
            yield _sse({"stage": "llm_scoring", "pct": min(pct, 92)})
            await asyncio.sleep(5)
            pct += 8

        llm_holder = llm_fut.result()
        final = _build_final(ats_result, hr_result_obj, llm_holder)
        yield _sse({"stage": "done", "pct": 100, "result": final})

    return StreamingResponse(event_stream(), media_type="text/event-stream")


@app.post("/score/job-fit")
async def score_job_fit(req: ScoreRequest, request: Request, api_key: str = Depends(verify_api_key_with_usage)):
    """
    Pre-application job fit screening.

    Evaluates whether a JD is worth applying to by checking:
    - Knockout disqualifiers (years of experience, certifications, visa)
    - 7-dimension fit scoring (experience, skills, title, domain, education, certs, seniority)
    - Gap analysis (fixable vs unfixable)

    Zero API cost — all local NLP/SBERT. Runs in <5 seconds.
    """
    from job_fit_scorer import calculate_job_fit

    req = _maybe_autofill_resume(req, api_key)
    resume_text, jd_text, _ = resolve_inputs(req)

    loop = asyncio.get_running_loop()
    result = await loop.run_in_executor(None, lambda: calculate_job_fit(resume_text, jd_text))

    return JSONResponse(content=result.to_dict())


@app.post("/coach/redflags")
async def coach_redflags(req: RedFlagCoachRequest):
    """Reject the legacy coaching path that could emit an unaudited draft.

    Authentication and usage dependencies are intentionally absent: this
    migration guard must execute without charging usage or mutating rate-limit
    state, and it never reads request content or invokes a model.
    """
    del req
    return JSONResponse(status_code=409, content=native_resume_team_required_response())


# =============================================================================
# RESUME REWRITE — thin wrapper around the audited agent pipeline
# =============================================================================

_TAILOR_UNAVAILABLE_DETAIL = (
    "We couldn't finish tailoring your resume just now. Please try again in "
    "a few minutes."
)


def _friendly_tier_required_detail(feature: str) -> str:
    return f"{feature} requires a Pro ($12/month) or Ultra ($29/month) subscription."


def _safe_agent_scores(ctx: "agent_tools.ToolContext", resume_text: str, jd_text: str):
    """Best-effort ATS/HR score for a piece of resume text. Never raises --
    a scoring hiccup must not take down an otherwise-successful rewrite."""
    try:
        scored = agent_tools.dispatch(
            "score_resume", ctx, resume_text=resume_text, jd_text=jd_text
        )
        return scored.get("ats_score"), scored.get("hr_score")
    except Exception:
        return None, None


@app.post("/rewrite")
def rewrite_resume_endpoint(req: ScoreRequest, auth=Depends(verify_api_key)):
    """
    Tailor the caller's resume via the audited four-role Resume Team pipeline
    (agent.tools.run_resume_team) and return it in the legacy REST shape the
    deployed PWA's Ultra "Rewrite" feature already reads:
    ``{"rewritten_resume": str, "ats_before", "ats_after", "hr_before",
    "hr_after"}`` (scores are null when unavailable, but ``rewritten_resume``
    is always present on a 200).

    403 for free tier, 402 for an exhausted Pro/Ultra monthly quota, 400 when
    no resume text is available from either the request or the account, and
    a 5xx with no internal error codes if the pipeline runs but does not
    publish a draft.

    Deliberately a plain ``def``: run_resume_team blocks for minutes on
    synchronous API calls. Declared ``async``, it would run on the event loop
    and freeze every other request -- including /health, whose failure has Fly
    restart the machine mid-run. FastAPI runs a sync handler in its worker
    threadpool instead.
    """
    if not CLOUD_AVAILABLE or not isinstance(auth, dict):
        raise HTTPException(
            status_code=501, detail="Resume tailoring requires cloud authentication."
        )

    user_id = auth["user_id"]
    tier = auth.get("tier", "free")

    if tier not in ("pro", "ultra"):
        raise HTTPException(
            status_code=403,
            detail=_friendly_tier_required_detail("Tailored resume rewriting"),
        )

    jd_text = (req.jd_text or "").strip()
    if not jd_text:
        raise HTTPException(
            status_code=400,
            detail="Include the job description you want this resume tailored for.",
        )

    provided_resume = (req.resume_text or "").strip()
    if provided_resume:
        # A resume_text in the request is a ONE-OFF source for this call
        # only. It is passed straight through to run_resume_team, which
        # tailors from it directly without ever touching the account's
        # saved resume (see agent/tools.py) -- this is what makes "request
        # resume_text if provided" take effect without silently clobbering
        # a carefully maintained saved resume.
        source_resume_text = provided_resume
    else:
        record = _get_resume_db(user_id)
        if not record or not record.get("resume_text"):
            raise HTTPException(
                status_code=400,
                detail=(
                    "We don't have a resume on file for your account yet. "
                    "Upload one, or include resume_text with this request."
                ),
            )
        source_resume_text = record["resume_text"]

    # Bound before opening a connection or claiming quota: a request refused
    # for load must cost the caller nothing.
    with _agent_slot():
        conn = db_get_conn(cloud_settings.DB_PATH)
        try:
            ctx = agent_tools.ToolContext(
                user_id=user_id, tier=tier, conn=conn, run_id=str(uuid.uuid4())
            )
            try:
                result = agent_tools.dispatch(
                    "run_resume_team",
                    ctx,
                    jd_text=jd_text,
                    resume_text=provided_resume or None,
                )
            except QuotaExceeded as exc:
                raise HTTPException(status_code=402, detail=exc.detail)
            except HTTPException:
                raise
            except Exception:
                logging.getLogger("scorer.rewrite").exception(
                    "rewrite dispatch crashed (user=%s)", user_id
                )
                raise HTTPException(status_code=502, detail=_TAILOR_UNAVAILABLE_DETAIL)

            if result.get("status") != "succeeded" or not result.get("draft"):
                terminal = str(result.get("error") or "UNKNOWN")
                logging.getLogger("scorer.rewrite").error(
                    "rewrite run not published (user=%s terminal=%s)", user_id, terminal
                )
                # A candidate-fit refusal is deterministic policy, not an
                # outage: "try again in a few minutes" would be a lie, and the
                # fit gate has no bypass by design. Tell the user the truth.
                if terminal.startswith("REJECTED:CANDIDATE_FIT"):
                    fit = result.get("candidate_fit_report") or {}
                    score = fit.get("score")
                    knockouts = fit.get("hard_knockouts") or []
                    parts = [
                        "Our fit check found this role is a significant stretch "
                        "for your resume, so the tailoring pipeline declined to "
                        "run — it never invents experience to close a gap."
                    ]
                    if isinstance(score, (int, float)):
                        bar = fit.get("threshold")
                        if not isinstance(bar, (int, float)):
                            bar = candidate_fit_preflight.CANDIDATE_FIT_THRESHOLD
                        parts.append(
                            f"Fit score: {round(score)} (needs {round(bar)})."
                        )
                    if knockouts:
                        # Name the requirement: the knockout detector has known
                        # false positives, so the user must be able to judge it.
                        reqs = "; ".join(
                            f"“{str(k.get('requirement', '')).strip()[:120]}”"
                            for k in knockouts[:2]
                            if str(k.get("requirement", "")).strip()
                        )
                        parts.append(
                            f"Hard requirement not met: {reqs}."
                            if reqs
                            else f"Hard requirements not met: {len(knockouts)}."
                        )
                    parts.append(
                        "Try a role closer to your experience, or update your "
                        "saved resume if it's missing relevant work."
                    )
                    raise HTTPException(status_code=409, detail=" ".join(parts))
                raise HTTPException(status_code=502, detail=_TAILOR_UNAVAILABLE_DETAIL)

            draft_text = result["draft"]
            ats_before, hr_before = _safe_agent_scores(ctx, source_resume_text, jd_text)
            ats_after, hr_after = _safe_agent_scores(ctx, draft_text, jd_text)
        finally:
            conn.close()

    return JSONResponse(content={
        "rewritten_resume": draft_text,
        "ats_before": ats_before,
        "ats_after": ats_after,
        "hr_before": hr_before,
        "hr_after": hr_after,
    })


# =============================================================================
# COVER LETTER ENDPOINT
# =============================================================================


_COVER_LETTER_UNAVAILABLE_DETAIL = (
    "We couldn't generate your cover letter just now. Please try again in a "
    "few minutes."
)


@app.post("/cover-letter")
def cover_letter_endpoint(req: CoverLetterRequest, auth=Depends(verify_api_key_with_usage)):
    """Generate a tailored cover letter via agent.tools.run_cover_letter.

    Requires Pro or Ultra tier. Response shape is unchanged from the prior
    direct-generation implementation (paragraphs/full_text/company/
    job_title/word_count) -- only the path producing it changed, so both
    the PWA and mcp_scorer.py's cloud fallback (which checks for
    "paragraphs" in the response) keep working. Quota is now enforced (Pro:
    30/month, Ultra: 1000/month -- see cloud/quotas.py) and every run is
    recorded in agent_runs, matching /rewrite.

    Plain ``def`` for the same reason as /rewrite above: the work below blocks
    on synchronous API calls and must not occupy the event loop.
    """
    if not CLOUD_AVAILABLE or not isinstance(auth, dict):
        raise HTTPException(
            status_code=501, detail="Cover letter generation requires cloud authentication."
        )

    user_id = auth["user_id"]
    tier = auth.get("tier", "free")
    if tier not in ("pro", "ultra"):
        raise HTTPException(
            status_code=403,
            detail=_friendly_tier_required_detail("Cover letter generation"),
        )

    jd_text = (req.jd_text or "").strip()
    if not jd_text:
        raise HTTPException(status_code=400, detail="Provide jd_text for the job description.")

    provided_resume = (req.resume_text or "").strip()
    if not provided_resume and not _get_resume_db(user_id):
        raise HTTPException(
            status_code=400,
            detail="Provide resume_text or upload a resume via POST /resume/upload.",
        )
    # A resume_text in the request is a ONE-OFF source for this call only --
    # passed straight through to run_cover_letter (mirrors /rewrite), which
    # never touches the account's saved resume (see agent/tools.py).

    _log_score_usage(auth, "/cover-letter")

    with _agent_slot():
        conn = db_get_conn(cloud_settings.DB_PATH)
        try:
            ctx = agent_tools.ToolContext(
                user_id=user_id, tier=tier, conn=conn, run_id=str(uuid.uuid4())
            )
            try:
                result = agent_tools.dispatch(
                    "run_cover_letter", ctx,
                    jd_text=jd_text, company=req.company_name, title=req.job_title,
                    resume_text=provided_resume or None,
                )
            except QuotaExceeded as exc:
                raise HTTPException(status_code=402, detail=exc.detail)
            except HTTPException:
                raise
            except Exception:
                raise HTTPException(status_code=502, detail=_COVER_LETTER_UNAVAILABLE_DETAIL)
        finally:
            conn.close()

    if result.get("status") != "succeeded" or not result.get("full_text"):
        raise HTTPException(status_code=502, detail=_COVER_LETTER_UNAVAILABLE_DETAIL)

    return JSONResponse(content={
        "full_text": result["full_text"],
        "paragraphs": result.get("paragraphs", []),
        "company": result.get("company"),
        "job_title": result.get("job_title"),
        "word_count": result.get("word_count"),
    })


# =============================================================================
# ASYNC AGENT RUNS — POST /agent/tailor, /agent/cover-letter; GET /agent/runs/{id}
#
# Task 10: queue-and-poll twins of /rewrite and /cover-letter above. The
# enqueue handlers do the auth/tier/quota checks synchronously (so a refused
# request never creates an agent_runs row) and then hand the actual work to
# a FastAPI background task, which calls the exact same agent.tools
# functions /rewrite and /cover-letter already use. See agent/runner.py's
# module docstring for why the background helpers below call
# agent_tools.dispatch(...) directly rather than agent_runner.create_run()/
# finish_run() -- those tool functions already own the full agent_runs
# bookkeeping for this run_id; wrapping them in agent_runner's primitives
# too would insert/update the same primary key twice.
# =============================================================================


def _agent_enqueue_precheck(auth, feature: str) -> tuple:
    """Shared auth/tier gate for the two POST /agent/* enqueue handlers.

    Mirrors /rewrite and /cover-letter's own checks exactly, so a free or
    anonymous caller sees the identical 501/403 behavior whether they hit
    the sync or the async entry point.
    """
    if not CLOUD_AVAILABLE or not isinstance(auth, dict):
        raise HTTPException(
            status_code=501, detail="This feature requires cloud authentication."
        )
    # An anonymous caller can never be pro/ultra, so the tier gate below would
    # refuse them anyway -- but 401 is the truthful answer ("sign in"), and it
    # keeps the anonymous bucket from ever reaching a per-account code path.
    if auth.get("anonymous"):
        raise HTTPException(status_code=401, detail="Sign in to use this feature.")
    user_id = auth["user_id"]
    tier = auth.get("tier", "free")
    if tier not in ("pro", "ultra"):
        raise HTTPException(status_code=403, detail=_friendly_tier_required_detail(feature))
    return user_id, tier


def _resolve_agent_jd_text(user_id: int, jd_text: Optional[str], application_id: Optional[int]) -> str:
    """Resolve the job description text for an async /agent/* enqueue call.

    Prefers an explicit ``jd_text``. Falls back to ``application_id``, read
    scoped to the caller from job_applications.jd_text — the full stored JD
    text. ``jd_ref`` is never used here: it is a bare filename/reference
    ("job_description.txt"), and tailoring against it would burn a quota
    slot producing garbage, so a row without stored text is refused with a
    clear 400 instead.
    """
    text = (jd_text or "").strip()
    if text:
        return text
    if application_id is None:
        raise HTTPException(status_code=400, detail="Provide jd_text or application_id.")

    conn = db_get_conn(cloud_settings.DB_PATH)
    try:
        row = scoped(conn, user_id).q(
            "SELECT jd_text FROM job_applications WHERE id = :application_id AND user_id = :user_id",
            {"application_id": application_id},
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        raise HTTPException(status_code=404, detail="Application not found.")
    stored = (row["jd_text"] or "").strip()
    if not stored:
        raise HTTPException(
            status_code=400,
            detail=(
                "No job description text is stored for this application "
                "(only a filename reference, if anything). Provide jd_text, "
                "or update the application with the full text first."
            ),
        )
    return stored


def _reserve_agent_run_slot(
    user_id: int, tier: str, action: str, run_id: str,
    jd_text: str, instruction: Optional[str],
) -> None:
    """Atomically verify quota AND claim the slot, in one write transaction.

    Checking the quota without claiming anything is not enough. month_usage()
    counts agent_runs rows, and on the async path the row was previously not
    written until the BackgroundTasks callable ran -- i.e. after the 202 had
    already been sent. Every request that arrived inside that window read the
    same stale count and was admitted, so a burst bought far more paid runs
    than the plan allows (measured: 25 accepted against a limit of 10).

    BEGIN IMMEDIATE takes SQLite's RESERVED lock up front, so concurrent
    enqueues serialize here: each one sees the rows its predecessors claimed.
    The row is written as 'queued', which also means GET /agent/runs/{run_id}
    can answer honestly the instant the caller gets its run_id, instead of
    404ing until the background task happened to reach its own insert.

    Raises 402 (naming the limit via QuotaExceeded's own friendly detail)
    without leaving a row behind.
    """
    import multi_agent_team  # local: keeps the heavy pipeline import off startup

    input_ref = multi_agent_team.canonical_digest(jd_text)
    conn = db_get_conn(cloud_settings.DB_PATH)
    try:
        # The transaction itself lives in agent.tools.reserve_run_slot, which
        # the synchronous /rewrite and /cover-letter paths share -- they had
        # the same double-spend hole and must not drift from this one.
        agent_tools.reserve_run_slot(
            conn, user_id, tier, action, run_id,
            input_ref=input_ref, instruction=instruction,
        )
    except QuotaExceeded as exc:
        raise HTTPException(status_code=402, detail=exc.detail)
    finally:
        conn.close()


_FIT_REJECTED_DETAIL = (
    "This role asks for more than your saved resume shows, so we stopped "
    "rather than stretch your experience to fit it. Nothing was used from "
    "your monthly allowance. Try a role that lines up more closely with your "
    "background, or update your saved resume if it is out of date."
)


def _friendly_agent_error(kind: str, error_code: Optional[str] = None) -> str:
    """Translate an internal agent_runs.error code into jargon-free copy.

    Raw codes are never surfaced -- 'FAILED:AGENT_UNAVAILABLE',
    'HUMAN_VOICE_AUDIT_FAILED' and friends name model and pipeline internals
    that mean nothing to an end user.

    But one internal outcome is not an error at all, and hiding it behind the
    generic copy actively misled people: REJECTED:CANDIDATE_FIT means the
    deterministic preflight judged the candidate too far from the role. Told
    'please try again in a few minutes', a user retries and gets the byte-for-
    byte identical refusal, because nothing about that verdict is transient.
    They conclude the product is broken rather than learning what it decided.

    So a fit rejection gets its own honest message. Everything else keeps the
    generic transient copy, which for those codes is accurate.
    """
    if error_code and str(error_code).startswith("REJECTED:CANDIDATE_FIT"):
        return _FIT_REJECTED_DETAIL
    if kind == "cover_letter":
        return _COVER_LETTER_UNAVAILABLE_DETAIL
    return _TAILOR_UNAVAILABLE_DETAIL


def _run_tailor_in_background(
    run_id: str, user_id: int, tier: str, jd_text: str,
    instruction: Optional[str], resume_text: Optional[str],
) -> None:
    """Background execution for POST /agent/tailor.

    Opens its OWN fresh connection -- a request-scoped connection must never
    cross into a BackgroundTasks callable. Starlette runs a plain ``def``
    background callable via a worker thread pool, and cloud.db.get_conn()
    does not pass ``check_same_thread=False``, so a connection created on
    the request's thread would raise the moment this thread touched it.
    """
    conn = db_get_conn(cloud_settings.DB_PATH)
    try:
        # slot_reserved: the enqueue handler already claimed this run's quota
        # slot and wrote its agent_runs row atomically.
        ctx = agent_tools.ToolContext(
            user_id=user_id, tier=tier, conn=conn, run_id=run_id, slot_reserved=True,
        )
        agent_tools.dispatch(
            "run_resume_team", ctx,
            jd_text=jd_text, instruction=instruction, resume_text=resume_text,
        )
    except Exception as exc:  # noqa: BLE001 -- fire-and-forget background task
        # run_resume_team already converts every internal failure into a
        # recorded 'failed' agent_runs row and returns normally rather than
        # raising. Anything that still escapes here (e.g. a DB error before
        # any row was written) has no HTTP response left to report to, and
        # no row to mark failed -- there is nothing to roll back, so it is
        # safe to drop after logging for operability.
        print(f"[/agent/tailor background] run {run_id} did not record a result: {exc}")
    finally:
        conn.close()


def _run_cover_letter_in_background(
    run_id: str, user_id: int, tier: str, jd_text: str,
    resume_text: Optional[str], company: Optional[str], title: Optional[str],
) -> None:
    """Background execution for POST /agent/cover-letter. See
    _run_tailor_in_background's docstring -- same fresh-connection and
    no-double-record rules apply."""
    conn = db_get_conn(cloud_settings.DB_PATH)
    try:
        # slot_reserved: the enqueue handler already claimed this run's quota
        # slot and wrote its agent_runs row atomically.
        ctx = agent_tools.ToolContext(
            user_id=user_id, tier=tier, conn=conn, run_id=run_id, slot_reserved=True,
        )
        agent_tools.dispatch(
            "run_cover_letter", ctx,
            jd_text=jd_text, company=company, title=title, resume_text=resume_text,
        )
    except Exception as exc:  # noqa: BLE001 -- fire-and-forget background task
        print(f"[/agent/cover-letter background] run {run_id} did not record a result: {exc}")
    finally:
        conn.close()


@app.post("/agent/tailor")
def agent_tailor_enqueue(
    req: AgentTailorRequest, background_tasks: BackgroundTasks, auth=Depends(verify_api_key)
):
    """Enqueue an async tailored-resume run.

    Returns immediately with a run_id to poll via GET /agent/runs/{run_id};
    the audited four-role pipeline (agent.tools.run_resume_team) executes
    afterward in a FastAPI background task. 401 unauthenticated, 403 for free
    tier, 402 for an exhausted Pro/Ultra monthly quota, 400 for missing input
    -- none of these create an agent_runs row. A successful enqueue claims its
    quota slot before returning (see _reserve_agent_run_slot), so the run is
    immediately pollable as 'queued'.
    """
    user_id, tier = _agent_enqueue_precheck(auth, "Tailored resume rewriting")
    jd_text = _resolve_agent_jd_text(user_id, req.jd_text, req.application_id)

    provided_resume = (req.resume_text or "").strip()
    if not provided_resume and not _get_resume_db(user_id):
        raise HTTPException(
            status_code=400,
            detail=(
                "We don't have a resume on file for your account yet. "
                "Upload one, or include resume_text with this request."
            ),
        )

    run_id = agent_runner.new_run_id()
    # Claim a concurrency slot BEFORE the quota slot, so a request refused for
    # load is free: no agent_runs row, nothing counted against the plan. The
    # background task releases it in its finally.
    _acquire_agent_slot()
    try:
        _reserve_agent_run_slot(user_id, tier, "tailor", run_id, jd_text, req.instruction)
        background_tasks.add_task(
            _in_agent_slot, _run_tailor_in_background,
            run_id, user_id, tier, jd_text, req.instruction, provided_resume or None,
        )
    except BaseException:
        _agent_run_slots.release()  # nothing will run, so nothing will release it
        raise
    return JSONResponse(status_code=202, content={"run_id": run_id})


@app.post("/agent/cover-letter")
def agent_cover_letter_enqueue(
    req: AgentCoverLetterRequest, background_tasks: BackgroundTasks,
    auth=Depends(verify_api_key_with_usage),
):
    """Enqueue an async cover-letter run. See agent_tailor_enqueue above --
    identical enqueue-time gating, executing agent.tools.run_cover_letter in
    the background instead.
    """
    user_id, tier = _agent_enqueue_precheck(auth, "Cover letter generation")
    jd_text = _resolve_agent_jd_text(user_id, req.jd_text, req.application_id)

    provided_resume = (req.resume_text or "").strip()
    if not provided_resume and not _get_resume_db(user_id):
        raise HTTPException(
            status_code=400,
            detail="Provide resume_text or upload a resume via POST /resume/upload.",
        )

    run_id = agent_runner.new_run_id()
    _acquire_agent_slot()  # see agent_tailor_enqueue: refuse-for-load costs nothing
    try:
        _reserve_agent_run_slot(user_id, tier, "cover_letter", run_id, jd_text, None)
        _log_score_usage(auth, "/agent/cover-letter")

        background_tasks.add_task(
            _in_agent_slot, _run_cover_letter_in_background,
            run_id, user_id, tier, jd_text, provided_resume or None,
            req.company_name or None, req.job_title or None,
        )
    except BaseException:
        _agent_run_slots.release()
        raise
    return JSONResponse(status_code=202, content={"run_id": run_id})


@app.get("/agent/runs/{run_id}")
def agent_run_status(run_id: str, auth=Depends(verify_api_key)):
    """Poll the status of an async /agent/* run.

    Returns ``{"status", "result", "error"}``. 404s for both an unknown
    run_id and a run_id owned by a different user -- this endpoint must
    never reveal whether a foreign run_id exists. ``error`` is always fixed,
    jargon-free copy (see _friendly_agent_error) -- the internal terminal
    class stored on the row is never echoed back to the caller.
    """
    user_id = _get_user_id(auth)
    if user_id is None:
        raise HTTPException(status_code=401, detail="Authentication required.")
    if not CLOUD_AVAILABLE:
        raise HTTPException(status_code=503, detail="Agent runs are unavailable.")

    conn = db_get_conn(cloud_settings.DB_PATH)
    try:
        row = agent_runner.get_run(conn, user_id, run_id)
    finally:
        conn.close()

    if row is None:
        raise HTTPException(status_code=404, detail="Run not found.")

    status = row["status"]
    return {
        "status": status,
        "result": row["result"] if status == "succeeded" else None,
        "error": (
            _friendly_agent_error(row["kind"], row["error"])
            if status == "failed"
            else None
        ),
    }


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
    except jd_fetcher.BlockedURLError as e:
        raise HTTPException(status_code=400, detail=str(e))
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
# JOB APPLICATION TRACKER
# =============================================================================

@app.post("/tracker/add")
def tracker_add(req: TrackerAddRequest, auth=Depends(verify_api_key)):
    """Save a job application to the user's tracker."""
    user_id = _get_user_id(auth)
    if user_id is None:
        raise HTTPException(status_code=401, detail="Authentication required to use the tracker.")
    if not CLOUD_AVAILABLE:
        raise HTTPException(status_code=503, detail="Tracker unavailable — cloud auth not configured.")
    _validate_tracker_classification(req.target_tier, req.fit_label, req.hard_reqs_missed)
    from cloud.auth import get_db
    with get_db() as conn:
        cur = conn.execute(
            """INSERT INTO job_applications
               (user_id, company, job_title, status, resume_file, cover_letter_file,
                ats_score, hr_score, llm_score, notes,
                applied_at, jd_ref, jd_text, target_tier, fit_label, hard_reqs_missed, referral_source)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (user_id, req.company, req.job_title, req.status,
             req.resume_file, req.cover_letter_file,
             req.ats_score, req.hr_score, req.llm_score, req.notes,
             req.applied_at, req.jd_ref, req.jd_text, req.target_tier, req.fit_label,
             req.hard_reqs_missed, req.referral_source),
        )
        return {"id": cur.lastrowid, "status": "added"}


@app.get("/tracker")
def tracker_list(auth=Depends(verify_api_key)):
    """Return all job applications for the authenticated user, newest first."""
    user_id = _get_user_id(auth)
    if user_id is None:
        raise HTTPException(status_code=401, detail="Authentication required.")
    if not CLOUD_AVAILABLE:
        raise HTTPException(status_code=503, detail="Tracker unavailable.")
    from cloud.auth import get_db
    with get_db() as conn:
        rows = conn.execute(
            """SELECT id, company, job_title, status, resume_file, cover_letter_file,
                      ats_score, hr_score, llm_score, notes, created_at, updated_at,
                      applied_at, jd_ref, target_tier, fit_label, hard_reqs_missed,
                      referral_source, rejection_reason, rejection_detail,
                      interview_stage, responded_at, closed_at
               FROM job_applications WHERE user_id = ?
               ORDER BY updated_at DESC, created_at DESC""",
            (user_id,),
        ).fetchall()
    return {"applications": [dict(r) for r in rows]}


@app.put("/tracker/{entry_id}")
def tracker_update(entry_id: int, req: TrackerUpdateRequest, auth=Depends(verify_api_key)):
    """Update status / notes / classification fields for a tracker entry owned by the user."""
    user_id = _get_user_id(auth)
    if user_id is None:
        raise HTTPException(status_code=401, detail="Authentication required.")
    if not CLOUD_AVAILABLE:
        raise HTTPException(status_code=503, detail="Tracker unavailable.")
    _validate_tracker_classification(req.target_tier, req.fit_label, req.hard_reqs_missed)
    from cloud.auth import get_db
    updates = {}
    if req.status is not None:
        updates["status"] = req.status
    if req.notes is not None:
        updates["notes"] = req.notes
    if req.resume_file is not None:
        updates["resume_file"] = req.resume_file
    if req.cover_letter_file is not None:
        updates["cover_letter_file"] = req.cover_letter_file
    if req.applied_at is not None:
        updates["applied_at"] = req.applied_at
    if req.jd_ref is not None:
        updates["jd_ref"] = req.jd_ref
    if req.jd_text is not None:
        updates["jd_text"] = req.jd_text
    if req.target_tier is not None:
        updates["target_tier"] = req.target_tier
    if req.fit_label is not None:
        updates["fit_label"] = req.fit_label
    if req.hard_reqs_missed is not None:
        updates["hard_reqs_missed"] = req.hard_reqs_missed
    if req.referral_source is not None:
        updates["referral_source"] = req.referral_source
    if not updates:
        return {"status": "no_change"}
    updates["updated_at"] = "datetime('now')"
    set_clause = ", ".join(
        f"{k} = datetime('now')" if k == "updated_at" else f"{k} = ?"
        for k in updates
    )
    values = [v for k, v in updates.items() if k != "updated_at"]
    values += [entry_id, user_id]
    with get_db() as conn:
        conn.execute(
            f"UPDATE job_applications SET {set_clause} WHERE id = ? AND user_id = ?",
            values,
        )
    return {"status": "updated"}


@app.delete("/tracker/{entry_id}")
def tracker_delete(entry_id: int, auth=Depends(verify_api_key)):
    """Delete a tracker entry owned by the user."""
    user_id = _get_user_id(auth)
    if user_id is None:
        raise HTTPException(status_code=401, detail="Authentication required.")
    if not CLOUD_AVAILABLE:
        raise HTTPException(status_code=503, detail="Tracker unavailable.")
    from cloud.auth import get_db
    with get_db() as conn:
        conn.execute(
            "DELETE FROM job_applications WHERE id = ? AND user_id = ?",
            (entry_id, user_id),
        )
    return {"status": "deleted"}


@app.post("/tracker/{entry_id}/outcome")
def tracker_outcome(entry_id: int, req: TrackerOutcomeRequest, auth=Depends(verify_api_key)):
    """Record a rejection/withdrawal outcome for a tracker entry owned by the user.

    Cross-user access returns 404 (never 403) so the endpoint never confirms
    or denies that another user's entry_id exists.
    """
    user_id = _get_user_id(auth)
    if user_id is None:
        raise HTTPException(status_code=401, detail="Authentication required.")
    if not CLOUD_AVAILABLE:
        raise HTTPException(status_code=503, detail="Tracker unavailable.")
    if req.rejection_reason not in _VALID_REJECTION_REASONS:
        raise HTTPException(
            status_code=422,
            detail=f"rejection_reason must be one of {sorted(_VALID_REJECTION_REASONS)}; got {req.rejection_reason!r}.",
        )
    if req.interview_stage is not None and not (0 <= req.interview_stage <= 5):
        raise HTTPException(
            status_code=422,
            detail=f"interview_stage must be an integer between 0 and 5, or null; got {req.interview_stage!r}.",
        )

    new_status = "Withdrawn" if req.rejection_reason == "withdrawn" else "Rejected"

    from cloud.auth import get_db
    with get_db() as conn:
        owned = conn.execute(
            "SELECT id FROM job_applications WHERE id = ? AND user_id = ?",
            (entry_id, user_id),
        ).fetchone()
        if owned is None:
            raise HTTPException(status_code=404, detail="Tracker entry not found.")

        # Partial-update idiom (matches tracker_update): only touch
        # rejection_detail/interview_stage when this call actually supplied
        # them, so a repeat call that omits one doesn't null out a value a
        # prior call recorded. responded_at is skipped entirely for
        # no_response -- the company never replied, so nothing should stamp
        # a reply time (that would fabricate a 100% response_rate and a
        # bogus days-to-response value in /insights/pipeline).
        set_clauses = ["rejection_reason = ?"]
        values = [req.rejection_reason]
        if req.detail is not None:
            set_clauses.append("rejection_detail = ?")
            values.append(req.detail)
        if req.interview_stage is not None:
            set_clauses.append("interview_stage = ?")
            values.append(req.interview_stage)
        if req.rejection_reason != "no_response":
            set_clauses.append("responded_at = COALESCE(responded_at, datetime('now'))")
        set_clauses.append("closed_at = datetime('now')")
        set_clauses.append("status = ?")
        values.append(new_status)
        set_clauses.append("updated_at = datetime('now')")
        values += [entry_id, user_id]

        conn.execute(
            f"UPDATE job_applications SET {', '.join(set_clauses)} WHERE id = ? AND user_id = ?",
            values,
        )
        conn.execute(
            """INSERT INTO events (user_id, application_id, kind, payload)
               VALUES (?, ?, ?, ?)""",
            (
                user_id,
                entry_id,
                "outcome",
                json.dumps({
                    "rejection_reason": req.rejection_reason,
                    "interview_stage": req.interview_stage,
                    "detail": req.detail,
                }),
            ),
        )

    return {"id": entry_id, "status": "outcome_recorded", "new_status": new_status}


# =============================================================================
# APPLICATION-ASSIST PROFILE
# =============================================================================

@app.get("/profile")
def profile_get(auth=Depends(verify_api_key)):
    """The caller's application profile, or {"profile": null} if never saved."""
    user_id = _get_user_id(auth)
    if user_id is None:
        raise HTTPException(status_code=401, detail="Authentication required.")
    if not CLOUD_AVAILABLE:
        raise HTTPException(status_code=503, detail="Profile unavailable — cloud auth not configured.")
    conn = db_get_conn(cloud_settings.DB_PATH)
    try:
        row = scoped(conn, user_id).q(
            "SELECT * FROM user_profile WHERE user_id = :user_id", {}
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        return {"profile": None}
    profile = {k: row[k] for k in _PROFILE_TEXT_FIELDS}
    profile["references_on_request"] = bool(row["references_on_request"])
    profile["updated_at"] = row["updated_at"]
    return {"profile": profile}


@app.put("/profile")
def profile_put(req: ProfileUpdateRequest, auth=Depends(verify_api_key)):
    """Full-replace upsert of the caller's application profile."""
    user_id = _get_user_id(auth)
    if user_id is None:
        raise HTTPException(status_code=401, detail="Authentication required.")
    if not CLOUD_AVAILABLE:
        raise HTTPException(status_code=503, detail="Profile unavailable — cloud auth not configured.")
    params = {f: getattr(req, f) for f in _PROFILE_TEXT_FIELDS}
    params["references_on_request"] = int(req.references_on_request)
    params["updated_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    conn = db_get_conn(cloud_settings.DB_PATH)
    try:
        scoped(conn, user_id).q(
            """
            INSERT OR REPLACE INTO user_profile
                (user_id, full_name, email, phone, city, state, postcode, country,
                 linkedin_url, portfolio_url, work_authorization, notice_period,
                 desired_salary, references_on_request, updated_at)
            VALUES
                (:user_id, :full_name, :email, :phone, :city, :state, :postcode, :country,
                 :linkedin_url, :portfolio_url, :work_authorization, :notice_period,
                 :desired_salary, :references_on_request, :updated_at)
            """,
            params,
        )
        conn.commit()
    finally:
        conn.close()
    return {"status": "saved"}


# =============================================================================
# FEEDBACK — signed-in or anonymous; the only write anonymous users get
# =============================================================================


class FeedbackRequest(BaseModel):
    """A message from a user, with an optional reply-to email."""
    message: str = Field(..., description="The feedback text")
    email: str = Field("", description="Optional reply-to email")
    page: str = Field("", description="Where in the app it was sent from")


@app.post("/feedback")
def feedback_submit(req: FeedbackRequest, auth=Depends(verify_api_key)):
    """Store feedback. Anonymous is allowed on purpose — launch-day visitors
    without accounts are exactly who we want to hear from. Rate limiting
    rides on verify_api_key; size caps stop paste-bombs."""
    if not CLOUD_AVAILABLE:
        raise HTTPException(status_code=503, detail="Feedback unavailable right now.")
    message = req.message.strip()
    if not message:
        raise HTTPException(status_code=400, detail="Write a little something first.")
    if len(message) > 5000:
        raise HTTPException(status_code=400, detail="Please keep feedback under 5000 characters.")
    if len(req.email) > 254 or len(req.page) > 200:
        raise HTTPException(status_code=400, detail="Invalid field length.")
    user_id = _get_user_id(auth)  # None for anonymous — stored as NULL
    conn = db_get_conn(cloud_settings.DB_PATH)
    try:
        conn.execute(
            "INSERT INTO feedback (user_id, email, message, page) VALUES (?, ?, ?, ?)",
            (user_id, req.email.strip(), message, req.page.strip()),
        )
        conn.commit()
    finally:
        conn.close()
    return {"status": "received"}


@app.get("/admin/feedback")
def feedback_list(request: Request):
    """All feedback, newest first. Admin secret required."""
    _verify_admin_secret(request)
    if not CLOUD_AVAILABLE:
        raise HTTPException(status_code=503, detail="Feedback unavailable.")
    conn = db_get_conn(cloud_settings.DB_PATH)
    try:
        rows = conn.execute(
            "SELECT id, user_id, email, message, page, created_at "
            "FROM feedback ORDER BY id DESC LIMIT 200"
        ).fetchall()
    finally:
        conn.close()
    return {"feedback": [dict(r) for r in rows]}


# =============================================================================
# CONNECTIONS — warm-path referrals from the user's own LinkedIn export
# =============================================================================

_CONNECTIONS_MAX_BYTES = 5_000_000
_CONNECTIONS_MAX_ROWS = 30_000
_COMPANY_SUFFIXES = {"inc", "llc", "ltd", "corp", "co", "plc", "gmbh", "company", "corporation"}


class ConnectionsUploadRequest(BaseModel):
    """Raw text of the user's LinkedIn Connections.csv export."""
    csv_text: str = Field(..., description="Contents of Connections.csv from LinkedIn's data export")


def _normalize_company(name: str) -> str:
    """Lowercase, strip punctuation and legal suffixes: 'Pfizer, Inc.' -> 'pfizer'."""
    import re as _re
    words = _re.sub(r"[^a-z0-9 ]", " ", name.lower()).split()
    while words and words[-1] in _COMPANY_SUFFIXES:
        words.pop()
    return " ".join(words)


def _parse_linkedin_connections(csv_text: str) -> list[dict]:
    """Parse Connections.csv, skipping LinkedIn's 'Notes:' preamble.

    The export prepends a free-text notes block; the real data starts at the
    header row containing First Name/Company. Rows without a name are noise.
    """
    import csv as _csv
    import io as _io

    lines = csv_text.splitlines()
    header_idx = next(
        (i for i, line in enumerate(lines)
         if "First Name" in line and "Company" in line),
        None,
    )
    if header_idx is None:
        raise HTTPException(
            status_code=400,
            detail="That doesn't look like a LinkedIn Connections.csv export — "
                   "download it from LinkedIn: Settings → Data privacy → Get a copy of your data.",
        )
    reader = _csv.DictReader(_io.StringIO("\n".join(lines[header_idx:])))
    rows = []
    for row in reader:
        full_name = f"{(row.get('First Name') or '').strip()} {(row.get('Last Name') or '').strip()}".strip()
        if not full_name:
            continue
        rows.append({
            "full_name": full_name,
            "company": (row.get("Company") or "").strip(),
            "position": (row.get("Position") or "").strip(),
            "profile_url": (row.get("URL") or "").strip(),
            "connected_on": (row.get("Connected On") or "").strip(),
        })
        if len(rows) >= _CONNECTIONS_MAX_ROWS:
            break
    return rows


def _require_connections_user(auth) -> int:
    user_id = _get_user_id(auth)
    if user_id is None:
        raise HTTPException(status_code=401, detail="Authentication required.")
    if not CLOUD_AVAILABLE:
        raise HTTPException(status_code=503, detail="Connections unavailable — cloud auth not configured.")
    return user_id


@app.post("/connections/upload")
def connections_upload(req: ConnectionsUploadRequest, auth=Depends(verify_api_key)):
    """Replace the caller's stored network with a fresh Connections.csv."""
    user_id = _require_connections_user(auth)
    if len(req.csv_text.encode("utf-8", errors="ignore")) > _CONNECTIONS_MAX_BYTES:
        raise HTTPException(status_code=400, detail="File too large — the Connections.csv export is expected.")
    rows = _parse_linkedin_connections(req.csv_text)
    conn = db_get_conn(cloud_settings.DB_PATH)
    try:
        s = scoped(conn, user_id)
        s.q("DELETE FROM connections WHERE user_id = :user_id", {})
        for r in rows:
            s.q(
                """INSERT OR IGNORE INTO connections
                   (user_id, full_name, company, position, profile_url, connected_on)
                   VALUES (:user_id, :full_name, :company, :position, :profile_url, :connected_on)""",
                r,
            )
        count = s.q("SELECT COUNT(*) AS n FROM connections WHERE user_id = :user_id", {}).fetchone()["n"]
        conn.commit()
    finally:
        conn.close()
    return {"imported": count}


@app.get("/connections")
def connections_summary(auth=Depends(verify_api_key)):
    """How many connections the caller has stored (the profile-card number)."""
    user_id = _require_connections_user(auth)
    conn = db_get_conn(cloud_settings.DB_PATH)
    try:
        row = scoped(conn, user_id).q(
            "SELECT COUNT(*) AS n FROM connections WHERE user_id = :user_id", {}
        ).fetchone()
    finally:
        conn.close()
    return {"count": row["n"]}


@app.get("/connections/match")
def connections_match(company: str, auth=Depends(verify_api_key)):
    """Connections at a company, matched loosely in both directions.

    'Pfizer' must find 'Pfizer, Inc.' and a job at 'Moderna Therapeutics'
    must find a connection stored as 'Moderna'. Very short normalized names
    only match exactly, so 'Co' can't match half the table.
    """
    user_id = _require_connections_user(auth)
    query_norm = _normalize_company(company)
    if not query_norm:
        return {"connections": []}
    conn = db_get_conn(cloud_settings.DB_PATH)
    try:
        rows = scoped(conn, user_id).q(
            "SELECT full_name, company, position, profile_url FROM connections "
            "WHERE user_id = :user_id AND company != '' ORDER BY full_name",
            {},
        ).fetchall()
    finally:
        conn.close()
    matches = []
    for r in rows:
        stored_norm = _normalize_company(r["company"])
        if not stored_norm:
            continue
        if min(len(stored_norm), len(query_norm)) < 3:
            hit = stored_norm == query_norm
        else:
            hit = stored_norm in query_norm or query_norm in stored_norm
        if hit:
            matches.append({k: r[k] for k in ("full_name", "company", "position", "profile_url")})
    return {"connections": matches}


@app.delete("/connections")
def connections_delete(auth=Depends(verify_api_key)):
    """Wipe the caller's stored network entirely."""
    user_id = _require_connections_user(auth)
    conn = db_get_conn(cloud_settings.DB_PATH)
    try:
        scoped(conn, user_id).q("DELETE FROM connections WHERE user_id = :user_id", {})
        conn.commit()
    finally:
        conn.close()
    return {"status": "deleted"}


# =============================================================================
# CAREER DATA — the saved resume parsed ONCE into structure, reused forever
# =============================================================================
# The rule that makes autofill cheap: AI runs once per resume, never once per
# application page. A parse is keyed on the SHA-256 of the saved resume text,
# so re-opening the same resume costs nothing and a genuinely new resume costs
# one call. Once the user corrects the parse (PUT), verified=1 and that record
# outranks whatever an ATS's own resume parser would have produced.

_CAREER_KEYS = ("work_history", "education", "certifications", "skills")
_CAREER_WORK_FIELDS = ("title", "company", "location", "start_month", "end_month")
_CAREER_EDU_FIELDS = ("degree", "school", "field", "start_year", "end_year")
_CAREER_MAX_WORK_ENTRIES = 200
_CAREER_MAX_BYTES = 100_000

_CAREER_PARSE_PROMPT = """Extract this resume into structured JSON.

Rules:
- Copy facts verbatim from the resume. Never invent, infer, or embellish.
- Omit anything the resume does not state; use "" for an unknown string field.
- Months use "YYYY-MM"; years use "YYYY". Set "current": true only for a role
  the resume shows as ongoing (Present/Current), and leave its end_month "".
- bullets are the role's achievement lines, copied as written.

Return ONLY a JSON object with exactly these top-level keys, no prose, no
markdown fences:

{{
  "work_history": [
    {{"title": "", "company": "", "location": "", "start_month": "",
      "end_month": "", "current": false, "bullets": [""]}}
  ],
  "education": [
    {{"degree": "", "school": "", "field": "", "start_year": "", "end_year": ""}}
  ],
  "certifications": [""],
  "skills": [""]
}}

RESUME:
{resume_text}"""


def _career_llm_call(prompt: str) -> str:
    """One Anthropic completion for resume structuring.

    Deliberately a tiny seam of its own: it is the only network call on the
    career path, so tests substitute this function and assert the
    once-per-digest contract instead of mocking the SDK. Reuses llm_scorer's
    env-override convention (ANTHROPIC_MODEL, ANTHROPIC_API_KEY).
    """
    try:
        import anthropic
    except ImportError:
        raise HTTPException(
            status_code=503,
            detail="Resume parsing unavailable — anthropic package not installed.",
        )
    client = anthropic.Anthropic()
    response = client.messages.create(
        model=os.getenv("ANTHROPIC_MODEL", "claude-sonnet-4-6"),
        max_tokens=8000,
        temperature=0,
        messages=[{"role": "user", "content": prompt}],
    )
    return response.content[0].text.strip()


def _career_str(value) -> str:
    """Coerce a scalar career field to text; reject containers.

    Models return years as ints often enough that strict typing would 502 on
    otherwise-good output, so numbers are accepted and stringified.
    """
    if value is None:
        return ""
    if isinstance(value, bool) or isinstance(value, (dict, list)):
        raise ValueError("expected a text field")
    if isinstance(value, (str, int, float)):
        return str(value).strip()
    raise ValueError("expected a text field")


def _normalize_career_data(raw) -> dict:
    """Validate and canonicalize a career blob. Raises ValueError on bad shape."""
    if not isinstance(raw, dict):
        raise ValueError("career data must be a JSON object")

    work = raw.get("work_history") or []
    if not isinstance(work, list):
        raise ValueError("work_history must be a list")
    if len(work) > _CAREER_MAX_WORK_ENTRIES:
        raise ValueError(
            f"work_history is limited to {_CAREER_MAX_WORK_ENTRIES} entries"
        )
    work_out = []
    for item in work:
        if not isinstance(item, dict):
            raise ValueError("each work_history entry must be an object")
        bullets = item.get("bullets") or []
        if not isinstance(bullets, list):
            raise ValueError("bullets must be a list of strings")
        entry = {f: _career_str(item.get(f)) for f in _CAREER_WORK_FIELDS}
        entry["current"] = bool(item.get("current"))
        entry["bullets"] = [_career_str(b) for b in bullets]
        work_out.append(entry)

    education = raw.get("education") or []
    if not isinstance(education, list):
        raise ValueError("education must be a list")
    edu_out = []
    for item in education:
        if not isinstance(item, dict):
            raise ValueError("each education entry must be an object")
        edu_out.append({f: _career_str(item.get(f)) for f in _CAREER_EDU_FIELDS})

    simple = {}
    for key in ("certifications", "skills"):
        values = raw.get(key) or []
        if not isinstance(values, list):
            raise ValueError(f"{key} must be a list of strings")
        simple[key] = [_career_str(v) for v in values]

    return {
        "work_history": work_out,
        "education": edu_out,
        "certifications": simple["certifications"],
        "skills": simple["skills"],
    }


def _career_json_from_model(text: str) -> dict:
    """Parse a model response into normalized career data, or raise ValueError."""
    body = (text or "").strip()
    if body.startswith("```"):
        body = "\n".join(
            line for line in body.split("\n") if not line.strip().startswith("```")
        )
    try:
        raw = json.loads(body)
    except json.JSONDecodeError as exc:
        raise ValueError(f"model did not return JSON: {exc}")
    if not isinstance(raw, dict) or any(k not in raw for k in _CAREER_KEYS):
        raise ValueError("model response is missing required top-level keys")
    return _normalize_career_data(raw)


def _require_autofill_user(auth, feature: str) -> int:
    """Authenticated-account gate shared by the autofill endpoints."""
    user_id = _get_user_id(auth)
    if user_id is None:
        raise HTTPException(status_code=401, detail="Authentication required.")
    if not CLOUD_AVAILABLE:
        raise HTTPException(
            status_code=503,
            detail=f"{feature} unavailable — cloud auth not configured.",
        )
    return user_id


def _career_size_guard(payload: dict) -> str:
    """Serialize career data, refusing anything too large to store."""
    blob = json.dumps(payload, ensure_ascii=False)
    if len(blob.encode("utf-8")) > _CAREER_MAX_BYTES:
        raise HTTPException(
            status_code=400,
            detail=f"Career data is too large (limit {_CAREER_MAX_BYTES // 1000}KB).",
        )
    return blob


class CareerParseRequest(BaseModel):
    """Options for POST /career/parse (the body is optional)."""
    force: bool = Field(False, description="Re-parse even if the resume digest is unchanged")


@app.post("/career/parse")
def career_parse(
    req: Optional[CareerParseRequest] = None, auth=Depends(verify_api_key)
):
    """Structure the caller's saved resume — one LLM call per resume digest."""
    user_id = _require_autofill_user(auth, "Career data")
    force = bool(req.force) if req is not None else False

    record = _get_resume_db(user_id)
    if not record or not (record.get("resume_text") or "").strip():
        raise HTTPException(
            status_code=400,
            detail="No resume on file — upload one first (POST /resume/upload).",
        )
    resume_text = record["resume_text"]
    digest = hashlib.sha256(resume_text.encode("utf-8")).hexdigest()

    conn = db_get_conn(cloud_settings.DB_PATH)
    try:
        row = scoped(conn, user_id).q(
            "SELECT data, resume_digest FROM career_data WHERE user_id = :user_id", {}
        ).fetchone()
        if row is not None and row["resume_digest"] == digest and not force:
            try:
                cached = json.loads(row["data"])
            except json.JSONDecodeError:
                cached = None
            if cached is not None:
                return {"career": cached, "cached": True}
    finally:
        conn.close()

    prompt = _CAREER_PARSE_PROMPT.format(resume_text=resume_text)
    last_error = None
    parsed = None
    # One retry: a single malformed response is usually a truncated or
    # fence-wrapped fluke, and a second attempt is cheaper than a failed run.
    for _ in range(2):
        try:
            parsed = _career_json_from_model(_career_llm_call(prompt))
            break
        except ValueError as exc:
            last_error = exc
    if parsed is None:
        raise HTTPException(
            status_code=502,
            detail=f"Could not structure the resume: {last_error}",
        )

    blob = _career_size_guard(parsed)
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    conn = db_get_conn(cloud_settings.DB_PATH)
    try:
        scoped(conn, user_id).q(
            """
            INSERT OR REPLACE INTO career_data
                (user_id, data, resume_digest, verified, parsed_at, updated_at)
            VALUES
                (:user_id, :data, :resume_digest, 0, :now, :now)
            """,
            {"data": blob, "resume_digest": digest, "now": now},
        )
        conn.commit()
    finally:
        conn.close()
    return {"career": parsed, "cached": False}


@app.get("/career")
def career_get(auth=Depends(verify_api_key)):
    """The caller's structured career data, or {"career": null}."""
    user_id = _require_autofill_user(auth, "Career data")
    conn = db_get_conn(cloud_settings.DB_PATH)
    try:
        row = scoped(conn, user_id).q(
            "SELECT data, verified, parsed_at, updated_at FROM career_data "
            "WHERE user_id = :user_id",
            {},
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        return {"career": None}
    try:
        data = json.loads(row["data"])
    except json.JSONDecodeError:
        return {"career": None}
    data["verified"] = bool(row["verified"])
    data["parsed_at"] = row["parsed_at"]
    data["updated_at"] = row["updated_at"]
    return {"career": data}


@app.put("/career")
def career_put(payload: Dict[str, Any], auth=Depends(verify_api_key)):
    """Full-replace user correction. What the user saves here is ground truth.

    resume_digest is deliberately left untouched: the correction belongs to the
    resume that was parsed, so a later unforced parse serves the corrected
    record from cache instead of overwriting it.
    """
    user_id = _require_autofill_user(auth, "Career data")
    try:
        data = _normalize_career_data(payload)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    blob = _career_size_guard(data)
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    conn = db_get_conn(cloud_settings.DB_PATH)
    try:
        scoped(conn, user_id).q(
            """
            INSERT INTO career_data
                (user_id, data, resume_digest, verified, parsed_at, updated_at)
            VALUES
                (:user_id, :data, '', 1, NULL, :now)
            ON CONFLICT(user_id) DO UPDATE SET
                data = excluded.data,
                verified = 1,
                updated_at = excluded.updated_at
            """,
            {"data": blob, "now": now},
        )
        conn.commit()
    finally:
        conn.close()
    return {"status": "saved"}


@app.delete("/career")
def career_delete(auth=Depends(verify_api_key)):
    """Wipe the caller's structured career data."""
    user_id = _require_autofill_user(auth, "Career data")
    conn = db_get_conn(cloud_settings.DB_PATH)
    try:
        scoped(conn, user_id).q(
            "DELETE FROM career_data WHERE user_id = :user_id", {}
        )
        conn.commit()
    finally:
        conn.close()
    return {"status": "deleted"}


# =============================================================================
# ANSWER BANK — every application question answered once, offered forever
# =============================================================================

_ANSWER_MAX_QUESTION = 1000
_ANSWER_MAX_ANSWER = 10_000
_ANSWER_MATCH_LIMIT = 3
_ANSWER_MATCH_THRESHOLD = 0.75

# EEO/demographic questions are never stored and never matched. This is a hard
# product boundary, not a heuristic: answering them is the applicant's choice
# alone, and a remembered answer would be auto-disclosed on every later form.
# Word-boundary matching on the NORMALIZED text, so "embrace" is not "race".
_EEO_DENYLIST = re.compile(
    r"\b(?:gender|race|ethnicity|veteran|disabilit(?:y|ies)|sexual orientation"
    r"|religion|religious|pregnancy|national origin)\b"
)


def _normalize_question(text: str) -> str:
    """Lowercase, punctuation to spaces, whitespace collapsed — the lookup key."""
    return " ".join(re.sub(r"[^\w\s]+", " ", (text or "").lower()).split())


def _reject_if_eeo(question_norm: str) -> None:
    if _EEO_DENYLIST.search(question_norm):
        raise HTTPException(
            status_code=400,
            detail="EEO and demographic questions are never stored or answered "
                   "from memory — answer those yourself, on the form.",
        )


class AnswerSaveRequest(BaseModel):
    """One application question and the answer to remember for it."""
    question_text: str = Field(..., description="The question exactly as the form asked it")
    answer_text: str = Field(..., description="The answer to reuse next time")


def _answer_row(row) -> dict:
    return {
        "id": row["id"],
        "question_text": row["question_text"],
        "answer_text": row["answer_text"],
        "times_used": row["times_used"],
        "updated_at": row["updated_at"],
    }


@app.get("/answers")
def answers_list(auth=Depends(verify_api_key)):
    """The caller's saved answers, most recently updated first."""
    user_id = _require_autofill_user(auth, "Answer bank")
    conn = db_get_conn(cloud_settings.DB_PATH)
    try:
        rows = scoped(conn, user_id).q(
            "SELECT id, question_text, answer_text, times_used, updated_at "
            "FROM answer_bank WHERE user_id = :user_id "
            "ORDER BY updated_at DESC, id DESC",
            {},
        ).fetchall()
    finally:
        conn.close()
    return {"answers": [_answer_row(r) for r in rows]}


@app.post("/answers")
def answers_save(req: AnswerSaveRequest, auth=Depends(verify_api_key)):
    """Save or update the answer for a question, keyed on its normalized form."""
    user_id = _require_autofill_user(auth, "Answer bank")
    question_text = (req.question_text or "").strip()
    answer_text = (req.answer_text or "").strip()
    if len(question_text) > _ANSWER_MAX_QUESTION:
        raise HTTPException(
            status_code=400,
            detail=f"Question is too long (limit {_ANSWER_MAX_QUESTION} characters).",
        )
    if len(answer_text) > _ANSWER_MAX_ANSWER:
        raise HTTPException(
            status_code=400,
            detail=f"Answer is too long (limit {_ANSWER_MAX_ANSWER} characters).",
        )
    question_norm = _normalize_question(question_text)
    if not question_norm:
        raise HTTPException(status_code=400, detail="Question text is required.")
    if not answer_text:
        raise HTTPException(status_code=400, detail="Answer text is required.")
    _reject_if_eeo(question_norm)

    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    conn = db_get_conn(cloud_settings.DB_PATH)
    try:
        s = scoped(conn, user_id)
        s.q(
            """
            INSERT INTO answer_bank
                (user_id, question_text, question_norm, answer_text, updated_at)
            VALUES
                (:user_id, :question_text, :question_norm, :answer_text, :now)
            ON CONFLICT(user_id, question_norm) DO UPDATE SET
                question_text = excluded.question_text,
                answer_text = excluded.answer_text,
                updated_at = excluded.updated_at
            """,
            {
                "question_text": question_text,
                "question_norm": question_norm,
                "answer_text": answer_text,
                "now": now,
            },
        )
        row = s.q(
            "SELECT id FROM answer_bank "
            "WHERE user_id = :user_id AND question_norm = :question_norm",
            {"question_norm": question_norm},
        ).fetchone()
        conn.commit()
    finally:
        conn.close()
    return {"id": row["id"]}


@app.get("/answers/match")
def answers_match(q: str, auth=Depends(verify_api_key)):
    """Find saved answers for a question: exact normalized form, then SBERT.

    The embedding step reuses the single model instance ats_scorer already
    loads for semantic scoring — a second SentenceTransformer would double the
    server's memory for no benefit. If that model is missing or throws, the
    ladder degrades to exact-match-only rather than failing the request.
    """
    user_id = _require_autofill_user(auth, "Answer bank")
    query_norm = _normalize_question(q)
    if not query_norm:
        return {"matches": []}
    _reject_if_eeo(query_norm)

    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    conn = db_get_conn(cloud_settings.DB_PATH)
    try:
        s = scoped(conn, user_id)
        rows = s.q(
            "SELECT id, question_text, question_norm, answer_text, times_used, updated_at "
            "FROM answer_bank WHERE user_id = :user_id",
            {},
        ).fetchall()
        exact = [r for r in rows if r["question_norm"] == query_norm]
        if exact:
            for r in exact[:_ANSWER_MATCH_LIMIT]:
                s.q(
                    "UPDATE answer_bank SET times_used = times_used + 1, "
                    "last_used_at = :now WHERE user_id = :user_id AND id = :id",
                    {"now": now, "id": r["id"]},
                )
            conn.commit()
    finally:
        conn.close()

    if exact:
        return {
            "matches": [
                {**_answer_row(r), "score": 1.0} for r in exact[:_ANSWER_MATCH_LIMIT]
            ]
        }

    scored = []
    try:
        if ats_scorer.get_sbert_model() is not None:
            import numpy as _np

            query_vec = ats_scorer.embed_with_cache(query_norm)
            if query_vec is not None:
                query_norm_len = float(_np.linalg.norm(query_vec))
                for r in rows:
                    vec = ats_scorer.embed_with_cache(r["question_norm"])
                    denom = query_norm_len * float(_np.linalg.norm(vec))
                    if not denom:
                        continue
                    score = float(_np.dot(query_vec, vec)) / denom
                    if score >= _ANSWER_MATCH_THRESHOLD:
                        scored.append((score, r))
    except Exception:
        # Embedding is a nice-to-have on this path; the exact rung above is the
        # guarantee. Never turn a model problem into a failed lookup.
        logging.getLogger("scorer.answers").warning(
            "answer match fell back to exact-only", exc_info=True
        )
        scored = []

    scored.sort(key=lambda pair: pair[0], reverse=True)
    return {
        "matches": [
            {**_answer_row(r), "score": round(score, 4)}
            for score, r in scored[:_ANSWER_MATCH_LIMIT]
        ]
    }


@app.delete("/answers/{answer_id}")
def answers_delete(answer_id: int, auth=Depends(verify_api_key)):
    """Delete one saved answer. 404 unless it belongs to the caller."""
    user_id = _require_autofill_user(auth, "Answer bank")
    conn = db_get_conn(cloud_settings.DB_PATH)
    try:
        s = scoped(conn, user_id)
        owned = s.q(
            "SELECT id FROM answer_bank WHERE user_id = :user_id AND id = :id",
            {"id": answer_id},
        ).fetchone()
        if owned is None:
            raise HTTPException(status_code=404, detail="Answer not found.")
        s.q(
            "DELETE FROM answer_bank WHERE user_id = :user_id AND id = :id",
            {"id": answer_id},
        )
        conn.commit()
    finally:
        conn.close()
    return {"status": "deleted"}


# =============================================================================
# FIELD MAPPINGS — global cache, no user data by construction
# =============================================================================
# A row says "this vendor field means desired_salary". It holds no values, so
# it is shareable across every account, which is the point: the second user
# anywhere pays nothing for a field the first one resolved.

_MAPPINGS_MAX_FIELDS = 200


class FieldDescriptor(BaseModel):
    """One form field as the extension sees it — structure only, no values."""
    sig: str = Field(..., max_length=200, description="Stable hash of label + automation id + input type")
    label: str = Field("", max_length=500)
    input_type: str = Field("", max_length=50)


class MappingResolveRequest(BaseModel):
    """A page's worth of unresolved fields for one ATS vendor."""
    ats_vendor: str = Field(..., max_length=100)
    fields: List[FieldDescriptor] = Field(default_factory=list)


@app.post("/mappings/resolve")
def mappings_resolve(req: MappingResolveRequest, auth=Depends(verify_api_key)):
    """Resolve form fields against the global mapping cache.

    Cache-only on purpose. The plan's resolution order ends in an LLM call on a
    global first-ever miss, but that spend is deliberately NOT wired up until
    the extension exists and real field signatures are arriving — an open
    endpoint that bills a model per unknown field is a cost incident waiting to
    happen. Unknown fields are recorded with profile_key NULL and source
    'queued' for the review/LLM pass to fill in later.
    """
    _require_autofill_user(auth, "Field mappings")
    vendor = (req.ats_vendor or "").strip().lower()
    if not vendor:
        raise HTTPException(status_code=400, detail="ats_vendor is required.")
    if len(req.fields) > _MAPPINGS_MAX_FIELDS:
        raise HTTPException(
            status_code=400,
            detail=f"Too many fields in one request (limit {_MAPPINGS_MAX_FIELDS}).",
        )

    results = []
    conn = db_get_conn(cloud_settings.DB_PATH)
    try:
        for field in req.fields:
            sig = (field.sig or "").strip()
            if not sig:
                raise HTTPException(status_code=400, detail="Every field needs a sig.")
            # Global table: scoped() is intentionally NOT used here — there is
            # no user_id column and nothing user-specific to protect.
            row = conn.execute(
                "SELECT profile_key, source FROM field_mappings "
                "WHERE ats_vendor = ? AND field_sig = ?",
                (vendor, sig),
            ).fetchone()
            if row is None:
                conn.execute(
                    "INSERT OR IGNORE INTO field_mappings "
                    "(ats_vendor, field_sig, label, profile_key, confidence, source) "
                    "VALUES (?, ?, ?, NULL, 0, 'queued')",
                    (vendor, sig, field.label or ""),
                )
                results.append({"sig": sig, "profile_key": None, "source": "queued"})
            else:
                results.append(
                    {"sig": sig, "profile_key": row["profile_key"], "source": row["source"]}
                )
        conn.commit()
    finally:
        conn.close()
    return {"mappings": results}


# =============================================================================
# PIPELINE INSIGHTS — deterministic SQL aggregates only, no LLM involved
# =============================================================================

_TRACKER_BREAKDOWN_COLUMNS = {"target_tier", "fit_label", "referral_source"}


# --- what "rejected", "closed", and "responded" actually mean -----------------
# Three aggregates were measuring things they did not claim to measure.
#
# rejected matched the literal string 'Rejected', but CLAUDE.md documents
# "Rejected after phone screen" as a legitimate status and the Excel importer
# copies whatever the spreadsheet said, so real rejections went uncounted. The
# typed rejection_reason is the reliable signal; status is free text.
#
# closed/active keyed on closed_at, which is written by exactly one endpoint
# (POST /tracker/{id}/outcome). Anything closed by any other path -- an
# imported row, a status edit -- stayed "active" forever, so active could
# exceed the total.
#
# responded counted responded_at, which is only ever set alongside a rejection.
# The metric therefore could not exceed the rejection rate: a user with a 100%
# interview rate and no rejections was shown 0%. What a job seeker means by
# "they got back to me" is that the employer engaged at all -- an interview
# stage reached, or any outcome other than silence.
_REJECTED_SQL = (
    "rejection_reason IN ('auto_reject', 'screen_reject', 'interview_reject')"
)
_CLOSED_SQL = "(closed_at IS NOT NULL OR rejection_reason IS NOT NULL)"
_RESPONDED_SQL = (
    "(responded_at IS NOT NULL"
    " OR COALESCE(interview_stage, 0) > 0"
    " OR (rejection_reason IS NOT NULL AND rejection_reason != 'no_response'))"
)


def _tracker_pipeline_breakdown(conn, user_id: int, column: str) -> Dict[str, Dict[str, Any]]:
    """Group the caller's job_applications rows by one classification column.

    `column` is never derived from request input — it is always one of the
    three hardcoded literals in _TRACKER_BREAKDOWN_COLUMNS. SQLite has no way
    to bind column/identifier names as query parameters (only values), so the
    identifier has to be interpolated; the whitelist check below keeps that
    safe by construction rather than by convention.
    """
    if column not in _TRACKER_BREAKDOWN_COLUMNS:
        raise ValueError(f"unsupported breakdown column: {column!r}")
    rows = conn.execute(
        f"""SELECT {column} AS bucket,
                   COUNT(*) AS cnt,
                   SUM(CASE WHEN {_REJECTED_SQL} THEN 1 ELSE 0 END) AS rejected_cnt,
                   SUM(CASE WHEN {_RESPONDED_SQL} THEN 1 ELSE 0 END) AS responded_cnt
            FROM job_applications
            WHERE user_id = ? AND {column} IS NOT NULL
            GROUP BY {column}""",
        (user_id,),
    ).fetchall()
    return {
        r["bucket"]: {
            "count": r["cnt"],
            "rejected": r["rejected_cnt"],
            "response_rate": round(r["responded_cnt"] / r["cnt"], 4) if r["cnt"] else 0.0,
        }
        for r in rows
    }


@app.get("/insights/pipeline")
def insights_pipeline(auth=Depends(verify_api_key)):
    """Deterministic SQL pipeline-health aggregates for the caller's tracker rows.

    Pure aggregation, no LLM. Empty data returns zeros/nulls, never a crash.
    """
    user_id = _get_user_id(auth)
    if user_id is None:
        raise HTTPException(status_code=401, detail="Authentication required.")
    if not CLOUD_AVAILABLE:
        raise HTTPException(status_code=503, detail="Insights unavailable.")

    from cloud.auth import get_db
    with get_db() as conn:
        total = conn.execute(
            "SELECT COUNT(*) AS c FROM job_applications WHERE user_id = ?",
            (user_id,),
        ).fetchone()["c"]
        rejected = conn.execute(
            f"SELECT COUNT(*) AS c FROM job_applications WHERE user_id = ? AND {_REJECTED_SQL}",
            (user_id,),
        ).fetchone()["c"]
        active = conn.execute(
            f"SELECT COUNT(*) AS c FROM job_applications WHERE user_id = ? AND NOT {_CLOSED_SQL}",
            (user_id,),
        ).fetchone()["c"]

        by_tier = _tracker_pipeline_breakdown(conn, user_id, "target_tier")
        by_fit = _tracker_pipeline_breakdown(conn, user_id, "fit_label")
        by_source = _tracker_pipeline_breakdown(conn, user_id, "referral_source")

        reason_rows = conn.execute(
            """SELECT rejection_reason, COUNT(*) AS c
               FROM job_applications
               WHERE user_id = ? AND rejection_reason IS NOT NULL
               GROUP BY rejection_reason""",
            (user_id,),
        ).fetchall()
        by_rejection_reason = {r["rejection_reason"]: r["c"] for r in reason_rows}

        day_rows = conn.execute(
            """SELECT (julianday(responded_at) - julianday(applied_at)) AS days
               FROM job_applications
               WHERE user_id = ? AND applied_at IS NOT NULL AND responded_at IS NOT NULL""",
            (user_id,),
        ).fetchall()

    day_values = [r["days"] for r in day_rows if r["days"] is not None]
    median_days = statistics.median(day_values) if day_values else None

    return {
        "totals": {"applications": total, "rejected": rejected, "active": active},
        "by_tier": by_tier,
        "by_fit": by_fit,
        "by_source": by_source,
        "median_days_to_response": median_days,
        "by_rejection_reason": by_rejection_reason,
    }


@app.get("/insights/application/{entry_id}")
def insights_application(entry_id: int, auth=Depends(verify_api_key)):
    """Per-application insight placeholder.

    LLM post-mortems land in Phase 2; for now this just confirms ownership
    (404 for other users' rows) and returns a clean null.
    """
    user_id = _get_user_id(auth)
    if user_id is None:
        raise HTTPException(status_code=401, detail="Authentication required.")
    if not CLOUD_AVAILABLE:
        raise HTTPException(status_code=503, detail="Insights unavailable.")

    from cloud.auth import get_db
    with get_db() as conn:
        owned = conn.execute(
            "SELECT id FROM job_applications WHERE id = ? AND user_id = ?",
            (entry_id, user_id),
        ).fetchone()
    if owned is None:
        raise HTTPException(status_code=404, detail="Tracker entry not found.")

    return {"postmortem": None}


def _get_user_id(auth) -> Optional[int]:
    """Extract the integer user_id of a REAL, authenticated account.

    Returns None for anonymous callers even though verify_api_key mints them
    a usable user_id. That anonymous identity is keyed on the caller-supplied
    X-Client-Fingerprint header (falling back to client IP), so it is a
    shared, spoofable bucket -- fine for metering free scores, never an
    account. Every caller of this helper owns per-account data (tracker rows,
    pipeline insights, agent runs); treating the anonymous bucket as an
    account would let two callers who send the same fingerprint -- or who sit
    behind one NAT address -- read and write each other's applications.
    """
    if not isinstance(auth, dict):
        return None
    if auth.get("anonymous"):
        return None
    uid = auth.get("user_id")
    try:
        return int(uid) if uid is not None else None
    except (ValueError, TypeError):
        return None


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
    print("  Resume Scorer API v3.0")
    print(f"{'='*60}")
    print(f"  Server:  http://{args.host}:{args.port}")
    print(f"  Auth:    {'Required (JWT + API Key)' if _config['require_auth'] else 'Disabled'}")
    print(f"  Cloud:   {'Enabled' if CLOUD_AVAILABLE else 'Disabled (local mode)'}")
    print(f"  Billing: {'Configured' if CLOUD_AVAILABLE and is_billing_configured() else 'Not configured'}")
    print(f"  CORS:    {args.cors_origins}")
    print(f"  Rate:    {args.rate_limit}/min per key")
    print("\n  Scoring Endpoints:")
    print("  GET  /health         — Server status and model info")
    print("  POST /score/ats      — ATS score")
    print("  POST /score/hr       — HR score")
    print("  POST /score/both     — Combined ATS + HR score")
    print("  POST /score/llm      — LLM-augmented score (Claude)")
    print("  POST /score/combined — Blended ATS + HR + LLM score")
    print("  POST /score/batch    — Batch scoring")
    print("  POST /explain        — Score explanation")
    print("  POST /cover-letter   — Cover letter generation (Pro/Ultra)")
    print("  POST /jobs/discover  — Job discovery + scoring")
    print("\n  Async Agent Endpoints:")
    print("  POST /agent/tailor        — Enqueue a tailored-resume run (Pro/Ultra)")
    print("  POST /agent/cover-letter  — Enqueue a cover-letter run (Pro/Ultra)")
    print("  GET  /agent/runs/{run_id} — Poll an async agent run")
    print("\n  Auth & Billing Endpoints:")
    print("  POST /auth/register  — Create account")
    print("  POST /auth/login     — Login (returns JWT)")
    print("  POST /auth/api-key   — Generate API key")
    print("  GET  /auth/usage     — Usage stats")
    print("  POST /billing/checkout — Upgrade to Pro (Stripe)")
    print("  POST /billing/webhook  — Stripe webhooks")
    print("  POST /billing/portal   — Manage subscription")
    print("\n  Admin Endpoints:")
    print("  POST /admin/create-key — Legacy key generation")
    print("  GET  /admin/stats      — Server statistics")
    print(f"{'='*60}\n")

    # Weekly digest scheduler: Mondays ~13:00 UTC (9am ET). The digest_log
    # table makes runs idempotent per user per ISO week, so restarts and the
    # half-hour polling loop can never double-send. Gated off by default until
    # the match-inbox rework lands — enable with DIGEST_SCHEDULER=1.
    if CLOUD_AVAILABLE and os.getenv("DIGEST_SCHEDULER") == "1":
        import threading

        def _digest_scheduler():
            import logging as _logging
            from datetime import datetime, timezone

            from cloud.digest import send_weekly_digests

            while True:
                now = datetime.now(timezone.utc)
                if now.weekday() == 0 and 13 <= now.hour < 15:
                    try:
                        send_weekly_digests()
                    except Exception:
                        _logging.getLogger("scorer.digest").exception("scheduled digest run failed")
                time.sleep(1800)

        threading.Thread(target=_digest_scheduler, daemon=True, name="digest-scheduler").start()

    uvicorn.run(app, host=args.host, port=args.port, log_level="info")
