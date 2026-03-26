"""
ATS Resume Scorer - Compare base and tailored resumes against job descriptions
Can be used as:
1. Web interface: python ats_scorer.py --web
2. CLI scoring: python ats_scorer.py --score <resume_path> <jd_path>
3. Module import: from ats_scorer import calculate_ats_score

Enhanced Features (v2.0):
- Lemmatization for morphological matching (running/ran -> run)
- Acronym expansion (FDA -> food and drug administration)
- Synonym/related term matching via skill taxonomy
- Healthcare/Clinical/Pharma domain focus
- Semantic similarity with Sentence Transformers (§2.2.1)
- BM25 probabilistic scoring (§8.1.1)
- Format risk assessment for Workday/Taleo (§2.1.1)
- Header/Footer detection (§2.1.2)
- Keyword stuffing detection (§2.3.2)
- Readability metrics with Flesch-Kincaid (§3.1.2)
- Domain auto-detection (§4)
"""

import re
import os
import sys
import json
import argparse
import math
from collections import Counter
from pathlib import Path
from typing import Dict, List, Tuple, Any, Optional
import pdfplumber

# NLTK-based lemmatization (replaces spaCy per-word calls)
try:
    from nltk.stem import WordNetLemmatizer
    _wnl = WordNetLemmatizer()
    NLTK_AVAILABLE = True
except ImportError:
    _wnl = None
    NLTK_AVAILABLE = False

# Lazy-load sentence-transformers for semantic similarity (§2.2.1)
# Model loads on first scoring request, NOT at import time (saves 45-90s cold start)
import threading
_sbert_model = None
_sbert_lock = threading.Lock()
_sbert_load_failed = False
_sbert_load_error = None

try:
    from sentence_transformers import SentenceTransformer, util as sbert_util
    SBERT_AVAILABLE = True
except ImportError:
    SBERT_AVAILABLE = False
    SentenceTransformer = None
    sbert_util = None

def get_sbert_model():
    """Thread-safe lazy loading of SBERT model. Loads once on first call."""
    global _sbert_model, _sbert_load_failed, _sbert_load_error, SBERT_AVAILABLE
    if not SBERT_AVAILABLE or _sbert_load_failed:
        return None

    if _sbert_model is None:
        with _sbert_lock:
            if _sbert_model is None and not _sbert_load_failed:
                try:
                    _sbert_model = SentenceTransformer('all-MiniLM-L6-v2')
                except Exception as exc:
                    _sbert_load_failed = True
                    _sbert_load_error = str(exc)
                    SBERT_AVAILABLE = False
                    _sbert_model = None
    return _sbert_model

# =============================================================================
# EMBEDDING CACHE — Disk-based cache for SBERT embeddings
# =============================================================================
import hashlib
import numpy as np

_EMBED_CACHE_DIR = Path(__file__).parent / "embed_cache"
_EMBED_CACHE_DIR.mkdir(exist_ok=True)

# In-memory LRU cache for current session
_embed_mem_cache = {}

def _text_hash(text: str) -> str:
    """Generate SHA-256 hash of text for cache keying."""
    return hashlib.sha256(text.encode('utf-8')).hexdigest()

def embed_with_cache(text: str) -> 'np.ndarray':
    """Encode text with SBERT, using disk + memory cache."""
    if not SBERT_AVAILABLE:
        return None

    h = _text_hash(text)

    # Check in-memory cache first
    if h in _embed_mem_cache:
        return _embed_mem_cache[h]

    # Check disk cache
    cache_path = _EMBED_CACHE_DIR / f"{h}.npy"
    if cache_path.exists():
        emb = np.load(str(cache_path))
        _embed_mem_cache[h] = emb
        return emb

    # Compute embedding
    model = get_sbert_model()
    if model is None:
        return None
    emb = model.encode(text, convert_to_numpy=True)

    # Save to both caches
    np.save(str(cache_path), emb)
    _embed_mem_cache[h] = emb

    # Limit in-memory cache size
    if len(_embed_mem_cache) > 100:
        oldest_key = next(iter(_embed_mem_cache))
        del _embed_mem_cache[oldest_key]

    return emb

# Try to import textstat for readability metrics (§3.1.2)
try:
    import textstat
    TEXTSTAT_AVAILABLE = True
except ImportError:
    TEXTSTAT_AVAILABLE = False

# O*NET taxonomy for keyword validation (§ skill filtering)
try:
    from taxonomy.onet_loader import is_recognized_skill, get_all_skills, get_skills_for_domain, merge_with_domain_keywords
    ONET_AVAILABLE = True
except ImportError:
    ONET_AVAILABLE = False
    def is_recognized_skill(term): return False
    def get_all_skills(): return set()
    def get_skills_for_domain(domain): return set()
    def merge_with_domain_keywords(domain, dkw): return set(str(k).lower() for k in dkw.keys())

# Load data files
DATA_DIR = Path(__file__).parent / "data"

