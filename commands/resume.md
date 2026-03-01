# Resume Builder — Precision Edition v5.1 (Triple-Scorer Swarm)

Generate a tailored resume AND cover letter using parallel agent execution with scoring-aware optimization. Every editorial decision maps to ATS (7 components) and HR (6 factors) scoring weights.

## Job Description
$ARGUMENTS

## Instructions

You are an expert resume editor AND a parallel-agent orchestrator. The user has provided a job description above. You will:
1. Internalize both scoring engines before writing a single word
2. Deconstruct the JD into a scoring blueprint
3. Draft with every section mapped to specific scoring components
4. Diagnose gaps by component weight, not guesswork
5. Execute via parallel agents for speed

---

## GLOBAL CONSTRAINTS (read first, enforce always)

- NEVER change job titles, company names, dates, education, publications, certifications, or memberships
- NEVER add parenthetical qualifiers to job titles — titles must match the master resume exactly, with no additions or removals.
- NEVER use `**bold**` markdown in `.md` files — the DOCX generator handles bold automatically
- NEVER exceed 2 appearances of any single keyword across the entire resume
- Publications & Education: Keep EXACTLY as in master resume — zero modifications
- Cover letter DOCX: ALWAYS use `create_ats_cover_letter()` directly — NEVER use `create_cover_letter_from_md()` (known KeyError bug)
- Score targets: ATS 75-85%, HR 70%+. If JD contains staffing/benefits boilerplate, ATS ceiling is ~69-73% — accept once all domain weights are maxed. Max 2 iterations against a boilerplate ceiling.

---

## PHASE 0: SCORER SERVER PRE-FLIGHT

Check if the scorer server is running:
```
curl -s http://localhost:8100/health
```

- If server responds with `{"status":"ok",...}`: Proceed immediately (scoring calls will take <2s each).
- If server NOT running: Start it in background:
```
Use Task tool (subagent_type: "Bash", run_in_background: true, name: "scorer-server"):
cd "." && python scorer_server.py --port 8100
```
Then retry `/health` up to 15 seconds (models now lazy-load on first request, server starts in ~5s). Once healthy, proceed.
- Fallback: If server can't start after 20s, fall back to CLI pattern (`python ats_scorer.py --score ... --json`).

NOTE: v2.1 Performance Improvements Applied:
- SBERT model lazy-loads on first scoring call (not at import), reducing server startup from 45-90s to ~5s
- Embedding cache (disk + memory) avoids re-encoding the same resume/JD
- BM25Plus (rank_bm25) replaces hardcoded fake BM25 for real lexical scoring
- Domain detection uses SBERT prototype embeddings for better domain classification
- NLTK WordNet lemmatizer replaces spaCy (not installed) for keyword normalization
- LLM-augmented scoring available via /score/llm and /score/combined endpoints (optional)

---

## PHASE 1: PARALLEL RESEARCH + JD DECONSTRUCTION (launch all simultaneously)

Execute these 3 actions in a single parallel tool call (no agents needed — use Read, Glob, and Write tools simultaneously):

Action A — Find best matching resume:
- Use `Glob` to find all `applications/**/*Resume*.docx` files
- From the folder names (format: `{Company} - {JobTitle}`), identify the most semantically similar role to the new JD (same domain, similar responsibilities, overlapping keywords)
- If a match is found (PREFERRED): Read the `.docx` using Python via Bash: `python -c "from docx import Document; [print(p.text) for p in Document('path').paragraphs]"`
- If no match found: Fall back to the master resume (read `config.json` for `master_resume_path`, or glob for `*MASTER*RESUME*.md`)

Action B — Read master resume:
- Read the master resume (path from `config.json` → `master_resume_path`) for canonical job titles, dates, company names, education, certifications, publications, and memberships (these NEVER change)

Action C — Setup output + initialize orchestration state:
- Extract company name and job title from JD
- Create output folder: `applications/{CompanyName} - {JobTitle}/`
- Save JD as `job_description.txt` in the output folder
- Initialize shared state via Bash:
```
cd "." && python -c "
from orchestration_state import init_state
init_state('applications/{folder}', '{Company}', '{JobTitle}', 'applications/{folder}/job_description.txt', '{base_template}')
print('State initialized')
"
```

THEN — Before writing anything, complete the JD Deconstruction (see STEP 1 in Scoring-Aware Writing Rules below). This takes 30 seconds and prevents generic, under-optimized drafts.

---

## PHASE 2: BACKGROUND BASE SCORING + IMMEDIATE RESUME WRITING

Launch background Bash agents AND start writing immediately — do NOT wait for base scores.

