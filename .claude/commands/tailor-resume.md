---
description: Tailor a resume to a job description, score it against ATS and HR rubrics, create a DOCX, and update the tracker.
---

# Tailor Resume Only — Native Four-Role Team

Optimize and tailor the resume using concurrent Codex shell and file operations for speed. Target: 75-85% ATS + 70%+ HR with AUTHENTIC content.

## Job Description
$ARGUMENTS

## CANDIDATE-FIT PREFLIGHT (MANDATORY FIRST GATE)

Before scorer startup, research, resume development, any role/native-team
invocation, output/application-directory creation, DOCX generation, or tracker
work, resolve `master_resume_path` from `config.json` and put this exact job
description in a private temporary UTF-8 file. Screen only that configured master
resume—never a previously tailored resume. Generate one safe `run_id`, one safe
`case_id`, and one strict ISO `as_of_date`, then run:

`python candidate_fit_preflight.py --resume <configured-master-resume> --job-description <private-exact-JD.txt> --run-id <run_id> --case-id <case_id> --as-of-date <YYYY-MM-DD> --json`

Require exit `0` and a valid `candidate-fit-policy-v2` report bound to the same
IDs, date, master SHA-256, and exact-JD SHA-256. Canonically hash it as
`candidate_fit_report_digest`. Continue only with exact threshold `70.0`, score at
least 70, trustworthy extraction, zero hard knockouts, `passed: true`, and no
codes. Exit `1`, a score below 70 (including 60–69), or any hard knockout is
`REJECTED:CANDIDATE_FIT`; create no application directory, draft, DOCX, or tracker
row and invoke no role or native team. Exit `2` or an unavailable, malformed,
stale, non-canonical, or digest-mismatched report is
`FAILED:CANDIDATE_FIT_PREFLIGHT` and fails closed. There is no automatic or manual
workflow bypass. ATS and HR baselines are separate advisory diagnostics and cannot
override this gate.

## Instructions

You are the coordinator, not the resume author. The user has provided a job description above. Execute the following phases, keeping independent scoring and file work concurrent where safe.

---

## NATIVE RESUME TEAM (mandatory)

Only after candidate fit passes, read `commands/resume-team.md`, run its host
preflight, and invoke `native_resume_team.py` for the current host with the same
run/case IDs, `as_of_date`, exact private JD file, and a prospective non-existing
output path. Do not create the path first. Do not manually reproduce the role
sequence for a publishable draft. The runtime runs `resume-researcher` ->
`resume-writer` -> `resume-auditor`, then `resume-editor` only after `FAIL`, followed
by a fresh Auditor. It validates handoff digests, parents, distinct identities, and
replay state and allows at most two Editor corrections. The coordinator must not
author or silently repair resume prose. Require macOS or Linux; Windows preflight
fails closed with `POSIX_RUNTIME_REQUIRED`.

For Codex only, omit model flags by default because the hardened subprocess ignores
user configuration and transient parent-session settings; do not claim an inherited
profile, model, or Ultra setting. Add `--model <exact-model>` and/or
`--reasoning-effort ultra` only when explicitly requested. There is no runtime
profile option, and Claude must not receive Codex-only pins.

Accept the `resume-team-result/v2` runtime result only on exit `0`,
`terminal_class: PUBLISHED`, and an
independent SHA-256 match between `final_draft_digest` and the published
`resume.md`. Require `candidate_fit_report` and `candidate_fit_report_digest` to
exactly match the independently validated preflight. This is only an authorized,
digest-verified Markdown draft-stage
artifact—not a completed resume package. This command must still create and verify
the resume DOCX, update and verify the tracker, clean up only after those gates, and
complete its final report before reporting package completion.

If the runtime's defense-in-depth recomputation returns
`REJECTED:CANDIDATE_FIT` or `FAILED:CANDIDATE_FIT_PREFLIGHT`, require the
prospective output path to remain absent and stop without any role result,
finalization, or fallback draft.

