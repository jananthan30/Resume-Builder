# Tailor Resume Only (ATS + HR Optimized) — Swarm v3.0

Optimize and tailor the resume using **parallel agent execution** for maximum speed. Target: 75-85% ATS + 70%+ HR with AUTHENTIC content.

## Job Description
$ARGUMENTS

## Instructions

You are an expert ATS optimization specialist with access to **parallel agents** (Task tool). The user has provided a job description above. Execute the following phases, launching background agents wherever possible.

---

## PHASE 0: SCORER SERVER PRE-FLIGHT

Check if the scorer server is running:
```
curl -s http://localhost:8100/health
```

- **If server responds** with `{"status":"ok",...}`: Proceed immediately (scoring calls will take <2s each).
- **If server NOT running**: Start it in background:
```
Use Task tool (subagent_type: "Bash", run_in_background: true, name: "scorer-server"):
cd "." && python scorer_server.py --port 8100
```
Then retry `/health` up to 45 seconds (models take ~30s to load). Once healthy, proceed.
- **Fallback**: If server can't start after 45s, fall back to CLI pattern (`python ats_scorer.py --score ... --json`).

---

## PHASE 1: PARALLEL RESEARCH (launch all simultaneously)

Execute these **3 actions in a single parallel tool call** (no agents — use Read, Glob, Write tools simultaneously):

**Action A — Find best matching resume:**
- Use `Glob` to find all `applications/**/*Resume*.docx` files
- From folder names (`{Company} - {JobTitle}`), identify the most semantically similar role
- **If match found (PREFERRED)**: Read `.docx` via Bash: `python -c "from docx import Document; [print(p.text) for p in Document('path').paragraphs]"`
- **If no match**: Fall back to the master resume (read `config.json` for `master_resume_path`, or glob for `*MASTER*RESUME*.md`)

**Action B — Read master resume:**
- Read the master resume (path from `config.json` → `master_resume_path`) for canonical job titles, dates, company names, education, certifications, publications, memberships (NEVER change these)

**Action C — Setup output:**
- Extract company name and job title from JD
- Create output folder: `applications/{CompanyName} - {JobTitle}/`
- Save JD as `job_description.txt`

---

## PHASE 2: BACKGROUND BASE SCORING + IMMEDIATE RESUME WRITING

**Launch 2 background Bash agents AND start writing immediately — do NOT wait for base scores.**

Base scores are only needed for the final comparison report.

**Background Agent A — Combined Base Score (ATS + HR):**
```
Use Task tool (subagent_type: "Bash", run_in_background: true, name: "base-scorer"):
curl -s -X POST http://localhost:8100/score/both -H "Content-Type: application/json" -d "{\"resume_path\": \"{base_template_path}\", \"jd_path\": \"applications/{folder}/job_description.txt\"}"
```
**Fallback** (if server not running): Use 2 separate Bash agents with `python ats_scorer.py --score ... --json` and `python hr_scorer.py --score ... --json`.

**MAIN AGENT — Generate the tailored resume immediately (see RESUME WRITING RULES below).**

Save as `resume.md` in the output folder.

**CRITICAL .md FORMATTING RULE:** Do NOT use `**` (markdown bold asterisks) anywhere in resume.md files. Write metrics and text as plain text (e.g., "11,300+ ICU stays" not "**11,300+ ICU stays**"). The DOCX generator handles bold formatting automatically — asterisks in .md files cause display issues.

---

## PHASE 3: PARALLEL TAILORED SCORING (launch both simultaneously)

Once `resume.md` is saved, launch **2 agents in a single parallel tool call**:

**Background Agent C — Combined Tailored Score (ATS + HR):**
```
Use Task tool (subagent_type: "Bash", run_in_background: true, name: "tailored-scorer"):
curl -s -X POST http://localhost:8100/score/both -H "Content-Type: application/json" -d "{\"resume_path\": \"applications/{folder}/resume.md\", \"jd_path\": \"applications/{folder}/job_description.txt\"}"
```
**Fallback** (if server not running): Use 2 separate Bash agents with CLI scorers.

