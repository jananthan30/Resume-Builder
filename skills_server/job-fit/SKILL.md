---
name: job-fit
description: Run the deterministic, digest-bound candidate-fit gate before any resume tailoring.
---

# Job Fit Pre-Screen — Deterministic GO/NO-GO Gate

Evaluate the candidate's master resume against one exact job description
before any resume tailoring work begins. This is a fixed, deterministic
check -- it does not get more lenient because a role sounds appealing, and
it cannot be talked around.

## Input

A job description, and the candidate's master resume on file. The master
resume is the only resume ever used for this check -- never a previously
tailored resume, and never a "best match" template.

## What the Check Reports

- Seven named component scores (0-100 each): Experience Match, Skills
  Match, Title Alignment, Domain Match, Education Match, Certification
  Match, and Seniority Match
- Whether the resume/JD extraction was trustworthy enough to trust the
  scores at all
- Any hard knockouts -- disqualifying requirements the candidate does not
  meet (e.g. a required license, clearance, or years of directly relevant
  experience) drawn only from the job description's actual requirements
  section, never from duties prose
- An overall score and a pass/fail recommendation

## Recommendation Policy (fixed, not adjustable per role)

- **70-100, zero hard knockouts, trustworthy extraction:** proceed with
  tailoring.
- **60-69:** rejected for this exact job description -- not a bypass.
  Reconsideration means either targeting a closer role or adding genuine,
  truthful evidence to the master resume, then re-running the check from
  scratch; it is never permission to override the threshold on this same
  result.
- **Below 60:** rejected -- target a closer role, or correct genuinely
  wrong source input, before trying again.
- **Any hard knockout:** rejected regardless of the overall score.
- **Untrustworthy extraction, or any incomplete/invalid result:** treat as
  blocking, the same as a rejection -- fix the input and re-run rather than
  proceeding on an uncertain read.

No dimension score, ATS estimate, HR estimate, or personal preference can
compensate for a failed condition above. Advisory ATS/HR baseline scores
may be shown later for context only, after this gate passes -- they are
never part of this decision and can never raise or lower it.

## Reporting the Result

State the outcome plainly: the overall score, the pass/fail decision, each
of the seven component scores, and any hard knockouts by name. If rejected,
say clearly what would need to change (a closer-fitting role, or specific
real evidence the master resume is missing) rather than implying the
decision is negotiable.
