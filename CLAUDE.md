# Resume Builder - Project Context

## Overview
AI-powered resume and cover letter generator that tailors applications to specific job descriptions with **native role separation** and dual-scoring optimization:
- **Authenticity**: Never invent facts; resumes must be interview-true
- **Human Voice**: Brevity, burstiness, plain language — hard gate via `human_voice_audit.py`
- **ATS Score**: Keyword matching for Applicant Tracking Systems (75-85% target)
- **HR Score**: Human recruiter evaluation simulation (70%+ target)
- **Candidate Fit**: The configured master must score at least 50 (the default
  bar; `candidate_fit_preflight.CANDIDATE_FIT_THRESHOLD`) with zero hard
  knockouts against the exact JD before any resume development
- **Safety**: Researcher, Writer, Auditor, and Editor have distinct least-authority contracts; finalization is ordered

**Editorial priority (never invert):** Authenticity → Human voice → HR impact → ATS match. 75% ATS with human prose beats 90% stuffed AI prose.

## Quick Start

### Option 1: Claude Code Custom Commands (Recommended)
```bash
# Full application package (resume + cover letter) - Dual optimized
/resume [paste job description]

# Resume only - Dual optimized
/tailor-resume [paste job description]

# Cover letter only
/cover-letter [paste job description]

# Explicit native four-role workflow (authorized resume.md draft only)
/resume-team [paste job description]

# Deterministic candidate-fit gate only
/job-fit [paste job description]
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
├── evidence_audit.py                       # Core Competencies must-trace checker
├── human_voice_audit.py                    # Human-voice / anti-AI-prose hard gate
├── data/ai_tells.json                      # Shared banned AI lexicon
├── references/human_voice_examples.md      # Good vs bad human-voice examples
├── docx_generator.py                       # ATS-compliant DOCX generator
├── tracker_utils.py                        # Job application tracker utilities
├── Job_Application_Tracker.xlsx            # Auto-updated application tracker
├── orchestration_state.py                  # Shared state.json manager for multi-agent workflow
├── llm_scorer.py                           # LLM-augmented scoring (Claude-based rubric scorer)
├── scorer_server.py                        # FastAPI server for ATS/HR/LLM scoring
├── resume_builder.py                       # Retired direct-rewrite CLI; native-team migration guard
├── multi_agent_team.py                    # Vendor-neutral fail-closed team controller
├── candidate_fit_preflight.py             # Master-only exact-JD first gate
├── native_resume_team.py                  # Hardened host adapter and Markdown draft publisher
├── schemas/resume-team-handoff.schema.json # Public strict handoff schema
├── schemas/resume-team-authorization.schema.json # Three-vote report schema
├── schemas/resume-team-final-receipt.schema.json # Durable draft receipt schema
├── schemas/resume-team-result.schema.json  # Draft-stage runtime result schema
├── config.json                             # User configuration
├── requirements.txt                        # Python dependencies
├── embed_cache/                            # SBERT embedding cache (disk-based .npy files)
├── applications/                           # Output directory
│   └── {Company} - {JobTitle}/            # Company + role folders
│       ├── *_Resume_*.docx                # Final resume (DOCX only)
│       ├── *_Cover_Letter_*.docx          # Final cover letter (DOCX only)
│       ├── job_description.txt            # Original JD for reference
│       └── state.json                     # Orchestration state (transient, deleted after run)
├── .codex/agents/                         # Manual Codex roles (unpinned; host inheritance applies)
└── .claude/
    ├── agents/                            # Manual Claude roles (unpinned; host inheritance applies)
    └── commands/
        ├── resume-team.md                 # Shared four-role coordinator protocol
        ├── resume.md                       # Full application command (native team)
        ├── tailor-resume.md               # Resume only command (native team)
        ├── cover-letter.md                # Cover letter command
        └── writing-coach.md               # Human Voice + Impact skill (Rules 0-16)
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

## Workflow (Native Four-Role Resume Team)

```
PHASE 0: CANDIDATE FIT ────────── Master-only exact-JD gate (>=70, no knockouts)
PHASE 1: RESEARCHER ────────────── JD-only rubric and evidence spans
PHASE 2: WRITER ───────────────── Master-resume-bound complete draft
PHASE 3: AUDITOR ──────────────── Independent PASS/FAIL; no editing authority
PHASE 4: EDITOR LOOP ──────────── Named findings only; max 2; fresh audit each time
PHASE 5: THREE VOTES ──────────── Evidence + human voice + canonical integrity
PHASE 6: DRAFT PUBLICATION ────── Atomic resume.md receipt + digest readback
PHASE 7: ORDERED FINALIZATION ─── Resume DOCX → cover DOCX → tracker → cleanup
```

**Safe concurrency:**
- No role, output, resume development, DOCX, or tracker work starts until the
  deterministic candidate-fit gate passes
- ATS/HR base scoring may overlap only after candidate fit passes; it is advisory
- Cover letter generates in parallel with tailored resume scoring
- Independent read/scoring work may overlap; authorization and publication remain ordered

**Detailed Steps:**
The hardened runtime requires macOS or Linux; Windows preflight fails closed with
`POSIX_RUNTIME_REQUIRED`. It ignores user configuration and transient parent-session
settings, so its default model/reasoning selection is unknown. Do not claim a
specific inherited profile, model, or Ultra setting. Codex may receive
`--model <exact-model>` and/or `--reasoning-effort ultra` only when the user
explicitly requests those pins; Claude must not receive Codex-only flags.

0. **Candidate fit** runs `candidate_fit_preflight.py` against only the configured
   master resume and exact JD before any output or role invocation. It requires a
   canonical, digest-bound `candidate-fit-policy-v2` report with score at least
   the default bar (50; `CANDIDATE_FIT_THRESHOLD`), trustworthy extraction,
   zero hard knockouts, `passed: true`, and no codes.
   A lower score or hard knockout is
   `REJECTED:CANDIDATE_FIT`; unavailable, malformed, stale, or mismatched analysis
   is `FAILED:CANDIDATE_FIT_PREFLIGHT`. Neither has a workflow bypass.
   Policy v2 calibration: tool knockouts ground only in requirements sections
   (duties prose never disqualifies), doctorates named in requirement ladders
   are alternative routes, and knockouts degrade the score with an informative
   cap (1 → 55, 2 → 45, 3+ → 35). For stretch-zone results (clean floor,
   score below 70) the coordinator may run `candidate_fit_review.py`: two
   distinct native reviewers with code-verified citations must both return
   PROCEED (`candidate-fit-review/v1`). Review certification authorizes
   coordinator-built manual packages only; the hardened runtime still requires
   the deterministic pass.
   After any rejection the user may record a manual override
   (`candidate_fit_override.py`, `candidate-fit-override/v1`, decision
   `PROCEED_MANUAL`): an explicit, digest-bound record of the exact reports
   overruled, authorizing only the manual package path — never the runtime,
   receipts, authorized wrappers, or tracker.
1. **Researcher** receives only the JD and returns a requirement rubric whose hard-then-soft strings exactly equal the uniquely anchored evidence strings one-for-one and in order.
2. **Writer** receives only the master resume and validated rubric; it cannot authorize or publish.
3. **Auditor** independently checks the exact draft; it cannot edit.
4. **Editor** corrects only explicit findings, with a maximum of two fresh re-audits.
5. **Authorization** requires Auditor PASS plus all three deterministic votes on the same digest.
6. **Draft publication** atomically writes and reads back only the authorized `resume.md`.
7. **Finalization** is strictly ordered and stops on the first failure.

Runtime `resume-team-result/v2` `PUBLISHED` is a draft-stage result, not a completed package. `/resume` and
`/tailor-resume` must still finish their DOCX, tracker, artifact-verification,
cleanup, and final-report gates before reporting package completion.

Before finalization, resolve the result's `authorization_receipt_path` against the
output directory when relative, require its resolved parent to equal that directory,
and read only regular, non-symlink `resume-team-final-receipt/v2` JSON. Match
`authorization_receipt_digest`, the inline receipt, run/case IDs, and the exact
passing `candidate_fit_report`/`candidate_fit_report_digest` to the result and
independently validated preflight. Match draft/verified-target digests to the
actual `resume.md`. Recompute `source_digest` from the current configured master
and `job_description_digest` from the fixed sibling `job_description.txt`; require
a SHA-256 Researcher artifact and distinct same-host native Researcher/Auditor
identities. Native hosts are `api` (the hosted product runtime — roles run
server-side via the Anthropic API) and the legacy local hosts `codex`/`claude`
(local CLI/plugin flows); the host prefix is a provenance record of which
runtime produced a role's output, so Researcher and Auditor must remain
distinct identities on the same host regardless of which of the three hosts
that is. Require a same-draft PASS `auditor_attestation` and a complete
passing `authorization_report` with no codes and exactly three ordered named
same-draft PASS votes with distinct IDs. Require `canonical_digest(report) ==
authorization_digest` and the receipt vote-ID list to equal those IDs in order.
Validate its shape against `schemas/resume-team-final-receipt.schema.json`.
The acceptance gate must execute `final_receipt_verifier.py` with the exact
sidecar path, `authorization_receipt_digest`, and `config.json` captured/used by
the runtime.
Repeat that executable check immediately before every side-effect boundary and
preserve this durable receipt during cleanup.

## HR Optimization Tips

To boost HR Score:
- Use plain strong verbs at bullet start (Led, Built, Wrote, Cut, Reviewed, Directed)
- Include quantified metrics in **40%+** of bullets (%, $, numbers)
- Show career progression with clear title escalation
- Highlight prestigious companies/universities
- Avoid job-hopping appearance (emphasize longer tenures)

## DOCX Generator Usage

Native Resume Team outputs may use only the receipt-validating wrappers. The
lower-level formatting functions are internal/legacy building blocks and are not
an authorized publication path.

```python
from docx_generator import (
    create_cover_letter_from_md_authorized,
    create_resume_from_md_authorized,
)

