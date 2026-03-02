"""
AI Resume Tuner — MCP Server

Score, analyze, and optimize resumes against job descriptions using
dual ATS + HR scoring with optional LLM-augmented analysis.

Tools:
    score_resume      — Full ATS + HR analysis in one call
    score_ats         — ATS keyword/semantic scoring only
    score_hr          — HR recruiter simulation only
    score_with_llm    — LLM-augmented scoring (requires ANTHROPIC_API_KEY)
    rewrite_resume    — AI-powered resume tailoring (requires ANTHROPIC_API_KEY)
    explain_score     — Actionable improvement suggestions
    generate_cover_letter — AI cover letter from resume + JD
    discover_jobs     — Search jobs and score against your resume
    save_resume       — Upload resume to cloud storage (one-time setup)
    get_saved_resume  — Retrieve previously saved resume
    extract_text      — Read PDF/DOCX/MD/TXT files

Cloud-first: tries https://resume-scorer.fly.dev, falls back to local scoring.

Usage:
    fastmcp run mcp_scorer.py
"""

import os
import sys
from pathlib import Path

# Load .env from project root
_env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
if os.path.isfile(_env_path):
    with open(_env_path, "r", encoding="utf-8") as _ef:
        for _line in _ef:
            _line = _line.strip()
            if _line and not _line.startswith("#") and "=" in _line:
                _k, _, _v = _line.partition("=")
                os.environ[_k.strip()] = _v.strip()

# Ensure project root is on sys.path
PROJECT_ROOT = str(Path(__file__).parent)
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from fastmcp import FastMCP

mcp = FastMCP(
    "AI Resume Tuner",
    instructions=(
        "Resume optimization toolkit. Score resumes against job descriptions "
        "using ATS keyword matching, HR recruiter simulation, and optional "
        "LLM-augmented analysis. Supports PDF, DOCX, Markdown, and plain text."
    ),
)

# ─── Cloud client (optional — falls back to local) ────────────────────────

try:
    from cloud.client import cloud_score, cloud_health, cloud_get_resume, cloud_save_resume
    CLOUD_AVAILABLE = True
except ImportError:
    CLOUD_AVAILABLE = False


UPGRADE_URL = "https://resume-scorer-web.streamlit.app"


def _try_cloud(endpoint: str, resume_text: str, jd_text: str, extra: dict = None):
    """Try cloud API first. Returns result dict or None."""
    if not CLOUD_AVAILABLE:
        return None
    try:
        return cloud_score(endpoint, resume_text, jd_text, extra)
    except Exception:
        return None


def _is_usage_limit(result: dict) -> bool:
    """Check if a cloud result is a usage limit error."""
    return isinstance(result, dict) and result.get("_error") == "usage_limit"


# ─── Lazy-load local scorers (SBERT takes ~5s on first call) ──────────────

_scorers_loaded = False


def _ensure_scorers():
    global _scorers_loaded
    if not _scorers_loaded:
        global ats_scorer, hr_scorer
        import ats_scorer as _ats
        import hr_scorer as _hr
        ats_scorer = _ats
        hr_scorer = _hr
        _scorers_loaded = True


def _local_available() -> bool:
    """Check if local scoring deps are installed."""
    try:
        import ats_scorer  # noqa: F401
        import hr_scorer   # noqa: F401
        return True
    except ImportError:
        return False


def _usage_limit_message(result: dict) -> str:
    """Build user-facing message when free tier is exhausted."""
    if _local_available():
        return (
            "☁️  Free cloud scoring limit reached (5 scores).\n\n"
            "✅  Local scoring is available on this machine — switching to local mode now.\n\n"
            "To unlock unlimited cloud scoring, upgrade to Pro ($12/mo):\n"
            "  • Claude Code / Claude.ai users: Pro is all you need.\n"
            "    Scoring is done by the server; resume writing is done by your\n"
            "    existing Anthropic subscription — no need for Ultra.\n"
            "  • Sign up at " + UPGRADE_URL + ", then run /setup to link your account."
        )
    return (
        "☁️  Free cloud scoring limit reached (5 scores).\n\n"
        "You have two options:\n\n"
        "Option A — Pro plan ($12/mo, unlimited scoring):\n"
        "  • Perfect for Claude Code / Claude.ai users — scoring only.\n"
        "    Your Anthropic subscription handles resume writing; Pro handles scoring.\n"
        "  1. Sign up at " + UPGRADE_URL + "\n"
        "  2. Dashboard → 'Claude Code Plugin Setup' → Generate API Key\n"
        "  3. Run /setup — Claude will save the key automatically\n\n"
        "Option B — Free local scoring (no subscription needed):\n"
        "  Run /setup — Claude will install the required packages\n"
        "  (requires Python + ~500MB for the ML models, works offline)"
    )


