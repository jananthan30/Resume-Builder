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
    required_degree: str = ""  # "bachelor", "master", "md", "phd"
    required_certifications: List[str] = field(default_factory=list)
    travel_requirement: float = 0.0  # percentage
    visa_sponsorship_available: bool = True  # True = they sponsor
    location_type: str = ""  # "remote", "on-site", "hybrid"

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


@dataclass
class EnrichedProfile:
    """Candidate profile enriched with fit-specific data."""
    base: CandidateProfile = None
    years_by_type: Dict[str, float] = field(default_factory=dict)
    highest_degree: str = ""
    titles_held: List[str] = field(default_factory=list)
    therapeutic_areas: List[str] = field(default_factory=list)
    tools_known: List[str] = field(default_factory=list)
    certifications: List[str] = field(default_factory=list)
    publications_count: int = 0
    avg_tenure_months: float = 0.0
    location: str = ""


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


def _profile_cache_key(text: str) -> str:
    return hashlib.sha256(text.encode('utf-8')).hexdigest()[:16]


# =============================================================================
# STEP 1: JD REQUIREMENT EXTRACTION
# =============================================================================

# Patterns for experience types commonly required in clinical research
EXPERIENCE_TYPE_PATTERNS = {
    'monitoring': [
        r'(\d+)\+?\s*years?\s*(?:of\s+)?(?:independent\s+)?(?:on-?site\s+)?(?:clinical\s+)?monitoring',
        r'(?:independent\s+)?(?:on-?site\s+)?monitoring\s+experience\s*[:\-–]?\s*(\d+)\+?\s*years?',
        r'minimum\s+(?:of\s+)?(\d+)\+?\s*years?\s*(?:of\s+)?(?:independent\s+)?monitoring',
        r'(?:at\s+least|minimum)\s+(\d+)\+?\s*years?\s*(?:of\s+)?(?:on-?site\s+)?monitoring',
        r'(\d+)\+?\s*years?\s*(?:of\s+)?(?:on-?site\s+)?monitoring\s+experience',
    ],
    'clinical_research': [
        r'(\d+)\+?\s*years?\s*(?:of\s+)?clinical\s+research\s+experience',
        r'(\d+)\+?\s*years?\s*(?:of\s+)?(?:experience\s+(?:in|with)\s+)?clinical\s+(?:trials?|research)',
    ],
    'pharmacovigilance': [
        r'(\d+)\+?\s*years?\s*(?:of\s+)?(?:experience\s+(?:in|with)\s+)?(?:pharmacovigilance|drug\s+safety|pv)',
    ],
    'medical_affairs': [
        r'(\d+)\+?\s*years?\s*(?:of\s+)?(?:experience\s+(?:in|with)\s+)?medical\s+(?:affairs|science\s+liaison)',
    ],
    'oncology': [
        r'(\d+)\+?\s*years?\s*(?:of\s+)?(?:experience\s+(?:in|with)\s+)?oncology',
        r'oncology\s+(?:monitoring\s+)?experience\s*[:\-–]?\s*(\d+)\+?\s*years?',
    ],
    'regulatory': [
        r'(\d+)\+?\s*years?\s*(?:of\s+)?(?:experience\s+(?:in|with)\s+)?regulatory\s+(?:affairs?|submissions?)',
    ],
    'data_management': [
        r'(\d+)\+?\s*years?\s*(?:of\s+)?(?:experience\s+(?:in|with)\s+)?(?:clinical\s+)?data\s+management',
    ],
    'medical_writing': [
        r'(\d+)\+?\s*years?\s*(?:of\s+)?(?:experience\s+(?:in|with)\s+)?medical\s+writing',
    ],
}

# Degree hierarchy for comparison
DEGREE_RANK = {
    'high_school': 0,
    'associate': 1,
    'bachelor': 2,
    'master': 3,
    'pharmd': 4,
    'md': 5,
    'phd': 5,
    'doctorate': 5,
}

