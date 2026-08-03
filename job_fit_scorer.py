"""
Job Fit Scorer — Pre-Application Screening
============================================
Evaluates whether a job description is worth applying to BEFORE any resume
tailoring, saving time and API costs.

Features:
- Knockout detection (hard disqualifiers like years of experience)
- 7-dimension fit scoring (SBERT + NLP, zero API cost)
- Gap analysis (fixable vs unfixable)
- Alternative job title suggestions

Usage:
    from job_fit_scorer import calculate_job_fit
    result = calculate_job_fit(resume_text, jd_text)
    print(result['recommendation'])  # "STRONG FIT", "NO-GO", etc.

CLI:
    python job_fit_scorer.py --check resume.md jd.txt
    python job_fit_scorer.py --check resume.md jd.txt --json
"""

import re
import json
import argparse
import hashlib
from pathlib import Path
from dataclasses import dataclass, field, asdict
from typing import Dict, List, Tuple, Optional, Any
from datetime import date

# Reuse existing infrastructure
from hr_scorer import (
    parse_resume, parse_job_description,
    CandidateProfile, JobRequirements, JobEntry
)
from ats_scorer import (
    detect_domain, extract_jd_keywords, check_job_title_match,
    embed_with_cache, SBERT_AVAILABLE, SYNONYM_MAP,
    is_valid_skill, extract_keywords
)

try:
    from ats_scorer import sbert_util
except ImportError:
    sbert_util = None


# =============================================================================
# DATA CLASSES
# =============================================================================

@dataclass
class ExtractedRequirements:
    """Structured requirements extracted from a JD."""
    # Hard requirements (knockout-eligible)
    min_years_total: float = 0.0
    min_years_specific: Dict[str, float] = field(default_factory=dict)
    # Atomic education/experience alternatives such as Amgen's
    # "Doctorate + 3 / Master's + 5 / ..." ladder.  These routes must never
    # be flattened into unconditional degree or years requirements.
    degree_experience_paths: List[Dict[str, Any]] = field(default_factory=list)
    degree_experience_type: str = ""
    required_degree: str = ""  # "bachelor", "master", "md", "phd"
    acceptable_degrees: List[str] = field(default_factory=list)
    required_certifications: List[str] = field(default_factory=list)
    required_languages: List[str] = field(default_factory=list)
    travel_requirement: float = 0.0  # percentage
    visa_sponsorship_available: bool = True  # True = they sponsor
    location_type: str = ""  # "remote", "on-site", "hybrid"
    location_mandatory: bool = False
    relocation_required: bool = False

    # Soft requirements
    preferred_skills: List[str] = field(default_factory=list)
    required_skills: List[str] = field(default_factory=list)
    preferred_experience: List[str] = field(default_factory=list)
    therapeutic_areas: List[str] = field(default_factory=list)
    tools_platforms: List[str] = field(default_factory=list)

    # Job metadata
    title: str = ""
    seniority: str = "mid"  # "entry", "mid", "senior", "director", "vp"
    company: str = ""
    domain: str = ""
    domain_confidence: float = 0.0

    extraction_confidence: float = 0.0
    extraction_trustworthy: bool = True


@dataclass
class EnrichedProfile:
    """Candidate profile enriched with fit-specific data."""
    base: CandidateProfile = None
    years_by_type: Dict[str, float] = field(default_factory=dict)
    highest_degree: str = ""
    degrees: List[str] = field(default_factory=list)
    titles_held: List[str] = field(default_factory=list)
    therapeutic_areas: List[str] = field(default_factory=list)
    tools_known: List[str] = field(default_factory=list)
    certifications: List[str] = field(default_factory=list)
    languages_known: List[str] = field(default_factory=list)
    publications_count: int = 0
    avg_tenure_months: float = 0.0
    location: str = ""
    requires_sponsorship: bool = False
    remote_only: bool = False
    cannot_relocate: bool = False


@dataclass
class KnockoutFlag:
    """A single knockout issue."""
    category: str  # "experience", "education", "certification", "travel", "visa"
    requirement: str  # What the JD requires
    candidate_has: str  # What the candidate has
    severity: str  # "hard" or "soft"
    fixable: bool  # Can resume modification help?
    suggestion: str  # What to do instead


@dataclass
class KnockoutResult:
    """All knockout checks."""
    passed: bool
    knockouts: List[KnockoutFlag] = field(default_factory=list)


@dataclass
class FitDimensions:
    """7-dimension fit scores."""
    experience_match: float = 0.0  # 25%
    skills_match: float = 0.0  # 25%
    title_alignment: float = 0.0  # 15%
    domain_match: float = 0.0  # 15%
    education_match: float = 0.0  # 10%
    certification_match: float = 0.0  # 5%
    seniority_match: float = 0.0  # 5%

    def weighted_score(self) -> float:
        return (
            self.experience_match * 0.25 +
            self.skills_match * 0.25 +
            self.title_alignment * 0.15 +
            self.domain_match * 0.15 +
            self.education_match * 0.10 +
            self.certification_match * 0.05 +
            self.seniority_match * 0.05
        )

    def to_dict(self) -> Dict[str, float]:
        return {
            'experience_match': round(self.experience_match, 1),
            'skills_match': round(self.skills_match, 1),
            'title_alignment': round(self.title_alignment, 1),
            'domain_match': round(self.domain_match, 1),
            'education_match': round(self.education_match, 1),
            'certification_match': round(self.certification_match, 1),
            'seniority_match': round(self.seniority_match, 1),
            'weighted_total': round(self.weighted_score(), 1),
        }


@dataclass
class Gap:
    """A single gap between requirements and profile."""
    category: str
    requirement: str
    current_state: str
    fixable: bool
    fix_suggestion: str


@dataclass
class GapAnalysis:
    """All gaps + suggestions."""
    fixable_gaps: List[Gap] = field(default_factory=list)
    unfixable_gaps: List[Gap] = field(default_factory=list)
    suggested_modifications: List[str] = field(default_factory=list)
    alternative_titles: List[str] = field(default_factory=list)


@dataclass
class JobFitResult:
    """Final output of job fit scoring."""
    overall_score: float
    recommendation: str  # "STRONG FIT", "MODERATE FIT", "WEAK FIT", "NO-GO", "POOR FIT"
    knockouts: KnockoutResult = None
    dimensions: FitDimensions = None
    gap_analysis: GapAnalysis = None
    requirements: ExtractedRequirements = None
    estimated_ats_range: Tuple[float, float] = (0.0, 0.0)
    estimated_hr_range: Tuple[float, float] = (0.0, 0.0)

    def to_dict(self) -> Dict[str, Any]:
        return {
            'overall_score': round(self.overall_score, 1),
            'recommendation': self.recommendation,
            'knockouts': {
                'passed': self.knockouts.passed if self.knockouts else True,
                'flags': [asdict(k) for k in self.knockouts.knockouts] if self.knockouts else [],
            },
            'dimensions': self.dimensions.to_dict() if self.dimensions else {},
            'gap_analysis': {
                'fixable_gaps': [asdict(g) for g in self.gap_analysis.fixable_gaps] if self.gap_analysis else [],
                'unfixable_gaps': [asdict(g) for g in self.gap_analysis.unfixable_gaps] if self.gap_analysis else [],
                'suggested_modifications': self.gap_analysis.suggested_modifications if self.gap_analysis else [],
                'alternative_titles': self.gap_analysis.alternative_titles if self.gap_analysis else [],
            },
            'requirements': asdict(self.requirements) if self.requirements else {},
            'estimated_ats_range': list(self.estimated_ats_range),
            'estimated_hr_range': list(self.estimated_hr_range),
        }


# =============================================================================
# PROFILE CACHE — Parse master resume once
# =============================================================================

_profile_cache: Dict[str, EnrichedProfile] = {}


def _profile_cache_key(text: str, as_of_date: Optional[date] = None) -> str:
    """Return a collision-resistant cache key including the scoring date."""
    date_key = as_of_date.isoformat() if as_of_date else "runtime-date"
    return hashlib.sha256(f"{date_key}\0{text}".encode('utf-8')).hexdigest()


# =============================================================================
# STEP 1: JD REQUIREMENT EXTRACTION
# =============================================================================

_YEAR_UNIT_PATTERN = r'(?:years?|yrs?\.?)'
_YEAR_VALUE_PATTERN = r'(\d+(?:\.\d+)?)\+?'

_NUMBER_WORD_VALUES = {
    'zero': 0,
    'one': 1,
    'two': 2,
    'three': 3,
    'four': 4,
    'five': 5,
    'six': 6,
    'seven': 7,
    'eight': 8,
    'nine': 9,
    'ten': 10,
    'eleven': 11,
    'twelve': 12,
    'thirteen': 13,
    'fourteen': 14,
    'fifteen': 15,
    'sixteen': 16,
    'seventeen': 17,
    'eighteen': 18,
    'nineteen': 19,
    'twenty': 20,
    'thirty': 30,
    'forty': 40,
    'fifty': 50,
    'sixty': 60,
    'seventy': 70,
    'eighty': 80,
    'ninety': 90,
}
_NUMBER_WORD_TOKEN = '|'.join(
    sorted((*_NUMBER_WORD_VALUES, 'hundred'), key=len, reverse=True)
)
_NUMBER_WORD_YEARS_RE = re.compile(
    rf'\b(?P<words>(?:{_NUMBER_WORD_TOKEN})'
    rf'(?:[\s-]+(?:(?:and)[\s-]+)?(?:{_NUMBER_WORD_TOKEN}))*)'
    rf'\s+(?P<unit>{_YEAR_UNIT_PATTERN})\b',
    re.IGNORECASE,
)


def _number_words_to_int(words: str) -> Optional[int]:
    """Convert a compact English cardinal phrase to an integer."""
    tokens = [token for token in re.split(r'[\s-]+', words.lower()) if token != 'and']
    if not tokens:
        return None
    total = 0
    current = 0
    for token in tokens:
        if token == 'hundred':
            if current <= 0:
                return None
            current *= 100
            continue
        value = _NUMBER_WORD_VALUES.get(token)
        if value is None:
            return None
        current += value
    total += current
    return total if total > 0 else None


def _normalize_experience_number_words(text: str) -> str:
    """Normalize spelled-out quantities only when immediately tied to years."""
    def replace(match: re.Match) -> str:
        value = _number_words_to_int(match.group('words'))
        return match.group(0) if value is None else f"{value} {match.group('unit')}"

    return _NUMBER_WORD_YEARS_RE.sub(replace, text)


def _ordered_unique(values: List[str]) -> List[str]:
    result: List[str] = []
    for value in values:
        if value not in result:
            result.append(value)
    return result


def _extract_total_experience_minimum(required_text: str) -> float:
    """Return the strongest parseable hard total-experience minimum."""
    patterns = (
        rf'\bminimum\s+experience\s*[:\-–]?\s*{_YEAR_VALUE_PATTERN}\s*{_YEAR_UNIT_PATTERN}',
        rf'\bminimum\s+(?:of\s+)?{_YEAR_VALUE_PATTERN}\s*{_YEAR_UNIT_PATTERN}',
        rf'\bat\s+least\s+{_YEAR_VALUE_PATTERN}\s*{_YEAR_UNIT_PATTERN}',
        rf'\b(?:requires?|must\s+have|required)\s+(?:a\s+minimum\s+of\s+)?'
        rf'{_YEAR_VALUE_PATTERN}\s*{_YEAR_UNIT_PATTERN}',
        rf'(?<![\d-]){_YEAR_VALUE_PATTERN}\s*{_YEAR_UNIT_PATTERN}\s*'
        rf'(?:of\s+)?(?:relevant\s+|related\s+|professional\s+|industry\s+)?'
        rf'(?:clinical\s+)?(?:research\s+)?experience\b',
        rf'(?m)^\s*[•*\-]?\s*{_YEAR_VALUE_PATTERN}\s*(?:yrs?\.?)\s*$'
    )
    values: List[float] = []
    for pattern in patterns:
        for match in re.finditer(pattern, required_text, re.IGNORECASE):
            value = float(match.group(1))
            if value > 0:
                values.append(value)
    return max(values, default=0.0)


def _has_unparsed_hard_experience_minimum(required_text: str) -> bool:
    """Detect explicit hard-minimum wording whose quantity was not parsed."""
    normalized = _normalize_experience_number_words(required_text.lower())
    cue = re.compile(
        r'\b(?:minimum(?:\s+experience)?|at\s+least|must\s+have|requires?)\b',
        re.IGNORECASE,
    )
    hard_quantity_words = re.compile(
        r'\b(?:several|multiple|many|few|numerous|extensive|significant|substantial)\b',
        re.IGNORECASE,
    )
    for clause in re.split(r'[\n.;]+', normalized):
        starts = [match.start() for match in cue.finditer(clause)]
        for index, start in enumerate(starts):
            end = starts[index + 1] if index + 1 < len(starts) else len(clause)
            segment = clause[start:end]
            has_year_unit = bool(re.search(rf'\b{_YEAR_UNIT_PATTERN}\b', segment))
            parsed_years = bool(
                re.search(rf'{_YEAR_VALUE_PATTERN}\s*{_YEAR_UNIT_PATTERN}\b', segment)
            )
            if has_year_unit and not parsed_years:
                return True
            if re.match(r'\bminimum\s+experience\s*[:\-–]', segment):
                if not parsed_years:
                    return True
            if 'experience' in segment and hard_quantity_words.search(segment):
                return True
    return False


