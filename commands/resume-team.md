---
description: Run the role-separated, fail-closed Resume Team workflow against a job description.
---

# Resume Team Coordinator

Use the native subscription host to coordinate a factual, role-separated resume draft for this job description:

$ARGUMENTS

## Authoritative executable path

## CANDIDATE-FIT PREFLIGHT (MANDATORY FIRST GATE)

Before invoking the native team, any role agent, or creating an output/application
directory, resolve `master_resume_path` from `config.json` and put the exact job
description in a private temporary UTF-8 file. The configured master resume is the
only resume that may be screened; never screen or substitute a previously tailored
resume. Generate one safe `run_id`, one safe `case_id`, and one strict ISO calendar
`as_of_date`, then run the deterministic machine preflight:

`python candidate_fit_preflight.py --resume <configured-master-resume> --job-description <private-exact-JD.txt> --run-id <run_id> --case-id <case_id> --as-of-date <YYYY-MM-DD> --json`

The JSON report must contain exactly the trusted `candidate-fit-policy-v3`
assessment bound to those run/case IDs, date, master-resume SHA-256, and exact-JD
SHA-256. Recompute its canonical JSON SHA-256 as `candidate_fit_report_digest`.
Continue only on exit `0` and a valid report with `threshold: 70.0`, `score >= 70`,
`extraction_trustworthy: true`, `hard_knockouts: []`, `passed: true`, and
`codes: []`. Exit `1`, any score below 70 (including 60–69), or any hard knockout
is terminal `REJECTED:CANDIDATE_FIT`; do not invoke Researcher, Writer, Auditor,
Editor, or `native_resume_team.py`, and do not create an application directory,
DOCX, tracker row, or resume draft. Exit `2` or any unavailable, malformed,
non-canonical, stale, or digest-mismatched report is
`FAILED:CANDIDATE_FIT_PREFLIGHT` and fails closed. There is no automatic or manual
workflow bypass. ATS and HR baseline scores are separate advisory diagnostics and
cannot override this gate.

The prohibition above covers the authorized pipeline only: no gate outcome may be
negotiated into the native runtime, its receipts, or its authorized wrappers.
Separately, the user may record a deliberate manual override with
`candidate_fit_override.py` (`candidate-fit-override/v1`, decision
`PROCEED_MANUAL`): it requires a rejected gate and an explicit reason, binds the
exact gate/review reports and digests it overrules, and authorizes only a
coordinator-built manual package (truthful master-sourced content,
`evidence_audit.py`, `human_voice_audit.py`, `resume_integrity_audit.py`,
non-authorized renderers, `MANUAL_DRAFT.txt` labeling). It never invokes the
runtime, creates receipts, or updates the tracker through authorized wrappers.

For an authorized draft, do not reproduce this orchestration manually. The hardened runtime currently requires macOS or Linux; its Windows host preflight fails closed with `POSIX_RUNTIME_REQUIRED`. Only after the candidate-fit gate passes, run the non-model host preflight with `python native_resume_team.py --host <codex|claude> --check-host --config config.json`. Continue only on exit `0` with `ready: true`. Select a prospective sanitized output path but do not create it. Capture the runtime's exact stdout in a private temporary result file so finalization retains the sidecar path and digest; for example, run `runtime_result_file="$(mktemp)"` followed by `python native_resume_team.py --host <codex|claude> --job-description-file <private-exact-JD.txt> --output-dir <new-application-dir> --config config.json --run-id <run_id> --case-id <case_id> --as-of-date <YYYY-MM-DD> >"$runtime_result_file"`, preserving and checking the Python process exit status before parsing the single JSON result. The output directory must not exist before invocation: the runtime has no replacement mode, recomputes the same deterministic candidate-fit report before constructing its role adapter or output state, and creates output only after that report passes. It atomically preserves the exact request as `<output-dir>/job_description.txt`; it accepts only a byte-identical pre-existing regular file and never overwrites a different one.

