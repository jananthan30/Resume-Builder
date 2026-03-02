"""
HTTP client for MCP thin client — calls cloud Scorer API with retry and fallback.

Used by mcp_scorer.py to try cloud API first, fall back to local scoring.
"""

import os
import json
import time
from typing import Dict, Any, Optional
from urllib.request import Request, urlopen
from urllib.error import URLError, HTTPError


# Cloud API base URL — configurable via environment
CLOUD_API_URL = os.getenv("SCORER_CLOUD_URL", "https://resume-scorer.fly.dev")
CLOUD_API_KEY = os.getenv("SCORER_CLOUD_API_KEY", "")
CLOUD_TIMEOUT = int(os.getenv("SCORER_CLOUD_TIMEOUT", "30"))
CLOUD_RETRIES = int(os.getenv("SCORER_CLOUD_RETRIES", "2"))


def _is_cloud_configured() -> bool:
    """Check if cloud scoring is configured. API key is optional (anonymous access allowed)."""
    return bool(CLOUD_API_URL)


def cloud_score(
    endpoint: str,
    resume_text: str,
    jd_text: str,
    extra_params: Optional[Dict[str, Any]] = None,
) -> Optional[Dict[str, Any]]:
    """
    Call the cloud Scorer API.

    Args:
        endpoint: API endpoint path (e.g., "/score/ats", "/score/both")
        resume_text: Resume text
        jd_text: Job description text
        extra_params: Additional parameters (domain_hint, etc.)

    Returns:
        API response dict, or None if cloud is unavailable/failed.
    """
    if not _is_cloud_configured():
        return None

    url = f"{CLOUD_API_URL.rstrip('/')}{endpoint}"

    payload = {
        "resume_text": resume_text,
        "jd_text": jd_text,
    }
    if extra_params:
        payload.update(extra_params)

    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    if CLOUD_API_KEY:
        headers["X-API-Key"] = CLOUD_API_KEY

    data = json.dumps(payload).encode("utf-8")

    for attempt in range(CLOUD_RETRIES + 1):
        try:
            req = Request(url, data=data, headers=headers, method="POST")
            with urlopen(req, timeout=CLOUD_TIMEOUT) as resp:
                body = resp.read().decode("utf-8")
                result = json.loads(body)
                result["_source"] = "cloud"
                return result

        except HTTPError as e:
            if e.code == 402:
                # Free tier limit exceeded — return error so caller can show upgrade prompt
                try:
                    body = json.loads(e.read().decode("utf-8"))
                except Exception:
                    body = {}
                return {
                    "_error": "usage_limit",
                    "_message": body.get("detail", "Free tier limit reached (5 scores). Upgrade to Pro for unlimited scoring."),
                    "_upgrade_url": "https://resume-scorer-web.streamlit.app",
                }
            if e.code in (401, 403):
                # Auth error — don't retry
                return None
            if e.code == 429:
                # Rate limited — wait and retry
                if attempt < CLOUD_RETRIES:
                    time.sleep(2 ** attempt)
                    continue
                return None
            if e.code >= 500:
                # Server error — retry
                if attempt < CLOUD_RETRIES:
                    time.sleep(1)
                    continue
                return None
            return None

        except (URLError, TimeoutError, OSError):
            # Network error — retry
            if attempt < CLOUD_RETRIES:
                time.sleep(1)
                continue
            return None

        except (json.JSONDecodeError, Exception):
            return None

    return None


def cloud_get_resume() -> Optional[Dict[str, Any]]:
    """Fetch the user's saved resume from cloud.
    Returns dict with resume_text + metadata, or None if not configured / not found."""
    if not CLOUD_API_KEY:
        return None
    url = f"{CLOUD_API_URL.rstrip('/')}/resume"
    headers = {"X-API-Key": CLOUD_API_KEY, "Accept": "application/json"}
    try:
        req = Request(url, headers=headers, method="GET")
        with urlopen(req, timeout=CLOUD_TIMEOUT) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except HTTPError as e:
        if e.code == 404:
            return {"resume_on_file": False}
        return None
    except Exception:
        return None


def cloud_save_resume(text: str, filename: str = "resume.txt") -> Optional[Dict[str, Any]]:
    """Upload or replace the user's saved resume in the cloud.
    Returns metadata dict or None on failure."""
    if not CLOUD_API_KEY:
        return None
    url = f"{CLOUD_API_URL.rstrip('/')}/resume/upload"
    payload = {"resume_text": text, "resume_filename": filename}
    headers = {
        "Content-Type": "application/json",
        "X-API-Key": CLOUD_API_KEY,
    }
    try:
        req = Request(url, data=json.dumps(payload).encode("utf-8"), headers=headers, method="POST")
        with urlopen(req, timeout=CLOUD_TIMEOUT) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception:
        return None


def cloud_health() -> Optional[Dict[str, Any]]:
    """Check cloud API health."""
    if not _is_cloud_configured():
        return None

    url = f"{CLOUD_API_URL.rstrip('/')}/health"
    try:
        req = Request(url, method="GET")
        with urlopen(req, timeout=5) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception:
        return None