_NEGATION_CUE_RE = re.compile(
    r"\b(?:no|not|never|without|lack|lacks|lacked|lacking|"
    r"do\s+not|does\s+not|did\s+not|has\s+no|have\s+no|"
    r"is\s+not|isn't|aren't|cannot|can't)\b",
    re.IGNORECASE,
)
_ADVERSATIVE_RE = re.compile(r'\b(?:but|however|although|yet)\b', re.IGNORECASE)
_ADVERSE_STATUS_RE = re.compile(
    r'^\s*(?:[-—:,]\s*)?(?:is\s+|was\s+)?'
    r'(?:inactive|expired|suspended|restricted|revoked|lapsed|invalid|pending|'
    r'(?:application\s+)?in\s+progress|'
    r'not\s+(?:active|current|valid|required|unrestricted)|optional)\b',
    re.IGNORECASE,
)
_ADVERSE_PREFIX_RE = re.compile(
    r'\b(?:inactive|expired|suspended|restricted|revoked|lapsed|invalid|pending)\s*$',
    re.IGNORECASE,
)


def _match_is_negated(text: str, match: re.Match) -> bool:
    """Return whether a nearby explicit negation governs this evidence match."""
    boundary = max(
        text.rfind('\n', 0, match.start()),
        text.rfind('.', 0, match.start()),
        text.rfind(';', 0, match.start()),
    )
    prefix = text[max(boundary + 1, match.start() - 120):match.start()]
    adversatives = list(_ADVERSATIVE_RE.finditer(prefix))
    if adversatives:
        prefix = prefix[adversatives[-1].end():]
    prefix = re.sub(r'\bnot\s+only\b', '', prefix, flags=re.IGNORECASE)
    if _NEGATION_CUE_RE.search(prefix) or _ADVERSE_PREFIX_RE.search(prefix):
        return True
    suffix = text[match.end():match.end() + 48]
    return bool(_ADVERSE_STATUS_RE.search(suffix))


def _non_negated_matches(text: str, pattern: str) -> List[re.Match]:
    return [
        match
        for match in re.finditer(pattern, text, re.IGNORECASE)
        if not _match_is_negated(text, match)
    ]


_NONPOSSESSORY_EVIDENCE_PREFIX_RE = re.compile(
    r'\b(?:applied|applying|application)\s+(?:for\s+)?(?:an?\s+)?$'
    r'|\beligible\s+for\s+(?:an?\s+)?$'
    r'|\b(?:pursuing|pursuit\s+of|seeking)\s+(?:an?\s+)?$'
    r'|\b(?:observed|watch(?:ed|ing)|shadow(?:ed|ing))\b'
    r'[^\n.;]{0,80}\b(?:using|use|operating)\s*$'
    r'|\b(?:basic|limited|conceptual|theoretical)\s+'
    r'(?:familiarity\s+with|familiar\s+with|exposure\s+to)\s*$',
    re.IGNORECASE,
)
_NONPOSSESSORY_EVIDENCE_SUFFIX_RE = re.compile(
    r'^\s*(?:[-—,:;]\s*)?(?:none|only|not\s+held|'
    r'no\s+(?:direct\s+|hands-on\s+)?(?:use|experience)|'
    r'application\s+(?:submitted|filed|pending|planned|in\s+progress|under\s+review)|'
    r'(?:training|course|certification)\s+'
    r'(?:planned|pending|scheduled|in\s+progress)|'
    r'(?:application\s+)?(?:pending|in\s+progress))\b',
    re.IGNORECASE,
)


def _match_is_affirmative_evidence(text: str, match: re.Match) -> bool:
    """Reject candidate references describing pursuit, eligibility, or observation."""
    if _match_is_negated(text, match):
        return False
    boundary = max(
        text.rfind('\n', 0, match.start()),
        text.rfind('.', 0, match.start()),
        text.rfind(';', 0, match.start()),
    )
    prefix = text[max(boundary + 1, match.start() - 120):match.start()]
    suffix = text[match.end():match.end() + 80]
    return not (
        _NONPOSSESSORY_EVIDENCE_PREFIX_RE.search(prefix)
        or _NONPOSSESSORY_EVIDENCE_SUFFIX_RE.search(suffix)
    )


def _affirmative_evidence_matches(text: str, pattern: str) -> List[re.Match]:
    """Find positive resume evidence without changing JD requirement parsing."""
    return [
        match
        for match in re.finditer(pattern, text, re.IGNORECASE)
        if _match_is_affirmative_evidence(text, match)
    ]


# Patterns for experience types commonly required in clinical research
EXPERIENCE_TYPE_PATTERNS = {
    'monitoring': [
        rf'{_YEAR_VALUE_PATTERN}\s*{_YEAR_UNIT_PATTERN}\s*(?:of\s+)?(?:independent\s+)?(?:on-?site\s+)?(?:clinical\s+)?monitoring',
        rf'(?:independent\s+)?(?:on-?site\s+)?monitoring\s+experience\s*[:\-–]?\s*{_YEAR_VALUE_PATTERN}\s*{_YEAR_UNIT_PATTERN}',
        rf'minimum\s+(?:of\s+)?{_YEAR_VALUE_PATTERN}\s*{_YEAR_UNIT_PATTERN}\s*(?:of\s+)?(?:independent\s+)?monitoring',
        rf'(?:at\s+least|minimum)\s+{_YEAR_VALUE_PATTERN}\s*{_YEAR_UNIT_PATTERN}\s*(?:of\s+)?(?:on-?site\s+)?monitoring',
        rf'{_YEAR_VALUE_PATTERN}\s*{_YEAR_UNIT_PATTERN}\s*(?:of\s+)?(?:on-?site\s+)?monitoring\s+experience',
    ],
    'clinical_research': [
        rf'{_YEAR_VALUE_PATTERN}\s*{_YEAR_UNIT_PATTERN}\s*(?:of\s+)?clinical\s+research\s+experience',
        rf'{_YEAR_VALUE_PATTERN}\s*{_YEAR_UNIT_PATTERN}\s*(?:of\s+)?(?:experience\s+(?:in|with)\s+)?clinical\s+(?:trials?|research)',
    ],
    'clinical_development': [
        rf'{_YEAR_VALUE_PATTERN}\s*{_YEAR_UNIT_PATTERN}\s*(?:of\s+)?clinical\s+development\s+experience',
        rf'{_YEAR_VALUE_PATTERN}\s*{_YEAR_UNIT_PATTERN}\s*(?:of\s+)?(?:experience\s+(?:in|with)\s+)?clinical\s+development',
    ],
    'pharmacovigilance': [
        rf'{_YEAR_VALUE_PATTERN}\s*{_YEAR_UNIT_PATTERN}\s*(?:of\s+)?(?:experience\s+(?:in|with)\s+)?(?:pharmacovigilance|drug\s+safety|pv)',
    ],
    'medical_affairs': [
        rf'{_YEAR_VALUE_PATTERN}\s*{_YEAR_UNIT_PATTERN}\s*(?:of\s+)?(?:experience\s+(?:in|with)\s+)?medical\s+(?:affairs|science\s+liaison)',
    ],
    'oncology': [
        rf'{_YEAR_VALUE_PATTERN}\s*{_YEAR_UNIT_PATTERN}\s*(?:of\s+)?(?:experience\s+(?:in|with)\s+)?oncology',
        rf'oncology\s+(?:monitoring\s+)?experience\s*[:\-–]?\s*{_YEAR_VALUE_PATTERN}\s*{_YEAR_UNIT_PATTERN}',
    ],
    'hematology': [
        rf'{_YEAR_VALUE_PATTERN}\s*{_YEAR_UNIT_PATTERN}\s*(?:of\s+)?'
        rf'(?:experience\s+(?:in|with)\s+)?(?:hematology|haematology|heme[/ -]?onc)',
        rf'(?:hematology|haematology|heme[/ -]?onc)\s+experience\s*[:\-–]?\s*'
        rf'{_YEAR_VALUE_PATTERN}\s*{_YEAR_UNIT_PATTERN}',
    ],
    'regulatory': [
        rf'{_YEAR_VALUE_PATTERN}\s*{_YEAR_UNIT_PATTERN}\s*(?:of\s+)?(?:experience\s+(?:in|with)\s+)?regulatory\s+(?:affairs?|submissions?)',
    ],
    'data_management': [
        rf'{_YEAR_VALUE_PATTERN}\s*{_YEAR_UNIT_PATTERN}\s*(?:of\s+)?(?:experience\s+(?:in|with)\s+)?(?:clinical\s+)?data\s+management',
    ],
    'medical_writing': [
        rf'{_YEAR_VALUE_PATTERN}\s*{_YEAR_UNIT_PATTERN}\s*(?:of\s+)?(?:experience\s+(?:in|with)\s+)?medical\s+writing',
    ],
}

EXPERIENCE_TYPE_TERMS = {
    'monitoring': r'(?:independent\s+)?(?:on-?site\s+)?(?:clinical\s+)?monitoring',
    'clinical_research': (
        r'(?:global\s+)?clinical\s+(?:trials?|research)(?:\s+global)?'
    ),
    'clinical_development': r'clinical\s+development',
    'pharmacovigilance': r'(?:pharmacovigilance|drug\s+safety|pv)',
    'medical_affairs': r'medical\s+(?:affairs|science\s+liaison)',
    'oncology': r'oncology',
    'hematology': r'(?:hematology|haematology|heme[/ -]?onc)',
    'regulatory': r'regulatory\s+(?:affairs?|submissions?)',
    'data_management': r'(?:clinical\s+)?data\s+management',
    'medical_writing': r'medical\s+writing',
}


def _extract_specific_experience_minimums(required_text: str) -> Dict[str, float]:
    """Bind a year quantity to an experience type stated in the same clause."""
    results: Dict[str, float] = {}
    connector = (
        r'\s*(?:(?:of\s+)?experience\s+(?:in|with)\s+|(?:of|in|with)\s+)?'
        r'(?:(?:relevant|direct|hands-on|progressive|professional|industry)\s+)*'
    )
    for clause in re.split(r'[\n.;]+', required_text):
        for exp_type, term in EXPERIENCE_TYPE_TERMS.items():
            patterns = (
                rf'{_YEAR_VALUE_PATTERN}\s*{_YEAR_UNIT_PATTERN}{connector}{term}'
                rf'(?:\s+experience)?',
                rf'{term}\s+experience\s*[:\-–]?\s*{_YEAR_VALUE_PATTERN}\s*'
                rf'{_YEAR_UNIT_PATTERN}',
            )
            values = [
                float(match.group(1))
                for pattern in patterns
                for match in re.finditer(pattern, clause, re.IGNORECASE)
                if float(match.group(1)) > 0
            ]
            if values:
                results[exp_type] = max(results.get(exp_type, 0.0), max(values))
    return results

# Degree hierarchy for comparison
DEGREE_RANK = {
    'high_school': 0,
    'associate': 1,
    'bachelor': 2,
    'master': 3,
    'pharmd': 5,
    'md': 5,
    'phd': 5,
    'doctorate': 5,
}

DEGREE_PATTERNS = {
    # Exact professional/research doctorates stay distinct. Generic
    # "doctorate degree" is represented separately and may accept any of
    # MD, PhD, or PharmD when the JD genuinely uses the generic wording.
    'phd': r'\bph\.?d\.?\b|\bdoctor\s+of\s+philosophy\b',
    'md': r'\bm\.?d\.?\b|\bdoctor\s+of\s+medicine\b|\bmedical\s+degree\b|\bm\.?b\.?b\.?s\.?\b',
    'pharmd': r'\bpharm\.?d\.?\b',
    'doctorate': r'\bdoctorate\s+degree\b|\bdoctoral\s+degree\b',
    'master': r"\bmaster(?:['’]s)?\b|\bm\.?s\.?\b|\bm\.?a\.?\b|\bm\.?b\.?a\.?\b|\bm\.?p\.?h\.?\b|\bm\.?sc\.?\b",
    'bachelor': r"\bbachelor(?:['’]s)?\b|\bb\.?s\.?\b|\bb\.?a\.?\b|\bb\.?sc\.?\b|\bundergraduate\b",
    # Do not interpret the job level in "Associate Director" as a degree.
    # The spelled-out degree requires either a possessive or the word degree;
    # standard A.S./A.A. abbreviations remain accepted.
    'associate': (
        r"\bassociate(?:['’]s|\s+degree)\b"
        r"|\ba\.s\.(?=\s|$)|\ba\.a\.(?=\s|$)"
        r"|\b(?:as|aa)\s+degree\b"
    ),
    'high_school': r'\bhigh\s+school\s+(?:diploma|degree)\b|\bged\b',
}

_DEGREE_EXPERIENCE_LABEL = (
    r"(?:doctorate\s+degree|doctoral\s+degree|ph\.?d\.?(?:\s+degree)?|"
    r"m\.?d\.?(?:\s+degree)?|pharm\.?d\.?(?:\s+degree)?|"
    r"master(?:['’]s|s')?\s+degree|bachelor(?:['’]s|s')?\s+degree|"
    r"associate(?:['’]s|s')?\s+degree|"
    r"high\s+school\s+(?:diploma|degree)(?:\s*/\s*ged)?|ged)"
)
_DEGREE_EXPERIENCE_PATH_RE = re.compile(
    rf"^\s*[•*\-]?\s*(?P<degree>{_DEGREE_EXPERIENCE_LABEL})\s+"
    rf"(?:and|with|\+)\s+(?P<years>\d+(?:\.\d+)?)\+?\s*"
    rf"(?:years?|yrs?\.?)\s*(?:of\s+)?"
    rf"(?P<experience>[a-z][a-z0-9 /&\-]{{0,80}}?)\s+experience\s*[.;]?\s*$",
    re.IGNORECASE,
)
_DEGREE_EXPERIENCE_LIKE_RE = re.compile(
    rf"^\s*[•*\-]?\s*{_DEGREE_EXPERIENCE_LABEL}\b.*"
    rf"\b(?:and|with)\b.*\bexperience\b",
    re.IGNORECASE,
)


