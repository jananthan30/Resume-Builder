# ResumeHQ Scoring Benchmarks

Most resume tools publish marketing claims about their scoring. This page
publishes measurements — including the failures — because ResumeHQ's engine is
open source and auditable, and we think a scorer you can't inspect is a scorer
you can't trust.

Updated with every scoring release. Current: **v1.2.0** (2026-08-10).

## Methodology

- **Corpus:** 61 real job postings collected through actual job-search use
  (heavier on clinical research / pharmacovigilance, with technology and
  adjacent roles), each scored against the same fixed master resume.
- The corpus itself is private — it is a real person's application history —
  but every aggregate below is reproducible in spirit against your own resume
  and postings: `pip install resumehq` and run the same functions.
- All engines measured here are **deterministic** (no LLM in the loop): the
  candidate-fit gate, the HR recruiter simulation, and the DOCX generator.
- Every defect listed carries a regression test in the repo; fixes are never
  claimed without one.

## Candidate-fit gate (v1.2.0)

The fit gate decides whether resume tailoring should proceed at all. All 61
postings are jobs the candidate genuinely applied to, so hard rejections are
presumptively false.

| Measure | Pre-fix (measured, v1.1 era) | Today (v1.2.0) |
|---|---|---|
| JDs flagged "untrustworthy extraction" from heading wording alone | 31/61 | fixed; guarded by regression tests |
| JDs producing a junk title or 0.0 alignment | 36/61 | fixed; guarded by regression tests |
| Wrongful hard knockouts (auto-reject) | 2/61 | **0/61** |
| Score distribution vs the 70 advisory bar | — | median 68 · p10 35 · p90 77; 8 STRONG FIT / 36 MODERATE / 1 WEAK / 16 NO-GO |

Calibration note: scores in the 60–69 band are advisory by design (the
stretch-zone review path exists for them); a "MODERATE FIT" on a stretch
application is intended behavior, not an error. The pre-fix numbers are kept
in test docstrings (`tests/test_candidate_fit_false_rejections.py`) as the
permanent record of what was wrong.

## HR skills factor: the JD-format sensitivity bug (fixed in v1.2.0)

The most instructive bug we've shipped. The same resume against the same
requirements, phrased two ways:

| JD requirement style | Before v1.2.0 | v1.2.0 |
|---|---|---|
| Fragment bullets ("Protocol compliance, GCP, ICH") | skills factor **0.0** — compound lines were matched verbatim and never hit | **41.7** — lines atomized into individual skills |
| Sentence bullets ("Working knowledge of GCP guidelines…") | **80.0 free pass** — long lines were silently filtered out, leaving nothing to match | real matching against extracted atoms |

A ±40-point swing based purely on how the *job description* was formatted.
Root cause and fix: `hr_scorer.py` (`_atomize_requirement`), commit `2b7b76c`.

Corpus-wide today (61 JDs, fixed engine): zero-rate **8/61**, median skills
factor **23.5**, p10 0.0, p90 44.7.

**Known open gap (next target):** 42/61 JDs yield no structured requirements
from bullet scanning (their requirement text lives in prose paragraphs), so
scoring falls back to keyword extraction. That fallback works but is coarser —
tracked as the current top parsing weakness.

## DOCX generation integrity

Two silent-content-loss bugs found and fixed: two-part role headers
(`TITLE | COMPANY`) and numbered publication lists were dropped from generated
DOCX files without error. Both are covered by parse-layer regression checks;
generated output is verified against source markdown in the finalization
gates.

## What we don't claim

- No "3× more interviews" numbers. We have no controlled outcome data yet;
  when the application tracker accumulates enough recorded outcomes, that
  analysis will appear here with its confidence intervals, not before.
- The corpus skews clinical/pharma; scores on other domains rely on the
  domain-detection layer and are less battle-tested.
- One master resume, one candidate. This measures parsing and scoring
  consistency, not universal accuracy.

## Reproduce

```bash
pip install resumehq
python - <<'PY'
import hr_scorer as h
jd = open("your_job_description.txt").read()
resume = open("your_resume.txt").read()
cand = h.parse_resume(resume)
req = h.parse_job_description(jd)
score, matched, missing = h.score_skills_contextual(
    cand.skills, cand.all_bullets, req.required_skills, jd)
print(score, matched, missing)
PY
```

Found a JD that scores absurdly? [Open an issue](https://github.com/jananthan30/Resume-Builder/issues)
with the JD text — format-sensitivity reports are how the fragment-bullet bug
was found, and corpus contributions make the next row of this table.