The `PUBLISHED` result must also contain an inline `resume-team-final-receipt/v2`
`authorization_receipt`, its
`authorization_receipt_digest`, and a durable `authorization_receipt_path`. Resolve
it against the output directory when relative and require its resolved parent to
equal the resolved output directory. Read only a regular, non-symlink JSON file;
validate `schemas/resume-team-final-receipt.schema.json`, recompute its canonical
digest, and match both receipt fields. Require matching run/case IDs; require the
receipt's candidate-fit report and digest to match the runtime and independent
preflight; and bind `draft_digest` and `verified_target_digest` to
`final_draft_digest` and the independently hashed `resume.md`. Recompute
`source_digest` from the current configured master and `job_description_digest`
from the fixed sibling `job_description.txt`; require a SHA-256 Researcher
artifact and distinct same-host native Researcher/Auditor identities. Require exact
`auditor_attestation` with a native agent ID, SHA-256 artifact digest, PASS verdict,
and the same draft digest. Require exact `authorization_report` with the same draft,
`passed: true`, no codes, and exactly three ordered named PASS votes—`evidence`,
`human_voice`, `canonical_integrity`—with no codes, the same draft, and distinct
invocation IDs. Its canonical digest must equal `authorization_digest`, and
`vote_invocation_ids` must equal the vote IDs in order. Require a publication ID.
Fail closed on any missing, malformed, stale, or mismatched value, and preserve the
durable sidecar during cleanup.

Draft-stage publication requires a final Auditor `PASS` plus three independent exit-0 votes on
the exact same final digest: `evidence_audit.py`, `human_voice_audit.py`, and
`resume_integrity_audit.py`. Scores are advisory and cannot override these gates.

Authorization is byte-specific and immutable. Once the runtime publishes the authorized draft,
the coordinator, scorers, and audits must not edit, rewrite, normalize, format, or
otherwise alter `resume.md`. Any desired change invalidates the run and requires a
complete new `resume-team/v2` sequence under a fresh `run_id`; never reuse its
handoffs, role outputs, Auditor verdict, votes, or digest. All writing guidance in
this command is for native role agents in a fresh run, not the coordinator.

---

## PHASE 0: SCORER SERVER PRE-FLIGHT

Check if the scorer server is running:
```
curl -s http://localhost:8100/health
```

- **If server responds** with `{"status":"ok",...}`: Proceed immediately (scoring calls will take <2s each).
- **If server NOT running**: Start it in background:
```
Start this as a background shell session from the repo root:
cd "." && python scorer_server.py --port 8100
```
Then retry `/health` up to 45 seconds (models take ~30s to load). Once healthy, proceed.
- **Fallback**: If server can't start after 45s, fall back to CLI pattern (`python ats_scorer.py --score ... --json`).

---

## PHASE 1: READ-ONLY MASTER/JD PLANNING

The candidate-fit gate is already complete. Keep the configured master resume as
the sole base and factual source; do not search for or use a prior tailored resume.

- Read the configured master for canonical job titles, dates, companies,
  education, certifications, publications, and memberships.
- Extract company and role only to derive a prospective sanitized
  `applications/{CompanyName} - {JobTitle}/` path. Require it not to exist; do not
  create it or save the JD there.
- Complete read-only JD planning for the fresh native run. Planning cannot alter or
  waive the passing candidate-fit report.

---

## PHASE 2: NATIVE TEAM, THEN ADVISORY BASE SCORING

Invoke the mandatory native runtime first with the exact private JD, prospective
non-existing output path, and the same `run_id`, `case_id`, and `as_of_date` used
for preflight. The runtime independently recomputes candidate fit before it creates
the output directory or invokes Researcher. Require the runtime report and digest
to exactly match the independently validated preflight.

Only after an authorized `PUBLISHED` result has been verified may ATS/HR base
scoring run. Score the configured master resume, never a previous tailored resume.
These scores are advisory and needed only for the final comparison report.

**Background Task A — Combined Base Score (ATS + HR):**
```
Run in a background shell session named `base-scorer` if available:
curl -s -X POST http://localhost:8100/score/both -H "Content-Type: application/json" -d "{\"resume_path\": \"{configured_master_resume_path}\", \"jd_path\": \"applications/{folder}/job_description.txt\"}"
```
**Fallback** (if server not running): Run `python ats_scorer.py --score ... --json` and `python hr_scorer.py --score ... --json` against the configured master and exact fixed JD as separate shell scoring commands.

**NATIVE RESUME TEAM — Invoke the mandatory runtime above.** It atomically writes the authorized `resume.md`; do not write or save the role output again. Independently recompute the published file's SHA-256 and require it to equal `final_draft_digest`. Record this as draft-stage authorization only, not package completion.

