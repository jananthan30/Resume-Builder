"""
PII Redaction module using Microsoft Presidio.

Redacts personal information before sending to external LLM APIs.
Preserves: job titles, company names, skills, dates, credentials.
Redacts: names, emails, phones, addresses, SSNs, URLs.

Usage:
    from pii_redactor import redact_text, PRESIDIO_AVAILABLE
    cleaned = redact_text(resume_text)
"""

import importlib.util
import re
from typing import List, Optional, Set

# Try to import Presidio; fall back to regex-only redaction if unavailable
try:
    from presidio_analyzer import AnalyzerEngine, Pattern, PatternRecognizer, RecognizerResult
    from presidio_analyzer.nlp_engine import NlpEngineProvider
    from presidio_anonymizer import AnonymizerEngine
    from presidio_anonymizer.entities import OperatorConfig
    PRESIDIO_AVAILABLE = True
except ImportError:
    PRESIDIO_AVAILABLE = False


# ---------------------------------------------------------------------------
# Lazy-loaded singletons
# ---------------------------------------------------------------------------
_analyzer: Optional[object] = None
_anonymizer: Optional[object] = None

# Entity types we redact by default
DEFAULT_REDACT_ENTITIES = [
    "PERSON",
    "EMAIL_ADDRESS",
    "PHONE_NUMBER",
    "US_SSN",
    "CREDIT_CARD",
    "IP_ADDRESS",
    "URL",
]

# Tokens to preserve (never redact even if NER flags them)
PRESERVE_TOKENS: Set[str] = {
    # Common company names that NER might flag as PERSON
    "google", "pfizer", "novartis", "merck", "amazon", "microsoft", "apple",
    "meta", "johnson", "yale", "harvard", "stanford", "columbia", "cornell",
    "duke", "mayo clinic", "cleveland clinic", "johns hopkins",
    # Credentials / certifications
    "m.d.", "ph.d.", "r.n.", "cpc", "ccs", "rhia", "rhit", "crc", "ccra",
    "pmp", "aws", "cpa", "cfa", "mba",
}


def get_analyzer():
    """Return a lazily-initialized Presidio AnalyzerEngine with custom recognizers."""
    global _analyzer
    if _analyzer is not None:
        return _analyzer

    if not PRESIDIO_AVAILABLE:
        return None

    # Do not trigger spaCy model downloads at runtime. On Fly's 1 GB VM, the
    # large English model can OOM the instance during first LLM request.
    nlp_engine = None
    if importlib.util.find_spec("en_core_web_sm") is not None:
        try:
            configuration = {
                "nlp_engine_name": "spacy",
                "models": [{"lang_code": "en", "model_name": "en_core_web_sm"}],
            }
            provider = NlpEngineProvider(nlp_configuration=configuration)
            nlp_engine = provider.create_engine()
        except Exception:
            nlp_engine = None

    if nlp_engine is None:
        return None

    _analyzer = AnalyzerEngine(nlp_engine=nlp_engine, supported_languages=["en"])

    # --- Custom recognizers ---

    # LinkedIn URL recognizer (we DO want to redact these)
    linkedin_pattern = Pattern(
        name="linkedin_pattern",
        regex=r"(?:https?://)?(?:www\.)?linkedin\.com/in/[\w\-]+/?",
        score=0.9,
    )
    linkedin_recognizer = PatternRecognizer(
        supported_entity="URL",
        name="LinkedInRecognizer",
        patterns=[linkedin_pattern],
    )
    _analyzer.registry.add_recognizer(linkedin_recognizer)

    # GitHub URL recognizer
    github_pattern = Pattern(
        name="github_pattern",
        regex=r"(?:https?://)?(?:www\.)?github\.com/[\w\-]+/?",
        score=0.9,
    )
    github_recognizer = PatternRecognizer(
        supported_entity="URL",
        name="GitHubRecognizer",
        patterns=[github_pattern],
    )
    _analyzer.registry.add_recognizer(github_recognizer)

    return _analyzer


def get_anonymizer():
    """Return a lazily-initialized Presidio AnonymizerEngine."""
    global _anonymizer
    if _anonymizer is not None:
        return _anonymizer

    if not PRESIDIO_AVAILABLE:
        return None

    _anonymizer = AnonymizerEngine()
    return _anonymizer


# ---------------------------------------------------------------------------
# Presidio-based redaction
# ---------------------------------------------------------------------------

