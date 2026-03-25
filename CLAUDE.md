# Resume Builder - Project Context

## Overview
AI-powered resume and cover letter generator that tailors applications to specific job descriptions with **dual-scoring optimization** and **parallel agent execution (Swarm v3.0)**:
- **ATS Score**: Keyword matching for Applicant Tracking Systems (75-85% target)
- **HR Score**: Human recruiter evaluation simulation (70%+ target)
- **Authenticity**: Resumes must read naturally to human HR reviewers
- **Speed**: Background agents run scoring, DOCX creation, and cover letter in parallel

## Quick Start

### Option 1: Claude Code Custom Commands (Recommended)
```bash
# Full application package (resume + cover letter) - Dual optimized
/resume [paste job description]

# Resume only - Dual optimized
/tailor-resume [paste job description]

# Cover letter only
/cover-letter [paste job description]
```

### Option 2: Scorers (Standalone)
```bash
# ATS Scorer - Keyword matching
python ats_scorer.py --score resume.pdf job_description.txt --json
python ats_scorer.py --web

# HR Scorer - Recruiter simulation
python hr_scorer.py --score resume.pdf job_description.txt --json
python hr_scorer.py --score resume.pdf jd.txt --web
```

## Project Structure
```
Resume Builder/
├── {master_resume from config.json}        # Master resume (DOCX, PDF, or Markdown)
├── SUPPLEMENTAL_EXPERIENCE.md              # Selective-use experience entries (NOT in master resume)
├── ats_scorer.py                           # ATS scoring engine (keyword matching)
├── hr_scorer.py                            # HR scoring engine (recruiter simulation)
├── docx_generator.py                       # ATS-compliant DOCX generator
├── tracker_utils.py                        # Job application tracker utilities
├── Job_Application_Tracker.xlsx            # Auto-updated application tracker
├── orchestration_state.py                  # Shared state.json manager for multi-agent workflow
├── llm_scorer.py                           # LLM-augmented scoring (Claude-based rubric scorer)
├── scorer_server.py                        # FastAPI server for ATS/HR/LLM scoring
├── resume_builder.py                       # Main CLI tool
├── config.json                             # User configuration
├── requirements.txt                        # Python dependencies
├── embed_cache/                            # SBERT embedding cache (disk-based .npy files)
├── applications/                           # Output directory
│   └── {Company} - {JobTitle}/            # Company + role folders
│       ├── *_Resume_*.docx                # Final resume (DOCX only)
│       ├── *_Cover_Letter_*.docx          # Final cover letter (DOCX only)
│       ├── job_description.txt            # Original JD for reference
│       └── state.json                     # Orchestration state (transient, deleted after run)
└── .claude/
    └── commands/
        ├── resume.md                       # Full application command (Swarm v3.0)
        ├── tailor-resume.md               # Resume only command (Swarm v3.0)
        ├── cover-letter.md                # Cover letter command
        └── writing-coach.md               # Writing enhancement skill (Rules 1-10)
```

## Dual Scoring System (v2.0 Enhanced)

### ATS Scorer (Semantic + Keyword Matching)
Advanced resume-to-job-description analysis with seven weighted components:

| Metric | Weight | Description |
|--------|--------|-------------|
| Keyword Match | 22% | Lemmatized keywords with synonym expansion |
| Semantic Similarity | 22% | Sentence Transformers vector matching |
| Weighted Industry Terms | 18% | Domain-specific terminology with decay |
| Phrase Match | 13% | Multi-word industry phrase detection |
| BM25 Score | 13% | Probabilistic relevance ranking |
| Graph Centrality | 7% | NetworkX skill inference bonus |
| Skill Recency | 5% | Exponential decay for older skills |

**New v2.0 Features:**
- **Domain Auto-Detection**: clinical_research, pharma_biotech, technology, finance, consulting, healthcare
- **Skill Graph Inference**: Infers missing skills from related skills (e.g., Pandas+NumPy→Python)
- **Experience Decay**: Skills used recently weighted higher via λ decay constants
- **Hidden Text Detection**: Flags white text, tiny fonts, zero-width characters
- **Readability Analysis**: Flesch-Kincaid optimal Grade 10-12
- **Format Risk Assessment**: Detects tables, text boxes, header/footer content

**Score Ratings:**
- **80-100%**: Excellent - Top Candidate
- **65-79%**: Good - Strong Match
- **50-64%**: Fair - Competitive
- **35-49%**: Low - Below Average
- **0-34%**: Poor - Unlikely Match

### HR Scorer (Recruiter Simulation + Visual Analysis)
Simulates human HR recruiter evaluation with six factors plus visual scoring:

