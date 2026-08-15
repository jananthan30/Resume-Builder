---
description: Generate a tailored resume and cover letter from a job description, score both, create DOCX files, and update the tracker.
---

# Resume Builder — Native Four-Role Team + Triple Scoring

Generate a tailored resume AND cover letter using Codex parallel tool execution with scoring-aware optimization. Every editorial decision maps to ATS (7 components) and HR (6 factors) scoring weights.

## Job Description
$ARGUMENTS

## CANDIDATE-FIT PREFLIGHT (MANDATORY FIRST GATE)

Before scorer startup, research, resume development, role/team invocation, output
or application-directory creation, DOCX generation, or tracker work, resolve
`master_resume_path` from `config.json` and save this exact job description to a
private temporary UTF-8 file. Screen only that configured master resume—never a
previously tailored resume. Generate one safe `run_id`, one safe `case_id`, and one
strict ISO `as_of_date`, then run:

`python candidate_fit_preflight.py --resume <configured-master-resume> --job-description <private-exact-JD.txt> --run-id <run_id> --case-id <case_id> --as-of-date <YYYY-MM-DD> --json`

Require exit `0` plus a valid `candidate-fit-policy-v3` report bound to the same
IDs, date, master SHA-256, and exact-JD SHA-256. Canonically hash it as
`candidate_fit_report_digest`. Proceed only when `threshold` is exactly `70.0`,
`score >= 70`, `extraction_trustworthy` is true, `hard_knockouts` is empty,
`passed` is true, and `codes` is empty. Exit `1`, a score below 70 (including
60–69), or any hard knockout is `REJECTED:CANDIDATE_FIT`; stop with no team/role
invocation and no application directory, draft, DOCX, or tracker mutation. Exit
`2` or an unavailable, malformed, stale, non-canonical, or digest-mismatched
report is `FAILED:CANDIDATE_FIT_PREFLIGHT` and fails closed. There is no automatic
or manual workflow bypass. ATS/HR baselines remain advisory and cannot override
candidate fit.

## Instructions

You are the coordinator, not the resume author. The user has provided a job description above. You will:
1. Pass the fixed candidate-fit gate against the configured master resume
2. Internalize both advisory scoring engines before writing a single word
3. Deconstruct the JD into a scoring blueprint
4. Delegate drafting to the native `resume-writer` with least-authority context
5. Diagnose gaps by component weight, not guesswork
6. Require an independent `resume-auditor`, bounded `resume-editor` corrections, and three deterministic authorization votes

---

## GLOBAL CONSTRAINTS (read first, enforce always)

**Editorial priority (never invert):** Authenticity → Human voice → HR impact → evidence coverage.

- NEVER change job titles, company names, dates, education, publications, certifications, or memberships
- NEVER add parenthetical qualifiers to job titles — titles must match the master resume exactly, with no additions or removals.
- NEVER use `**bold**` markdown in `.md` files — the DOCX generator handles bold automatically
- NEVER exceed 2 appearances of any single keyword across the entire resume
- NEVER keyword-stuff experience bullets — Core Competencies is the primary ATS keyword home; bullets stay factual and human
- NEVER open bullets with AI-cliché verbs (spearheaded, leveraged, orchestrated, championed, …) — use plain strong verbs (Led, Built, Wrote, Cut, Reviewed)
- Publications & Education: Keep EXACTLY as in master resume — zero modifications
- DOCX and tracker finalization must use only the receipt-validating authorized wrappers specified in Phase 5; never call lower-level generators or tracker mutations directly
- Acceptance targets: **Evidence Match ≥ 75%**, **zero must-haves with NO_EVIDENCE_FOUND**, eligibility not FAIL, **Human Voice audit pass**. Evidence Match is the authoritative measure — it reports which of the job's requirements the resume actually evidences, with the exact excerpt behind each.
- Legacy ATS/HR scores remain available for continuity but are diagnostic only. No universal ATS score exists across recruiting systems, so never present one as the result or retry a run to chase it.
- Report gaps as "no evidence found in the resume", never as "the candidate lacks this". Absence of evidence is not proof of absence.
- A 75% evidence match with human prose beats a 90% keyword-stuffed draft — stuffing raises keyword counts and lowers evidence quality.

---

## NATIVE RESUME TEAM (mandatory control plane)