DEGREE_PATTERNS = {
    'phd': r'\bph\.?d\.?\b|\bdoctorate\b|\bdoctoral\b',
    'md': r'\bm\.?d\.?\b|\bdoctor\s+of\s+medicine\b|\bmedical\s+degree\b',
    'pharmd': r'\bpharm\.?d\.?\b',
    'master': r"\bmaster'?s?\b|\bm\.?s\.?\b|\bm\.?a\.?\b|\bm\.?b\.?a\.?\b|\bm\.?p\.?h\.?\b|\bm\.?sc\.?\b",
    'bachelor': r"\bbachelor'?s?\b|\bb\.?s\.?\b|\bb\.?a\.?\b|\bb\.?sc\.?\b|\bundergraduate\b",
    'associate': r"\bassociate'?s?\b|\ba\.?s\.?\b|\ba\.?a\.?\b",
}

CERTIFICATION_PATTERNS = [
    r'(?:ACRP|CCRA|CCRC)\b',
    r'(?:SOCRA|CCRP)\b',
    r'\bGCP\b',
    r'\bCITI\b',
    r'\bACLS\b',
    r'\bBLS\b',
    r'\bECFMG\b',
    r'\bboard\s+certif',
    r'\bPMP\b',
    r'\bRAPS?\b',
    r'\bSAS\b',
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
    r'\b(redcap)\b', r'\b(oracle\s*(?:siebel)?)\b',
    r'\b(veeva)\b', r'\b(salesforce)\b',
    r'\b(sas)\b', r'\b(spss)\b', r'\b(stata)\b',
    r'\b(python)\b', r'\b(r\s+programming|r\s+studio)\b',
    r'\b(sql)\b', r'\b(tableau)\b',
    r'\b(e-?clinical\s*works)\b', r'\b(epic)\b', r'\b(cerner)\b',
    r'\b(ctms)\b', r'\b(edc)\b', r'\b(etmf)\b', r'\b(ivrs)\b',
    r'\b(microsoft\s+(?:word|excel|powerpoint|office))\b',
    r'\b(sharepoint)\b', r'\b(jira)\b',
]

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