| Factor | Weight (Mid-Level) | Description |
|--------|-------------------|-------------|
| Experience Fit | 30% | Years match, Goldilocks zone, relevance |
| Skills Match | 20% | Contextual skill demonstration (action vs listed) |
| Career Trajectory | 20% | Title hierarchy regression slope |
| Impact Signals | 20% | Metrics density + Bloom's Taxonomy verb power |
| Competitive Edge | 10% | Prestige signals (companies/universities) |
| Job Fit | Weighted | Domain/role alignment score |
| **F-Pattern Visual** | ±5pt | Golden triangle, bullet economy, whitespace |

**New v2.0 Features:**
- **F-Pattern Scoring**: Eye-tracking research compliance (golden triangle, left-rail)
- **Bloom's Taxonomy Verbs**: 4-level verb power classification
- **Job Fit Analysis**: Domain-specific role alignment scoring
- **Bias Audit Mode**: PII stripping for blind hiring support

**Risk Penalties:**
- Job Hopping: -8 to -15 points (avg tenure < 18 months)
- Unexplained Gaps: -5 to -15 points
- Recent Instability: -5 points (3+ jobs in 3 years)

**HR Recommendations:**
- **85%+**: STRONG INTERVIEW (Top Candidate)
- **70-84%**: INTERVIEW (Competitive)
- **55-69%**: MAYBE (Marginal)
- **<55%**: PASS (Weak Match)

### Domain-Specific Scoring Profiles
The scorer auto-detects domain and applies appropriate adjustments:

| Domain | Key Adjustments |
|--------|-----------------|
| **Clinical/Pharma** | Publications bonus, transferable skills mapping |
| **Finance** | Deal artifacts required, 1.5x prestige weight, strict formatting |
| **Technology** | Portfolio links bonus, 1.3x skill recency weight, title validation |
| **Consulting** | Impact metrics required, 1.4x prestige weight, education weight 1.3x |
| **Healthcare** | Certifications required, quality improvement focus |

## ATS/Workday Format Rules

### DO NOT Use
- Columns or tables
- Text boxes
- Graphics or icons
- Headers/footers (put contact in main body)
- Fancy fonts or colors

### DO Use
- **Bold text** for job titles and metrics
- ALL-CAPS for section headers
- Bullet points (•)
- Horizontal lines (___) to separate sections
- Safe fonts: Calibri, Arial, Roboto (10-12pt body, 14-16pt headers)

### Resume Structure (Workday Pattern)
```
FULL NAME, CREDENTIALS
City, State ZIP | Phone | Email
LinkedIn URL

_______________________________________________________________________________
PROFESSIONAL SUMMARY

[3-4 lines with JD keywords]

_______________________________________________________________________________
CORE COMPETENCIES

• Keyword 1    • Keyword 2    • Keyword 3
• Keyword 4    • Keyword 5    • Keyword 6

_______________________________________________________________________________
PROFESSIONAL EXPERIENCE

JOB TITLE | COMPANY NAME | Location
Month Year – Present

• [Action verb] [task] resulting in [quantified metrics]
• Managed 8 centers, ensuring 100% compliance

_______________________________________________________________________________
EDUCATION

Degree Name
University, Location | Years

_______________________________________________________________________________
CERTIFICATIONS & LICENSURE

• Certification – Issuer
```

## Workflow (Swarm v3.0 — Parallel Agent Execution)

```
PHASE 1: PARALLEL RESEARCH ──────── 3 parallel Read/Glob/Write calls
PHASE 2: BASE SCORING + WRITING ── 2 background scorers + main writes resume
PHASE 3: SCORING + COVER LETTER ── 2 background scorers + 1 cover letter agent
PHASE 4: ITERATION ────────────── Parallel re-scoring if needed (max 2 rounds)
PHASE 5: FINALIZATION ──────────── 3 parallel agents (DOCX + DOCX + Tracker)
PHASE 6: CLEANUP + REPORT ─────── Delete .md files, display dual-score report
```

**Key Speed Optimizations:**
- Base scoring runs in background while resume is being written (non-blocking)
- Cover letter generates in parallel with tailored resume scoring
- DOCX creation + tracker update all happen simultaneously
- ~50% faster than sequential execution

**Detailed Steps:**
1. **Parallel Research**: Find best matching resume, read master resume, setup output folder (all simultaneously)
2. **Background Base Scoring + Writing**: Launch ATS/HR scorers in background, start resume generation immediately
3. **Parallel Scoring + Cover Letter**: Score tailored resume + generate cover letter simultaneously
4. **Iteration**: If scores < threshold, revise and re-score in parallel (max 2 rounds)
5. **Parallel Finalization**: Create resume DOCX + cover letter DOCX + update tracker simultaneously
6. **Report**: Show dual scores, HR insights, improvement metrics, swarm agent count

## HR Optimization Tips