# ─── Tools ────────────────────────────────────────────────────────────────


@mcp.tool()
def score_resume(resume_text: str, jd_text: str) -> dict:
    """Score a resume against a job description using both ATS and HR analysis.

    This is the recommended tool for full resume evaluation. It runs:
    1. ATS scoring — keyword matching, semantic similarity, phrase matching,
       industry term recognition, and format risk assessment.
    2. HR scoring — recruiter simulation evaluating experience fit, skills match,
       career trajectory, impact signals, and competitive edge.

    Args:
        resume_text: Full text of the resume.
        jd_text: Full text of the job description.

    Returns:
        Combined results with ats_score (0-100), hr_score (0-100),
        matched/missing keywords, HR recommendation, and detailed breakdowns.
    """
    cloud_result = _try_cloud("/score/both", resume_text, jd_text)
    if _is_usage_limit(cloud_result):
        if not _local_available():
            return {"error": "free_tier_limit_reached", "message": _usage_limit_message(cloud_result)}
        # Fall through to local scoring silently
    elif cloud_result and "ats" in cloud_result:
        return cloud_result

    _ensure_scorers()

    ats_result = ats_scorer.calculate_ats_score(resume_text, jd_text)
    rating, likelihood, _color = ats_scorer.get_likelihood_rating(ats_result["total_score"])
    ats_result["rating"] = rating
    ats_result["likelihood"] = likelihood

    try:
        hr_result = hr_scorer.calculate_hr_score_from_text(resume_text, jd_text)
        hr_dict = hr_scorer.result_to_dict(hr_result)
    except Exception as e:
        hr_dict = {"overall_score": 0, "error": str(e)}

    ats_score = round(ats_result.get("total_score", 0), 1)
    hr_score = round(hr_dict.get("overall_score", 0), 1)

    return {
        "ats": ats_result,
        "hr": hr_dict,
        "summary": {
            "ats_score": ats_score,
            "hr_score": hr_score,
            "ats_rating": ats_result.get("rating", "Unknown"),
            "hr_recommendation": hr_dict.get("recommendation", "Unknown"),
            "assessment": _overall_assessment(ats_score, hr_score),
        },
    }


@mcp.tool()
def score_ats(resume_text: str, jd_text: str) -> dict:
    """Score a resume using ATS (Applicant Tracking System) analysis only.

    Evaluates how well a resume matches a job description through eight
    weighted components: keyword match (20%), phrase match (25%),
    industry terms (15%), semantic similarity (10%), BM25 relevance (10%),
    job title match (10%), graph centrality (5%), skill recency (5%).

    Args:
        resume_text: Full text of the resume.
        jd_text: Full text of the job description.

    Returns:
        Score (0-100) with matched/missing keywords, domain detection,
        readability analysis, format risk flags, and component breakdowns.
    """
    cloud_result = _try_cloud("/score/ats", resume_text, jd_text)
    if _is_usage_limit(cloud_result):
        if not _local_available():
            return {"error": "free_tier_limit_reached", "message": _usage_limit_message(cloud_result)}
    elif cloud_result and "total_score" in cloud_result:
        return cloud_result

    _ensure_scorers()
    result = ats_scorer.calculate_ats_score(resume_text, jd_text)
    rating, likelihood, _color = ats_scorer.get_likelihood_rating(result["total_score"])
    result["rating"] = rating
    result["likelihood"] = likelihood
    return result