The runtime has already published `resume.md`; never save, rewrite, normalize, or
otherwise alter it.

**CRITICAL .md FORMATTING RULE:** Do NOT use `**` (markdown bold asterisks) anywhere in resume.md files. Write metrics and text as plain text (e.g., "11,300+ ICU stays" not "**11,300+ ICU stays**"). The DOCX generator handles bold formatting automatically — asterisks in .md files cause display issues.

---

## PHASE 3: PARALLEL TAILORED SCORING (launch both simultaneously)

Once the runtime-published `resume.md` and its receipt are verified, run tailored scoring in a background shell session:

**Background Task C — Combined Tailored Score (ATS + HR):**
```
Run in a background shell session named `tailored-scorer` if available:
curl -s -X POST http://localhost:8100/score/both -H "Content-Type: application/json" -d "{\"resume_path\": \"applications/{folder}/resume.md\", \"jd_path\": \"applications/{folder}/job_description.txt\"}"
```
**Fallback** (if server not running): Run the CLI scorers as shell commands.

---

## PHASE 4: ADVISORY SCORE REVIEW (no post-authorization editing)

1. **Collect scores** from the tailored scoring task and record them for reporting.
2. Scores are advisory. They may decide whether to accept or reject the candidate,
   but they cannot authorize any edit to the saved draft.
3. If ATS or HR is below target, either accept the fully authorized candidate and
   report the result honestly, or discard it and start a complete native-team run
   with a fresh `run_id`. A retry begins at Researcher and ends with a new Auditor.
4. Never patch `resume.md`, call Writer or Editor alone, reuse a prior handoff, or
   carry forward a verdict or vote. Limit retries to two complete fresh runs; if
   targets remain unmet, accept a fully authorized candidate or stop without DOCX.

---

## PHASE 4.5: EVIDENCE + HUMAN VOICE AUDITS (mandatory before DOCX)

First require that the current `resume.md` SHA-256 still equals the candidate
digest and that the final Auditor returned `PASS` for exactly that digest. Treat
the three commands below as independent finalization rechecks, not editing tools:
```bash
python evidence_audit.py "applications/{folder}/resume.md"
python human_voice_audit.py "applications/{folder}/resume.md"
python resume_integrity_audit.py --config config.json --tailored "applications/{folder}/resume.md"
```

- Recompute SHA-256 immediately before and after each command, and record that
  command's exit code only against the observed unchanged candidate digest.
- Proceed only if the final Auditor passed, all three commands exited 0, and the
  digest remained unchanged across all four decisions.
- If any vote fails or the digest changes, reject the run. Do not repair the file
  from audit output. Either stop or repeat the full team under a fresh `run_id`.
- Do not create DOCX, update the tracker, clean up, or report success while a vote
  is failed, missing, or stale. Lexicon: `data/ai_tells.json`.

---

## PHASE 5: ORDERED FINALIZATION

Immediately before DOCX, reread and revalidate the durable authorization sidecar
against the runtime result and `resume.md`, then recompute `resume.md` SHA-256 one
last time and require it
to equal the digest shared by the final Auditor `PASS` and all three exit-0 votes.
No process may alter `resume.md` after this check. Create and verify the resume DOCX
first and let generator exceptions propagate. Only after that command succeeds may
the tracker run; cleanup remains forbidden until the tracker and DOCX are verified.

**Task E — Authorized resume DOCX (from markdown):**
```
cd "." && python -c "
from pathlib import Path
from docx_generator import create_resume_from_md_authorized
from final_receipt_verifier import verify_final_receipt
app_dir = Path('applications/{folder}')
resume_path = app_dir / 'resume.md'
raw_receipt = Path('{authorization_receipt_path from runtime result}')
receipt_path = raw_receipt if raw_receipt.is_absolute() else app_dir / raw_receipt
receipt_digest = '{authorization_receipt_digest from runtime result}'
output_path = app_dir / '{Name}_Resume_{Company}.docx'
verify_final_receipt(resume_path=resume_path, receipt_path=receipt_path, expected_receipt_digest=receipt_digest)
create_resume_from_md_authorized(str(resume_path), str(output_path), receipt_path=str(receipt_path), expected_receipt_digest=receipt_digest, config_path='config.json')
if output_path.is_symlink() or not output_path.is_file() or output_path.stat().st_size == 0:
    raise RuntimeError('AUTHORIZED_RESUME_DOCX_NOT_VERIFIED')
print('Resume DOCX created successfully')
"
```

