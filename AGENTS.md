# Resume Builder - Codex Context

## Overview

Resume Builder creates tailored resumes, cover letters, job-fit reports, DOCX application packages, and tracker updates from job descriptions.

Core targets:
- Authenticity: never invent experience, metrics, titles, dates, education, publications, certifications, or memberships
- **Human voice**: brevity, burstiness, plain verbs — must pass `human_voice_audit.py` before DOCX
- ATS score: 75-85% (keywords in Core Competencies first; never stuff bullets)
- HR score: 70%+
- Output: ATS/Workday-compatible DOCX files

**Editorial priority (never invert):** Authenticity → Human voice → HR impact → ATS match.
75% ATS with human prose beats 90% stuffed AI prose.

## Codex Plugin Surface

Codex uses:
- `.codex-plugin/plugin.json` for plugin metadata
- `.agents/plugins/marketplace.json` for the local marketplace entry
- `skills/resume-team/` for the installed `$resume-team` workflow
- `.codex/agents/` for project-scoped custom role definitions
- `.codex.mcp.json` for the Codex MCP scorer server

Installed Codex invocation:
- `$resume-team [job description]`

The Markdown files in `commands/` are the shared workflow source and Claude Code
slash-command surface; they are not installed Codex `/resume-builder:*`
commands. Resume Team execution requires Python 3.10+, `config.json`, the
configured master resume, and the local deterministic audit helpers.

## Native Four-Role Resume Team

- Candidate fit is the mandatory first gate. Before any role/native-team invocation,
  output/application directory, resume development, DOCX, or tracker work, run
  `candidate_fit_preflight.py` against the exact JD and only the configured
  `master_resume_path` (never a previous tailored resume). Continue only with a
  canonical, digest-bound `candidate-fit-policy-v2` report scoring at least 70,
  trustworthy extraction, zero hard knockouts, `passed: true`, and no codes. A
  lower score (including 60–69) or hard knockout is
  `REJECTED:CANDIDATE_FIT`; unavailable, malformed, stale, or mismatched analysis
  is `FAILED:CANDIDATE_FIT_PREFLIGHT`. Both fail closed with no bypass. ATS/HR
  baseline scores are advisory and cannot override candidate fit.
- Candidate-fit policy v2 calibration: required-tool knockouts are grounded only
  in requirements/qualifications sections (a tool named in duties prose, e.g.
  "data mining in Empirica Signal," is never a knockout); doctorates named
  anywhere in requirement ladders are alternative degree routes (an MD satisfies
  "PharmD/PhD or equivalent" listings); hard knockouts degrade the score with an
  informative cap (1 → 55, 2 → 45, 3+ → 35) instead of flattening to 35.
- Stretch-zone review layer: when the deterministic floor passes (trustworthy
  extraction, zero hard knockouts) but the score is below 70, the coordinator may
  run `python candidate_fit_review.py --resume <master> --job-description <jd>
  --run-id <id> --case-id <id> --as-of-date <date> --json`. It invokes two
  structurally distinct native reviewers (skeptical-recruiter and
  hiring-manager lenses), requires every claim to cite exact resume/JD lines,
  verifies each citation in code, and passes only when both reviewers return
  PROCEED with fully verified citations (`candidate-fit-review/v1`,
  `REVIEW_CERTIFIED_PASS`). Disagreement, malformed output, unverifiable
  citations, or reviewer unavailability fail closed. A review-certified pass
  authorizes coordinator-built manual packages only; the hardened
  `native_resume_team.py` runtime still requires the deterministic
  `candidate-fit-policy-v2` pass (`passed: true`, score ≥ 70) and does not
  consume review reports.
- Manual user override: the final call belongs to the user. After any gate
  rejection (deterministic or review), the user may record a deliberate
  override with `python candidate_fit_override.py --resume <master>
  --job-description <jd> --run-id <id> --case-id <id> --as-of-date <date>
  --reason "<explicit reason>" [--review-report <path>] --output <path>`. The
  override requires a rejected gate and an explicit reason, binds the exact
  gate/review reports and digests it overrules (`candidate-fit-override/v1`,
  decision `PROCEED_MANUAL`), and prints its warning. It authorizes only the
  coordinator-built manual package path — truthful master-sourced content,
  evidence/human-voice/integrity audits, non-authorized renderers, files
  labeled `MANUAL_DRAFT.txt` — and never the native runtime, authorization
  receipts, authorized DOCX/tracker wrappers, or any claim that candidate fit
  passed. Overriding a passing gate errors; a missing reason fails closed.