To boost HR Score:
- Use strong action verbs at bullet start (Led, Directed, Spearheaded, Achieved)
- Include quantified metrics in **40%+** of bullets (%, $, numbers)
- Show career progression with clear title escalation
- Highlight prestigious companies/universities
- Avoid job-hopping appearance (emphasize longer tenures)

## DOCX Generator Usage

```python
from docx_generator import create_ats_resume, create_ats_cover_letter

# Create resume
create_ats_resume(
    output_path='output.docx',
    name='NAME, CREDENTIALS',
    contact_info={'city': '', 'state': '', 'phone': '', 'email': '', 'linkedin': ''},
    summary='Professional summary...',
    core_competencies=['Skill 1', 'Skill 2'],
    experience=[{
        'title': 'Job Title',
        'company': 'Company',
        'location': 'City, State',
        'dates': 'Month Year – Present',
        'bullets': ['Achievement 1', 'Achievement 2']
    }],
    education=[{'degree': 'Degree', 'school': 'University', 'location': 'City', 'dates': 'Years'}],
    certifications=['Cert 1'],
    professional_memberships=['Org 1']
)
```

## AUTHENTICITY RULES (CRITICAL)

### What You CAN Modify:
1. **Professional Summary** - Naturally incorporate 3-5 key JD terms
2. **Core Competencies** - Match to JD keywords (PRIMARY place for keywords)
3. **Bullet points in Professional Experience** - Reframe achievements using JD language where it fits naturally

### What You CANNOT Modify:
1. **Job Titles** - Must remain EXACTLY as in master resume (ethical requirement)
2. **Company Names** - Never change
3. **Dates** - Never change
4. **Education** - Degree names and school names stay exactly as-is
5. **Publications** - NEVER add keywords to publication titles or descriptions
6. **Certifications** - Keep exactly as-is
7. **Professional Memberships** - Keep exactly as-is

### Keyword Frequency Rules:
- Each keyword should appear **1-2 times MAX** across the entire resume
- Core Competencies is the main place for keyword matching
- Do NOT repeat the same keyword in every bullet point
- Do NOT force awkward phrases just to match JD terminology

### Authenticity Guidelines:
- Resume must read naturally to a human HR reviewer
- Bullets should describe REAL achievements, not be keyword checklists
- If a JD term doesn't fit your experience, DON'T force it
- Prioritize strong action verbs and metrics over keyword stuffing
- **A 75% ATS score with authentic content beats 90% with obvious stuffing**

## Notes for Claude
- Master resume: Read from `config.json` → `master_resume_path` (or glob for `*MASTER*RESUME*.md`, `*MASTER*RESUME*.docx`, `*MASTER*RESUME*.pdf`). For `.docx` files, use the `extract_text` MCP tool (Claude cannot read binary DOCX directly).
- Output folder format: `applications/{Company} - {JobTitle}/`
- ATS target: 75-85% before creating DOCX (authenticity over high score)
- HR target: 70%+ before creating DOCX
- Delete .md files after DOCX creation
- Use `docx_generator.py` for ATS-compliant formatting
- Do NOT use ** in .md files — DOCX generator handles bold formatting automatically
- Use "TITLE | COMPANY | Location" format for Workday parsing
- **NEVER CHANGE JOB TITLES** - Job titles and company names must remain EXACTLY as they appear in the master resume. This is an ethical requirement. Only reframe bullet points with relevant keywords.
- **NEVER CHANGE PUBLICATIONS** - Publication titles and citations stay exactly as-is
- Never invent experience - only reframe existing content
- **Auto-update tracker** after DOCX creation using `tracker_utils.add_application()`

## Job Application Tracker

The `Job_Application_Tracker.xlsx` is automatically updated whenever a resume is created using `/resume` or `/tailor-resume` commands.

### Tracker Columns
| Column | Description |
|--------|-------------|
| Company | Company name |
| Job Title | Position title |
| Application Date | Date applied (auto-filled) |
| Status | Applied, Interview Scheduled, Rejected, Offer, etc. |
| Resume File | Generated resume filename |
| Cover Letter File | Generated cover letter filename |
| Job Description | JD filename |
| ATS Score | Final ATS score achieved |
| HR Score | Final HR score achieved |
| Notes | Additional notes |
| Interview Date | Scheduled interview date |
| Follow Up Date | When to follow up |
| Response | Company response |

### Tracker Utilities

```python
from tracker_utils import add_application, update_application_status, rebuild_tracker_from_folders

# Add new application (called automatically by /resume and /tailor-resume)
add_application(
    company="Company Name",
    job_title="Job Title",
    resume_file="resume.docx",
    cover_letter_file="cover_letter.docx",
    ats_score=83.0,
    hr_score=71.6
)

# Update status manually
update_application_status("Company Name", "Job Title", "Interview Scheduled")

# Rebuild tracker from applications folder
rebuild_tracker_from_folders()
```