**Task F — Update Tracker (only after Task E succeeds):**
```
cd "." && python -c "
from pathlib import Path
from final_receipt_verifier import verify_final_receipt
from tracker_utils import TrackerUpdateError, add_application_authorized
app_dir = Path('applications/{folder}')
resume_path = app_dir / 'resume.md'
raw_receipt = Path('{authorization_receipt_path from runtime result}')
receipt_path = raw_receipt if raw_receipt.is_absolute() else app_dir / raw_receipt
receipt_digest = '{authorization_receipt_digest from runtime result}'
verify_final_receipt(resume_path=resume_path, receipt_path=receipt_path, expected_receipt_digest=receipt_digest)
updated = add_application_authorized(
    company='{Company}',
    job_title='{Job Title}',
    authorized_resume_path=str(resume_path),
    receipt_path=str(receipt_path),
    expected_receipt_digest=receipt_digest,
    resume_file='{Name}_Resume_{Company}.docx',
    cover_letter_file='',
    jd_file='job_description.txt',
    ats_score={final_ats},
    hr_score={final_hr},
    application_date=None,
    status='Applied'
)
if updated is not True:
    raise TrackerUpdateError('TRACKER_UPDATE_NOT_CONFIRMED')
print('Tracker updated successfully')
"
```

---

## PHASE 6: CLEANUP + REPORT

1. **Collect all results** (verify DOCX + tracker)
2. **Collect base scores** from the Phase 2 scoring task (for comparison)
3. **Delete `resume.md`** (AFTER DOCX creation confirms success — .md file is needed as input for DOCX creation). Never delete the durable authorization-receipt sidecar.
4. **Display final report:**

```
================================================================================
                    RESUME TAILOR - FINAL REPORT (Native Team)
================================================================================

COMPANY: {Company Name}
POSITION: {Job Title}
DOMAIN DETECTED: {clinical_research/pharma_biotech/technology/etc.}
BASE RESUME: {configured master_resume_path}

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
CONCURRENT TASKS USED: {count} | ITERATIONS: {count}
================================================================================
```

5. **Offer** web reports:
```bash
python ats_scorer.py --web --base "{configured_master_resume_path}" --tailored "applications/{folder}/resume.md" --jd "applications/{folder}/job_description.txt"
python hr_scorer.py --score "applications/{folder}/{Name}_Resume_{Company}.docx" "applications/{folder}/job_description.txt" --web
```

---

## NATIVE WRITER GUIDANCE (fresh team runs during Phase 2 only)

Only native role agents may act on this section. It never authorizes the
coordinator, a scorer, or an audit to change a saved candidate.

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

### WRITING COACH — HUMAN VOICE + IMPACT (Rules 0–16)

Full skill: `commands/writing-coach.md`. Priority: Authenticity → Human voice → HR → ATS.

- **Rule 0:** Human voice gate + `human_voice_audit.py` must pass before DOCX
- **Rules 1–4:** So-what, front-load, deadwood out, real metrics
- **Rule 5:** Plain strong verbs — ban spearheaded/leveraged/orchestrated/championed openers
- **Rule 7:** Burstiness — mix short/medium/long; mean ≤ 22 words; CV ≥ 0.30
- **Rule 9:** Plain summary (no "Results-driven…"); ≤ 3 sentences, ≤ 70 words
- **Rules 11–13:** Banned AI lexicon; no synonym-pair padding; keywords live in Core Competencies first
- **75% authentic human prose > 90% stuffed AI prose**

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

**Verbs:** Led, Built, Wrote, Cut, Reviewed, Directed, Managed, Validated, Established, Governed

**Tone:** Senior professional — authoritative and evidence-based.

**Bullet Distribution:** Current role 4-6, recent 3-4, older 2-3, very old 1-2.

---

## ETHICAL REQUIREMENTS (NON-NEGOTIABLE)

- **NEVER CHANGE JOB TITLES** — Match master resume exactly
- **NEVER CHANGE PUBLICATIONS** — Titles/citations stay as-is
- **Never invent experience** — Only reframe existing content
