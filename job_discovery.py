"""
Job Discovery Module — Search jobs and score them against your resume.

APIs:
  - Adzuna (primary): REST API, requires ADZUNA_APP_ID + ADZUNA_APP_KEY env vars
  - Remotive (secondary): Free, no auth, remote jobs only

Two-tier scoring:
  1. Lightweight score (keyword + phrase + BM25 + title match) for top 20 candidates
  2. Full ATS + HR score for top N finalists
"""

import hashlib
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from html.parser import HTMLParser
from typing import Any, Dict, List, Optional, Tuple


# ---------------------------------------------------------------------------
# HTML Stripping
# ---------------------------------------------------------------------------

class _HTMLStripper(HTMLParser):
    """Strip HTML tags and decode entities to plain text."""

    def __init__(self):
        super().__init__()
        self._parts: List[str] = []
        self._skip = False

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self._skip = True

    def handle_endtag(self, tag):
        if tag in ("script", "style"):
            self._skip = False

    def handle_data(self, data):
        if not self._skip:
            self._parts.append(data)

    def handle_entityref(self, name):
        from html import unescape
        self._parts.append(unescape(f"&{name};"))

    def handle_charref(self, name):
        from html import unescape
        self._parts.append(unescape(f"&#{name};"))

    def get_text(self) -> str:
        return " ".join(self._parts)


def strip_html(html: str) -> str:
    """Remove HTML tags and decode entities, returning plain text."""
    if not html:
        return ""
    stripper = _HTMLStripper()
    stripper.feed(html)
    text = stripper.get_text()
    # Collapse whitespace
    text = re.sub(r"\s+", " ", text).strip()
    return text


# ---------------------------------------------------------------------------
# API Configuration
# ---------------------------------------------------------------------------

def _adzuna_configured() -> bool:
    return bool(os.getenv("ADZUNA_APP_ID")) and bool(os.getenv("ADZUNA_APP_KEY"))


def adzuna_configured() -> bool:
    """Public check for whether Adzuna API keys are set."""
    return _adzuna_configured()


# ---------------------------------------------------------------------------
# Adzuna API
# ---------------------------------------------------------------------------

ADZUNA_BASE = "https://api.adzuna.com/v1/api/jobs"


def search_adzuna(
    query: str,
    location: str = "",
    country: str = "",
    results_per_page: int = 50,
) -> List[Dict[str, Any]]:
    """Search Adzuna for jobs. Returns normalized job dicts."""
    app_id = os.getenv("ADZUNA_APP_ID", "")
    app_key = os.getenv("ADZUNA_APP_KEY", "")
    if not app_id or not app_key:
        return []

    if not country:
        country = os.getenv("ADZUNA_COUNTRY", "us")

    params = {
        "app_id": app_id,
        "app_key": app_key,
        "results_per_page": str(min(results_per_page, 50)),
        "what": query,
        "content-type": "application/json",
    }
    if location:
        params["where"] = location

    url = f"{ADZUNA_BASE}/{country}/search/1?{urllib.parse.urlencode(params)}"

    try:
        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception:
        return []

    results = data.get("results", [])
    return [_normalize_adzuna_result(r) for r in results]


def _normalize_adzuna_result(raw: dict) -> Dict[str, Any]:
    """Normalize an Adzuna API result to common schema."""
    location_parts = []
    loc = raw.get("location", {})
    if loc.get("display_name"):
        location_parts.append(loc["display_name"])

    salary_min = raw.get("salary_min")
    salary_max = raw.get("salary_max")

    # Parse date
    posted = raw.get("created", "")
    if posted:
        try:
            dt = datetime.fromisoformat(posted.replace("Z", "+00:00"))
            posted = dt.strftime("%Y-%m-%d")
        except (ValueError, TypeError):
            posted = posted[:10] if len(posted) >= 10 else posted

    return {
        "source": "adzuna",
        "id": str(raw.get("id", "")),
        "title": raw.get("title", "").strip(),
        "company": (raw.get("company", {}) or {}).get("display_name", "Unknown"),
        "location": ", ".join(location_parts) if location_parts else "Not specified",
        "description": strip_html(raw.get("description", "")),
        "salary_min": salary_min,
        "salary_max": salary_max,
        "url": raw.get("redirect_url", ""),
        "category": (raw.get("category", {}) or {}).get("label", ""),
        "posted_date": posted,
    }