Base scores are only needed for the final comparison report, NOT for writing the resume.

Background Agent A — Combined Base Score (ATS + HR) → writes to state.json:
```
Use Task tool (subagent_type: "Bash", run_in_background: true, name: "base-scorer"):
cd "." && python -c "
from orchestration_state import write_score_results, set_phase, log_error
import subprocess, json
set_phase('applications/{folder}', 'scoring_base')
try:
    result = subprocess.run(
        ['curl', '-s', '-X', 'POST', 'http://localhost:8100/score/both',
         '-H', 'Content-Type: application/json',
         '-d', json.dumps({'resume_path': '{base_template_path}',
                           'jd_path': 'applications/{folder}/job_description.txt'})],
        capture_output=True, text=True, timeout=120)
    write_score_results('applications/{folder}', 'base_both', result.stdout)
    print('Base scores written to state.json')
except Exception as e:
    log_error('applications/{folder}', 'scoring_base', str(e))
    print(f'Error: {e}')
"
```
Fallback (if server not running): Use 2 separate Bash agents with `python ats_scorer.py --score ... --json` and `python hr_scorer.py --score ... --json`, piping output through `write_score_results('applications/{folder}', 'base_ats'|'base_hr', result)`.

MAIN AGENT — Generate the tailored resume using the Scoring-Aware Writing Rules (Steps 0-2 below).

Save as `resume.md` in the output folder when done, then update state:
```
cd "." && python -c "
from orchestration_state import update_state, set_phase
update_state('applications/{folder}', 'tailored_resume_path', 'applications/{folder}/resume.md')
set_phase('applications/{folder}', 'writing')
print('State updated: resume path + phase=writing')
"
```

CRITICAL .md FORMATTING RULE: Do NOT use `**` (markdown bold asterisks) anywhere in resume.md or cover_letter.md files. Write metrics and text as plain text (e.g., "11,300+ ICU stays" not "**11,300+ ICU stays**"). The DOCX generator handles bold formatting automatically.

---

## PHASE 3: PARALLEL SCORING + COVER LETTER (launch all simultaneously)

Once `resume.md` is saved, launch 3 agents in a single parallel tool call:

Background Agent C — Combined Tailored Score (ATS + HR + LLM) → writes to state.json:
```
Use Task tool (subagent_type: "Bash", run_in_background: true, name: "tailored-scorer"):
cd "." && python -c "
from orchestration_state import write_score_results, set_phase, log_error
import subprocess, json
set_phase('applications/{folder}', 'scoring_tailored')
try:
    result = subprocess.run(
        ['curl', '-s', '-X', 'POST', 'http://localhost:8100/score/combined',
         '-H', 'Content-Type: application/json',
         '-d', json.dumps({'resume_path': 'applications/{folder}/resume.md',
                           'jd_path': 'applications/{folder}/job_description.txt'})],
        capture_output=True, text=True, timeout=180)
    write_score_results('applications/{folder}', 'tailored_combined', result.stdout)
    print('Tailored scores (ATS + HR + LLM) written to state.json')
except Exception as e:
    log_error('applications/{folder}', 'scoring_tailored', str(e))
    print(f'Error: {e}')
"
```
NOTE: `/score/combined` runs all 3 scorers (ATS rules + HR rules + LLM Claude) and returns blended scores (70% rules + 30% LLM). If LLM fails (no API key, timeout), it gracefully falls back to rules-only.
Fallback (if server not running): Use CLI scorers + llm_scorer.py directly.

Background Agent E — Cover Letter:
```
Use Task tool (subagent_type: "general-purpose", run_in_background: true, name: "cover-letter-writer"):
Prompt: "Generate a one-page cover letter (350-400 words) for {Name} applying to {Job Title} at {Company}.

JD: [paste full JD text]

Resume bullets to reference: [paste the key achievements from the tailored resume]

Structure (4 paragraphs):
P1 — Hook (50-60 words): Lead with a specific metric achievement + direct connection to what this role needs. Name the role and company. Lead with a number.
P2 — Proof Point 1 (80-100 words): STAR story for strongest JD-relevant experience. Use at least 1 exact JD noun phrase. Include a metric.
P3 — Proof Point 2 (80-100 words): STAR story for secondary JD requirement. Include a metric with magnitude ($M or multiplier preferred).
P4 — Close (50-60 words): Forward-looking statement tied to company mission or pipeline + call to action. Confident, not pleading. No 'I would welcome the opportunity.'

Tone: Senior professional writing to peers, not a job-seeker writing to gatekeepers. Confident, specific, evidence-based.
- Do NOT use ** markdown bold — write metrics as plain text
- At least 2 exact JD phrases used across the letter
- At least 2 quantified metrics included
- No sentence exceeds 25 words
- Total word count 350-400
- Save the cover letter text to: applications/{folder}/cover_letter.md

Contact info:
Name: {user_name from config.json}
Address: {user_city, user_state from config.json}
Phone: {user_phone from config.json}
Email: {user_email from config.json}"
```