---

## PHASE 4: SCORE CHECK + ITERATION (max 2 rounds)

1. **Collect scores** from agents C and D
2. **Evaluate:**

```
IF ATS < 75%:
    → Add keywords to Core Competencies (primary method)
    → Naturally reframe 1-2 bullet points with JD language
    → Re-score via curl: `curl -s -X POST http://localhost:8100/score/both -H "Content-Type: application/json" -d '{"resume_path": "...", "jd_path": "..."}'`

IF ATS ≥ 75% AND HR < 70%:
    → Improve bullet impact (metrics, action verbs)
    → Remove awkward keyword insertions
    → Re-score via curl to /score/both (1 background Bash agent)

IF ATS ≥ 75% AND HR ≥ 70%:
    → PASS — proceed to finalization
```

3. **Max 2 iteration rounds.** Each round = 2 parallel scoring agents.

---

## PHASE 5: PARALLEL FINALIZATION (launch both simultaneously)

Once scores pass, launch **2 agents in a single parallel tool call**:

**Background Agent E — Resume DOCX (from markdown):**
```
Use Task tool (subagent_type: "Bash", run_in_background: true, name: "resume-docx-creator"):
cd "." && python -c "from docx_generator import create_resume_from_md; create_resume_from_md('applications/{folder}/resume.md', 'applications/{folder}/{Name}_Resume_{Company}.docx'); print('Resume DOCX created successfully')"
```

**Background Agent F — Update Tracker:**
```
Use Task tool (subagent_type: "Bash", run_in_background: true, name: "tracker-updater"):
cd "." && python -c "
from tracker_utils import add_application
add_application(
    company='{Company}',
    job_title='{Job Title}',
    resume_file='{Name}_Resume_{Company}.docx',
    cover_letter_file='',
    jd_file='job_description.txt',
    ats_score={final_ats},
    hr_score={final_hr},
    application_date=None,
    status='Applied'
)
print('Tracker updated successfully')
"
```

---

## PHASE 6: CLEANUP + REPORT

1. **Collect all results** (verify DOCX + tracker)
2. **Collect base scores** from Phase 2 agent (for comparison)
3. **Delete `resume.md`** (AFTER DOCX agent confirms success — .md file is needed as input for DOCX creation)
4. **Display final report:**

```
================================================================================
                    RESUME TAILOR - FINAL REPORT (v3.0 Swarm)
================================================================================

COMPANY: {Company Name}
POSITION: {Job Title}
DOMAIN DETECTED: {clinical_research/pharma_biotech/technology/etc.}
BASE TEMPLATE: {source application folder or "Master Resume"}

--------------------------------------------------------------------------------
                         SCORING SUMMARY
--------------------------------------------------------------------------------

                    |  BASE RESUME  |  TAILORED RESUME  |  IMPROVEMENT
--------------------------------------------------------------------------------
ATS SCORE           |    {X}%       |      {Y}%         |    +{Z}%
HR SCORE            |    {X}%       |      {Y}%         |    +{Z}%
--------------------------------------------------------------------------------

ATS RATING: {Excellent/Good/Fair}
HR RECOMMENDATION: {STRONG INTERVIEW/INTERVIEW/MAYBE/PASS}

--------------------------------------------------------------------------------
                         AUTHENTICITY CHECK
--------------------------------------------------------------------------------

  [✓] Job titles preserved exactly from master resume
  [✓] Publications unchanged
  [✓] No keyword stuffing (each keyword 1-2x max)
  [✓] Bullets read naturally to human reviewer

GENERATED: {Name}_Resume_{Company}.docx
FOLDER: applications/{Company} - {JobTitle}/