def _canonical_degree_experience_label(value: str) -> str:
    normalized = re.sub(r"\s+", " ", value.lower()).strip(" .")
    if re.search(r"\bdoctorate\b|\bdoctoral\b", normalized):
        return 'doctorate'
    if re.search(r"\bph\.?d\.?\b", normalized):
        return 'phd'
    if re.search(r"\bm\.?d\.?\b", normalized):
        return 'md'
    if re.search(r"\bpharm\.?d\.?\b", normalized):
        return 'pharmd'
    if normalized.startswith('master'):
        return 'master'
    if normalized.startswith('bachelor'):
        return 'bachelor'
    if normalized.startswith('associate'):
        return 'associate'
    if 'high school' in normalized or normalized == 'ged':
        return 'high_school'
    return ''


def _canonical_degree_experience_type(value: str) -> str:
    normalized = re.sub(r"[^a-z0-9]+", " ", value.lower()).strip()
    if normalized == 'clinical development':
        return 'clinical_development'
    return ''


def _extract_degree_experience_ladder(
    required_text: str,
) -> Tuple[List[Dict[str, Any]], str, str, bool]:
    """Extract one ordered OR ladder and remove it from hard scalar parsing.

    A ladder is accepted only as a complete alternating sequence of at least
    two path lines separated by standalone ``OR`` lines. If a sequence looks
    like a degree/experience ladder but any route is malformed or unsupported,
    the extraction is marked untrustworthy and the whole attempted block is
    removed so a parseable fragment cannot become an unconditional minimum.
    """
    lines = required_text.splitlines()
    kinds: List[str] = []
    for line in lines:
        stripped = line.strip()
        if re.fullmatch(r'or', stripped, re.IGNORECASE):
            kinds.append('or')
        elif _DEGREE_EXPERIENCE_LIKE_RE.match(stripped):
            kinds.append('path')
        else:
            kinds.append('other')

    blocks: List[Tuple[int, int]] = []
    index = 0
    while index < len(lines):
        if kinds[index] == 'other':
            index += 1
            continue
        end = index
        while end + 1 < len(lines) and kinds[end + 1] != 'other':
            end += 1
        block_kinds = kinds[index:end + 1]
        if 'path' in block_kinds and 'or' in block_kinds:
            blocks.append((index, end))
        index = end + 1

    if not blocks:
        return [], '', required_text, True

    removed: set[int] = set()
    parsed_blocks: List[Tuple[List[Dict[str, Any]], str]] = []
    trustworthy = len(blocks) == 1
    for start, end in blocks:
        removed.update(range(start, end + 1))
        block_kinds = kinds[start:end + 1]
        alternating = (
            len(block_kinds) >= 3
            and block_kinds[0] == 'path'
            and block_kinds[-1] == 'path'
            and all(
                kind == ('path' if offset % 2 == 0 else 'or')
                for offset, kind in enumerate(block_kinds)
            )
        )
        paths: List[Dict[str, Any]] = []
        experience_types: List[str] = []
        if alternating:
            for line_index in range(start, end + 1, 2):
                match = _DEGREE_EXPERIENCE_PATH_RE.fullmatch(lines[line_index].strip())
                if not match:
                    trustworthy = False
                    continue
                degree = _canonical_degree_experience_label(match.group('degree'))
                experience_type = _canonical_degree_experience_type(
                    match.group('experience')
                )
                years = float(match.group('years'))
                if not degree or not experience_type or years <= 0:
                    trustworthy = False
                    continue
                paths.append({'degree': degree, 'years': years})
                experience_types.append(experience_type)
        else:
            trustworthy = False

        if (
            len(paths) < 2
            or len(paths) != block_kinds.count('path')
            or len(set(experience_types)) != 1
            or len({path['degree'] for path in paths}) != len(paths)
        ):
            trustworthy = False
        elif alternating:
            parsed_blocks.append((paths, experience_types[0]))

    remaining_text = '\n'.join(
        line for line_index, line in enumerate(lines) if line_index not in removed
    )
    if not trustworthy or len(parsed_blocks) != 1:
        return [], '', remaining_text, False
    paths, experience_type = parsed_blocks[0]
    return paths, experience_type, remaining_text, True

CERTIFICATION_PATTERNS = [
    r'(?:ACRP|CCRA|CCRC)\b',
    r'(?:SOCRA|CCRP)\b',
    r'\b(?:GCP|good\s+clinical\s+practice)\s+(?:certif(?:ied|ication)|certificate)\b',
    r'\bcertif(?:ied|ication)\s+(?:in\s+)?(?:GCP|good\s+clinical\s+practice)\b',
    r'\bCITI\b',
    r'\bACLS\b',
    r'\bBLS\b',
    r'\bECFMG\b',
    r'\bboard\s+certif',
    r'\bPMP\b',
    r'\bRAC(?:[\s-]+(?:US|EU|Drugs|Devices))?\b',
    r'\bRAPS?\b',
    r'\bSAS\b',
    (
        r'\b(?:(?:active|current|valid|unrestricted)\s*(?:[,/]\s*)?(?:and\s+)?)*'
        r'(?:state\s+)?(?:medical|physician)\s+licen[cs](?:e|ure)\b'
    ),
    r'\blicen[cs]e\s+to\s+practice\s+medicine\b',
    r'\blicensed\s+(?:physician|medical\s+doctor|to\s+practice\s+medicine)\b',
    r'\bstate\s+licen[cs]e\s+(?:as\s+)?(?:a\s+)?physician\b',
    r'\blicen[cs](?:e|ure)\s+(?:as\s+)?(?:a\s+)?physician\b',
]

TRAVEL_PATTERN = r'(\d+)\s*%\s*(?:of\s+(?:the\s+)?time\s+)?(?:travel|domestic|international)'

VISA_PATTERNS = [
    r'(?:must\s+be\s+)?(?:legally\s+)?authorized\s+to\s+work',
    r'(?:should\s+)?not\s+require.*?(?:visa\s+)?sponsorship',
    r'no\s+(?:visa\s+)?sponsorship',
    r'without\s+(?:the\s+need\s+for\s+)?(?:visa\s+)?sponsorship',
    r'cannot\s+(?:provide|offer)\s+(?:visa\s+)?sponsorship',
    r'eligible\s+to\s+work\s+(?:in|without)',
]

TOOLS_PATTERNS = [
    r'\b(medidata\s*(?:rave)?)\b', r'\b(rave)\b',
    r'\b(redcap)\b', r'\b(oracle\s+siebel)\b',
    r'\b(veeva)\b', r'\b(salesforce)\b',
    r'\b((?:oracle\s+)?argus(?:\s+safety)?)\b', r'\b(meddra)\b',
    r'\b((?:oracle\s+)?empirica(?:\s+signal)?)\b',
    r'\b(sas)\b', r'\b(spss)\b', r'\b(stata)\b',
    r'\b(python)\b', r'\b(r\s+programming|r\s+studio)\b',
    r'\b(sql)\b', r'\b(tableau)\b',
    r'\b(e-?clinical\s*works)\b', r'\b(epic)\b', r'\b(cerner)\b',
    r'\b(ctms)\b', r'\b(edc)\b', r'\b(etmf)\b', r'\b(ivrs)\b',
    r'\b(microsoft\s+(?:word|excel|powerpoint|office))\b',
    r'\b(sharepoint)\b', r'\b(jira)\b',
]


def _canonical_tool_name(value: str) -> str:
    normalized = re.sub(r'\s+', ' ', value.lower()).strip()
    if 'argus' in normalized:
        return 'argus'
    if normalized == 'meddra':
        return 'meddra'
    if 'empirica' in normalized:
        return 'empirica signal'
    return normalized

# Section scoping for tool requirements. A tool named in duties prose
# ("routine and ad hoc data mining in Empirica Signal") describes what the
# hire will DO; it is not a candidate requirement. Only requirement sections
# and unattributed lines may ground a required-tool knockout.
_TOOL_REQUIREMENT_HEADING = re.compile(
    r'^(?:(?:basic|minimum|required|must\s+have|essential)\s+)?'
    r'(?:qualifications?|requirements?)\s*:?$'
    r'|^required\s+skills(?:\s+(?:and|&)\s+experience)?\s*:?$'
    r'|^(?:your\s+(?:background|qualifications)|who\s+you\s+are|'
    r'what\s+you(?:\'ll|\s+will)\s+need|'
    r'education\s+(?:and|&)\s+experience)\s*:?$',
    re.IGNORECASE,
)
_TOOL_PREFERRED_HEADING = re.compile(
    r'^(?:preferred|desired|bonus|nice\s+to\s+have)(?:\s+[a-z&]{1,12}){0,3}\s*:?$',
    re.IGNORECASE,
)
_TOOL_DUTY_HEADING = re.compile(
    r'^(?:(?:principal|key|primary|major|essential|main|core)\s+)?'
    r'(?:responsibilities|duties|functions|activities)\s*:?$'
    r'|^(?:what\s+you(?:\'ll|\s+will)\s+do|your\s+role|the\s+role|'
    r'role\s+overview|about\s+the\s+(?:job|role|position|company|team)|'
    r'about\s+us|position\s+(?:summary|overview|description)|'
    r'job\s+(?:summary|overview|description)|'
    r'position\s+at\s+a\s+glance|overview)\s*:?$',
    re.IGNORECASE,
)


def _tool_requirement_search_text(jd_text: str, pref_section: str) -> str:
    """Return JD lines that may state required tool/platform experience.

    Duty/responsibilities sections are excluded: they describe the job, not
    the candidate screen. Preferred sections and individually preferred lines
    are excluded as well. Lines outside any recognized heading keep the
    legacy behavior (treated as requirement-eligible).
    """
    pref_lines = {
        line.strip().lower()
        for line in pref_section.splitlines()
        if line.strip()
    }
    kept: List[str] = []
    section = None  # 'requirement' | 'preferred' | 'duty' | None
    for raw_line in jd_text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if _TOOL_REQUIREMENT_HEADING.fullmatch(line):
            section = 'requirement'
            continue
        if _TOOL_PREFERRED_HEADING.fullmatch(line):
            section = 'preferred'
            continue
        if _TOOL_DUTY_HEADING.fullmatch(line):
            section = 'duty'
            continue
        if section in {'duty', 'preferred'}:
            continue
        if line.lower() in pref_lines:
            continue
        kept.append(line)
    return '\n'.join(kept)


def _canonical_credential(value: str) -> str:
    normalized = re.sub(r'\s+', ' ', value.lower()).strip()
    if re.search(r'\bgcp\b|good clinical practice', normalized):
        return 'gcp certification'
    if re.search(r'\bboard\s+certif', normalized):
        return 'board certification'
    if re.search(r'\brac\b', normalized):
        return 'rac certification'
    if re.search(
        r'(?:medical|physician)\s+licen[cs]|licen[cs](?:e|ed)\s+to\s+practice\s+medicine|'
        r'licensed\s+physician|licen[cs](?:e|ure)\s+(?:as\s+)?(?:a\s+)?physician',
        normalized,
    ):
        return 'medical license'
    return normalized


_LANGUAGE_NAMES = (
    'arabic', 'cantonese', 'english', 'french', 'german', 'hindi',
    'italian', 'japanese', 'korean', 'mandarin', 'portuguese', 'russian',
    'spanish', 'tamil',
)
_LANGUAGE_ALTERNATION = '|'.join(_LANGUAGE_NAMES)


def _extract_required_languages(required_text: str) -> List[str]:
    """Extract explicit language-fluency requirements from hard JD text."""
    patterns = (
        rf'\b(?:fluent|fluency|proficient|proficiency|bilingual)\s+(?:in\s+)?'
        rf'(?P<language>{_LANGUAGE_ALTERNATION})\b',
        rf'\b(?P<language>{_LANGUAGE_ALTERNATION})\s+(?:language\s+)?'
        rf'(?:fluency|proficiency|required)\b',
        rf'\b(?:native|professional|business|advanced)\s+(?:speaker|fluency|proficiency)'
        rf'\s+(?:in\s+)?(?P<language>{_LANGUAGE_ALTERNATION})\b',
    )
    found: List[Tuple[int, str]] = []
    for pattern in patterns:
        for match in _non_negated_matches(required_text, pattern):
            found.append((match.start(), match.group('language').lower()))
    found.sort(key=lambda item: item[0])
    return _ordered_unique([language for _, language in found])


def _has_unparsed_required_language(required_text: str, parsed: List[str]) -> bool:
    """Fail closed when a hard language-fluency clause cannot be identified."""
    if parsed:
        return False
    return bool(re.search(
        r'\b(?:language\s+fluency|fluen(?:t|cy)\s+in|bilingual)\b',
        required_text,
        re.IGNORECASE,
    ))


def _degree_mentions(text: str) -> List[Tuple[int, str]]:
    mentions: List[Tuple[int, str]] = []
    for degree, pattern in DEGREE_PATTERNS.items():
        mentions.extend(
            (match.start(), degree)
            for match in re.finditer(pattern, text, re.IGNORECASE)
        )
    mentions.sort(key=lambda item: item[0])
    return mentions


def _extract_degree_requirement(required_text: str) -> Tuple[str, List[str]]:
    mentions = _degree_mentions(required_text)
    if not mentions:
        return '', []
    required_degree = max(
        (degree for _, degree in mentions),
        key=lambda degree: DEGREE_RANK.get(degree, 0),
    )
    acceptable = [required_degree]
    for clause in re.split(r'[\n.;]+', required_text):
        clause_mentions = _degree_mentions(clause)
        clause_degrees = _ordered_unique([degree for _, degree in clause_mentions])
        if (
            required_degree in clause_degrees
            and len(clause_degrees) > 1
            and re.search(r'\bor\b|/', clause, re.IGNORECASE)
        ):
            acceptable = clause_degrees
            break
    return required_degree, acceptable


_EXACT_DEGREE_REQUIREMENTS = frozenset({'md', 'phd', 'pharmd'})
_GENERIC_DOCTORATE_DEGREES = frozenset({'doctorate', 'md', 'phd', 'pharmd'})