---

## PHASE 4: PRECISION DIAGNOSIS + ITERATION (max 3 rounds)

1. Collect all three scores from state.json (single read replaces polling multiple agent outputs):
```
cd "." && python -c "
from orchestration_state import read_state
import json
state = read_state('applications/{folder}')
ts = state.get('tailored_scores', {})
# Combined (blended) scores — these are the primary decision scores
combined_ats = ts.get('combined_ats', ts.get('ats', {}).get('total', 'pending'))
combined_hr = ts.get('combined_hr', ts.get('hr', {}).get('total', 'pending'))
print(f'=== COMBINED (70% rules + 30% LLM) ===')
print(f'ATS: {combined_ats}%')
print(f'HR:  {combined_hr}%')
# Individual scorer breakdown
rules_ats = ts.get('rules_ats', {})
rules_hr = ts.get('rules_hr', {})
llm = ts.get('llm', {})
print(f'--- Rules-based ---')
print(f'ATS (rules): {rules_ats.get(\"total_score\", \"?\")}%')
print(f'HR  (rules): {rules_hr.get(\"overall_score\", \"?\")}%')
print(f'--- LLM (Claude) ---')
print(f'ATS (LLM): {llm.get(\"ats_score\", \"?\")}%')
print(f'HR  (LLM): {llm.get(\"hr_score\", \"?\")}%')
if llm.get('explanation'):
    print(f'LLM says: {llm[\"explanation\"]}')
blend = ts.get('blend_details', {})
print(f'Blend method: {blend.get(\"method\", \"unknown\")}')
if state.get('errors'):
    print(f'Errors: {json.dumps(state[\"errors\"], indent=2)}')
"
```
Use the COMBINED scores for iteration decisions (they incorporate LLM semantic understanding).
2. Diagnose BEFORE editing — follow the decision trees in Step 3 of Scoring-Aware Writing Rules below. Fix the highest-weighted gap first.

IF ATS < 75%:
```
1. Keyword Match (22%) — Are all high-frequency JD nouns in Core Competencies?
   FIX: Add missing JD keywords to Core Competencies
2. Semantic Similarity (22%) — Is Summary using JD vocabulary or paraphrased vocabulary?
   FIX: Rewrite Summary sentences 2-3 to mirror JD phrasing exactly
3. Weighted Industry Terms (18%) — Are all domain-critical keywords present?
   FIX: Add missing domain terms to Core Competencies
4. Phrase Match (13%) — Are exact 2-4 word JD phrases appearing verbatim?
   FIX: Insert 1-2 exact JD phrases into bullets naturally
5. BM25 (13%) — Are key terms appearing at least twice but not more than twice?
   FIX: Add one natural repetition of under-represented terms
STOP if boilerplate ceiling applies (~69-73%). Max 2 iteration cycles.
```

IF ATS >= 75% AND HR < 70%:
```
1. Job Fit (25%) — Are domain-defining terms in first 100 words?
   FIX: Rewrite Summary sentence 1 to lead with domain identity
2. Skills Match (20%) — Are skills IN ACTION (verb + skill + metric) or just listed?
   FIX: Reframe 2-3 listed skills as action bullets (2x weight multiplier)
3. Experience Fit (20%) — Does Summary explicitly state years of experience?
   FIX: Ensure Summary mentions years matching JD minimum +/- 3 yrs
4. Impact Signals (15%) — Do 50%+ of bullets contain metrics?
   FIX: Add metrics to bare bullets. Move highest-magnitude metric to bullet 1.
5. Competitive Edge (10%) — Are prestige signals (top companies/universities) appearing early?
   FIX: Name-drop in Summary sentence 1 or 3
```

IF ATS >= 75% AND HR >= 70%:
   PASS — proceed to finalization