Only after the candidate-fit preflight passes, read `commands/resume-team.md`, run
its host preflight, and invoke `native_resume_team.py` for the current host with the
same `run_id`, `case_id`, `as_of_date`, exact private JD file, and a prospective
non-existing output path. Do not create the path first. Do not manually
reproduce the role sequence for a publishable draft. The runtime enforces:

1. `resume-researcher` receives only the job description.
2. `resume-writer` receives only the master resume and validated Researcher artifact.
3. `resume-auditor` independently audits the exact Writer draft.
4. `resume-editor` is called only after `FAIL`, corrects only named findings, and is followed by a fresh Auditor review. Maximum two corrections.

The coordinator must validate every `resume-team-handoff/v1` digest and lineage,
require distinct role identities, and fail closed on malformed, stale, replayed,
ambiguous, unavailable, timed-out, or side-effecting results. The coordinator must
not write or silently repair the resume itself. Require macOS or Linux; Windows
preflight fails closed with `POSIX_RUNTIME_REQUIRED`.

For Codex only, omit model flags by default because the hardened subprocess ignores
user configuration and transient parent-session settings; do not claim an inherited
profile, model, or Ultra setting. Add `--model <exact-model>` and/or
`--reasoning-effort ultra` only when the user explicitly requests those pins. There
is no runtime profile option, and Claude must not receive Codex-only pins.

Accept the `resume-team-result/v2` runtime result only on exit `0`,
`terminal_class: PUBLISHED`, and an
independent SHA-256 match between `final_draft_digest` and the published
`resume.md`. Require its `candidate_fit_report` and
`candidate_fit_report_digest` to exactly match the independently validated
preflight. `PUBLISHED` means only an authorized, digest-verified Markdown
draft-stage artifact. It is not a completed package and must not be reported as
one. This command must still complete cover-letter generation, resume DOCX,
cover-letter DOCX, tracker update, artifact/state verification, cleanup, and its
final report before reporting package completion.

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
digest, and match both receipt fields. Require matching
run/case IDs; require its candidate-fit report and digest to match the runtime and
independent preflight; and bind `draft_digest` and `verified_target_digest` to
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

Authorization is byte-specific and immutable. After the runtime publishes `resume.md`, neither
the coordinator nor any scoring/audit step may edit, rewrite, normalize, format, or
otherwise change it. Any desired resume change invalidates the authorization and
requires an entirely new `resume-team/v2` run with a fresh `run_id`; do not reuse
agents, handoffs, verdicts, votes, or digests from the prior run. All writing and
score-improvement guidance later in this command is input guidance for a fresh
native-team run only, never permission for coordinator-authored edits.

Immediately before DOCX creation, all three independent votes must exit 0 on the
same final draft digest: `evidence_audit.py`, `human_voice_audit.py`, and
`resume_integrity_audit.py`. ATS/HR/LLM scores are advisory and cannot override a
failed role or deterministic gate.

---

## PHASE 0: SCORER SERVER PRE-FLIGHT

Check if the scorer server is running:
```
curl -s http://localhost:8100/health
```

- If server responds with `{"status":"ok",...}`: Proceed immediately (scoring calls will take <2s each).
- If server NOT running: Start it in background:
```
Start this as a background shell session from the repo root:
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

## PHASE 1: READ-ONLY MASTER/JD PLANNING

The candidate-fit gate is already complete. Keep the configured master resume as
the sole base and factual source; do not search for or use a prior tailored resume.

- Read the configured master for canonical job titles, dates, company names,
  education, certifications, publications, and memberships.
- Extract company and role only to derive a prospective sanitized
  `applications/{CompanyName} - {JobTitle}/` path. Require the path not to exist;
  do not create it or save state there.
- Complete the JD Deconstruction in STEP 1 below as planning input for a fresh
  native run. It cannot alter or waive the already passing candidate-fit report.

---

## PHASE 2: NATIVE TEAM, THEN ADVISORY BASE SCORING

Invoke the mandatory native runtime first with the exact private JD, prospective
non-existing output path, and the same `run_id`, `case_id`, and `as_of_date` used
by the candidate-fit preflight. The runtime independently recomputes that report
before it creates the output directory or invokes Researcher. Require its report
and digest to exactly match the independently validated preflight.

After an authorized `PUBLISHED` result has been fully verified, initialize shared
state using the configured master resume—not a prior tailored resume:

```
cd "." && python -c "
from orchestration_state import init_state
init_state('applications/{folder}', '{Company}', '{JobTitle}', 'applications/{folder}/job_description.txt', '{configured_master_resume_path}')
print('State initialized')
"
```

ATS and HR base scores are needed only for the final comparison report. They are
advisory and cannot override candidate fit or authorize writing.

Background Task A — Combined Base Score (ATS + HR) -> writes to state.json:
```
Run in a background shell session named `base-scorer` if available:
cd "." && python -c "
from orchestration_state import write_score_results, set_phase, log_error
import subprocess, json
set_phase('applications/{folder}', 'scoring_base')
try:
    result = subprocess.run(
        ['curl', '-s', '-X', 'POST', 'http://localhost:8100/score/both',
         '-H', 'Content-Type: application/json',
         '-d', json.dumps({'resume_path': '{configured_master_resume_path}',
                           'jd_path': 'applications/{folder}/job_description.txt'})],
        capture_output=True, text=True, timeout=120)
    write_score_results('applications/{folder}', 'base_both', result.stdout)
    print('Base scores written to state.json')
