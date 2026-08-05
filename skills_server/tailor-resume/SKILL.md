---
name: tailor-resume
description: Tailor a resume to a job description, score it against ATS and HR rubrics, create a DOCX, and update the tracker.
---

# Tailor Resume — Role-Separated, Fail-Closed Workflow

Optimize and tailor a resume for one specific job description. Target:
75-85% ATS match and 70%+ HR score, with every change authentic and
evidence-backed.

Editorial priority (never invert): **Authenticity -> Human voice -> HR
impact -> ATS match.** A 75% ATS score with honest, human prose beats a 90%
score built on keyword-stuffed, AI-sounding prose.

## Input

A job description, and the candidate's master resume on file.

## Gate 0: Candidate Fit (mandatory, before any tailoring)

Before any resume development begins, run the deterministic candidate-fit
check against the configured master resume and this exact job description
-- never a previously tailored resume. Continue only when it reports:

- an overall score of at least 70 out of 100
- trustworthy extraction of the resume/JD content
- zero hard knockouts (a missing licensure, required clearance, or similar
  disqualifying requirement drawn only from the job description's actual
  requirements, never from duties prose)
- a clean pass with no error codes

A score below 70 (including 60-69) or any hard knockout means the role is
not a fit for this candidate as written: stop, and do not create a draft,
score, or tracker entry. An incomplete or untrustworthy check fails closed
the same way -- treat it as blocking, not as permission to proceed. There is
no override inside this workflow; only the candidate can decide to widen the
target or add real, truthful evidence to their master resume and try again.
Advisory ATS/HR scores can never substitute for this gate or raise/lower its
decision.

## Gates 1-4: The Audited Four-Role Pipeline

Once candidate fit passes, the tailored draft is produced by an
independently audited, role-separated pipeline rather than by one pass of
freeform editing:

1. **Researcher** reads only the job description and produces an ordered
   requirement rubric, hard requirements before soft ones, each backed by
   an exact, uniquely anchored quote from the job description itself. A
   requirement is never "found" in the JD's prose alone without an
   anchoring quote, and the JD's own duties/requirements are never treated
   as evidence that the candidate has a skill.
2. **Writer** works only from the master resume and the validated rubric --
   it selects and reframes real, already-documented experience; it has no
   authority to publish the draft.
3. **Auditor** independently checks the exact draft against the master
   resume and rubric and returns a straight pass/fail; the Auditor never
   edits.
4. **Editor** runs only after a failed audit, fixes only the named
   findings, and gets at most two attempts, each followed by a fresh,
   independent audit.

A draft is authorized only when the Auditor passes it AND three separate,
deterministic checks agree on the exact same draft: every claim traces to
real evidence, the prose reads as human-written (see the writing-coach
guidance), and nothing was silently changed that must never change (see
below). Once authorized, the draft is immutable -- no later step may
rewrite, reformat, or "clean up" it. Any wanted change means starting a
fresh run through this same pipeline, not hand-editing the result.

## Authenticity Rules (non-negotiable)

**Can be adapted to the job description:**
1. Professional Summary -- naturally incorporate 3-5 key JD terms
2. Core Competencies -- the primary place to match JD keywords
3. Experience bullet points -- reframe real achievements using JD language
   where it genuinely fits

**Must stay exactly as in the master resume:**
1. Job titles
2. Company names
3. Dates
4. Education (degree names and schools)
5. Publications (titles and citations -- never add keywords to these)
6. Certifications
7. Professional memberships

**Keyword rules:**
- Each keyword appears at most 1-2 times across the whole resume
- Core Competencies is the primary keyword location; do not repeat the same
  keyword in every bullet
- Never force an awkward phrase just to match a JD term the candidate's
  real experience does not support

**Core Competencies must-trace rule:** every item listed must be backed by
evidence elsewhere in the resume -- in a bullet, in the summary, in
education/certifications/publications, or carry an honest qualifier such as
`(exposure)`, `(coursework)`, `(trainable)`, or `(familiar)`. A skill listed
with no evidence and no honest qualifier is an interview liability: remove
it rather than let a recruiter ask "tell me about a time you did X" with no
answer ready.

## Resume Structure (ATS-friendly)

```
FULL NAME, CREDENTIALS
City, State ZIP | Phone | Email
LinkedIn URL

PROFESSIONAL SUMMARY
[3-4 lines, JD keywords woven in naturally]

CORE COMPETENCIES
[12-14 JD-relevant keywords/phrases]

PROFESSIONAL EXPERIENCE
EXACT TITLE | EXACT COMPANY | Location
Dates
- [Plain strong verb] [what was done], [quantified result]

EDUCATION
[Exactly as in the master resume]

CERTIFICATIONS & LICENSURE
[Exactly as in the master resume]

PUBLICATIONS
[Exactly as in the master resume -- no additions]

PROFESSIONAL MEMBERSHIPS
[Exactly as in the master resume]
```

Avoid columns, tables, text boxes, graphics, or content hidden in
headers/footers -- these confuse automated parsing. Use plain bullets and
ALL-CAPS section headers. Use a "TITLE | COMPANY | Location" line for each
role.

## STAR Bullets + Verb Bank

Formula: `[Plain strong verb] [context + action] -> [quantified result]`.

Good verbs: Led, Built, Wrote, Cut, Reviewed, Directed, Managed, Validated,
Established, Governed, Reduced, Increased, Trained, Audited, Published,
Presented, Coordinated, Developed, Improved, Resolved.

Distribute bullets by recency: current role 4-6, recent role 3-4, older
roles 2-3, very old roles 1-2. Vary bullet length (see the writing-coach
guidance's burstiness rule) rather than making every bullet the same size.

## Human Voice (hard gate, same severity as the evidence check)

Follow the writing-coach guidance's Rules 0-16 while drafting: plain strong
verbs, no banned AI lexicon, varied sentence length, a plain 2-3 sentence
summary, and keywords placed in Core Competencies first rather than forced
into every bullet. A draft that reads like AI-generated marketing copy
fails this gate regardless of its keyword match score.

## Ethical Requirements (non-negotiable)

- Never change job titles -- match the master resume exactly
- Never change publications -- titles and citations stay as-is
- Never invent experience, metrics, or credentials -- only reframe what is
  real and already documented
- A 75-85% ATS match with authentic, human prose beats a 90%+ match built
  on keyword stuffing or invented content