Re-score after each iteration using all 3 scorers and write to state.json:
```
cd "." && python -c "
from orchestration_state import write_score_results, update_state, read_state
import subprocess, json
result = subprocess.run(
    ['curl', '-s', '-X', 'POST', 'http://localhost:8100/score/combined',
     '-H', 'Content-Type: application/json',
     '-d', json.dumps({'resume_path': 'applications/{folder}/resume.md',
                        'jd_path': 'applications/{folder}/job_description.txt'})],
    capture_output=True, text=True, timeout=180)
write_score_results('applications/{folder}', 'tailored_combined', result.stdout)
state = read_state('applications/{folder}')
ts = state.get('tailored_scores', {})
iters = state.get('iterations', [])
iters.append({
    'round': len(iters)+1,
    'combined_ats': ts.get('combined_ats', '?'),
    'combined_hr': ts.get('combined_hr', '?'),
    'rules_ats': ts.get('rules_ats', {}).get('total_score', '?'),
    'rules_hr': ts.get('rules_hr', {}).get('overall_score', '?'),
    'llm_ats': ts.get('llm', {}).get('ats_score', '?'),
    'llm_hr': ts.get('llm', {}).get('hr_score', '?'),
    'changes': ['describe changes']
})
update_state('applications/{folder}', 'iterations', iters)
"
```

Iteration protocol:
| Iteration | Focus | Stop Condition |
|-----------|-------|----------------|
| 1 | Fix top 2 gaps from diagnosis | Re-score |
| 2 | Fix remaining gaps if still below target | Re-score |
| 3 | Micro-adjustments only (single word/phrase swaps) | Accept if within 3 pts of target |
| MAX | Do not exceed 3 iterations | Diminishing returns / risk of over-optimization |

Anti-patterns to avoid during iteration:
- Stuffing keywords that don't match real experience
- Inflating metrics beyond defensible truth
- Breaking readability grade above 12 with complex rewrites
- Removing metrics to make room for keywords (metrics are 15% of HR)
- Editing Publications or Education sections

---

## PHASE 5: PARALLEL FINALIZATION (launch all 3 simultaneously)

Once scores pass AND cover letter is ready, set phase to finalizing and launch 3 agents in a single parallel tool call:
```
cd "." && python -c "from orchestration_state import set_phase; set_phase('applications/{folder}', 'finalizing')"
```

Background Agent F — Resume DOCX (from markdown) → updates state.json:
```
Use Task tool (subagent_type: "Bash", run_in_background: true, name: "resume-docx-creator"):
cd "." && python -c "
from docx_generator import create_resume_from_md
from orchestration_state import update_state, log_error
try:
    create_resume_from_md('applications/{folder}/resume.md', 'applications/{folder}/{Name}_Resume_{Company}.docx')
    update_state('applications/{folder}', 'docx_resume_path', 'applications/{folder}/{Name}_Resume_{Company}.docx')
    print('Resume DOCX created successfully')
except Exception as e:
    log_error('applications/{folder}', 'finalizing', f'Resume DOCX failed: {e}')
    print(f'Error: {e}')
"
```

Background Agent G — Cover Letter DOCX (ALWAYS use create_ats_cover_letter directly) → updates state.json:
```
Use Task tool (subagent_type: "Bash", run_in_background: true, name: "cover-letter-docx-creator"):
cd "." && python -c "
from docx_generator import create_ats_cover_letter
from orchestration_state import update_state, log_error
try:
    with open('applications/{folder}/cover_letter.md', 'r') as f:
        content = f.read()
    lines = [l.strip() for l in content.split('\n') if l.strip()]
    body_paragraphs = []
    skip_patterns = [config_name.split()[0], config_city, config_phone[:7], 'Dear', 'Sincerely', 'February', 'March', 'April', 'May', 'June', 'July', 'August', 'September', 'October', 'November', 'December', 'January', 'Hiring Manager', 'Re:']
    for line in lines:
        if not any(p in line for p in skip_patterns) and len(line) > 50:
            body_paragraphs.append(line)
    create_ats_cover_letter(
        output_path='applications/{folder}/{Name}_Cover_Letter_{Company}.docx',
        name='{user_name from config.json}',
        contact_info={'city': '{city}', 'state': '{state}', 'phone': '{phone}', 'email': '{email}'},
        date='{date}',
        recipient_info={'name': 'Hiring Manager', 'company': '{Company}', 'title': ''},
        job_title='{Job Title}',
        paragraphs=body_paragraphs[:4],
        closing='Sincerely'
    )
    update_state('applications/{folder}', 'docx_cover_letter_path', 'applications/{folder}/{Name}_Cover_Letter_{Company}.docx')
    print('Cover Letter DOCX created successfully')
except Exception as e:
    log_error('applications/{folder}', 'finalizing', f'Cover Letter DOCX failed: {e}')
    print(f'Error: {e}')
"
```