def extract_requirements(jd_text: str) -> ExtractedRequirements:
    """Extract structured requirements from a job description."""
    req = ExtractedRequirements()
    jd_lower = jd_text.lower()
    confidence_signals = 0

    # --- Title & Company ---
    lines = [l.strip() for l in jd_text.split('\n') if l.strip()]
    if lines:
        req.title = lines[0]
    # Company from second line or pattern
    for line in lines[:5]:
        company_match = re.search(
            r'^([A-Z][A-Za-z\s&\.]+?)(?:\s*[|·•–—]|\s+is\s|\s+plc|\s+Inc)', line
        )
        if company_match and company_match.group(1).strip() != req.title:
            req.company = company_match.group(1).strip()
            break

    # --- Domain Detection ---
    domain, confidence, _ = detect_domain(jd_text)
    req.domain = domain
    req.domain_confidence = confidence

    # --- Total Years of Experience ---
    total_years_patterns = [
        r'(\d+)\+?\s*years?\s*(?:of\s+)?(?:relevant\s+|related\s+)?(?:clinical\s+)?(?:research\s+)?experience',
        r'minimum\s+(?:of\s+)?(\d+)\+?\s*years?\s*(?:of\s+)?experience',
        r'(?:at\s+least|requires?\s+(?:at\s+least\s+)?)\s*(\d+)\+?\s*years?',
    ]
    for pattern in total_years_patterns:
        match = re.search(pattern, jd_lower)
        if match:
            req.min_years_total = float(match.group(1))
            confidence_signals += 1
            break

    # --- Specific Experience Types ---
    for exp_type, patterns in EXPERIENCE_TYPE_PATTERNS.items():
        for pattern in patterns:
            match = re.search(pattern, jd_lower)
            if match:
                years = float(match.group(1)) if match.group(1) else 0
                # Some patterns have the number in group(2)
                if years == 0 and match.lastindex and match.lastindex >= 2:
                    try:
                        years = float(match.group(2))
                    except (ValueError, IndexError):
                        pass
                if years > 0:
                    req.min_years_specific[exp_type] = years
                    confidence_signals += 1
                break

    # Also check for "independent monitoring" as a keyword even without years
    if 'monitoring' not in req.min_years_specific:
        if re.search(r'independent\s+(?:site\s+)?monitoring', jd_lower):
            req.min_years_specific['monitoring'] = req.min_years_total or 1.0
            confidence_signals += 1

    # --- Degree Requirements ---
    # Split into required vs preferred sections
    req_section, pref_section = _split_required_vs_preferred(jd_text)
    req_lower = req_section.lower() if req_section else jd_lower

    for degree, pattern in DEGREE_PATTERNS.items():
        if re.search(pattern, req_lower, re.IGNORECASE):
            if DEGREE_RANK.get(degree, 0) > DEGREE_RANK.get(req.required_degree, 0):
                req.required_degree = degree
                confidence_signals += 1

    # If no degree found in required section, check full JD
    if not req.required_degree:
        for degree, pattern in DEGREE_PATTERNS.items():
            if re.search(pattern, jd_lower, re.IGNORECASE):
                req.required_degree = degree
                break

    # --- Certifications ---
    for cert_pattern in CERTIFICATION_PATTERNS:
        if re.search(cert_pattern, jd_text, re.IGNORECASE):
            match = re.search(cert_pattern, jd_text, re.IGNORECASE)
            req.required_certifications.append(match.group(0).strip())
    req.required_certifications = list(set(req.required_certifications))

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
    if re.search(r'\b(?:fully\s+)?remote\b', jd_lower):
        req.location_type = 'remote'
    elif re.search(r'\bhybrid\b', jd_lower):
        req.location_type = 'hybrid'
    elif re.search(r'\bon-?site\b|\bin-?office\b|\bin-?person\b', jd_lower):
        req.location_type = 'on-site'

    # --- Skills (required vs preferred) ---
    jd_keywords = extract_jd_keywords(jd_text, domain=domain)
    all_skills = jd_keywords.get('keywords', []) + jd_keywords.get('phrases', [])

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
    req.therapeutic_areas = list(set(req.therapeutic_areas))

    # --- Tools & Platforms ---
    for tool_pattern in TOOLS_PATTERNS:
        match = re.search(tool_pattern, jd_lower)
        if match:
            req.tools_platforms.append(match.group(1).strip())
    req.tools_platforms = list(set(req.tools_platforms))

    # --- Seniority ---
    req.seniority = _detect_seniority(req.title, req.min_years_total)

    # --- Confidence ---
    req.extraction_confidence = min(100.0, confidence_signals * 15)

    return req


def _split_required_vs_preferred(jd_text: str) -> Tuple[str, str]:
    """Split JD text into required vs preferred sections."""
    required_markers = [
        r'(?:minimum|required|must\s+have|essential|qualifications|requirements)',
    ]
    preferred_markers = [
        r'(?:preferred|nice\s+to\s+have|desired|bonus|advantageous|plus)',
    ]

    lines = jd_text.split('\n')
    required_lines = []
    preferred_lines = []
    current = 'required'

    for line in lines:
        line_lower = line.lower().strip()
        if any(re.search(p, line_lower) for p in preferred_markers):
            current = 'preferred'
        elif any(re.search(p, line_lower) for p in required_markers):
            current = 'required'

        if current == 'required':
            required_lines.append(line)
        else:
            preferred_lines.append(line)

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