except Exception as e:
    log_error('applications/{folder}', 'scoring_base', str(e))
    print(f'Error: {e}')
"
```
Fallback (if server not running): Run `python ats_scorer.py --score ... --json` and `python hr_scorer.py --score ... --json` against the configured master and exact fixed JD as separate shell scoring commands, piping output through `write_score_results('applications/{folder}', 'base_ats'|'base_hr', result)`.

NATIVE RESUME TEAM — The mandatory runtime above atomically writes the authorized `resume.md`; the coordinator must not write or save the role output again. Independently recompute the published file's SHA-256 and require it to equal `final_draft_digest`. Any later byte change invalidates this run. Record this as draft-stage authorization only, not package completion.

The runtime has already published `resume.md`; never save or rewrite it. Only after
the runtime result, resume digest, and durable receipt have all been independently
verified may the coordinator record that existing path and advance state:
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

Once the runtime-published `resume.md` and its receipt are verified, run tailored scoring and cover-letter writing concurrently where possible:

Background Task C — Combined Tailored Score (ATS + HR + LLM) -> writes to state.json:
```
Run in a background shell session named `tailored-scorer` if available:
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
NOTE: `/score/combined` runs all 3 scorers (ATS rules + HR rules + LLM rubric scorer) and returns blended scores (70% rules + 30% LLM). If LLM fails (no API key, timeout), it gracefully falls back to rules-only.
Fallback (if server not running): Use CLI scorers + llm_scorer.py directly.