Background Agent H — Update Tracker → updates state.json:
```
Use Task tool (subagent_type: "Bash", run_in_background: true, name: "tracker-updater"):
cd "." && python -c "
from tracker_utils import add_application
from orchestration_state import update_state, log_error
try:
    add_application(
        company='{Company}',
        job_title='{Job Title}',
        resume_file='{Name}_Resume_{Company}.docx',
        cover_letter_file='{Name}_Cover_Letter_{Company}.docx',
        jd_file='job_description.txt',
        ats_score={final_ats},
        hr_score={final_hr},
        application_date=None,
        status='Applied'
    )
    update_state('applications/{folder}', 'tracker_updated', True)
    print('Tracker updated successfully')
except Exception as e:
    log_error('applications/{folder}', 'finalizing', f'Tracker update failed: {e}')
    print(f'Error: {e}')
"
```

---

## PHASE 6: CLEANUP + REPORT

1. Read final state from state.json (single source of truth for all agent results):
```
cd "." && python -c "
from orchestration_state import read_state, set_phase, cleanup_state
import json
state = read_state('applications/{folder}')
set_phase('applications/{folder}', 'done')
print(json.dumps(state, indent=2))
# Check for any errors logged during the run
errors = state.get('errors', [])
if errors:
    print(f'\nWARNING: {len(errors)} error(s) during run:')
    for e in errors: print(f'  [{e[\"phase\"]}] {e[\"message\"]}')
"
```
2. Extract base_scores and tailored_scores from the state dict for the comparison report
3. Delete intermediate files: `resume.md`, `cover_letter.md`, and `state.json` (AFTER verifying DOCX paths exist in state)
```
cd "." && python -c "
from orchestration_state import cleanup_state
import os
for f in ['applications/{folder}/resume.md', 'applications/{folder}/cover_letter.md']:
    if os.path.exists(f): os.remove(f)
cleanup_state('applications/{folder}')
print('Cleanup complete')
"
```
4. Display final report:

```
================================================================================
          RESUME BUILDER - FINAL REPORT (v5.1 Triple-Scorer Swarm)
================================================================================

COMPANY: {Company Name}
POSITION: {Job Title}
DOMAIN DETECTED: {clinical_research/pharma_biotech/technology/etc.}
BASE TEMPLATE: {source application folder or "Master Resume"}

--------------------------------------------------------------------------------
                    COMBINED SCORES (70% Rules + 30% LLM)
--------------------------------------------------------------------------------

                    |  BASE RESUME  |  TAILORED RESUME  |  IMPROVEMENT
--------------------------------------------------------------------------------
COMBINED ATS        |    {X}%       |      {Y}%         |    +{Z}%
COMBINED HR         |    {X}%       |      {Y}%         |    +{Z}%
--------------------------------------------------------------------------------

--------------------------------------------------------------------------------
                    SCORER BREAKDOWN — TAILORED RESUME
--------------------------------------------------------------------------------

                    |  ATS (Rules)  |  HR (Rules)  |  ATS (LLM)  |  HR (LLM)
--------------------------------------------------------------------------------
SCORES              |    {X}%       |    {Y}%      |    {X}%     |    {Y}%
--------------------------------------------------------------------------------
  ATS Components:
  - Keywords        |    {X}%       |
  - Semantic        |    {X}%       |
  - Phrases         |    {X}%       |
  - BM25            |    {X}%       |

  HR Factors:
  - Experience      |               |    {Y}%      |
  - Skills          |               |    {Y}%      |
  - Impact          |               |    {Y}%      |
  - Job Fit         |               |    {Y}%      |
--------------------------------------------------------------------------------

  LLM INSIGHT: {llm_explanation from state.json}

--------------------------------------------------------------------------------
                         AUTHENTICITY CHECK
--------------------------------------------------------------------------------

  [x] Job titles preserved exactly from master resume
  [x] Publications unchanged
  [x] No keyword stuffing (each keyword appears 1-2x max)
  [x] Bullets read naturally to human reviewer

--------------------------------------------------------------------------------
                         GENERATED FILES
--------------------------------------------------------------------------------

  [x] {Name}_Resume_{Company}.docx
  [x] {Name}_Cover_Letter_{Company}.docx
  [x] job_description.txt

FOLDER: applications/{Company} - {JobTitle}/

================================================================================
SCORERS: 3 (ATS Rules + HR Rules + LLM Claude)
SWARM AGENTS USED: {count} | ITERATIONS: {count}
================================================================================
```

5. Offer to open web comparison reports:
```bash
python ats_scorer.py --web --base "{base_template}" --tailored "applications/{folder}/resume.md" --jd "applications/{folder}/job_description.txt"
python hr_scorer.py --score "applications/{folder}/{Name}_Resume_{Company}.docx" "applications/{folder}/job_description.txt" --web
```

---

## SCORING-AWARE WRITING RULES (Applied during Phase 2)