create_resume_from_md_authorized(
    "applications/Company - Role/resume.md",
    "applications/Company - Role/Name_Resume_Company.docx",
    receipt_path="applications/Company - Role/resume-team-receipt.<digest>.json",
    expected_receipt_digest="<authorization_receipt_digest from runtime result>",
    config_path="config.json",
)

create_cover_letter_from_md_authorized(
    "applications/Company - Role/cover_letter.md",
    "applications/Company - Role/Name_Cover_Letter_Company.docx",
    authorized_resume_path="applications/Company - Role/resume.md",
    receipt_path="applications/Company - Role/resume-team-receipt.<digest>.json",
    expected_receipt_digest="<authorization_receipt_digest from runtime result>",
    job_title="Role",
    company="Company",
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

### Core Competencies Must-Trace Rule (enforced by `evidence_audit.py`):
**Every Core Competency item must be backed by evidence elsewhere in the resume.**

For each item, ONE of the following must be true:
1. The item (or one of its key phrases) appears in a Professional Experience bullet
2. The item appears in the Summary
3. The item is backed by Publications / Education / Certifications / Memberships / Projects (e.g., "Peer-Reviewed Publications" backed by the Publications section)
4. The item carries an honest exposure qualifier: `(exposure)`, `(coursework)`, `(trainable)`, `(familiar)`, `(rapid trainability)`, `(in progress)`, `(learning)`

If none of the above, REMOVE the item from Core Competencies. Listing a skill without evidence creates an interview liability — recruiters ask "tell me about a time you did X" and the candidate has no answer.

**Validated tools whitelist:** When listing software / platforms (Veeva Vault, Argus, Medidata Rave, EDC, MedDRA, etc.), only include them if the candidate has hands-on experience documented in a bullet. If they only have indirect or "trainable" exposure, mark with `(trainable)` — e.g., `Veeva Vault (trainable)`.

**Audit commands (run before DOCX creation):**
```bash
python evidence_audit.py applications/{Company - JobTitle}/resume.md
python human_voice_audit.py applications/{Company - JobTitle}/resume.md
python human_voice_audit.py applications/{Company - JobTitle}/cover_letter.md --mode cover_letter
```
Exit code 0 = passed. Exit code 1 = fix failures before generating DOCX.

### Human Voice Rules (enforced by `human_voice_audit.py` + writing-coach)
- Priority: Authenticity → Human voice → HR → ATS
- Plain strong verbs (Led, Built, Wrote, Cut); ban AI openers (spearheaded, leveraged, orchestrated, …)
- Bullet mean ≤ 22 words; hard max 28; burstiness CV ≥ 0.30
- Summary ≤ 3 sentences / 70 words; never "Results-driven…"
- Keywords in Core Competencies first — never cosplay JD jargon into every bullet
- Lexicon: `data/ai_tells.json` | Examples: `references/human_voice_examples.md`

## Notes for Claude
- Master resume: Read from `config.json` → `master_resume_path` (or glob for `*MASTER*RESUME*.md`, `*MASTER*RESUME*.docx`, `*MASTER*RESUME*.pdf`). For `.docx` files, use the `extract_text` MCP tool (Claude cannot read binary DOCX directly).
- Output folder format: `applications/{Company} - {JobTitle}/`
- ATS target: 75-85% before creating DOCX (authenticity + human voice over raw score)
- HR target: 70%+ before creating DOCX
- Human voice audit must pass (exit 0) before DOCX — same severity as evidence audit
- Delete transient .md files only after authorized DOCX creation, a literal-`True` tracker update, and artifact verification; never delete the durable receipt
- Use `docx_generator.py` for ATS-compliant formatting
- Do NOT use ** in .md files — DOCX generator handles bold formatting automatically
- Use "TITLE | COMPANY | Location" format for Workday parsing
- **NEVER CHANGE JOB TITLES** - Job titles and company names must remain EXACTLY as they appear in the master resume. This is an ethical requirement. Only reframe bullet points with relevant keywords.
- **NEVER CHANGE PUBLICATIONS** - Publication titles and citations stay exactly as-is
- Never invent experience - only reframe existing content
- **Auto-update tracker** only after both verified DOCX files using `tracker_utils.add_application_authorized()` with the captured sidecar path/digest; require a literal `True` return

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
| Response | Date company responded (filled by `mark_response`) |
| **Target Tier** | IC / Sr / Manager / AD / Director — used to spot pipeline-mix issues (too many AD/Director applications) |
| **Fit Label** | MEETS / STRETCH / MISS — taken from `job_fit_scorer` recommendation |
| **Hard Reqs Missed** | Count of knockout flags at apply time |
| **Referral Source** | cold / alumni / recruiter / network / referral |
| **Rejection Reason** | no_response / auto_reject / screen_reject / interview_reject / offer_declined / withdrawn |
| **Days To Response** | Auto-computed (Response date − Application Date) |
| **Interview Stages Reached** | 0=applied, 1=phone screen, 2=hiring mgr, 3=panel, 4=onsite, 5=offer |

Strategic columns let the system *learn* which categories of role actually convert. Without these, the tracker is bookkeeping; with these, you can answer "what's my response rate on AD-tier vs Sr Specialist?"

### Tracker Utilities

```python
from tracker_utils import (
    TrackerUpdateError, add_application_authorized, mark_response, pipeline_summary,
    update_application_status, rebuild_tracker_from_folders,
)

# Add a new application — strategic columns are optional but recommended
updated = add_application_authorized(
    company="Company Name",
    job_title="Job Title",
    authorized_resume_path="applications/Company - Role/resume.md",
    receipt_path="applications/Company - Role/resume-team-receipt.<digest>.json",
    expected_receipt_digest="<authorization_receipt_digest from runtime result>",
    resume_file="resume.docx",
    cover_letter_file="cover_letter.docx",
    ats_score=83.0,
    hr_score=71.6,
    target_tier="Sr Specialist",     # IC / Sr / Manager / AD / Director
    fit_label="MEETS",                # from job_fit_scorer
    hard_reqs_missed=0,
    referral_source="alumni",         # cold / alumni / recruiter / network / referral
)
if updated is not True:
    raise TrackerUpdateError("TRACKER_UPDATE_NOT_CONFIRMED")

# Record an outcome — auto-computes Days To Response
mark_response(
    company="Company Name",
    job_title="Job Title",
    response_date="2026-06-01",
    rejection_reason="screen_reject",
    interview_stages_reached=1,
    status="Rejected after phone screen",
)

# See pipeline conversion by tier / fit / referral source
pipeline_summary()

# Manual status update (legacy)
update_application_status("Company Name", "Job Title", "Interview Scheduled")

# Rebuild tracker from applications folder
rebuild_tracker_from_folders()
```
