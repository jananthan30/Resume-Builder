# Batch Resume Builder — Team Swarm (Parallel Processing)

Process multiple job descriptions simultaneously using a team of parallel agents. Each agent independently generates a full application package (resume + cover letter + DOCX + tracker update).

## Arguments
$ARGUMENTS

## Instructions

You are the **team lead** for a batch resume processing operation. Execute the following steps:

---

## STEP 1: SCORER SERVER PRE-FLIGHT

Check if the scorer server is running:
```
curl -s http://localhost:8100/health
```

- **If server responds**: Proceed immediately.
- **If NOT running**: Start it:
```
Use Task tool (subagent_type: "Bash", run_in_background: true, name: "scorer-server"):
cd "." && python scorer_server.py --port 8100
```
Wait up to 45 seconds for `/health` to respond. If it fails, warn the user.

---

## STEP 2: DISCOVER JDs

Scan the `batch_jds/` folder for `.txt` files:
```
Use Glob: batch_jds/*.txt
```

Each file should be named: `{Company} - {Job Title}.txt`

Parse the filename to extract:
- **Company**: Everything before ` - `
- **Job Title**: Everything after ` - ` (without `.txt`)

If no files found, tell the user to add JD text files to `batch_jds/` and explain the naming format.

Display a numbered list of all JDs found and confirm with the user before proceeding.

---

## STEP 3: PARALLEL RESEARCH