### STEP 0 — INTERNALIZE THE SCORING ENGINE

Read both scoring tables in full before writing a single word. Every section you write maps to specific weighted components. The weights tell you where to spend your editing budget.

ATS Scorer (7 components):

| # | Component | Weight | What Wins | Primary Section |
|---|-----------|--------|-----------|-----------------|
| 1 | Keyword Match | 22% | Lemmatized exact + synonym match. High-frequency JD nouns in Core Competencies first. | Core Competencies |
| 2 | Semantic Similarity | 22% | Sentence-transformer cosine sim. Use JD phrasing verbatim — paraphrases score lower. | Summary, Bullets |
| 3 | Weighted Industry Terms | 18% | Domain-specific terms score 3x. Auto-detected from JD domain (clinical, tech, finance, etc.). | Core Competencies, Bullets |
| 4 | Phrase Match | 13% | Exact 2-4 word JD phrases. If JD says "Medical Monitoring Plan", use those exact words. | Bullets |
| 5 | BM25 Score | 13% | Term frequency x inverse document frequency. Diminishing returns after 2 uses. | Distributed |
| 6 | Graph Centrality | 7% | Inferred skill bonus. "Protocol design" + "EDC" = scorer infers "data management". | Core Competencies (strategic adjacency) |
| 7 | Skill Recency | 5% | Exponential decay by year. Recent skills must appear in current/most-recent role. | Most recent role bullets |

HR Scorer (6 factors):

| # | Factor | Weight | What Wins | Primary Section |
|---|--------|--------|-----------|-----------------|
| 1 | Job Fit | 25% | Domain + role alignment. Must hit the auto-detected domain. Domain-defining terms in first 100 words. | Summary (first 100 words) |
| 2 | Experience Fit | 20% | Years match JD minimum +/- 3 yrs (Goldilocks zone). Don't undersell seniority. | Summary sentence 1, dates |
| 3 | Skills Match | 20% | Skill IN ACTION = 2x weight vs. skill listed. "Led medical monitoring" >> "Medical Monitoring" in a list. | Bullets (action verbs) |
| 4 | Impact Signals | 15% | Metric magnitude: $M/$B = +3 pts, multipliers (10x) = +2.5 pts, % = +2 pts, large raw numbers = +1.5 pts. 50%+ of bullets need metrics. | Bullets |
| 5 | Career Trajectory | 10% | Title regression slope must be positive. Senior to Lead to Director = good. Don't bury senior titles. | Job title ordering |
| 6 | Competitive Edge | 10% | Top-tier companies/universities = high prestige. Name them early. | Summary, Education placement |

Domain Bonuses (auto-detected):
- All domain-critical keywords found = +10 ATS pts
- Publications section present (if applicable to domain) = +10 ATS pts
- Readability grade 10-12 = +3 pts (Grade 13+ = -3 penalty — avoid complex sentences, semicolons, nested clauses)

---

### STEP 1 — JD DECONSTRUCTION (complete before writing)

Extract each item below and hold it as your editing blueprint. If you skip this step, your draft will be generic and under-optimized.

1A. Role Classification:
- Role tier: Lead CS, supporting CS, or hybrid? Determines seniority framing in summary.
- Management scope: People management required? Cross-functional leadership? Determines verb level in bullets.
- Domain focus: Specific specialty/vertical? Drives domain term selection.

1B. Language Extraction:

| Extract | Purpose | Example |
|---------|---------|---------|
| Top 5 explicit verbs from responsibilities | Drive bullet verb choices | "leads", "authors", "monitors", "coordinates", "reviews" |
| Critical noun phrases (exact 2-4 word phrases) | Reuse verbatim for Phrase Match (13%) | Extract exact multi-word phrases from the JD — these must appear verbatim |
| Hard requirements | Must appear or instant disqualification | Minimum years, degree, certifications, specific system experience |
| Preferred qualifications | High-value differentiators if experience exists | Board certification, specific TA experience, publications |
| Implicit signals | Drives summary framing and bullet emphasis | Scientific rigor? Stakeholder management? Data oversight? Operational speed? |

1C. Ceiling Check:
Does the JD contain non-role boilerplate? (Salary ranges, benefits paragraphs, staffing-agency language, EEO text exceeding 2 sentences)
- If YES: ATS ceiling is ~69-73%. Set expectations. Do NOT over-iterate chasing 75%+ if all domain component weights are at 100%. Max 2 iteration cycles.
- If NO: Standard 75-85% ATS target applies.

---

### STEP 2 — SECTION-BY-SECTION OPTIMIZATION

Each section targets specific scoring components. The component targets are listed so you know exactly why you're making each choice.