For Codex only, omit model flags by default: the hardened subprocess ignores user configuration and transient parent-session settings, so the effective managed CLI default is unknown and must not be reported as an inherited profile, model, or Ultra setting. If the user explicitly requests pins, append `--model <exact-model>` and/or `--reasoning-effort ultra`. There is no runtime profile option. Never pass those Codex-only pins to Claude.

An exit-0 `resume-team-result/v2` result with `terminal_class: PUBLISHED` means only that an authorized, digest-verified `<output-dir>/resume.md` draft-stage artifact was atomically committed and read back. It is not a completed application package. Before accepting it, require the inline `candidate_fit_report` and `candidate_fit_report_digest` to exactly match the independently validated preflight report and canonical digest. Resolve `authorization_receipt_path` against `<output-dir>` when relative and require the resolved path's parent to equal the resolved output directory. Read only a regular, non-symlink file. Parse its exact `resume-team-final-receipt/v2` JSON, recompute its canonical digest, and require equality with `authorization_receipt_digest` and the inline `authorization_receipt`. Require matching result/receipt run and case IDs, require the receipt's candidate-fit report and digest to equal the result and independently validated preflight, and bind receipt `draft_digest` and `verified_target_digest` to `final_draft_digest` and the independently hashed `resume.md`.

The acceptance gate is executable, not optional prose. Extract the exact
`authorization_receipt_path` and `authorization_receipt_digest` from the captured
runtime result, resolve the path as described above, and require exit `0` plus
`verified: true` from:

`python final_receipt_verifier.py --resume <output-dir>/resume.md --receipt <resolved-authorization-receipt-path> --expected-receipt-digest <authorization_receipt_digest-from-runtime-result> --config config.json`

Run this same code-bound verifier again immediately before each side-effecting
finalization boundary. Never substitute a manually reconstructed or guessed digest.

Validate the sidecar against `schemas/resume-team-final-receipt.schema.json`. Recompute `source_digest` from the currently configured master resume and `job_description_digest` from the fixed sibling `job_description.txt`. Revalidate the embedded candidate-fit report against those digests, the fixed threshold, zero hard knockouts, trustworthy extraction, run/case IDs, and date, and require its canonical digest to equal `candidate_fit_report_digest`. Require a SHA-256 `researcher_artifact_digest` and distinct same-host native `researcher_agent_id` and Auditor identity (`codex:` or `claude:`). Require an exact `auditor_attestation` object with SHA-256 `artifact_digest`, `verdict: PASS`, and the same draft digest. Require an exact `authorization_report` with `schema_version: resume-team-authorization/v1`, that same draft digest, `passed: true`, no codes, no findings, and exactly three ordered `evidence`, `human_voice`, and `canonical_integrity` votes. Each vote must be `resume-team-vote/v1`, PASS with no codes, bound to the same draft, and have a distinct non-empty invocation ID. Require `canonical_digest(authorization_report) == authorization_digest` and require `vote_invocation_ids` to equal those three IDs in order. Also require a non-empty publication ID. A missing, symlinked, malformed, stale, or mismatched candidate-fit report, source, JD, sidecar, or draft fails closed. Preserve the JD and durable receipt during cleanup. This standalone command must report the authorized draft as ready for finalization, not report package completion. `/resume` or `/tailor-resume` must still run their ordered DOCX, tracker, artifact verification, cleanup, and final-report gates. The protocol below defines the contracts enforced by the runtime.

## Protocol and authority

The coordinator is the only control plane. Use vendor-neutral protocol `resume-team/v2` and create exact `resume-team-context/v1` inputs with `multi_agent_team.build_context()`. Treat every role response as an untrusted payload proposal. Normalize its exact source-text anchors with `normalize_native_payload()`, wrap it with the real host invocation identity through `build_handoff()`, and accept it only when `validate_handoff()` returns valid. Cryptographic metadata is coordinator-owned; never ask a model to guess hashes. The role agents never write files, call one another, publish, update the tracker, or authorize their own work. Do not require API keys or an external orchestrator. Project custom-agent definitions omit model pins and therefore follow their host's custom-agent inheritance rules; the hardened executable path is different and uses isolated runtime configuration as described above.