def _candidate_degree_satisfies(candidate_degree: str, requirement: str) -> bool:
    if requirement in _EXACT_DEGREE_REQUIREMENTS:
        return candidate_degree == requirement
    if requirement == 'doctorate':
        return candidate_degree in _GENERIC_DOCTORATE_DEGREES
    return DEGREE_RANK.get(candidate_degree, -1) >= DEGREE_RANK.get(
        requirement, 0
    )


def _select_degree_experience_path(
    profile: EnrichedProfile,
    req: ExtractedRequirements,
) -> Optional[Dict[str, Any]]:
    """Choose the lowest-years route the candidate's education satisfies."""
    if not req.degree_experience_paths:
        return None
    candidate_degrees = list(
        profile.degrees
        or ([profile.highest_degree] if profile.highest_degree else [])
    )
    eligible: List[Tuple[float, int, Dict[str, Any]]] = []
    for route_index, route in enumerate(req.degree_experience_paths):
        requirement = str(route.get('degree', ''))
        try:
            years = float(route.get('years', 0))
        except (TypeError, ValueError):
            continue
        if years <= 0:
            continue
        if any(
            _candidate_degree_satisfies(candidate_degree, requirement)
            for candidate_degree in candidate_degrees
        ):
            eligible.append((years, route_index, route))
    if not eligible:
        return None
    # The route index is the deterministic tie-breaker and preserves JD order.
    return min(eligible, key=lambda item: (item[0], item[1]))[2]


def _candidate_meets_degree_requirement(
    profile: EnrichedProfile,
    req: ExtractedRequirements,
) -> bool:
    if not req.required_degree:
        return True
    candidate_degrees = set(profile.degrees or ([profile.highest_degree] if profile.highest_degree else []))
    acceptable = req.acceptable_degrees or [req.required_degree]
    for requirement in acceptable:
        if any(
            _candidate_degree_satisfies(degree, requirement)
            for degree in candidate_degrees
        ):
            return True
    return False

SENIORITY_KEYWORDS = {
    'entry': ['entry', 'junior', 'associate', 'trainee', 'intern', 'assistant', 'i\b', 'level 1'],
    'mid': ['mid', 'ii\b', 'level 2', 'specialist', 'coordinator'],
    'senior': ['senior', 'sr', 'iii\b', 'level 3', 'lead', 'principal'],
    'director': ['director', 'head of', 'vp', 'vice president', 'executive'],
}

THERAPEUTIC_AREA_PATTERNS = [
    r'\b(oncology|hematology|immuno-?oncology)\b',
    r'\b(cardiology|cardiovascular)\b',
    r'\b(neurology|neuroscience|cns)\b',
    r'\b(hiv|infectious\s+disease)\b',
    r'\b(immunology|autoimmune)\b',
    r'\b(rare\s+disease|orphan)\b',
    r'\b(dermatology)\b',
    r'\b(ophthalmology|retina)\b',
    r'\b(gastroenterology|gi\b)\b',
    r'\b(respiratory|pulmonary)\b',
    r'\b(endocrinology|metabolic|diabetes)\b',
    r'\b(pediatric)\b',
    r'\b(medical\s+device)\b',
]


def extract_requirements(
    jd_text: str,
    *,
    semantic_enabled: bool = True,
) -> ExtractedRequirements:
    """Extract structured requirements from a job description."""
    req = ExtractedRequirements()
    jd_lower = jd_text.lower()
    # Both typographic and ASCII possessives are common in phrases such as
    # "5+ years' experience".  Normalize only that grammatical form so the
    # experience patterns remain readable and deterministic.
    jd_lower = re.sub(r"(?<=years)['’](?=\s+experience\b)", "", jd_lower)
    jd_lower = _normalize_experience_number_words(jd_lower)
    req_section, pref_section = _split_required_vs_preferred(jd_text)
    req_lower = (req_section.lower() if req_section else jd_lower)
    req_lower = re.sub(r"(?<=years)['’](?=\s+experience\b)", "", req_lower)
    req_lower = _normalize_experience_number_words(req_lower)
    (
        req.degree_experience_paths,
        req.degree_experience_type,
        scalar_req_lower,
        ladder_trustworthy,
    ) = _extract_degree_experience_ladder(req_lower)
    confidence_signals = 0

    # --- Title & Company ---
    lines = [l.strip() for l in jd_text.split('\n') if l.strip()]
    req.title = _extract_job_title(jd_text, lines)
    # Company from second line or pattern
    for line in lines[:5]:
        company_match = re.search(
            r'^([A-Z][A-Za-z\s&\.]+?)(?:\s*[|·•–—]|\s+is\s|\s+plc|\s+Inc)', line
        )
        if company_match and company_match.group(1).strip() != req.title:
            req.company = company_match.group(1).strip()
            break

    # --- Domain Detection ---
    domain, confidence, _ = detect_domain(
        jd_text, semantic_enabled=semantic_enabled
    )
    req.domain = domain
    req.domain_confidence = confidence

    # --- Total Years of Experience ---
    req.min_years_total = _extract_total_experience_minimum(scalar_req_lower)
    if req.min_years_total > 0:
        confidence_signals += 1
    req.extraction_trustworthy = bool(ladder_trustworthy) and not (
        _has_unparsed_hard_experience_minimum(scalar_req_lower)
    )

    # --- Specific Experience Types ---
    for exp_type, patterns in EXPERIENCE_TYPE_PATTERNS.items():
        values: List[float] = []
        for pattern in patterns:
            values.extend(
                float(match.group(1))
                for match in re.finditer(pattern, scalar_req_lower, re.IGNORECASE)
                if float(match.group(1)) > 0
            )
        if values:
            req.min_years_specific[exp_type] = max(values)
            confidence_signals += 1
    for exp_type, years in _extract_specific_experience_minimums(
        scalar_req_lower
    ).items():
        previous = req.min_years_specific.get(exp_type, 0.0)
        req.min_years_specific[exp_type] = max(previous, years)
        if previous <= 0:
            confidence_signals += 1
    if req.min_years_specific:
        # Specific experience is necessarily experience in total. Preserve the
        # strongest hard floor even when the wording places the domain between
        # the year quantity and the word "experience" (for example,
        # "10 years of pharmacovigilance experience").
        req.min_years_total = max(
            req.min_years_total,
            max(req.min_years_specific.values()),
        )

    # Also check for "independent monitoring" as a keyword even without years
    if 'monitoring' not in req.min_years_specific:
        if re.search(r'independent\s+(?:site\s+)?monitoring', scalar_req_lower):
            req.min_years_specific['monitoring'] = req.min_years_total or 1.0
            confidence_signals += 1

    # --- Degree Requirements ---
    if req.degree_experience_paths:
        route_degrees = [
            str(path['degree']) for path in req.degree_experience_paths
        ]
        req.required_degree = max(
            route_degrees,
            key=lambda degree: DEGREE_RANK.get(degree, 0),
        )
        req.acceptable_degrees = route_degrees
        confidence_signals += 1
    else:
        req.required_degree, req.acceptable_degrees = _extract_degree_requirement(
            scalar_req_lower
        )
        # Exact doctorates named anywhere in the requirements are alternative
        # routes in a ladder (e.g., a PharmD/PhD bullet followed by an MD
        # bullet). The clause-local "or" rule above misses routes split
        # across bullets, so union doctorate mentions from requirement text.
        if req.required_degree in _EXACT_DEGREE_REQUIREMENTS:
            for _, degree in _degree_mentions(scalar_req_lower):
                if (
                    degree in _EXACT_DEGREE_REQUIREMENTS
                    and degree not in req.acceptable_degrees
                ):
                    req.acceptable_degrees.append(degree)
    if req.required_degree:
        confidence_signals += 1
    # Degrees named in preferred prose describe acceptable alternative routes
    # (e.g., "PharmD or PhD ...; MD ... is preferred" — a trailing qualifier
    # must not erase the MD route). Preferred mentions can only widen the
    # acceptable list; they never create or harden a requirement.
    if req.required_degree and pref_section:
        for _, degree in _degree_mentions(pref_section):
            if degree not in req.acceptable_degrees:
                req.acceptable_degrees.append(degree)

    # --- Certifications ---
    # Search the REQUIRED section only so "preferred" / "desired" certs (e.g.,
    # Takeda's "Board Certification in therapeutic area of interest" listed
    # under Desired) don't get treated as hard requirements.
    cert_search_text = req_section if req_section else jd_text
    pref_lower = pref_section.lower() if pref_section else ''
    for cert_pattern in CERTIFICATION_PATTERNS:
        matches = _non_negated_matches(cert_search_text, cert_pattern)
        if matches:
            cert_str = _canonical_credential(matches[0].group(0))
            # Sanity check: skip if this exact phrase also appears in the
            # preferred/desired section — likely a duplicate hit.
            if pref_lower and cert_str.lower() in pref_lower:
                continue
            req.required_certifications.append(cert_str)
    req.required_certifications = _ordered_unique(req.required_certifications)

    # --- Required Language Fluency ---
    # Fluency is a candidate qualification, not an ATS keyword. Only an
    # explicit positive proficiency claim in the master can satisfy it.
    req.required_languages = _extract_required_languages(cert_search_text)
    if _has_unparsed_required_language(cert_search_text, req.required_languages):
        req.extraction_trustworthy = False
    if req.required_languages:
        confidence_signals += 1

    # --- Travel ---
    travel_match = re.search(TRAVEL_PATTERN, jd_lower)
    if travel_match:
        req.travel_requirement = float(travel_match.group(1))
        confidence_signals += 1

    # --- Visa ---
    for visa_pattern in VISA_PATTERNS:
        if re.search(visa_pattern, jd_lower):
            req.visa_sponsorship_available = False
            confidence_signals += 1
            break

    # --- Location Type ---
    location_search_text = scalar_req_lower
    if re.search(r'\b(?:fully\s+)?remote\b', location_search_text):
        req.location_type = 'remote'
    elif re.search(r'\bhybrid\b', location_search_text):
        req.location_type = 'hybrid'
    elif re.search(r'\bon-?site\b|\bin-?office\b|\bin-?person\b', location_search_text):
        req.location_type = 'on-site'
        req.location_mandatory = bool(re.search(
            r'\b(?:requires?|must|mandatory|required|full[- ]time)\b[^\n.;]{0,80}'
            r'\b(?:on-?site|in-?office|in-?person)\b'
            r'|\b(?:on-?site|in-?office|in-?person)\b[^\n.;]{0,50}'
            r'\b(?:presence|role|position|required|mandatory)\b',
            location_search_text,
            re.IGNORECASE,
        ))
    req.relocation_required = bool(re.search(
        r'\b(?:must\s+(?:be\s+willing\s+to\s+)?|required\s+to\s+|'
        r'willing(?:ness)?\s+to\s+)relocat(?:e|ion)\b'
        r'|\brelocation\s+(?:is\s+)?required\b',
        location_search_text,
        re.IGNORECASE,
    ))

    # --- Skills (required vs preferred) ---
    jd_keywords = extract_jd_keywords(jd_text, domain=domain)
    all_skills = sorted(
        _ordered_unique(
            jd_keywords.get('keywords', []) + jd_keywords.get('phrases', [])
        ),
        key=lambda skill: (skill.lower(), skill),
    )

    if pref_section:
        pref_lower = pref_section.lower()
        for skill in all_skills:
            if skill in pref_lower:
                req.preferred_skills.append(skill)
            else:
                req.required_skills.append(skill)
    else:
        req.required_skills = all_skills

    # --- Therapeutic Areas ---
    for ta_pattern in THERAPEUTIC_AREA_PATTERNS:
        match = re.search(ta_pattern, jd_lower)
        if match:
            req.therapeutic_areas.append(match.group(1).lower())
    req.therapeutic_areas = _ordered_unique(req.therapeutic_areas)

    # --- Tools & Platforms ---
    # Search requirement-eligible lines only: duties prose ("data mining in
    # Empirica Signal") and preferred sections never ground a hard knockout.
    tool_search_text = _tool_requirement_search_text(jd_text, pref_section).lower()
    for tool_pattern in TOOLS_PATTERNS:
        matches = _non_negated_matches(tool_search_text, tool_pattern)
        if matches:
            tool_str = _canonical_tool_name(matches[0].group(1))
            if pref_lower and tool_str in pref_lower:
                continue
            req.tools_platforms.append(tool_str)
    req.tools_platforms = _ordered_unique(req.tools_platforms)

    # --- Seniority ---
    req.seniority = _detect_seniority(req.title, req.min_years_total)

    # --- Confidence ---
    req.extraction_confidence = min(100.0, confidence_signals * 15)

    return req


_TITLE_LABEL_RE = re.compile(
    r'\bjob\s*title\s*:\s*(?P<title>[^|\n]+)', re.IGNORECASE
)

# Page furniture that job boards emit above the real title.
_TITLE_BOILERPLATE_RE = re.compile(
    r'^(?:about\s+(?:the\s+)?(?:job|us|the\s+role|this\s+role)|at\s+a\s+glance'
    r'|job\s+description|position\s+summary|overview|the\s+role|role\s+overview'
    r'|who\s+we\s+are|our\s+company|summary|description|apply\s+now'
    r'|full[-\s]?time|part[-\s]?time|remote|hybrid|on[-\s]?site)\s*:?$',
    re.IGNORECASE,
)

# A bare organization name is not a job title.
_COMPANY_ONLY_RE = re.compile(
    r'^[^,|]*\b(?:inc|llc|l\.l\.c|ltd|limited|plc|corp|corporation|company|co|'
    r'gmbh|ag|s\.a|n\.v|therapeutics|pharmaceuticals|pharma|biosciences|'
    r'bioscience|labs|laboratories|holdings|group|partners|health|healthcare)\b'
    r'\.?\s*$',
    re.IGNORECASE,
)

_TITLE_TOKEN_RE = re.compile(
    r'\b(?:director|manager|lead|head|vice\s+president|vp|scientist|physician|'
    r'specialist|associate|coordinator|engineer|analyst|consultant|officer|'
    r'researcher|fellow|intern|nurse|advisor|liaison|architect|designer|'
    r'developer|strategist|principal|supervisor|administrator|technician|'
    r'writer|reviewer|investigator|counsel|controller|accountant|recruiter)\b',
    re.IGNORECASE,
)


