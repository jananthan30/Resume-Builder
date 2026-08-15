---
description: Process multiple job descriptions from batch_jds into tailored application packages.
---

# Batch Resume Builder — Native Resume Teams

Process multiple job descriptions with concurrent read/scoring work. Each resume is produced by the native four-role team; final DOCX and tracker mutations remain ordered and coordinator-owned.

## Arguments
$ARGUMENTS

## Instructions

You are the **team lead** for a batch resume processing operation. Execute the following steps:

---

## STEP 1: DISCOVER JDs (READ-ONLY)

Scan the `batch_jds/` folder for `.txt` files:
```
Use file search (`rg --files batch_jds` or `find`) for `batch_jds/*.txt`
```

Each file should be named: `{Company} - {Job Title}.txt`

Parse the filename to extract:
- **Company**: Everything before ` - `
- **Job Title**: Everything after ` - ` (without `.txt`)

If no files found, tell the user to add JD text files to `batch_jds/` and explain the naming format.

Display a numbered list of all JDs found and confirm with the user before proceeding.

---

## STEP 2: CANDIDATE-FIT PREFLIGHT (MANDATORY FIRST GATE PER JD)

Resolve and read `master_resume_path` from `config.json` once. It is the sole base
and factual source for every job; never inspect, screen, or substitute an existing
tailored application resume. For each exact JD, independently generate a safe
`run_id`, safe `case_id`, and strict ISO `as_of_date`, then run the deterministic
machine preflight before starting a scorer, role/native team, output directory,
resume draft, DOCX, or tracker operation for that JD:

`python candidate_fit_preflight.py --resume <configured-master-resume> --job-description <exact-JD-file> --run-id <run_id> --case-id <case_id> --as-of-date <YYYY-MM-DD> --json`

Require exit `0` and a valid `candidate-fit-policy-v3` report bound to that JD,
master, date, and IDs. Canonically hash it as `candidate_fit_report_digest`.
A passing report has exact threshold `70.0`, score at least 70, trustworthy
extraction, zero hard knockouts, `passed: true`, and no codes. Exit `1`, any score
below 70 (including 60–69), or any hard knockout is
`REJECTED:CANDIDATE_FIT`. Create nothing for that JD, record the rejection in the
in-memory/private batch report, and continue screening the remaining JDs. Exit `2`
or an unavailable, malformed, stale, non-canonical, or digest-mismatched report is
`FAILED:CANDIDATE_FIT_PREFLIGHT`; fail that JD closed, create nothing, and continue
the remaining independent JDs. There is no automatic or manual workflow bypass.
ATS/HR scores are advisory and cannot override this gate.

Keep each passing report, canonical digest, IDs, and date in private coordinator
state. Do not create an application directory to store preflight results.

---

## STEP 3: SCORER SERVER PRE-FLIGHT FOR PASSING JDs

If no JD passes candidate fit, skip scorer startup and finish with the rejection
report. Otherwise, check whether the scorer server is running:

```
curl -s http://localhost:8100/health
```

- **If server responds**: Proceed immediately.
- **If NOT running**: Start it in a background shell session from the repo root
  with `python scorer_server.py --port 8100`.
- Wait up to 45 seconds for `/health`. If unavailable, use CLI ATS/HR scoring after
  publication; that advisory failure cannot bypass or reverse candidate fit.

---

## STEP 4: RUN ONE NATIVE RESUME TEAM PER JD

Operate only on JDs with a passing candidate-fit report. Read
`commands/resume-team.md`, run the non-model host preflight once, and require
macOS or Linux; Windows fails closed with `POSIX_RUNTIME_REQUIRED`. For **each JD
file**, derive a prospective non-existing output path but do not create it. Invoke
`native_resume_team.py` for the current host with that JD's exact `run_id`,
`case_id`, and `as_of_date` instead of manually
reproducing `resume-team/v2`. The runtime enforces role separation, lineage,
replay protection, three deterministic votes, and receipt/readback verification.
Independent read/scoring work may overlap across JDs, but draft publication and
tracker writes are serialized.

The runtime recomputes candidate fit from the configured master and exact JD before
constructing its output state or role adapter. Require its report and canonical
digest to exactly match the independently validated preflight. Each
`resume-team-result/v2` runtime `PUBLISHED` result is only an authorized,
digest-verified `resume.md`
draft-stage artifact. Independently verify `final_draft_digest`; do not count it as
a completed package. The task must still finish its cover letter, both DOCX files,
tracker update, artifact verification, cleanup, and final report.

If a runtime defense-in-depth recomputation instead returns
`REJECTED:CANDIDATE_FIT` or `FAILED:CANDIDATE_FIT_PREFLIGHT`, require that JD's
prospective output path to remain absent, record the closed result, and continue
the remaining independently passing JDs without creating a fallback package.

For every draft, resolve `authorization_receipt_path` against its output directory
when relative and require the resolved parent to equal that resolved directory.
Read only regular, non-symlink JSON, recompute its canonical digest, and require it
to equal `authorization_receipt_digest` and the inline `authorization_receipt`.
Validate its `resume-team-final-receipt/v2` shape against
`schemas/resume-team-final-receipt.schema.json` before accepting it. Require the
receipt's `candidate_fit_report` and `candidate_fit_report_digest` to exactly match
the runtime and independent preflight, including the fixed threshold, passing
decision, empty hard knockouts/codes, IDs, date, and master/JD digests. Require
matching run/case IDs and bind `draft_digest` and `verified_target_digest`
to `final_draft_digest` and the independently hashed `resume.md`. Recompute
`source_digest` from the current configured master and `job_description_digest`
from the fixed sibling `job_description.txt`; require a SHA-256 Researcher
artifact and distinct same-host native Researcher/Auditor identities. Require exact
`auditor_attestation` with a native agent ID, SHA-256 artifact digest, PASS verdict,
and the same draft. Require exact `authorization_report` with the same draft,
`passed: true`, no codes, and exactly three ordered named PASS votes—`evidence`,
`human_voice`, `canonical_integrity`—with no codes, the same draft, and distinct
invocation IDs. Require its canonical digest to equal `authorization_digest`, and
require `vote_invocation_ids` to equal those vote IDs in order. Require a
publication ID. Any missing, malformed, stale, or mismatched sidecar fails that
package closed. Preserve every durable receipt during cleanup.

Every final resume requires a final Auditor `PASS` and three independent exit-0
votes on the same digest: `evidence_audit.py`, `human_voice_audit.py`, and
`resume_integrity_audit.py`. No score can override a failed vote.

Authorization is byte-specific and immutable for every package. After the runtime
publishes an authorized role draft, coordinators, scorers, and audits must not edit,
rewrite, normalize, format, or otherwise change it. Any desired resume change
invalidates that package run and requires the complete four-role sequence under a
fresh `run_id`; never reuse handoffs, outputs, verdicts, votes, or digests. Writing
guidance below is for native role agents in a fresh run only.

Each package task uses this prompt (fill in the specifics per JD):

```
You are the coordinator for one job. Generate a COMPLETE application package through the native role-separated Resume Team.

## YOUR ASSIGNMENT
- Company: {Company}
- Job Title: {Job Title}
- JD file: .\batch_jds\{filename}
- Output folder: .\applications\{Company} - {Job Title}\
- Base resume: {configured master_resume_path only}

## MASTER RESUME (canonical — titles, dates, education, publications, certifications, memberships NEVER change)
[Paste the full master resume text here]

## STEPS (execute in order)

### 1. Setup
- Read the JD file
- Revalidate this JD's private passing candidate-fit report and digest
- Require the prospective output path not to exist; do not create it or copy the JD
- Do not read or use any previous tailored resume

### 2. Run Native Resume Team
Invoke `native_resume_team.py` for this JD and prospective output path with the
same run/case IDs and date used by candidate-fit preflight. It recomputes and gates
fit before creating output or invoking Researcher. Accept only a matching passing
candidate-fit report/digest, exit `0`, `terminal_class: PUBLISHED`, and an
independent SHA-256 match between `resume.md`
and `final_draft_digest`. The runtime writes the file atomically; do not write or
save it again. Treat this as draft-stage authorization only and then apply these
STRICT rules throughout the remaining package gates:

Read and validate `authorization_receipt_path`, `authorization_receipt_digest`, and
the inline `authorization_receipt` against the result and `resume.md` exactly as
required above. Do not continue on a missing, symlinked, malformed, stale, or
mismatched sidecar.

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

**WRITING RULES (Human voice first — see commands/writing-coach.md):**
- Priority: Authenticity → Human voice → HR impact → ATS
- 50%+ bullets with real metrics (plain text, no ** bold in .md files)
- Front-load value; plain strong verbs: Led, Built, Wrote, Cut, Reviewed, Directed, Managed
- **Banned openers:** Spearheaded, Leveraged, Orchestrated, Championed, Architected, Pioneered, Utilized, Facilitated, Streamlined
- Brevity: prefer 12–22 words/bullet; hard max 28; mix short + medium lengths (burstiness)
- Summary: 2–3 short sentences; never "Results-driven…"; ≤ 70 words
- Keywords live in Core Competencies first — never cosplay JD jargon into every bullet
- No deadwood: strip "Responsible for", "Successfully", "Various", "Helped"
- Current role: 4-6 bullets, recent roles: 3-4, older: 2-3, very old: 1-2

The runtime already published `resume.md` in the output folder. Do not save, rewrite,
normalize, or otherwise alter it.

**MANDATORY DIGEST-BOUND AUTHORIZATION before DOCX:**

First require that `resume.md` still matches the candidate digest and the final
Auditor returned `PASS` for exactly that digest. Then run all three independent
votes; they diagnose and authorize but never edit:
```bash
python evidence_audit.py "applications/{Company} - {Job Title}/resume.md"
python human_voice_audit.py "applications/{Company} - {Job Title}/resume.md"
python resume_integrity_audit.py --config config.json --tailored "applications/{Company} - {Job Title}/resume.md"
```

Recompute SHA-256 immediately before and after each command, and record that
command's exit code only against the observed unchanged candidate digest.
Authorization exists only when the final Auditor passed, all three votes exited
0, and the digest stayed identical. If any vote fails or the digest changes,
reject this run. Do not repair `resume.md` from audit output. Either stop this
package or restart Researcher under a fresh `run_id` and repeat the complete role
sequence plus all three votes.

**CRITICAL .md FORMATTING RULE:** Do NOT use `**` (markdown bold asterisks) anywhere in resume.md or cover_letter.md files. Write all text as plain text. The DOCX generator handles bold formatting automatically.

### 3. Score Resume
Score using the server:
```bash
curl -s -X POST http://localhost:8100/score/both -H "Content-Type: application/json" -d "{\"resume_path\": \"applications/{Company} - {Job Title}/resume.md\", \"jd_path\": \"applications/{Company} - {Job Title}/job_description.txt\"}"
```

Parse the JSON response to extract ATS total_score and HR overall_score. Scores are
advisory and may not cause edits to this authorized candidate. If ATS < 70% or HR
< 65%, either accept and report the fully authorized draft honestly, or discard it
and perform one complete fresh `resume-team/v2` run with a new `run_id`, followed
by a new Auditor verdict and three new digest-bound votes. Never patch the old
draft, invoke Writer or Editor alone, or reuse any prior authorization artifact.

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

[Para 1: Hook with a concrete, supported metric + connection to company mission]
[Para 2: 2-3 STAR achievements mapped to JD requirements]
[Para 3: Additional experience + domain expertise]
[Para 4: Closing with call to action]

Sincerely,

{User Name, Credentials}
```

Save as cover_letter.md in the output folder. Cover-letter creation or correction
must never alter the authorized resume bytes.

Run `python human_voice_audit.py "applications/{Company} - {Job Title}/cover_letter.md" --mode cover_letter` and require exit 0 before cover-letter DOCX creation. Maximum two correction rounds.

### 5. Create DOCX Files
Immediately before DOCX, reread and revalidate the durable authorization sidecar
against the runtime result and `resume.md`. Then recompute `resume.md` SHA-256 and require it to equal the
digest shared by the final Auditor `PASS` and all three exit-0 votes. No process
may change `resume.md` after this check. Run the resume command synchronously and
let exceptions propagate. Do not start the cover-letter DOCX or tracker until the
verified resume DOCX succeeds.
```bash
cd "." && python -c "
from pathlib import Path
from docx_generator import create_resume_from_md_authorized
from final_receipt_verifier import verify_final_receipt
app_dir = Path('applications/{Company} - {Job Title}')
resume_path = app_dir / 'resume.md'
raw_receipt = Path('{authorization_receipt_path from runtime result}')
receipt_path = raw_receipt if raw_receipt.is_absolute() else app_dir / raw_receipt
receipt_digest = '{authorization_receipt_digest from runtime result}'
output_path = app_dir / '{Name}_Resume_{CompanyShort}.docx'
verify_final_receipt(resume_path=resume_path, receipt_path=receipt_path, expected_receipt_digest=receipt_digest)
create_resume_from_md_authorized(str(resume_path), str(output_path), receipt_path=str(receipt_path), expected_receipt_digest=receipt_digest, config_path='config.json')
if output_path.is_symlink() or not output_path.is_file() or output_path.stat().st_size == 0:
    raise RuntimeError('AUTHORIZED_RESUME_DOCX_NOT_VERIFIED')
print('Resume DOCX done')
"
```

```bash
cd "." && python -c "
from pathlib import Path
from docx_generator import create_cover_letter_from_md_authorized
from final_receipt_verifier import verify_final_receipt
app_dir = Path('applications/{Company} - {Job Title}')
resume_path = app_dir / 'resume.md'
raw_receipt = Path('{authorization_receipt_path from runtime result}')
receipt_path = raw_receipt if raw_receipt.is_absolute() else app_dir / raw_receipt
receipt_digest = '{authorization_receipt_digest from runtime result}'
output_path = app_dir / '{Name}_Cover_Letter_{CompanyShort}.docx'
verify_final_receipt(resume_path=resume_path, receipt_path=receipt_path, expected_receipt_digest=receipt_digest)
create_cover_letter_from_md_authorized(str(app_dir / 'cover_letter.md'), str(output_path), authorized_resume_path=str(resume_path), receipt_path=str(receipt_path), expected_receipt_digest=receipt_digest, job_title='{Job Title}', company='{Company}')
if output_path.is_symlink() or not output_path.is_file() or output_path.stat().st_size == 0:
    raise RuntimeError('AUTHORIZED_COVER_LETTER_DOCX_NOT_VERIFIED')
print('Cover letter DOCX done')
"
```

{CompanyShort} = Company name with spaces replaced by underscores, no special characters.

### 6. Return Tracker Row (do not mutate the shared tracker)
After both DOCX commands succeed, return the company, job title, two verified
filenames, JD filename, final scores, authorized resume digest, durable
authorization-receipt path and digest, inline receipt, final Auditor receipt, and
three vote receipts to the batch coordinator. Only the batch coordinator may call
`tracker_utils.add_application_authorized()`, one row at a time, after each package
and its authorization record are verified. Lower-level tracker mutation is forbidden.

### 7. Preserve Intermediates
Do not delete resume.md, cover_letter.md, state, or authorization records. The
batch coordinator cleans transient files only after its serialized
tracker write succeeds and the two DOCX files are re-verified.

### 8. Report Back
When done, report:
- Company + Job Title
- ATS Score + HR Score
- Files created
- Any issues encountered
```

---

## STEP 5: MONITOR & COLLECT PASSING RESULTS

Wait only for package tasks whose candidate-fit gate passed; rejected/failed-fit
JDs never become package tasks. As each passing task finishes, verify its two DOCX
artifacts, final resume digest, durable authorization sidecar and its canonical
digest, inline receipt, Auditor receipt, three same-digest vote receipts, and
tracker row. Then update `Job_Application_Tracker.xlsx` serially in deterministic
JD order. If authorization is incomplete/stale or one tracker write fails, stop;
do not clean that or any later package, and do not report the failed row as applied.
For each row, resolve the exact sidecar path and digest from that package's captured
runtime result, then use this guarded mutation path:

```python
from pathlib import Path
from final_receipt_verifier import verify_final_receipt
from tracker_utils import TrackerUpdateError, add_application_authorized

app_dir = Path("applications/{Company} - {Job Title}")
resume_path = app_dir / "resume.md"
raw_receipt = Path("{authorization_receipt_path from runtime result}")
receipt_path = raw_receipt if raw_receipt.is_absolute() else app_dir / raw_receipt
receipt_digest = "{authorization_receipt_digest from runtime result}"
verify_final_receipt(resume_path=resume_path, receipt_path=receipt_path, expected_receipt_digest=receipt_digest)
updated = add_application_authorized(
    company="{Company}",
    job_title="{Job Title}",
    authorized_resume_path=str(resume_path),
    receipt_path=str(receipt_path),
    expected_receipt_digest=receipt_digest,
    resume_file="{Name}_Resume_{CompanyShort}.docx",
    cover_letter_file="{Name}_Cover_Letter_{CompanyShort}.docx",
    jd_file="job_description.txt",
    ats_score={final_ats},
    hr_score={final_hr},
    application_date=None,
    status="Applied",
)
if updated is not True:
    raise TrackerUpdateError("TRACKER_UPDATE_NOT_CONFIRMED")
```

The authorized tracker wrapper revalidates the receipt again at its mutation
boundary. Propagate every exception. Do not mark the row successful or start
cleanup unless the return value is literally `True`.
After each successful tracker row, re-verify both DOCX files, then delete only that
package's transient Markdown/state files. Never delete its durable authorization
receipt.

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
#  | COMPANY              | JOB TITLE              | FIT  | ATS  | HR   | STATUS
--------------------------------------------------------------------------------
1  | {Company1}           | {Title1}               | {F}  | {X}% | {Y}% | Done
2  | {Company2}           | {Title2}               | {F}  |  —   |  —   | REJECTED:CANDIDATE_FIT
...
--------------------------------------------------------------------------------

GENERATED FILES:
{list all output folders and their contents}

================================================================================
CONCURRENT TASKS USED: {count} | TOTAL TIME: ~{X} min
================================================================================
```

After the report, offer to:
1. Open individual web score reports for any application
2. Re-run advisory tailoring for a fit-passing JD that scored poorly. A blocked
   candidate-fit report may be rerun only after genuine master-resume evidence or
   exact-JD input changes; it cannot be bypassed.

---

## NOTES

- All package tasks share the scorer server on port 8100 (no model reloading)
- Each candidate-fit decision is independent. A rejected JD creates no package and
  does not prevent other passing JDs from continuing.
- DOCX creation uses markdown-to-DOCX pipeline (no bash quoting issues)
- Master resume is passed directly to each package task (no file read race conditions)
- The batch_jds/ folder is NOT cleared after processing — user manages it manually