- The root session is the sole coordinator. For an authorized draft, `$resume-team` must use `python native_resume_team.py --host codex`; do not replace it with manual prompt-spawn orchestration. The hardened runtime requires macOS or Linux and fails Windows preflight closed with `POSIX_RUNTIME_REQUIRED`. The project agents `resume-researcher`, `resume-writer`, `resume-auditor`, and `resume-editor` remain available for interactive inspection.
- Run Researcher → Writer → Auditor in order. Call Editor only after `FAIL`, then run a fresh Auditor. Maximum two corrections.
- Role agents return proposals only. They never write files, spawn roles, publish, update the tracker, clean up, or authorize their own output.
- Give each role only the scoped payload defined in `commands/resume-team.md`; validate every handoff digest, parent, attempt, run/case ID, and globally unique per-invocation agent identity, including re-audits and retries.
- Manual Codex `sandbox_mode = "read-only"` prevents role writes but does not isolate filesystem reads. The production CLI runtime supplies the capability-isolated path by launching roles in empty temporary working directories with interactive tool surfaces disabled. Do not claim equivalent isolation for manual role use.
- Researcher requirements are provenance-bound: `hard_requirements` followed by `soft_requirements` must equal the exact uniquely anchored `jd_evidence_spans[*].evidence_text` values one-for-one, byte-for-byte, and in order.
- Draft-stage publication requires a durable run claim, trusted source attestation, a final Auditor `PASS`, three fresh independent evidence/human-voice/canonical-integrity votes on the same draft digest, an exact committed publication receipt, and verified readback.
- Runtime `PUBLISHED` means only an authorized, digest-verified `resume.md` draft-stage artifact carried by `resume-team-result/v2`. It is not DOCX, tracker, cleanup, or package completion. `/resume` or `/tailor-resume` must finish all command-specific finalization gates before reporting package success.
- A `PUBLISHED` result is not consumable until its `candidate_fit_report` and `candidate_fit_report_digest` exactly match the independently validated passing preflight; the coordinator resolves `authorization_receipt_path` against the output directory when relative, requires its resolved parent to equal that directory, reads regular non-symlink `resume-team-final-receipt/v2` JSON, validates `schemas/resume-team-final-receipt.schema.json`, recomputes its canonical digest, matches the inline receipt and result run/case IDs, and binds the same passing candidate-fit report/digest plus `draft_digest` and `verified_target_digest` to the configured master, exact JD, `final_draft_digest`, and actual `resume.md`. Recompute `source_digest` from the current configured master and `job_description_digest` from the fixed sibling `job_description.txt`; require a SHA-256 Researcher artifact and distinct same-host native Researcher/Auditor identities. Require a same-draft PASS `auditor_attestation`; require the full `authorization_report` to pass with no codes and exactly three ordered named same-draft PASS votes with distinct IDs; require `canonical_digest(authorization_report) == authorization_digest` and the receipt vote-ID list to equal those IDs in order. Revalidate immediately before DOCX and preserve the JD and durable receipt during cleanup.
- The hardened Codex runtime ignores user configuration and transient parent-session settings. Do not claim an inherited profile, model, or Ultra mode. Omit pins by default; use `--model <exact-model>` and/or `--reasoning-effort ultra` only when explicitly requested. No runtime profile option exists, and Codex-only pins must not be passed to Claude.
- The coordinator alone performs ordered finalization: verified resume DOCX → verified cover-letter DOCX → tracker update → artifact/state verification → cleanup → report. It must retain the captured runtime result, invoke `final_receipt_verifier.py` with that result's exact sidecar path and digest immediately before every side-effect boundary, and use only `create_resume_from_md_authorized`, `create_cover_letter_from_md_authorized`, and `add_application_authorized`. Advance state only after verified regular non-empty DOCX output or a tracker return value that is literally `True`; exceptions and false results stop finalization.
- Any malformed, stale, replayed, ambiguous, timed-out, unavailable, side-effecting, or partially published result fails closed.

## Operating Rules

- Read `config.json` for `master_resume_path` before tailoring or scoring.
- Never begin tailoring from a previous application resume. The configured master
  must pass the exact-JD candidate-fit gate before any development begins.
- For `.docx` resumes, use the MCP `extract_text` tool when available, or Python with `python-docx`.
- Use `rg --files` or `find` for file discovery.
- Start the scorer server with `python scorer_server.py --port 8100` when REST scoring is needed and `http://localhost:8100/health` is not already healthy.
- Preserve canonical resume facts exactly: job titles, company names, dates, education, publications, certifications, and memberships.
- Every master resume role must remain in tailored resumes unless the user explicitly asks otherwise.
- Generated `resume.md` and `cover_letter.md` must not use markdown bold markers; DOCX generation handles formatting.
- Delete transient markdown/state files only after the corresponding DOCX files and tracker updates are verified.
- Before DOCX: run `python evidence_audit.py <resume.md>` and `python human_voice_audit.py <resume.md>` (both exit 0 required). Cover letters: `python human_voice_audit.py <cover_letter.md> --mode cover_letter`.
- Writing skill: `commands/writing-coach.md` (Human Voice + Impact, Rules 0–16). Shared AI-tell lexicon: `data/ai_tells.json`. Examples: `references/human_voice_examples.md`.
- Ban AI-cliché bullet openers (spearheaded, leveraged, orchestrated, …). Prefer plain verbs (Led, Built, Wrote, Cut, Reviewed).