def _clean_title_candidate(candidate: str) -> str:
    """Strip labels, trailing parentheticals, and leading company prefixes."""
    text = candidate.strip()

    label_match = _TITLE_LABEL_RE.search(text)
    if label_match:
        text = label_match.group('title')

    # "(Remote, US, Full-time, $90-110k)" is posting metadata, not a title.
    text = re.sub(r'\s*\([^()]*\)\s*$', '', text).strip()
    text = re.sub(r'\s*[-–—|]\s*(?:remote|hybrid|on[-\s]?site|full[-\s]?time|'
                  r'part[-\s]?time|contract)\b.*$', '', text, flags=re.IGNORECASE)

    # "CentralReach — Clinical AI Evaluation Specialist" keeps only the title
    # half. A prefix that itself names a role ("Sr. Analyst - Healthcare
    # Private Equity") must survive intact.
    separator = re.match(r'^(?P<prefix>[^|]{2,60}?)\s+[–—]\s+(?P<rest>.+)$', text)
    if separator:
        prefix, rest = separator.group('prefix'), separator.group('rest')
        if _TITLE_TOKEN_RE.search(rest) and not _TITLE_TOKEN_RE.search(prefix):
            text = rest

    return re.sub(r'\s+', ' ', text).strip(' \t,:;|-')


def _is_usable_title(candidate: str) -> bool:
    if not candidate or len(candidate) > 160:
        return False
    if _TITLE_BOILERPLATE_RE.match(candidate):
        return False
    if _COMPANY_ONLY_RE.match(candidate) and not _TITLE_TOKEN_RE.search(candidate):
        return False
    if re.search(r'[.!?]\s*$', candidate):
        return False
    return 1 <= len(candidate.split()) <= 18


def _extract_job_title(jd_text: str, lines: Optional[List[str]] = None) -> str:
    """Extract a title announced in Position Summary prose, then fall back."""
    summary_match = re.search(
        r'(?ims)^\s*position\s+summary\s*:?\s*$'
        r'(?P<summary>.*?)(?=^\s*[A-Za-z][^\n:]{1,80}:\s*$|\Z)',
        jd_text,
    )
    title_search_text = summary_match.group('summary') if summary_match else jd_text
    seeking_match = re.search(
        r'\bcurrently\s+seeking\s+an?\s+(?:experienced\s+)?'
        r'(?P<title>[^\n.!?]{2,120}?)\s+to\s+'
        r'(?:lead(?:\s*/\s*support)?|support)\b',
        title_search_text,
        re.IGNORECASE,
    )
    if seeking_match:
        return re.sub(r'\s+', ' ', seeking_match.group('title')).strip(' \t,:;-')

    fallback_lines = lines if lines is not None else [
        line.strip() for line in jd_text.split('\n') if line.strip()
    ]
    # LinkedIn-style postings frequently start with the generic "About the
    # job" label and place the real standalone title immediately before the
    # "What You Will Do" section. Accept that line only when it is compact and
    # title-shaped; ordinary prose before the section must not become a title.
    title_tokens = re.compile(
        r'\b(?:director|manager|lead|head|vice\s+president|vp|scientist|'
        r'physician|specialist|associate|coordinator|engineer|analyst|'
        r'consultant|officer|researcher)\b',
        re.IGNORECASE,
    )
    for line_index, line in enumerate(fallback_lines):
        if not re.fullmatch(r'what\s+you\s+will\s+do\s*:?', line, re.IGNORECASE):
            continue
        if line_index <= 0:
            break
        candidate = fallback_lines[line_index - 1].strip()
        if (
            2 <= len(candidate.split()) <= 18
            and len(candidate) <= 160
            and not re.search(r'[.!?]\s*$', candidate)
            and title_tokens.search(candidate)
        ):
            return candidate
        break

    # An explicit "Job Title:" label wins wherever it appears in the header.
    for line in fallback_lines[:8]:
        if _TITLE_LABEL_RE.search(line):
            labelled = _clean_title_candidate(line)
            if _is_usable_title(labelled):
                return labelled

    # Otherwise take the first header line that is actually title-shaped,
    # skipping page furniture and bare company names. Returning '' is
    # deliberate: title alignment scores an absent title as neutral, which
    # beats scoring the candidate against "About the job".
    for line in fallback_lines[:6]:
        cleaned = _clean_title_candidate(line)
        if _is_usable_title(cleaned) and _TITLE_TOKEN_RE.search(cleaned):
            return cleaned
    for line in fallback_lines[:3]:
        cleaned = _clean_title_candidate(line)
        if _is_usable_title(cleaned):
            return cleaned
    return ''


def _split_required_vs_preferred(jd_text: str) -> Tuple[str, str]:
    """Split required and preferred text without leaking inline preference.

    A sentence such as ``Oncology experience strongly preferred.`` is local to
    that sentence.  It must not silently turn every qualification below it into
    a preference.  Only a standalone preferred-section heading changes the
    persistent section state.
    """
    required_heading = re.compile(
        r'^(?:(?:basic|minimum|required|must\s+have|essential|qualifications|requirements)'
        r'(?:\s+qualifications?|\s+requirements?)?'
        r'|required\s+skills\s+(?:and|&)\s+experience)\s*:?$',
        re.IGNORECASE,
    )
    preferred_heading = re.compile(
        r'^(?:preferred|preferred\s+qualifications?|nice\s+to\s+have|desired|bonus)\s*:?$',
        re.IGNORECASE,
    )
    inline_preferred = re.compile(
        r'\b(?:strongly\s+preferred|preferred|nice\s+to\s+have|desired|advantageous)\b',
        re.IGNORECASE,
    )
    parenthetical_preferred = re.compile(
        r'\((?P<qualifier>[^()]*(?:strongly\s+preferred|preferred|'
        r'nice\s+to\s+have|desired|advantageous)[^()]*)\)',
        re.IGNORECASE,
    )

    required_lines: List[str] = []
    preferred_lines: List[str] = []
    current = 'required'
    for line in jd_text.split('\n'):
        stripped = line.strip()
        if preferred_heading.fullmatch(stripped):
            current = 'preferred'
            preferred_lines.append(line)
            continue
        if required_heading.fullmatch(stripped):
            current = 'required'
            required_lines.append(line)
            continue

        # Classify sentence/semicolon-delimited clauses independently. This
        # preserves "Medical Degree ... . Oncology ... strongly preferred."
        # as one hard sentence plus one preferred sentence.
        clauses = re.split(r'(?<=[.!?])\s+|;\s+', line)
        for clause in clauses:
            if not clause:
                continue
            preferred_qualifiers = list(parenthetical_preferred.finditer(clause))
            hard_remainder = parenthetical_preferred.sub('', clause).strip()
            # A parenthetical preference can narrow a hard years minimum (for
            # example, a preferred trial phase) without making the minimum
            # itself optional. Keep the qualifier soft and the outer clause
            # hard. Other inline-preferred clauses retain the prior behavior.
            if (
                preferred_qualifiers
                and re.search(
                    rf'{_YEAR_VALUE_PATTERN}\s*{_YEAR_UNIT_PATTERN}',
                    hard_remainder,
                    re.IGNORECASE,
                )
            ):
                preferred_lines.extend(
                    match.group('qualifier').strip()
                    for match in preferred_qualifiers
                )
                if current == 'preferred':
                    preferred_lines.append(hard_remainder)
                else:
                    required_lines.append(hard_remainder)
            elif inline_preferred.search(clause):
                preferred_lines.append(clause)
            elif current == 'preferred':
                preferred_lines.append(clause)
            else:
                required_lines.append(clause)

    return '\n'.join(required_lines), '\n'.join(preferred_lines)


def _detect_seniority(title: str, years_required: float) -> str:
    """Detect seniority level from title and years."""
    title_lower = title.lower()

    for level in ['director', 'senior', 'entry', 'mid']:
        for keyword in SENIORITY_KEYWORDS[level]:
            if re.search(r'\b' + keyword, title_lower):
                return level

    # Infer from years
    if years_required >= 8:
        return 'director'
    elif years_required >= 4:
        return 'senior'
    elif years_required >= 1:
        return 'mid'
    return 'entry'


# =============================================================================
# STEP 2: CANDIDATE PROFILE BUILDER
# =============================================================================

# Experience type classification based on job titles and bullets
TITLE_TO_EXPERIENCE_TYPE = {
    'monitoring': [
        r'\bcra\b', r'\bclinical\s+research\s+associate\b',
        r'\bmonitor\b', r'\bsite\s+management\b',
    ],
    'clinical_research': [
        r'\bresearch\s+(?:associate|assistant|coordinator|scientist)\b',
        r'\bclinical\s+(?:research|trial|study)\b',
        r'\bcrf\s+specialist\b',
    ],
    'pharmacovigilance': [
        r'\bpharmacovigilance\b', r'\bdrug\s+safety\b', r'\bpv\s+(?:scientist|specialist)\b',
        r'\bsafety\s+(?:scientist|associate|officer)\b',
    ],
    'medical_affairs': [
        r'\bmedical\s+(?:science\s+liaison|affairs|advisor|director)\b',
        r'\bmsl\b',
    ],
    'oncology': [
        r'\boncolog',
    ],
    'hematology': [
        r'\bhematolog', r'\bhaematolog', r'\bheme[/ -]?onc\b',
    ],
    'clinical_operations': [
        r'\bclinical\s+operations\b', r'\bclinical\s+(?:ops|project)\b',
    ],
    'medical_practice': [
        r'\bphysician\b', r'\bdoctor\b', r'\bresident\b', r'\bintern\s+medical\b',
        r'\bmedical\s+officer\b', r'\bclinical\s+(?:assistant|care|coordinator)\b',
    ],
    'data_management': [
        r'\bdata\s+(?:manager|management|analyst|scientist)\b',
    ],
    'leadership': [
        r'\bdirector\b', r'\bhead\b', r'\bvp\b', r'\bchief\b',
        r'\blead\b', r'\bmanager\b', r'\bgovernance\b',
    ],
}

_TITLE_ONLY_EXPERIENCE_TYPES = frozenset({'oncology', 'hematology'})

_CLINICAL_DEVELOPMENT_TRIAL_EVIDENCE_RE = re.compile(
    r'\bphase\s*(?:i{1,3}|[1-3])\b|\b(?:clinical|drug)\s+trials?\b',
    re.IGNORECASE,
)
_CLINICAL_DEVELOPMENT_SUPPORT_TITLE_RE = re.compile(
    r'\b(?:clinical\s+)?research\s+(?:associate|assistant)\b'
    r'|\bstudy\s+investigator\b',
    re.IGNORECASE,
)


def _job_qualifies_for_clinical_development(job: JobEntry) -> bool:
    """Conservatively identify tenure applicable to clinical development.

    Direct clinical-development/scientist/research titles qualify by title.
    Research associate/assistant and study-investigator roles require explicit
    Phase I/II/III or clinical/drug-trial evidence in that same role's bullets.
    This excludes academic research, anesthesia operations, and medical
    practice whose wording is merely adjacent to clinical work.
    """
    title = str(getattr(job, 'title', '') or '')
    bullets = ' '.join(getattr(job, 'bullets', []) or [])
    if _CLINICAL_DEVELOPMENT_SUPPORT_TITLE_RE.search(title):
        return bool(_CLINICAL_DEVELOPMENT_TRIAL_EVIDENCE_RE.search(bullets))
    return bool(re.search(
        r'\bclinical\s+development\b|\bclinical\s+scientist\b|'
        r'\bclinical\s+research(?:er|\s+(?!associate\b|assistant\b))',
        title,
        re.IGNORECASE,
    ))


def build_candidate_profile(
    resume_text: str,
    *,
    as_of_date: Optional[date] = None,
    use_cache: bool = True,
) -> EnrichedProfile:
    """Build an enriched candidate profile.

    ``as_of_date`` freezes open-ended job durations for reproducible scoring.
    ``use_cache=False`` gives security-sensitive callers a calculation that is
    independent of mutable process-global cache state.
    """
    if as_of_date is not None:
        if type(as_of_date) is not date:
            raise ValueError("as_of_date must be a date")
        if as_of_date > date.today():
            raise ValueError("as_of_date must not be in the future")
    cache_key = _profile_cache_key(resume_text, as_of_date)
    if use_cache and cache_key in _profile_cache:
        return _profile_cache[cache_key]

    base = parse_resume(resume_text)
    if as_of_date is not None:
        for job in base.jobs:
            if job.start_date:
                # Freeze every role, including closed roles. A future-dated
                # closed-role end may be a typo, but it cannot be allowed to
                # manufacture experience for an authorization decision.
                effective_end = as_of_date
                if not job.is_current and job.end_date is not None:
                    effective_end = min(job.end_date, as_of_date)
                job.duration_months = max(
                    0,
                    (effective_end.year - job.start_date.year) * 12
                    + (effective_end.month - job.start_date.month),
                )
        base.total_years_experience = sum(job.duration_months for job in base.jobs) / 12.0
    profile = EnrichedProfile(base=base)

    # --- Titles Held ---
    profile.titles_held = [job.title for job in base.jobs]

    # --- Years by Type ---
    profile.years_by_type = _calculate_years_by_type(base.jobs)

    # --- Highest Degree ---
    profile.degrees = _infer_degrees(base.education)
    profile.highest_degree = _infer_highest_degree(base.education, resume_text)

    # --- Certifications ---
    profile.certifications = [
        c.lower().strip()
        for c in base.certifications
        if not re.match(
            r'^\W*(?:no|not|never|without|lacks?|do(?:es)?\s+not|is\s+not)\b',
            c,
            re.IGNORECASE,
        )
    ]

    # Candidate-side constraints must be explicit. Absence never means that a
    # candidate needs sponsorship or cannot work on site.
    profile.languages_known = _extract_languages_from_resume(resume_text)
    profile.requires_sponsorship = _resume_requires_sponsorship(resume_text)
    profile.remote_only = _resume_is_remote_only(resume_text)
    profile.cannot_relocate = _resume_cannot_relocate(resume_text)

    # --- Therapeutic Areas ---
    profile.therapeutic_areas = _extract_therapeutic_areas(resume_text)

    # --- Tools Known ---
    profile.tools_known = _extract_tools_from_resume(resume_text)

    # --- Publications Count ---
    pub_count = len(re.findall(
        r'(?:et\s+al\.?|Cureus|Journal|Book\s+Chapter)',
        resume_text, re.IGNORECASE
    ))
    profile.publications_count = max(pub_count, len(re.findall(r'^•\s+.+?(?:Cureus|Journal|Vol\b)', resume_text, re.MULTILINE | re.IGNORECASE)))

    # --- Average Tenure ---
    durations = [j.duration_months for j in base.jobs if j.duration_months > 0]
    profile.avg_tenure_months = sum(durations) / len(durations) if durations else 0

    # --- Location ---
    loc_match = re.search(r'([A-Z][a-z]+(?:\s+[A-Z][a-z]+)*,\s*[A-Z]{2})', resume_text)
    if loc_match:
        profile.location = loc_match.group(1)

    if use_cache:
        _profile_cache[cache_key] = profile
    return profile


