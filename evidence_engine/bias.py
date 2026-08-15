# evidence_engine/bias.py
"""Protected-attribute screening for extracted requirements.

Automated employment decision tools are regulated (NYC Local Law 144,
California's automated-decision regulations, EEOC/DOJ ADA guidance, EU AI Act
Annex III). The engineering consequence is concrete: a job description line
that encodes a protected characteristic must never become a scored criterion,
and the exclusion must be visible rather than silent.

Screened requirements are not deleted. They are recorded with a category and
the exact source text, excluded from every weight and score, and surfaced as a
warning so a human can see what the engine refused to score and why.

Allowed job-related constraints are deliberately never screened: work
authorization, location, travel, licences, certifications and degrees are
legitimate eligibility criteria under the spec, even though careless pattern
matching would confuse some of them with protected attributes.
"""
from __future__ import annotations

import re
from typing import Iterable

from evidence_engine.models import ExcludedRequirement, RequirementType

# Requirement types the spec explicitly sanctions as eligibility criteria.
# They are exempt from screening so "authorized to work in the US" is never
# mistaken for national-origin discrimination.
_ALLOWED_TYPES = {
    RequirementType.WORK_AUTHORIZATION, RequirementType.LOCATION,
    RequirementType.TRAVEL, RequirementType.LICENSE,
    RequirementType.CERTIFICATION, RequirementType.DEGREE,
}

# Patterns describe a demand on the candidate's person, not a topic they may
# legitimately have worked on. "Disability insurance experience" is a real
# clinical/insurance skill; "must be able-bodied" is not a job requirement.
_PROTECTED: list[tuple[str, str]] = [
    ("age", r"\b(?:under|over|below|above|younger than|older than)\s+\d{2}\s*"
            r"(?:years?\s*(?:old|of\s*age))?\b"),
    ("age", r"\bage\s+(?:requirement|limit|range|bracket)\b"),
    # Intervening adjectives are common ("young energetic professionals"), but
    # the noun list stays candidate-specific so clinical phrasing such as
    # "young adult patient populations" is not mistaken for a candidate demand.
    ("age", r"\b(?:young|youthful|digital native)s?\s+(?:\w+\s+){0,2}"
            r"(?:candidate|applicant|professional|graduate|hire|worker)s?\b"),
    ("age", r"\brecent\s+(?:college\s+)?grad(?:uate)?s?\s+"
            r"(?:only|preferred|required)\b"),
    ("date_of_birth", r"\b(?:date of birth|d\.?o\.?b\.?)\b"),
    ("sex_or_gender", r"\b(?:male|female)\s+(?:candidate|applicant|only|"
                      r"preferred|required)s?\b"),
    # "sex-specific dosing" and "sex specific analysis" are real clinical
    # terms, so only role-directed phrasing counts.
    ("sex_or_gender", r"\b(?:gender|sex)\s+(?:requirement|preference|"
                      r"restriction)s?\b"),
    ("sexual_orientation", r"\bsexual orientation\b"),
    # "race condition" is standard software vocabulary; "race to market" is a
    # business idiom. Neither describes a candidate characteristic.
    ("race_or_ethnicity", r"\brace\b(?!\s*(?:condition|car|track|to\b))"),
    ("race_or_ethnicity", r"\b(?:racial|ethnicity|ethnic background|"
                          r"national origin)\b"),
    ("religion", r"\breligio(?:n|us)\s+(?:affiliation|background|belief|"
                 r"requirement|observance)\b"),
    ("marital_or_family", r"\b(?:marital status|pregnan(?:t|cy)|childbearing|"
                          r"family status|number of children)\b"),
    ("disability_or_medical", r"\bmust be (?:able[- ]bodied|in good health|"
                              r"physically fit)\b"),
    ("disability_or_medical", r"\bno\s+(?:disabilities|medical conditions|"
                              r"health (?:issues|problems))\b"),
    ("genetic_information", r"\bgenetic\s+(?:information|testing|history|"
                            r"screening)\b"),
    ("veteran_status", r"\bprotected veteran\b"),
    ("photograph", r"\b(?:photo(?:graph)?|headshot)\b[^.]{0,40}\b"
                   r"(?:required|attach|include|submit|with your)\b"),
    ("photograph", r"\b(?:attach|include|submit|send)\b[^.]{0,40}\b"
                   r"(?:photo(?:graph)?|headshot)\b"),
]

# Proxies: excluded unless the job independently requires them. The spec is
# explicit that graduation year, ZIP code, prestige and employment gaps are
# bias-prone stand-ins rather than qualifications.
_PROXIES: list[tuple[str, str]] = [
    ("graduation_year", r"\bgraduat(?:ed|ion)\s+(?:year|date|between|after|"
                        r"before|in)\s*\d{4}\b"),
    ("graduation_year", r"\bclass of\s+\d{4}\b"),
    ("zip_code", r"\b(?:zip|postal)\s*code\b"),
    ("school_prestige", r"\b(?:ivy league|top[- ]?(?:\d+\s*)?(?:ranked|tier)|"
                        r"tier[- ]?1|elite|prestigious)\s+"
                        r"(?:school|universit(?:y|ies)|college|institution|"
                        r"program)s?\b"),
    ("employer_prestige", r"\b(?:fortune\s*(?:500|100|50)|big[- ]?(?:4|four)|"
                          r"top[- ]?tier|blue[- ]chip|brand[- ]name)\s+"
                          r"(?:compan(?:y|ies)|firm|employer|organi[sz]ation)s?\b"),
    ("employment_gap", r"\b(?:no|without|zero)\s+(?:employment\s+|career\s+|"
                       r"resume\s+)?gaps?\b"),
    ("employment_gap", r"\b(?:continuous|uninterrupted)\s+employment\s+"
                       r"(?:history|record)\b"),
]

_COMPILED = [(cat, re.compile(p, re.I), False) for cat, p in _PROTECTED] + \
            [(cat, re.compile(p, re.I), True) for cat, p in _PROXIES]


def classify_text(text: str) -> tuple[str, bool] | None:
    """Return (category, is_proxy) for the first protected pattern matched."""
    for category, pattern, is_proxy in _COMPILED:
        if pattern.search(text or ""):
            return category, is_proxy
    return None


def screen_requirements(
    requirements: Iterable,
) -> tuple[list, list[ExcludedRequirement]]:
    """Split requirements into scoreable ones and protected-attribute exclusions.

    Excluded requirements carry no weight and produce no score, so a protected
    characteristic can never move Qualification Evidence Fit in either
    direction.
    """
    kept, excluded = [], []
    for req in requirements:
        if req.type in _ALLOWED_TYPES:
            kept.append(req)
            continue
        hit = classify_text(req.source_text) or classify_text(req.normalized_text)
        if hit is None:
            kept.append(req)
            continue
        category, is_proxy = hit
        excluded.append(ExcludedRequirement(
            requirement_id=req.requirement_id,
            source_text=req.source_text,
            category=category,
            is_proxy=is_proxy,
            reason=(
                f"This line references {category.replace('_', ' ')}, which is a "
                + ("bias-prone proxy" if is_proxy else "protected attribute")
                + ". It was excluded from scoring and carries no weight."
            ),
        ))
    return kept, excluded


def exclusion_warnings(excluded: Iterable[ExcludedRequirement]) -> list[str]:
    return [
        f"Excluded from scoring ({e.category}): {e.source_text.strip()!r}"
        for e in excluded
    ]