def _presidio_redact(text: str, entities: Optional[List[str]] = None) -> str:
    """Redact PII using Presidio (NER-backed)."""
    analyzer = get_analyzer()
    anonymizer = get_anonymizer()

    if analyzer is None or anonymizer is None:
        # Presidio init failed; fall back to regex
        return _regex_redact(text, entities)

    target_entities = entities or DEFAULT_REDACT_ENTITIES

    results = analyzer.analyze(
        text=text,
        language="en",
        entities=target_entities,
        score_threshold=0.4,
    )

    # Filter out false positives (preserve known tokens)
    filtered: List[RecognizerResult] = []
    for r in results:
        span_text = text[r.start:r.end].strip().lower()
        if span_text in PRESERVE_TOKENS:
            continue
        # Don't redact very short matches that are likely false positives
        if r.entity_type == "PERSON" and len(span_text) <= 2:
            continue
        filtered.append(r)

    # Build operator map: entity type -> placeholder tag
    operators = {}
    for entity in target_entities:
        tag = entity.replace("_ADDRESS", "").replace("US_", "")
        operators[entity] = OperatorConfig("replace", {"new_value": f"<{tag}>"})

    result = anonymizer.anonymize(
        text=text,
        analyzer_results=filtered,
        operators=operators,
    )
    return result.text


# ---------------------------------------------------------------------------
# Regex-based fallback redaction
# ---------------------------------------------------------------------------

_EMAIL_RE = re.compile(
    r'\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b'
)
_PHONE_RE = re.compile(
    r'(?:\+?1[\s\-.]?)?\(?\d{3}\)?[\s\-.]?\d{3}[\s\-.]?\d{4}\b'
)
_SSN_RE = re.compile(
    r'\b\d{3}[\-\s]?\d{2}[\-\s]?\d{4}\b'
)
_URL_RE = re.compile(
    r'https?://[^\s<>\"\']+|(?:www\.)[^\s<>\"\']+',
    re.IGNORECASE,
)
_LINKEDIN_RE = re.compile(
    r'(?:https?://)?(?:www\.)?linkedin\.com/in/[\w\-]+/?',
    re.IGNORECASE,
)
_GITHUB_RE = re.compile(
    r'(?:https?://)?(?:www\.)?github\.com/[\w\-]+/?',
    re.IGNORECASE,
)
_IP_RE = re.compile(
    r'\b(?:\d{1,3}\.){3}\d{1,3}\b'
)
_CREDIT_CARD_RE = re.compile(
    r'\b(?:\d{4}[\s\-]?){3}\d{4}\b'
)
# Street address pattern
_ADDRESS_RE = re.compile(
    r'\b\d+\s+[A-Za-z]+\s+(?:Street|St|Avenue|Ave|Road|Rd|Drive|Dr|Lane|Ln|Boulevard|Blvd|Way|Court|Ct|Circle|Cir|Place|Pl)\b',
    re.IGNORECASE,
)


def _regex_redact(text: str, entities: Optional[List[str]] = None) -> str:
    """Fallback PII redaction using regex patterns (no NER model needed)."""
    target = set(entities or DEFAULT_REDACT_ENTITIES)

    # Order matters: do more specific patterns first

    if "EMAIL_ADDRESS" in target:
        text = _EMAIL_RE.sub("<EMAIL>", text)

    if "URL" in target:
        text = _LINKEDIN_RE.sub("<URL>", text)
        text = _GITHUB_RE.sub("<URL>", text)
        text = _URL_RE.sub("<URL>", text)

    if "PHONE_NUMBER" in target:
        text = _PHONE_RE.sub("<PHONE>", text)

    if "US_SSN" in target:
        text = _SSN_RE.sub("<SSN>", text)

    if "CREDIT_CARD" in target:
        text = _CREDIT_CARD_RE.sub("<CREDIT_CARD>", text)

    if "IP_ADDRESS" in target:
        text = _IP_RE.sub("<IP_ADDRESS>", text)

    # Regex-based name redaction: first non-empty line that looks like a name
    if "PERSON" in target:
        lines = text.split('\n')
        for i, line in enumerate(lines):
            stripped = line.strip()
            if stripped and len(stripped) < 60:
                # Looks like a name line (mostly alpha, commas, periods, spaces)
                cleaned = stripped.replace(',', '').replace('.', '').strip()
                if cleaned and re.match(r'^[A-Za-z\s]+$', cleaned):
                    lines[i] = "<PERSON>"
                    break
        text = '\n'.join(lines)

    # Street addresses (partial location redaction)
    text = _ADDRESS_RE.sub("<ADDRESS>", text)

    return text


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def redact_text(text: str, entities: Optional[List[str]] = None) -> str:
    """
    Redact PII from text.

    Uses Presidio (NER-backed) when available, falls back to regex patterns.

    Args:
        text: Input text (resume or job description).
        entities: Optional list of Presidio entity types to redact.
                  Defaults to DEFAULT_REDACT_ENTITIES.

    Returns:
        Text with PII replaced by placeholder tags like <PERSON>, <EMAIL>, etc.
    """
    if not text or not text.strip():
        return text

    if PRESIDIO_AVAILABLE:
        return _presidio_redact(text, entities)
    else:
        return _regex_redact(text, entities)