#### PROFESSIONAL SUMMARY
Targets: Semantic Similarity (22%), Job Fit (25%), BM25 (13%)

| Sentence | Purpose | Rule |
|----------|---------|------|
| 1 | Identity + seniority + domain | "[Title descriptor] with [X] years in [domain/specialty]" |
| 2 | JD phrase injection | Use 2-3 exact JD noun phrases naturally in one sentence |
| 3 | Top differentiator | Include highest-magnitude metric available |
| 4 | Forward-looking alignment | Match JD mission or company therapeutic focus |

Constraints: Max 4 lines. Readability grade 10-12. No semicolons, no nested clauses. Domain-defining terms must appear within the first 100 words of the resume (Job Fit trigger).

#### CORE COMPETENCIES
Targets: Keyword Match (22%), Weighted Industry Terms (18%), Graph Centrality (7%)

Layout: 12-14 items in a 3-column grid.

Priority order for item selection:
1. Exact JD keyword matches (Keyword Match — 22%)
2. Domain-critical terms not in JD but expected by scorer for the detected domain (Industry Terms — 18%)
3. Strategic adjacency terms that trigger inferred skills (Graph Centrality — 7%)
4. Transferable skills only if slots remain

Domain-critical keyword strategy:
Extract 10-15 domain-critical keywords from the JD + the scorer's auto-detected domain. The ATS scorer detects the domain (clinical_research, pharma_biotech, technology, finance, consulting, healthcare) and applies appropriate keyword databases automatically. Focus on terms that appear in the JD's requirements and qualifications sections — these carry the highest weight.

Rule: Each keyword gets its 1 counted appearance here. Do NOT repeat in bullets unless demonstrating it in action (which counts as a different scoring signal — Skills Match).

#### PROFESSIONAL EXPERIENCE — BULLETS
Targets: Skills Match (20%, action = 2x), Impact Signals (15%), Semantic Similarity (22%)

The Action Formula:
```
[JD verb at L3+] + [exact JD noun phrase] + resulting in + [metric with magnitude]
```

| Quality | Example | Scoring Impact |
|---------|---------|----------------|
| BAD (listed) | "Experienced in medical monitoring" | Skills Match 1x |
| GOOD (in action) | "Led Medical Monitoring team of 6 across 3 Phase III studies, reviewing 200+ SAE reports" | Skills Match 2x + Impact + Phrase Match |

Verb Hierarchy (use L3+ for 70%+ of bullets):

| Level | Label | Verbs | Usage Target |
|-------|-------|-------|--------------|
| L4 | Transformative | Pioneered, Architected, Instituted, Generated, Secured | 1-2 bullets max (signature achievements) |
| L3 | Directive | Spearheaded, Directed, Championed, Orchestrated, Established | Primary verb level (40-50% of bullets) |
| L2 | Managerial | Led, Managed, Oversaw, Coordinated, Supervised | Supporting bullets (20-30%) |
| L1 | Contributory | Reviewed, Monitored, Assisted, Supported, Participated | Minimize (10% or less) |
| L0 | AVOID | "Responsible for", "Helped", "Worked on" | Never use |

Metric Magnitude Targets:

| Magnitude Type | Score Bonus | Minimum Requirement |
|----------------|-------------|---------------------|
| $M / $B values | +3 pts | Include in at least 2 bullets |
| Multipliers (10x, 3x) | +2.5 pts | Include where truthful |
| Percentages | +2 pts | Use liberally |
| Large raw numbers | +1.5 pts | Fallback when $ or % unavailable |

50%+ of all bullets must contain a quantified metric.

Phrase Insertion Strategy (Phrase Match — 13%):
Extract exact 2-4 word noun phrases from the JD and insert them verbatim in bullets where the candidate has matching experience. The scorer rewards exact phrase matches, not paraphrases. Prioritize phrases from the JD's core responsibilities and required qualifications sections.

#### PUBLICATIONS (if present in master resume)
Targets: Domain Bonus (+10 ATS), Competitive Edge (10%)
Rule: Keep EXACTLY as in master resume. The section's existence is worth +10 ATS points. Zero edits. Only include this section if the master resume contains publications.

#### EDUCATION
Targets: Competitive Edge (10%), Experience Fit (20%)
Rule: Keep EXACTLY as in master resume. Top-tier institution = high prestige multiplier. Do not bury.

#### CERTIFICATIONS & LICENSURE
Rule: Keep EXACTLY as in master resume. Zero edits.

#### PROFESSIONAL MEMBERSHIPS
Rule: Keep EXACTLY as in master resume. Zero edits.

---

### RESUME STRUCTURE (ATS/Workday Compliant)