def build_candidate_profile(resume_text: str) -> EnrichedProfile:
    """Build enriched candidate profile from resume text. Cached."""
    cache_key = _profile_cache_key(resume_text)
    if cache_key in _profile_cache:
        return _profile_cache[cache_key]

    base = parse_resume(resume_text)
    profile = EnrichedProfile(base=base)

    # --- Titles Held ---
    profile.titles_held = [job.title for job in base.jobs]

    # --- Years by Type ---
    profile.years_by_type = _calculate_years_by_type(base.jobs)

    # --- Highest Degree ---
    profile.highest_degree = _infer_highest_degree(base.education, resume_text)

    # --- Certifications ---
    profile.certifications = [c.lower().strip() for c in base.certifications]

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

    _profile_cache[cache_key] = profile
    return profile


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
                if re.search(pattern, title_lower) or re.search(pattern, bullets_text[:200]):
                    matched_types.add(exp_type)
                    break

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

        for exp_type in matched_types:
            years_by_type[exp_type] = years_by_type.get(exp_type, 0) + years

    return years_by_type


def _infer_highest_degree(education: list, resume_text: str) -> str:
    """Infer highest degree from education entries and resume text."""
    highest = ''
    highest_rank = -1

    text_lower = resume_text.lower()

    for degree, pattern in DEGREE_PATTERNS.items():
        if re.search(pattern, text_lower):
            rank = DEGREE_RANK.get(degree, 0)
            if rank > highest_rank:
                highest_rank = rank
                highest = degree

    return highest


def _extract_therapeutic_areas(text: str) -> List[str]:
    """Extract therapeutic areas mentioned in resume."""
    areas = []
    text_lower = text.lower()
    for ta_pattern in THERAPEUTIC_AREA_PATTERNS:
        match = re.search(ta_pattern, text_lower)
        if match:
            areas.append(match.group(1))
    return list(set(areas))


def _extract_tools_from_resume(text: str) -> List[str]:
    """Extract tools and platforms mentioned in resume."""
    tools = []
    text_lower = text.lower()
    for tool_pattern in TOOLS_PATTERNS:
        match = re.search(tool_pattern, text_lower)
        if match:
            tools.append(match.group(1).strip())
    return list(set(tools))


# =============================================================================
# STEP 3: KNOCKOUT DETECTION
# =============================================================================