Background Task E — Cover Letter:
```
Draft the cover letter in parallel with scoring if possible:
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

## PHASE 4: ADVISORY SCORE REVIEW (no post-authorization editing)

1. Collect all three scores from state.json (single read replaces polling multiple task outputs):
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
print(f'--- LLM rubric scorer ---')
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
Use the COMBINED scores only for reporting and for deciding whether to accept the
already-authorized draft. Scoring is advisory: it never authorizes a resume change,
and a low score is not a failed safety vote.

2. If ATS or HR is below target, record the component-level diagnosis without
changing `resume.md`. The coordinator has only two allowed choices:
   - accept the authorized draft and report the advisory score honestly; or
   - discard this candidate and start a complete new native-team run with a fresh
     `run_id`, beginning again at Researcher and ending with a new Auditor verdict.

3. A fresh run may use the scoring diagnosis only as planning context outside the
signed handoffs. It must still obey the scoped payloads in `commands/resume-team.md`.
Never patch the old draft, invoke Writer or Editor alone, reuse a handoff, or carry
forward an Auditor verdict or deterministic vote. Bound the workflow to three
complete team runs; if targets remain unmet, accept the best fully authorized
candidate or report failure without publishing.

---

## PHASE 4.5: EVIDENCE + HUMAN VOICE AUDITS (mandatory before DOCX)

The following are authorization votes, not editing tools. First confirm the current
`resume.md` SHA-256 still equals the candidate digest and that the final Auditor
returned `PASS` for exactly that digest. Then run all three independent commands:

```bash
python evidence_audit.py "applications/{folder}/resume.md"
python human_voice_audit.py "applications/{folder}/resume.md"
python resume_integrity_audit.py --config config.json --tailored "applications/{folder}/resume.md"
```

- Recompute SHA-256 immediately before and after each command, and record that
  command's exit code only against the observed unchanged candidate digest.
- Authorization exists only when the final Auditor verdict is `PASS`, all three
  commands exit 0, and all four decisions refer to the same unchanged digest.
- If a vote fails or the digest changes, reject this run. Do not fix the file from
  audit output. Either stop or begin a complete fresh team run with a new `run_id`.
- Run `python human_voice_audit.py "applications/{folder}/cover_letter.md" --mode cover_letter`
  separately. Cover-letter correction may change only `cover_letter.md`; it must
  never change the authorized resume.
- Do not generate DOCX while any required vote is missing, failed, or digest-stale.

Shared lexicon: `data/ai_tells.json`. Examples: `references/human_voice_examples.md`.

---

## PHASE 5: ORDERED FINALIZATION

Immediately before DOCX, reread and revalidate the durable authorization sidecar
against the runtime result and `resume.md`, then recompute `resume.md` SHA-256 one
last time. Proceed only
if it equals the digest shared by the final Auditor `PASS` and all three exit-0
votes. No process may modify `resume.md` after this check. Resume DOCX creation
must complete first, and its exceptions must propagate and stop the workflow.
Only after the verified resume exists may the phase become `finalizing`; then
create the cover-letter DOCX, and only after both DOCX files succeed may the
tracker run. Tracker updates and cleanup are forbidden if authorization is absent,
failed, or stale.

Task F — Authorized resume DOCX -> update state only after verified success:
```
cd "." && python -c "
from pathlib import Path
from docx_generator import create_resume_from_md_authorized
from final_receipt_verifier import verify_final_receipt
from orchestration_state import set_phase, update_state
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
update_state(str(app_dir), 'docx_resume_path', str(output_path))
set_phase(str(app_dir), 'finalizing')
print('Resume DOCX created successfully')
"
```

Task G — Authorized cover-letter DOCX (run only after Task F succeeds) -> update state only after verified success:
```
cd "." && python -c "
from pathlib import Path
from docx_generator import create_cover_letter_from_md_authorized
from final_receipt_verifier import verify_final_receipt
from orchestration_state import update_state
app_dir = Path('applications/{folder}')
resume_path = app_dir / 'resume.md'
raw_receipt = Path('{authorization_receipt_path from runtime result}')
receipt_path = raw_receipt if raw_receipt.is_absolute() else app_dir / raw_receipt
receipt_digest = '{authorization_receipt_digest from runtime result}'
output_path = app_dir / '{Name}_Cover_Letter_{Company}.docx'
verify_final_receipt(resume_path=resume_path, receipt_path=receipt_path, expected_receipt_digest=receipt_digest)
create_cover_letter_from_md_authorized(str(app_dir / 'cover_letter.md'), str(output_path), authorized_resume_path=str(resume_path), receipt_path=str(receipt_path), expected_receipt_digest=receipt_digest, job_title='{Job Title}', company='{Company}')
if output_path.is_symlink() or not output_path.is_file() or output_path.stat().st_size == 0:
    raise RuntimeError('AUTHORIZED_COVER_LETTER_DOCX_NOT_VERIFIED')
update_state(str(app_dir), 'docx_cover_letter_path', str(output_path))
print('Cover Letter DOCX created successfully')
"
```

Task H — Authorized tracker update (run only after Tasks F and G succeed) -> mark state only after literal `True`:
```
cd "." && python -c "
from pathlib import Path
from final_receipt_verifier import verify_final_receipt
from orchestration_state import update_state
from tracker_utils import TrackerUpdateError, add_application_authorized
app_dir = Path('applications/{folder}')
resume_path = app_dir / 'resume.md'
raw_receipt = Path('{authorization_receipt_path from runtime result}')
receipt_path = raw_receipt if raw_receipt.is_absolute() else app_dir / raw_receipt
receipt_digest = '{authorization_receipt_digest from runtime result}'
verify_final_receipt(resume_path=resume_path, receipt_path=receipt_path, expected_receipt_digest=receipt_digest)
updated = add_application_authorized(company='{Company}', job_title='{Job Title}', authorized_resume_path=str(resume_path), receipt_path=str(receipt_path), expected_receipt_digest=receipt_digest, resume_file='{Name}_Resume_{Company}.docx', cover_letter_file='{Name}_Cover_Letter_{Company}.docx', jd_file='job_description.txt', ats_score={final_ats}, hr_score={final_hr}, application_date=None, status='Applied')
if updated is not True:
    raise TrackerUpdateError('TRACKER_UPDATE_NOT_CONFIRMED')