_SAFETY_REVIEW_EVIDENCE_RE = re.compile(
    r'\bpharmacovigilance\b'
    r'|\bfaers\b'
    r'|\bsignal\s+detection\b'
    r'|\bsafety\s+(?:trend\s+analysis|surveillance|signal|database)\b'
    r'|\b(?:ae\s*/\s*sae|sae|adverse\s+events?)\b[^.;]{0,60}'
    r'\b(?:assessment|review|reporting|narrative|coding|triage)\b',
    re.IGNORECASE,
)


def _job_qualifies_for_pharmacovigilance(job: JobEntry) -> bool:
    """Credit safety-review tenure evidenced anywhere in that role's bullets.

    Type classification otherwise inspects only the first 200 characters of a
    role's bullets, so a role whose AE/SAE evidence appears further down
    contributed no pharmacovigilance tenure at all. This credits the role's
    own duration and nothing more; a genuine multi-year shortfall must still
    knock out.
    """
    return bool(_SAFETY_REVIEW_EVIDENCE_RE.search(' '.join(job.bullets)))


def _calculate_years_by_type(jobs: List[JobEntry]) -> Dict[str, float]:
    """Classify each job's duration into experience types."""
    years_by_type: Dict[str, float] = {}

    for job in jobs:
        if job.duration_months <= 0:
            continue

        title_lower = job.title.lower()
        company_lower = job.company.lower() if job.company else ""
        bullets_text = ' '.join(job.bullets).lower()

        years = job.duration_months / 12.0
        matched_types = set()

        for exp_type, patterns in TITLE_TO_EXPERIENCE_TYPE.items():
            for pattern in patterns:
                title_match = re.search(pattern, title_lower)
                bullet_match = (
                    exp_type not in _TITLE_ONLY_EXPERIENCE_TYPES
                    and re.search(pattern, bullets_text[:200])
                )
                if title_match or bullet_match:
                    matched_types.add(exp_type)
                    break

        if _job_qualifies_for_clinical_development(job):
            matched_types.add('clinical_development')

        if _job_qualifies_for_pharmacovigilance(job):
            matched_types.add('pharmacovigilance')

        # CRO-specific detection
        if any(cro in company_lower for cro in ['covance', 'labcorp', 'icon', 'iqvia', 'parexel',
                                                   'ppd', 'syneos', 'medpace', 'pra health']):
            matched_types.add('cro_experience')

        # Independent monitoring is a subset — only if title is CRA/Monitor AND at a CRO
        if 'monitoring' in matched_types and 'cro_experience' in matched_types:
            matched_types.add('independent_monitoring')

        # If no specific type matched, classify as general
        if not matched_types:
            matched_types.add('general')

        for exp_type in sorted(matched_types):
            years_by_type[exp_type] = years_by_type.get(exp_type, 0) + years

    return years_by_type


def _infer_degrees(education: list) -> List[str]:
    """Infer degrees only from structured Education-section entries."""
    degree_text = '\n'.join(
        str(getattr(entry, 'degree', '') or '')
        for entry in education
    )
    found: List[str] = []
    for _, degree in _degree_mentions(degree_text):
        pattern = DEGREE_PATTERNS[degree]
        if _non_negated_matches(degree_text, pattern):
            found.append(degree)
    return _ordered_unique(found)


def _infer_highest_degree(education: list, resume_text: str = '') -> str:
    """Infer the highest degree; raw resume prose is intentionally ignored."""
    del resume_text
    degrees = _infer_degrees(education)
    return max(degrees, key=lambda degree: DEGREE_RANK.get(degree, 0), default='')


def _extract_therapeutic_areas(text: str) -> List[str]:
    """Extract therapeutic areas mentioned in resume."""
    areas = []
    text_lower = text.lower()
    for ta_pattern in THERAPEUTIC_AREA_PATTERNS:
        match = re.search(ta_pattern, text_lower)
        if match:
            areas.append(match.group(1))
    return _ordered_unique(areas)


def _extract_tools_from_resume(text: str) -> List[str]:
    """Extract tools and platforms mentioned in resume."""
    tools = []
    text_lower = text.lower()
    for tool_pattern in TOOLS_PATTERNS:
        matches = _affirmative_evidence_matches(text_lower, tool_pattern)
        if matches:
            tools.append(_canonical_tool_name(matches[0].group(1)))
    return _ordered_unique(tools)


def _extract_languages_from_resume(text: str) -> List[str]:
    """Return languages backed by an explicit positive proficiency claim."""
    patterns = (
        rf'\b(?:fluent|proficient|native|bilingual)\s+(?:in\s+)?'
        rf'(?P<language>{_LANGUAGE_ALTERNATION})\b',
        rf'\b(?P<language>{_LANGUAGE_ALTERNATION})\s*[:\-—]\s*'
        rf'(?:native|fluent|professional|business|advanced)\b',
        rf'\b(?:native|professional|business|advanced)\s+(?:speaker|fluency|proficiency)'
        rf'\s+(?:in\s+)?(?P<language>{_LANGUAGE_ALTERNATION})\b',
        rf'\bbilingual\b[^\n.;]{{0,40}}\b(?P<language>{_LANGUAGE_ALTERNATION})\b',
    )
    found: List[Tuple[int, str]] = []
    for pattern in patterns:
        for match in _affirmative_evidence_matches(text, pattern):
            found.append((match.start(), match.group('language').lower()))
    found.sort(key=lambda item: item[0])
    return _ordered_unique([language for _, language in found])


def _resume_requires_sponsorship(text: str) -> bool:
    # Resume candidate-status statements are normally standalone lines or
    # bullets. Anchoring the subjectless/first-person forms prevents prose such
    # as "Employer requires sponsorship" from being misread as candidate state.
    patterns = (
        r'^\s*(?:[•*\-]\s*)?(?:i\s+)?'
        r'(?:require|requires|need|needs|will\s+require|will\s+need)\s+'
        r'(?:(?:employment|work)\s+visa|visa|employment|work)\s+sponsorship\b',
        r'^\s*(?:[•*\-]\s*)?'
        r'(?:(?:employment|work)\s+visa|visa|employment|work)\s+sponsorship\s+'
        r'(?:is\s+)?required\b',
    )
    return any(
        _non_negated_matches(text, f'(?m){pattern}')
        for pattern in patterns
    )


def _resume_is_remote_only(text: str) -> bool:
    patterns = (
        r'\bremote[- ]only\b',
        r'\b(?:can|able|available)\s+(?:to\s+)?work\s+(?:only\s+)?remotely\s+only\b',
        r'\b(?:cannot|can\s+not|unable\s+to|not\s+available\s+to)\s+work\s+'
        r'(?:on-?site|in[- ]person|in[- ]office)\b',
    )
    return any(re.search(pattern, text, re.IGNORECASE) for pattern in patterns)


def _resume_cannot_relocate(text: str) -> bool:
    return bool(re.search(
        r'\b(?:cannot|can\s+not|unable\s+to|not\s+willing\s+to|will\s+not)\s+'
        r'relocate\b|\bno\s+relocation\b',
        text,
        re.IGNORECASE,
    ))


# =============================================================================
# STEP 3: KNOCKOUT DETECTION
# =============================================================================

def _candidate_has_credential(profile: EnrichedProfile, required: str) -> bool:
    canonical = _canonical_credential(required)
    certification_entries = profile.certifications or []

    if canonical == 'gcp certification':
        entry_pattern = r'\b(?:gcp|good\s+clinical\s+practice)\b'
        raw_patterns = (
            r'\b(?:gcp|good\s+clinical\s+practice)\s+(?:certif(?:ied|ication)|certificate)\b',
            r'\bcertif(?:ied|ication)\s+(?:in\s+)?(?:gcp|good\s+clinical\s+practice)\b',
        )
    elif canonical == 'medical license':
        entry_pattern = (
            r'\b(?:(?:state|medical|physician)\s+)*licen[cs](?:e|ure)\b'
            r'|\blicensed\s+physician\b'
            r'|\blicen[cs](?:e|ure)\s+(?:as\s+)?(?:a\s+)?physician\b'
        )
        raw_patterns = (
            r'\b(?:(?:active|current|valid|unrestricted)\s*(?:[,/]\s*)?(?:and\s+)?)*'
            r'(?:state\s+)?(?:medical|physician)\s+licen[cs](?:e|ure)\b',
            r'\blicen[cs]e\s+to\s+practice\s+medicine\b',
            r'\blicensed\s+(?:physician|medical\s+doctor|to\s+practice\s+medicine)\b',
            r'\bstate\s+licen[cs]e\s+(?:as\s+)?(?:a\s+)?physician\b',
            r'\blicen[cs](?:e|ure)\s+(?:as\s+)?(?:a\s+)?physician\b',
        )
    elif canonical == 'board certification':
        entry_pattern = r'\bboard\s+certif(?:ied|ication)\b'
        raw_patterns = (entry_pattern,)
    elif canonical == 'rac certification':
        entry_pattern = r'\brac(?:[\s-]+(?:us|eu|drugs|devices))?\b'
        raw_patterns = (entry_pattern,)
    else:
        entry_pattern = r'\b' + re.escape(canonical).replace(r'\ ', r'\s+') + r'\b'
        raw_patterns = (entry_pattern,)

    if any(
        _affirmative_evidence_matches(entry, entry_pattern)
        for entry in certification_entries
    ):
        return True

    resume_text = profile.base.raw_text if profile.base else ''
    return any(
        _affirmative_evidence_matches(resume_text, pattern)
        for pattern in raw_patterns
    )