```
[FULL NAME, CREDENTIALS]
[City, State ZIP] | [Phone] | [Email]
[LinkedIn URL]

_______________________________________________________________________________
PROFESSIONAL SUMMARY

[4 sentences per Step 2 rules — NOT a keyword dump]

_______________________________________________________________________________
CORE COMPETENCIES

[12-14 JD-relevant keywords — PRIMARY keyword location]
[Keyword 1]    [Keyword 2]    [Keyword 3]

_______________________________________________________________________________
PROFESSIONAL EXPERIENCE

[EXACT TITLE] | [EXACT COMPANY] | [Location]
[Month Year] - [Present/End Date]

[L3+ Verb] [JD noun phrase] [STAR context + action], achieving [quantified metric]

_______________________________________________________________________________
EDUCATION

[EXACT from master resume]

_______________________________________________________________________________
CERTIFICATIONS & LICENSURE

[EXACT from master resume]

_______________________________________________________________________________
PUBLICATIONS

[EXACT from master resume — NO keyword additions]

_______________________________________________________________________________
PROFESSIONAL MEMBERSHIPS

[EXACT from master resume]
```

ATS FORMAT RULES:
- NO columns, tables, text boxes, graphics, icons, headers/footers
- YES ALL-CAPS headers, bullet points, horizontal lines (___)
- Font: Calibri/Arial, 10-12pt body, 14-16pt name
- Contact info in MAIN BODY
- Job format: "TITLE | COMPANY | Location" (Workday pattern)
- Do NOT use ** in .md files — DOCX generator handles bold formatting

### EXPERIENCE BULLET DISTRIBUTION
- Current role: 4-6 bullets (strongest metrics, most detail)
- Recent relevant roles: 3-4 bullets each
- Older relevant roles: 2-3 bullets each
- Very old roles (10+ years): 1-2 bullets

---

### WRITING COACH (Rules 1-10 — Apply to EVERY bullet)

Rule 1 (So What?): Every bullet must show impact, not just activity
Rule 2 (6-Second): Front-load value in first 3 words of each bullet
Rule 3 (Deadwood): Strip "Responsible for", "Successfully", "Various", "Helped", "Assisted"
Rule 4 (Metrics): 50%+ of bullets must contain quantified metrics (plain text, no ** bold)
Rule 5 (Verbs L3+): 70%+ verbs at Directive/Strategic/Transformative level
Rule 6 (Architecture): Use Impact Lead, Challenge-Action-Result, or Scope-Authority structures
Rule 7 (Rhythm): Vary bullet lengths — mix long, medium, short punchy bullets
Rule 8 (Parallel): Consistent grammar patterns within each role
Rule 9 (Summary Hook): Open with identity + authority, close with differentiator
Rule 10 (Authenticity): Every bullet must pass the "Could they discuss this in an interview?" test

Tone: Senior professional — authoritative and evidence-based, NOT junior coordinator.

---

## QUICK REFERENCE — SCORING CHEAT SHEET

Fastest levers by gap type:

| Problem | Fastest Fix | Weight Moved |
|---------|-------------|--------------|
| ATS low, keywords missing | Add to Core Competencies | 22% |
| ATS low, phrasing off | Rewrite Summary in JD language | 22% |
| HR low, skills listed not demonstrated | Convert list items to action bullets | 20% (2x multiplier) |
| HR low, no metrics | Add metrics to 50%+ of bullets | 15% |
| HR low, weak opening | Rewrite Summary sentence 1 with domain identity | 25% |
| Both low, domain terms missing | Add domain-critical keywords | 18% ATS + 25% HR (Job Fit) |

Component coverage by section:

| Section | ATS Components Hit | HR Factors Hit |
|---------|-------------------|----------------|
| Summary | Semantic (22%), BM25 (13%) | Job Fit (25%), Experience (20%), Edge (10%) |
| Core Competencies | Keyword (22%), Industry (18%), Graph (7%) | — |
| Bullets | Phrase (13%), Semantic (22%), Recency (5%) | Skills (20%), Impact (15%), Trajectory (10%) |
| Publications | Domain bonus (+10) | Edge (10%) |
| Education | — | Edge (10%), Experience (20%) |

---

## ETHICAL REQUIREMENTS (NON-NEGOTIABLE)

- NEVER CHANGE JOB TITLES — Must match master resume exactly
- NEVER CHANGE PUBLICATIONS — Titles and citations stay as-is
- Never invent experience — Only reframe existing content
- Keywords go in: Core Competencies (primary), Summary (3-5 terms), select bullets
- Keywords do NOT go in: Titles, company names, education, publications, certifications, memberships