Read `config.json` and resolve `master_resume_path` before delegation. Preserve the master resume as the sole factual authority. Never invent or alter experience, metrics, titles, companies, dates, Education, Publications, Certifications or Licensure, or Memberships. The editorial priority is Authenticity, then Human voice, then HR impact, then ATS match.

Each context must contain exactly `schema_version`, `run_id`, `case_id`, `role`, `attempt`, `parent_artifact_digest`, and `payload`. Give each role only its least-authority payload:

Attempt values are role-specific: Researcher and Writer are exactly attempt `0`; Auditor is attempt `0`, `1`, or `2`; Editor is correction attempt `1` or `2`. Any other role/attempt combination is schema-invalid.

- Researcher: `job_description` only.
- Writer: `master_resume` and the validated `researcher_artifact` only; never the raw job description.
- Auditor: `master_resume`, validated `researcher_artifact`, and `writer_draft` only; never the raw job description.
- Editor: `master_resume`, `writer_draft`, and `audit_findings` only; never the raw job description or unrelated context.

The coordinator-built handoff must contain exactly `schema_version`, `run_id`, `case_id`, `role`, `agent_id`, `attempt`, `parent_artifact_digest`, `artifact_digest`, `status`, and `payload`. Recompute the SHA-256 of canonical payload JSON with sorted keys, compact separators, and non-ASCII characters escaped. Reject unknown fields, wrong types, failed status, invalid or ambiguous source anchors, stale run or case IDs, wrong parents, skipped attempts, duplicate or replayed packets, unexpected roles, late results, and previously seen draft digests. Require a globally unique real native `agent_id` for every invocation, including every Auditor re-audit and Editor attempt; no role or retry may reuse an identity. On any crash, timeout, cancellation, unavailable role, ambiguous result, provenance failure, or side effect, fail closed. The coordinator must not substitute for a missing role.

At the application boundary, require the exact `multi_agent_team.run_team()` control-plane contracts. Durably claim the `run_id`/`case_id` before invoking a role and reject replay. Require a trusted source attestation bound to the exact master-resume digest. Each deterministic authorization report must contain the ordered `evidence`, `human_voice`, and `canonical_integrity` votes with distinct fresh invocation IDs, exact per-vote codes, the same candidate digest, and a digest-bound finding for every failed code. A finding is actionable only when the trusted audit supplies its exact full draft line, 1-based line number, and SHA-256 line digest; aggregate or infrastructure failures remain non-actionable and cannot authorize an edit. Draft publication must return an exact committed receipt bound to that digest, followed by an independent readback verification of the same publication ID and target digest and a durable authorization-receipt sidecar. A boolean, missing field, stale identity, unverified write, unreadable sidecar, or mismatched digest is not authorization.

Require the exact role payload keys:

- Researcher model payload: `rubric`, `jd_evidence_spans`; concatenate `hard_requirements` then `soft_requirements` and require that exact sequence to equal `jd_evidence_spans[*].evidence_text` one-for-one, byte-for-byte, with no missing, extra, reordered, or paraphrased item. Every evidence string must be one exact, uniquely locatable, complete non-separator JD line—not a substring—so surrounding negation, scope, bounds, and qualification remain bound; the coordinator verifies the boundary, then anchors and hashes it.
- Writer model payload follows this exact declaration:

```text
Return exactly one top-level key and no others:
  "replacements": [
    {
      "source_span_text": string,
      "replacement_text": string
    }, ...
  ]

Return replacements only, never a complete draft. source_span_text is one
exact, uniquely occurring, complete non-separator line copied byte-for-byte
from the master resume. replacement_text is either one safe single line or
the empty string to blank an optional source line. Use each source line at
most once. Do not emit unchanged replacements. Return [] when no supported
change is needed. The coordinator resolves every anchor against the immutable
master, applies all replacements in source order, and derives the full draft,
evidence offsets, and digests.
```