@mcp.tool()
def score_hr(resume_text: str, jd_text: str) -> dict:
    """Score a resume using HR recruiter simulation.

    Evaluates the resume as a human hiring manager would, analyzing:
    experience fit (30%), skills match (20%), career trajectory (20%),
    impact signals (20%), and competitive edge (10%). Includes F-pattern
    visual scoring, job-hopping detection, and interview question generation.

    Args:
        resume_text: Full text of the resume.
        jd_text: Full text of the job description.

    Returns:
        Score (0-100) with recommendation (INTERVIEW/MAYBE/PASS),
        factor breakdown, strengths, concerns, and interview questions.
    """
    cloud_result = _try_cloud("/score/hr", resume_text, jd_text)
    if _is_usage_limit(cloud_result):
        if not _local_available():
            return {"error": "free_tier_limit_reached", "message": _usage_limit_message(cloud_result)}
    elif cloud_result and "overall_score" in cloud_result:
        return cloud_result

    _ensure_scorers()
    hr_result = hr_scorer.calculate_hr_score_from_text(resume_text, jd_text)
    result = hr_scorer.result_to_dict(hr_result)
    return result


@mcp.tool()
def score_with_llm(resume_text: str, jd_text: str, domain_hint: str = "") -> dict:
    """Score a resume using Claude LLM-augmented analysis.

    Requires ANTHROPIC_API_KEY in environment. Uses Claude to evaluate
    the resume against a structured rubric covering keyword alignment,
    semantic relevance, industry terminology, job fit, experience quality,
    impact signals, career trajectory, and competitive positioning.

    Args:
        resume_text: Full text of the resume.
        jd_text: Full text of the job description.
        domain_hint: Optional domain (technology, finance, consulting,
            clinical_research, healthcare, pharma_biotech).

    Returns:
        ATS and HR scores with per-dimension breakdowns, evidence quotes,
        and a human-readable explanation.
    """
    try:
        from llm_scorer import score_with_llm as _score_llm, ANTHROPIC_AVAILABLE
        if not ANTHROPIC_AVAILABLE:
            return {"error": "anthropic package not installed. Run: pip install anthropic"}
        if not os.environ.get("ANTHROPIC_API_KEY"):
            return {"error": "ANTHROPIC_API_KEY not set. Add it to your .env file."}
        return _score_llm(resume_text, jd_text, domain_hint=domain_hint or None)
    except Exception as e:
        return {"error": str(e)}


@mcp.tool()
def rewrite_resume(resume_text: str, jd_text: str, domain_hint: str = "") -> dict:
    """Rewrite a resume to better match a job description using Claude AI.

    Requires ANTHROPIC_API_KEY in environment. Tailors the resume while
    preserving authenticity — never changes job titles, company names,
    dates, education, publications, or certifications. Only modifies:
    professional summary, core competencies, and bullet point phrasing.

    Args:
        resume_text: Full text of the resume to optimize.
        jd_text: Full text of the target job description.
        domain_hint: Optional domain (technology, finance, consulting,
            clinical_research, healthcare, pharma_biotech).

    Returns:
        rewritten_resume (full text), changes_made (list of modifications),
        and explanation (summary of optimization strategy).
    """
    try:
        from llm_scorer import rewrite_resume as _rewrite, ANTHROPIC_AVAILABLE
        if not ANTHROPIC_AVAILABLE:
            return {
                "error": "anthropic package not installed. Run: pip install anthropic",
                "rewritten_resume": None,
                "changes_made": [],
            }
        if not os.environ.get("ANTHROPIC_API_KEY"):
            return {
                "error": "ANTHROPIC_API_KEY not set. Add it to your .env file.",
                "rewritten_resume": None,
                "changes_made": [],
            }
        return _rewrite(resume_text, jd_text, domain_hint=domain_hint or None)
    except Exception as e:
        return {"error": str(e), "rewritten_resume": None, "changes_made": []}


@mcp.tool()
def explain_score(resume_text: str, jd_text: str) -> dict:
    """Get actionable improvement suggestions for a resume.

    Analyzes the resume against the job description and returns prioritized
    suggestions: top missing keywords with placement recommendations,
    quick wins, section-by-section improvement tips, and format warnings.

    Args:
        resume_text: Full text of the resume.
        jd_text: Full text of the job description.

    Returns:
        Current ATS score plus detailed explanation with missing keywords,
        improvement priorities, and specific suggestions.
    """
    cloud_result = _try_cloud("/explain", resume_text, jd_text)
    if _is_usage_limit(cloud_result):
        return _usage_limit_response(cloud_result)
    if cloud_result and "explanation" in cloud_result:
        return cloud_result

    _ensure_scorers()

    # Import the explanation generator from scorer_server
    try:
        from scorer_server import generate_ats_explanation
    except ImportError:
        # Fallback: score and return basic info
        ats_result = ats_scorer.calculate_ats_score(resume_text, jd_text)
        missing = ats_result.get("missing_keywords", [])
        return {
            "current_score": round(ats_result.get("total_score", 0), 1),
            "explanation": {
                "top_missing_keywords": missing[:10],
                "suggestion": "Add these missing keywords to your Core Competencies or bullet points.",
            },
        }

    ats_result = ats_scorer.calculate_ats_score(resume_text, jd_text)
    explanation = generate_ats_explanation(resume_text, jd_text, ats_result)

    return {
        "current_score": round(ats_result.get("total_score", 0), 1),
        "explanation": explanation,
    }