================================================================================
SWARM AGENTS USED: {count} | ITERATIONS: {count}
================================================================================
```

5. **Offer** web reports:
```bash
python ats_scorer.py --web --base "{base_template}" --tailored "applications/{folder}/resume.md" --jd "applications/{folder}/job_description.txt"
python hr_scorer.py --score "applications/{folder}/{Name}_Resume_{Company}.docx" "applications/{folder}/job_description.txt" --web
```

---

## RESUME WRITING RULES (Applied during Phase 2)

### AUTHENTICITY RULES (CRITICAL)

**What You CAN Modify:**
1. **Professional Summary** - Naturally incorporate 3-5 key JD terms
2. **Core Competencies** - Match to JD keywords (PRIMARY place for keywords)
3. **Bullet points** - Reframe achievements using JD language where natural

**What You CANNOT Modify:**
1. **Job Titles** - EXACTLY as in master resume
2. **Company Names** - Never change
3. **Dates** - Never change
4. **Education** - Exactly as-is
5. **Publications** - NEVER add keywords
6. **Certifications** - Exactly as-is
7. **Professional Memberships** - Exactly as-is

**Keyword Rules:**
- Each keyword: **1-2 times MAX** across entire resume
- Core Competencies = primary keyword location
- 75% ATS with authentic content > 90% with stuffing

### WRITING COACH (Rules 1-10)

**Rule 1 (So What?):** Every bullet shows impact, not just activity
**Rule 2 (6-Second):** Front-load value in first 3 words
**Rule 3 (Deadwood):** Strip "Responsible for", "Successfully", "Various", "Helped"
**Rule 4 (Metrics):** 50%+ bullets contain quantified metrics (plain text, no ** bold)
**Rule 5 (Verbs L3+):** 70%+ verbs at Directive/Strategic/Transformative level
**Rule 6 (Architecture):** Impact Lead, Challenge-Action-Result, or Scope-Authority
**Rule 7 (Rhythm):** Vary bullet lengths
**Rule 8 (Parallel):** Consistent grammar patterns per role
**Rule 9 (Summary Hook):** Identity + authority → differentiator
**Rule 10 (Authenticity):** Interview Test on every bullet

### RESUME STRUCTURE (ATS/Workday)

```
[FULL NAME, CREDENTIALS]
[City, State ZIP] | [Phone] | [Email]
[LinkedIn URL]

_______________________________________________________________________________
PROFESSIONAL SUMMARY
[3-4 lines with JD terms naturally woven in]

_______________________________________________________________________________
CORE COMPETENCIES
[12-14 JD-relevant keywords]

_______________________________________________________________________________
PROFESSIONAL EXPERIENCE
[EXACT TITLE] | [EXACT COMPANY] | [Location]
[Dates]
• [L3+ Verb] [STAR], achieving [quantified metric]

_______________________________________________________________________________
EDUCATION
[EXACT from master]

_______________________________________________________________________________
CERTIFICATIONS & LICENSURE
[EXACT from master]

_______________________________________________________________________________
PUBLICATIONS
[EXACT from master — NO additions]

_______________________________________________________________________________
PROFESSIONAL MEMBERSHIPS
[EXACT from master]
```

**ATS FORMAT:** No columns/tables/graphics. No ** in .md files (DOCX handles bold). ALL-CAPS headers. "TITLE | COMPANY | Location" format.

### STAR BULLETS + VERB BANK

**Formula:** `[Executive Verb] [context + action] → [quantified result]`

**Verbs:** Directed, Spearheaded, Championed, Orchestrated, Architected, Pioneered, Streamlined, Validated, Established, Governed

**Tone:** Senior professional — authoritative and evidence-based.

**Bullet Distribution:** Current role 4-6, recent 3-4, older 2-3, very old 1-2.

---

## ETHICAL REQUIREMENTS (NON-NEGOTIABLE)

- **NEVER CHANGE JOB TITLES** — Match master resume exactly
- **NEVER CHANGE PUBLICATIONS** — Titles/citations stay as-is
- **Never invent experience** — Only reframe existing content
