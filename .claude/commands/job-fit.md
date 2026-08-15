---
description: Run the deterministic, digest-bound candidate-fit gate before any resume tailoring.
---

# Job Fit Pre-Screen — Deterministic GO/NO-GO Gate

Evaluate the configured master resume against this exact job description before
any resume work begins.

## Input
$ARGUMENTS

## CANDIDATE-FIT PREFLIGHT (MANDATORY FIRST GATE)

1. Read `config.json` and resolve its exact `master_resume_path`. This configured
   master is the only resume allowed in the assessment. Never use a previous
   tailored resume, application resume, or “best match” template.
2. Put the exact job description in a private temporary UTF-8 file. Do not create
   an application/output directory, resume draft, DOCX, or tracker row.
3. Generate one safe `run_id`, one safe `case_id`, and one strict ISO calendar
   `as_of_date`, then capture the sole intended machine output from:

   `python candidate_fit_preflight.py --resume <configured-master-resume> --job-description <private-exact-JD.txt> --run-id <run_id> --case-id <case_id> --as-of-date <YYYY-MM-DD> --json`

4. Parse only the JSON report. Require `schema_version: "1.0.0"`,
   `policy_version: "candidate-fit-policy-v3"`,
   `scorer_version: "evidence-match-v1"`, the exact run/case IDs and date,
   exact master/JD SHA-256 digests, exact `threshold: 70.0`, all seven named
   component scores, a boolean `extraction_trustworthy`, `hard_knockouts`,
   `passed`, and ordered `codes`. Recompute the canonical JSON SHA-256 and display
   it as `candidate_fit_report_digest`.
5. Proceed status is valid only when the process exits `0`, `score >= 70`,
   extraction is trustworthy, `hard_knockouts` is empty, `passed` is true, and
   `codes` is empty. No dimension, recommendation, ATS score, HR score, or user
   preference can compensate for a failed condition.
6. Exit `1`, any score below 70, or any hard knockout is
   `REJECTED:CANDIDATE_FIT`. Exit `2` or an unavailable, malformed, non-canonical,
   stale, or digest-mismatched report is `FAILED:CANDIDATE_FIT_PREFLIGHT`. Both
   fail closed and authorize no tailoring, role/native-team invocation, output,
   DOCX, or tracker operation.

## Report

Display the exact source-bound decision clearly:

```text
================================================================
  CANDIDATE FIT: XX.X/100 — [PROCEED | REJECTED | PREFLIGHT FAILED]
================================================================

  Source: configured master_resume_path
  Policy: candidate-fit-policy-v3 | Threshold: 70.0
  Master digest: [SHA-256]
  JD digest: [SHA-256]
  Report digest: [candidate_fit_report_digest]

  HARD KNOCKOUTS:
  [None, or exact category / requirement / candidate-has summary]

  COMPONENTS:
  Experience Match:     XX/100
  Skills Match:         XX/100
  Title Alignment:      XX/100
  Domain Match:         XX/100
  Education Match:      XX/100
  Certification Match:  XX/100
  Seniority Match:      XX/100

  CODES: [none | UNTRUSTWORTHY_EXTRACTION | ELIGIBILITY_FAILED |
          SCORE_BELOW_THRESHOLD]

  VERDICT:
  [Clear next action]
================================================================
```

## Fixed recommendation policy

- `70–100`, zero hard knockouts, trustworthy extraction: `PROCEED`. The same
  report and digest must be recomputed and bound into any later
  `resume-team-result/v2` and `resume-team-final-receipt/v2`.
- `60–69`: `REJECTED:CANDIDATE_FIT` — blocked for manual reconsideration. The
  system must not automatically tailor or offer a bypass. Reconsideration means
  reviewing the target or adding genuine evidence to the master resume, then
  running a fresh assessment; it is not permission to override the threshold.
- Below `60`: `REJECTED:CANDIDATE_FIT` — target a closer role or correct genuinely
  wrong source input before a fresh assessment.
- Any hard knockout: `REJECTED:CANDIDATE_FIT` regardless of score.
- Untrustworthy extraction or any invalid report:
  `FAILED:CANDIDATE_FIT_PREFLIGHT`; fix the input/tooling and rerun from scratch.

ATS and HR baseline scoring may be shown later as advisory diagnostics only after
candidate fit passes. They are not this gate, cannot raise or lower its decision,
and cannot authorize resume development.