@mcp.tool()
def generate_cover_letter(
    resume_text: str,
    jd_text: str,
    company_name: str = "",
    job_title: str = "",
) -> dict:
    """Generate a tailored cover letter from a resume and job description.

    Uses Claude AI to write a compelling, ready-to-send cover letter that
    highlights the candidate's most relevant experience for the role.

    Args:
        resume_text: Full text of the candidate's resume.
        jd_text: Full text of the job description.
        company_name: Company name (auto-detected from JD if empty).
        job_title: Job title (auto-detected from JD if empty).

    Returns:
        Cover letter paragraphs, full text, detected company/title, and word count.
    """
    cloud_result = _try_cloud(
        "/cover-letter", resume_text, jd_text,
        extra={"company_name": company_name, "job_title": job_title},
    )
    if _is_usage_limit(cloud_result):
        return _usage_limit_response(cloud_result)
    if cloud_result and "paragraphs" in cloud_result:
        return cloud_result

    # Fallback to local
    from llm_scorer import generate_cover_letter as _gen_cl, ANTHROPIC_AVAILABLE
    if not ANTHROPIC_AVAILABLE:
        return {
            "error": "no_api_key",
            "message": (
                "Cover letter generation requires the Anthropic API. "
                "Set ANTHROPIC_API_KEY in your .env file, or use the cloud "
                f"version at {UPGRADE_URL} (Pro tier required)."
            ),
        }
    return _gen_cl(resume_text, jd_text, company_name=company_name, job_title=job_title)


@mcp.tool()
def discover_jobs(
    job_title: str,
    resume_text: str = "",
    location: str = "",
    remote_only: bool = False,
    max_results: int = 10,
) -> dict:
    """Search for jobs and score them against your resume.

    Searches Adzuna (and Remotive for remote jobs), then scores each result:
    1. Lightweight pre-screening of top 20 candidates
    2. Full ATS + HR scoring of top finalists

    Args:
        job_title: Target job title to search for (e.g., "Data Scientist").
        resume_text: Full text of your resume. Leave blank to use your saved
            resume (uploaded via save_resume or the web app).
        location: Geographic location filter (e.g., "New York", "Remote").
        remote_only: If True, also searches Remotive for remote-only jobs.
        max_results: Number of top-scored jobs to return (1-20, default 10).

    Returns:
        Ranked list of jobs with ATS scores, HR scores, salary data, and apply URLs.
    """
    cloud_result = _try_cloud(
        "/jobs/discover", resume_text, "",
        extra={
            "job_title": job_title,
            "location": location,
            "remote_only": remote_only,
            "max_results": max_results,
        },
    )
    if _is_usage_limit(cloud_result):
        return _usage_limit_response(cloud_result)
    if cloud_result and "jobs" in cloud_result:
        return cloud_result

    # Fallback to local
    _ensure_scorers()
    import job_discovery as _jd

    # If no Adzuna keys locally and cloud failed, give a clear message
    if not _jd.adzuna_configured() and not CLOUD_AVAILABLE:
        return {
            "error": "no_api_keys",
            "message": (
                "Job discovery requires Adzuna API keys. You have two options:\n\n"
                "1. Cloud (recommended): Use the hosted scorer at "
                f"{UPGRADE_URL} — no setup needed, Adzuna search is built in.\n\n"
                "2. Local: Get free API keys at https://developer.adzuna.com/ "
                "and add to your .env file:\n"
                "   ADZUNA_APP_ID=your_app_id\n"
                "   ADZUNA_APP_KEY=your_app_key\n\n"
                "Note: Without Adzuna, only Remotive (remote jobs) is available."
            ),
        }

    return _jd.discover_jobs(
        resume_text=resume_text,
        job_title=job_title,
        location=location,
        remote_only=remote_only,
        max_results=max_results,
    )


