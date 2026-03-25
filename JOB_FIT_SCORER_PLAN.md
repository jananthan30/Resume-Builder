# Job Fit Scorer — Implementation Plan

## Problem
Applying to jobs with hard disqualifiers (e.g., "3 years independent monitoring" when you have 0) wastes 15+ minutes of resume tailoring, API costs, and application effort — only to get instantly auto-rejected by Workday knockout questions.

## Solution
A pre-application screening module that runs in <5 seconds with zero API cost, detecting knockouts and scoring fit BEFORE any resume work begins.

## Implementation Steps

### Step 1: Core Module — `job_fit_scorer.py` [LARGE]
- [x] JD requirement extraction (regex + NLP)
- [x] Candidate profile builder (from master resume, cached)
- [x] Knockout detection (binary hard checks)
- [x] 7-dimension fit scoring (SBERT + NLP, no LLM)
- [x] Gap analysis (fixable vs unfixable)
- [x] Alternative job title suggestions
- [x] Main function: `calculate_job_fit(resume_text, jd_text) -> JobFitResult`

### Step 2: Server Endpoint — `scorer_server.py` [SMALL]
- [x] Add `POST /score/job-fit` endpoint
- [x] Reuse existing `ScoreRequest` model
- [x] Add caching (SHA-256 key, 24h TTL)

### Step 3: Custom Command — `.claude/commands/job-fit.md` [SMALL]
- [x] New `/job-fit` slash command for quick pre-screening
- [x] Display formatted GO/NO-GO report

### Step 4: Pre-Check in `/resume` — `.claude/commands/resume.md` [SMALL]
- [x] Add Phase 0.5: Job Fit pre-check before any tailoring
- [x] NO-GO = stop immediately with explanation
- [x] WEAK FIT = ask user before proceeding

### Step 5: Pre-Check in `/tailor-resume` — `.claude/commands/tailor-resume.md` [SMALL]
- [x] Same Phase 0.5 as Step 4

### Step 6: Job Discovery Integration — `job_discovery.py` [SMALL]
- [ ] Add knockout check to `lightweight_score()`
- [ ] Show GO/NO-GO in `/find-jobs` results

### Step 7: Test Suite — `tests/test_job_fit.py` [MEDIUM]
- [ ] Test requirement extraction (years, degrees, certs, travel, visa)
- [ ] Test knockout detection with real JDs (ICON, IQVIA)
- [ ] Test dimension scoring
- [ ] Test gap analysis output
- [ ] Test edge cases (missing sections, vague JDs)

## Architecture

```
job_fit_scorer.py
├── extract_requirements(jd_text) -> JDRequirements
│   ├── _extract_years_experience(text) -> (total, specific_dict)
│   ├── _extract_degree_requirements(text) -> str
│   ├── _extract_certifications(text) -> List[str]
│   ├── _extract_travel_requirement(text) -> float
│   ├── _extract_visa_requirement(text) -> bool
│   ├── _extract_seniority(title, years) -> str
│   ├── _split_required_vs_preferred(text) -> (required, preferred)
│   └── _extract_tools_platforms(text) -> List[str]
│
├── build_candidate_profile(resume_text) -> CandidateProfile
│   ├── _calculate_years_by_type(jobs) -> Dict[str, float]
│   ├── _extract_therapeutic_areas(text) -> List[str]
│   ├── _extract_tools_from_resume(text) -> List[str]
│   └── _infer_highest_degree(education) -> str
│
├── check_knockouts(profile, requirements) -> KnockoutResult
│   ├── _check_experience_knockout()
│   ├── _check_education_knockout()
│   ├── _check_certification_knockout()
│   ├── _check_travel_knockout()
│   └── _check_visa_knockout()
│
├── score_fit_dimensions(profile, requirements) -> FitDimensions
│   ├── _score_experience_match()      # 25%
│   ├── _score_skills_match()          # 25%
│   ├── _score_title_alignment()       # 15%
│   ├── _score_domain_match()          # 15%
│   ├── _score_education_match()       # 10%
│   ├── _score_certification_match()   # 5%
│   └── _score_seniority_match()       # 5%
│
├── analyze_gaps(profile, requirements, dimensions) -> GapAnalysis
│   ├── _identify_fixable_gaps()
│   ├── _identify_unfixable_gaps()
│   └── _suggest_alternatives()
│
└── calculate_job_fit(resume_text, jd_text) -> JobFitResult  [MAIN]
    └── Orchestrates all above, returns final score + recommendation
```

## Data Classes

```python
JDRequirements     # Structured JD extraction
CandidateProfile   # Structured resume profile (cached)
KnockoutFlag       # Single knockout issue
KnockoutResult     # All knockouts (passed: bool)
FitDimensions      # 7 dimension scores
Gap                # Single gap (fixable/unfixable)
GapAnalysis        # All gaps + suggestions
JobFitResult       # Final output (score, recommendation, knockouts, gaps)
```

## Recommendation Thresholds

| Score | Knockouts | Recommendation |
|-------|-----------|----------------|
| Any   | Hard knockout | NO-GO |
| 75+   | None | STRONG FIT — proceed |
| 55-74 | None | MODERATE FIT — proceed with modifications |
| 35-54 | None | WEAK FIT — ask user |
| 0-34  | None | POOR FIT — don't apply |

## Key Design Decisions
- Zero API cost (all local: regex, NLTK, SBERT)
- Reuses existing infrastructure (detect_domain, extract_jd_keywords, parse_resume, SBERT embeddings)
- Profile is parsed once from master resume and cached
- Knockout detection is the critical feature — catches what ATS/HR scorers miss
- Gap analysis distinguishes "fix with resume changes" vs "fundamental mismatch"