def check_knockouts(profile: EnrichedProfile, req: ExtractedRequirements) -> KnockoutResult:
    """Check for hard disqualifiers."""
    knockouts = []

    if not req.extraction_trustworthy:
        knockouts.append(KnockoutFlag(
            category='extraction',
            requirement='Parseable explicit minimum-experience requirement',
            candidate_has='Requirement quantity could not be parsed safely',
            severity='hard',
            fixable=False,
            suggestion='Review the original job description before applying.',
        ))

    # --- Experience Knockouts ---
    for exp_type, years_required in req.min_years_specific.items():
        years_have = profile.years_by_type.get(exp_type, 0)
        # An explicit minimum is a hard screening condition.  A scorer must not
        # silently turn a shortfall into a resume-tailoring opportunity.
        if years_have < years_required:
            knockouts.append(KnockoutFlag(
                category='experience',
                requirement=f'{years_required} years {exp_type.replace("_", " ")} experience',
                candidate_has=f'{years_have:.1f} years',
                severity='hard',
                fixable=False,
                suggestion=_suggest_alternative_for_experience(exp_type),
            ))

    # Total years check
    if req.min_years_total > 0:
        total = profile.base.total_years_experience if profile.base else 0
        if total < req.min_years_total:
            knockouts.append(KnockoutFlag(
                category='experience',
                requirement=f'{req.min_years_total} years total experience',
                candidate_has=f'{total:.1f} years',
                severity='hard',
                fixable=False,
                suggestion='Target roles requiring fewer years of experience.',
            ))

    selected_degree_path = _select_degree_experience_path(profile, req)
    if req.degree_experience_paths and selected_degree_path is not None:
        years_required = float(selected_degree_path['years'])
        experience_type = req.degree_experience_type
        years_have = profile.years_by_type.get(experience_type, 0.0)
        if years_have < years_required:
            knockouts.append(KnockoutFlag(
                category='experience',
                requirement=(
                    f'{years_required} years '
                    f'{experience_type.replace("_", " ")} experience'
                ),
                candidate_has=f'{years_have:.1f} years',
                severity='hard',
                fixable=False,
                suggestion=_suggest_alternative_for_experience(experience_type),
            ))

    # --- Education Knockout ---
    if req.degree_experience_paths:
        if selected_degree_path is None:
            acceptable = [
                str(path['degree']) for path in req.degree_experience_paths
            ]
            shown = ' or '.join(
                degree.replace('_', ' ').upper() for degree in acceptable
            )
            knockouts.append(KnockoutFlag(
                category='education',
                requirement=f'{shown} or equivalent',
                candidate_has=', '.join(degree.upper() for degree in profile.degrees)
                              or profile.highest_degree.upper()
                              or 'Not detected',
                severity='hard',
                fixable=False,
                suggestion='Target roles matching your education level.',
            ))
    elif req.required_degree:
        if not _candidate_meets_degree_requirement(profile, req):
            acceptable = req.acceptable_degrees or [req.required_degree]
            shown = ' or '.join(degree.upper() for degree in acceptable)
            knockouts.append(KnockoutFlag(
                category='education',
                requirement=f'{shown} or equivalent',
                candidate_has=', '.join(degree.upper() for degree in profile.degrees)
                              or profile.highest_degree.upper()
                              or 'Not detected',
                severity='hard',
                fixable=False,
                suggestion='Target roles matching your education level.',
            ))

    # --- Required Language Knockout ---
    if req.required_languages:
        # The resume itself is the evidence for English. Requiring the literal
        # word "english" to appear knocked candidates out of English-language
        # postings for writing their resume in English.
        languages_have = set(profile.languages_known or []) | {'english'}
        missing_languages = [
            language for language in req.required_languages
            if language not in languages_have
        ]
        if missing_languages:
            shown = ', '.join(missing_languages)
            knockouts.append(KnockoutFlag(
                category='language',
                requirement=f'Required language fluency: {shown}',
                candidate_has='Not demonstrated in resume',
                severity='hard',
                fixable=False,
                suggestion=(
                    f'The JD explicitly requires fluency in {shown}; only proceed '
                    f'when that proficiency can be documented truthfully.'
                ),
            ))

    # --- Visa Knockout ---
    if not req.visa_sponsorship_available:
        if profile.requires_sponsorship:
            knockouts.append(KnockoutFlag(
                category='visa',
                requirement='Must be authorized to work without sponsorship',
                candidate_has='Resume explicitly states sponsorship is required',
                severity='hard',
                fixable=False,
                suggestion='Target roles that provide sponsorship or update the master '
                           'only if the stated work-authorization status has changed.',
            ))

    # --- On-site / Relocation Knockout ---
    location_conflicts: List[str] = []
    if req.location_type == 'on-site' and req.location_mandatory:
        if profile.remote_only:
            location_conflicts.append('remote-only availability')
        if profile.cannot_relocate:
            location_conflicts.append('cannot relocate')
    elif req.relocation_required and profile.cannot_relocate:
        location_conflicts.append('cannot relocate')
    if location_conflicts:
        knockouts.append(KnockoutFlag(
            category='location',
            requirement=(
                'Mandatory on-site presence'
                if req.location_type == 'on-site'
                else 'Relocation required'
            ),
            candidate_has=', '.join(_ordered_unique(location_conflicts)),
            severity='hard',
            fixable=False,
            suggestion='Resolve the explicit location constraint before applying.',
        ))

    # --- Travel Knockout ---
    if req.travel_requirement >= 50:
        knockouts.append(KnockoutFlag(
            category='travel',
            requirement=f'{req.travel_requirement}% travel required',
            candidate_has='Not confirmed — verify willingness',
            severity='soft',
            fixable=True,
            suggestion=f'This role requires {req.travel_requirement}% travel. '
                       f'Ensure you can commit before applying.',
        ))

    # --- Required Tools / Platforms Knockout ---
    # Tools that the JD lists as required (e.g., Veeva Vault, Argus, Medidata
    # Rave, EDC, MedDRA) are real screen-outs in pharma. If the candidate has
    # no demonstrated experience, this is a hard knockout — not a soft "missing
    # skill". The candidate can claim "trainable" but the recruiter usually
    # filters before that conversation happens.
    if req.tools_platforms:
        tools_have_norm = {
            _canonical_tool_name(tool) for tool in (profile.tools_known or [])
        }
        missing_required = []
        for tool in req.tools_platforms:
            tool_lower = _canonical_tool_name(tool)
            if not tool_lower:
                continue
            has_tool = tool_lower in tools_have_norm
            if not has_tool:
                missing_required.append(tool)
        if missing_required:
            shown = ', '.join(missing_required[:4]) + ('…' if len(missing_required) > 4 else '')
            knockouts.append(KnockoutFlag(
                category='tools',
                requirement=f'Proficiency in {shown}',
                candidate_has='Not demonstrated in resume',
                severity='hard',
                fixable=False,
                suggestion=(
                    f'JD requires hands-on use of {shown}. Either acquire training '
                    f'(Veeva Vault Foundations, Oracle Argus tutorials, Medidata U), '
                    f'add a contract gig that uses the platform, or target roles '
                    f'that do not require this specific tool.'
                ),
            ))

    # --- Required Certifications Knockout ---
    # JDs that explicitly require a certification (board cert, GCP cert,
    # specific licensure) screen out candidates lacking it. Soft "preferred"
    # certs are NOT knockouts — only items the JD frames as required.
    if req.required_certifications:
        missing_certs = []
        for cert in req.required_certifications:
            if not cert.strip():
                continue
            if _candidate_has_credential(profile, cert):
                continue
            missing_certs.append(cert)
        if missing_certs:
            shown = ', '.join(missing_certs[:3]) + ('…' if len(missing_certs) > 3 else '')
            knockouts.append(KnockoutFlag(
                category='certification',
                requirement=f'Required certification: {shown}',
                candidate_has='Not present in resume',
                severity='hard',
                fixable=False,
                suggestion=(
                    f'Required certification ({shown}) is not on the resume. '
                    f'Either pursue the credential, target roles that do not '
                    f'require it, or confirm with the recruiter whether '
                    f'equivalent experience is accepted before applying.'
                ),
            ))

    passed = not any(k.severity == 'hard' for k in knockouts)
    return KnockoutResult(passed=passed, knockouts=knockouts)


def _suggest_alternative_for_experience(exp_type: str) -> str:
    """Suggest alternative roles for a missing experience type."""
    suggestions = {
        'monitoring': (
            'You lack CRA monitoring experience. Consider: '
            'CRA Trainee programs (Parexel APEX, Thermo Fisher Academy, IQVIA CRA Program), '
            'Associate CRA / Site Management Associate roles, '
            'or Medical Monitor / Drug Safety Physician roles (leverage your MD).'
        ),
        'independent_monitoring': (
            'You lack independent monitoring experience. '
            'Apply to CRA training programs or Associate CRA roles instead. '
            'Your site-side research experience does not count as CRA monitoring.'
        ),
        'pharmacovigilance': (
            'Consider Drug Safety Associate / PV Specialist entry-level roles, '
            'or PV training programs at CROs.'
        ),
        'medical_affairs': (
            'Consider Associate MSL / Medical Information Specialist roles '
            'that accept candidates with clinical background.'
        ),
        'oncology': (
            'You lack oncology-specific experience. '
            'Target roles in therapeutic areas where you have experience (HIV, critical care).'
        ),
        'data_management': (
            'Your CRF/EDC experience may partially qualify. '
            'Consider Clinical Data Coordinator or CDM Associate roles.'
        ),
    }
    return suggestions.get(exp_type,
                          f'You lack {exp_type.replace("_", " ")} experience. '
                          f'Target roles matching your actual background.')


# =============================================================================
# STEP 4: DIMENSION SCORING
# =============================================================================

def score_fit_dimensions(
    profile: EnrichedProfile,
    req: ExtractedRequirements,
    resume_text: str,
    jd_text: str,
    *,
    semantic_enabled: bool = True,
) -> FitDimensions:
    """Score 7 fit dimensions."""
    dims = FitDimensions()

    dims.experience_match = _score_experience_match(profile, req)
    dims.skills_match = _score_skills_match(
        profile, req, resume_text, jd_text, semantic_enabled=semantic_enabled
    )
    dims.title_alignment = _score_title_alignment(
        profile, req, resume_text, jd_text, semantic_enabled=semantic_enabled
    )
    dims.domain_match = _score_domain_match(
        profile, req, resume_text, jd_text, semantic_enabled=semantic_enabled
    )
    dims.education_match = _score_education_match(profile, req)
    dims.certification_match = _score_certification_match(profile, req)
    dims.seniority_match = _score_seniority_match(profile, req)

    return dims


def _score_experience_match(profile: EnrichedProfile, req: ExtractedRequirements) -> float:
    """Score experience alignment (0-100)."""
    if (
        not req.min_years_specific
        and req.min_years_total <= 0
        and not req.degree_experience_paths
    ):
        # No specific requirements — score based on general experience
        total = profile.base.total_years_experience if profile.base else 0
        return min(100, total * 10)  # Cap at 100

    scores = []

    # Score specific experience types
    for exp_type, years_required in req.min_years_specific.items():
        years_have = profile.years_by_type.get(exp_type, 0)
        if years_required > 0:
            ratio = years_have / years_required
            score = min(100, ratio * 100)
            scores.append(score)

    # Score total years
    if req.min_years_total > 0:
        total = profile.base.total_years_experience if profile.base else 0
        ratio = total / req.min_years_total
        score = min(100, ratio * 100)
        scores.append(score)

    if req.degree_experience_paths:
        selected_path = _select_degree_experience_path(profile, req)
        if selected_path is None:
            scores.append(0.0)
        else:
            years_required = float(selected_path['years'])
            years_have = profile.years_by_type.get(
                req.degree_experience_type, 0.0
            )
            scores.append(min(100, years_have / years_required * 100))

    return sum(scores) / len(scores) if scores else 50.0


def _score_skills_match(
    profile: EnrichedProfile,
    req: ExtractedRequirements,
    resume_text: str,
    jd_text: str,
    *,
    semantic_enabled: bool = True,
) -> float:
    """Score skills overlap (0-100). Uses keyword match + SBERT if available."""
    if not req.required_skills:
        return 50.0

    resume_lower = resume_text.lower()
    matched = 0
    total = len(req.required_skills)

    for skill in req.required_skills:
        if skill.lower() in resume_lower:
            matched += 1

    keyword_score = (matched / total * 100) if total > 0 else 50

    # Boost with SBERT semantic similarity if available
    if semantic_enabled and SBERT_AVAILABLE and sbert_util:
        resume_emb = embed_with_cache(resume_text[:2000])
        jd_emb = embed_with_cache(jd_text[:2000])
        if resume_emb is not None and jd_emb is not None:
            import numpy as np
            cos_sim = float(np.dot(resume_emb, jd_emb) / (
                np.linalg.norm(resume_emb) * np.linalg.norm(jd_emb) + 1e-8
            ))
            semantic_score = max(0, min(100, cos_sim * 100))
            # Blend: 70% keyword, 30% semantic
            return keyword_score * 0.7 + semantic_score * 0.3

    return keyword_score


def _score_title_alignment(
    profile: EnrichedProfile,
    req: ExtractedRequirements,
    resume_text: str,
    jd_text: str,
    *,
    semantic_enabled: bool = True,
) -> float:
    """Score how well past titles align with target title (0-100)."""
    if not req.title:
        return 50.0

    target_lower = req.title.lower()
    best_score = 0

    for title in profile.titles_held:
        title_lower = title.lower()

        # Only normalized equality is exact.  A generic held title such as
        # "Physician" is not an exact match for "Director, Drug Safety
        # Physician" merely because it is a substring.
        if re.sub(r'\W+', ' ', target_lower).strip() == re.sub(r'\W+', ' ', title_lower).strip():
            return 100.0

        # Partial keyword overlap
        target_words = set(re.findall(r'\b\w+\b', target_lower))
        title_words = set(re.findall(r'\b\w+\b', title_lower))
        stop = {'a', 'an', 'the', 'of', 'in', 'at', 'for', 'and', 'or', 'to', 'i', 'ii', 'iii'}
        target_words -= stop
        title_words -= stop

        if target_words:
            overlap = len(target_words & title_words) / len(target_words) * 100
            best_score = max(best_score, overlap)

    # SBERT semantic title similarity
    if semantic_enabled and SBERT_AVAILABLE and sbert_util and best_score < 80:
        target_emb = embed_with_cache(req.title)
        for title in profile.titles_held[:5]:
            title_emb = embed_with_cache(title)
            if target_emb is not None and title_emb is not None:
                import numpy as np
                cos_sim = float(np.dot(target_emb, title_emb) / (
                    np.linalg.norm(target_emb) * np.linalg.norm(title_emb) + 1e-8
                ))
                semantic = max(0, min(100, cos_sim * 100))
                best_score = max(best_score, semantic)

    return best_score


def _score_domain_match(
    profile: EnrichedProfile,
    req: ExtractedRequirements,
    resume_text: str,
    jd_text: str,
    *,
    semantic_enabled: bool = True,
) -> float:
    """Score domain/therapeutic area alignment (0-100)."""
    score = 0.0

    # Therapeutic area overlap
    if req.therapeutic_areas and profile.therapeutic_areas:
        overlap = set(req.therapeutic_areas) & set(profile.therapeutic_areas)
        if overlap:
            score += (len(overlap) / len(req.therapeutic_areas)) * 60
        else:
            score += 10  # Some clinical experience is still relevant
    elif not req.therapeutic_areas:
        score += 40  # No specific TA required — neutral

    # Domain detection match
    resume_domain, _, _ = detect_domain(
        resume_text, semantic_enabled=semantic_enabled
    )
    if resume_domain == req.domain:
        score += 40
    elif resume_domain in ('clinical_research', 'pharma_biotech') and \
         req.domain in ('clinical_research', 'pharma_biotech'):
        score += 30  # Close enough
    else:
        score += 10

    return min(100, score)


def _score_education_match(profile: EnrichedProfile, req: ExtractedRequirements) -> float:
    """Score education alignment (0-100)."""
    if req.degree_experience_paths:
        return 100.0 if _select_degree_experience_path(profile, req) else 0.0

    if not req.required_degree:
        return 80.0  # No specific requirement — neutral-positive

    if not _candidate_meets_degree_requirement(profile, req):
        return 0.0

    acceptable = req.acceptable_degrees or [req.required_degree]
    if any(
        requirement in _EXACT_DEGREE_REQUIREMENTS
        and requirement in set(profile.degrees or [profile.highest_degree])
        for requirement in acceptable
    ):
        return 100.0

    candidate_rank = DEGREE_RANK.get(profile.highest_degree, 0)
    required_rank = min(
        DEGREE_RANK.get(requirement, 0) for requirement in acceptable
    )

    if candidate_rank >= required_rank:
        # Meets or exceeds
        if candidate_rank == required_rank:
            return 100.0
        elif candidate_rank > required_rank + 1:
            return 85.0  # Overqualified — slight concern
        else:
            return 95.0  # One level above — fine
    else:
        # Below requirement
        gap = required_rank - candidate_rank
        return max(0, 100 - gap * 40)


