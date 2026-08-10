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

import ipaddress
import os
import re
import socket
from typing import Optional
from urllib.parse import parse_qs, urlencode, urljoin, urlparse, urlunparse


class BlockedURLError(ValueError):
    """URL refused by the SSRF policy — never fetched."""


_ALLOWED_SCHEMES = {"http", "https"}
_MAX_REDIRECTS = 5
_BLOCKED_MSG = "This URL points to a private or internal address and cannot be fetched."

# Ports a job listing is ever served on. Without this, a public hostname is a
# way to reach ANY port on that host -- databases, admin panels, internal
# services bound to a routable interface -- because the address check only
# ever looked at the host.
_ALLOWED_PORTS = {80, 443}
_PORT_BLOCKED_MSG = "Only standard web ports (80 and 443) can be fetched."

# Hard ceiling on how much we will pull down. The body was previously read
# whole into memory on a 1GB machine, and the 8,000-char cap applied only
# after the download, on one fallback branch. gzip is in Accept-Encoding and
# requests decompresses transparently, so a small response can expand far
# past its wire size -- the check below therefore counts DECOMPRESSED bytes.
_MAX_DOWNLOAD_BYTES = 4 * 1024 * 1024
_TOO_LARGE_MSG = "This page is too large to fetch."

# Whole-request wall clock. requests' own timeout bounds each socket read
# individually, so a server dripping one byte at a time holds the worker
# indefinitely without ever tripping it.
_MAX_TOTAL_SECONDS = 30.0

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


def _host_resolves_public(host: str) -> bool:
    """True only when *host* resolves and every resolved address is global.

    Fail closed: unresolvable hosts are treated as non-public. All addresses
    must pass because an attacker-controlled name can mix public and private
    records in one answer.
    """
    try:
        infos = socket.getaddrinfo(host, 443, proto=socket.IPPROTO_TCP)
    except (socket.gaierror, OSError):
        return False
    ips = [info[4][0] for info in infos]
    if not ips:
        return False
    for ip_str in ips:
        try:
            addr = ipaddress.ip_address(ip_str)
        except ValueError:
            return False
        if not addr.is_global or addr.is_multicast:
            return False
    return True


def validate_public_url(url: str) -> str:
    """Raise BlockedURLError unless *url* is http(s), on a standard web port,
    to a public address."""
    parsed = urlparse(url)
    if parsed.scheme not in _ALLOWED_SCHEMES or not parsed.hostname:
        raise BlockedURLError(_BLOCKED_MSG)
    # An explicit port must be one we allow; absent means the scheme default,
    # which already is. urlparse raises for a malformed port, which is a
    # refusal too -- never a pass.
    try:
        port = parsed.port
    except ValueError:
        raise BlockedURLError(_BLOCKED_MSG) from None
    if port is not None and port not in _ALLOWED_PORTS:
        raise BlockedURLError(_PORT_BLOCKED_MSG)
    if not _host_resolves_public(parsed.hostname):
        raise BlockedURLError(_BLOCKED_MSG)
    return url


def _read_capped(resp, deadline: float) -> str:
    """Return the decoded body, refusing anything past the size or time cap.

    Streams so an oversized response is abandoned mid-transfer rather than
    buffered whole and rejected afterwards -- the point is never to hold it
    in memory at all.
    """
    import time

    declared = resp.headers.get("Content-Length")
    if declared and declared.isdigit() and int(declared) > _MAX_DOWNLOAD_BYTES:
        raise BlockedURLError(_TOO_LARGE_MSG)

    chunks: list[bytes] = []
    total = 0
    for chunk in resp.iter_content(chunk_size=64 * 1024):
        if time.monotonic() > deadline:
            raise BlockedURLError("Timed out fetching this URL.")
        if not chunk:
            continue
        total += len(chunk)
        if total > _MAX_DOWNLOAD_BYTES:
            raise BlockedURLError(_TOO_LARGE_MSG)
        chunks.append(chunk)

    body = b"".join(chunks)
    encoding = resp.encoding or resp.apparent_encoding or "utf-8"
    try:
        return body.decode(encoding, errors="replace")
    except LookupError:
        return body.decode("utf-8", errors="replace")


def _safe_get(url: str, timeout: int):
    """The one guarded fetch path: redirects followed manually so every hop
    is re-validated before it is requested (a public URL may 302 internal).

    Returns (response, body_text); the body is read here, under the size and
    deadline caps, because streaming has to be consumed while the response is
    still open.

    Residual risk: DNS answers can change between validation and connect
    (rebinding); closing that fully needs connection-time IP pinning.
    """
    import time

    import requests

    deadline = time.monotonic() + _MAX_TOTAL_SECONDS
    current = url
    for _ in range(_MAX_REDIRECTS + 1):
        validate_public_url(current)
        if time.monotonic() > deadline:
            raise BlockedURLError("Timed out fetching this URL.")
        resp = requests.get(
            current, headers=_HEADERS, timeout=timeout,
            allow_redirects=False, stream=True,
        )
        try:
            if resp.status_code in (301, 302, 303, 307, 308):
                location = resp.headers.get("Location")
                if not location:
                    return resp, ""
                current = urljoin(current, location)
                continue
            return resp, _read_capped(resp, deadline)
        finally:
            resp.close()
    raise BlockedURLError("Too many redirects while fetching this URL.")


def fetch_jd_from_url(url: str, timeout: int = 15) -> Optional[str]:
    """Return plain-text job description scraped from *url*, or None on failure.

    Raises BlockedURLError (never fetches) for non-http(s) schemes, non-standard
    ports, and hosts that resolve to private, loopback, link-local, or otherwise
    non-global addresses — including any redirect hop that lands on one. Also
    raised when the response exceeds the size cap or the whole fetch outruns its
    deadline; both are refusals, not silent truncation.
    """
    # Strip tracking params — reduces bot-detection likelihood
    url = _strip_tracking_params(url)

    try:
        resp, html = _safe_get(url, timeout)
    except BlockedURLError:
        raise
    except Exception:
        return None
    if resp.status_code != 200 or not html:
        return None

    # ── 1. trafilatura extraction (best quality) — on HTML we already
    #        fetched; its own fetch_url would bypass the guard above ────────
    try:
        import trafilatura

        text = trafilatura.extract(
            html,
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

    # ── 2. BeautifulSoup selector fallback ─────────────────────────────────
    try:
        from bs4 import BeautifulSoup

        soup = BeautifulSoup(html, "lxml")

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
            model=os.getenv("ANTHROPIC_MODEL", "claude-haiku-4-5-20251001"),
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