# ---------------------------------------------------------------------------
# Remotive API
# ---------------------------------------------------------------------------

REMOTIVE_URL = "https://remotive.com/api/remote-jobs"


def search_remotive(query: str) -> List[Dict[str, Any]]:
    """Search Remotive for remote jobs. Returns normalized job dicts."""
    params = {"search": query, "limit": "50"}
    url = f"{REMOTIVE_URL}?{urllib.parse.urlencode(params)}"

    try:
        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception:
        return []

    jobs = data.get("jobs", [])
    return [_normalize_remotive_result(r) for r in jobs]


def _normalize_remotive_result(raw: dict) -> Dict[str, Any]:
    """Normalize a Remotive API result to common schema."""
    # Salary parsing (Remotive gives a text string or nothing)
    salary_min = None
    salary_max = None
    salary_text = raw.get("salary", "") or ""
    if salary_text:
        numbers = re.findall(r"[\d,]+", salary_text.replace(",", ""))
        if len(numbers) >= 2:
            try:
                salary_min = int(numbers[0])
                salary_max = int(numbers[1])
            except ValueError:
                pass
        elif len(numbers) == 1:
            try:
                salary_min = int(numbers[0])
            except ValueError:
                pass

    posted = raw.get("publication_date", "")
    if posted:
        posted = posted[:10]

    candidate_location = raw.get("candidate_required_location", "Remote")

    return {
        "source": "remotive",
        "id": str(raw.get("id", "")),
        "title": raw.get("title", "").strip(),
        "company": raw.get("company_name", "Unknown"),
        "location": candidate_location if candidate_location else "Remote",
        "description": strip_html(raw.get("description", "")),
        "salary_min": salary_min,
        "salary_max": salary_max,
        "url": raw.get("url", ""),
        "category": raw.get("category", ""),
        "posted_date": posted,
    }


# ---------------------------------------------------------------------------
# Title Similarity (fast pre-filter)
# ---------------------------------------------------------------------------

def _tokenize(text: str) -> set:
    """Lowercase tokenize, removing common stop words."""
    stops = {"a", "an", "the", "and", "or", "of", "in", "at", "to", "for", "with", "on", "is"}
    tokens = set(re.findall(r"[a-z0-9]+", text.lower()))
    return tokens - stops


def _title_similarity(job_title: str, query_title: str) -> float:
    """Token overlap score (0-1) between a job title and the search query."""
    job_tokens = _tokenize(job_title)
    query_tokens = _tokenize(query_title)
    if not query_tokens:
        return 0.0
    overlap = job_tokens & query_tokens
    # Jaccard-like: weight by coverage of query tokens
    return len(overlap) / len(query_tokens)


# ---------------------------------------------------------------------------
# Lightweight Scoring (fast, no SBERT)
# ---------------------------------------------------------------------------

def lightweight_score(resume_text: str, jd_text: str) -> float:
    """
    Fast scoring using keyword + phrase + BM25 + title match only.
    Skips SBERT semantic similarity for speed.

    Returns a score 0-100.
    """
    import ats_scorer

    # Keyword match (30.8% weight, renormalized from 20% without SBERT)
    kw_pct, _, _ = ats_scorer.calculate_keyword_match(resume_text, jd_text)
    kw_score = kw_pct  # 0-100

    # Phrase match (38.5% weight, renormalized from 25%)
    phrase_pct, _, _ = ats_scorer.calculate_phrase_match(resume_text, jd_text)
    phrase_score = phrase_pct  # 0-100

    # BM25 (15.4% weight, renormalized from 10%)
    bm25_raw, _ = ats_scorer.calculate_bm25_score(resume_text, jd_text)
    bm25_score = min(bm25_raw * 100, 100)  # Normalize to 0-100

    # Job title match (15.4% weight, renormalized from 10%)
    title_score, _ = ats_scorer.check_job_title_match(resume_text, jd_text)

    total = (
        kw_score * 0.308
        + phrase_score * 0.385
        + bm25_score * 0.154
        + title_score * 0.154
    )
    return round(min(max(total, 0), 100), 1)


# ---------------------------------------------------------------------------
# AI Resume Analysis (LLM-enhanced search query generation)
# ---------------------------------------------------------------------------