update_state(str(app_dir), 'tracker_updated', True)
print('Tracker updated successfully')
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
required = ('docx_resume_path', 'docx_cover_letter_path')
missing = [key for key in required if not state.get(key)]
errors = state.get('errors', [])
if missing or errors or not state.get('tracker_updated'):
    raise SystemExit(f'Finalization incomplete: missing={missing}, errors={len(errors)}, tracker={state.get("tracker_updated", False)}')
set_phase('applications/{folder}', 'done')
print(json.dumps(state, indent=2))
"
```
2. Extract base_scores and tailored_scores from the state dict for the comparison report
3. Delete intermediate files: `resume.md`, `cover_letter.md`, and `state.json` (AFTER verifying DOCX paths exist in state). Never delete the durable authorization-receipt sidecar.
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
          RESUME BUILDER - FINAL REPORT (Native Team + Triple Scoring)
================================================================================

COMPANY: {Company Name}
POSITION: {Job Title}
DOMAIN DETECTED: {clinical_research/pharma_biotech/technology/etc.}
BASE RESUME: {configured master_resume_path}

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
SCORERS: 3 (ATS Rules + HR Rules + LLM rubric scorer)
CONCURRENT TASKS USED: {count} | ITERATIONS: {count}
================================================================================
```

5. Offer to open web comparison reports:
```bash
python ats_scorer.py --web --base "{configured_master_resume_path}" --tailored "applications/{folder}/resume.md" --jd "applications/{folder}/job_description.txt"
python hr_scorer.py --score "applications/{folder}/{Name}_Resume_{Company}.docx" "applications/{folder}/job_description.txt" --web
```

---

## NATIVE WRITER GUIDANCE (fresh team runs during Phase 2 only)

Only native role agents may act on this section. It never authorizes the
coordinator, a scorer, or an audit to change a saved candidate.

### STEP 0 — INTERNALIZE THE SCORING ENGINE

Read both scoring tables before starting the native team. Every section the Writer
proposes maps to weighted components; the guidance must remain subordinate to the
role scopes, evidence contract, and Auditor.

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

The Researcher extracts the items below into its scoped rubric for a fresh run.

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
- If YES: ATS ceiling is ~69-73%. Set expectations. Do not chase 75%+ when all domain component weights are maxed; at most two complete fresh team retries.
- If NO: Standard 75-85% ATS target applies.

---

### STEP 2 — SECTION-BY-SECTION OPTIMIZATION

These constraints guide the Writer's proposal inside a fresh authorized run.

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
| L4 | Transformative | Built, Created, Secured, Cut, Recovered | Use only where the master resume proves the result |
| L3 | Directive | Led, Directed, Established, Governed, Validated | Primary verb level (40-50% of bullets) |
| L2 | Managerial | Managed, Oversaw, Coordinated, Supervised, Reviewed | Supporting bullets (20-30%) |
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

### WRITING COACH — HUMAN VOICE + IMPACT (Rules 0–16)

Full skill: `commands/writing-coach.md`. Rule 0 (human voice) overrides all other writing rules.

- Rule 0: Human voice gate — out-loud test + `human_voice_audit.py` must pass
- Rule 1–2: So-what + front-load value
- Rule 3: Deadwood out; do NOT replace with leverage/spearhead/etc.
- Rule 4: 50%+ bullets with real metrics (plain text, no ** bold)
- Rule 5: Plain strong verbs (Led, Built, Wrote, Cut, Reviewed) — ban AI-cliché openers
- Rule 6: Flexible structures; allow punch fragments; avoid identical skeletons
- Rule 7: Burstiness — mix 6–12 / 13–20 / 21–28 word bullets; mean ≤ 22; CV ≥ 0.30
- Rule 8: Light parallel structure (tense/grammar), not metronome length
- Rule 9: Summary = plain identity + one proof + optional differentiator (≤ 3 sentences, ≤ 70 words). Never "Results-driven…"
- Rule 10: Interview test
- Rules 11–16: Banned AI lexicon, no synonym-pair padding, keyword hierarchy (Competencies → Summary → bullets), brevity caps, machine audit, out-loud test

Tone: Confident human professional — specific, calm, brief. Not junior fluff and not corporate AI poetry.

---

## QUICK REFERENCE — SCORING CHEAT SHEET

Possible Writer emphasis for a complete fresh run:

| Advisory score gap | Fresh-run Writer emphasis | Weight Moved |
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