def load_json_data(filename, default=None):
    """Load JSON data file with fallback to default."""
    filepath = DATA_DIR / filename
    if filepath.exists():
        try:
            with open(filepath, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception:
            pass
    return default if default is not None else {}

# Load taxonomy and reference data
ACRONYMS = load_json_data("acronyms.json", {})
SKILL_TAXONOMY = load_json_data("skill_taxonomy.json", {})
ACTION_VERBS = load_json_data("action_verbs.json", {})

# Build reverse lookup for synonyms (term -> canonical form and related terms)
SYNONYM_MAP = {}
RELATED_TERMS = {}

def build_synonym_maps():
    """Build synonym lookup maps from skill taxonomy."""
    global SYNONYM_MAP, RELATED_TERMS

    for category, skills in SKILL_TAXONOMY.items():
        for skill_name, skill_data in skills.items():
            # Normalize the skill name
            normalized = skill_name.lower().strip()

            # Add the skill itself
            if normalized not in SYNONYM_MAP:
                SYNONYM_MAP[normalized] = normalized

            # Add related terms as synonyms pointing to canonical form
            if isinstance(skill_data, dict) and 'related' in skill_data:
                related = skill_data['related']
                RELATED_TERMS[normalized] = [r.lower() for r in related]

                for related_term in related:
                    related_normalized = related_term.lower().strip()
                    if related_normalized not in SYNONYM_MAP:
                        SYNONYM_MAP[related_normalized] = normalized

# Initialize synonym maps
build_synonym_maps()

# Common words to ignore in keyword extraction
STOP_WORDS = {
    'a', 'an', 'the', 'and', 'or', 'but', 'in', 'on', 'at', 'to', 'for', 'of', 'with',
    'by', 'from', 'as', 'is', 'was', 'are', 'were', 'been', 'be', 'have', 'has', 'had',
    'do', 'does', 'did', 'will', 'would', 'could', 'should', 'may', 'might', 'must',
    'shall', 'can', 'need', 'dare', 'ought', 'used', 'it', 'its', 'this', 'that',
    'these', 'those', 'i', 'you', 'he', 'she', 'we', 'they', 'what', 'which', 'who',
    'whom', 'when', 'where', 'why', 'how', 'all', 'each', 'every', 'both', 'few',
    'more', 'most', 'other', 'some', 'such', 'no', 'nor', 'not', 'only', 'own',
    'same', 'so', 'than', 'too', 'very', 'just', 'also', 'now', 'here', 'there',
    'then', 'once', 'if', 'unless', 'until', 'while', 'during', 'before', 'after',
    'above', 'below', 'between', 'into', 'through', 'about', 'against', 'over',
    'under', 'again', 'further', 'any', 'our', 'your', 'their', 'his', 'her', 'my',
    'etc', 'eg', 'ie', 'via', 'per', 'vs', 'including', 'within', 'across', 'along',
    'among', 'around', 'behind', 'beyond', 'like', 'near', 'since', 'upon', 'based'
}

# JD boilerplate words — common in job descriptions but NOT meaningful skills.
# These are filtered out to prevent noise keywords like "job", "position", "team"
# from inflating ATS scores. Identified via O*NET crosswalk as non-skill terms.
JD_BOILERPLATE_WORDS = {
    'position', 'job', 'role', 'opportunity', 'candidate', 'applicant',
    'requirement', 'qualification', 'responsibility', 'duty', 'duties',
    'company', 'organization', 'employer', 'employee', 'team', 'department',
    'salary', 'compensation', 'benefits', 'package', 'competitive',
    'equal', 'eoe', 'diversity', 'inclusion',
    'preferred', 'required', 'desired', 'minimum', 'maximum',
    'experience', 'year', 'years', 'month', 'months',
    'able', 'ability', 'capable', 'proficient', 'excellent',
    'strong', 'proven', 'demonstrated', 'successful', 'effective',
    'work', 'working', 'environment', 'office', 'remote', 'hybrid',
    'full', 'time', 'part', 'contract', 'permanent', 'temporary',
    'apply', 'submit', 'resume', 'cover', 'letter', 'application',
    'please', 'note', 'must', 'shall', 'ensure', 'provide',
    'support', 'assist', 'help', 'maintain', 'manage', 'develop',
    'report', 'review', 'prepare', 'coordinate', 'oversee',
    'retail', 'location', 'travel', 'schedule', 'shift',
    'ideal', 'looking', 'seeking', 'join', 'growing',
    'dynamic', 'innovative', 'exciting', 'passionate', 'motivated',
    'self', 'starter', 'driven', 'oriented', 'focused',
    'detail', 'details', 'fast', 'paced', 'multi', 'task',
    'independently', 'level', 'senior', 'junior', 'entry', 'mid',
    'include', 'includes', 'including', 'involve', 'involves',
    'perform', 'performs', 'responsible', 'various', 'multiple',
}

# Merge boilerplate words into STOP_WORDS for unified filtering
STOP_WORDS = STOP_WORDS | JD_BOILERPLATE_WORDS

# =============================================================================
# DOMAIN KEYWORD LOADING — Dynamic from data/keywords_{domain}.json files
# Replaces hardcoded PV_KEYWORDS with domain-aware keyword sets
# =============================================================================

# Domain-to-filename mapping
_DOMAIN_KEYWORD_FILES = {
    'clinical_research': 'keywords_clinical.json',
    'pharma_biotech': 'keywords_clinical.json',
    'technology': 'keywords_technology.json',
    'finance': 'keywords_finance.json',
    'consulting': 'keywords_consulting.json',
    'healthcare': 'keywords_healthcare.json',
    'general': 'keywords_general.json',
}

# Cache for loaded domain keywords
_domain_keywords_cache = {}

def load_domain_keywords(domain='clinical_research'):
    """
    Load domain-specific keywords from JSON data files.

    Returns:
        dict: {term: weight} mapping for the specified domain
    """
    if domain in _domain_keywords_cache:
        return _domain_keywords_cache[domain]

    filename = _DOMAIN_KEYWORD_FILES.get(domain, 'keywords_general.json')
    raw_data = load_json_data(filename, {})

    # Flatten the categorized structure into {term: weight}
    keywords = {}
    for category_key, category_data in raw_data.items():
        if category_key == '_metadata':
            continue
        if isinstance(category_data, dict):
            for term, info in category_data.items():
                if isinstance(info, dict):
                    keywords[term.lower()] = info.get('weight', 1)
                elif isinstance(info, (int, float)):
                    keywords[term.lower()] = info

    _domain_keywords_cache[domain] = keywords
    return keywords


def load_domain_phrases(domain='clinical_research'):
    """
    Extract multi-word phrases from domain keyword JSON files.

    Returns:
        list: Multi-word phrases for the specified domain
    """
    keywords = load_domain_keywords(domain)
    return [term for term in keywords.keys() if ' ' in term]


def get_domain_keywords_for_text(text):
    """
    Auto-detect domain from text and return appropriate keyword set.

    Returns:
        tuple: (keywords_dict, phrases_list, detected_domain)
    """
    domain, _, _ = detect_domain(text)
    keywords = load_domain_keywords(domain)
    phrases = load_domain_phrases(domain)
    return keywords, phrases, domain


# Legacy aliases — PV_KEYWORDS and CLINICAL_PHRASES now load from clinical domain JSON
# These are loaded at module level for backward compatibility with code that references them
PV_KEYWORDS = load_domain_keywords('clinical_research')
CLINICAL_PHRASES = load_domain_phrases('clinical_research')


# ============== Enhanced NLP Functions ==============

def lemmatize_word(word):
    """Lemmatize a word to its base form using NLTK WordNet."""
    word = word.lower()
    if NLTK_AVAILABLE and _wnl:
        try:
            return _wnl.lemmatize(word)
        except LookupError:
            pass  # WordNet corpus not downloaded, fall through to suffix stripping
    # Fallback: simple suffix stripping for common patterns
    suffixes = ['ing', 'ed', 'er', 'est', 'ly', 'tion', 'ment', 's', 'es']
    for suffix in suffixes:
        if word.endswith(suffix) and len(word) > len(suffix) + 2:
            return word[:-len(suffix)]
    return word


def lemmatize_text(text):
    """Lemmatize all words in text, returning both original and lemmatized forms."""
    words = text.lower().split()
    lemmas = set()
    for w in words:
        if len(w) > 1:
            lemmas.add(w)
            lemma = lemmatize_word(w)
            if lemma != w:
                lemmas.add(lemma)
    return lemmas


def expand_acronyms(text):
    """
    Expand acronyms in text using the acronym dictionary.
    E.g., 'FDA' -> 'FDA food and drug administration'
    """
    expanded_text = text.lower()
    additions = []

    for acronym, expansion in ACRONYMS.items():
        # Check if acronym appears as a standalone word
        pattern = r'\b' + re.escape(acronym) + r'\b'
        if re.search(pattern, expanded_text, re.IGNORECASE):
            additions.append(expansion.lower())

    # Append expansions to the text
    if additions:
        expanded_text = expanded_text + ' ' + ' '.join(additions)

    return expanded_text


def get_canonical_term(term):
    """
    Get the canonical form of a term using synonym mapping.
    E.g., 'tensorflow' -> 'python', 'react' -> 'javascript'
    """
    term_lower = term.lower().strip()
    return SYNONYM_MAP.get(term_lower, term_lower)


def get_related_terms(term):
    """
    Get all related terms for a canonical term.
    E.g., 'python' -> ['pandas', 'numpy', 'scikit-learn', ...]
    """
    term_lower = term.lower().strip()
    canonical = get_canonical_term(term_lower)
    return RELATED_TERMS.get(canonical, [])


def match_with_synonyms(jd_terms, resume_terms):
    """
    Match JD terms against resume terms, considering synonyms and related terms.
    Returns matched terms, partial matches (via synonyms), and missing terms.
    """
    matched = set()
    partial_matched = set()  # Matched via synonym/related term
    missing = set()

    resume_canonical = {get_canonical_term(t) for t in resume_terms}
    resume_lower = {t.lower() for t in resume_terms}

    for jd_term in jd_terms:
        jd_lower = jd_term.lower()
        jd_canonical = get_canonical_term(jd_lower)

        # Direct match
        if jd_lower in resume_lower:
            matched.add(jd_term)
        # Canonical match (synonym resolution)
        elif jd_canonical in resume_canonical:
            partial_matched.add(jd_term)
        # Related term match
        elif any(related in resume_lower for related in get_related_terms(jd_canonical)):
            partial_matched.add(jd_term)
        else:
            missing.add(jd_term)

    return matched, partial_matched, missing


# =============================================================================
# §2.2.1 - SEMANTIC SIMILARITY WITH SENTENCE TRANSFORMERS
# =============================================================================

def calculate_semantic_similarity(resume_text: str, jd_text: str) -> Tuple[float, Dict[str, Any]]:
    """
    Calculate semantic similarity using Sentence Transformers (§2.2.1).

    Uses all-MiniLM-L6-v2 model to create 384-dimensional embeddings and
    calculates cosine similarity between resume and JD.

    Returns:
        score: Semantic similarity score (0-100)
        details: Dictionary with embedding details
    """
    if not SBERT_AVAILABLE:
        return 0, {'available': False, 'message': _sbert_load_error or 'sentence-transformers not installed'}

    try:
        resume_embedding = embed_with_cache(resume_text)
        jd_embedding = embed_with_cache(jd_text)
        if resume_embedding is None or jd_embedding is None:
            return 0, {'available': False, 'message': _sbert_load_error or 'Model not loaded'}

        import torch
        resume_tensor = torch.tensor(resume_embedding)
        jd_tensor = torch.tensor(jd_embedding)
        cosine_sim = sbert_util.cos_sim(resume_tensor, jd_tensor).item()

        score = max(0, min(100, cosine_sim * 100))
        details = {
            'available': True,
            'cosine_similarity': round(cosine_sim, 4),
            'interpretation': 'Strong Match' if cosine_sim > 0.7 else 'Relevant' if cosine_sim > 0.5 else 'Weak Match',
            'cached': True
        }
        return score, details
    except Exception as e:
        return 0, {'available': False, 'error': str(e)}


# =============================================================================
# §8.1.1 - BM25 PROBABILISTIC SCORING
# =============================================================================

def calculate_bm25_score(resume_text: str, jd_text: str, k1: float = 1.5, b: float = 0.75) -> Tuple[float, Dict[str, Any]]:
    """
    Calculate BM25 score using rank_bm25 BM25Plus (§8.1.1).

    Treats each resume line/bullet as a document in a micro-corpus,
    then queries with JD keywords. BM25Plus reduces short-document bias.
    """
    try:
        from rank_bm25 import BM25Plus
    except ImportError:
        # Fallback to simple term overlap if rank_bm25 not installed
        return _calculate_bm25_fallback(resume_text, jd_text)

    # Build corpus from resume segments (each line is a "document")
    segments = [seg.strip() for seg in resume_text.split('\n') if seg.strip() and len(seg.strip()) > 10]
    if not segments:
        return 0, {'error': 'No resume segments found'}

    # Tokenize
    def tokenize(text):
        tokens = clean_text(text).split()
        return [t for t in tokens if t not in STOP_WORDS and len(t) > 2]

    tokenized_corpus = [tokenize(seg) for seg in segments]
    # Filter out empty tokenized segments
    tokenized_corpus = [t for t in tokenized_corpus if t]

    if not tokenized_corpus:
        return 0, {'error': 'No valid tokens in resume'}

    # Build BM25Plus index
    bm25 = BM25Plus(tokenized_corpus)

    # Query with JD tokens
    query_tokens = tokenize(jd_text)
    if not query_tokens:
        return 0, {'error': 'No valid tokens in JD'}

    scores = bm25.get_scores(query_tokens)

    # Aggregate: use mean of top-K segment scores (captures best-matching sections)
    top_k = min(10, len(scores))
    sorted_scores = sorted(scores, reverse=True)
    top_score = sum(sorted_scores[:top_k]) / top_k if top_k > 0 else 0

    # Normalize to 0-100
    # BM25 scores vary widely; use sigmoid-like normalization
    normalized = min(100, (top_score / (top_score + 5)) * 100) if top_score > 0 else 0

    details = {
        'method': 'BM25Plus (rank_bm25)',
        'corpus_segments': len(tokenized_corpus),
        'query_tokens': len(query_tokens),
        'max_segment_score': round(float(max(scores)), 2) if len(scores) > 0 else 0,
        'mean_top_k_score': round(float(top_score), 2),
        'k1': k1,
        'b': b
    }

    return round(normalized, 1), details


def _calculate_bm25_fallback(resume_text: str, jd_text: str) -> Tuple[float, Dict[str, Any]]:
    """Simple term-overlap fallback when rank_bm25 is not installed."""
    resume_tokens = set(clean_text(resume_text).split())
    jd_tokens = set(clean_text(jd_text).split())

    resume_tokens = {t for t in resume_tokens if t not in STOP_WORDS and len(t) > 2}
    jd_tokens = {t for t in jd_tokens if t not in STOP_WORDS and len(t) > 2}

    if not jd_tokens:
        return 0, {'error': 'No valid JD tokens', 'method': 'fallback'}

    overlap = resume_tokens & jd_tokens
    score = (len(overlap) / len(jd_tokens)) * 100

    return round(min(100, score), 1), {
        'method': 'term_overlap_fallback',
        'matched_terms': len(overlap),
        'total_jd_terms': len(jd_tokens)
    }


# =============================================================================
# §2.1.1, §7.2 - FORMAT RISK ASSESSMENT (Workday/Taleo Simulation)
# =============================================================================

def assess_format_risk(file_path: str) -> Tuple[int, List[str], Dict[str, Any]]:
    """
    Assess format risk for ATS parsing (§2.1.1, §7.2).

    Detects formatting elements that cause parsing failures in Workday, Taleo, iCIMS:
    - Tables (merge columns, scramble data)
    - Text boxes (often ignored entirely)
    - Multi-column layouts (linear parsers merge lines)
    - Headers/Footers (content often stripped)

    Returns:
        risk_score: 0-100 (higher = more risky)
        warnings: List of specific warnings
        details: Dictionary with detection details
    """
    warnings = []
    risk_score = 0
    details = {
        'tables_detected': 0,
        'text_boxes_detected': 0,
        'columns_detected': False,
        'header_content': False,
        'footer_content': False,
        'graphics_detected': 0
    }

    ext = os.path.splitext(file_path)[1].lower() if file_path else ''

    if ext == '.pdf':
        try:
            with pdfplumber.open(file_path) as pdf:
                for page_num, page in enumerate(pdf.pages):
                    # Check for tables
                    tables = page.find_tables()
                    if tables:
                        details['tables_detected'] += len(tables)
                        risk_score += 20 * len(tables)
                        warnings.append(f"Page {page_num + 1}: {len(tables)} table(s) detected - Workday may scramble content")

                    # Check for images/graphics
                    if hasattr(page, 'images') and page.images:
                        details['graphics_detected'] += len(page.images)
                        risk_score += 5 * len(page.images)

                    # Check header/footer regions (top/bottom 10%)
                    page_height = page.height
                    text_objects = page.chars if hasattr(page, 'chars') else []

                    header_chars = [c for c in text_objects if c.get('top', 0) < page_height * 0.1]
                    footer_chars = [c for c in text_objects if c.get('top', 0) > page_height * 0.9]

                    if header_chars:
                        details['header_content'] = True
                    if footer_chars:
                        details['footer_content'] = True

        except Exception as e:
            warnings.append(f"Could not analyze PDF structure: {str(e)}")

    elif ext == '.docx':
        try:
            import zipfile
            import xml.etree.ElementTree as ET

            with zipfile.ZipFile(file_path) as docx:
                # Read document.xml
                if 'word/document.xml' in docx.namelist():
                    xml_content = docx.read('word/document.xml')
                    root = ET.fromstring(xml_content)

                    # Define namespace
                    ns = {'w': 'http://schemas.openxmlformats.org/wordprocessingml/2006/main'}

                    # Count tables
                    tables = root.findall('.//w:tbl', ns)
                    details['tables_detected'] = len(tables)
                    if tables:
                        risk_score += 20 * len(tables)
                        warnings.append(f"{len(tables)} table(s) detected - Workday/Taleo may parse incorrectly")

                    # Count text boxes
                    textboxes = root.findall('.//w:txbxContent', ns)
                    details['text_boxes_detected'] = len(textboxes)
                    if textboxes:
                        risk_score += 30 * len(textboxes)
                        warnings.append(f"{len(textboxes)} text box(es) detected - Often IGNORED by ATS parsers")

                # Check for headers/footers
                if 'word/header1.xml' in docx.namelist():
                    details['header_content'] = True
                if 'word/footer1.xml' in docx.namelist():
                    details['footer_content'] = True

        except Exception as e:
            warnings.append(f"Could not analyze DOCX structure: {str(e)}")

    # Add warnings for header/footer content
    if details['header_content']:
        risk_score += 15
        warnings.append("Content in HEADER detected - May be stripped by Taleo/iCIMS")

    if details['footer_content']:
        risk_score += 10
        warnings.append("Content in FOOTER detected - May be stripped by ATS")

    risk_score = min(100, risk_score)

    return risk_score, warnings, details


# =============================================================================
# §2.3.2 - KEYWORD STUFFING DETECTION
# =============================================================================

def detect_keyword_stuffing(text: str) -> Tuple[bool, float, Dict[str, Any]]:
    """
    Detect keyword stuffing and manipulation (§2.3.2).

    Checks for:
    - Abnormal keyword density (>3 standard deviations)
    - White/hidden text patterns
    - Excessive repetition of specific terms
    - Unusually high skill-to-content ratio

    Returns:
        is_stuffed: Boolean indicating suspected stuffing
        manipulation_score: 0-100 (higher = more suspicious)
        details: Dictionary with detection details
    """
    cleaned = clean_text(text)
    words = cleaned.split()
    word_count = len(words)

    if word_count < 50:
        return False, 0, {'error': 'Text too short for analysis'}

    # Calculate word frequencies
    word_freq = Counter(words)

    # Filter to meaningful words (not stop words)
    meaningful_words = {w: c for w, c in word_freq.items()
                       if w not in STOP_WORDS and len(w) > 2}

    if not meaningful_words:
        return False, 0, {'error': 'No meaningful words found'}

    # Calculate statistics
    frequencies = list(meaningful_words.values())
    mean_freq = sum(frequencies) / len(frequencies)
    variance = sum((f - mean_freq) ** 2 for f in frequencies) / len(frequencies)
    std_dev = math.sqrt(variance) if variance > 0 else 1

    # Find outliers (>3 standard deviations)
    outliers = {w: c for w, c in meaningful_words.items()
               if c > mean_freq + 3 * std_dev}

    # Calculate manipulation indicators
    manipulation_score = 0
    flags = []

    # Check for excessive repetition
    if outliers:
        manipulation_score += 30
        flags.append(f"Excessive repetition of: {list(outliers.keys())[:5]}")

    # Check skill keyword density
    skill_keywords = set(SYNONYM_MAP.keys())
    resume_skills = [w for w in words if w in skill_keywords]
    skill_density = len(resume_skills) / word_count if word_count > 0 else 0

    if skill_density > 0.15:  # More than 15% skills
        manipulation_score += 20
        flags.append(f"High skill density: {skill_density*100:.1f}%")

    # Check for consecutive repeated words
    for i in range(len(words) - 2):
        if words[i] == words[i+1] == words[i+2]:
            manipulation_score += 15
            flags.append(f"Triple repetition found: '{words[i]}'")
            break

    # Check for list-heavy content (skills dumping)
    bullet_count = text.count('•') + text.count('-') + text.count('*')
    bullet_ratio = bullet_count / (word_count / 10) if word_count > 0 else 0
    if bullet_ratio > 5:  # More than 5 bullets per 10 words
        manipulation_score += 15
        flags.append("Excessive bullet points (possible skill dumping)")

    manipulation_score = min(100, manipulation_score)
    is_stuffed = manipulation_score > 40

    details = {
        'word_count': word_count,
        'unique_words': len(meaningful_words),
        'mean_frequency': round(mean_freq, 2),
        'std_deviation': round(std_dev, 2),
        'outlier_words': list(outliers.keys())[:10],
        'skill_density': round(skill_density * 100, 1),
        'flags': flags
    }

    return is_stuffed, manipulation_score, details


# =============================================================================
# §3.1.2 - READABILITY METRICS (Flesch-Kincaid + Dale-Chall for technical)
# =============================================================================

# Technical domains where Dale-Chall is preferred over Flesch-Kincaid.
# FK penalizes legitimate domain jargon ("pharmacokinetics", "heteroscedasticity")
# while Dale-Chall uses a familiar-word list that better handles technical writing.
_DALE_CHALL_DOMAINS = {'clinical_research', 'pharma_biotech', 'technology'}


def _calculate_dale_chall_score(dc_score: float) -> float:
    """Convert Dale-Chall readability score to 0-100 (optimal at 7.0-8.0 for technical docs)."""
    if 7.0 <= dc_score <= 8.0:
        return 100  # Optimal for technical professional docs
    elif 6.0 <= dc_score < 7.0:
        return 80 + (dc_score - 6.0) * 20  # 80-100
    elif 8.0 < dc_score <= 9.0:
        return 100 - (dc_score - 8.0) * 20  # 100-80
    elif dc_score < 6.0:
        return max(40, 80 - (6.0 - dc_score) * 15)  # Too simple
    else:  # > 9.0
        return max(30, 80 - (dc_score - 9.0) * 15)  # Too complex


def calculate_readability(text: str, domain: str = None) -> Tuple[float, str, Dict[str, Any]]:
    """
    Calculate readability metrics (§3.1.2).

    Uses Dale-Chall for technical domains (clinical_research, pharma_biotech, technology)
    to avoid penalizing legitimate domain jargon. Uses Flesch-Kincaid for other domains.

    Dale-Chall target: 7.0-8.0 (technical professional level)
    Flesch-Kincaid target: Grade 10-12 (business professional level)

    Args:
        text: Text to analyze
        domain: Optional domain hint. If provided and technical, uses Dale-Chall.

    Returns:
        score: 0-100 (100 = optimal readability)
        grade_level: String description
        details: Dictionary with all metrics
    """
    use_dale_chall = domain in _DALE_CHALL_DOMAINS if domain else False

    if not TEXTSTAT_AVAILABLE:
        # Fallback: simple readability estimation
        words = text.split()
        sentences = text.count('.') + text.count('!') + text.count('?')
        sentences = max(1, sentences)

        avg_words_per_sentence = len(words) / sentences
        avg_word_length = sum(len(w) for w in words) / len(words) if words else 0

        # Simple grade estimate
        estimated_grade = (avg_words_per_sentence * 0.39) + (avg_word_length * 11.8) - 15.59
        estimated_grade = max(1, min(20, estimated_grade))

        return calculate_readability_score(estimated_grade), f"Grade {estimated_grade:.1f}", {
            'available': False,
            'estimated_grade': round(estimated_grade, 1),
            'avg_sentence_length': round(avg_words_per_sentence, 1)
        }

    try:
        # Always compute both for the details dict
        fk_grade = textstat.flesch_kincaid_grade(text)
        flesch_ease = textstat.flesch_reading_ease(text)
        gunning_fog = textstat.gunning_fog(text)
        smog = textstat.smog_index(text)

        if use_dale_chall:
            # Dale-Chall for technical domains — doesn't penalize domain jargon
            dc_score = textstat.dale_chall_readability_score(text)
            score = _calculate_dale_chall_score(dc_score)

            if dc_score < 6.0:
                grade_desc = f"Dale-Chall {dc_score:.1f} (Too Simple)"
            elif dc_score <= 8.0:
                grade_desc = f"Dale-Chall {dc_score:.1f} (Optimal - Technical Professional)"
            elif dc_score <= 9.0:
                grade_desc = f"Dale-Chall {dc_score:.1f} (Slightly Complex)"
            else:
                grade_desc = f"Dale-Chall {dc_score:.1f} (Too Complex)"

            details = {
                'available': True,
                'method': 'dale_chall',
                'domain': domain,
                'dale_chall_score': round(dc_score, 1),
                'flesch_kincaid_grade': round(fk_grade, 1),
                'flesch_reading_ease': round(flesch_ease, 1),
                'gunning_fog_index': round(gunning_fog, 1),
                'smog_index': round(smog, 1),
                'optimal_range': '7.0-8.0 (Dale-Chall)'
            }
        else:
            # Flesch-Kincaid for non-technical domains
            score = calculate_readability_score(fk_grade)

            if fk_grade < 8:
                grade_desc = f"Grade {fk_grade:.1f} (Too Simple)"
            elif fk_grade <= 12:
                grade_desc = f"Grade {fk_grade:.1f} (Optimal - Business Professional)"
            elif fk_grade <= 14:
                grade_desc = f"Grade {fk_grade:.1f} (Slightly Complex)"
            else:
                grade_desc = f"Grade {fk_grade:.1f} (Too Complex - Academic)"

            details = {
                'available': True,
                'method': 'flesch_kincaid',
                'domain': domain,
                'flesch_kincaid_grade': round(fk_grade, 1),
                'flesch_reading_ease': round(flesch_ease, 1),
                'gunning_fog_index': round(gunning_fog, 1),
                'smog_index': round(smog, 1),
                'optimal_range': '10-12 (FK Grade)'
            }

        return score, grade_desc, details

    except Exception as e:
        return 50, "Unknown", {'error': str(e)}


def calculate_readability_score(grade_level: float) -> float:
    """Convert grade level to 0-100 score (optimal at 10-12)."""
    if 10 <= grade_level <= 12:
        return 100  # Optimal
    elif 8 <= grade_level < 10:
        return 80 + (grade_level - 8) * 10  # 80-100
    elif 12 < grade_level <= 14:
        return 100 - (grade_level - 12) * 15  # 100-70
    elif grade_level < 8:
        return max(40, 80 - (8 - grade_level) * 10)  # Penalty for too simple
    else:  # > 14
        return max(30, 70 - (grade_level - 14) * 10)  # Penalty for too complex


# =============================================================================
# §4 - DOMAIN AUTO-DETECTION
# =============================================================================

# Domain keyword patterns
DOMAIN_PATTERNS = {
    'clinical_research': {
        'keywords': ['clinical trial', 'fda', 'gcp', 'irb', 'protocol', 'pharmacovigilance',
                    'adverse event', 'patient safety', 'regulatory', 'cro', 'phase i',
                    'phase ii', 'phase iii', 'investigator', 'medical monitor', 'ehr', 'emr',
                    'epic', 'cerner', 'hipaa', 'clinical documentation'],
        'weight': 1.0
    },
    'pharma_biotech': {
        'keywords': ['drug development', 'biologics', 'oncology', 'immunology', 'therapeutic',
                    'pipeline', 'r&d', 'preclinical', 'clinical development', 'nda', 'bla',
                    'mechanism of action', 'biomarker', 'genomics', 'cell therapy'],
        'weight': 1.0
    },
    'technology': {
        'keywords': ['software', 'engineer', 'developer', 'python', 'java', 'javascript',
                    'cloud', 'aws', 'azure', 'kubernetes', 'docker', 'api', 'microservices',
                    'agile', 'scrum', 'devops', 'machine learning', 'data science'],
        'weight': 1.0
    },
    'finance': {
        'keywords': ['investment banking', 'private equity', 'hedge fund', 'm&a', 'ipo',
                    'valuation', 'dcf', 'lbo', 'financial modeling', 'bloomberg', 'trading',
                    'portfolio', 'derivatives', 'fixed income', 'equity research'],
        'weight': 1.0
    },
    'consulting': {
        'keywords': ['consulting', 'strategy', 'mckinsey', 'bain', 'bcg', 'deloitte',
                    'client engagement', 'stakeholder', 'business transformation',
                    'change management', 'due diligence', 'market analysis'],
        'weight': 1.0
    },
    'healthcare': {
        'keywords': ['hospital', 'patient care', 'nursing', 'physician', 'medical director',
                    'health system', 'ambulatory', 'inpatient', 'outpatient', 'jcaho',
                    'quality improvement', 'care coordination', 'population health'],
        'weight': 1.0
    }
}


# Domain prototype texts for embedding-based classification
DOMAIN_PROTOTYPES = {
    'clinical_research': [
        "Phase I-III clinical trial design, protocol development, study execution, and regulatory submissions",
        "Investigational new drug (IND) applications, pivotal studies, and medical monitoring",
        "Good Clinical Practice (GCP), IRB oversight, informed consent, and clinical data management",
        "Clinical research associate, clinical operations, site management, patient enrollment",
        "Adverse event reporting, safety monitoring, pharmacovigilance, and DSMB oversight",
    ],
    'pharma_biotech': [
        "Drug development pipeline, biologics, small molecules, and mechanism of action research",
        "Preclinical development, biomarker discovery, genomics, and translational medicine",
        "R&D portfolio strategy, therapeutic area leadership, and clinical development planning",
        "Cell and gene therapy, immunotherapy, monoclonal antibodies, and biosimilars",
        "New drug application (NDA), biologics license application (BLA), and regulatory strategy",
    ],
    'technology': [
        "Software engineering, full-stack development, cloud architecture, and microservices",
        "Machine learning, data science, AI/ML pipelines, and model deployment",
        "DevOps, CI/CD, Kubernetes, Docker, and infrastructure as code",
        "Product management, agile methodology, sprint planning, and technical roadmap",
        "API design, system architecture, scalability, and performance optimization",
    ],
    'finance': [
        "Investment banking, M&A advisory, leveraged buyout, and financial modeling",
        "Private equity, venture capital, portfolio management, and due diligence",
        "DCF valuation, comparable company analysis, pitch books, and deal execution",
        "Risk management, derivatives trading, fixed income, and equity research",
        "Financial planning and analysis, budgeting, forecasting, and P&L management",
    ],
    'consulting': [
        "Management consulting, strategy development, client engagement, and business transformation",
        "Market analysis, competitive landscape, growth strategy, and organizational design",
        "Change management, process improvement, stakeholder alignment, and implementation planning",
        "Due diligence, operational assessment, cost optimization, and digital transformation",
        "Presentation development, executive communication, and workshop facilitation",
    ],
    'healthcare': [
        "Hospital administration, patient care operations, and health system management",
        "Quality improvement, JCAHO accreditation, patient safety, and care coordination",
        "Electronic health records, Epic implementation, clinical documentation, and health informatics",
        "Population health management, value-based care, and ambulatory care operations",
        "Nursing leadership, physician practice management, and clinical workflow optimization",
    ]
}

# Pre-computed prototype embeddings (computed lazily on first use)
_domain_proto_embeddings = None
_domain_proto_lock = threading.Lock()

def _get_domain_proto_embeddings():
    """Lazily compute and cache domain prototype embeddings."""
    global _domain_proto_embeddings
    if _domain_proto_embeddings is None:
        with _domain_proto_lock:
            if _domain_proto_embeddings is None and SBERT_AVAILABLE:
                model = get_sbert_model()
                if model is not None:
                    embeds = {}
                    for domain, texts in DOMAIN_PROTOTYPES.items():
                        emb = model.encode(texts, convert_to_numpy=True)
                        # Normalize for cosine similarity via dot product
                        norms = np.linalg.norm(emb, axis=1, keepdims=True)
                        embeds[domain] = emb / norms
                    _domain_proto_embeddings = embeds
    return _domain_proto_embeddings


def detect_domain(text: str) -> Tuple[str, float, Dict[str, float]]:
    """
    Auto-detect the industry domain from text (§4).

    Uses embedding-based prototype classification when SBERT is available,
    with keyword counting as fallback.

    Returns:
        primary_domain: Most likely domain
        confidence: 0-100 confidence score
        domain_scores: Dictionary of all domain scores
    """
    # Try embedding-based classification first
    if SBERT_AVAILABLE:
        proto_embeds = _get_domain_proto_embeddings()
        if proto_embeds is not None:
            try:
                model = get_sbert_model()
                jd_vec = model.encode(text, convert_to_numpy=True)
                jd_vec = jd_vec / np.linalg.norm(jd_vec)

                domain_scores = {}
                for domain, embs in proto_embeds.items():
                    # Average cosine similarity to all prototypes in this domain
                    sims = embs @ jd_vec
                    domain_scores[domain] = round(float(np.mean(sims)) * 100, 1)

                primary_domain = max(domain_scores, key=domain_scores.get)
                confidence = domain_scores[primary_domain]

                return primary_domain, confidence, domain_scores
            except Exception:
                pass  # Fall through to keyword-based detection

    # Fallback: keyword-based detection (original logic)
    text_lower = text.lower()
    domain_scores = {}

    for domain, config in DOMAIN_PATTERNS.items():
        score = 0
        for keyword in config['keywords']:
            if keyword in text_lower:
                score += config['weight']
        domain_scores[domain] = round((score / len(config['keywords'])) * 100, 1)

    if domain_scores:
        primary_domain = max(domain_scores, key=domain_scores.get)
        confidence = domain_scores[primary_domain]
    else:
        primary_domain = 'general'
        confidence = 0

    return primary_domain, confidence, domain_scores


# =============================================================================
# §2.3.1 - EXPERIENCE DECAY FUNCTION
# =============================================================================

def calculate_skill_decay(skill: str, years_since_use: float, domain: str = None) -> float:
    """
    Calculate skill relevance with exponential decay (§2.3.1).

    Formula: R(s,t) = W_base * e^(-λ * Δt)

    Where:
    - R(s,t) is the relevance score of skill s at current time t
    - W_base is the initial weight (1.0 for matched skills)
    - λ (lambda) is the decay constant (varies by skill type)
    - Δt is the time elapsed in years since skill was last used

    Decay constants:
    - High λ (0.2): Volatile skills (JS frameworks, marketing tools)
    - Medium λ (0.1): Standard tech skills (Python, Java)
    - Low λ (0.05): Durable skills (SQL, clinical trials, leadership)
    - Very Low λ (0.01-0.03): Evergreen skills (communication, strategic planning)

    Args:
        skill: The skill name
        years_since_use: Years since the skill was actively used
        domain: Optional domain context for better lambda selection

    Returns:
        Relevance score between 0 and 1
    """
    skill_lower = skill.lower().strip()

    # Get decay lambda from skill taxonomy
    decay_lambda = 0.1  # Default medium decay

    for category, skills in SKILL_TAXONOMY.items():
        for skill_name, skill_data in skills.items():
            if skill_lower == skill_name.lower():
                if isinstance(skill_data, dict) and 'decay_lambda' in skill_data:
                    decay_lambda = skill_data['decay_lambda']
                    break
            # Check related terms
            if isinstance(skill_data, dict) and 'related' in skill_data:
                for related in skill_data['related']:
                    if skill_lower == related.lower():
                        decay_lambda = skill_data.get('decay_lambda', 0.1)
                        break

    # Calculate decay
    relevance = math.exp(-decay_lambda * years_since_use)

    return round(relevance, 3)


def extract_skills_with_recency(resume_text: str) -> Dict[str, Dict[str, Any]]:
    """
    Extract skills from resume with recency information.

    Attempts to determine when skills were last used based on:
    - Position in resume (recent jobs first)
    - Date patterns near skill mentions
    - Section context (current role vs. older experience)

    Returns:
        Dictionary mapping skill -> {recency_years, context, decay_score}
    """
    import datetime
    current_year = datetime.datetime.now().year

    skills_with_recency = {}
    lines = resume_text.split('\n')

    # Date patterns to detect years
    year_pattern = re.compile(r'\b(20\d{2}|19\d{2})\b')
    present_pattern = re.compile(r'\b(present|current|ongoing|now)\b', re.IGNORECASE)

    current_year_context = current_year

    for i, line in enumerate(lines):
        # Update year context from date ranges
        years_found = year_pattern.findall(line)
        if years_found:
            current_year_context = max(int(y) for y in years_found)

        # Check for "Present" indicating current role
        if present_pattern.search(line):
            current_year_context = current_year

        # Extract skills from this line
        line_lower = line.lower()

        # Check against skill taxonomy
        for category, skills in SKILL_TAXONOMY.items():
            for skill_name, skill_data in skills.items():
                skill_lower = skill_name.lower()

                if skill_lower in line_lower:
                    years_since = current_year - current_year_context
                    years_since = max(0, years_since)  # Can't be negative

                    if skill_lower not in skills_with_recency or \
                       skills_with_recency[skill_lower]['recency_years'] > years_since:
                        decay_score = calculate_skill_decay(skill_name, years_since)
                        skills_with_recency[skill_lower] = {
                            'recency_years': years_since,
                            'context_year': current_year_context,
                            'decay_score': decay_score,
                            'line_number': i + 1
                        }

                # Also check related terms
                if isinstance(skill_data, dict) and 'related' in skill_data:
                    for related in skill_data['related']:
                        related_lower = related.lower()
                        if related_lower in line_lower and related_lower not in skills_with_recency:
                            years_since = current_year - current_year_context
                            years_since = max(0, years_since)
                            decay_score = calculate_skill_decay(related, years_since)
                            skills_with_recency[related_lower] = {
                                'recency_years': years_since,
                                'context_year': current_year_context,
                                'decay_score': decay_score,
                                'line_number': i + 1
                            }

    return skills_with_recency


def calculate_recency_adjusted_score(
    matched_skills: List[str],
    resume_text: str,
    base_score: float
) -> Tuple[float, Dict[str, Any]]:
    """
    Adjust skill match score based on recency of skill usage.

    Skills used recently get full weight; older skills are decayed.

    Returns:
        adjusted_score: Recency-adjusted score
        details: Dictionary with skill recency information
    """
    skills_with_recency = extract_skills_with_recency(resume_text)

    if not matched_skills:
        return base_score, {'no_skills': True}

    total_weight = 0
    total_decay_adjusted = 0
    skill_details = []

    for skill in matched_skills:
        skill_lower = skill.lower()

        if skill_lower in skills_with_recency:
            recency_info = skills_with_recency[skill_lower]
            decay_score = recency_info['decay_score']
            total_weight += 1
            total_decay_adjusted += decay_score

            skill_details.append({
                'skill': skill,
                'years_ago': recency_info['recency_years'],
                'decay_multiplier': decay_score
            })
        else:
            # Skill found but no recency info - assume moderate recency
            total_weight += 1
            total_decay_adjusted += 0.8  # Default 80% weight
            skill_details.append({
                'skill': skill,
                'years_ago': 'unknown',
                'decay_multiplier': 0.8
            })

    # Calculate average decay factor
    avg_decay = total_decay_adjusted / total_weight if total_weight > 0 else 1.0

    # Adjust base score (decay factor between 0.7 and 1.0 to not penalize too harshly)
    adjustment_factor = 0.7 + (avg_decay * 0.3)
    adjusted_score = base_score * adjustment_factor

    details = {
        'average_decay_factor': round(avg_decay, 3),
        'adjustment_factor': round(adjustment_factor, 3),
        'skills_analyzed': len(skill_details),
        'skill_recency': skill_details[:10]  # Top 10 for brevity
    }

    return round(adjusted_score, 1), details


# =============================================================================
# §2.3.2 ENHANCED - HIDDEN TEXT DETECTION
# =============================================================================

def detect_hidden_text(file_path: str) -> Tuple[bool, List[str], Dict[str, Any]]:
    """
    Detect hidden/invisible text manipulation (§2.3.2).

    Checks for:
    - White text on white background (white fonting)
    - Very small font sizes (<4pt)
    - Text outside visible margins
    - Invisible characters or zero-width spaces

    Args:
        file_path: Path to resume file (PDF or DOCX)

    Returns:
        is_manipulated: Boolean indicating manipulation detected
        warnings: List of warning messages
        details: Dictionary with detection details
    """
    warnings = []
    details = {
        'white_text_detected': False,
        'tiny_text_detected': False,
        'hidden_chars_detected': False,
        'suspicious_patterns': []
    }

    if not file_path or not os.path.exists(file_path):
        return False, [], {'error': 'File not found'}

    # Check for DOCX files
    if file_path.endswith('.docx'):
        try:
            from zipfile import ZipFile
            import xml.etree.ElementTree as ET

            with ZipFile(file_path, 'r') as docx:
                # Read document.xml
                if 'word/document.xml' in docx.namelist():
                    xml_content = docx.read('word/document.xml').decode('utf-8')

                    # Check for white color (FFFFFF) text
                    if 'w:color w:val="FFFFFF"' in xml_content or \
                       'w:color w:val="ffffff"' in xml_content:
                        details['white_text_detected'] = True
                        warnings.append("WHITE TEXT DETECTED: Possible keyword stuffing with invisible text")

                    # Check for very small font sizes
                    root = ET.fromstring(xml_content)
                    ns = {'w': 'http://schemas.openxmlformats.org/wordprocessingml/2006/main'}

                    for sz in root.findall('.//w:sz', ns):
                        size = sz.get('{http://schemas.openxmlformats.org/wordprocessingml/2006/main}val')
                        if size and int(size) < 8:  # Half-points, so 8 = 4pt
                            details['tiny_text_detected'] = True
                            warnings.append(f"TINY TEXT DETECTED: Font size {int(size)/2}pt found")
                            break

        except Exception as e:
            details['docx_error'] = str(e)

    # Check for PDF files
    elif file_path.endswith('.pdf'):
        try:
            with pdfplumber.open(file_path) as pdf:
                for page_num, page in enumerate(pdf.pages[:3]):  # Check first 3 pages
                    if page.chars:
                        for char in page.chars[:500]:  # Sample characters
                            # Check for white/near-white text
                            if 'non_stroking_color' in char:
                                color = char['non_stroking_color']
                                if isinstance(color, tuple) and len(color) >= 3:
                                    # RGB values close to white
                                    if all(c > 0.95 for c in color[:3]):
                                        details['white_text_detected'] = True
                                        warnings.append("WHITE TEXT DETECTED in PDF")
                                        break

                            # Check for tiny text
                            if 'size' in char and char['size'] < 4:
                                details['tiny_text_detected'] = True
                                warnings.append(f"TINY TEXT ({char['size']}pt) detected in PDF")
                                break

        except Exception as e:
            details['pdf_error'] = str(e)

    # Check text content for hidden characters
    try:
        if file_path.endswith('.pdf'):
            text = extract_text_from_pdf(file_path)
        elif file_path.endswith('.docx'):
            try:
                from docx import Document
                doc = Document(file_path)
                text = '\n'.join([p.text for p in doc.paragraphs])
            except:
                text = ""
        else:
            with open(file_path, 'r', encoding='utf-8') as f:
                text = f.read()

        # Check for zero-width characters
        zero_width_chars = ['\u200b', '\u200c', '\u200d', '\ufeff', '\u2060']
        for zwc in zero_width_chars:
            if zwc in text:
                details['hidden_chars_detected'] = True
                warnings.append("ZERO-WIDTH CHARACTERS detected (possible manipulation)")
                break

        # Check for excessive repeated keywords in suspicious patterns
        words = text.lower().split()
        word_counts = Counter(words)
        for word, count in word_counts.most_common(20):
            if len(word) > 3 and count > 15:
                details['suspicious_patterns'].append(f"'{word}' appears {count} times")

    except Exception as e:
        details['text_analysis_error'] = str(e)

    is_manipulated = details['white_text_detected'] or \
                     details['tiny_text_detected'] or \
                     details['hidden_chars_detected']

    return is_manipulated, warnings, details


# =============================================================================
# §2.2.2, §8.2 - NETWORKX SKILL GRAPH WITH INFERENCE
# =============================================================================

# Try to import NetworkX for graph-based skill inference
try:
    import networkx as nx
    NETWORKX_AVAILABLE = True
except ImportError:
    NETWORKX_AVAILABLE = False
    nx = None


def build_skill_graph() -> 'nx.Graph':
    """
    Build a skill knowledge graph from the taxonomy (§2.2.2, §8.2).

    Creates nodes for skills and edges for relationships.
    Enables inference of related skills.

    Returns:
        NetworkX Graph object
    """
    if not NETWORKX_AVAILABLE:
        return None

    G = nx.Graph()

    # Add nodes and edges from skill taxonomy
    for category, skills in SKILL_TAXONOMY.items():
        # Add category as a node
        G.add_node(category, node_type='category')

        for skill_name, skill_data in skills.items():
            # Add skill node
            skill_lower = skill_name.lower()
            G.add_node(skill_lower, node_type='skill', category=category)

            # Connect to category
            G.add_edge(category, skill_lower, relationship='contains')

            # Add related terms as nodes and connect them
            if isinstance(skill_data, dict) and 'related' in skill_data:
                for related in skill_data['related']:
                    related_lower = related.lower()
                    G.add_node(related_lower, node_type='related_skill', parent=skill_lower)
                    G.add_edge(skill_lower, related_lower, relationship='related')

    return G


# Build the skill graph at module load
SKILL_GRAPH = build_skill_graph() if NETWORKX_AVAILABLE else None


def infer_skills_from_graph(
    detected_skills: List[str],
    threshold: float = 0.5
) -> Tuple[List[str], Dict[str, Any]]:
    """
    Infer additional skills using graph-based analysis (§8.2).

    If a candidate lists skills that are neighbors of a skill they
    didn't explicitly mention, infer high probability of that skill.

    Example: If resume has "Pandas", "NumPy", "Scikit-Learn" but not "Python",
    infer Python proficiency.

    Args:
        detected_skills: List of skills found in resume
        threshold: Minimum proportion of neighbors needed to infer (0-1)

    Returns:
        inferred_skills: List of inferred skills
        details: Inference reasoning
    """
    if not NETWORKX_AVAILABLE or SKILL_GRAPH is None:
        return [], {'error': 'NetworkX not available'}

    detected_lower = set(s.lower() for s in detected_skills)
    inferred_skills = []
    inference_details = []

    # Get all skill nodes
    skill_nodes = [n for n, d in SKILL_GRAPH.nodes(data=True)
                   if d.get('node_type') == 'skill']

    for skill in skill_nodes:
        if skill in detected_lower:
            continue  # Already detected

        # Get neighbors of this skill
        neighbors = list(SKILL_GRAPH.neighbors(skill))
        skill_neighbors = [n for n in neighbors
                         if SKILL_GRAPH.nodes[n].get('node_type') in ['related_skill', 'skill']]

        if not skill_neighbors:
            continue

        # Count how many neighbors are in detected skills
        neighbor_matches = sum(1 for n in skill_neighbors if n in detected_lower)
        match_ratio = neighbor_matches / len(skill_neighbors)

        if match_ratio >= threshold:
            inferred_skills.append(skill)
            inference_details.append({
                'skill': skill,
                'confidence': round(match_ratio * 100, 1),
                'evidence': [n for n in skill_neighbors if n in detected_lower][:5]
            })

    return inferred_skills, {
        'total_inferred': len(inferred_skills),
        'inference_threshold': threshold,
        'details': inference_details[:10]  # Limit output
    }


def calculate_graph_centrality_score(
    resume_skills: List[str],
    jd_skills: List[str]
) -> Tuple[float, Dict[str, Any]]:
    """
    Calculate skill match score using graph centrality (§8.2).

    Evaluates how central the candidate's skills are to the
    required skills graph.

    Returns:
        score: 0-100 centrality-weighted match score
        details: Analysis breakdown
    """
    if not NETWORKX_AVAILABLE or SKILL_GRAPH is None:
        return 0, {'error': 'NetworkX not available'}

    resume_lower = set(s.lower() for s in resume_skills)
    jd_lower = set(s.lower() for s in jd_skills)

    # Create subgraph of JD skills
    jd_nodes = [s for s in jd_lower if s in SKILL_GRAPH]

    if not jd_nodes:
        return 50, {'error': 'No JD skills found in graph'}

    # Calculate PageRank centrality
    try:
        pagerank = nx.pagerank(SKILL_GRAPH, alpha=0.85)
    except:
        pagerank = {n: 1/len(SKILL_GRAPH) for n in SKILL_GRAPH}

    # Score based on overlap with centrality weighting
    total_jd_centrality = sum(pagerank.get(s, 0) for s in jd_nodes)
    matched_centrality = sum(pagerank.get(s, 0) for s in jd_nodes if s in resume_lower)

    if total_jd_centrality > 0:
        score = (matched_centrality / total_jd_centrality) * 100
    else:
        score = 0

    # Bonus for inferred skills
    inferred, _ = infer_skills_from_graph(list(resume_lower))
    inferred_in_jd = [s for s in inferred if s in jd_lower]

    if inferred_in_jd:
        score += len(inferred_in_jd) * 5  # +5 per inferred match

    score = min(100, score)

    details = {
        'jd_skills_in_graph': len(jd_nodes),
        'matched_with_centrality': round(matched_centrality, 4),
        'total_jd_centrality': round(total_jd_centrality, 4),
        'inferred_matches': inferred_in_jd,
        'graph_available': True
    }

    return round(score, 1), details


# =============================================================================
# §4.1-4.3 - DOMAIN-SPECIFIC SCORING MODULES
# =============================================================================

DOMAIN_SCORING_PROFILES = {
    'finance': {
        'description': 'Investment Banking / Private Equity / Finance',
        'format_strictness': 'high',  # Penalize non-standard fonts, colors
        'required_sections': ['experience', 'education', 'transactions', 'deal experience'],
        'critical_keywords': ['m&a', 'ipo', 'lbo', 'valuation', 'dcf', 'financial modeling',
                             'due diligence', 'pitch book', 'bulge bracket', 'boutique'],
        'deal_patterns': [
            r'\$[\d.,]+\s*[MBmb]',  # Deal sizes like $500M
            r'(?:sell|buy)-side',
            r'(?:lead|support)(?:ed|ing)?\s+(?:transaction|deal)',
        ],
        'prestige_weight': 1.5,  # Higher weight for prestige
        'length_penalty': True,  # Strict 1-page rule
        'formatting_rules': {
            'acceptable_fonts': ['times new roman', 'arial', 'calibri'],
            'color_allowed': False,
            'max_pages': 1
        }
    },
    'technology': {
        'description': 'Software Engineering / Data Science / Tech',
        'format_strictness': 'medium',
        'required_sections': ['experience', 'skills', 'projects', 'education'],
        'critical_keywords': ['github', 'gitlab', 'portfolio', 'api', 'microservices',
                             'cloud', 'aws', 'kubernetes', 'docker', 'ci/cd'],
        'portfolio_patterns': [
            r'github\.com/\w+',
            r'gitlab\.com/\w+',
            r'linkedin\.com/in/\w+',
        ],
        'prestige_weight': 1.2,
        'length_penalty': False,  # 2 pages OK for senior roles
        'skill_recency_weight': 1.3,  # Higher weight for recent skills
        'title_validation': {
            'junior': {'max_years': 3},
            'senior': {'min_years': 4},
            'staff': {'min_years': 7},
            'principal': {'min_years': 10}
        }
    },
    'clinical_research': {
        'description': 'Clinical Research / Pharma / Biotech',
        'format_strictness': 'low',  # CV format acceptable
        'required_sections': ['experience', 'education', 'publications', 'certifications'],
        'critical_keywords': ['clinical trial', 'fda', 'gcp', 'irb', 'protocol',
                             'pharmacovigilance', 'regulatory', 'phase i', 'phase ii',
                             'phase iii', 'ind', 'nda', 'cro'],
        'publication_patterns': [
            r'(?:et al\.|et al,)',  # Citation patterns
            r'(?:pubmed|doi|pmid)',
            r'(?:journal|conference|abstract)',
        ],
        'prestige_weight': 1.0,
        'length_penalty': False,  # Multi-page CVs expected
        'transferable_skills_map': {
            'study coordination': 'project management',
            'grant writing': 'business development',
            'protocol development': 'strategic planning',
            'data collection': 'data management'
        }
    },
    'consulting': {
        'description': 'Management Consulting / Strategy',
        'format_strictness': 'high',
        'required_sections': ['experience', 'education', 'skills'],
        'critical_keywords': ['consulting', 'strategy', 'client', 'engagement',
                             'transformation', 'analysis', 'recommendation',
                             'stakeholder', 'framework'],
        'impact_patterns': [
            r'\d+%\s*(?:increase|decrease|improvement|reduction)',
            r'\$[\d.,]+\s*(?:saved|generated|revenue)',
        ],
        'prestige_weight': 1.4,  # MBB prestige matters
        'length_penalty': True,
        'education_weight': 1.3  # Top MBA/degree matters more
    },
    'healthcare': {
        'description': 'Healthcare Operations / Hospital Administration',
        'format_strictness': 'medium',
        'required_sections': ['experience', 'education', 'certifications', 'licensure'],
        'critical_keywords': ['patient care', 'quality improvement', 'jcaho', 'hipaa',
                             'ehr', 'epic', 'cerner', 'clinical operations',
                             'care coordination', 'population health'],
        'certification_patterns': [
            r'\b(?:RN|BSN|MSN|NP|PA|MD|DO)\b',
            r'\b(?:CPHQ|LEAN|Six Sigma)\b',
        ],
        'prestige_weight': 1.0,
        'length_penalty': False
    },
    'general': {
        'description': 'General / Default Profile',
        'format_strictness': 'medium',
        'required_sections': ['experience', 'education', 'skills'],
        'critical_keywords': [],
        'prestige_weight': 1.0,
        'length_penalty': False
    }
}


def get_domain_scoring_profile(domain: str) -> Dict[str, Any]:
    """Get the scoring profile for a specific domain."""
    return DOMAIN_SCORING_PROFILES.get(domain, DOMAIN_SCORING_PROFILES['general'])


def apply_domain_specific_scoring(
    base_scores: Dict[str, float],
    resume_text: str,
    jd_text: str,
    domain: str,
    file_path: str = None
) -> Tuple[Dict[str, float], List[str], Dict[str, Any]]:
    """
    Apply domain-specific scoring adjustments (§4.1-4.3).

    Adjusts scores based on domain requirements:
    - Finance: Deal sheet, prestige, strict formatting
    - Tech: Portfolio links, skill recency, title validation
    - Clinical: Publications, certifications, transferable skills
    - Consulting: Impact metrics, education prestige

    Args:
        base_scores: Dictionary of base scores
        resume_text: Resume text content
        jd_text: Job description text
        domain: Detected or specified domain
        file_path: Optional path to resume file

    Returns:
        adjusted_scores: Domain-adjusted scores
        warnings: Domain-specific warnings
        details: Analysis breakdown
    """
    profile = get_domain_scoring_profile(domain)
    adjusted_scores = base_scores.copy()
    warnings = []
    details = {
        'domain': domain,
        'profile_used': profile['description'],
        'adjustments': []
    }

    text_lower = resume_text.lower()

    # Check for critical keywords
    critical_found = []
    critical_missing = []
    for kw in profile.get('critical_keywords', []):
        if kw in text_lower:
            critical_found.append(kw)
        else:
            critical_missing.append(kw)

    # Adjust keyword score based on critical keywords
    if critical_found:
        critical_ratio = len(critical_found) / len(profile.get('critical_keywords', [1]))
        bonus = critical_ratio * 10
        adjusted_scores['keyword_score'] = min(100, adjusted_scores.get('keyword_score', 0) + bonus)
        details['adjustments'].append(f"Critical keywords bonus: +{bonus:.1f}")

    if critical_missing:
        warnings.append(f"Missing critical {domain} keywords: {', '.join(critical_missing[:5])}")

    # Domain-specific checks
    if domain == 'finance':
        # Check for deal patterns
        deal_count = 0
        for pattern in profile.get('deal_patterns', []):
            deal_count += len(re.findall(pattern, text_lower, re.IGNORECASE))

        if deal_count == 0:
            warnings.append("FINANCE: No transaction/deal artifacts found")
            adjusted_scores['phrase_score'] = adjusted_scores.get('phrase_score', 0) * 0.8
        elif deal_count >= 3:
            adjusted_scores['phrase_score'] = min(100, adjusted_scores.get('phrase_score', 0) + 15)
            details['adjustments'].append(f"Deal experience bonus: +15 ({deal_count} deals found)")

    elif domain == 'technology':
        # Check for portfolio links
        portfolio_found = False
        for pattern in profile.get('portfolio_patterns', []):
            if re.search(pattern, text_lower):
                portfolio_found = True
                break

        if not portfolio_found:
            warnings.append("TECH: No GitHub/portfolio links found")
        else:
            adjusted_scores['weighted_score'] = min(100, adjusted_scores.get('weighted_score', 0) + 5)
            details['adjustments'].append("Portfolio link bonus: +5")

        # Apply skill recency weight
        if 'semantic_score' in adjusted_scores:
            recency_weight = profile.get('skill_recency_weight', 1.0)
            # This would be combined with decay scoring
            details['skill_recency_weight'] = recency_weight

    elif domain == 'clinical_research':
        # Check for publications
        pub_count = 0
        for pattern in profile.get('publication_patterns', []):
            pub_count += len(re.findall(pattern, text_lower))

        if pub_count > 0:
            adjusted_scores['weighted_score'] = min(100, adjusted_scores.get('weighted_score', 0) + 10)
            details['adjustments'].append(f"Publications bonus: +10 ({pub_count} indicators)")

        # Check for transferable skills mapping
        transferable = profile.get('transferable_skills_map', {})
        transfers_found = []
        for academic_term, industry_term in transferable.items():
            if academic_term in text_lower:
                transfers_found.append(f"{academic_term} → {industry_term}")

        if transfers_found:
            details['transferable_skills'] = transfers_found

    elif domain == 'consulting':
        # Check for impact patterns
        impact_count = 0
        for pattern in profile.get('impact_patterns', []):
            impact_count += len(re.findall(pattern, resume_text, re.IGNORECASE))

        if impact_count >= 3:
            adjusted_scores['weighted_score'] = min(100, adjusted_scores.get('weighted_score', 0) + 10)
            details['adjustments'].append(f"Impact metrics bonus: +10 ({impact_count} found)")
        elif impact_count == 0:
            warnings.append("CONSULTING: No quantified impact metrics found")

    # Apply prestige weight multiplier
    prestige_weight = profile.get('prestige_weight', 1.0)
    if 'competitive_score' in adjusted_scores and prestige_weight != 1.0:
        adjusted_scores['competitive_score'] = adjusted_scores.get('competitive_score', 0) * prestige_weight
        adjusted_scores['competitive_score'] = min(100, adjusted_scores['competitive_score'])

    details['critical_keywords_found'] = len(critical_found)
    details['critical_keywords_missing'] = len(critical_missing)
    details['prestige_weight_applied'] = prestige_weight

    return adjusted_scores, warnings, details


# =============================================================================
# §5.2, §11 - FAIRLEARN BIAS AUDIT FRAMEWORK
# =============================================================================

# Try to import fairlearn for bias auditing
try:
    from fairlearn.metrics import demographic_parity_difference, equalized_odds_difference
    FAIRLEARN_AVAILABLE = True
except ImportError:
    FAIRLEARN_AVAILABLE = False


def strip_pii_for_bias_audit(resume_text: str) -> Tuple[str, Dict[str, Any]]:
    """
    Strip PII (Personally Identifiable Information) for bias-free scoring (§5.2).

    Uses pii_redactor (Presidio-backed when available, regex fallback otherwise)
    for name/email/phone/address/URL redaction, then applies bias-audit-specific
    stripping for graduation years and gender indicators.

    Returns:
        cleaned_text: Text with PII stripped
        stripped_info: Dictionary of what was stripped (for audit logging)
    """
    from pii_redactor import redact_text

    stripped = {
        'name_stripped': False,
        'address_stripped': False,
        'graduation_years_stripped': [],
        'gender_indicators_stripped': []
    }

    # Phase 1: Core PII redaction via pii_redactor (names, emails, phones, etc.)
    cleaned_text = redact_text(resume_text)

    # Check what was redacted
    if '<PERSON>' in cleaned_text:
        stripped['name_stripped'] = True
    if '<ADDRESS>' in cleaned_text:
        stripped['address_stripped'] = True

    # Phase 2: Bias-audit-specific stripping (graduation years, gender indicators)

    # Strip zip codes
    zip_pattern = r'\b\d{5}(-\d{4})?\b'
    cleaned_text = re.sub(zip_pattern, '[ZIP]', cleaned_text)

    # Strip graduation years (age proxy) - years between 1960-2030
    lines = cleaned_text.split('\n')
    cleaned_lines = []
    grad_year_pattern = r'\b(19[6-9]\d|20[0-3]\d)\b'
    for line in lines:
        grad_years = re.findall(grad_year_pattern, line)
        if grad_years:
            if any(edu in line.lower() for edu in ['degree', 'graduated', 'bachelor', 'master', 'phd', 'university', 'college']):
                for year in grad_years:
                    stripped['graduation_years_stripped'].append(year)
                line = re.sub(grad_year_pattern, '[YEAR]', line)
        cleaned_lines.append(line)
    cleaned_text = '\n'.join(cleaned_lines)

    # Strip gender indicators throughout
    gender_indicators = [
        (r'\b(he|him|his)\b', '[PRONOUN]'),
        (r'\b(she|her|hers)\b', '[PRONOUN]'),
        (r'\b(Mr\.|Mrs\.|Ms\.|Miss)\b', '[TITLE]'),
        (r'\b(husband|wife|father|mother)\b', '[FAMILY]'),
    ]

    for pattern, replacement in gender_indicators:
        matches = re.findall(pattern, cleaned_text, re.IGNORECASE)
        if matches:
            stripped['gender_indicators_stripped'].extend(matches)
            cleaned_text = re.sub(pattern, replacement, cleaned_text, flags=re.IGNORECASE)

    return cleaned_text, stripped


def audit_scoring_bias(
    scores_by_group: Dict[str, List[float]],
    group_labels: List[str] = None
) -> Dict[str, Any]:
    """
    Audit scoring results for demographic bias (§5.2, §11).

    Calculates:
    - Mean scores by group
    - Score distribution variance
    - Demographic parity difference (if fairlearn available)

    Args:
        scores_by_group: Dictionary mapping group name to list of scores
                        e.g., {'ivy_league': [85, 90, 88], 'state_school': [72, 78, 75]}
        group_labels: Optional list of group identifiers

    Returns:
        Dictionary with bias audit results
    """
    import statistics

    audit_results = {
        'groups_analyzed': list(scores_by_group.keys()),
        'group_statistics': {},
        'fairness_metrics': {},
        'recommendations': []
    }

    # Calculate statistics for each group
    all_scores = []
    for group, scores in scores_by_group.items():
        if scores:
            all_scores.extend(scores)
            audit_results['group_statistics'][group] = {
                'count': len(scores),
                'mean': round(statistics.mean(scores), 2),
                'median': round(statistics.median(scores), 2),
                'std_dev': round(statistics.stdev(scores), 2) if len(scores) > 1 else 0
            }

    if not all_scores:
        return {'error': 'No scores provided for audit'}

    # Calculate overall statistics
    overall_mean = statistics.mean(all_scores)
    audit_results['overall_mean'] = round(overall_mean, 2)

    # Calculate demographic parity difference manually
    # (difference between highest and lowest group means)
    group_means = [stats['mean'] for stats in audit_results['group_statistics'].values()]
    if len(group_means) >= 2:
        max_mean = max(group_means)
        min_mean = min(group_means)
        parity_difference = max_mean - min_mean

        audit_results['fairness_metrics']['demographic_parity_difference'] = round(parity_difference, 2)
        audit_results['fairness_metrics']['max_group_mean'] = max_mean
        audit_results['fairness_metrics']['min_group_mean'] = min_mean

        # Generate recommendations based on findings
        if parity_difference > 15:
            audit_results['recommendations'].append(
                f"HIGH BIAS ALERT: {parity_difference:.1f} point difference between groups. "
                "Review scoring weights for prestige factors."
            )
        elif parity_difference > 10:
            audit_results['recommendations'].append(
                f"MODERATE BIAS: {parity_difference:.1f} point difference. "
                "Consider reducing prestige weight multipliers."
            )
        elif parity_difference > 5:
            audit_results['recommendations'].append(
                f"MINOR BIAS: {parity_difference:.1f} point difference. "
                "Scoring is reasonably balanced."
            )
        else:
            audit_results['recommendations'].append(
                "FAIR: Group scoring differences within acceptable range (<5 points)."
            )

    # Use fairlearn if available for more sophisticated analysis
    if FAIRLEARN_AVAILABLE and len(scores_by_group) >= 2:
        try:
            # Prepare data for fairlearn
            # This is a simplified example - real implementation would need
            # actual predictions and labels
            audit_results['fairlearn_available'] = True
        except Exception as e:
            audit_results['fairlearn_error'] = str(e)
    else:
        audit_results['fairlearn_available'] = False

    return audit_results


def create_blind_scoring_mode(resume_text: str, jd_text: str) -> Dict[str, Any]:
    """
    Create a bias-mitigated "blind" scoring mode (§5.2).

    Strips identifying information and adjusts weights to focus
    purely on skills and experience, reducing prestige bias.

    Returns:
        Dictionary with blind scoring results
    """
    # Strip PII from resume
    cleaned_resume, stripped_info = strip_pii_for_bias_audit(resume_text)

    # Import the main scoring function components
    # Use reduced prestige weights for blind mode
    keyword_score, matched_kw, missing_kw = calculate_keyword_match(cleaned_resume, jd_text)
    phrase_score, matched_phrases, missing_phrases = calculate_phrase_match(cleaned_resume, jd_text)
    weighted_score, matched_weighted, missing_weighted = calculate_weighted_score(cleaned_resume, jd_text)
    bm25_score, _ = calculate_bm25_score(cleaned_resume, jd_text)

    # Blind mode: Focus on skills, reduce prestige impact
    blind_total = (
        keyword_score * 0.35 +
        phrase_score * 0.25 +
        weighted_score * 0.25 +
        bm25_score * 0.15
        # Note: No prestige/competitive score in blind mode
    )

    return {
        'blind_score': round(blind_total, 1),
        'component_scores': {
            'keyword': round(keyword_score, 1),
            'phrase': round(phrase_score, 1),
            'weighted': round(weighted_score, 1),
            'bm25': round(bm25_score, 1)
        },
        'pii_stripped': stripped_info,
        'mode': 'blind_hiring',
        'note': 'Prestige factors excluded to reduce bias'
    }


def extract_text_from_pdf(pdf_path):
    """Extract text from PDF file."""
    text = ""
    try:
        with pdfplumber.open(pdf_path) as pdf:
            for page in pdf.pages:
                text += page.extract_text() or ""
    except Exception as e:
        print(f"Error reading PDF: {e}", file=sys.stderr)
    return text


def extract_text_from_file(file_path):
    """Extract text from PDF, DOCX, MD, or TXT file."""
    from text_extractor import extract_text
    return extract_text(file_path)


def clean_text(text):
    """Clean and normalize text."""
    text = text.lower()
    text = re.sub(r'[^\w\s\-/]', ' ', text)
    text = re.sub(r'\s+', ' ', text)
    return text.strip()


def normalize_match_term(term: str) -> str:
    """Normalize a search term using the same cleaning rules as document text."""
    return clean_text(term).strip()


def contains_normalized_term(normalized_text: str, normalized_tokens: set, term: str) -> bool:
    """
    Match a term against cleaned text without allowing substring false positives.

    Single-token terms must match a full token. Multi-token or slash/hyphen terms
    use boundary-aware regex matching on the normalized text.
    """
    normalized_term = normalize_match_term(term)
    if not normalized_term:
        return False

    if ' ' not in normalized_term and '/' not in normalized_term and '-' not in normalized_term:
        return normalized_term in normalized_tokens

    pattern = r'(?<!\w)' + re.escape(normalized_term) + r'(?!\w)'
    return re.search(pattern, normalized_text) is not None


def is_valid_skill(term, domain=None):
    """Check if a term is a recognized skill via O*NET + domain keywords + skill taxonomy.

    Validates against three sources:
    1. O*NET skill taxonomy (data/onet_skills.json)
    2. Domain-specific keywords (data/keywords_{domain}.json)
    3. Existing skill taxonomy / synonym map

    Args:
        term: Keyword to validate (case-insensitive).
        domain: Optional domain for domain-specific validation.

    Returns:
        True if the term is a recognized skill, False if likely noise.
    """
    t = term.lower().strip()

    # Check O*NET taxonomy
    if ONET_AVAILABLE and is_recognized_skill(t):
        return True

    # Check existing SYNONYM_MAP (from skill_taxonomy.json)
    if t in SYNONYM_MAP:
        return True

    # Check domain keywords
    if domain:
        domain_kw = load_domain_keywords(domain)
        if t in domain_kw:
            return True
    else:
        # Check all domains
        for d in _DOMAIN_KEYWORD_FILES:
            dkw = load_domain_keywords(d)
            if t in dkw:
                return True

    return False


def extract_keywords(text, min_length=2, use_lemmatization=True, expand_acro=True):
    """
    Extract meaningful keywords from text with enhanced NLP.

    Uses O*NET taxonomy validation to filter noise words. Only returns
    terms that are recognized skills, domain keywords, or appear in the
    skill taxonomy synonym map.

    Args:
        text: Input text
        min_length: Minimum word length
        use_lemmatization: Apply lemmatization for morphological matching
        expand_acro: Expand acronyms to their full forms
    """
    # Expand acronyms first
    if expand_acro:
        text = expand_acronyms(text)

    cleaned = clean_text(text)
    words = cleaned.split()

    # Basic filtering (stop words + boilerplate)
    candidates = [w for w in words if w not in STOP_WORDS and len(w) >= min_length]

    # Add lemmatized forms for better matching
    if use_lemmatization:
        lemmatized = set()
        for w in candidates:
            lemmatized.add(w)
            lemma = lemmatize_word(w)
            if lemma != w:
                lemmatized.add(lemma)
        candidates = list(lemmatized)

    # Validate against O*NET + domain keywords + skill taxonomy
    if ONET_AVAILABLE:
        keywords = [w for w in candidates if is_valid_skill(w)]
    else:
        # Fallback: original behavior if O*NET not available
        keywords = candidates

    return keywords


def extract_phrases(text, domain=None):
    """
    Extract important multi-word phrases using domain-aware keyword sets.

    Args:
        text: Text to extract phrases from
        domain: Optional domain override. If None, uses all loaded domain keywords.
    """
    cleaned = clean_text(text)
    phrases = []

    if domain:
        # Use domain-specific keywords and phrases
        domain_kw = load_domain_keywords(domain)
        domain_phrases = load_domain_phrases(domain)

        for phrase in domain_kw.keys():
            if ' ' in phrase and phrase in cleaned:
                phrases.append(phrase)

        for phrase in domain_phrases:
            if phrase in cleaned and phrase not in phrases:
                phrases.append(phrase)
    else:
        # Fallback: check PV_KEYWORDS and CLINICAL_PHRASES (legacy behavior)
        for phrase in PV_KEYWORDS.keys():
            if ' ' in phrase and phrase in cleaned:
                phrases.append(phrase)

        for phrase in CLINICAL_PHRASES:
            if phrase in cleaned and phrase not in phrases:
                phrases.append(phrase)

    return phrases


def extract_jd_keywords(jd_text, domain=None):
    """
    Extract important keywords and phrases from job description.
    Enhanced with acronym expansion, lemmatization, domain-aware filtering,
    and O*NET taxonomy validation.

    Args:
        jd_text: Job description text
        domain: Optional domain override. If None, auto-detects from JD.
    """
    # Auto-detect domain from JD if not provided
    if domain is None:
        domain, _, _ = detect_domain(jd_text)

    domain_kw = load_domain_keywords(domain)

    # Expand acronyms to capture full forms
    expanded_jd = expand_acronyms(jd_text)
    cleaned = clean_text(expanded_jd)

    # Extract single keywords
    words = cleaned.split()
    keywords = [w for w in words if w not in STOP_WORDS and len(w) >= 3]

    # Count frequency
    word_counts = Counter(keywords)

    # Get top keywords — validated against O*NET + domain keywords + skill taxonomy
    important_keywords = []
    domain_terms = set(str(k).lower() for k in domain_kw.keys())

    for word, count in word_counts.most_common(100):
        # Include if: recognized skill OR (appears 2+ times AND passes O*NET check)
        if word in domain_terms or word in SYNONYM_MAP:
            important_keywords.append(word)
        elif ONET_AVAILABLE and is_valid_skill(word, domain=domain):
            important_keywords.append(word)
        elif count > 1 and not ONET_AVAILABLE:
            # Fallback: original behavior if O*NET not available
            important_keywords.append(word)

    # Extract phrases (domain-aware)
    phrases = extract_phrases(jd_text, domain=domain)

    # all_keywords also validated through O*NET when available
    if ONET_AVAILABLE:
        validated_all = [w for w in set(keywords) if is_valid_skill(w, domain=domain)
                         or w in domain_terms or w in SYNONYM_MAP]
    else:
        validated_all = list(set(keywords))

    return {
        'keywords': important_keywords[:50],
        'phrases': phrases,
        'all_keywords': validated_all
    }


def check_job_title_match(resume_text, jd_text):
    """
    Check if the JD job title appears in the resume (§10.6x callback data).

    Returns:
        score (float): 100 if exact title in header/summary (top 20% of resume),
                       50 if found elsewhere in resume, 0 if missing.
        title (str): The extracted job title from JD, or empty string.
    """
    # Extract job title from JD - look for common patterns
    jd_lower = jd_text.lower().strip()
    title = ""

    # Pattern 1: "Job Title: <title>" or "Position: <title>" or "Role: <title>"
    title_patterns = [
        r'(?:job\s*title|position|role)\s*[:：]\s*(.+?)(?:\n|$)',
        r'^(.+?)\s*(?:\n|$)',  # First line of JD as fallback
    ]

    for pattern in title_patterns:
        match = re.search(pattern, jd_lower, re.MULTILINE)
        if match:
            candidate_title = match.group(1).strip()
            # Skip if it looks like a company name or is too long
            if len(candidate_title) > 5 and len(candidate_title) < 80:
                title = candidate_title
                break

    if not title:
        return 0, ""

    # Clean the title
    title_clean = re.sub(r'[^\w\s]', ' ', title).strip()
    title_clean = re.sub(r'\s+', ' ', title_clean).lower()

    if len(title_clean) < 3:
        return 0, ""

    resume_lower = resume_text.lower()
    resume_lines = resume_lower.split('\n')

    # Top 20% of resume = header/summary area
    top_section_end = max(1, len(resume_lines) // 5)
    top_section = '\n'.join(resume_lines[:top_section_end])

    # Check for exact title match in header/summary
    if title_clean in top_section:
        return 100, title_clean

    # Check for exact title match anywhere in resume
    if title_clean in resume_lower:
        return 50, title_clean

    # Check for partial match (all significant words present in same section)
    title_words = [w for w in title_clean.split() if w not in STOP_WORDS and len(w) > 2]
    if title_words:
        # Check if all title words appear in top section
        if all(w in top_section for w in title_words):
            return 80, title_clean
        # Check if all title words appear anywhere
        if all(w in resume_lower for w in title_words):
            return 40, title_clean

    return 0, title_clean


def calculate_keyword_match(resume_text, jd_text):
    """
    Calculate keyword match percentage with enhanced matching.

    Uses:
    - Lemmatization (running/ran -> run)
    - Acronym expansion (FDA -> food and drug administration)
    - Synonym matching via skill taxonomy

    Returns:
        match_pct: Percentage of JD keywords found in resume
        matched: List of matched keywords
        missing: List of missing keywords
    """
    # Extract keywords with lemmatization and acronym expansion
    jd_keywords = set(extract_keywords(jd_text, use_lemmatization=True, expand_acro=True))
    resume_keywords = set(extract_keywords(resume_text, use_lemmatization=True, expand_acro=True))

    if not jd_keywords:
        return 0, [], []

    # Use enhanced synonym-aware matching
    matched, partial_matched, missing = match_with_synonyms(jd_keywords, resume_keywords)

    # Partial matches count as 0.7 weight (synonym/related term match)
    total_matches = len(matched) + (len(partial_matched) * 0.7)
    match_pct = (total_matches / len(jd_keywords)) * 100

    # Combine matched and partial for reporting
    all_matched = list(matched) + [f"{p}*" for p in partial_matched]  # * indicates synonym match

    return match_pct, all_matched[:30], list(missing)[:20]


def calculate_phrase_match(resume_text, jd_text, domain=None):
    """
    Calculate important phrase matches using domain-aware phrase sets.

    Args:
        resume_text: Resume content
        jd_text: Job description content
        domain: Optional domain override. If None, auto-detects from JD.
    """
    # Auto-detect domain from JD if not provided
    if domain is None:
        domain, _, _ = detect_domain(jd_text)

    jd_phrases = set(extract_phrases(jd_text, domain=domain))
    resume_phrases = set(extract_phrases(resume_text, domain=domain))

    if not jd_phrases:
        return 100, [], []  # No specific phrases required

    matched = jd_phrases.intersection(resume_phrases)
    missing = jd_phrases - resume_phrases

    match_pct = (len(matched) / len(jd_phrases)) * 100
    return match_pct, list(matched), list(missing)


def calculate_weighted_score(resume_text, jd_text, domain=None):
    """
    Calculate weighted score based on domain-specific industry keywords.
    Uses domain auto-detection to select the right keyword set.
    Enhanced with acronym expansion for better matching.

    Args:
        resume_text: Resume content
        jd_text: Job description content
        domain: Optional domain override. If None, auto-detects from JD.
    """
    # Auto-detect domain from JD if not provided
    if domain is None:
        domain, _, _ = detect_domain(jd_text)

    domain_kw = load_domain_keywords(domain)

    # Expand acronyms in both texts
    expanded_resume = expand_acronyms(resume_text)
    expanded_jd = expand_acronyms(jd_text)

    cleaned_resume = clean_text(expanded_resume)
    cleaned_jd = clean_text(expanded_jd)
    resume_tokens = set(cleaned_resume.split())
    jd_tokens = set(cleaned_jd.split())

    total_weight = 0
    matched_weight = 0
    matched_terms = []
    missing_terms = []

    for term, weight in domain_kw.items():
        if contains_normalized_term(cleaned_jd, jd_tokens, term):
            total_weight += weight
            # Check direct match
            if contains_normalized_term(cleaned_resume, resume_tokens, term):
                matched_weight += weight
                matched_terms.append((term, weight))
            # Check lemmatized match
            elif contains_normalized_term(cleaned_resume, resume_tokens, lemmatize_word(term)):
                matched_weight += weight * 0.9  # Slightly lower for lemma match
                matched_terms.append((term + "*", weight))
            else:
                missing_terms.append((term, weight))

    if total_weight == 0:
        return 100, [], []  # No specific terms required

    score = (matched_weight / total_weight) * 100
    return score, matched_terms, missing_terms


def calculate_ats_score(resume_text, jd_text, file_path: str = None):
    """
    Calculate comprehensive ATS score with all enhanced features (v2.0).

    Features (Research Document Implementation):
    - §2.2.1: Semantic similarity with Sentence Transformers
    - §8.1.1: BM25 probabilistic scoring
    - §2.1.1: Format risk assessment (Workday/Taleo simulation)
    - §2.3.2: Keyword stuffing detection
    - §3.1.2: Readability metrics (Flesch-Kincaid)
    - §4: Domain auto-detection
    - Lemmatization, acronym expansion, synonym matching

    Scoring Weights v2.5 (Research-rebalanced):
    - Keyword Match: 20%
    - Phrase Match: 25% (exact phrases = 10.6x callbacks)
    - Weighted Industry Terms: 15%
    - Semantic Similarity: 10% (reduced — SBERT overestimates ATS)
    - BM25 Score: 10%
    - Graph Centrality: 5%
    - Skill Recency: 5%
    - Job Title Match: 10% (NEW — 10.6x callback data)
    """
    # =========================================================================
    # DOMAIN DETECTION (run first to inform all domain-aware scoring)
    # =========================================================================
    domain, domain_confidence, domain_scores = detect_domain(jd_text)

    # =========================================================================
    # CORE SCORING (Always Available)
    # =========================================================================

    # Keyword match (20% weight) - with lemmatization & synonyms
    keyword_score, matched_kw, missing_kw = calculate_keyword_match(resume_text, jd_text)

    # Phrase match (25% weight) - domain-aware multi-word phrases
    phrase_score, matched_phrases, missing_phrases = calculate_phrase_match(resume_text, jd_text, domain=domain)

    # Weighted industry terms (15% weight) - domain-specific keyword weights
    weighted_score, matched_weighted, missing_weighted = calculate_weighted_score(resume_text, jd_text, domain=domain)

    # BM25 scoring (10% weight) - probabilistic ranking
    bm25_score, bm25_details = calculate_bm25_score(resume_text, jd_text)

    # =========================================================================
    # ADVANCED SCORING (Optional Dependencies)
    # =========================================================================

    # Semantic similarity (10% weight if available)
    semantic_score, semantic_details = calculate_semantic_similarity(resume_text, jd_text)

    # =========================================================================
    # QUALITY ANALYSIS
    # =========================================================================

    # Keyword stuffing detection
    is_stuffed, stuffing_score, stuffing_details = detect_keyword_stuffing(resume_text)

    # Readability analysis
    readability_score, readability_grade, readability_details = calculate_readability(resume_text, domain=domain)

    # Format risk (if file path provided)
    format_risk = 0
    format_warnings = []
    format_details = {}
    if file_path:
        format_risk, format_warnings, format_details = assess_format_risk(file_path)

    # =========================================================================
    # NEW FEATURES (§2.3.1, §2.3.2, §2.2.2, §4.1-4.3)
    # =========================================================================

    # Hidden text detection (§2.3.2 enhanced)
    hidden_text_detected = False
    hidden_text_warnings = []
    hidden_text_details = {}
    if file_path:
        hidden_text_detected, hidden_text_warnings, hidden_text_details = detect_hidden_text(file_path)

    # Skill recency/decay analysis (§2.3.1)
    recency_adjusted_score, recency_details = calculate_recency_adjusted_score(
        matched_kw, resume_text, keyword_score
    )

    # Job title match (§research: 10.6x callback increase for exact title match)
    job_title_score, job_title_extracted = check_job_title_match(resume_text, jd_text)

    # Graph-based skill inference (§2.2.2, §8.2)
    inferred_skills = []
    inference_details = {}
    graph_score = 0
    graph_details = {}
    if NETWORKX_AVAILABLE and SKILL_GRAPH is not None:
        inferred_skills, inference_details = infer_skills_from_graph(matched_kw)
        resume_skills = extract_keywords(resume_text)
        jd_skills = extract_keywords(jd_text)
        graph_score, graph_details = calculate_graph_centrality_score(resume_skills, jd_skills)

    # Domain-specific scoring adjustments (§4.1-4.3)
    base_scores = {
        'keyword_score': keyword_score,
        'phrase_score': phrase_score,
        'weighted_score': weighted_score,
        'semantic_score': semantic_score,
        'bm25_score': bm25_score
    }
    domain_adjusted_scores, domain_warnings, domain_details = apply_domain_specific_scoring(
        base_scores, resume_text, jd_text, domain, file_path
    )

    # =========================================================================
    # COMBINED SCORING
    # =========================================================================

    # Use domain-adjusted scores for final calculation
    adj_keyword = domain_adjusted_scores.get('keyword_score', keyword_score)
    adj_phrase = domain_adjusted_scores.get('phrase_score', phrase_score)
    adj_weighted = domain_adjusted_scores.get('weighted_score', weighted_score)
    adj_semantic = domain_adjusted_scores.get('semantic_score', semantic_score)
    adj_bm25 = bm25_score

    if SBERT_AVAILABLE and adj_semantic > 0:
        # Full scoring with semantic similarity (v2.5 rebalanced weights)
        # Research: exact phrases = 10.6x callbacks, SBERT overestimates ATS capability
        total_score = (
            adj_keyword * 0.20 +      # was 0.22: slight reduction
            adj_phrase * 0.25 +        # was 0.13: exact phrases = 10.6x callbacks
            adj_weighted * 0.15 +      # was 0.18: slight reduction
            adj_semantic * 0.10 +      # was 0.22: SBERT overweighted per research
            adj_bm25 * 0.10 +          # was 0.13: slight reduction
            graph_score * 0.05 +       # was 0.07: minor contributor
            recency_adjusted_score * 0.05 +  # unchanged
            job_title_score * 0.10     # NEW: 10.6x callback data
        )
    else:
        # Fallback: redistribute semantic weight proportionally
        total_score = (
            adj_keyword * 0.23 +       # was 0.30
            adj_phrase * 0.28 +        # was 0.18: phrase match most important
            adj_weighted * 0.17 +      # was 0.22
            adj_bm25 * 0.12 +          # was 0.18
            graph_score * 0.05 +       # was 0.07
            recency_adjusted_score * 0.05 +  # unchanged
            job_title_score * 0.10     # NEW: job title match
        )

    # Apply penalties
    penalties = {}

    # Stuffing penalty
    if is_stuffed:
        penalty = min(15, stuffing_score * 0.15)
        total_score -= penalty
        penalties['keyword_stuffing'] = -penalty

    # Hidden text manipulation penalty (CRITICAL - §2.3.2)
    if hidden_text_detected:
        penalty = 25  # Severe penalty for manipulation
        total_score -= penalty
        penalties['hidden_text_manipulation'] = -penalty

    # Format risk penalty (if severe)
    if format_risk > 50:
        penalty = min(10, format_risk * 0.1)
        total_score -= penalty
        penalties['format_risk'] = -penalty

    # Readability penalty (if too complex or too simple)
    if readability_score < 60:
        penalty = (60 - readability_score) * 0.1
        total_score -= penalty
        penalties['readability'] = -penalty

    total_score = max(0, min(100, total_score))

    # Separate synonym matches (marked with *) from direct matches
    direct_matches = [kw for kw in matched_kw if not kw.endswith('*')]
    synonym_matches = [kw.rstrip('*') for kw in matched_kw if kw.endswith('*')]

    return {
        # Primary scores
        'total_score': round(total_score, 1),
        'keyword_score': round(keyword_score, 1),
        'phrase_score': round(phrase_score, 1),
        'weighted_score': round(weighted_score, 1),
        'semantic_score': round(semantic_score, 1),
        'bm25_score': round(bm25_score, 1),

        # Match details
        'matched_keywords': matched_kw[:15],
        'missing_keywords': missing_kw[:15],
        'matched_phrases': matched_phrases,
        'missing_phrases': missing_phrases,
        'matched_weighted': [t[0] for t in matched_weighted[:10]],
        'missing_weighted': [t[0] for t in missing_weighted[:10]],
        'direct_matches': direct_matches[:10],
        'synonym_matches': synonym_matches[:10],

        # Quality analysis
        'readability': {
            'score': round(readability_score, 1),
            'grade': readability_grade,
            'details': readability_details
        },
        'stuffing_analysis': {
            'is_stuffed': is_stuffed,
            'score': round(stuffing_score, 1),
            'details': stuffing_details
        },
        'format_risk': {
            'score': format_risk,
            'warnings': format_warnings,
            'details': format_details
        },

        # Domain detection
        'domain': {
            'detected': domain,
            'confidence': domain_confidence,
            'all_scores': domain_scores
        },

        # Penalties applied
        'penalties': penalties,

        # Feature availability
        'nlp_features': {
            'nltk_available': NLTK_AVAILABLE,
            'sbert_available': SBERT_AVAILABLE,
            'textstat_available': TEXTSTAT_AVAILABLE,
            'networkx_available': NETWORKX_AVAILABLE,
            'acronyms_loaded': len(ACRONYMS),
            'taxonomy_terms': len(SYNONYM_MAP),
            'domain_phrases': len(load_domain_phrases(domain)),
            'domain_keywords': len(load_domain_keywords(domain))
        },

        # Detailed breakdowns
        'semantic_details': semantic_details,
        'bm25_details': bm25_details,

        # NEW: Skill Recency Analysis (§2.3.1)
        'skill_recency': {
            'recency_adjusted_score': round(recency_adjusted_score, 1),
            'details': recency_details
        },

        # NEW: Graph-Based Skill Inference (§2.2.2, §8.2)
        'skill_graph': {
            'graph_score': round(graph_score, 1),
            'inferred_skills': inferred_skills[:10],
            'inference_details': inference_details,
            'centrality_details': graph_details
        },

        # NEW: Job Title Match (§research: 10.6x callback)
        'job_title_match': {
            'score': job_title_score,
            'extracted_title': job_title_extracted
        },

        # NEW: Hidden Text Detection (§2.3.2 enhanced)
        'hidden_text': {
            'detected': hidden_text_detected,
            'warnings': hidden_text_warnings,
            'details': hidden_text_details
        },

        # NEW: Domain-Specific Adjustments (§4.1-4.3)
        'domain_adjustments': {
            'original_scores': base_scores,
            'adjusted_scores': domain_adjusted_scores,
            'warnings': domain_warnings,
            'details': domain_details
        }
    }


def get_likelihood_rating(score):
    """Convert score to likelihood rating."""
    if score >= 80:
        return "Excellent", "Top Candidate", "#22c55e"
    elif score >= 65:
        return "Good", "Strong Match", "#84cc16"
    elif score >= 50:
        return "Fair", "Competitive", "#eab308"
    elif score >= 35:
        return "Low", "Below Average", "#f97316"
    else:
        return "Poor", "Unlikely Match", "#ef4444"


def score_resume(resume_path, jd_path):
    """Score a resume against a job description and return results."""
    resume_text = extract_text_from_file(resume_path)
    jd_text = extract_text_from_file(jd_path)

    scores = calculate_ats_score(resume_text, jd_text)
    rating, likelihood, color = get_likelihood_rating(scores['total_score'])

    scores['rating'] = rating
    scores['likelihood'] = likelihood

    return scores


def score_resume_text(resume_text, jd_text):
    """Score resume text directly against job description text."""
    scores = calculate_ats_score(resume_text, jd_text)
    rating, likelihood, color = get_likelihood_rating(scores['total_score'])

    scores['rating'] = rating
    scores['likelihood'] = likelihood

    return scores


# ============== Web Interface ==============

HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>ATS Resume Scorer</title>
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body {
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Oxygen, Ubuntu, sans-serif;
            background: linear-gradient(135deg, #1e3a5f 0%, #0d1b2a 100%);
            min-height: 100vh;
            padding: 40px 20px;
            color: #e0e0e0;
        }
        .container { max-width: 1400px; margin: 0 auto; }
        h1 {
            text-align: center;
            font-size: 2.5rem;
            margin-bottom: 10px;
            color: #fff;
        }
        .subtitle {
            text-align: center;
            color: #94a3b8;
            margin-bottom: 40px;
            font-size: 1.1rem;
        }
        .comparison {
            display: grid;
            grid-template-columns: 1fr 1fr;
            gap: 30px;
        }
        .card {
            background: rgba(255,255,255,0.05);
            border-radius: 16px;
            padding: 30px;
            border: 1px solid rgba(255,255,255,0.1);
        }
        .card-title {
            font-size: 1.4rem;
            margin-bottom: 20px;
            padding-bottom: 15px;
            border-bottom: 2px solid rgba(255,255,255,0.1);
        }
        .base-card .card-title { color: #f97316; }
        .tailored-card .card-title { color: #22c55e; }

        .score-circle {
            width: 180px;
            height: 180px;
            border-radius: 50%;
            display: flex;
            flex-direction: column;
            align-items: center;
            justify-content: center;
            margin: 20px auto;
            position: relative;
        }
        .score-circle::before {
            content: '';
            position: absolute;
            inset: 0;
            border-radius: 50%;
            padding: 6px;
            background: conic-gradient(var(--score-color) calc(var(--score) * 3.6deg), rgba(255,255,255,0.1) 0);
            -webkit-mask: linear-gradient(#fff 0 0) content-box, linear-gradient(#fff 0 0);
            mask: linear-gradient(#fff 0 0) content-box, linear-gradient(#fff 0 0);
            -webkit-mask-composite: xor;
            mask-composite: exclude;
        }
        .score-value {
            font-size: 3rem;
            font-weight: bold;
            color: var(--score-color);
        }
        .score-label { font-size: 0.9rem; color: #94a3b8; }

        .likelihood {
            text-align: center;
            padding: 15px;
            border-radius: 10px;
            margin: 20px 0;
            font-weight: 600;
            font-size: 1.2rem;
        }

        .metrics {
            display: grid;
            grid-template-columns: repeat(3, 1fr);
            gap: 15px;
            margin: 25px 0;
        }
        .metric {
            background: rgba(255,255,255,0.05);
            padding: 15px;
            border-radius: 10px;
            text-align: center;
        }
        .metric-value {
            font-size: 1.8rem;
            font-weight: bold;
            color: #60a5fa;
        }
        .metric-label {
            font-size: 0.8rem;
            color: #94a3b8;
            margin-top: 5px;
        }

        .keywords-section { margin-top: 25px; }
        .keywords-title {
            font-size: 1rem;
            margin-bottom: 10px;
            color: #94a3b8;
        }
        .keywords {
            display: flex;
            flex-wrap: wrap;
            gap: 8px;
        }
        .keyword {
            padding: 5px 12px;
            border-radius: 20px;
            font-size: 0.85rem;
        }
        .keyword.matched {
            background: rgba(34, 197, 94, 0.2);
            color: #22c55e;
            border: 1px solid rgba(34, 197, 94, 0.3);
        }
        .keyword.missing {
            background: rgba(239, 68, 68, 0.2);
            color: #ef4444;
            border: 1px solid rgba(239, 68, 68, 0.3);
        }

        .improvement {
            background: linear-gradient(135deg, rgba(34, 197, 94, 0.2), rgba(34, 197, 94, 0.05));
            border: 2px solid rgba(34, 197, 94, 0.3);
            border-radius: 16px;
            padding: 30px;
            margin-top: 30px;
            text-align: center;
        }
        .improvement h2 {
            color: #22c55e;
            margin-bottom: 15px;
        }
        .improvement-value {
            font-size: 4rem;
            font-weight: bold;
            color: #22c55e;
        }
        .improvement-label {
            color: #94a3b8;
            font-size: 1.1rem;
        }

        .job-info {
            background: rgba(255,255,255,0.05);
            border-radius: 12px;
            padding: 20px;
            margin-bottom: 30px;
            text-align: center;
        }
        .job-title { font-size: 1.3rem; color: #60a5fa; }
        .job-company { color: #94a3b8; margin-top: 5px; }

        @media (max-width: 900px) {
            .comparison { grid-template-columns: 1fr; }
        }
    </style>
</head>
<body>
    <div class="container">
        <h1>ATS Resume Scorer</h1>
        <p class="subtitle">Compare your base resume vs tailored resume against the job description</p>

        <div class="job-info">
            <div class="job-title">{{ job_title }}</div>
            <div class="job-company">{{ company }}</div>
        </div>

        <div class="comparison">
            <div class="card base-card">
                <h2 class="card-title">Original Resume</h2>

                <div class="score-circle" style="--score: {{ base.total_score }}; --score-color: {{ base_color }}">
                    <div class="score-value">{{ base.total_score }}%</div>
                    <div class="score-label">ATS Score</div>
                </div>

                <div class="likelihood" style="background: {{ base_color }}20; color: {{ base_color }}; border: 1px solid {{ base_color }}40">
                    {{ base_likelihood }}
                </div>

                <div class="metrics">
                    <div class="metric">
                        <div class="metric-value">{{ base.keyword_score }}%</div>
                        <div class="metric-label">Keyword Match</div>
                    </div>
                    <div class="metric">
                        <div class="metric-value">{{ base.phrase_score }}%</div>
                        <div class="metric-label">Key Phrases</div>
                    </div>
                    <div class="metric">
                        <div class="metric-value">{{ base.weighted_score }}%</div>
                        <div class="metric-label">Industry Terms</div>
                    </div>
                </div>

                <div class="keywords-section">
                    <div class="keywords-title">Matched Keywords</div>
                    <div class="keywords">
                        {% for kw in base.matched_keywords %}
                        <span class="keyword matched">{{ kw }}</span>
                        {% endfor %}
                    </div>
                </div>

                <div class="keywords-section">
                    <div class="keywords-title">Missing Keywords</div>
                    <div class="keywords">
                        {% for kw in base.missing_keywords %}
                        <span class="keyword missing">{{ kw }}</span>
                        {% endfor %}
                    </div>
                </div>
            </div>

            <div class="card tailored-card">
                <h2 class="card-title">Tailored Resume</h2>

                <div class="score-circle" style="--score: {{ tailored.total_score }}; --score-color: {{ tailored_color }}">
                    <div class="score-value">{{ tailored.total_score }}%</div>
                    <div class="score-label">ATS Score</div>
                </div>

                <div class="likelihood" style="background: {{ tailored_color }}20; color: {{ tailored_color }}; border: 1px solid {{ tailored_color }}40">
                    {{ tailored_likelihood }}
                </div>

                <div class="metrics">
                    <div class="metric">
                        <div class="metric-value">{{ tailored.keyword_score }}%</div>
                        <div class="metric-label">Keyword Match</div>
                    </div>
                    <div class="metric">
                        <div class="metric-value">{{ tailored.phrase_score }}%</div>
                        <div class="metric-label">Key Phrases</div>
                    </div>
                    <div class="metric">
                        <div class="metric-value">{{ tailored.weighted_score }}%</div>
                        <div class="metric-label">Industry Terms</div>
                    </div>
                </div>

                <div class="keywords-section">
                    <div class="keywords-title">Matched Keywords</div>
                    <div class="keywords">
                        {% for kw in tailored.matched_keywords %}
                        <span class="keyword matched">{{ kw }}</span>
                        {% endfor %}
                    </div>
                </div>

                <div class="keywords-section">
                    <div class="keywords-title">Missing Keywords</div>
                    <div class="keywords">
                        {% for kw in tailored.missing_keywords %}
                        <span class="keyword missing">{{ kw }}</span>
                        {% endfor %}
                    </div>
                </div>
            </div>
        </div>

        <div class="improvement">
            <h2>Score Improvement</h2>
            <div class="improvement-value">+{{ improvement }}%</div>
            <div class="improvement-label">Your tailored resume scores {{ improvement }} points higher than the original</div>
        </div>
    </div>
</body>
</html>
"""


def run_web_server(base_resume_path, tailored_resume_path, jd_path, job_title="Job", company="Company"):
    """Run the Flask web server to display comparison."""
    from flask import Flask, render_template_string

    app = Flask(__name__)

    @app.route('/')
    def index():
        base_resume_text = extract_text_from_file(base_resume_path)
        tailored_resume_text = extract_text_from_file(tailored_resume_path)
        jd_text = extract_text_from_file(jd_path)

        base_scores = calculate_ats_score(base_resume_text, jd_text)
        tailored_scores = calculate_ats_score(tailored_resume_text, jd_text)

        base_rating, base_likelihood, base_color = get_likelihood_rating(base_scores['total_score'])
        tailored_rating, tailored_likelihood, tailored_color = get_likelihood_rating(tailored_scores['total_score'])

        improvement = round(tailored_scores['total_score'] - base_scores['total_score'], 1)

        return render_template_string(
            HTML_TEMPLATE,
            base=base_scores,
            tailored=tailored_scores,
            base_likelihood=f"{base_rating} - {base_likelihood}",
            tailored_likelihood=f"{tailored_rating} - {tailored_likelihood}",
            base_color=base_color,
            tailored_color=tailored_color,
            improvement=improvement,
            job_title=job_title,
            company=company
        )

    print("\n" + "="*60)
    print("ATS Resume Scorer")
    print("="*60)
    print(f"\nComparing:")
    print(f"  Base: {base_resume_path}")
    print(f"  Tailored: {tailored_resume_path}")
    print(f"  JD: {jd_path}")
    print("\nStarting server at http://localhost:5000")
    print("Press Ctrl+C to stop\n")
    app.run(debug=False, port=5000)


def main():
    parser = argparse.ArgumentParser(description='ATS Resume Scorer')
    parser.add_argument('--web', action='store_true', help='Run web interface')
    parser.add_argument('--score', nargs=2, metavar=('RESUME', 'JD'), help='Score resume against JD')
    parser.add_argument('--json', action='store_true', help='Output as JSON')
    parser.add_argument('--base', help='Base resume path for web comparison')
    parser.add_argument('--tailored', help='Tailored resume path for web comparison')
    parser.add_argument('--jd', help='Job description path')
    parser.add_argument('--title', default='Job Position', help='Job title for display')
    parser.add_argument('--company', default='Company', help='Company name for display')

    args = parser.parse_args()

    if args.score:
        resume_path, jd_path = args.score
        scores = score_resume(resume_path, jd_path)

        if args.json:
            print(json.dumps(scores, indent=2))
        else:
            print(f"\nATS Score: {scores['total_score']}%")
            print(f"Rating: {scores['rating']} - {scores['likelihood']}")
            print(f"\nBreakdown:")
            print(f"  Keyword Match: {scores['keyword_score']}%")
            print(f"  Key Phrases: {scores['phrase_score']}%")
            print(f"  Industry Terms: {scores['weighted_score']}%")
            print(f"\nMissing Keywords: {', '.join(scores['missing_keywords'][:10])}")
            print(f"Missing Phrases: {', '.join(scores['missing_phrases'][:5])}")

    elif args.web:
        base_dir = os.path.dirname(os.path.abspath(__file__))
        base_resume = args.base or os.path.join(base_dir, "master_resume.pdf")
        tailored_resume = args.tailored or os.path.join(base_dir, "applications", "Sanofi", "resume.md")
        jd = args.jd or os.path.join(base_dir, "applications", "Sanofi", "job_description.txt")

        run_web_server(base_resume, tailored_resume, jd, args.title, args.company)

    else:
        # Default: run web with default paths
        base_dir = os.path.dirname(os.path.abspath(__file__))
        base_resume = os.path.join(base_dir, "master_resume.pdf")
        tailored_resume = os.path.join(base_dir, "applications", "Sanofi", "resume.md")
        jd = os.path.join(base_dir, "applications", "Sanofi", "job_description.txt")

        run_web_server(base_resume, tailored_resume, jd, "Global Safety Officer", "Sanofi")


if __name__ == '__main__':
    main()