The Writer must preserve authenticity and human voice, make only minimal additive one-line changes, and may not return claim evidence, offsets, hashes, line numbers, a full draft, or extra keys. It has no tools or authority to write files, audit, authorize, publish, update a tracker, use credentials, or make network calls. The coordinator resolves anchors against the immutable master, owns application and evidence bookkeeping, and normalizes the resulting draft and claim evidence into the wire handoff.
- Auditor model payload: `verdict`, `findings`, `audited_draft`; verdict is exactly `PASS` or `FAIL`, each finding supplies exact `evidence_text`, and `audited_draft` must byte-match the candidate. The coordinator converts these to `evidence_digest` and `draft_digest`.
- Editor model payload: `draft`, `addressed_finding_ids`, `claim_evidence`; the draft is complete, all active finding IDs are covered, and every changed line has the same complete-source-line, case-preserving token equality, one-use-only provenance, and symmetric role binding required of Writer. No verdict or publication request is present.

After normalization, require the exact wire payload keys from `schemas/resume-team-handoff.schema.json`: Researcher `rubric`/`jd_evidence_spans`, Writer `draft`/`claim_evidence`, Auditor `verdict`/`findings`/`draft_digest`, and Editor `draft`/`addressed_finding_ids`/`claim_evidence`. Every normalized Writer and Editor `claim_evidence` item contains exactly `claim_digest`, `source_span_digest`, `source_start`, and `source_end`; the coordinator computes those values from the uniquely located source text.

## Required delegation sequence

Delegate in this order to distinct native role agents:

1. Researcher -> `resume-researcher`, attempt 0. Validate its handoff before continuing.
2. Writer -> `resume-writer`, attempt 0. Validate its source-anchored replacements and coordinator-compiled evidence-bound draft before continuing.
3. Auditor -> `resume-auditor`, attempt 0. Validate that it audited the exact Writer draft digest.

Call Editor only on a `FAIL`. A fail is the safety union of an Auditor `FAIL` or any failed deterministic authorization vote. Normalize deterministic failures into coded findings before delegating to `resume-editor`. The Editor may correct only those findings and must return a complete draft. Re-audit every edited draft with a fresh `resume-auditor` handoff. Allow at most 2 Editor corrections and therefore at most 2 re-audits; never make a third correction. Stop immediately on a repeated draft digest, editor overreach, a new unsupported fact or metric, unresolved findings, or exhausted correction budget.

Invoke Editor only when the safety union fails and every active deterministic failure is actionable and bound to exact verified draft lines. If both Auditor and all three deterministic votes return `PASS` on the first draft, never invoke Editor. An unsolicited Editor artifact is unexpected and must not advance the run. If two audit artifacts conflict, a deterministic finding is aggregate or malformed, or a verdict is not exactly `PASS` or `FAIL`, fail closed.

## Three independent authorization votes

After the last draft change and before draft-stage publication, write only a temporary candidate and collect three independent deterministic authorization votes. Each vote has veto power; none can compensate for another:

1. Evidence and factual-support vote: `python evidence_audit.py <candidate_resume.md>` must exit 0. It cannot waive canonical or voice failures.
2. Human-voice vote: `python human_voice_audit.py <candidate_resume.md> --mode resume` must exit 0. It cannot waive evidence or canonical failures.
3. Canonical-integrity vote: `python resume_integrity_audit.py --tailored <candidate_resume.md> --config config.json` must exit 0. It cannot waive evidence or voice failures.

Draft-stage publication requires the final Auditor `PASS` and all three independent authorization votes on the exact same final draft digest. Re-run all three votes after every edit; stale results never count. If any vote fails, route through the bounded `FAIL` correction path only when it supplies actionable findings; otherwise reject. Never let an agent verdict override a deterministic failure.

The runtime may atomically publish only the authorized `resume.md` draft and must verify its receipt, readback, and durable authorization sidecar before returning `PUBLISHED`. That terminal class does not represent DOCX generation, cover-letter generation, tracker mutation, cleanup, or package completion. Only after the caller independently revalidates the sidecar and published file against the result may `/resume` or `/tailor-resume` enter its existing ordered finalization workflow. Revalidate both immediately before DOCX creation, and never delete the durable receipt during transient cleanup. Do not report a completed package until every command-specific finalization gate succeeds. Preserve any pre-existing package and tracker bytes on every failure.
