"""
JD Fetcher — scrape full job description from a listing URL.

Strategy (in order):
  1. trafilatura  — best-quality main-content extractor
  2. requests + BeautifulSoup — CSS-selector fallback
  3. Raw body text (capped at 8 000 chars)

Then optionally run Claude Haiku to extract only the JD portion from the
raw page dump (useful when the page has a lot of surrounding nav/footer noise).
"""

from __future__ import annotations

import os
import re
from typing import Optional
from urllib.parse import urlparse, urlencode, parse_qs, urlunparse

# Realistic browser headers — mimic Chrome more completely to reduce bot detection
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Cache-Control": "max-age=0",
}

# Tracking params that identify API/bot traffic — strip before scraping
_TRACKING_PARAMS = {"utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
                    "se", "v", "ref", "source", "sid", "cid", "clickid"}

# CSS selectors tried in order for common job-board layouts
_JD_SELECTORS = [
    # Generic job description containers
    "[class*='job-description']",
    "[class*='jobDescription']",
    "[id*='job-description']",
    "[id*='jobDescription']",
    "[class*='jobDetails']",
    "[class*='posting-description']",
    "[class*='job-details']",
    # Adzuna
    "[class*='adp-body']",
    "[class*='job__description']",
    "[class*='jsr_description']",
    # LinkedIn
    "[class*='description__text']",
    "[class*='show-more-less-html']",
    # Greenhouse / Lever / Workday ATS
    "[data-testid='job-description']",
    "[id='job-description']",
    "[class*='job-body']",
    "[class*='posting-body']",
    "[class*='content-intro']",
    # Generic fallbacks
    "article",
    "main",
    "[role='main']",
]


def _strip_tracking_params(url: str) -> str:
    """Remove UTM and other tracking params that can trigger bot-detection."""
    try:
        parsed = urlparse(url)
        params = {k: v for k, v in parse_qs(parsed.query).items()
                  if k.lower() not in _TRACKING_PARAMS}
        clean_query = urlencode(params, doseq=True)
        return urlunparse(parsed._replace(query=clean_query))
    except Exception:
        return url


def fetch_jd_from_url(url: str, timeout: int = 15) -> Optional[str]:
    """Return plain-text job description scraped from *url*, or None on failure."""
    # Strip tracking params — reduces bot-detection likelihood
    url = _strip_tracking_params(url)

    # ── 1. trafilatura (best quality) ──────────────────────────────────────
    try:
        import trafilatura

        downloaded = trafilatura.fetch_url(url)
        if downloaded:
            text = trafilatura.extract(
                downloaded,
                include_tables=False,
                favor_recall=True,
                no_fallback=False,
            )
            if text and len(text) > 300:
                return _clean(text)
    except ImportError:
        pass
    except Exception:
        pass

    # ── 2. requests + BeautifulSoup ────────────────────────────────────────
    try:
        import requests
        from bs4 import BeautifulSoup

        resp = requests.get(url, headers=_HEADERS, timeout=timeout, allow_redirects=True)
        if resp.status_code != 200:
            return None

        soup = BeautifulSoup(resp.text, "lxml")

        # Strip navigation noise
        for tag in soup(["script", "style", "nav", "header", "footer",
                          "aside", "iframe", "noscript", "form"]):
            tag.decompose()

        # Try known selectors first
        for sel in _JD_SELECTORS:
            el = soup.select_one(sel)
            if el:
                text = el.get_text(separator="\n", strip=True)
                if len(text) > 300:
                    return _clean(text)

        # Fallback: full body text, capped
        body = soup.body
        if body:
            text = body.get_text(separator="\n", strip=True)
            if len(text) > 300:
                return _clean(text[:8000])

    except Exception:
        pass

    return None


def extract_jd_with_ai(
    raw_text: str,
    job_title: str = "",
    api_key: str = "",
) -> str:
    """
    Use Claude Haiku to pull out just the job-description content from a
    raw page dump.  Falls back to returning *raw_text* unchanged on any error.
    """
    key = api_key or os.getenv("ANTHROPIC_API_KEY", "")
    if not key:
        return raw_text

    try:
        import anthropic

        client = anthropic.Anthropic(api_key=key)
        msg = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=2000,
            messages=[
                {
                    "role": "user",
                    "content": (
                        "Extract ONLY the job description from the web page text below. "
                        "Include: role overview, responsibilities, requirements, "
                        "qualifications, benefits. "
                        "Exclude: navigation, cookie banners, headers, footers, "
                        "unrelated content.\n\n"
                        f"Job title hint: {job_title}\n\n"
                        f"Page text:\n{raw_text[:5000]}\n\n"
                        "Return the job description text only, no commentary."
                    ),
                }
            ],
        )
        result = msg.content[0].text.strip()
        # Sanity check: if AI returned something too short, use original
        return result if len(result) > 200 else raw_text
    except Exception:
        return raw_text


def _clean(text: str) -> str:
    """Collapse excessive whitespace / blank lines."""
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    lines = [ln for ln in text.splitlines() if len(ln.strip()) > 1 or ln.strip() == ""]
    return "\n".join(lines).strip()
