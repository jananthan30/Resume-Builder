"""
HR Cognitive Simulation Engine (HR-CSE)
========================================
Simulates human HR recruiter evaluation logic beyond ATS keyword matching.

Design Philosophy:
- Models INFERENCE and RISK ASSESSMENT logic used by human recruiters
- Evaluates career narrative, trajectory, impact signals, and red flags
- Provides explainable scoring with actionable feedback

Enhanced Features (v1.1.0):
- Exponential decay for skill freshness (R(s,t) = W_base * e^(-λ * Δt))
- Bloom's Taxonomy verb power classification
- Expanded prestige databases from JSON data files
- Healthcare/Clinical/Pharma domain focus

Author: Resume Builder Project
Version: 1.1.0
"""

import re
import json
import argparse
import os
from pathlib import Path
from datetime import datetime, date
from typing import Dict, List, Tuple, Optional, Any
from dataclasses import dataclass, field
from collections import defaultdict
import math

# =============================================================================
# DATA FILE LOADING
# =============================================================================

DATA_DIR = Path(__file__).parent / "data"

def load_json_data(filename: str, default: Any = None) -> Any:
    """Load JSON data file with fallback to default."""
    filepath = DATA_DIR / filename
    if filepath.exists():
        try:
            with open(filepath, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception:
            pass
    return default if default is not None else {}

# Load enhanced data files
SKILL_TAXONOMY = load_json_data("skill_taxonomy.json", {})
ACTION_VERBS_DATA = load_json_data("action_verbs.json", {})
COMPANY_PRESTIGE_DATA = load_json_data("company_prestige.json", {})
UNIVERSITY_RANKINGS_DATA = load_json_data("university_rankings.json", {})

# Build skill decay lookup: skill -> lambda value
SKILL_DECAY_MAP = {}
for category, skills in SKILL_TAXONOMY.items():
    for skill_name, skill_data in skills.items():
        if isinstance(skill_data, dict) and 'decay_lambda' in skill_data:
            SKILL_DECAY_MAP[skill_name.lower()] = skill_data['decay_lambda']
            # Also add related terms
            if 'related' in skill_data:
                for related in skill_data['related']:
                    if related.lower() not in SKILL_DECAY_MAP:
                        SKILL_DECAY_MAP[related.lower()] = skill_data['decay_lambda']


def get_skill_decay_lambda(skill: str) -> float:
    """
    Get the decay constant (λ) for a skill.
    Higher λ = faster decay (tech skills), Lower λ = slower decay (soft skills)

    Default values:
    - Technical/Framework skills: 0.15-0.20 (volatile)
    - Clinical/Domain skills: 0.05 (durable)
    - Soft skills: 0.01-0.03 (very durable)
    """
    skill_lower = skill.lower().strip()
    return SKILL_DECAY_MAP.get(skill_lower, 0.10)  # Default moderate decay


def calculate_skill_freshness(skill: str, years_since_use: float) -> float:
    """
    Calculate skill freshness using exponential decay.

    Formula: R(s,t) = W_base * e^(-λ * Δt)

    Where:
    - W_base = 1.0 (full weight when current)
    - λ = decay constant from skill taxonomy
    - Δt = years since last use

    Returns: weight multiplier (0.0 to 1.0)
    """
    lambda_val = get_skill_decay_lambda(skill)
    freshness = math.exp(-lambda_val * years_since_use)
    return max(0.1, freshness)  # Minimum 10% weight even for old skills


# Build verb power lookup from action_verbs.json (Bloom's Taxonomy)
VERB_POWER_MAP = {}
for level_name, level_data in ACTION_VERBS_DATA.items():
    if isinstance(level_data, dict) and 'verbs' in level_data and 'score' in level_data:
        score = level_data['score']
        for verb in level_data['verbs']:
            VERB_POWER_MAP[verb.lower()] = score


def get_verb_power_score(verb: str) -> float:
    """
    Get verb power score based on Bloom's Taxonomy classification.

    Levels:
    - Level 4 (Impact): generated, saved, increased - Score 4
    - Level 3 (Strategy): spearheaded, architected - Score 3
    - Level 2 (Management): managed, led, directed - Score 2
    - Level 1 (Execution): assisted, helped, created - Score 1
    - Level 0 (Weak): responsible for, duties included - Score 0
    """
    verb_lower = verb.lower().strip()
    return VERB_POWER_MAP.get(verb_lower, 1.5)  # Default mid-level score


def normalize_match_text(text: str) -> str:
    """Normalize text for boundary-aware term matching."""
    text = text.lower().replace('_', ' ')
    text = re.sub(r'[^a-z0-9\s/&+-]', ' ', text)
    text = re.sub(r'\s+', ' ', text)
    return text.strip()


def compile_term_pattern(term: str) -> Optional[re.Pattern]:
    """Compile a boundary-aware matcher for a term."""
    normalized_term = normalize_match_text(term)
    if not normalized_term:
        return None
    return re.compile(r'(?<![a-z0-9])' + re.escape(normalized_term) + r'(?![a-z0-9])')


def contains_term(text: str, term: str) -> bool:
    """Return True only when the term appears as a standalone token or phrase."""
    pattern = compile_term_pattern(term)
    return bool(pattern and pattern.search(text))


def find_term_positions(text: str, term: str) -> List[int]:
    """Return all boundary-aware match offsets for a term."""
    pattern = compile_term_pattern(term)
    if not pattern:
        return []
    return [match.start() for match in pattern.finditer(text)]

# Optional imports for advanced NLP (graceful degradation if not available)
try:
    from sentence_transformers import SentenceTransformer, util
    SBERT_AVAILABLE = True
except ImportError:
    SBERT_AVAILABLE = False

# Note: spaCy import removed (was dead code - SPACY_AVAILABLE was never referenced)
# HR scorer uses regex-based parsing, not spaCy NLP pipeline


# =============================================================================
# CONFIGURATION & CONSTANTS
# =============================================================================

# Dynamic weights based on role seniority
# job_fit is heavily weighted because HR cares most about "is this person right for THIS role"
WEIGHT_PROFILES = {
    'junior': {
        'experience': 0.15,
        'skills': 0.25,
        'trajectory': 0.10,
        'impact': 0.15,
        'competitive': 0.10,
        'job_fit': 0.25  # Domain fit matters even for juniors
    },
    'mid': {
        'experience': 0.20,
        'skills': 0.20,
        'trajectory': 0.10,
        'impact': 0.15,
        'competitive': 0.10,
        'job_fit': 0.25  # Is their background relevant?
    },
    'senior': {
        'experience': 0.20,
        'skills': 0.15,
        'trajectory': 0.15,
        'impact': 0.15,
        'competitive': 0.10,
        'job_fit': 0.25  # Domain expertise expected
    },
    'executive': {
        'experience': 0.15,
        'skills': 0.10,
        'trajectory': 0.15,
        'impact': 0.20,
        'competitive': 0.10,
        'job_fit': 0.30  # Deep domain expertise critical
    },
    'pivot': {  # Career changer weights - job fit less strict
        'experience': 0.10,
        'skills': 0.30,
        'trajectory': 0.10,
        'impact': 0.25,
        'competitive': 0.10,
        'job_fit': 0.15  # More lenient for career changers
    }
}

# Title hierarchy mapping for trajectory scoring
TITLE_HIERARCHY = {
    # Entry Level (1-2)
    'intern': 1, 'trainee': 1, 'apprentice': 1,
    'assistant': 2, 'associate': 2, 'junior': 2, 'entry': 2,
    'coordinator': 2, 'administrator': 2,

    # Mid Level (3-4)
    'analyst': 3, 'specialist': 3, 'engineer': 3, 'developer': 3,
    'consultant': 3, 'officer': 3, 'representative': 3,
    'senior analyst': 4, 'senior specialist': 4, 'senior engineer': 4,
    'senior': 4, 'lead': 4, 'principal': 4, 'staff': 4,

    # Management (5-6)
    'supervisor': 5, 'team lead': 5, 'manager': 5, 'program manager': 5,
    'project manager': 5, 'product manager': 5,
    'senior manager': 6, 'associate director': 6, 'director': 6,
    'head': 6, 'head of': 6,

    # Executive (7-9)
    'senior director': 7, 'vice president': 7, 'vp': 7,
    'senior vice president': 8, 'svp': 8, 'evp': 8,
    'chief': 9, 'ceo': 9, 'cfo': 9, 'cto': 9, 'coo': 9, 'cmo': 9,
    'president': 9, 'partner': 8, 'managing director': 8,

    # Medical/Clinical specific
    'resident': 3, 'fellow': 4, 'attending': 5, 'physician': 5,
    'medical officer': 5, 'senior medical officer': 6,
    'medical director': 7, 'chief medical officer': 9,

    # Research specific
    'research assistant': 2, 'research associate': 3,
    'senior research associate': 4, 'research scientist': 4,
    'senior scientist': 5, 'principal scientist': 6,
}

# Prestige tiers for companies (sample - expandable)
COMPANY_PRESTIGE = {
    'tier1': [  # FAANG + Top Pharma + Consulting
        'google', 'meta', 'facebook', 'apple', 'amazon', 'microsoft', 'netflix',
        'pfizer', 'johnson & johnson', 'j&j', 'merck', 'novartis', 'roche',
        'regeneron', 'gilead', 'amgen', 'abbvie', 'eli lilly', 'bristol-myers',
        'mckinsey', 'bain', 'bcg', 'boston consulting', 'deloitte', 'pwc', 'ey', 'kpmg',
        'goldman sachs', 'morgan stanley', 'jpmorgan', 'jp morgan',
        'mayo clinic', 'cleveland clinic', 'johns hopkins', 'mass general',
    ],
    'tier2': [  # Strong regional/specialty companies
        'salesforce', 'oracle', 'ibm', 'cisco', 'intel', 'nvidia',
        'sanofi', 'astrazeneca', 'gsk', 'glaxosmithkline', 'takeda', 'boehringer',
        'biogen', 'vertex', 'moderna', 'biontech',
        'labcorp', 'covance', 'iqvia', 'pra health', 'ppd', 'parexel', 'icon',
        'accenture', 'capgemini', 'cognizant',
    ],
    'tier3': [  # Recognized companies
        'medtronic', 'abbott', 'stryker', 'boston scientific',
        'cerner', 'epic', 'athenahealth',
    ]
}

# University prestige tiers
UNIVERSITY_PRESTIGE = {
    'tier1': [  # Ivy League + Top Medical/Research
        'harvard', 'stanford', 'mit', 'yale', 'princeton', 'columbia',
        'university of pennsylvania', 'upenn', 'penn', 'cornell', 'brown', 'dartmouth',
        'johns hopkins', 'duke', 'university of chicago', 'northwestern',
        'berkeley', 'ucla', 'ucsf', 'caltech',
        'oxford', 'cambridge', 'imperial college',
    ],
    'tier2': [  # Strong research universities
        'university of michigan', 'umich', 'university of virginia', 'uva',
        'georgetown', 'notre dame', 'vanderbilt', 'emory', 'wash u', 'wustl',
        'nyu', 'boston university', 'bu', 'tufts', 'case western',
        'unc', 'university of north carolina', 'university of wisconsin',
        'university of pittsburgh', 'upmc', 'university of rochester',
    ],
    'tier3': [  # Recognized universities
        'ohio state', 'penn state', 'michigan state', 'purdue',
        'university of florida', 'university of texas', 'ut austin',
        'indiana university', 'university of iowa', 'university of minnesota',
    ]
}

# Strong action verbs for impact scoring
STRONG_ACTION_VERBS = {
    'leadership': [
        'led', 'directed', 'managed', 'headed', 'spearheaded', 'oversaw',
        'supervised', 'orchestrated', 'championed', 'pioneered', 'established',
        'founded', 'launched', 'initiated', 'drove', 'transformed'
    ],
    'achievement': [
        'achieved', 'exceeded', 'surpassed', 'delivered', 'generated',
        'increased', 'improved', 'reduced', 'saved', 'accelerated',
        'optimized', 'maximized', 'doubled', 'tripled', 'grew'
    ],
    'technical': [
        'developed', 'designed', 'engineered', 'built', 'created',
        'implemented', 'deployed', 'architected', 'automated', 'integrated',
        'programmed', 'coded', 'configured', 'migrated', 'scaled'
    ],
    'analytical': [
        'analyzed', 'evaluated', 'assessed', 'investigated', 'researched',
        'identified', 'discovered', 'diagnosed', 'validated', 'verified',
        'quantified', 'measured', 'tracked', 'monitored', 'audited'
    ],
    'collaborative': [
        'collaborated', 'partnered', 'coordinated', 'facilitated', 'negotiated',
        'liaised', 'aligned', 'unified', 'bridged', 'integrated'
    ]
}

# Gap explanation keywords (no penalty if found)
GAP_EXPLANATIONS = [
    'parental leave', 'maternity leave', 'paternity leave', 'family leave',
    'sabbatical', 'caregiving', 'caregiver', 'medical leave', 'health',
    'relocation', 'immigration', 'visa', 'travel', 'study', 'education',
    'graduate school', 'mba', 'certification', 'training', 'bootcamp',
    'startup', 'entrepreneur', 'freelance', 'consulting', 'contract'
]

# =============================================================================
# JOB FIT ANALYSIS - DOMAIN ADJACENCY & TRANSFERABILITY MAPPINGS
# =============================================================================

# Therapeutic area adjacency - how HR thinks about related experience
THERAPEUTIC_ADJACENCY = {
    'oncology': {
        'direct': ['hematology', 'hematology/oncology', 'heme/onc', 'immuno-oncology', 'tumor'],
        'related': ['immunology', 'cell therapy', 'biologics', 'critical care', 'palliative'],
        'transferable': ['internal medicine', 'infectious disease', 'rheumatology']
    },
    'hematology': {
        'direct': ['oncology', 'hematology/oncology', 'blood disorders', 'hem/onc'],
        'related': ['transfusion medicine', 'coagulation', 'stem cell', 'bone marrow'],
        'transferable': ['internal medicine', 'critical care', 'pediatrics']
    },
    'cardiology': {
        'direct': ['cardiovascular', 'heart failure', 'interventional cardiology'],
        'related': ['electrophysiology', 'vascular', 'cardiac surgery', 'hypertension'],
        'transferable': ['internal medicine', 'critical care', 'pulmonology']
    },
    'neurology': {
        'direct': ['neuroscience', 'cns', 'neurological'],
        'related': ['psychiatry', 'movement disorders', 'epilepsy', 'stroke'],
        'transferable': ['internal medicine', 'rehabilitation', 'geriatrics']
    },
    'gastroenterology': {
        'direct': ['gi', 'digestive', 'hepatology', 'liver'],
        'related': ['nutrition', 'obesity', 'metabolic'],
        'transferable': ['internal medicine', 'surgery', 'oncology']
    },
    'immunology': {
        'direct': ['autoimmune', 'allergy', 'rheumatology'],
        'related': ['oncology', 'transplant', 'infectious disease'],
        'transferable': ['internal medicine', 'dermatology']
    },
    'rare disease': {
        'direct': ['orphan drug', 'genetic disorders', 'metabolic disorders'],
        'related': ['pediatrics', 'genetics', 'specialty pharmacy'],
        'transferable': ['any therapeutic area with complex patients']
    },
    'infectious disease': {
        'direct': ['virology', 'bacteriology', 'antimicrobial', 'vaccines'],
        'related': ['immunology', 'pulmonology', 'critical care'],
        'transferable': ['internal medicine', 'public health', 'epidemiology']
    }
}

# Experience type transferability - how HR views related experience
EXPERIENCE_TRANSFERABILITY = {
    'clinical research': {
        'direct': ['drug development', 'clinical trials', 'clinical development', 'clinical sciences'],
        'related': ['medical affairs', 'pharmacovigilance', 'regulatory affairs', 'medical monitoring'],
        'transferable': ['clinical practice', 'academic research', 'healthcare consulting']
    },
    'drug development': {
        'direct': ['clinical development', 'clinical research', 'clinical sciences'],
        'related': ['regulatory affairs', 'medical affairs', 'pharmacology'],
        'transferable': ['clinical practice', 'academic medicine', 'basic research']
    },
    'medical affairs': {
        'direct': ['field medical', 'msl', 'medical director', 'medical science liaison'],
        'related': ['clinical development', 'medical communications', 'health economics'],
        'transferable': ['clinical practice', 'academic medicine', 'kol engagement']
    },
    'pharmacovigilance': {
        'direct': ['drug safety', 'safety monitoring', 'adverse events', 'signal detection'],
        'related': ['clinical research', 'regulatory affairs', 'risk management'],
        'transferable': ['clinical practice', 'quality assurance', 'epidemiology']
    },
    'regulatory affairs': {
        'direct': ['regulatory submissions', 'fda', 'ema', 'health authority'],
        'related': ['clinical development', 'quality assurance', 'compliance'],
        'transferable': ['clinical research', 'legal', 'policy']
    }
}

# Phase experience transferability
PHASE_EXPERIENCE = {
    'phase i': {
        'direct': ['phase 1', 'first-in-human', 'fih', 'dose escalation', 'pk/pd'],
        'related': ['phase ii', 'phase 2', 'early development'],
        'weight': 1.0
    },
    'phase ii': {
        'direct': ['phase 2', 'proof of concept', 'poc'],
        'related': ['phase i', 'phase iii', 'phase 1', 'phase 3'],
        'weight': 1.0
    },
    'phase iii': {
        'direct': ['phase 3', 'pivotal', 'registration', 'confirmatory'],
        'related': ['phase ii', 'phase iv', 'phase 2', 'phase 4'],
        'weight': 1.0
    },
    'phase iv': {
        'direct': ['phase 4', 'post-marketing', 'real world', 'rwe', 'observational'],
        'related': ['phase iii', 'phase 3', 'registry', 'surveillance'],
        'weight': 0.9
    }
}

# Role level alignment - expected progression
ROLE_LEVEL_REQUIREMENTS = {
    'medical director': {
        'expected_levels': [6, 7, 8],  # Associate Director to VP range
        'min_years_clinical': 5,
        'min_years_industry': 3,
        'education': ['md', 'do', 'mbbs', 'phd'],
        'stretch_acceptable': True
    },
    'associate director': {
        'expected_levels': [5, 6, 7],
        'min_years_clinical': 3,
        'min_years_industry': 2,
        'education': ['md', 'do', 'mbbs', 'phd', 'pharmd', 'masters'],
        'stretch_acceptable': True
    },
    'senior director': {
        'expected_levels': [7, 8, 9],
        'min_years_clinical': 7,
        'min_years_industry': 5,
        'education': ['md', 'do', 'mbbs', 'phd'],
        'stretch_acceptable': False
    },
    'director': {
        'expected_levels': [6, 7],
        'min_years_clinical': 5,
        'min_years_industry': 3,
        'education': ['md', 'do', 'mbbs', 'phd', 'pharmd'],
        'stretch_acceptable': True
    },
    'manager': {
        'expected_levels': [4, 5, 6],
        'min_years_clinical': 2,
        'min_years_industry': 1,
        'education': ['md', 'pharmd', 'phd', 'masters', 'bachelors'],
        'stretch_acceptable': True
    }
}


# =============================================================================
# DATA STRUCTURES
# =============================================================================

@dataclass
class JobEntry:
    """Represents a single job/position from resume"""
    title: str
    company: str
    location: str = ""
    start_date: Optional[date] = None
    end_date: Optional[date] = None  # None = Present
    bullets: List[str] = field(default_factory=list)
    duration_months: int = 0
    hierarchy_level: int = 3  # Default mid-level
    is_current: bool = False
    date_context: str = ""  # Raw date line text (captures "Per Diem", "Part-Time", "Concurrent" etc.)

@dataclass
class EducationEntry:
    """Represents an education entry"""
    degree: str
    school: str
    field: str = ""
    graduation_year: Optional[int] = None
    gpa: Optional[float] = None

@dataclass
class CandidateProfile:
    """Parsed candidate information"""
    jobs: List[JobEntry] = field(default_factory=list)
    education: List[EducationEntry] = field(default_factory=list)
    skills: List[str] = field(default_factory=list)
    certifications: List[str] = field(default_factory=list)
    all_bullets: List[str] = field(default_factory=list)
    total_years_experience: float = 0.0
    relevant_years_experience: float = 0.0
    tags: List[str] = field(default_factory=list)
    raw_text: str = ""

@dataclass
class JobRequirements:
    """Parsed job description requirements"""
    title: str = ""
    company: str = ""
    required_years: float = 5.0
    seniority_level: str = "mid"  # junior, mid, senior, executive
    required_skills: List[str] = field(default_factory=list)
    preferred_skills: List[str] = field(default_factory=list)
    required_education: str = ""
    industry_keywords: List[str] = field(default_factory=list)
    raw_text: str = ""
    # Job Fit Analysis fields
    therapeutic_areas: List[str] = field(default_factory=list)
    experience_types: List[str] = field(default_factory=list)
    required_phases: List[str] = field(default_factory=list)
    required_degrees: List[str] = field(default_factory=list)
    preferred_specializations: List[str] = field(default_factory=list)
    is_industry_role: bool = True  # vs academic/clinical

@dataclass
class ScoreBreakdown:
    """Detailed score breakdown"""
    experience: float = 0.0
    skills: float = 0.0
    trajectory: float = 0.0
    impact: float = 0.0
    competitive: float = 0.0
    job_fit: float = 0.0  # NEW: Domain/role alignment score

    def to_dict(self) -> Dict[str, float]:
        return {
            'experience': round(self.experience, 1),
            'skills': round(self.skills, 1),
            'trajectory': round(self.trajectory, 1),
            'impact': round(self.impact, 1),
            'competitive': round(self.competitive, 1),
            'job_fit': round(self.job_fit, 1)
        }

@dataclass
class HRScoreResult:
    """Complete HR scoring result"""
    overall_score: float
    recommendation: str  # INTERVIEW, MAYBE, PASS, AUTO-REJECT
    rating_label: str
    confidence: str
    factor_breakdown: ScoreBreakdown
    penalties_applied: Dict[str, float]
    strengths: List[str]
    concerns: List[str]
    suggested_questions: List[str]
    candidate_tags: List[str]
    weights_used: Dict[str, float]
    writing_quality: Dict[str, Any] = field(default_factory=dict)


# =============================================================================
# PARSING FUNCTIONS
# =============================================================================

def extract_text_from_file(file_path: str) -> str:
    """Extract text from PDF, DOCX, MD, or TXT file."""
    from text_extractor import extract_text
    return extract_text(file_path)


def parse_date(date_str: str) -> Optional[date]:
    """Parse various date formats to date object"""
    if not date_str:
        return None

    date_str = date_str.strip().lower()

    # Handle "Present", "Current", etc.
    if any(word in date_str for word in ['present', 'current', 'now', 'ongoing']):
        return None  # None represents "Present"

    # Common date patterns
    patterns = [
        (r'(\w+)\s+(\d{4})', '%B %Y'),  # "January 2024"
        (r'(\w{3})\s+(\d{4})', '%b %Y'),  # "Jan 2024"
        (r'(\d{1,2})/(\d{4})', '%m/%Y'),  # "01/2024"
        (r'(\d{4})', '%Y'),  # "2024"
    ]

    for pattern, fmt in patterns:
        match = re.search(pattern, date_str, re.IGNORECASE)
        if match:
            try:
                if fmt == '%Y':
                    return date(int(match.group(1)), 6, 1)  # Assume mid-year
                else:
                    parsed = datetime.strptime(match.group(0), fmt)
                    return parsed.date()
            except ValueError:
                continue

    return None


def extract_years_from_text(text: str) -> Optional[float]:
    """Extract years of experience from text like '5+ years' or 'minimum 3 years'"""
    # Priority patterns - look for explicit requirements first
    priority_patterns = [
        r'minimum\s*(?:of\s+)?(\d+)\s*(?:years?|yrs?)',
        r'at\s+least\s+(\d+)\s*(?:years?|yrs?)',
        r'(\d+)\+?\s*(?:years?|yrs?)\s*(?:of\s+)?(?:experience|exp)',
        r'(\d+)\+?\s*(?:years?|yrs?)\s+(?:in\s+)?(?:clinical|drug|research|development)',
    ]

    for pattern in priority_patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            years = float(match.group(1))
            # Sanity check - requirements are typically 1-15 years, not 20+
            if years <= 15:
                return years

    # Fallback - general years pattern but with lower numbers
    general_pattern = r'(\d+)\+?\s*(?:years?|yrs?)'
    matches = re.findall(general_pattern, text, re.IGNORECASE)
    # Filter to reasonable experience requirements (1-15 years)
    reasonable_years = [float(y) for y in matches if 1 <= float(y) <= 15]
    if reasonable_years:
        return min(reasonable_years)  # Take the minimum (likely the requirement)

    return None


def determine_seniority_level(text: str) -> str:
    """Determine job seniority level from job description text"""
    text_lower = text.lower()

    # Executive indicators
    if any(term in text_lower for term in ['chief', 'vp', 'vice president', 'c-level', 'cxo', 'executive']):
        return 'executive'

    # Senior/Director indicators
    if any(term in text_lower for term in ['director', 'senior manager', 'head of', 'principal']):
        return 'senior'

    # Mid-level indicators
    if any(term in text_lower for term in ['senior', 'lead', 'sr.', 'experienced']):
        return 'mid'

    # Junior indicators
    if any(term in text_lower for term in ['entry', 'junior', 'associate', 'graduate', 'trainee', '0-2 years', '1-3 years']):
        return 'junior'

    # Default based on years required
    years = extract_years_from_text(text)
    if years:
        if years >= 10:
            return 'senior'
        elif years >= 5:
            return 'mid'
        else:
            return 'junior'

    return 'mid'  # Default


def get_title_hierarchy_level(title: str) -> int:
    """Map job title to hierarchy level (1-9)"""
    title_lower = title.lower().strip()

    # Direct lookup
    for key, level in TITLE_HIERARCHY.items():
        if key in title_lower:
            return level

    # Default to mid-level
    return 3


def parse_resume(text: str) -> CandidateProfile:
    """Parse resume text into structured CandidateProfile"""
    profile = CandidateProfile(raw_text=text)

    lines = text.split('\n')
    current_section = None
    current_job = None
    in_experience_section = False

    # Section detection patterns
    section_patterns = {
        'experience': (
            r'^(?:professional\s+)?experience$'
            r'|^work\s+history$'
            r'|^employment'
            r'|^(?:research\s+)?work\s+experience'
            r'|^research\s+(?:and\s+)?(?:work\s+)?experience'
            r'|^clinical\s+experience'
            r'|^teaching\s+experience'
            r'|^academic\s+(?:and\s+)?(?:professional\s+)?experience'
            r'|^industry\s+experience'
            r'|^(?:relevant\s+)?(?:work\s+)?experience'
            r'|^career\s+(?:history|summary|profile)'
            r'|^positions?\s+(?:held|history)'
            r'|^professional\s+(?:history|background|profile)'
            r'|^job\s+(?:history|experience)'
        ),
        'education': r'^education$|^academic|^qualifications|^academic\s+background',
        'skills': (
            r'^skills$|^competencies$|^core\s+competencies$'
            r'|^technical\s+skills?$'
            r'|^key\s+skills?'
            r'|^areas?\s+of\s+expertise'
            r'|^expertise$'
            r'|^proficiencies'
            r'|^(?:technical\s+)?capabilities'
        ),
        'certifications': r'^certifications?|^licenses?|^credentials',
        'summary': r'^(?:professional\s+)?summary$|^profile$|^objective$|^about',
        'publications': r'^publications?$',
        'memberships': r'^(?:professional\s+)?memberships?$',
    }

    # Enhanced job pattern: handles mixed case, leading bullet/dash, optional location
    job_pattern = r'^[•\-*]?\s*(.+?)\s*\|\s*(.+?)\s*(?:\|\s*(.+))?$'
    # Enhanced date patterns
    date_patterns = [
        r'(\w+\s+\d{4})\s*[-–—]\s*(\w+\s+\d{4}|present|current)',  # "June 2024 – Present"
        r'(\d{1,2}/\d{4})\s*[-–—]\s*(\d{1,2}/\d{4}|present|current)',  # "06/2024 - Present"
        r'(\d{4})\s*[-–—]\s*(\d{4}|present|current)',  # "2024 - Present"
    ]

    for i, line in enumerate(lines):
        line_stripped = line.strip()
        if not line_stripped:
            continue

        # Check for section headers (lines that are just the section name)
        is_section_header = False
        for section, pattern in section_patterns.items():
            if re.match(pattern, line_stripped, re.IGNORECASE):
                current_section = section
                in_experience_section = (section == 'experience')
                is_section_header = True
                break

        # Handle decorated headers: ─── SECTION NAME ─── or === SECTION === etc.
        # (Modern format uses box-drawing ─ chars; strip all non-alpha prefix/suffix)
        if not is_section_header:
            _clean_header = re.sub(r'^[^a-zA-Z]+|[^a-zA-Z\s]+$', '', line_stripped).strip()
            if _clean_header and _clean_header != line_stripped and len(_clean_header) < 50:
                for section, pattern in section_patterns.items():
                    if re.match(pattern, _clean_header, re.IGNORECASE):
                        current_section = section
                        in_experience_section = (section == 'experience')
                        is_section_header = True
                        break

        # Also detect ALL-CAPS section headers directly (without preceding ___)
        if not is_section_header and line_stripped.isupper() and len(line_stripped) < 40 and not line_stripped.startswith(('•', '-', '*')):
            for section, pattern in section_patterns.items():
                if re.match(pattern, line_stripped, re.IGNORECASE):
                    current_section = section
                    in_experience_section = (section == 'experience')
                    is_section_header = True
                    break

        # Skip if this line is a section header
        if is_section_header:
            continue

        # Also detect section by horizontal line (underscores or box-drawing) followed by section name
        if '___' in line_stripped or re.match(r'^[─═\-─]{3,}$', line_stripped):
            # Next non-empty line might be section header
            for j in range(i + 1, min(i + 3, len(lines))):
                next_line = lines[j].strip()
                if next_line:
                    for section, pattern in section_patterns.items():
                        if re.match(pattern, next_line, re.IGNORECASE):
                            current_section = section
                            in_experience_section = (section == 'experience')
                            break
                    break
            continue

        # Parse based on current section
        if current_section == 'experience' or in_experience_section:
            # Check for job title line (TITLE | COMPANY | LOCATION)
            job_match = re.match(job_pattern, line_stripped)
            if job_match:
                potential_title = job_match.group(1).strip()
                potential_company = job_match.group(2).strip()

                # Validate: title should have at least 2 chars and not be a separator line
                if len(potential_title) < 2 or len(potential_company) < 2:
                    pass  # Skip invalid matches
                elif '___' in potential_title or '---' in potential_title:
                    pass  # Skip separator lines
                else:
                    if current_job:
                        current_job.hierarchy_level = get_title_hierarchy_level(current_job.title)
                        profile.jobs.append(current_job)

                    # Handle Title|Date|Company format (academic/research resumes)
                    # e.g. "PhD student | 2022-present | Mount Sinai"
                    actual_company = potential_company
                    actual_location = job_match.group(3).strip() if job_match.group(3) else ""
                    inline_start = None
                    inline_end = None
                    inline_is_current = False
                    for dp in date_patterns:
                        dm = re.search(dp, potential_company, re.IGNORECASE)
                        if dm:
                            # company field is actually a date — shift fields
                            actual_company = actual_location
                            actual_location = ""
                            inline_start = parse_date(dm.group(1))
                            end_str = dm.group(2)
                            if 'present' in end_str.lower() or 'current' in end_str.lower():
                                inline_end = None
                                inline_is_current = True
                            else:
                                inline_end = parse_date(end_str)
                            break

                    # Also handle Title|Company|Date format (date in 3rd field)
                    if not inline_start and actual_location:
                        for dp in date_patterns:
                            dm = re.search(dp, actual_location, re.IGNORECASE)
                            if dm:
                                inline_start = parse_date(dm.group(1))
                                end_str = dm.group(2)
                                if 'present' in end_str.lower() or 'current' in end_str.lower():
                                    inline_end = None
                                    inline_is_current = True
                                else:
                                    inline_end = parse_date(end_str)
                                actual_location = ""
                                break

                    current_job = JobEntry(
                        title=potential_title,
                        company=actual_company,
                        location=actual_location,
                    )
                    if inline_start:
                        current_job.start_date = inline_start
                        current_job.end_date = inline_end
                        current_job.is_current = inline_is_current
                        end = inline_end or date.today()
                        if inline_start:
                            delta = (end.year - inline_start.year) * 12 + (end.month - inline_start.month)
                            current_job.duration_months = max(0, delta)
                    continue

            # Check for date line (separate line with just dates)
            # Handle semicolon-separated date ranges (e.g., "Oct 2021 – Jul 2022; Jul 2023 – Jan 2024")
            if current_job and current_job.start_date is None and ';' in line_stripped:
                semicolon_parts = [p.strip() for p in line_stripped.split(';')]
                total_months = 0
                first_start = None
                last_end = None
                for part in semicolon_parts:
                    for date_pattern in date_patterns:
                        dm = re.search(date_pattern, part, re.IGNORECASE)
                        if dm:
                            s = parse_date(dm.group(1))
                            end_str = dm.group(2)
                            if 'present' in end_str.lower() or 'current' in end_str.lower():
                                e = None
                                current_job.is_current = True
                            else:
                                e = parse_date(end_str)
                            if first_start is None:
                                first_start = s
                            last_end = e
                            if s:
                                end_d = e or date.today()
                                total_months += max(0, (end_d.year - s.year) * 12 + (end_d.month - s.month))
                            break
                if first_start:
                    current_job.start_date = first_start
                    current_job.end_date = last_end
                    current_job.duration_months = total_months
                    current_job.date_context = line_stripped
            else:
                for date_pattern in date_patterns:
                    date_match = re.search(date_pattern, line_stripped, re.IGNORECASE)
                    if date_match:
                        if current_job and current_job.start_date is None:
                            current_job.start_date = parse_date(date_match.group(1))
                            end_str = date_match.group(2)
                            if 'present' in end_str.lower() or 'current' in end_str.lower():
                                current_job.end_date = None
                                current_job.is_current = True
                            else:
                                current_job.end_date = parse_date(end_str)

                            # Capture full date line text for contract/part-time detection
                            current_job.date_context = line_stripped

                            # Calculate duration
                            if current_job.start_date:
                                end = current_job.end_date or date.today()
                                delta = (end.year - current_job.start_date.year) * 12 + (end.month - current_job.start_date.month)
                                current_job.duration_months = max(0, delta)
                        break

            # Check for bullet points (•, -, *, —, numbered lists, or no marker)
            bullet_match = re.match(r'^[•\-*—]\s+(.+)$|^\d+[.)]\s+(.+)$', line_stripped)
            if bullet_match and current_job:
                bullet = (bullet_match.group(1) or bullet_match.group(2) or "").strip()
                if bullet:
                    current_job.bullets.append(bullet)
                    profile.all_bullets.append(bullet)
            elif (current_job and current_job.start_date is not None
                  and not line_stripped.startswith(('•', '-', '*', '—'))
                  and '|' not in line_stripped
                  and len(line_stripped) > 20
                  and not any(re.search(dp, line_stripped, re.IGNORECASE) for dp in date_patterns)):
                # Plain sentence in experience section without bullet marker — treat as bullet
                current_job.bullets.append(line_stripped)
                profile.all_bullets.append(line_stripped)
            elif (in_experience_section and not job_match
                  and not line_stripped.startswith(('•', '-', '*', '—'))
                  and '|' not in line_stripped
                  and not any(re.search(dp, line_stripped, re.IGNORECASE) for dp in date_patterns)
                  and len(line_stripped) > 3 and len(line_stripped) < 80
                  and current_job is None):
                # Looks like a standalone title line (no-pipe format) — peek ahead for company+dates
                next_lines = [lines[k].strip() for k in range(i + 1, min(i + 5, len(lines))) if lines[k].strip()]
                has_date_nearby = any(
                    any(re.search(dp, nl, re.IGNORECASE) for dp in date_patterns)
                    for nl in next_lines[:3]
                )
                if has_date_nearby:
                    # Treat this line as a job title; next non-date line before the date is the company
                    pending_title = line_stripped
                    pending_company = ""
                    for nl in next_lines:
                        if any(re.search(dp, nl, re.IGNORECASE) for dp in date_patterns):
                            break
                        if nl and not nl.startswith(('•', '-', '*', '—')):
                            pending_company = nl
                    if current_job:
                        current_job.hierarchy_level = get_title_hierarchy_level(current_job.title)
                        profile.jobs.append(current_job)
                    current_job = JobEntry(title=pending_title, company=pending_company, location="")

        elif current_section == 'skills':
            # Extract skills (comma or bullet separated)
            if line_stripped.startswith(('•', '-', '*')):
                skill = re.sub(r'^[•\-*]\s*', '', line_stripped)
                profile.skills.append(skill)
            elif ',' in line_stripped or '•' in line_stripped:
                skills = re.split(r'[,•]', line_stripped)
                profile.skills.extend([s.strip() for s in skills if s.strip()])

        elif current_section == 'certifications':
            if line_stripped.startswith(('•', '-', '*')):
                cert = re.sub(r'^[•\-*]\s*', '', line_stripped)
                profile.certifications.append(cert)

        elif current_section == 'education':
            # Look for degree patterns
            degree_indicators = [
                'master', 'bachelor', 'doctor', 'ph.d', 'phd', 'm.d.', 'md',
                'm.b.b.s', 'mbbs', 'm.s.', 'ms', 'm.a.', 'ma', 'b.s.', 'bs',
                'b.a.', 'ba', 'mba', 'mph', 'pharmd', 'pharm.d',
                'diploma', 'certificate', 'degree', 'fellow'
            ]
            line_lower = line_stripped.lower()
            if any(d in line_lower for d in degree_indicators):
                edu = EducationEntry(
                    degree=line_stripped,
                    school=""
                )
                # Try to find school in next non-empty line
                for j in range(i + 1, min(i + 3, len(lines))):
                    next_line = lines[j].strip()
                    if next_line and not next_line.startswith(('•', '-', '*')):
                        # Check if it looks like a school name (not a date or bullet)
                        if not re.match(r'^\d{4}', next_line) and '|' not in next_line:
                            edu.school = next_line
                        break
                profile.education.append(edu)
            # Also handle "University" or "College" lines as school names
            elif any(term in line_lower for term in ['university', 'college', 'institute', 'school of']):
                if profile.education and not profile.education[-1].school:
                    profile.education[-1].school = line_stripped

    # Don't forget the last job
    if current_job:
        current_job.hierarchy_level = get_title_hierarchy_level(current_job.title)
        profile.jobs.append(current_job)

    # Calculate total experience
    total_months = sum(job.duration_months for job in profile.jobs)
    profile.total_years_experience = total_months / 12

    # If no jobs parsed but text has experience indicators, estimate from text
    if profile.total_years_experience == 0:
        years_match = re.search(r'(\d+)\+?\s*years?\s*(?:of\s+)?experience', text, re.IGNORECASE)
        if years_match:
            profile.total_years_experience = float(years_match.group(1))

    return profile


def parse_job_description(text: str) -> JobRequirements:
    """Parse job description into structured requirements"""
    requirements = JobRequirements(raw_text=text)

    # Extract title (usually first significant line)
    lines = [l.strip() for l in text.split('\n') if l.strip()]
    if lines:
        requirements.title = lines[0]

    # Extract company name
    company_patterns = [
        r'(?:at|for|join)\s+([A-Z][A-Za-z\s&]+?)(?:\s+as|\s+is|\.|,)',
        r'([A-Z][A-Za-z]+(?:\s+[A-Z][A-Za-z]+)*)\s+is\s+(?:looking|seeking|hiring)',
    ]
    for pattern in company_patterns:
        match = re.search(pattern, text)
        if match:
            requirements.company = match.group(1).strip()
            break

    # Extract required years
    years = extract_years_from_text(text)
    if years:
        requirements.required_years = years

    # Determine seniority
    requirements.seniority_level = determine_seniority_level(text)

    # Extract skills (look for bullet points in requirements section)
    skill_section = False
    for line in text.split('\n'):
        line_lower = line.lower()
        if any(term in line_lower for term in ['requirements', 'qualifications', 'must have', 'required']):
            skill_section = True
        elif any(term in line_lower for term in ['preferred', 'nice to have', 'bonus']):
            skill_section = False  # Switch to preferred

        if skill_section and line.strip().startswith(('•', '-', '*')):
            skill = re.sub(r'^[•\-*]\s*', '', line.strip())
            requirements.required_skills.append(skill)

    # Extract industry keywords (common terms in the JD)
    words = re.findall(r'\b[a-zA-Z]{4,}\b', text.lower())
    word_freq = defaultdict(int)
    for word in words:
        if word not in ['that', 'this', 'with', 'from', 'have', 'will', 'your', 'about', 'which']:
            word_freq[word] += 1

    # Top frequent meaningful words as keywords
    sorted_words = sorted(word_freq.items(), key=lambda x: x[1], reverse=True)
    requirements.industry_keywords = [w[0] for w in sorted_words[:30]]

    return requirements


# =============================================================================
# SCORING FUNCTIONS
# =============================================================================

def score_experience_trapezoidal(candidate_years: float, required_years: float) -> Tuple[float, str]:
    """
    Trapezoidal scoring for experience:
    - Under 50% requirement: Knockout (0)
    - 50-100% requirement: Ramp up
    - 100-150% requirement: Sweet spot (100)
    - Over 150%: Decay (overqualified risk)
    """
    C = candidate_years
    R = required_years

    if R <= 0:
        R = 1  # Prevent division by zero

    if C < 0.5 * R:
        return 0, f"Knockout: {C:.1f} years < {0.5*R:.1f} years minimum"

    elif C < R:
        score = ((C - 0.5 * R) / (0.5 * R)) * 100
        return score, f"Ramp Up: {C:.1f} years approaching {R:.1f} years required"

    elif C <= 1.5 * R:
        return 100, f"Sweet Spot: {C:.1f} years in ideal range ({R:.1f}-{1.5*R:.1f} years)"

    else:
        # Overqualified decay
        decay = 10 * (C - 1.5 * R)
        score = max(70, 100 - decay)  # Floor at 70
        return score, f"Overqualified: {C:.1f} years > {1.5*R:.1f} years (flight risk)"


def score_skills_contextual(
    candidate_skills: List[str],
    candidate_bullets: List[str],
    required_skills: List[str],
    jd_text: str
) -> Tuple[float, List[str], List[str]]:
    """
    Context-weighted skills scoring:
    - List mention: 1.0x
    - Sentence context (used in action): 2.0x
    - Main skill (appears in multiple roles): 3.0x
    """
    if not required_skills:
        # Extract skills from JD if not parsed
        required_skills = extract_skills_from_text(jd_text)

    if not required_skills:
        return 80, [], []  # No skills to match, give benefit of doubt

    matched_skills = []
    missing_skills = []
    total_weighted_score = 0
    max_possible_score = len(required_skills) * 3.0  # Max weight per skill

    # Combine all candidate text for matching
    candidate_text = ' '.join(candidate_skills + candidate_bullets).lower()
    skills_text = ' '.join(candidate_skills).lower()
    bullets_text = ' '.join(candidate_bullets).lower()

    for skill in required_skills:
        skill_lower = skill.lower()
        skill_words = skill_lower.split()

        # Check for match
        skill_found = False
        weight = 0

        # Check in bullets (action context) - highest weight
        if any(word in bullets_text for word in skill_words):
            weight = 2.0
            skill_found = True

            # Check if it appears in multiple job entries (main skill)
            appearances = sum(1 for b in candidate_bullets if any(w in b.lower() for w in skill_words))
            if appearances >= 3:
                weight = 3.0

        # Check in skills list
        elif any(word in skills_text for word in skill_words):
            weight = 1.0
            skill_found = True

        # General text check
        elif any(word in candidate_text for word in skill_words):
            weight = 0.5
            skill_found = True

        if skill_found:
            matched_skills.append(skill)
            total_weighted_score += weight
        else:
            missing_skills.append(skill)

    # Calculate percentage
    if max_possible_score > 0:
        score = (total_weighted_score / max_possible_score) * 100
    else:
        score = 50

    return min(100, score), matched_skills, missing_skills


def extract_skills_from_text(text: str) -> List[str]:
    """Extract skill-like phrases from job description"""
    # Common skill patterns
    skill_patterns = [
        r'\b(?:experience\s+(?:with|in)|knowledge\s+of|proficiency\s+in|expertise\s+in)\s+([A-Za-z\s,]+)',
        r'\b([A-Z][a-z]+(?:\s+[A-Z][a-z]+)*)\s+(?:experience|skills?|knowledge)',
    ]

    skills = []
    for pattern in skill_patterns:
        matches = re.findall(pattern, text, re.IGNORECASE)
        skills.extend([m.strip() for m in matches if len(m.strip()) > 2])

    # Also extract capitalized terms that look like technologies/tools
    tech_terms = re.findall(r'\b([A-Z][A-Za-z]+(?:\.[A-Za-z]+)?)\b', text)
    skills.extend([t for t in tech_terms if len(t) > 2 and t not in ['The', 'This', 'You', 'Our', 'Your']])

    return list(set(skills))[:20]  # Limit to top 20


def calculate_career_slope(jobs: List[JobEntry]) -> Tuple[float, str]:
    """
    Calculate career trajectory using linear regression on title hierarchy.
    Returns score (0-100) and narrative.
    """
    if len(jobs) < 2:
        return 75, "Insufficient data: Only 1 position to evaluate trajectory"

    # Sort jobs by start date
    sorted_jobs = sorted(
        [j for j in jobs if j.start_date],
        key=lambda x: x.start_date
    )

    if len(sorted_jobs) < 2:
        return 75, "Insufficient dated positions for trajectory analysis"

    # Create time series of hierarchy levels
    x_values = []  # Time index
    y_values = []  # Hierarchy level

    base_date = sorted_jobs[0].start_date
    for job in sorted_jobs:
        months_from_start = (job.start_date.year - base_date.year) * 12 + (job.start_date.month - base_date.month)
        x_values.append(months_from_start)
        y_values.append(job.hierarchy_level)

    # Simple linear regression
    n = len(x_values)
    sum_x = sum(x_values)
    sum_y = sum(y_values)
    sum_xy = sum(x * y for x, y in zip(x_values, y_values))
    sum_x2 = sum(x * x for x in x_values)

    # Calculate slope
    denominator = n * sum_x2 - sum_x * sum_x
    if denominator == 0:
        slope = 0
    else:
        slope = (n * sum_xy - sum_x * sum_y) / denominator

    # Convert slope to score
    # Positive slope = growth, negative = regression
    # Slope is per month, so annualize it
    annual_slope = slope * 12

    if annual_slope > 0.3:  # Growing more than 0.3 levels per year
        score = 100
        narrative = f"Fast Track: Rapid career progression (slope: {annual_slope:.2f} levels/year)"
    elif annual_slope > 0.1:
        score = 90
        narrative = f"Strong Growth: Consistent upward trajectory (slope: {annual_slope:.2f} levels/year)"
    elif annual_slope >= 0:
        score = 80
        narrative = f"Stable: Steady career (slope: {annual_slope:.2f} levels/year)"
    elif annual_slope > -0.1:
        score = 60
        narrative = f"Stagnant: Limited progression (slope: {annual_slope:.2f} levels/year)"
    else:
        score = 40
        narrative = f"Concerning: Apparent career regression (slope: {annual_slope:.2f} levels/year)"

    return score, narrative


# =============================================================================
# §3.1.1, §9.1 - F-PATTERN VISUAL SCORING
# =============================================================================

def score_f_pattern_compliance(resume_text: str, bullets: List[str]) -> Tuple[float, Dict[str, Any]]:
    """
    Score resume for F-Pattern reading compliance (§3.1.1, §9.1).

    Based on eye-tracking research showing recruiters scan in F-pattern:
    1. Top Header: Name, Title (Golden Triangle)
    2. Upper Left: Current Company, Current Title
    3. Scan Down: Looking for dates and titles on left rail
    4. Short Bursts: First few words of bullet points

    Scoring factors:
    - Golden Triangle content (top section with key info)
    - Left-rail data presence (titles, dates aligned left)
    - Bullet point economy (1-2 lines optimal, 3+ penalized)
    - Whitespace ratio (40-60% optimal)
    - Section header standardization

    Returns:
        score: 0-100 F-pattern compliance score
        details: Dictionary with analysis breakdown
    """
    lines = resume_text.split('\n')
    total_chars = len(resume_text)
    total_lines = len(lines)

    if total_lines < 10:
        return 50, {'error': 'Resume too short for F-pattern analysis'}

    # -------------------------------------------------------------------------
    # Golden Triangle Analysis (Top 20% of document)
    # -------------------------------------------------------------------------
    golden_triangle_lines = lines[:max(5, total_lines // 5)]
    golden_triangle_text = '\n'.join(golden_triangle_lines).lower()

    golden_triangle_score = 0
    golden_triangle_items = []

    # Check for essential elements in Golden Triangle
    essential_patterns = [
        (r'\b[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}\b', 'Email', 15),
        (r'\b\d{3}[-.]?\d{3}[-.]?\d{4}\b', 'Phone', 10),
        (r'\b(linkedin|github|portfolio)\b', 'Professional Link', 10),
        (r'\b(summary|profile|objective)\b', 'Summary Section', 10),
        (r'\b(senior|director|manager|lead|engineer|analyst)\b', 'Title Keywords', 10),
    ]

    for pattern, name, points in essential_patterns:
        if re.search(pattern, golden_triangle_text, re.IGNORECASE):
            golden_triangle_score += points
            golden_triangle_items.append(name)

    golden_triangle_score = min(50, golden_triangle_score)

    # -------------------------------------------------------------------------
    # Bullet Point Economy Analysis
    # -------------------------------------------------------------------------
    bullet_lengths = []
    long_bullets = 0
    short_bullets = 0

    for bullet in bullets:
        char_count = len(bullet)
        bullet_lengths.append(char_count)

        if char_count > 200:  # More than ~2 lines
            long_bullets += 1
        elif char_count < 50:  # Too short
            short_bullets += 1

    bullet_economy_score = 0
    if bullets:
        avg_bullet_length = sum(bullet_lengths) / len(bullet_lengths)

        # Optimal: 80-150 characters (1-2 lines)
        if 80 <= avg_bullet_length <= 150:
            bullet_economy_score = 20
        elif 50 <= avg_bullet_length <= 200:
            bullet_economy_score = 15
        else:
            bullet_economy_score = 10

        # Penalty for too many long bullets
        long_bullet_ratio = long_bullets / len(bullets)
        if long_bullet_ratio > 0.3:
            bullet_economy_score -= 5

    # -------------------------------------------------------------------------
    # Section Header Standardization
    # -------------------------------------------------------------------------
    standard_headers = [
        'experience', 'education', 'skills', 'summary', 'certifications',
        'professional experience', 'work experience', 'core competencies',
        'technical skills', 'professional summary', 'career summary'
    ]

    found_headers = []
    non_standard_headers = []

    header_patterns = [
        r'^[A-Z][A-Z\s&]+$',  # ALL CAPS headers
        r'^[A-Z][a-z]+(?:\s+[A-Z][a-z]+)*:?$',  # Title Case headers
    ]

    for line in lines:
        line_stripped = line.strip()
        if len(line_stripped) < 30:  # Headers are short
            for pattern in header_patterns:
                if re.match(pattern, line_stripped):
                    header_lower = line_stripped.lower().rstrip(':')
                    if any(std in header_lower for std in standard_headers):
                        found_headers.append(line_stripped)
                    else:
                        non_standard_headers.append(line_stripped)
                    break

    header_score = 0
    if found_headers:
        header_score = min(15, len(found_headers) * 5)
    if non_standard_headers:
        header_score -= min(5, len(non_standard_headers) * 2)

    header_score = max(0, header_score)

    # -------------------------------------------------------------------------
    # Whitespace Ratio Analysis
    # -------------------------------------------------------------------------
    whitespace_chars = resume_text.count(' ') + resume_text.count('\n') + resume_text.count('\t')
    text_chars = total_chars - whitespace_chars

    if total_chars > 0:
        text_ratio = text_chars / total_chars
        whitespace_ratio = 1 - text_ratio

        # Optimal: 40-60% text coverage
        if 0.4 <= text_ratio <= 0.6:
            whitespace_score = 15
        elif 0.3 <= text_ratio <= 0.7:
            whitespace_score = 10
        else:
            whitespace_score = 5
    else:
        whitespace_score = 0
        whitespace_ratio = 0

    # -------------------------------------------------------------------------
    # Combined Score
    # -------------------------------------------------------------------------
    total_score = golden_triangle_score + bullet_economy_score + header_score + whitespace_score
    total_score = max(0, min(100, total_score))

    details = {
        'golden_triangle': {
            'score': golden_triangle_score,
            'items_found': golden_triangle_items
        },
        'bullet_economy': {
            'score': bullet_economy_score,
            'avg_length': round(sum(bullet_lengths) / len(bullet_lengths), 1) if bullet_lengths else 0,
            'long_bullets': long_bullets,
            'total_bullets': len(bullets)
        },
        'headers': {
            'score': header_score,
            'standard_found': len(found_headers),
            'non_standard_found': len(non_standard_headers)
        },
        'whitespace': {
            'score': whitespace_score,
            'text_ratio': round(text_ratio * 100, 1) if total_chars > 0 else 0,
            'optimal_range': '40-60%'
        }
    }

    return total_score, details


def score_text_block_penalty(bullets: List[str], max_lines: int = 3) -> Tuple[float, Dict[str, Any]]:
    """
    Penalize excessively long bullet points that hurt readability.

    Bullets longer than max_lines (default 3) get -2 pts each, max -10 total.
    Recruiters spend 6-7 seconds on initial scan — long text blocks get skipped.

    Returns:
        penalty: Negative adjustment (0 to -10)
        details: Dict with offending bullets and their lengths
    """
    if not bullets:
        return 0, {'offending_bullets': 0, 'total_bullets': 0}

    offending = []
    for i, bullet in enumerate(bullets):
        # Estimate lines: ~80 chars per line in typical resume format
        estimated_lines = max(1, len(bullet) / 80)
        if estimated_lines > max_lines:
            offending.append({
                'index': i,
                'chars': len(bullet),
                'estimated_lines': round(estimated_lines, 1),
                'preview': bullet[:60] + '...' if len(bullet) > 60 else bullet
            })

    penalty = min(10, len(offending) * 2)  # -2 per offending bullet, max -10

    return -penalty, {
        'offending_bullets': len(offending),
        'total_bullets': len(bullets),
        'max_lines_threshold': max_lines,
        'details': offending[:5]  # Show first 5
    }


def check_page_length_penalty(resume_text: str, domain: str = None) -> Tuple[float, Dict[str, Any]]:
    """
    Apply domain-specific page length rules.

    - Finance: strict 1-page rule; >~3000 chars penalized
    - Healthcare/Clinical: multi-page acceptable; no penalty up to ~6000 chars
    - General: 1-2 pages; >~5000 chars penalized

    Returns:
        penalty: Negative adjustment (0 to -5)
        details: Dict with char count and domain rule applied
    """
    char_count = len(resume_text)
    # Rough page estimate: ~3000 chars/page with formatting
    estimated_pages = max(1, char_count / 3000)

    if domain in ('finance', 'consulting'):
        # Strict 1-page rule for finance/consulting
        if estimated_pages > 1.5:
            penalty = -5
            rule = "Finance/consulting: 1-page strongly preferred"
        else:
            penalty = 0
            rule = "Finance/consulting: within 1-page limit"
    elif domain in ('clinical_research', 'pharma_biotech', 'healthcare'):
        # Multi-page acceptable for clinical/healthcare (publications, certifications)
        if estimated_pages > 3:
            penalty = -3
            rule = "Healthcare/clinical: 2-3 pages acceptable, exceeds limit"
        else:
            penalty = 0
            rule = "Healthcare/clinical: multi-page acceptable"
    else:
        # General: 1-2 pages
        if estimated_pages > 2.5:
            penalty = -3
            rule = "General: 1-2 pages preferred, exceeds limit"
        else:
            penalty = 0
            rule = "General: within page limit"

    return penalty, {
        'char_count': char_count,
        'estimated_pages': round(estimated_pages, 1),
        'domain': domain or 'general',
        'rule': rule,
        'penalty': penalty
    }


def score_impact_density(bullets: List[str]) -> Tuple[float, Dict[str, Any]]:
    """
    Score based on density of impact indicators using Bloom's Taxonomy.

    Enhanced scoring:
    - Metrics (%, $, numbers) - weighted by magnitude
    - Verb Power Index using Bloom's Taxonomy classification:
      - Level 4 (Impact): generated, saved, increased
      - Level 3 (Strategy): spearheaded, architected, pioneered
      - Level 2 (Management): managed, led, directed
      - Level 1 (Execution): assisted, helped, created
      - Level 0 (Weak): responsible for, duties included
    """
    if not bullets:
        return 50, {'metrics': 0, 'strong_verbs': 0, 'total_bullets': 0, 'verb_power_avg': 0}

    metrics_count = 0
    metrics_magnitude_score = 0
    verb_power_scores = []
    level_4_verbs = 0
    level_3_verbs = 0
    level_2_verbs = 0
    weak_verbs = 0

    # Enhanced metric patterns with magnitude detection
    metric_patterns = [
        (r'(\d+)%', 'percent'),  # Percentages
        (r'\$[\d,.]+[MBK]', 'money_large'),  # Large dollar amounts (M/B/K)
        (r'\$[\d,]+', 'money'),  # Dollar amounts
        (r'\b\d{1,3}(?:,\d{3})+\b', 'large_number'),  # Large numbers with commas
        (r'\b\d+\s*(?:x|times)\b', 'multiplier'),  # Multipliers
        (r'\b(?:doubled|tripled|quadrupled)\b', 'multiplier_word'),  # Word multipliers
        (r'\b\d{2,}\b', 'number'),  # 2+ digit numbers
    ]

    # Flatten strong verbs (legacy fallback)
    all_strong_verbs = []
    for verb_list in STRONG_ACTION_VERBS.values():
        all_strong_verbs.extend(verb_list)

    for bullet in bullets:
        bullet_lower = bullet.lower()
        words = bullet_lower.split()

        # Check for metrics with magnitude scoring
        bullet_has_metric = False
        for pattern, metric_type in metric_patterns:
            matches = re.findall(pattern, bullet, re.IGNORECASE)
            if matches:
                bullet_has_metric = True
                # Weight by magnitude
                if metric_type == 'money_large':
                    metrics_magnitude_score += 3
                elif metric_type in ['multiplier', 'multiplier_word']:
                    metrics_magnitude_score += 2.5
                elif metric_type == 'percent':
                    metrics_magnitude_score += 2
                elif metric_type == 'large_number':
                    metrics_magnitude_score += 1.5
                else:
                    metrics_magnitude_score += 1
                break

        if bullet_has_metric:
            metrics_count += 1

        # Get verb power score using Bloom's Taxonomy
        if words:
            first_word = words[0].rstrip('ed').rstrip('ing')  # Basic normalization
            verb_score = get_verb_power_score(words[0])
            verb_power_scores.append(verb_score)

            # Track verb levels
            if verb_score >= 4:
                level_4_verbs += 1
            elif verb_score >= 3:
                level_3_verbs += 1
            elif verb_score >= 2:
                level_2_verbs += 1
            elif verb_score <= 0.5:
                weak_verbs += 1

    # Calculate scores
    total_bullets = len(bullets)
    avg_verb_power = sum(verb_power_scores) / len(verb_power_scores) if verb_power_scores else 1.5

    # Strong verb count (score >= 2)
    strong_verb_count = sum(1 for s in verb_power_scores if s >= 2)

    # Calculate density
    proof_points = metrics_count + strong_verb_count
    density = proof_points / total_bullets if total_bullets > 0 else 0

    # Calculate Verb Power Index (0-100)
    # avg_verb_power is 0-4, normalize to 0-100
    verb_power_index = min(100, (avg_verb_power / 4) * 100)

    # Metric magnitude bonus
    magnitude_bonus = min(20, metrics_magnitude_score * 2)

    # Combined score: base density + verb power contribution + magnitude bonus
    base_score = min(70, density * 175)  # Up to 70 from density
    verb_contribution = verb_power_index * 0.15  # Up to 15 from verb quality
    score = min(100, base_score + verb_contribution + magnitude_bonus)

    # Penalty for weak verbs
    weak_verb_ratio = weak_verbs / total_bullets if total_bullets > 0 else 0
    if weak_verb_ratio > 0.3:  # More than 30% weak verbs
        score -= 10

    # Metrics density enforcement: penalize if <30% of bullets have metrics
    metrics_density_pct = (metrics_count / total_bullets * 100) if total_bullets > 0 else 0
    metrics_density_penalty = 0
    if metrics_density_pct < 30:
        # Scale penalty: 0% metrics = -8, 15% = -4, 29% = -1
        metrics_density_penalty = round(max(1, (30 - metrics_density_pct) / 30 * 8))
        score -= metrics_density_penalty

    stats = {
        'metrics': metrics_count,
        'metrics_magnitude': round(metrics_magnitude_score, 1),
        'metrics_density_pct': round(metrics_density_pct, 1),
        'metrics_density_penalty': metrics_density_penalty,
        'strong_verbs': strong_verb_count,
        'total_bullets': total_bullets,
        'density': round(density * 100, 1),
        'verb_power_avg': round(avg_verb_power, 2),
        'verb_power_index': round(verb_power_index, 1),
        'level_4_verbs': level_4_verbs,  # Impact verbs (generated, saved)
        'level_3_verbs': level_3_verbs,  # Strategy verbs (spearheaded)
        'level_2_verbs': level_2_verbs,  # Management verbs (managed, led)
        'weak_verbs': weak_verbs  # Passive/weak verbs
    }

    return round(score, 1), stats


AI_CLICHE_VERBS = {
    # past tense (most common in resume bullets)
    'spearheaded', 'leveraged', 'utilized', 'facilitated', 'ensured',
    'demonstrated', 'collaborated', 'streamlined', 'championed', 'fostered',
    'harnessed', 'navigated', 'liaised', 'interfaced',
    # present/base forms
    'spearhead', 'leverage', 'utilize', 'facilitate', 'ensure',
    'demonstrate', 'collaborate', 'streamline', 'champion', 'foster',
    'harness', 'navigate', 'liaise', 'interface',
    # -ing forms
    'spearheading', 'leveraging', 'utilizing', 'facilitating', 'ensuring',
    'demonstrating', 'collaborating', 'streamlining', 'championing', 'fostering',
    'harnessing', 'navigating', 'liaising', 'interfacing',
}


def score_burstiness(bullets: List[str]) -> Tuple[float, Dict[str, Any]]:
    """
    Score sentence-length variability (burstiness).
    Coefficient of variation (std_dev / mean) of word counts.
    CV >= 0.40 = 100 (human-like), CV < 0.10 = 10 (AI-uniform).
    Also penalizes AI cliché verbs (max -15).
    """
    if not bullets or len(bullets) < 3:
        return 50, {'note': 'too few bullets'}
    word_counts = [len(b.split()) for b in bullets if b.strip()]
    mean_wc = sum(word_counts) / len(word_counts)
    std_dev = (sum((w - mean_wc) ** 2 for w in word_counts) / len(word_counts)) ** 0.5
    cv = std_dev / mean_wc if mean_wc else 0

    if cv >= 0.40:
        score = 100
    elif cv >= 0.30:
        score = 85 + (cv - 0.30) / 0.10 * 15
    elif cv >= 0.20:
        score = 60 + (cv - 0.20) / 0.10 * 25
    elif cv >= 0.10:
        score = 30 + (cv - 0.10) / 0.10 * 30
    else:
        score = max(10, cv / 0.10 * 30)

    cliche_hits = [
        b for b in bullets
        if b.strip() and b.strip().split()[0].lower().rstrip('.,;:') in AI_CLICHE_VERBS
    ]
    cliche_ratio = len(cliche_hits) / len(bullets)
    cliche_penalty = min(15, cliche_ratio * 30)

    return round(max(10, score - cliche_penalty), 1), {
        'word_counts': word_counts,
        'mean_word_count': round(mean_wc, 1),
        'std_dev': round(std_dev, 1),
        'coefficient_variation': round(cv, 3),
        'cliche_count': len(cliche_hits),
        'cliche_examples': [b[:70] for b in cliche_hits[:3]],
        'cliche_penalty': round(cliche_penalty, 1),
    }


def score_competitive(
    schools: List[EducationEntry],
    companies: List[str],
    certifications: List[str]
) -> Tuple[float, List[str]]:
    """
    Score based on prestige signals using expanded databases.

    Enhanced with:
    - Comprehensive company prestige database (company_prestige.json)
    - Detailed university rankings (university_rankings.json)
    - Industry-specific tiers (Pharma, CRO, MedTech, Healthcare)
    - Medical school and business school recognition
    """
    prestige_signals = []
    score = 50  # Base score
    company_boost = 0
    university_boost = 0

    # Check companies using expanded database
    all_companies_lower = ' '.join([c.lower() for c in companies])

    # Check JSON-loaded prestige data first (more comprehensive)
    for tier_name, tier_data in COMPANY_PRESTIGE_DATA.items():
        if isinstance(tier_data, dict) and 'companies' in tier_data:
            tier_boost = tier_data.get('score_boost', 10)
            tier_desc = tier_data.get('description', tier_name)

            for company in tier_data['companies']:
                if re.search(r'\b' + re.escape(company.lower()) + r'\b', all_companies_lower):
                    if tier_boost > company_boost:  # Take highest tier found
                        company_boost = tier_boost
                        prestige_signals = [s for s in prestige_signals if 'Company' not in s]
                        prestige_signals.append(f"{tier_desc}: {company.title()}")
                    break

    # Fallback to legacy COMPANY_PRESTIGE if no match found
    if company_boost == 0:
        for company in COMPANY_PRESTIGE.get('tier1', []):
            if re.search(r'\b' + re.escape(company) + r'\b', all_companies_lower):
                company_boost = 15
                prestige_signals.append(f"Tier 1 Company: {company.title()}")
                break

        if company_boost == 0:
            for company in COMPANY_PRESTIGE.get('tier2', []):
                if re.search(r'\b' + re.escape(company) + r'\b', all_companies_lower):
                    company_boost = 10
                    prestige_signals.append(f"Tier 2 Company: {company.title()}")
                    break

    score += company_boost

    # Check universities using expanded database
    for edu in schools:
        school_lower = edu.school.lower() if edu.school else ""
        degree_lower = edu.degree.lower() if edu.degree else ""
        combined = school_lower + " " + degree_lower

        # Check JSON-loaded university rankings
        for tier_name, tier_data in UNIVERSITY_RANKINGS_DATA.items():
            if isinstance(tier_data, dict) and 'universities' in tier_data:
                tier_boost = tier_data.get('score_boost', 10)
                tier_desc = tier_data.get('description', tier_name)

                for uni in tier_data['universities']:
                    if re.search(r'\b' + re.escape(uni.lower()) + r'\b', combined):
                        if tier_boost > university_boost:
                            university_boost = tier_boost
                            prestige_signals = [s for s in prestige_signals if 'University' not in s and 'Medical' not in s and 'Business' not in s]
                            prestige_signals.append(f"{tier_desc}: {uni.title()}")
                        break

        # Fallback to legacy UNIVERSITY_PRESTIGE
        if university_boost == 0:
            for uni in UNIVERSITY_PRESTIGE.get('tier1', []):
                if re.search(r'\b' + re.escape(uni) + r'\b', combined):
                    university_boost = 20
                    prestige_signals.append(f"Tier 1 University: {uni.title()}")
                    break

            if university_boost == 0:
                for uni in UNIVERSITY_PRESTIGE.get('tier2', []):
                    if re.search(r'\b' + re.escape(uni) + r'\b', combined):
                        university_boost = 10
                        prestige_signals.append(f"Tier 2 University: {uni.title()}")
                        break

    score += university_boost

    # Check for advanced degrees
    advanced_found = False
    for edu in schools:
        degree_lower = (edu.degree or "").lower()
        if any(d in degree_lower for d in ['ph.d', 'phd', 'doctorate', 'm.d', 'md', 'm.b.b.s', 'mbbs', 'do']):
            if not advanced_found:
                score += 10
                prestige_signals.append("Advanced Degree (PhD/MD/DO)")
                advanced_found = True
            break
        elif any(d in degree_lower for d in ['mba', 'master', 'mph', 'mpa', 'pharmd', 'ms', 'ma']):
            if not advanced_found:
                score += 5
                prestige_signals.append("Graduate Degree (Master's/PharmD)")
                advanced_found = True
            break

    # Check for relevant certifications
    cert_boost = 0
    valuable_certs = [
        ('pmp', 8, 'PMP Certification'),
        ('cra', 8, 'Clinical Research Associate'),
        ('ccrp', 8, 'Certified Clinical Research Professional'),
        ('cphq', 6, 'Certified Professional Healthcare Quality'),
        ('six sigma', 6, 'Six Sigma'),
        ('lean', 4, 'Lean Certification'),
        ('gcp', 5, 'Good Clinical Practice'),
        ('epic', 8, 'Epic Certification'),
    ]

    all_certs_lower = ' '.join([c.lower() for c in certifications])
    for cert_key, boost, cert_name in valuable_certs:
        if cert_key in all_certs_lower:
            if boost > cert_boost:
                cert_boost = boost
                prestige_signals.append(f"Certification: {cert_name}")

    score += cert_boost

    return min(100, score), prestige_signals


# =============================================================================
# JOB FIT ANALYSIS SCORING
# =============================================================================

def extract_job_fit_requirements(jd_text: str, jd_title: str) -> JobRequirements:
    """
    Enhanced JD parsing to extract job fit requirements.
    Thinks like HR: what does this role REALLY need?
    """
    jd_match_text = normalize_match_text(jd_text)
    requirements = JobRequirements(raw_text=jd_text, title=jd_title)

    # Extract therapeutic areas mentioned
    for ta in THERAPEUTIC_ADJACENCY.keys():
        if contains_term(jd_match_text, ta):
            requirements.therapeutic_areas.append(ta)
        # Also check direct synonyms
        adjacency = THERAPEUTIC_ADJACENCY.get(ta, {})
        for synonym in adjacency.get('direct', []):
            if contains_term(jd_match_text, synonym) and ta not in requirements.therapeutic_areas:
                requirements.therapeutic_areas.append(ta)

    # Extract experience types
    for exp_type in EXPERIENCE_TRANSFERABILITY.keys():
        if contains_term(jd_match_text, exp_type.replace('_', ' ')) or contains_term(jd_match_text, exp_type):
            requirements.experience_types.append(exp_type)
        # Check direct synonyms
        exp_data = EXPERIENCE_TRANSFERABILITY.get(exp_type, {})
        for synonym in exp_data.get('direct', []):
            if contains_term(jd_match_text, synonym) and exp_type not in requirements.experience_types:
                requirements.experience_types.append(exp_type)

    # Extract phase requirements
    phase_patterns = [
        (r'phase\s*[i1I]', 'phase i'),
        (r'phase\s*[ii2II]', 'phase ii'),
        (r'phase\s*[iii3III]', 'phase iii'),
        (r'phase\s*[iv4IV]', 'phase iv'),
        (r'phase\s*i[-–]iv', 'all phases'),
        (r'phases?\s*1[-–]4', 'all phases'),
    ]
    for pattern, phase in phase_patterns:
        if re.search(pattern, jd_text, re.IGNORECASE):
            if phase == 'all phases':
                requirements.required_phases = ['phase i', 'phase ii', 'phase iii', 'phase iv']
            elif phase not in requirements.required_phases:
                requirements.required_phases.append(phase)

    # Extract required degrees
    degree_patterns = [
        (r'\bmd\b|m\.d\.', 'md'),
        (r'\bdo\b|d\.o\.', 'do'),
        (r'\bmbbs\b|m\.b\.b\.s', 'mbbs'),
        (r'\bphd\b|ph\.d\.', 'phd'),
        (r'\bpharmd\b|pharm\.?d\.?', 'pharmd'),
        (r'\bmph\b|m\.p\.h', 'mph'),
        (r'\bmba\b|m\.b\.a', 'mba'),
    ]
    for pattern, degree in degree_patterns:
        if re.search(pattern, jd_text, re.IGNORECASE):
            if degree not in requirements.required_degrees:
                requirements.required_degrees.append(degree)

    # Extract preferred specializations (fellowship, board certification)
    specialization_patterns = [
        r'fellowship\s+(?:in\s+)?([a-zA-Z/\s]+)',
        r'board\s+certifi(?:ed|cation)\s+(?:in\s+)?([a-zA-Z/\s]+)',
        r'specialty\s+(?:in\s+)?([a-zA-Z/\s]+)',
    ]
    for pattern in specialization_patterns:
        matches = re.findall(pattern, jd_text, re.IGNORECASE)
        for match in matches:
            spec = match.strip().lower()
            if len(spec) > 2 and spec not in requirements.preferred_specializations:
                requirements.preferred_specializations.append(spec)

    # Determine if industry vs academic role
    industry_signals = ['pharma', 'biotech', 'industry', 'drug development', 'sponsor']
    academic_signals = ['academic', 'university', 'teaching', 'faculty', 'professor']

    industry_count = sum(1 for s in industry_signals if contains_term(jd_match_text, s))
    academic_count = sum(1 for s in academic_signals if contains_term(jd_match_text, s))
    requirements.is_industry_role = industry_count >= academic_count

    return requirements


def score_therapeutic_area_fit(
    candidate_text: str,
    required_areas: List[str]
) -> Tuple[float, str, List[str]]:
    """
    Score therapeutic area alignment using REALISTIC HR reasoning.

    Key principle: HR distinguishes between:
    - WORK EXPERIENCE in a therapeutic area (high value)
    - Just MENTIONING the area in publications/skills (low value)
    - TRANSFERABLE background (partial credit, but honest about gap)

    Returns:
        score (0-100), narrative, matched_areas
    """
    if not required_areas:
        return 80, "No specific therapeutic area required", []

    candidate_match_text = normalize_match_text(candidate_text)
    matched_areas = []
    match_details = []

    # Work experience indicators - these suggest ACTUAL experience
    work_indicators = [
        'worked in', 'experience in', 'specialized in', 'focused on',
        'treated', 'managed patients', 'managing patients', 'clinical experience',
        'drug development in', 'clinical trials in', 'research in',
        'rotation', 'rotations in', 'completed rotation', 'service',
        'patients with', 'patient management', 'patient care',
        'receiving treatment', 'active treatment', 'chemotherapy',
        'malignancies', 'tumors', 'blood disorders', 'attending',
        'coordinating', 'consulting', 'evaluated patients'
    ]

    # Weak indicators - just mentions, not real work experience
    weak_indicators = ['publication', 'published', 'article', 'paper', 'literature review',
                       'book chapter', 'journal', 'cureus', 'peer-reviewed']
    normalized_work_indicators = [normalize_match_text(ind) for ind in work_indicators]
    normalized_weak_indicators = [normalize_match_text(ind) for ind in weak_indicators]

    for area in required_areas:
        adjacency = THERAPEUTIC_ADJACENCY.get(area, {})
        area_terms = [area] + adjacency.get('direct', [])

        # Check for ACTUAL work experience (look for work context)
        has_work_experience = False
        for term in area_terms:
            if contains_term(candidate_match_text, term):
                # Check if it's in a work context vs just a mention
                # Look for the term near work indicators
                term_positions = find_term_positions(candidate_match_text, term)
                for pos in term_positions:
                    context_start = max(0, pos - 100)
                    context_end = min(len(candidate_match_text), pos + 100)
                    context = candidate_match_text[context_start:context_end]

                    # Check if this is work experience or just a publication mention
                    is_work = any(ind in context for ind in normalized_work_indicators)
                    is_weak = any(ind in context for ind in normalized_weak_indicators)

                    if is_work and not is_weak:
                        has_work_experience = True
                        matched_areas.append((area, 'direct_work', 100))
                        match_details.append(f"Direct {area.title()} work experience")
                        break

                if has_work_experience:
                    break

        if has_work_experience:
            continue

        # Check if area is mentioned but only in publications (low credit)
        area_mentioned = any(contains_term(candidate_match_text, term) for term in area_terms)
        if area_mentioned:
            matched_areas.append((area, 'mention_only', 30))
            match_details.append(f"CAUTION: {area.title()} mentioned (publications only, no direct experience)")
            continue

        # Check related experience (partial credit, honest about gap)
        related_match = False
        for related in adjacency.get('related', []):
            if contains_term(candidate_match_text, related):
                matched_areas.append((area, 'related', 50))
                match_details.append(f"Related but different: {related.title()} (not direct {area.title()})")
                related_match = True
                break

        if related_match:
            continue

        # Check transferable experience (low credit - being honest)
        transferable_match = False
        for transferable in adjacency.get('transferable', []):
            if contains_term(candidate_match_text, transferable):
                matched_areas.append((area, 'transferable', 35))
                match_details.append(f"Transferable only: {transferable.title()} (significant ramp-up needed for {area.title()})")
                transferable_match = True
                break

        if not transferable_match:
            matched_areas.append((area, 'none', 0))
            match_details.append(f"CRITICAL GAP: No {area.title()} experience found")

    # Calculate weighted score
    if not matched_areas or all(m[2] == 0 for m in matched_areas):
        score = 20  # Very low - missing required TA
        narrative = f"CRITICAL: No {', '.join(required_areas)} experience - significant gap for this role"
    else:
        total_weight = sum(m[2] for m in matched_areas)
        score = total_weight / len(required_areas)

        if score >= 90:
            narrative = f"Strong TA fit: {'; '.join(match_details[:2])}"
        elif score >= 70:
            narrative = f"Good TA fit: {'; '.join(match_details[:2])}"
        elif score >= 50:
            narrative = f"Partial TA fit: {'; '.join(match_details[:2])}"
        else:
            narrative = f"Weak TA fit: {'; '.join(match_details[:2])}"

    return score, narrative, [m[0] for m in matched_areas]


def score_experience_type_fit(
    candidate_text: str,
    candidate_bullets: List[str],
    required_types: List[str]
) -> Tuple[float, str]:
    """
    Score experience type alignment (clinical research, drug development, etc.)
    """
    if not required_types:
        return 80, "No specific experience type required"

    candidate_lower = candidate_text.lower()
    bullets_lower = ' '.join(candidate_bullets).lower()
    combined = candidate_lower + ' ' + bullets_lower

    matched_types = []

    for exp_type in required_types:
        exp_data = EXPERIENCE_TRANSFERABILITY.get(exp_type, {})

        # Direct match
        if exp_type.replace('_', ' ') in combined:
            matched_types.append((exp_type, 100))
            continue

        # Direct synonym match
        direct_found = False
        for direct in exp_data.get('direct', []):
            if direct in combined:
                matched_types.append((exp_type, 100))
                direct_found = True
                break
        if direct_found:
            continue

        # Related match
        related_found = False
        for related in exp_data.get('related', []):
            if related in combined:
                matched_types.append((exp_type, 70))
                related_found = True
                break
        if related_found:
            continue

        # Transferable
        for transferable in exp_data.get('transferable', []):
            if transferable in combined:
                matched_types.append((exp_type, 50))
                break

    if not matched_types:
        return 40, f"Limited experience type alignment with {', '.join(required_types)}"

    score = sum(m[1] for m in matched_types) / len(required_types)

    if score >= 90:
        narrative = f"Strong experience alignment with required {', '.join(required_types)}"
    elif score >= 70:
        narrative = f"Good experience alignment (some transferable)"
    else:
        narrative = f"Partial experience alignment - may need ramp-up time"

    return score, narrative


def score_phase_experience(
    candidate_text: str,
    required_phases: List[str]
) -> Tuple[float, str]:
    """
    Score clinical trial phase experience.
    HR understands: Phase I experience often transfers to other phases.
    """
    if not required_phases:
        return 80, "No specific phase experience required"

    candidate_lower = candidate_text.lower()

    # Check which phases candidate has
    candidate_phases = []
    for phase, data in PHASE_EXPERIENCE.items():
        if phase.replace(' ', '') in candidate_lower.replace(' ', ''):
            candidate_phases.append(phase)
            continue
        for direct in data.get('direct', []):
            if direct in candidate_lower:
                candidate_phases.append(phase)
                break

    if not candidate_phases:
        # Check for general clinical trials mention
        if 'clinical trial' in candidate_lower or 'clinical study' in candidate_lower:
            return 50, "General clinical trials experience (phases unspecified)"
        return 30, "Limited clinical trial phase experience evident"

    # Calculate coverage
    matched = set(candidate_phases) & set(required_phases)
    if len(matched) == len(required_phases):
        return 100, f"Complete phase coverage: {', '.join(matched)}"
    elif len(matched) > 0:
        # Partial credit + transferability consideration
        coverage = len(matched) / len(required_phases)
        # Phase experience is somewhat transferable
        score = 60 + (coverage * 40)
        return score, f"Partial phase coverage: {', '.join(matched)} (missing: {', '.join(set(required_phases) - matched)})"
    else:
        # Has phase experience but not the required ones - still valuable
        return 55, f"Has {', '.join(candidate_phases)} experience (can adapt to {', '.join(required_phases)})"


def score_education_fit(
    candidate_education: List[EducationEntry],
    candidate_text: str,
    required_degrees: List[str],
    preferred_specializations: List[str],
    role_title: str = ""
) -> Tuple[float, str]:
    """
    Score education alignment with REALISTIC expectations.

    For director-level roles:
    - Fellowship is practically expected (not just "nice to have")
    - Board certification is a strong differentiator
    - Missing these = significant disadvantage vs other candidates
    """
    candidate_lower = candidate_text.lower()
    role_lower = role_title.lower()

    # Is this a senior/director role where specialization matters more?
    is_senior_role = any(term in role_lower for term in
        ['director', 'senior', 'head', 'chief', 'vp', 'vice president', 'lead'])

    # Check required degrees (with equivalency)
    medical_degrees = {'md', 'do', 'mbbs'}
    has_medical_degree = any(d in candidate_lower for d in ['m.d.', 'md', 'mbbs', 'm.b.b.s', 'd.o.', 'do'])
    has_phd = any(d in candidate_lower for d in ['ph.d', 'phd', 'doctorate'])
    has_pharmd = any(d in candidate_lower for d in ['pharmd', 'pharm.d', 'doctor of pharmacy'])

    degree_score = 0
    degree_narrative = []

    if required_degrees:
        required_medical = set(required_degrees) & medical_degrees

        if required_medical and has_medical_degree:
            degree_score = 100
            degree_narrative.append("Medical degree requirement met (MD/MBBS)")
        elif 'phd' in required_degrees and has_phd:
            degree_score = 100
            degree_narrative.append("PhD requirement met")
        elif 'pharmd' in required_degrees and has_pharmd:
            degree_score = 100
            degree_narrative.append("PharmD requirement met")
        elif has_medical_degree or has_phd:
            degree_score = 70
            degree_narrative.append("Has advanced degree (may meet requirement)")
        else:
            degree_score = 30
            degree_narrative.append("CRITICAL: Required degree not found")
    else:
        degree_score = 80
        degree_narrative.append("No specific degree requirement")

    # Check fellowship - CRITICAL for director-level specialized roles
    has_fellowship = 'fellowship' in candidate_lower or 'fellow' in candidate_lower
    fellowship_score = 0

    if preferred_specializations:
        matched_specs = []
        for spec in preferred_specializations:
            # Check for fellowship in this specialization
            spec_pattern = rf'fellowship.*{re.escape(spec)}|{re.escape(spec)}.*fellowship'
            if re.search(spec_pattern, candidate_lower):
                matched_specs.append(spec)

        if matched_specs:
            fellowship_score = 30  # Strong bonus
            degree_narrative.append(f"Has fellowship in: {', '.join(matched_specs)}")
        elif has_fellowship:
            fellowship_score = 10  # Has fellowship but not in required area
            degree_narrative.append("Has fellowship (but not in required specialization)")
        else:
            if is_senior_role:
                # For director roles, missing fellowship is a SIGNIFICANT gap
                fellowship_score = -20  # Penalty
                degree_narrative.append("CONCERN: No fellowship training (expected for director-level)")
            else:
                fellowship_score = 0
                degree_narrative.append("No fellowship (less critical for this role level)")

    # Check board certification
    has_board_cert = any(term in candidate_lower for term in
        ['board certified', 'board-certified', 'board certification', 'diplomate'])

    board_score = 0
    if preferred_specializations:
        # Check if board certified in the required specialization
        board_in_spec = False
        for spec in preferred_specializations:
            cert_pattern = rf'board.{{0,20}}{re.escape(spec)}|{re.escape(spec)}.{{0,20}}board'
            if re.search(cert_pattern, candidate_lower):
                board_in_spec = True
                break

        if board_in_spec:
            board_score = 20
            degree_narrative.append(f"Board certified in required specialization")
        elif has_board_cert:
            board_score = 5
            degree_narrative.append("Board certified (but not in required specialization)")
        else:
            if is_senior_role:
                board_score = -10  # Penalty for senior roles
                degree_narrative.append("CONCERN: No board certification in specialization")
            else:
                degree_narrative.append("No board certification (may be acceptable)")

    # Calculate final score with caps
    raw_score = degree_score + fellowship_score + board_score
    final_score = max(20, min(100, raw_score))  # Floor at 20, cap at 100

    return final_score, '; '.join(degree_narrative)


def score_role_level_fit(
    candidate_jobs: List[JobEntry],
    target_title: str,
    candidate_years: float
) -> Tuple[float, str]:
    """
    Score whether candidate's career level aligns with target role.
    HR thinks: Is this a reasonable next step for them?
    """
    target_lower = target_title.lower()

    # Determine target role requirements
    target_requirements = None
    for role, reqs in ROLE_LEVEL_REQUIREMENTS.items():
        if role in target_lower:
            target_requirements = reqs
            break

    if not target_requirements:
        return 75, "Unable to assess role level fit"

    # Get candidate's current/recent level
    if not candidate_jobs:
        return 50, "Insufficient work history for level assessment"

    # Use most recent job's level
    current_level = candidate_jobs[0].hierarchy_level if candidate_jobs else 3

    # Find highest level achieved
    max_level = max(j.hierarchy_level for j in candidate_jobs) if candidate_jobs else 3

    expected_levels = target_requirements['expected_levels']

    if current_level in expected_levels:
        return 100, f"Level aligned: Current level {current_level} matches target range {expected_levels}"
    elif current_level < min(expected_levels):
        gap = min(expected_levels) - current_level
        if gap <= 1 and target_requirements.get('stretch_acceptable', True):
            return 80, f"Stretch candidate: {gap} level(s) below target (acceptable stretch)"
        elif gap <= 2:
            return 60, f"Significant stretch: {gap} levels below target"
        else:
            return 40, f"Large gap: {gap} levels below target role"
    else:  # current_level > max(expected_levels)
        gap = current_level - max(expected_levels)
        if gap <= 1:
            return 85, "Slightly senior for role (may be seeking work-life balance or new area)"
        else:
            return 65, f"Potentially overqualified: {gap} levels above target (flight risk)"

    return 75, "Role level assessment inconclusive"


def score_job_fit(
    candidate: CandidateProfile,
    jd: JobRequirements,
    verbose: bool = False
) -> Tuple[float, Dict[str, Any], List[str], List[str]]:
    """
    Main Job Fit scoring function.
    Combines all fit dimensions with human-like weighting.

    Returns:
        overall_score, component_scores, strengths, concerns
    """
    components = {}
    strengths = []
    concerns = []

    # 1. Therapeutic Area Fit (most important for specialized roles)
    ta_score, ta_narrative, matched_areas = score_therapeutic_area_fit(
        candidate.raw_text,
        jd.therapeutic_areas
    )
    components['therapeutic_area'] = {'score': ta_score, 'narrative': ta_narrative, 'weight': 0.30}
    if ta_score >= 80:
        strengths.append(ta_narrative)
    elif ta_score < 60:
        concerns.append(ta_narrative)

    # 2. Experience Type Fit
    exp_type_score, exp_type_narrative = score_experience_type_fit(
        candidate.raw_text,
        candidate.all_bullets,
        jd.experience_types
    )
    components['experience_type'] = {'score': exp_type_score, 'narrative': exp_type_narrative, 'weight': 0.25}
    if exp_type_score >= 80:
        strengths.append(exp_type_narrative)
    elif exp_type_score < 60:
        concerns.append(exp_type_narrative)

    # 3. Phase Experience
    phase_score, phase_narrative = score_phase_experience(
        candidate.raw_text,
        jd.required_phases
    )
    components['phase_experience'] = {'score': phase_score, 'narrative': phase_narrative, 'weight': 0.15}
    if phase_score >= 80:
        strengths.append(phase_narrative)
    elif phase_score < 60:
        concerns.append(phase_narrative)

    # 4. Education Fit (includes fellowship/board cert for director roles)
    edu_score, edu_narrative = score_education_fit(
        candidate.education,
        candidate.raw_text,
        jd.required_degrees,
        jd.preferred_specializations,
        jd.title  # Pass role title for seniority-aware scoring
    )
    components['education'] = {'score': edu_score, 'narrative': edu_narrative, 'weight': 0.20}
    if edu_score >= 80:
        strengths.append(edu_narrative)
    elif edu_score < 60:
        concerns.append(edu_narrative)

    # 5. Role Level Fit
    level_score, level_narrative = score_role_level_fit(
        candidate.jobs,
        jd.title,
        candidate.total_years_experience
    )
    components['role_level'] = {'score': level_score, 'narrative': level_narrative, 'weight': 0.10}
    if level_score >= 80:
        strengths.append(level_narrative)
    elif level_score < 60:
        concerns.append(level_narrative)

    # Calculate weighted overall score
    overall_score = sum(
        comp['score'] * comp['weight']
        for comp in components.values()
    )

    return overall_score, components, strengths, concerns


# =============================================================================
# RISK & PENALTY CALCULATIONS
# =============================================================================

def calculate_penalties(
    jobs: List[JobEntry],
    resume_text: str,
    jd_requirements: JobRequirements
) -> Tuple[float, Dict[str, float], List[str]]:
    """
    Calculate risk penalties:
    - Job hopping (avg tenure < 18 months)
    - Unexplained gaps
    - Location mismatch (if detectable)
    """
    penalties = {}
    concerns = []
    total_penalty = 0

    # 1. Job Hopping Check (context-aware: contract/temp/part-time roles get 50% penalty reduction)
    contract_keywords = {'contract', 'temporary', 'interim', 'consultant', 'consulting',
                         'freelance', 'locum', 'locums', 'per diem', 'prn', 'temp',
                         'part-time', 'part time', 'concurrent'}
    if len(jobs) >= 3:
        tenures = [j.duration_months for j in jobs if j.duration_months > 0]
        if tenures:
            avg_tenure = sum(tenures) / len(tenures)

            # Check if most short-tenure jobs are contract/temp/part-time roles
            # Check title AND date_context (date line annotations like "Per Diem, Concurrent")
            contract_count = sum(
                1 for j in jobs
                if any(kw in j.title.lower() for kw in contract_keywords)
                or any(kw in getattr(j, 'date_context', '').lower() for kw in contract_keywords)
            )
            is_primarily_contract = contract_count >= len(jobs) * 0.3  # 30%+ are contract/part-time/per-diem roles
            penalty_multiplier = 0.5 if is_primarily_contract else 1.0

            if avg_tenure < 12:
                base_penalty = 15
                penalties['job_hopping'] = round(base_penalty * penalty_multiplier)
                contract_note = " (reduced — contract/temp roles detected)" if is_primarily_contract else ""
                concerns.append(f"High turnover risk: Average tenure {avg_tenure:.0f} months (<12 months){contract_note}")
            elif avg_tenure < 18:
                base_penalty = 8
                penalties['job_hopping'] = round(base_penalty * penalty_multiplier)
                contract_note = " (reduced — contract/temp roles detected)" if is_primarily_contract else ""
                concerns.append(f"Moderate turnover risk: Average tenure {avg_tenure:.0f} months (<18 months){contract_note}")

    # 2. Gap Detection
    sorted_jobs = sorted(
        [j for j in jobs if j.start_date],
        key=lambda x: x.start_date
    )

    gaps_found = []
    for i in range(len(sorted_jobs) - 1):
        current_end = sorted_jobs[i].end_date or date.today()
        next_start = sorted_jobs[i + 1].start_date

        if next_start and current_end:
            gap_months = (next_start.year - current_end.year) * 12 + (next_start.month - current_end.month)
            if gap_months > 6:  # Gap > 6 months
                gaps_found.append(gap_months)

    if gaps_found:
        # Check for explanations
        text_lower = resume_text.lower()
        has_explanation = any(exp in text_lower for exp in GAP_EXPLANATIONS)

        if not has_explanation:
            max_gap = max(gaps_found)
            if max_gap > 24:
                penalties['unexplained_gap'] = 15
                concerns.append(f"Major unexplained gap: {max_gap} months")
            elif max_gap > 12:
                penalties['unexplained_gap'] = 10
                concerns.append(f"Unexplained gap: {max_gap} months")
            else:
                penalties['unexplained_gap'] = 5
                concerns.append(f"Minor gap: {max_gap} months (may warrant question)")

    # 3. Recent job instability (last 3 years)
    three_years_ago = date.today().replace(year=date.today().year - 3)
    recent_jobs = [j for j in jobs if j.start_date and j.start_date >= three_years_ago]

    if len(recent_jobs) >= 3:
        penalties['recent_instability'] = 5
        concerns.append(f"Recent instability: {len(recent_jobs)} positions in last 3 years")

    total_penalty = sum(penalties.values())
    return total_penalty, penalties, concerns


def detect_edge_cases(
    candidate: CandidateProfile,
    experience_score: float,
    skills_score: float
) -> Tuple[str, List[str]]:
    """
    Detect special cases that may require weight adjustment:
    - Career Changer: Low exp relevance but high skills
    - Return-to-work: Gaps with explanation
    - Bootcamper: Non-traditional education path
    """
    tags = []
    mode = 'normal'

    # Career Changer Detection
    if experience_score < 50 and skills_score > 75:
        tags.append("Career Changer - Pivot Candidate")
        mode = 'pivot'

    # Check for bootcamp/non-traditional
    resume_lower = candidate.raw_text.lower()
    if any(term in resume_lower for term in ['bootcamp', 'coding bootcamp', 'self-taught', 'online certification']):
        tags.append("Non-Traditional Background")

    # Check for return-to-work signals
    if any(term in resume_lower for term in GAP_EXPLANATIONS[:6]):  # Family/health related
        tags.append("Return-to-Work Candidate")

    # International experience
    if any(term in resume_lower for term in ['international', 'global', 'abroad', 'overseas', 'expat']):
        tags.append("International Experience")

    return mode, tags


# =============================================================================
# MAIN SCORING ENGINE
# =============================================================================

def calculate_hr_score(
    resume_path: str,
    jd_path: str,
    config: Optional[Dict] = None
) -> HRScoreResult:
    """
    Main HR scoring function.

    Args:
        resume_path: Path to resume file (PDF, DOCX, MD, TXT)
        jd_path: Path to job description file
        config: Optional configuration overrides

    Returns:
        HRScoreResult with comprehensive scoring and feedback
    """
    # 1. PARSE INPUTS
    resume_text = extract_text_from_file(resume_path)
    jd_text = extract_text_from_file(jd_path)

    candidate = parse_resume(resume_text)
    jd = parse_job_description(jd_text)

    # 1b. EXTRACT JOB FIT REQUIREMENTS (enhanced parsing)
    jd_fit = extract_job_fit_requirements(jd_text, jd.title)
    # Merge into jd object
    jd.therapeutic_areas = jd_fit.therapeutic_areas
    jd.experience_types = jd_fit.experience_types
    jd.required_phases = jd_fit.required_phases
    jd.required_degrees = jd_fit.required_degrees
    jd.preferred_specializations = jd_fit.preferred_specializations
    jd.is_industry_role = jd_fit.is_industry_role

    # 2. KNOCKOUT CHECK
    if candidate.total_years_experience < 0.3 * jd.required_years:
        return HRScoreResult(
            overall_score=0,
            recommendation="AUTO-REJECT",
            rating_label="Knockout - Insufficient Experience",
            confidence="High (95%)",
            factor_breakdown=ScoreBreakdown(),
            penalties_applied={},
            strengths=[],
            concerns=[f"Experience knockout: {candidate.total_years_experience:.1f} years vs {jd.required_years:.1f} required"],
            suggested_questions=[],
            candidate_tags=["Knockout"],
            weights_used={}
        )

    # 3. COMPONENT SCORING
    scores = ScoreBreakdown()
    strengths = []
    concerns = []

    # A. Experience Score
    exp_score, exp_narrative = score_experience_trapezoidal(
        candidate.total_years_experience,
        jd.required_years
    )
    scores.experience = exp_score
    if exp_score >= 80:
        strengths.append(exp_narrative)
    elif exp_score < 60:
        concerns.append(exp_narrative)

    # B. Skills Score
    skills_score, matched, missing = score_skills_contextual(
        candidate.skills,
        candidate.all_bullets,
        jd.required_skills,
        jd.raw_text
    )
    scores.skills = skills_score
    if matched:
        strengths.append(f"Skills Match: {len(matched)} of {len(matched) + len(missing)} required skills")
    if missing:
        concerns.append(f"Missing Skills: {', '.join(missing[:3])}")

    # C. Trajectory Score
    traj_score, traj_narrative = calculate_career_slope(candidate.jobs)
    scores.trajectory = traj_score
    if traj_score >= 90:
        strengths.append(traj_narrative)
    elif traj_score < 60:
        concerns.append(traj_narrative)

    # D. Impact Score
    impact_score, impact_stats = score_impact_density(candidate.all_bullets)
    scores.impact = impact_score
    density = impact_stats.get('density', 0)
    if density >= 30:
        strengths.append(f"High Impact Density: {density:.0f}% of bullets contain metrics/strong verbs")
    elif density < 15:
        concerns.append(f"Low Impact Quantification: Only {density:.0f}% of bullets contain metrics")

    # E. Competitive Score
    companies = [j.company for j in candidate.jobs]
    comp_score, prestige_signals = score_competitive(
        candidate.education,
        companies,
        candidate.certifications
    )
    scores.competitive = comp_score
    if prestige_signals:
        strengths.extend(prestige_signals[:2])  # Top 2 prestige signals

    # F. JOB FIT SCORE (NEW - Domain/Role Alignment)
    job_fit_score, job_fit_components, job_fit_strengths, job_fit_concerns = score_job_fit(
        candidate,
        jd
    )
    scores.job_fit = job_fit_score
    strengths.extend(job_fit_strengths[:2])  # Top 2 fit strengths
    concerns.extend(job_fit_concerns[:2])    # Top 2 fit concerns

    # G. F-PATTERN VISUAL SCORING (§3.1.1, §9.1)
    f_pattern_score, f_pattern_details = score_f_pattern_compliance(
        resume_text,
        candidate.all_bullets
    )
    # F-Pattern is used as a quality adjustment (±5 points max)
    f_pattern_adjustment = 0
    if f_pattern_score >= 80:
        f_pattern_adjustment = 5
        strengths.append(f"Excellent visual format: F-Pattern score {f_pattern_score:.0f}/100")
    elif f_pattern_score >= 60:
        f_pattern_adjustment = 2
    elif f_pattern_score < 40:
        f_pattern_adjustment = -3
        concerns.append(f"Poor visual format: F-Pattern score {f_pattern_score:.0f}/100 - may reduce recruiter engagement")

    # G2. TEXT BLOCK PENALTY (v2.5: penalize bullets >3 lines)
    text_block_penalty, text_block_details = score_text_block_penalty(candidate.all_bullets)
    if text_block_penalty < 0:
        concerns.append(f"Long bullet points detected: {text_block_details['offending_bullets']} bullets exceed 3 lines")

    # G3. DOMAIN-SPECIFIC PAGE LENGTH CHECK (v2.5)
    # Auto-detect domain from JD text for page length rules
    page_domain = None
    jd_lower = jd_text.lower() if jd_text else ""
    if any(kw in jd_lower for kw in ['clinical trial', 'fda', 'pharmacovigilance', 'patient safety']):
        page_domain = 'clinical_research'
    elif any(kw in jd_lower for kw in ['investment', 'trading', 'portfolio', 'banking', 'equity']):
        page_domain = 'finance'
    elif any(kw in jd_lower for kw in ['consulting', 'strategy', 'advisory']):
        page_domain = 'consulting'
    elif any(kw in jd_lower for kw in ['patient care', 'nursing', 'hospital', 'medical']):
        page_domain = 'healthcare'
    page_length_penalty, page_length_details = check_page_length_penalty(resume_text, domain=page_domain)
    if page_length_penalty < 0:
        concerns.append(f"Page length: {page_length_details['rule']}")

    # 4. EDGE CASE DETECTION & MODE SWITCHING
    mode, tags = detect_edge_cases(candidate, scores.experience, scores.skills)
    candidate.tags.extend(tags)

    # 5. SELECT WEIGHTS
    if mode == 'pivot':
        weights = WEIGHT_PROFILES['pivot']
    else:
        weights = WEIGHT_PROFILES.get(jd.seniority_level, WEIGHT_PROFILES['mid'])

    # 6. CALCULATE RAW SCORE
    raw_score = (
        scores.experience * weights['experience'] +
        scores.skills * weights['skills'] +
        scores.trajectory * weights['trajectory'] +
        scores.impact * weights['impact'] +
        scores.competitive * weights['competitive'] +
        scores.job_fit * weights['job_fit']
    )

    # 7. APPLY PENALTIES
    penalty_total, penalty_breakdown, penalty_concerns = calculate_penalties(
        candidate.jobs,
        resume_text,
        jd
    )
    concerns.extend(penalty_concerns)

    final_score = max(0, min(100, raw_score - penalty_total + f_pattern_adjustment + text_block_penalty + page_length_penalty))

    # 8. DETERMINE RECOMMENDATION (considering job_fit specifically)
    # Job fit is critical - low job_fit should impact recommendation even if other scores are high
    job_fit_is_low = scores.job_fit < 60
    job_fit_is_marginal = 60 <= scores.job_fit < 75

    if final_score >= 85 and not job_fit_is_low:
        recommendation = "STRONG INTERVIEW"
        rating_label = "Strong Candidate"
    elif final_score >= 70 and not job_fit_is_low:
        if job_fit_is_marginal:
            recommendation = "INTERVIEW"
            rating_label = "Competitive (but role may be a stretch)"
        else:
            recommendation = "INTERVIEW"
            rating_label = "Competitive"
    elif final_score >= 55 or (final_score >= 50 and not job_fit_is_low):
        recommendation = "MAYBE"
        rating_label = "Marginal - Screening Call Recommended"
    elif job_fit_is_low and final_score >= 60:
        recommendation = "MAYBE"
        rating_label = "Skills strong but Job Fit is weak - STRETCH candidate"
    else:
        recommendation = "PASS"
        rating_label = "Weak Match"

    # Add stretch/reach tag if job_fit is significantly below other scores
    avg_other_scores = (scores.experience + scores.skills + scores.trajectory + scores.impact + scores.competitive) / 5
    if scores.job_fit < avg_other_scores - 20:
        candidate.tags.append("STRETCH: Strong general profile but weak fit for THIS specific role")

    # 9. GENERATE INTERVIEW QUESTIONS
    questions = generate_interview_questions(
        scores,
        penalty_breakdown,
        candidate,
        impact_stats
    )

    # 10. CALCULATE CONFIDENCE
    data_completeness = min(100, len(candidate.jobs) * 15 + len(candidate.all_bullets) * 2)
    confidence = f"{'High' if data_completeness > 70 else 'Medium' if data_completeness > 40 else 'Low'} ({data_completeness:.0f}%)"

    return HRScoreResult(
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
        weights_used=weights
    )


def generate_interview_questions(
    scores: ScoreBreakdown,
    penalties: Dict[str, float],
    candidate: CandidateProfile,
    impact_stats: Dict
) -> List[str]:
    """Generate relevant interview questions based on scoring gaps"""
    questions = []

    if 'unexplained_gap' in penalties:
        questions.append("I notice a gap in your employment history. Can you walk me through that period?")

    if 'job_hopping' in penalties:
        questions.append("You've had several transitions recently. What's driving your interest in a longer-term opportunity?")

    if scores.impact < 60:
        questions.append(f"Your resume mentions accomplishments but lacks specific metrics. Can you quantify the impact of your work at your most recent role?")

    if impact_stats.get('strong_verbs', 0) < impact_stats.get('total_bullets', 1) * 0.3:
        questions.append("Tell me about a time you took initiative to drive a project or improvement without being asked.")

    if scores.trajectory < 70:
        questions.append("Where do you see your career heading in the next 3-5 years?")

    if "Career Changer" in candidate.tags:
        questions.append("What's motivating your transition into this field, and how do your previous experiences prepare you for this role?")

    # Default question if none triggered
    if not questions:
        questions.append("What attracted you to this specific opportunity?")

    return questions


def result_to_dict(result: HRScoreResult) -> Dict[str, Any]:
    """Convert HRScoreResult to JSON-serializable dictionary"""
    d = {
        "overall_score": result.overall_score,
        "recommendation": result.recommendation,
        "rating_label": result.rating_label,
        "confidence": result.confidence,
        "factor_breakdown": result.factor_breakdown.to_dict(),
        "penalties_applied": result.penalties_applied,
        "strengths": result.strengths,
        "concerns": result.concerns,
        "suggested_questions": result.suggested_questions,
        "candidate_tags": result.candidate_tags,
        "weights_used": {k: round(v, 2) for k, v in result.weights_used.items()}
    }
    if result.writing_quality:
        d["writing_quality"] = result.writing_quality
    return d


def calculate_hr_score_from_text(resume_text: str, jd_text: str) -> "HRScoreResult":
    """Score a resume against a JD, both provided as plain text strings.

    This mirrors the scoring logic in scorer_server.py /score/hr so the
    /score/combined endpoint can call it without needing file paths.
    """
    candidate = parse_resume(resume_text)
    jd = parse_job_description(jd_text)

    jd_fit = extract_job_fit_requirements(jd_text, jd.title)
    jd.therapeutic_areas = jd_fit.therapeutic_areas
    jd.experience_types = jd_fit.experience_types
    jd.required_phases = jd_fit.required_phases
    jd.required_degrees = jd_fit.required_degrees
    jd.preferred_specializations = jd_fit.preferred_specializations
    jd.is_industry_role = jd_fit.is_industry_role

    if candidate.total_years_experience < 0.3 * jd.required_years:
        return HRScoreResult(
            overall_score=0,
            recommendation="AUTO-REJECT",
            rating_label="Knockout - Insufficient Experience",
            confidence="High (95%)",
            factor_breakdown=ScoreBreakdown(),
            penalties_applied={},
            strengths=[],
            concerns=[f"Experience knockout: {candidate.total_years_experience:.1f} years vs {jd.required_years:.1f} required"],
            suggested_questions=[],
            candidate_tags=["Knockout"],
            weights_used={},
        )

    scores = ScoreBreakdown()
    strengths: List[str] = []
    concerns: List[str] = []

    exp_score, exp_narrative = score_experience_trapezoidal(candidate.total_years_experience, jd.required_years)
    scores.experience = exp_score
    if exp_score >= 80:
        strengths.append(exp_narrative)
    elif exp_score < 60:
        concerns.append(exp_narrative)

    skills_score, matched, missing = score_skills_contextual(candidate.skills, candidate.all_bullets, jd.required_skills, jd.raw_text)
    scores.skills = skills_score
    if matched:
        strengths.append(f"Skills Match: {len(matched)} of {len(matched) + len(missing)} required skills")
    if missing:
        concerns.append(f"Missing Skills: {', '.join(missing[:3])}")

    traj_score, traj_narrative = calculate_career_slope(candidate.jobs)
    scores.trajectory = traj_score
    if traj_score >= 90:
        strengths.append(traj_narrative)
    elif traj_score < 60:
        concerns.append(traj_narrative)

    impact_score, impact_stats = score_impact_density(candidate.all_bullets)
    scores.impact = impact_score
    density = impact_stats.get("density", 0)
    if density >= 30:
        strengths.append(f"High Impact Density: {density:.0f}% of bullets contain metrics/strong verbs")
    elif density < 15:
        concerns.append(f"Low Impact Quantification: Only {density:.0f}% of bullets contain metrics")

    companies = [j.company for j in candidate.jobs]
    comp_score, prestige_signals = score_competitive(candidate.education, companies, candidate.certifications)
    scores.competitive = comp_score
    if prestige_signals:
        strengths.extend(prestige_signals[:2])

    job_fit_score, _, job_fit_strengths, job_fit_concerns = score_job_fit(candidate, jd)
    scores.job_fit = job_fit_score
    strengths.extend(job_fit_strengths[:2])
    concerns.extend(job_fit_concerns[:2])

    f_pattern_score, _ = score_f_pattern_compliance(resume_text, candidate.all_bullets)
    f_pattern_adjustment = 5 if f_pattern_score >= 80 else (2 if f_pattern_score >= 60 else (-3 if f_pattern_score < 40 else 0))

    # v2.5: Text block penalty + page length check
    text_block_penalty, text_block_details = score_text_block_penalty(candidate.all_bullets)
    if text_block_penalty < 0:
        concerns.append(f"Long bullet points: {text_block_details['offending_bullets']} bullets exceed 3 lines")

    page_domain = None
    jd_lower = jd_text.lower() if jd_text else ""
    if any(kw in jd_lower for kw in ['clinical trial', 'fda', 'pharmacovigilance', 'patient safety']):
        page_domain = 'clinical_research'
    elif any(kw in jd_lower for kw in ['investment', 'trading', 'portfolio', 'banking', 'equity']):
        page_domain = 'finance'
    elif any(kw in jd_lower for kw in ['consulting', 'strategy', 'advisory']):
        page_domain = 'consulting'
    elif any(kw in jd_lower for kw in ['patient care', 'nursing', 'hospital', 'medical']):
        page_domain = 'healthcare'
    page_length_penalty, _ = check_page_length_penalty(resume_text, domain=page_domain)
    if page_length_penalty < 0:
        concerns.append("Resume exceeds recommended page length for this domain")

    mode, tags = detect_edge_cases(candidate, scores.experience, scores.skills)
    candidate.tags.extend(tags)

    weights = WEIGHT_PROFILES.get("pivot" if mode == "pivot" else jd.seniority_level, WEIGHT_PROFILES["mid"])

    raw_score = (
        scores.experience * weights["experience"]
        + scores.skills * weights["skills"]
        + scores.trajectory * weights["trajectory"]
        + scores.impact * weights["impact"]
        + scores.competitive * weights["competitive"]
        + scores.job_fit * weights["job_fit"]
    )

    penalty_total, penalty_breakdown, penalty_concerns = calculate_penalties(candidate.jobs, resume_text, jd)
    concerns.extend(penalty_concerns)

    burstiness_score, burstiness_stats = score_burstiness(candidate.all_bullets)
    cv = burstiness_stats.get('coefficient_variation', 0)
    if cv < 0.15:
        burstiness_penalty = -8
    elif cv < 0.25:
        burstiness_penalty = -4
    else:
        burstiness_penalty = 0

    final_score = max(0, min(100, raw_score - penalty_total + f_pattern_adjustment + text_block_penalty + page_length_penalty + burstiness_penalty))

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

    questions = generate_interview_questions(scores, penalty_breakdown, candidate, impact_stats)
    data_completeness = min(100, len(candidate.jobs) * 15 + len(candidate.all_bullets) * 2)
    confidence = f"{'High' if data_completeness > 70 else 'Medium' if data_completeness > 40 else 'Low'} ({data_completeness:.0f}%)"

    writing_quality = {
        **burstiness_stats,
        'burstiness_score': burstiness_score,
        'burstiness_penalty': burstiness_penalty,
        'quantification_rate': impact_stats.get('density', 0),
    }

    return HRScoreResult(
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


# =============================================================================
# CLI INTERFACE
# =============================================================================

def print_score_report(result: HRScoreResult):
    """Print formatted score report to console"""
    print("\n" + "=" * 70)
    print("  HR COGNITIVE SIMULATION ENGINE (HR-CSE) REPORT")
    print("=" * 70)

    # Overall Score (ASCII-compatible bar)
    filled = int(result.overall_score / 5)
    score_bar = "#" * filled + "-" * (20 - filled)
    print(f"\n  OVERALL SCORE: {result.overall_score:.1f}/100  [{score_bar}]")
    print(f"  RECOMMENDATION: {result.recommendation}")
    print(f"  RATING: {result.rating_label}")
    print(f"  CONFIDENCE: {result.confidence}")

    if result.candidate_tags:
        print(f"  TAGS: {', '.join(result.candidate_tags)}")

    # Factor Breakdown
    print("\n" + "-" * 70)
    print("  FACTOR BREAKDOWN")
    print("-" * 70)

    breakdown = result.factor_breakdown.to_dict()
    weights = result.weights_used

    for factor, score in breakdown.items():
        weight = weights.get(factor, 0) * 100
        filled = int(score / 5)
        bar = "#" * filled + "-" * (20 - filled)
        weighted_contrib = score * weights.get(factor, 0)
        print(f"  {factor.upper():15} {score:5.1f}  [{bar}]  (Weight: {weight:.0f}%, Contrib: {weighted_contrib:.1f})")

    # Penalties
    if result.penalties_applied:
        print("\n" + "-" * 70)
        print("  PENALTIES APPLIED")
        print("-" * 70)
        for penalty, value in result.penalties_applied.items():
            print(f"  [-] {penalty.replace('_', ' ').title()}: -{value:.1f} points")

    # Strengths
    print("\n" + "-" * 70)
    print("  TOP STRENGTHS")
    print("-" * 70)
    for strength in result.strengths:
        print(f"  [+] {strength}")

    # Concerns
    if result.concerns:
        print("\n" + "-" * 70)
        print("  CONCERNS / RISKS")
        print("-" * 70)
        for concern in result.concerns:
            print(f"  [!] {concern}")

    # Interview Questions
    if result.suggested_questions:
        print("\n" + "-" * 70)
        print("  SUGGESTED INTERVIEW QUESTIONS")
        print("-" * 70)
        for i, q in enumerate(result.suggested_questions, 1):
            print(f"  {i}. {q}")

    print("\n" + "=" * 70 + "\n")


def generate_html_report(result: HRScoreResult) -> str:
    """Generate HTML report for web interface"""
    score_color = (
        "#22c55e" if result.overall_score >= 85 else
        "#84cc16" if result.overall_score >= 70 else
        "#eab308" if result.overall_score >= 55 else
        "#ef4444"
    )

    breakdown = result.factor_breakdown.to_dict()

    factors_html = ""
    for factor, score in breakdown.items():
        weight = result.weights_used.get(factor, 0) * 100
        bar_width = score
        factors_html += f"""
        <div class="factor-row">
            <div class="factor-name">{factor.title()}</div>
            <div class="factor-bar-container">
                <div class="factor-bar" style="width: {bar_width}%"></div>
            </div>
            <div class="factor-score">{score:.1f}</div>
            <div class="factor-weight">({weight:.0f}%)</div>
        </div>
        """

    strengths_html = "".join(f"<li class='strength'>✓ {s}</li>" for s in result.strengths)
    concerns_html = "".join(f"<li class='concern'>⚠ {c}</li>" for c in result.concerns)
    questions_html = "".join(f"<li>{q}</li>" for q in result.suggested_questions)
    tags_html = "".join(f"<span class='tag'>{t}</span>" for t in result.candidate_tags)

    penalties_html = ""
    if result.penalties_applied:
        penalties_html = "<h3>Penalties Applied</h3><ul>"
        for p, v in result.penalties_applied.items():
            penalties_html += f"<li>{p.replace('_', ' ').title()}: -{v:.1f} pts</li>"
        penalties_html += "</ul>"

    return f"""
    <!DOCTYPE html>
    <html>
    <head>
        <title>HR-CSE Score Report</title>
        <style>
            * {{ box-sizing: border-box; margin: 0; padding: 0; }}
            body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
                   background: #0f172a; color: #e2e8f0; padding: 20px; }}
            .container {{ max-width: 900px; margin: 0 auto; }}
            .header {{ text-align: center; margin-bottom: 30px; }}
            .header h1 {{ color: #f8fafc; font-size: 24px; margin-bottom: 10px; }}
            .score-circle {{
                width: 180px; height: 180px; border-radius: 50%;
                background: conic-gradient({score_color} {result.overall_score * 3.6}deg, #1e293b {result.overall_score * 3.6}deg);
                display: flex; align-items: center; justify-content: center; margin: 20px auto;
            }}
            .score-inner {{
                width: 150px; height: 150px; border-radius: 50%; background: #0f172a;
                display: flex; flex-direction: column; align-items: center; justify-content: center;
            }}
            .score-value {{ font-size: 48px; font-weight: bold; color: {score_color}; }}
            .score-label {{ font-size: 14px; color: #94a3b8; }}
            .recommendation {{
                display: inline-block; padding: 8px 20px; border-radius: 20px;
                background: {score_color}; color: #0f172a; font-weight: bold; margin: 10px 0;
            }}
            .rating {{ color: #94a3b8; font-size: 14px; }}
            .tags {{ margin: 15px 0; }}
            .tag {{
                display: inline-block; background: #1e293b; padding: 4px 12px;
                border-radius: 12px; font-size: 12px; margin: 2px; color: #94a3b8;
            }}
            .section {{ background: #1e293b; border-radius: 12px; padding: 20px; margin: 20px 0; }}
            .section h3 {{ color: #f8fafc; margin-bottom: 15px; font-size: 16px; }}
            .factor-row {{ display: flex; align-items: center; margin: 10px 0; }}
            .factor-name {{ width: 120px; color: #94a3b8; font-size: 14px; }}
            .factor-bar-container {{ flex: 1; height: 8px; background: #0f172a; border-radius: 4px; margin: 0 10px; }}
            .factor-bar {{ height: 100%; background: linear-gradient(90deg, #3b82f6, #8b5cf6); border-radius: 4px; }}
            .factor-score {{ width: 50px; text-align: right; font-weight: bold; }}
            .factor-weight {{ width: 50px; text-align: right; color: #64748b; font-size: 12px; }}
            ul {{ list-style: none; }}
            li {{ padding: 8px 0; border-bottom: 1px solid #334155; font-size: 14px; }}
            li:last-child {{ border-bottom: none; }}
            .strength {{ color: #22c55e; }}
            .concern {{ color: #f59e0b; }}
            .two-col {{ display: grid; grid-template-columns: 1fr 1fr; gap: 20px; }}
            @media (max-width: 600px) {{ .two-col {{ grid-template-columns: 1fr; }} }}
        </style>
    </head>
    <body>
        <div class="container">
            <div class="header">
                <h1>HR Cognitive Simulation Engine</h1>
                <div class="score-circle">
                    <div class="score-inner">
                        <div class="score-value">{result.overall_score:.0f}</div>
                        <div class="score-label">HR SCORE</div>
                    </div>
                </div>
                <div class="recommendation">{result.recommendation}</div>
                <div class="rating">{result.rating_label} • Confidence: {result.confidence}</div>
                <div class="tags">{tags_html}</div>
            </div>

            <div class="section">
                <h3>Factor Breakdown</h3>
                {factors_html}
            </div>

            {f'<div class="section">{penalties_html}</div>' if penalties_html else ''}

            <div class="two-col">
                <div class="section">
                    <h3>Strengths</h3>
                    <ul>{strengths_html}</ul>
                </div>
                <div class="section">
                    <h3>Concerns</h3>
                    <ul>{concerns_html if concerns_html else '<li style="color:#64748b">No major concerns identified</li>'}</ul>
                </div>
            </div>

            <div class="section">
                <h3>Suggested Interview Questions</h3>
                <ul>{questions_html}</ul>
            </div>
        </div>
    </body>
    </html>
    """


def run_web_interface(resume_path: str, jd_path: str, port: int = 8081):
    """Run simple web interface for HR scoring"""
    from http.server import HTTPServer, SimpleHTTPRequestHandler
    import webbrowser
    import threading

    # Generate report
    result = calculate_hr_score(resume_path, jd_path)
    html_content = generate_html_report(result)

    # Save temp HTML
    temp_path = os.path.join(os.path.dirname(resume_path), 'hr_score_report.html')
    with open(temp_path, 'w', encoding='utf-8') as f:
        f.write(html_content)

    print(f"\nHR Score Report saved to: {temp_path}")
    print(f"Opening in browser...")
    webbrowser.open(f'file://{os.path.abspath(temp_path)}')


def main():
    parser = argparse.ArgumentParser(
        description="HR Cognitive Simulation Engine (HR-CSE) - Simulate HR recruiter evaluation",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python hr_scorer.py --score resume.pdf job_description.txt
  python hr_scorer.py --score resume.docx jd.txt --json
  python hr_scorer.py --web resume.pdf jd.txt
        """
    )

    parser.add_argument('--score', nargs=2, metavar=('RESUME', 'JD'),
                       help='Score resume against job description')
    parser.add_argument('--json', action='store_true',
                       help='Output results as JSON')
    parser.add_argument('--web', action='store_true',
                       help='Open results in web browser')
    parser.add_argument('--port', type=int, default=8081,
                       help='Port for web interface (default: 8081)')

    args = parser.parse_args()

    if args.score:
        resume_path, jd_path = args.score

        if not os.path.exists(resume_path):
            print(f"Error: Resume file not found: {resume_path}")
            return 1

        if not os.path.exists(jd_path):
            print(f"Error: Job description file not found: {jd_path}")
            return 1

        result = calculate_hr_score(resume_path, jd_path)

        if args.json:
            print(json.dumps(result_to_dict(result), indent=2))
        elif args.web:
            run_web_interface(resume_path, jd_path, args.port)
        else:
            print_score_report(result)

    else:
        parser.print_help()

    return 0


if __name__ == '__main__':
    exit(main())
