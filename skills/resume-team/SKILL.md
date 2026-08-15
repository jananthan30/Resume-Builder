---
name: resume-team
description: Run the capability-isolated native Researcher, Writer, Auditor, and Editor runtime to publish a verified Markdown resume draft. Use when Codex is asked to tailor or generate a resume from a job description with strict provenance, human-voice, canonical-integrity, and draft-publication gates using the Codex subscription rather than external model APIs.
---

# Resume Team

1. Resolve the plugin root two directories above this file. Require macOS or Linux,
   `candidate_fit_preflight.py`, `native_resume_team.py`, `config.json`, and the
   configured master resume. Fail closed if any is unavailable. Put the exact job
   description in a private temporary UTF-8 file, and generate a safe `run_id`,
   safe `case_id`, and strict ISO `as_of_date`. Before invoking the native team or
   creating an output directory, screen only `master_resume_path` from `config.json`
   (never a previous tailored resume):

   `python candidate_fit_preflight.py --resume <CONFIGURED_MASTER> --job-description <PRIVATE_EXACT_JD.txt> --run-id <RUN_ID> --case-id <CASE_ID> --as-of-date <YYYY-MM-DD> --json`

   Continue only on exit `0` and a valid digest-bound `candidate-fit-policy-v3`
   report with threshold `70.0`, score at least `70`, trustworthy extraction, zero
   hard knockouts, `passed: true`, and no codes. Canonically hash the exact report
   as `candidate_fit_report_digest`. Exit `1`, any score below 70 (including
   60–69), or any hard knockout is `REJECTED:CANDIDATE_FIT`; report it and create
   no output, draft, DOCX, or tracker row. Exit `2` or an unavailable, malformed,
   stale, non-canonical, or digest-mismatched report is
   `FAILED:CANDIDATE_FIT_PREFLIGHT`. Neither condition has a bypass. ATS/HR scores
   are advisory and cannot override candidate fit.
2. Only after candidate fit passes, run the non-model host preflight. Windows is
   not supported by the hardened runtime; its host preflight returns
   `POSIX_RUNTIME_REQUIRED`:

   `python native_resume_team.py --host codex --check-host --config config.json`

   Continue only when it exits `0` and returns `ready: true` with every check passing.
3. Choose only a prospective new output path. Use a user-supplied path only when it
   does not exist; otherwise derive a sanitized, non-existing
   `applications/<Company> - <Role>` path from the job description. Do not create
   it. Never reuse, replace, or clobber an existing output or `resume.md`; the
   runtime intentionally has no replacement mode.
4. Capture the
   runtime's exact stdout in a private temporary result file so the result-provided
   authorization sidecar path and digest remain available to finalization:

   `runtime_result_file="$(mktemp)"; python native_resume_team.py --host codex --job-description-file <PRIVATE_EXACT_JD.txt> --output-dir <APP_DIR> --config config.json --run-id <RUN_ID> --case-id <CASE_ID> --as-of-date <YYYY-MM-DD> >"$runtime_result_file"`

   Preserve and check the Python process exit status before parsing the single JSON
   result from that file. Do not reconstruct result fields from logs or filenames.
   The runtime independently recomputes the same fit report before constructing an
   output or role adapter. Require its report and canonical digest to exactly match
   the caller-validated preflight. Only then does it create `<APP_DIR>` and
   atomically bind that exact text to `<APP_DIR>/job_description.txt`;
   it accepts only a byte-identical pre-existing regular file and never clobbers a
   different one. The Researcher contract admits only nonblank, alphanumeric
   requirements that each cover one exact, unique, complete non-separator JD
   line; substrings are rejected so surrounding negation, scope, bounds, and
   qualification cannot be trimmed before the Writer or Auditor sees the rubric.

   Omit `--model` and `--reasoning-effort` by default. The hardened subprocess ignores user configuration and transient parent-session settings, so report its effective model/reasoning selection as the isolated managed CLI default and unknown—not as an inherited model, Ultra setting, or profile. Only when the user explicitly requests a Codex pin may you append `--model <exact-model>` and/or `--reasoning-effort ultra`. There is no runtime profile or replacement option; always choose a new output directory and fail closed rather than clobber an existing artifact.
5. Accept draft-stage success only when the process exits `0`, the captured result
   is `resume-team-result/v2` with `terminal_class: PUBLISHED`, its
   `candidate_fit_report` and `candidate_fit_report_digest` exactly match the
   independently validated preflight, and `<APP_DIR>/resume.md` exists with the
   reported `final_draft_digest`. Independently hash `resume.md`. Resolve the exact
   `authorization_receipt_path` from that result against `<APP_DIR>` when relative
   and require the resolved path's parent to equal the resolved output directory.
   Then invoke the code-bound acceptance gate with the exact result-provided digest
   and require exit `0` plus `verified: true`:

   `python final_receipt_verifier.py --resume <APP_DIR>/resume.md --receipt <RESOLVED_RECEIPT_PATH> --expected-receipt-digest <authorization_receipt_digest-from-captured-runtime-result> --config config.json`

   This verifier reads only regular, non-symlink artifacts, validates the
   `resume-team-final-receipt/v2` sidecar against
   `schemas/resume-team-final-receipt.schema.json`, enforces canonical receipt
   bytes and digest, and binds the receipt to the actual resume, current configured
   master, and fixed sibling `job_description.txt`. Its checks include the exact
   passing candidate-fit report and digest; matching run/case IDs; `draft_digest`,
   `source_digest`, `job_description_digest`, and `verified_target_digest`; a
   SHA-256 Researcher artifact; distinct same-host native Researcher/Auditor
   identities; an exact same-draft PASS `auditor_attestation`; and the exact passing
   `authorization_report` with three ordered named `evidence`, `human_voice`, and
   `canonical_integrity` votes, no codes, no findings, distinct invocation IDs,
   `canonical_digest(authorization_report) == authorization_digest`, matching
   vote-ID order, and a non-empty publication ID. Manual inspection may explain
   these properties but may not replace the verifier. Any missing, malformed,
   stale, or mismatched value fails closed. On any failure, report the stable
   failure class and do not create a replacement, DOCX, tracker update, or success
   message.
6. State explicitly that `PUBLISHED` means only an authorized, digest-verified `resume.md` draft-stage artifact. It is not DOCX generation, tracker completion, cleanup, or a completed application package. If this skill was called standalone, report the draft as ready for finalization. Only a caller implementing `/resume` or `/tailor-resume` may continue its existing ordered DOCX, tracker, artifact-verification, cleanup, and final-report gates. That caller must retain the captured result, use only `create_resume_from_md_authorized`, `create_cover_letter_from_md_authorized`, and `add_application_authorized`, and pass the exact resolved sidecar path and result-provided digest to every wrapper. Invoke `final_receipt_verifier.py` again immediately before each finalization boundary; the wrappers also revalidate at the side-effect boundary. Preserve the durable receipt during transient cleanup. Advance state only after a verified non-empty regular DOCX or a tracker return value that is literally `True`, propagate every exception, and do not report package completion until all gates pass.

Do not manually spawn role agents as a fallback. The CLI runtime isolates Codex roles in empty temporary working directories, disables interactive tool surfaces, verifies unique native invocation identities, runs the three deterministic votes, and performs receipt/readback-bound draft publication.