Read the master resume once (it's shared across all jobs):
```
Read the master resume file (check config.json for master_resume_path, or glob for *MASTER*RESUME*.md)
```

Also find existing application resumes to use as best-match templates:
```
Glob: applications/**/*Resume*.docx
```

For each JD, identify the best matching existing resume based on domain/role similarity.

---

## STEP 4: LAUNCH PARALLEL AGENTS (one per JD)

For **each JD file**, launch a background `general-purpose` agent using the Task tool. Launch ALL agents in a **single parallel tool call** to maximize concurrency.

Each agent gets this prompt (fill in the specifics per JD):

```
You are a resume writer agent. Generate a COMPLETE application package for one job.

## YOUR ASSIGNMENT
- Company: {Company}
- Job Title: {Job Title}
- JD file: .\batch_jds\{filename}
- Output folder: .\applications\{Company} - {Job Title}\
- Base template: {best_matching_resume_path or master resume from config.json}

## MASTER RESUME (canonical — titles, dates, education, publications, certifications, memberships NEVER change)
[Paste the full master resume text here]

## STEPS (execute in order)

### 1. Setup
- Read the JD file
- Create the output folder (mkdir -p)
- Copy JD as job_description.txt in the output folder
- If a best-match template .docx exists, read it via: python -c "from docx import Document; [print(p.text) for p in Document('path').paragraphs]"

### 2. Write Tailored Resume
Write resume.md following these STRICT rules:

**AUTHENTICITY (NON-NEGOTIABLE):**
- Job titles: EXACTLY as master resume
- Company names: NEVER change
- Dates: NEVER change
- Education: EXACTLY as-is
- Publications: NEVER modify — copy exactly from master resume
- Certifications: EXACTLY as-is
- Professional memberships: EXACTLY as-is

**WHAT YOU CAN MODIFY:**
- Professional Summary: Incorporate 3-5 JD keywords naturally
- Core Competencies: 12-14 JD-relevant keywords (PRIMARY keyword location)
- Bullet points: Reframe achievements using JD language where natural

**KEYWORD RULES:** Each keyword 1-2x MAX. No stuffing. 75% authentic > 90% stuffed.

**RESUME FORMAT (ATS/Workday):**
```
{USER_NAME, CREDENTIALS}
{City, State ZIP} | {Phone} | {Email}
{LinkedIn URL}

_______________________________________________________________________________
PROFESSIONAL SUMMARY
[3-4 lines with JD terms naturally]

_______________________________________________________________________________
CORE COMPETENCIES
• Keyword 1    • Keyword 2    • Keyword 3
[12-14 keywords in 2-column layout with bullet separators]

_______________________________________________________________________________
PROFESSIONAL EXPERIENCE
[EXACT TITLE from master] | [EXACT COMPANY] | [Location]
[Exact Dates from master]
• [Strong verb] [achievement with quantified metrics]

_______________________________________________________________________________
EDUCATION
[EXACT from master]

_______________________________________________________________________________
CERTIFICATIONS & LICENSURE
[EXACT from master]

_______________________________________________________________________________
PUBLICATIONS
[EXACT from master — ALL publications, NO modifications]

_______________________________________________________________________________
PROFESSIONAL MEMBERSHIPS
[EXACT from master]
```

**WRITING RULES:**
- 50%+ bullets must have quantified metrics (plain text, no ** bold in .md files)
- Front-load value in first 3 words of each bullet
- Use L3+ verbs: Directed, Spearheaded, Championed, Orchestrated, Architected, Pioneered
- No deadwood: strip "Responsible for", "Successfully", "Various", "Helped"
- Current role: 4-6 bullets, recent roles: 3-4, older: 2-3, very old: 1-2

Save as resume.md in the output folder.

**CRITICAL .md FORMATTING RULE:** Do NOT use `**` (markdown bold asterisks) anywhere in resume.md or cover_letter.md files. Write all text as plain text. The DOCX generator handles bold formatting automatically.

### 3. Score Resume
Score using the server:
```bash
curl -s -X POST http://localhost:8100/score/both -H "Content-Type: application/json" -d "{\"resume_path\": \"applications/{Company} - {Job Title}/resume.md\", \"jd_path\": \"applications/{Company} - {Job Title}/job_description.txt\"}"
```

Parse the JSON response to extract ATS total_score and HR overall_score.

If ATS < 70% or HR < 65%, do ONE iteration:
- Add missing keywords to Core Competencies
- Reframe 1-2 bullets with JD language
- Re-score

### 4. Write Cover Letter
Write cover_letter.md (350-400 words, 4 paragraphs):
```
{User Name, Credentials}
{City, State ZIP}
{Phone} | {Email}

{Today's Date}

{Company}
{City, State}

Dear Hiring Manager,

[Para 1: Hook with bold metric + connection to company mission]
[Para 2: 2-3 STAR achievements mapped to JD requirements]
[Para 3: Additional experience + domain expertise]
[Para 4: Closing with call to action]

Sincerely,

{User Name, Credentials}
```

Save as cover_letter.md in the output folder.

### 5. Create DOCX Files
```bash
cd "." && python -c "from docx_generator import create_resume_from_md; create_resume_from_md('applications/{Company} - {Job Title}/resume.md', 'applications/{Company} - {Job Title}/{Name}_Resume_{CompanyShort}.docx'); print('Resume DOCX done')"
```

```bash
cd "." && python -c "from docx_generator import create_cover_letter_from_md; create_cover_letter_from_md('applications/{Company} - {Job Title}/cover_letter.md', 'applications/{Company} - {Job Title}/{Name}_Cover_Letter_{CompanyShort}.docx', '{Job Title}'); print('Cover letter DOCX done')"
```

{CompanyShort} = Company name with spaces replaced by underscores, no special characters.

### 6. Update Tracker
```bash
cd "." && python -c "from tracker_utils import add_application; add_application(company='{Company}', job_title='{Job Title}', resume_file='{Name}_Resume_{CompanyShort}.docx', cover_letter_file='{Name}_Cover_Letter_{CompanyShort}.docx', jd_file='job_description.txt', ats_score={ats_score}, hr_score={hr_score}, status='Applied'); print('Tracker updated')"
```

### 7. Cleanup
Delete resume.md and cover_letter.md from the output folder AFTER DOCX creation succeeds.

### 8. Report Back
When done, report:
- Company + Job Title
- ATS Score + HR Score
- Files created
- Any issues encountered
```

---

## STEP 5: MONITOR & COLLECT RESULTS

Wait for all agents to complete. As each agent finishes, collect their results.

---

## STEP 6: FINAL BATCH REPORT

Display a summary table:

```
================================================================================
                 BATCH RESUME BUILDER - FINAL REPORT
================================================================================

Total JDs Processed: {count}
Scorer Server: http://localhost:8100 (warm)

--------------------------------------------------------------------------------
#  | COMPANY              | JOB TITLE                    | ATS  | HR   | STATUS
--------------------------------------------------------------------------------
1  | {Company1}           | {Title1}                     | {X}% | {Y}% | Done
2  | {Company2}           | {Title2}                     | {X}% | {Y}% | Done
...
--------------------------------------------------------------------------------

GENERATED FILES:
{list all output folders and their contents}

================================================================================
AGENTS USED: {count} | TOTAL TIME: ~{X} min
================================================================================
```

After the report, offer to:
1. Open individual web score reports for any application
2. Re-run any specific JD that scored poorly

---

## NOTES

- All agents share the scorer server on port 8100 (no model reloading)
- Each agent is fully independent — if one fails, others continue
- DOCX creation uses markdown-to-DOCX pipeline (no bash quoting issues)
- Master resume is passed directly to each agent (no file read race conditions)
- The batch_jds/ folder is NOT cleared after processing — user manages it manually