def analyze_resume_for_search(resume_text: str, include_queries: bool = True) -> Dict[str, Any]:
    """
    Use Claude Haiku to build a structured candidate profile from the resume.

    Always returns: recent_title, career_level, domain, role_type,
                    role_family, excluded_roles, specialties, job_zone.
    When include_queries=True: also returns search_queries (4-5 targeted queries).

    Role family / excluded_roles enable precise job-title-level filtering so a
    physician never sees nurse or coordinator results, a senior engineer never
    sees junior/intern roles, etc.

    Falls back to empty dict if no API key or the call fails.
    """
    api_key = os.getenv("ANTHROPIC_API_KEY", "")
    if not api_key:
        return {}

    model = os.getenv("ANTHROPIC_MODEL", "claude-haiku-4-5-20251001")
    resume_excerpt = resume_text[:3000]

    queries_instruction = (
        "- search_queries: list of 4-5 short (2-4 word) job search queries for this "
        "person's next logical career step. Include the obvious title AND 2-3 adjacent "
        "roles they could realistically land.\n"
    ) if include_queries else ""

    prompt = (
        "Analyze this resume and return a JSON object with EXACTLY these keys:\n"
        "- recent_title: most recent job title (string)\n"
        "- career_level: one of entry, mid, senior, director, executive (string)\n"
        "- domain: primary industry/domain, e.g. 'clinical research', 'software engineering' (string)\n"
        "- role_type: professional category, e.g. 'physician', 'nurse', 'data scientist', "
        "'software engineer', 'clinical researcher', 'financial analyst' (string)\n"
        "- role_family: list of 3-6 job title keywords this person SHOULD match. "
        "Be specific to their role type and seniority level. "
        "Example for a physician: [\"physician\", \"medical officer\", \"attending\", "
        "\"medical director\", \"doctor\", \"hospitalist\"].\n"
        "- excluded_roles: list of 3-6 job title keywords for roles this person is clearly "
        "OVERQUALIFIED for or in a DIFFERENT role family. These will be used to filter out "
        "irrelevant jobs. Example for a physician: [\"nurse\", \"nursing\", \"coordinator\", "
        "\"technician\", \"assistant\", \"aide\"]. "
        "Example for a senior software engineer: [\"junior\", \"intern\", \"qa tester\", \"support\"].\n"
        "- specialties: list of 2-4 domain specialties (e.g. [\"oncology\", \"cardiology\"] for a cardiologist)\n"
        "- job_zone: O*NET job zone 1-5 (1=no degree, 3=associate/bachelor, 5=advanced degree). "
        "MD/PhD/JD = 5, bachelor required = 4, some college = 3, high school = 2.\n"
        + queries_instruction +
        "\nRules:\n"
        "- role_family and excluded_roles must be MUTUALLY EXCLUSIVE word lists.\n"
        "- excluded_roles must reflect clear mismatches, not just different job titles.\n"
        "- Return ONLY valid JSON, no markdown, no explanation.\n\n"
        f"Resume:\n{resume_excerpt}"
    )

    max_tokens = 500 if include_queries else 300

    payload = json.dumps({
        "model": model,
        "max_tokens": max_tokens,
        "messages": [{"role": "user", "content": prompt}],
    }).encode("utf-8")

    try:
        req = urllib.request.Request(
            "https://api.anthropic.com/v1/messages",
            data=payload,
            headers={
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        raw = data["content"][0]["text"].strip()
        raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw, flags=re.MULTILINE).strip()
        return json.loads(raw)
    except Exception:
        return {}


# ---------------------------------------------------------------------------
# Domain-Aware Job Filtering
# ---------------------------------------------------------------------------

# Keyword signals per domain — use ROLE TITLE terms only (not generic tech terms)
# to avoid false positives when a professional uses ML/data tools in another domain
_DOMAIN_SIGNALS: Dict[str, set] = {
    "molecular_biology": {
        "molecular biologist", "biochemist", "cell biologist",
        "genomics scientist", "proteomics", "pcr technician", "western blot",
        "flow cytometry", "crispr scientist", "sequencing scientist",
        "gene expression", "tissue culture", "wet lab scientist", "bench scientist",
        "laboratory scientist biology", "assay development scientist",
        "cell culture scientist", "immunologist", "microbiologist", "virologist",
    },
    "data_science": {
        "data scientist", "ml engineer", "data engineer",
        "analytics engineer", "data analyst", "ai engineer",
        "nlp engineer", "computer vision engineer", "data science manager",
        "machine learning engineer", "data mining analyst",
        "business intelligence analyst", "bi developer", "quantitative researcher",
    },
    "software_engineering": {
        "software engineer", "software developer", "backend engineer", "frontend engineer",
        "full stack developer", "devops engineer", "platform engineer",
        "site reliability engineer", "sre", "mobile developer",
        "ios developer", "android developer", "web developer",
        "cloud engineer", "infrastructure engineer", "systems engineer",
    },
    "clinical_research": {
        "clinical research", "clinical trial", "clinical research associate", "cra",
        "clinical research coordinator", "crc", "clinical monitor", "study coordinator",
        "research coordinator", "regulatory affairs", "pharmacovigilance", "drug safety",
        "medical monitor", "clinical operations", "clinical project manager",
        "physician", "medical officer", "health informatics", "irb", "gcp compliance",
        "safety monitoring", "medical director", "principal investigator",
        "clinical data manager", "redcap", "medidata", "emr", "ehr",
    },
    "finance": {
        "investment banking", "portfolio manager", "quantitative analyst", "quant analyst",
        "financial analyst", "risk analyst", "credit analyst", "equity research analyst",
        "asset management", "hedge fund", "fixed income analyst",
        "derivatives trader", "private equity analyst", "venture capital analyst",
    },
}

# If resume domain is key → jobs detected in these domains are incompatible
_INCOMPATIBLE_DOMAINS: Dict[str, set] = {
    "molecular_biology": {"data_science", "software_engineering", "finance"},
    "data_science": {"molecular_biology"},
    "software_engineering": {"molecular_biology", "clinical_research", "finance"},
    "clinical_research": {"data_science", "software_engineering", "finance"},
    "finance": {"molecular_biology", "clinical_research", "software_engineering"},
}


def _detect_text_domain(text: str) -> Optional[str]:
    """Detect domain from text using keyword signals. Returns domain key or None."""
    text_lower = text.lower()
    scores: Dict[str, int] = {}
    for domain, signals in _DOMAIN_SIGNALS.items():
        score = sum(1 for s in signals if s in text_lower)
        if score > 0:
            scores[domain] = score
    if not scores:
        return None
    return max(scores, key=scores.__getitem__)


def _normalize_domain(domain_str: str) -> Optional[str]:
    """Map a free-text domain description (from AI analysis) to a domain key."""
    if not domain_str:
        return None
    d = domain_str.lower()
    if any(k in d for k in ("molecular", "biochem", "cell bio", "genomic", "virol", "microbi", "immunol", "neurosci")):
        return "molecular_biology"
    if any(k in d for k in ("data sci", "machine learn", "ml ", "artificial intel", "nlp", "analytics")):
        return "data_science"
    if any(k in d for k in ("software", "web dev", "backend", "frontend", "devops", "platform eng", "cloud eng")):
        return "software_engineering"
    if any(k in d for k in ("clinical", "trial", "cra", "crc", "regulatory", "pharmacovig", "drug safety")):
        return "clinical_research"
    if any(k in d for k in ("financ", "banking", "invest", "portfolio", "quant", "hedge fund", "private equity")):
        return "finance"
    return None


def _keyword_role_filter(
    candidates: List[Dict[str, Any]],
    excluded_roles: List[str],
) -> List[Dict[str, Any]]:
    """
    Fast pre-filter: remove any job whose title contains an excluded role keyword.
    No API call — purely string matching on job titles.

    Example: excluded_roles=["nurse","coordinator","technician","aide"] for a physician
    immediately removes "Clinical Research Nurse" and "Study Coordinator" listings.
    """
    if not excluded_roles:
        return candidates

    excluded_lower = [e.lower().strip() for e in excluded_roles if e.strip()]
    filtered = []
    for job in candidates:
        title_lower = job.get("title", "").lower()
        if any(excl in title_lower for excl in excluded_lower):
            continue
        filtered.append(job)

    # No safety threshold here — AI-generated role exclusions are precise.
    # A physician getting all nurse results from Adzuna should have them ALL removed.
    # If filtered is empty, discover_jobs will return 0 results and tell the user
    # to broaden their search, which is more useful than showing wrong-role jobs.
    return filtered


def _ai_role_filter(
    candidates: List[Dict[str, Any]],
    profile: Dict[str, Any],
) -> List[Dict[str, Any]]:
    """
    AI batch filter: one Claude Haiku call to confirm role + seniority compatibility.
    Uses the full structured candidate profile so Claude understands exactly who this
    person is and what roles they should NOT see.

    Falls back to all candidates on any error.
    """
    api_key = os.getenv("ANTHROPIC_API_KEY", "")
    if not api_key or not candidates:
        return candidates

    model = os.getenv("ANTHROPIC_MODEL", "claude-haiku-4-5-20251001")

    role_type = profile.get("role_type", "professional")
    domain = profile.get("domain", "")
    career_level = profile.get("career_level", "")
    excluded_roles = profile.get("excluded_roles", [])
    role_family = profile.get("role_family", [])
    specialties = profile.get("specialties", [])

    job_list = "\n".join(
        f"{i}: {job['title']} at {job.get('company', 'Unknown')}"
        for i, job in enumerate(candidates)
    )

    prompt = (
        f"Candidate profile:\n"
        f"  Role type    : {role_type}\n"
        f"  Career level : {career_level}\n"
        f"  Domain       : {domain}\n"
        f"  Role family  : {role_family}\n"
        f"  Specialties  : {specialties}\n"
        f"  Exclude roles: {excluded_roles}\n\n"
        f"Job listings:\n{job_list}\n\n"
        "Return a JSON array of indices to KEEP. Keep jobs that match the candidate's "
        "role type and career level. Remove jobs in a different role family "
        "(e.g., nursing/coordinator jobs for a physician) or far below their level. "
        "When uncertain, KEEP. Return ONLY a JSON array of integers, e.g. [0,1,3]. No explanation."
    )

    payload = json.dumps({
        "model": model,
        "max_tokens": 150,
        "messages": [{"role": "user", "content": prompt}],
    }).encode("utf-8")

    try:
        req = urllib.request.Request(
            "https://api.anthropic.com/v1/messages",
            data=payload,
            headers={
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        raw = data["content"][0]["text"].strip()
        raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw, flags=re.MULTILINE).strip()
        keep_indices = set(json.loads(raw))
        filtered = [job for i, job in enumerate(candidates) if i in keep_indices]
        # Safety: only override AI if it removed EVERYTHING (true edge case)
        if len(filtered) == 0 and len(candidates) > 0:
            return candidates
        return filtered
    except Exception:
        return candidates  # Fail open


def _heuristic_role_filter(
    candidates: List[Dict[str, Any]],
    resume_domain_key: Optional[str],
) -> List[Dict[str, Any]]:
    """
    Fallback when no API key: keyword-based domain incompatibility filter.
    """
    blocked_domains = _INCOMPATIBLE_DOMAINS.get(resume_domain_key or "", set())
    if not blocked_domains:
        return candidates

    filtered = []
    for job in candidates:
        job_text = f"{job.get('title', '')} {job.get('description', '')[:300]}"
        job_domain = _detect_text_domain(job_text)
        if job_domain and job_domain in blocked_domains:
            continue
        filtered.append(job)

    if len(filtered) < len(candidates) * 0.4:
        return candidates
    return filtered


# ---------------------------------------------------------------------------
# Main Orchestrator
# ---------------------------------------------------------------------------

def discover_jobs(
    resume_text: str,
    job_title: str,
    location: str = "",
    remote_only: bool = False,
    max_results: int = 10,
) -> Dict[str, Any]:
    """
    Search for jobs and score them against the user's resume.

    Two-tier scoring:
      1. Lightweight score top 20 candidates (~2s)
      2. Full ATS+HR score top `max_results` finalists (~5-8s)

    Args:
        resume_text: Full text of the user's resume.
        job_title: Target job title to search for.
        location: Geographic location filter (optional).
        remote_only: If True, also search Remotive for remote jobs.
        max_results: Number of top-scored jobs to return (1-20).

    Returns:
        Dict with ranked jobs, query info, and attribution.
    """
    max_results = min(max(max_results, 1), 20)
    all_jobs: List[Dict[str, Any]] = []

    # --- Step 0: AI resume analysis — ALWAYS run to get structured candidate profile ---
    # When job_title is given: profile-only (no search query generation, faster/cheaper)
    # When job_title is blank: full analysis including search queries
    has_title = bool(job_title.strip())
    ai_analysis: Dict[str, Any] = analyze_resume_for_search(
        resume_text, include_queries=not has_title
    )

    if not has_title:
        job_title = ai_analysis.get("recent_title", "") or "professional"

    # Build search query list
    search_queries = [job_title]
    if not has_title and ai_analysis:
        for q in ai_analysis.get("search_queries", []):
            if q.lower().strip() != job_title.lower().strip() and q not in search_queries:
                search_queries.append(q)
        search_queries = search_queries[:4]

    # --- Step 1: Search APIs ---
    has_adzuna = _adzuna_configured()
    seen_ids: set = set()

    for query in search_queries:
        if has_adzuna:
            for job in search_adzuna(query, location=location):
                if job["id"] not in seen_ids:
                    seen_ids.add(job["id"])
                    all_jobs.append(job)

        if remote_only or not all_jobs:
            for job in search_remotive(query):
                if job["id"] not in seen_ids:
                    seen_ids.add(job["id"])
                    all_jobs.append(job)

        if len(all_jobs) >= 40:
            break

    if not all_jobs:
        if not has_adzuna:
            return {
                "jobs": [],
                "query": {"job_title": job_title, "location": location, "remote_only": remote_only},
                "attribution": "No API keys configured.",
                "setup_required": True,
                "message": (
                    "Job discovery requires API keys. You have two options:\n\n"
                    "1. **Cloud (recommended):** Use the hosted scorer at "
                    "https://resume-scorer-web.streamlit.app — no setup needed, "
                    "Adzuna search is built in.\n\n"
                    "2. **Local:** Get free Adzuna API keys at https://developer.adzuna.com/ "
                    "and add to your .env file:\n"
                    "   ADZUNA_APP_ID=your_app_id\n"
                    "   ADZUNA_APP_KEY=your_app_key"
                ),
            }
        return {
            "jobs": [],
            "query": {"job_title": job_title, "location": location, "remote_only": remote_only},
            "attribution": "No results found. Try a broader job title or different location.",
        }

    # --- Step 2: Pre-filter by title similarity (keep top 30) ---
    for job in all_jobs:
        job["_title_sim"] = _title_similarity(job["title"], job_title)

    all_jobs.sort(key=lambda j: j["_title_sim"], reverse=True)
    candidates = all_jobs[:30]  # Wider pool before role filtering

    # --- Step 2a: Fast keyword role exclusion (no API call) ---
    # Use AI-extracted excluded_roles if available, else heuristic domain detection
    excluded_roles: List[str] = ai_analysis.get("excluded_roles", [])
    if excluded_roles:
        candidates = _keyword_role_filter(candidates, excluded_roles)

    # --- Step 2b: AI role + seniority filter (batch, one API call) ---
    if ai_analysis and os.getenv("ANTHROPIC_API_KEY", ""):
        candidates = _ai_role_filter(candidates, ai_analysis)
    else:
        # No AI profile: fall back to heuristic domain filter
        resume_domain_key = _normalize_domain(ai_analysis.get("domain", ""))
        if not resume_domain_key:
            resume_domain_key = _detect_text_domain(resume_text[:2000])
        candidates = _heuristic_role_filter(candidates, resume_domain_key)

    # --- Step 3: Lightweight score all candidates ---
    for job in candidates:
        desc = job.get("description", "")
        if not desc:
            job["_light_score"] = 0.0
            continue
        try:
            job["_light_score"] = lightweight_score(resume_text, desc)
        except Exception:
            job["_light_score"] = 0.0

    candidates.sort(key=lambda j: j["_light_score"], reverse=True)
    finalists = candidates[:max_results]

    # If domain/role filtering removed everything, return a helpful message
    if not finalists:
        role_type = ai_analysis.get("role_type", "your role") if ai_analysis else "your role"
        return {
            "jobs": [],
            "query": {"job_title": job_title, "location": location, "remote_only": remote_only},
            "attribution": "Powered by Adzuna" if _adzuna_configured() else "No source",
            "message": (
                f"No matching jobs found for {role_type} in this search. "
                "The available listings were filtered out because they didn't match "
                "your role family or career level. Try: a broader location, "
                "a different job title, or leave the title blank to let AI suggest searches."
            ),
        }

    # --- Step 4: Full ATS+HR score for finalists ---
    import ats_scorer
    import hr_scorer

    ranked_jobs = []
    for rank_idx, job in enumerate(finalists, 1):
        desc = job.get("description", "")
        result_entry = {
            "rank": rank_idx,
            "source": job["source"],
            "title": job["title"],
            "company": job["company"],
            "location": job["location"],
            "salary_min": job.get("salary_min"),
            "salary_max": job.get("salary_max"),
            "url": job["url"],
            "posted_date": job.get("posted_date", ""),
            "category": job.get("category", ""),
            "description": desc,
        }

        if not desc:
            result_entry["scoring_tier"] = "none"
            result_entry["ats_score"] = 0
            result_entry["hr_score"] = 0
            result_entry["ats_detail"] = {}
            result_entry["hr_detail"] = {}
            ranked_jobs.append(result_entry)
            continue

        # Full ATS scoring
        try:
            ats_result = ats_scorer.calculate_ats_score(resume_text, desc)
            ats_score = round(ats_result.get("total_score", 0), 1)
            ats_detail = {
                "matched_keywords": ats_result.get("matched_keywords", []),
                "missing_keywords": ats_result.get("missing_keywords", []),
                "domain": ats_result.get("domain", ""),
            }
        except Exception:
            ats_score = round(job.get("_light_score", 0), 1)
            ats_detail = {"error": "Full ATS scoring failed, using lightweight score"}

        # Full HR scoring
        try:
            hr_result = hr_scorer.calculate_hr_score_from_text(resume_text, desc)
            hr_dict = hr_scorer.result_to_dict(hr_result)
            hr_score = round(hr_dict.get("overall_score", 0), 1)
            hr_detail = {
                "recommendation": hr_dict.get("recommendation", "Unknown"),
                "experience_fit": hr_dict.get("factor_breakdown", {}).get("experience", 0),
                "skills_match": hr_dict.get("factor_breakdown", {}).get("skills", 0),
            }
        except Exception as e:
            hr_score = round(job.get("_light_score", 0) * 0.8, 1)  # fallback: 80% of light score
            hr_detail = {"error": f"HR scoring failed: {type(e).__name__}: {e}"}

        result_entry["scoring_tier"] = "full"
        result_entry["ats_score"] = ats_score
        result_entry["hr_score"] = hr_score
        result_entry["ats_detail"] = ats_detail
        result_entry["hr_detail"] = hr_detail
        ranked_jobs.append(result_entry)

    # Sort finalists by combined score (ATS 60% + HR 40%)
    for job in ranked_jobs:
        job["_combined"] = job.get("ats_score", 0) * 0.6 + job.get("hr_score", 0) * 0.4
    ranked_jobs.sort(key=lambda j: j["_combined"], reverse=True)

    # Re-rank
    for idx, job in enumerate(ranked_jobs, 1):
        job["rank"] = idx
        del job["_combined"]

    # Build attribution
    sources = set(j["source"] for j in ranked_jobs)
    attr_parts = []
    if "adzuna" in sources:
        attr_parts.append("Adzuna")
    if "remotive" in sources:
        attr_parts.append("Remotive")
    attribution = f"Powered by {' & '.join(attr_parts)}" if attr_parts else "No source"

    result: Dict[str, Any] = {
        "jobs": ranked_jobs,
        "query": {
            "job_title": job_title,
            "location": location,
            "remote_only": remote_only,
        },
        "attribution": attribution,
    }
    if ai_analysis:
        result["ai_analysis"] = {
            "recent_title": ai_analysis.get("recent_title", ""),
            "career_level": ai_analysis.get("career_level", ""),
            "domain": ai_analysis.get("domain", ""),
            "role_type": ai_analysis.get("role_type", ""),
            "role_family": ai_analysis.get("role_family", []),
            "excluded_roles": ai_analysis.get("excluded_roles", []),
            "specialties": ai_analysis.get("specialties", []),
            "search_queries_used": search_queries,
        }
    return result