@mcp.tool()
def save_resume(resume_text: str, filename: str = "resume.txt") -> dict:
    """Upload your resume to cloud storage so you don't need to re-enter it.

    After saving once, all scoring tools (score_resume, score_ats, score_hr,
    discover_jobs, generate_cover_letter, etc.) will automatically use this
    resume when no resume_text is provided.

    Args:
        resume_text: Full text of your resume.
        filename: Original filename for reference (default: resume.txt).

    Returns:
        Confirmation dict with saved=True, char_count, and content_hash.
    """
    if not CLOUD_AVAILABLE:
        return {"error": "Cloud not configured. Set SCORER_CLOUD_URL and SCORER_CLOUD_API_KEY in your .env file."}
    result = cloud_save_resume(resume_text, filename)
    if result is None:
        return {"error": "Failed to save resume. Check your API key and connection."}
    return {"saved": True, **result}


@mcp.tool()
def get_saved_resume() -> dict:
    """Retrieve your previously saved resume from cloud storage.

    Returns the resume text and metadata if a resume has been saved via
    save_resume or uploaded through the web app.

    Returns:
        Dict with resume_text, filename, char_count, uploaded_at if found,
        or {"resume_on_file": False} if no resume is saved.
    """
    if not CLOUD_AVAILABLE:
        return {"error": "Cloud not configured. Set SCORER_CLOUD_URL and SCORER_CLOUD_API_KEY in your .env file."}
    result = cloud_get_resume()
    if result is None:
        return {"error": "Could not connect to cloud. Check SCORER_CLOUD_URL and SCORER_CLOUD_API_KEY."}
    return result


@mcp.tool()
def extract_text(file_path: str) -> dict:
    """Extract text from a resume file (PDF, DOCX, Markdown, or TXT).

    Use this to read resume files that Claude can't open directly,
    such as .docx and .pdf files.

    Args:
        file_path: Path to the file (.pdf, .docx, .md, .txt).

    Returns:
        Extracted text content, detected format, and character count.
    """
    p = Path(file_path)
    if not p.exists():
        return {"error": f"File not found: {file_path}"}

    ext = p.suffix.lower()
    text = ""

    try:
        if ext == ".pdf":
            import pdfplumber
            with pdfplumber.open(str(p)) as pdf:
                for page in pdf.pages:
                    page_text = page.extract_text()
                    if page_text:
                        text += page_text + "\n"
        elif ext == ".docx":
            from docx import Document
            doc = Document(str(p))
            text = "\n".join(para.text for para in doc.paragraphs)
        elif ext in (".md", ".txt"):
            with open(str(p), "r", encoding="utf-8") as f:
                text = f.read()
        else:
            return {"error": f"Unsupported format: {ext}. Use .pdf, .docx, .md, or .txt"}
    except ImportError as e:
        pkg = "pdfplumber" if ext == ".pdf" else "python-docx"
        return {"error": f"Missing dependency for {ext} files. Run: pip install {pkg}"}
    except Exception as e:
        return {"error": f"Failed to read {p.name}: {e}"}

    if not text.strip():
        return {"error": f"No text extracted from {p.name}. The file may be empty or image-based."}

    return {
        "text": text.strip(),
        "format": ext,
        "char_count": len(text.strip()),
    }


# ─── Helpers ──────────────────────────────────────────────────────────────


def _overall_assessment(ats_score: float, hr_score: float) -> str:
    if ats_score >= 75 and hr_score >= 70:
        return "STRONG: Passes both ATS and HR evaluation. Ready to submit."
    elif ats_score >= 75 and hr_score < 70:
        return "ATS READY, HR WEAK: Will pass ATS filters but may not impress recruiters. Strengthen impact signals."
    elif ats_score < 75 and hr_score >= 70:
        return "HR STRONG, ATS WEAK: Reads well to humans but may be filtered by ATS. Add more JD keywords."
    elif ats_score >= 60 and hr_score >= 55:
        return "COMPETITIVE: Decent match with room for improvement on both ATS keywords and HR appeal."
    else:
        return "NEEDS WORK: Significant gaps in keyword matching and recruiter appeal. Major revision recommended."


if __name__ == "__main__":
    mcp.run()