def _score_certification_match(profile: EnrichedProfile, req: ExtractedRequirements) -> float:
    """Score certification match (0-100)."""
    if not req.required_certifications:
        return 80.0  # No certs required

    matched = 0
    for req_cert in req.required_certifications:
        if _candidate_has_credential(profile, req_cert):
            matched += 1

    return (matched / len(req.required_certifications)) * 100


def _score_seniority_match(profile: EnrichedProfile, req: ExtractedRequirements) -> float:
    """Score seniority alignment (0-100)."""
    seniority_rank = {'entry': 0, 'mid': 1, 'senior': 2, 'director': 3, 'vp': 4}

    # Infer candidate seniority from most recent titles
    candidate_seniority = 'mid'
    if profile.titles_held:
        recent_title = profile.titles_held[0].lower()
        for level in ['director', 'senior', 'entry']:
            for kw in SENIORITY_KEYWORDS.get(level, []):
                if re.search(r'\b' + kw, recent_title):
                    candidate_seniority = level
                    break
            if candidate_seniority != 'mid':
                break

    cand_rank = seniority_rank.get(candidate_seniority, 1)
    req_rank = seniority_rank.get(req.seniority, 1)

    diff = abs(cand_rank - req_rank)
    if diff == 0:
        return 100.0
    elif diff == 1:
        return 70.0  # One level off
    elif diff == 2:
        return 40.0  # Two levels off
    else:
        return 15.0  # Very misaligned


# =============================================================================
# STEP 5: GAP ANALYSIS
# =============================================================================

def analyze_gaps(
    profile: EnrichedProfile,
    req: ExtractedRequirements,
    dims: FitDimensions,
) -> GapAnalysis:
    """Identify fixable vs unfixable gaps and suggest alternatives."""
    gaps = GapAnalysis()

    # --- Skills gaps (fixable via resume modification) ---
    if dims.skills_match < 70:
        resume_lower = ' '.join(profile.base.skills).lower() if profile.base else ''
        all_text = profile.base.raw_text.lower() if profile.base else ''

        missing_skills = []
        for skill in req.required_skills[:15]:
            if skill.lower() not in all_text:
                missing_skills.append(skill)

        if missing_skills:
            gaps.fixable_gaps.append(Gap(
                category='skills',
                requirement=f'Skills: {", ".join(missing_skills[:8])}',
                current_state='Not found in resume',
                fixable=True,
                fix_suggestion='Add these to Core Competencies if you have the experience. '
                              'Reframe bullet points to use JD terminology.',
            ))
            gaps.suggested_modifications.append(
                f'Add to Core Competencies: {", ".join(missing_skills[:6])}'
            )

    # --- Experience gaps (unfixable) ---
    for exp_type, years_required in req.min_years_specific.items():
        years_have = profile.years_by_type.get(exp_type, 0)
        if years_have < years_required * 0.7:
            gaps.unfixable_gaps.append(Gap(
                category='experience',
                requirement=f'{years_required} years {exp_type.replace("_", " ")}',
                current_state=f'{years_have:.1f} years',
                fixable=False,
                fix_suggestion=_suggest_alternative_for_experience(exp_type),
            ))

    # --- Title alignment gap (partially fixable) ---
    if dims.title_alignment < 50:
        gaps.fixable_gaps.append(Gap(
            category='title',
            requirement=f'Target title: {req.title}',
            current_state=f'Held titles: {", ".join(profile.titles_held[:3])}',
            fixable=True,
            fix_suggestion='Reframe professional summary to highlight transferable experience. '
                          'Cannot change actual job titles — ethical constraint.',
        ))

    # --- Tools gap (fixable) ---
    if req.tools_platforms:
        missing_tools = [t for t in req.tools_platforms
                        if t.lower() not in ' '.join(profile.tools_known).lower()]
        if missing_tools:
            known = [t for t in req.tools_platforms if t not in missing_tools]
            if len(missing_tools) > len(known):
                gaps.fixable_gaps.append(Gap(
                    category='tools',
                    requirement=f'Tools: {", ".join(missing_tools)}',
                    current_state=f'Known: {", ".join(profile.tools_known[:5])}' if profile.tools_known else 'None listed',
                    fixable=True,
                    fix_suggestion='Add tools you actually know to the Skills section.',
                ))

    # --- Alternative Titles ---
    gaps.alternative_titles = _suggest_alternative_titles(profile, req)

    return gaps


def _suggest_alternative_titles(profile: EnrichedProfile, req: ExtractedRequirements) -> List[str]:
    """Suggest better-fitting job titles based on profile."""
    suggestions = []

    has_md = 'md' in (profile.degrees or [profile.highest_degree])
    has_research = profile.years_by_type.get('clinical_research', 0) > 0
    has_cro = profile.years_by_type.get('cro_experience', 0) > 0
    has_pv = profile.years_by_type.get('pharmacovigilance', 0) > 0
    has_publications = profile.publications_count > 3

    if has_md:
        suggestions.extend([
            'Medical Monitor',
            'Drug Safety Physician',
            'Associate Medical Director',
            'Medical Science Liaison (MSL)',
            'Clinical Scientist',
        ])

    if has_research:
        suggestions.extend([
            'Clinical Research Coordinator',
            'Clinical Trial Associate',
        ])

    if has_cro:
        suggestions.append('CRA Trainee / Associate CRA')

    if has_publications:
        suggestions.extend([
            'Medical Writer',
            'Clinical Scientist',
            'Medical Affairs Specialist',
        ])

    if has_md and has_research:
        suggestions.extend([
            'Medical Reviewer',
            'Clinical Data Reviewer',
        ])

    # Deduplicate
    seen = set()
    unique = []
    for s in suggestions:
        if s.lower() not in seen:
            seen.add(s.lower())
            unique.append(s)

    return unique[:8]


# =============================================================================
# STEP 6: MAIN FUNCTION
# =============================================================================

def calculate_job_fit(
    resume_text: str,
    jd_text: str,
    *,
    semantic_enabled: bool = True,
    as_of_date: Optional[date] = None,
    use_profile_cache: bool = True,
) -> JobFitResult:
    """
    Main entry point. Calculate job fit score with knockout detection.

    Args:
        resume_text: Full master resume text
        jd_text: Job description text

    Returns:
        JobFitResult with score, recommendation, knockouts, dimensions, gaps
    """
    # Step 1: Extract requirements from JD
    req = extract_requirements(jd_text, semantic_enabled=semantic_enabled)

    # Step 2: Build candidate profile
    profile = build_candidate_profile(
        resume_text,
        as_of_date=as_of_date,
        use_cache=use_profile_cache,
    )

    # Step 3: Check knockouts
    knockout_result = check_knockouts(profile, req)

    # Step 4: Score dimensions
    dims = score_fit_dimensions(
        profile,
        req,
        resume_text,
        jd_text,
        semantic_enabled=semantic_enabled,
    )

    # Step 5: Gap analysis
    gaps = analyze_gaps(profile, req, dims)

    # Step 6: Calculate overall score
    overall = dims.weighted_score()

    # Apply knockout penalty
    if not knockout_result.passed:
        overall = min(overall, 35.0)  # Cap at 35 if hard knockouts exist

    # Determine recommendation
    if not knockout_result.passed:
        recommendation = 'NO-GO'
    elif overall >= 75:
        recommendation = 'STRONG FIT'
    elif overall >= 55:
        recommendation = 'MODERATE FIT'
    elif overall >= 35:
        recommendation = 'WEAK FIT'
    else:
        recommendation = 'POOR FIT'

    # Estimate ATS/HR ranges based on fit dimensions
    est_ats = _estimate_ats_range(dims, req)
    est_hr = _estimate_hr_range(dims, profile)

    return JobFitResult(
        overall_score=round(overall, 1),
        recommendation=recommendation,
        knockouts=knockout_result,
        dimensions=dims,
        gap_analysis=gaps,
        requirements=req,
        estimated_ats_range=est_ats,
        estimated_hr_range=est_hr,
    )


def _estimate_ats_range(dims: FitDimensions, req: ExtractedRequirements) -> Tuple[float, float]:
    """Estimate likely ATS score range after tailoring."""
    base = dims.skills_match * 0.6 + dims.domain_match * 0.2 + dims.experience_match * 0.2
    # Skills match is the main ATS driver — if skills overlap is high, tailoring can boost it
    low = max(30, base * 0.75)
    high = min(95, base * 1.15)
    return (round(low, 0), round(high, 0))


def _estimate_hr_range(dims: FitDimensions, profile: EnrichedProfile) -> Tuple[float, float]:
    """Estimate likely HR score range."""
    base = (dims.experience_match * 0.3 + dims.title_alignment * 0.2 +
            dims.seniority_match * 0.2 + dims.skills_match * 0.15 +
            dims.education_match * 0.15)
    # Job hopping penalty estimate
    penalty = 0
    if profile.avg_tenure_months > 0 and profile.avg_tenure_months < 18:
        penalty = 8
    low = max(30, base * 0.75 - penalty)
    high = min(90, base * 1.1 - penalty * 0.5)
    return (round(low, 0), round(high, 0))


# =============================================================================
# CLI
# =============================================================================

def format_report(result: JobFitResult) -> str:
    """Format a human-readable report."""
    lines = []

    # Header
    rec = result.recommendation
    score = result.overall_score
    lines.append('=' * 65)
    lines.append(f'  JOB FIT SCORE: {score}/100 — {rec}')
    lines.append('=' * 65)

    # Requirements detected
    req = result.requirements
    if req:
        lines.append(f'\n  Job: {req.title}')
        if req.company:
            lines.append(f'  Company: {req.company}')
        lines.append(f'  Domain: {req.domain} (confidence: {req.domain_confidence:.0f}%)')
        lines.append(f'  Seniority: {req.seniority}')
        if req.min_years_total > 0:
            lines.append(f'  Years Required: {req.min_years_total}+')
        if req.min_years_specific:
            for exp_type, years in req.min_years_specific.items():
                lines.append(f'  Specific: {years}+ years {exp_type.replace("_", " ")}')
        if req.travel_requirement > 0:
            lines.append(f'  Travel: {req.travel_requirement}%')
        if not req.visa_sponsorship_available:
            lines.append('  Visa: No sponsorship available')

    # Knockouts
    if result.knockouts and result.knockouts.knockouts:
        lines.append(f'\n  KNOCKOUTS ({len(result.knockouts.knockouts)} found):')
        for k in result.knockouts.knockouts:
            icon = 'X' if k.severity == 'hard' else '!'
            lines.append(f'  [{icon}] {k.requirement} — you have {k.candidate_has}')
            lines.append(f'      > {k.suggestion}')
    else:
        lines.append('\n  KNOCKOUTS: None found')

    # Dimensions
    if result.dimensions:
        dims = result.dimensions
        lines.append('\n  DIMENSIONS:')
        lines.append(f'  Experience Match:     {dims.experience_match:5.1f}/100 (25%)')
        lines.append(f'  Skills Match:         {dims.skills_match:5.1f}/100 (25%)')
        lines.append(f'  Title Alignment:      {dims.title_alignment:5.1f}/100 (15%)')
        lines.append(f'  Domain Match:         {dims.domain_match:5.1f}/100 (15%)')
        lines.append(f'  Education Match:      {dims.education_match:5.1f}/100 (10%)')
        lines.append(f'  Certification Match:  {dims.certification_match:5.1f}/100 (5%)')
        lines.append(f'  Seniority Match:      {dims.seniority_match:5.1f}/100 (5%)')

    # Estimated ranges
    lines.append(f'\n  ESTIMATED SCORES (if tailored):')
    lines.append(f'  ATS: {result.estimated_ats_range[0]:.0f}% - {result.estimated_ats_range[1]:.0f}%')
    lines.append(f'  HR:  {result.estimated_hr_range[0]:.0f}% - {result.estimated_hr_range[1]:.0f}%')

    # Gaps
    if result.gap_analysis:
        gaps = result.gap_analysis
        if gaps.unfixable_gaps:
            lines.append('\n  UNFIXABLE GAPS:')
            for g in gaps.unfixable_gaps:
                lines.append(f'  - {g.requirement} (you have: {g.current_state})')
        if gaps.fixable_gaps:
            lines.append('\n  FIXABLE GAPS:')
            for g in gaps.fixable_gaps:
                lines.append(f'  - {g.requirement}')
                lines.append(f'    Fix: {g.fix_suggestion}')
        if gaps.suggested_modifications:
            lines.append('\n  SUGGESTED MODIFICATIONS:')
            for m in gaps.suggested_modifications:
                lines.append(f'  - {m}')
        if gaps.alternative_titles:
            lines.append('\n  BETTER-FIT JOB TITLES:')
            for t in gaps.alternative_titles:
                lines.append(f'  - {t}')

    lines.append('\n' + '=' * 65)
    return '\n'.join(lines)


def main():
    parser = argparse.ArgumentParser(description='Job Fit Scorer — Pre-Application Screening')
    parser.add_argument('--check', nargs=2, metavar=('RESUME', 'JD'),
                       help='Check fit: resume file + JD file')
    parser.add_argument('--json', action='store_true', help='Output JSON instead of report')
    args = parser.parse_args()

    if args.check:
        resume_path, jd_path = args.check
        from text_extractor import extract_text

        resume_text = extract_text(resume_path)
        jd_text = extract_text(jd_path)

        result = calculate_job_fit(resume_text, jd_text)

        if args.json:
            print(json.dumps(result.to_dict(), indent=2))
        else:
            print(format_report(result))
    else:
        parser.print_help()


if __name__ == '__main__':
    main()