def check_knockouts(profile: EnrichedProfile, req: ExtractedRequirements) -> KnockoutResult:
    """Check for hard disqualifiers."""
    knockouts = []

    # --- Experience Knockouts ---
    for exp_type, years_required in req.min_years_specific.items():
        years_have = profile.years_by_type.get(exp_type, 0)
        # Allow some tolerance: if they have >= 70% of required, it's soft
        if years_have < years_required * 0.5:
            knockouts.append(KnockoutFlag(
                category='experience',
                requirement=f'{years_required} years {exp_type.replace("_", " ")} experience',
                candidate_has=f'{years_have:.1f} years',
                severity='hard',
                fixable=False,
                suggestion=_suggest_alternative_for_experience(exp_type),
            ))
        elif years_have < years_required:
            knockouts.append(KnockoutFlag(
                category='experience',
                requirement=f'{years_required} years {exp_type.replace("_", " ")} experience',
                candidate_has=f'{years_have:.1f} years',
                severity='soft',
                fixable=False,
                suggestion=f'Close to requirement — emphasize related experience in resume. '
                           f'Recruiter may accept {years_have:.1f} years if other qualifications are strong.',
            ))

    # Total years check
    if req.min_years_total > 0:
        total = profile.base.total_years_experience if profile.base else 0
        if total < req.min_years_total * 0.5:
            knockouts.append(KnockoutFlag(
                category='experience',
                requirement=f'{req.min_years_total} years total experience',
                candidate_has=f'{total:.1f} years',
                severity='hard',
                fixable=False,
                suggestion='Target roles requiring fewer years of experience.',
            ))

    # --- Education Knockout ---
    if req.required_degree:
        candidate_rank = DEGREE_RANK.get(profile.highest_degree, 0)
        required_rank = DEGREE_RANK.get(req.required_degree, 0)
        if candidate_rank < required_rank:
            knockouts.append(KnockoutFlag(
                category='education',
                requirement=f'{req.required_degree.upper()} or equivalent',
                candidate_has=profile.highest_degree.upper() or 'Not detected',
                severity='hard',
                fixable=False,
                suggestion='Target roles matching your education level.',
            ))

    # --- Visa Knockout ---
    if not req.visa_sponsorship_available:
        # We can't know for sure, but flag it as a consideration
        # Check if candidate might need sponsorship (international medical graduate)
        has_intl_education = any(
            'sri lanka' in (e.school or '').lower() or
            'jaffna' in (e.school or '').lower()
            for e in (profile.base.education if profile.base else [])
        )
        if has_intl_education:
            knockouts.append(KnockoutFlag(
                category='visa',
                requirement='Must be authorized to work without sponsorship',
                candidate_has='International medical graduate — verify work authorization',
                severity='soft',
                fixable=False,
                suggestion='Ensure you have valid work authorization (green card, citizenship, EAD). '
                           'If you do, answer "Yes" to the screening question confidently.',
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
) -> FitDimensions:
    """Score 7 fit dimensions."""
    dims = FitDimensions()

    dims.experience_match = _score_experience_match(profile, req)
    dims.skills_match = _score_skills_match(profile, req, resume_text, jd_text)
    dims.title_alignment = _score_title_alignment(profile, req, resume_text, jd_text)
    dims.domain_match = _score_domain_match(profile, req, resume_text, jd_text)
    dims.education_match = _score_education_match(profile, req)
    dims.certification_match = _score_certification_match(profile, req)
    dims.seniority_match = _score_seniority_match(profile, req)

    return dims


def _score_experience_match(profile: EnrichedProfile, req: ExtractedRequirements) -> float:
    """Score experience alignment (0-100)."""
    if not req.min_years_specific and req.min_years_total <= 0:
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

    return sum(scores) / len(scores) if scores else 50.0


def _score_skills_match(
    profile: EnrichedProfile,
    req: ExtractedRequirements,
    resume_text: str,
    jd_text: str,
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
    if SBERT_AVAILABLE and sbert_util:
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
) -> float:
    """Score how well past titles align with target title (0-100)."""
    if not req.title:
        return 50.0

    target_lower = req.title.lower()
    best_score = 0

    for title in profile.titles_held:
        title_lower = title.lower()

        # Exact match
        if target_lower in title_lower or title_lower in target_lower:
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
    if SBERT_AVAILABLE and sbert_util and best_score < 80:
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
    resume_domain, _, _ = detect_domain(resume_text)
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
    if not req.required_degree:
        return 80.0  # No specific requirement — neutral-positive

    candidate_rank = DEGREE_RANK.get(profile.highest_degree, 0)
    required_rank = DEGREE_RANK.get(req.required_degree, 0)

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
        req_cert_lower = req_cert.lower()
        for cand_cert in profile.certifications:
            if req_cert_lower in cand_cert or cand_cert in req_cert_lower:
                matched += 1
                break

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

    has_md = profile.highest_degree == 'md'
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

def calculate_job_fit(resume_text: str, jd_text: str) -> JobFitResult:
    """
    Main entry point. Calculate job fit score with knockout detection.

    Args:
        resume_text: Full master resume text
        jd_text: Job description text

    Returns:
        JobFitResult with score, recommendation, knockouts, dimensions, gaps
    """
    # Step 1: Extract requirements from JD
    req = extract_requirements(jd_text)

    # Step 2: Build candidate profile
    profile = build_candidate_profile(resume_text)

    # Step 3: Check knockouts
    knockout_result = check_knockouts(profile, req)

    # Step 4: Score dimensions
    dims = score_fit_dimensions(profile, req, resume_text, jd_text)

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
        with open(resume_path, 'r', encoding='utf-8') as f:
            resume_text = f.read()
        with open(jd_path, 'r', encoding='utf-8') as f:
            jd_text = f.read()

        result = calculate_job_fit(resume_text, jd_text)

        if args.json:
            print(json.dumps(result.to_dict(), indent=2))
        else:
            print(format_report(result))
    else:
        parser.print_help()


if __name__ == '__main__':
    main()
