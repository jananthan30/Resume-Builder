# Single-Writer Web Rewrite Design

## Problem

The deployed `POST /rewrite` route currently runs the full hosted Resume Team:
Researcher, Writer, Auditor, and sometimes Editor. The roles are sequential, not
concurrent, but each model handoff adds a schema and repair boundary.

The production run `95b2fcf2-7f73-4753-b8b6-3edf4db346c0` demonstrated the
failure pattern after the Sonnet 5 deployment:

1. The Researcher response required a schema repair.
2. The Writer returned source-anchored replacements.
3. The coordinator rejected the combined draft with `draft duplicates or moves
   source claims`.
4. A model repair returned an invalid normalized handoff.
5. The route terminated as `FAILED:WRITER_SCHEMA` and returned the generic 502
   message.

The model and transport were healthy. The failure was the composition of model
handoffs, whole-batch Writer validation, and model-driven semantic repair.

## Goal

Make the authenticated web Rewrite path reliable by using exactly one
resume-writing AI role: the Sonnet 5 Writer. Keep all authenticity, candidate-fit,
provenance, deterministic audit, quota, and publication-readback controls.

The internal four-role workflow remains available for non-web/native workflows.
This design changes only the hosted web Rewrite mode.

## Non-goals

- Do not weaken source anchoring or claim-support checks.
- Do not let a model write files, publish, audit itself, or update the tracker.
- Do not create a second unaudited endpoint or return partial model text.
- Do not change authentication, plan quotas, or the legacy `/rewrite` response
  shape.
- Do not add an AI Editor fallback.
- Do not expose resume, job-description, replacement, or provider-response text
  in logs.

## Considered approaches

### 1. Hybrid single-writer adapter inside the existing coordinator — selected

Use deterministic Researcher and Auditor implementations around the existing
Sonnet 5 Writer. Run them through `multi_agent_team.run_team`, so the existing
candidate-fit gate, handoff digests, independent authorization votes, publication
receipt, and durable readback remain authoritative.

This removes model-to-model coordination while retaining the mature safety
boundary. It is the smallest design that improves reliability without creating a
second publication protocol.

### 2. A separate lightweight `/rewrite` implementation — rejected

Calling Sonnet directly from `scorer_server.py` would be shorter, but it would
duplicate candidate-fit, claim evidence, audit, publication, and receipt logic.
Two authorization paths would drift and make the web route weaker than the rest
of the product.

### 3. Keep all roles and loosen the Writer validator — rejected

This would preserve the same Researcher, Auditor, Editor, and repair failure
surfaces. Loosening whole-draft checks would also trade reliability for weaker
authenticity guarantees. The correct change is to remove unnecessary model roles
and salvage only replacements that already pass the strict compiler.

## Architecture

### Entry point and scope

`rewrite_resume_endpoint` invokes `run_resume_team` with an explicit hosted
single-writer mode. The default remains the current four-role adapter, so other
callers do not change implicitly.

The mode builds the same eight-key `run_team` request with
`max_editor_attempts = 0`. The request still uses the exact saved or one-off master
resume and the normalized job description.

### Deterministic Researcher

The composed adapter lives in `agent/adapter.py`, exposes its Writer host for
existing token accounting, and constructs the Researcher handoff without a model
call:

- Work only from complete, unique lines in the normalized job description.
- Exclude blank lines, separators, and heading-only lines.
- Classify lines containing mandatory cues such as `required`, `must`,
  `minimum`, or `need` as hard requirements.
- Classify preferred cues such as `preferred`, `desired`, `plus`, or
  `nice to have` as soft requirements.
- Include remaining substantive posting lines as soft context so the Writer sees
  the full source without inventing a rubric.
- Preserve every selected line byte-for-byte and derive offsets and digests with
  the existing `normalize_native_payload` function.
- Fail with the existing job-description formatting response only when no unique
  substantive line can be anchored.

The deterministic Researcher gets a unique `api:` invocation identity such as
`api:researcher.<run>.0.det` and a normal digest-bound handoff built and checked
with the existing `build_handoff`, `normalize_native_payload`, and
`validate_handoff` functions. It does not call Anthropic.

### Sonnet 5 Writer

The Writer is the only AI role that can propose resume text:

- Use the existing `AnthropicHost`, which is pinned and fail-closed to
  `claude-sonnet-5`.
- Keep adaptive thinking disabled for the bounded JSON response.
- Accept only the existing model-facing contract:
  `{"replacements": [{"source_span_text": ..., "replacement_text": ...}]}`.
- Invoke `AnthropicHost.run_role("writer", ...)` once as one logical role call.
  The host may perform its existing syntax-only JSON repair when the provider
  returns malformed JSON.
- Do not ask the model to repair coordinator or semantic-validator failures.
- Never expose a complete or intermediate model draft.

The composed adapter must not delegate to `AnthropicTeamAdapter.invoke` for the
Writer because that adapter performs a second model call for coordinator-semantic
repair—the failure loop this design removes.

### Deterministic replacement salvage

The current compiler rejects the entire batch when any combined replacement
breaks ownership, structure, evidence, or claim-support rules. The single-writer
mode instead evaluates proposals through the same strict compiler incrementally:

1. Validate the top-level contract and each replacement item's exact keys and
   value types.
2. Resolve every source against the immutable master and require one unique,
   complete, non-separator line.
3. Reject no-ops, duplicate anchors, unsafe characters, and multiline
   replacement text.
4. Sort candidates by their original source offsets.
5. Add one candidate at a time to the accepted set and compile the complete
   accepted set with `_compile_writer_replacements`.
6. Keep the candidate only if the strict compiler, ownership parser, claim
   support, Core Competencies rule, evidence normalization, and a deterministic
   canonical-integrity comparison against the master all pass.
7. Build the final draft once from the accepted source-ordered set.

This is fail-closed salvage: invalid proposals are discarded, never relaxed or
rewritten. The result is the maximal greedy source-ordered subset accepted by all
gates. Two individually valid but mutually conflicting proposals may cause the
later proposal to be discarded; the ordering is deterministic and never grants
extra authority.

If at least one proposal is accepted, the normal Writer handoff continues. If the
Writer explicitly returns an empty list, the byte-identical master may continue
through the audits. If it proposes changes but none are safe, the adapter raises a
narrow typed coordinator signal. `run_team` handles only that signal as the stable
`REJECTED:NO_SAFE_CHANGES` terminal instead of publishing an unchanged resume or
claiming a transient outage. No schema-incompatible sentinel payload is used.

Only counts and stable rejection categories are logged. Source and replacement
text never enter logs or events.

### Deterministic audit attestation and no Editor

The coordinator protocol requires an Auditor packet before it runs the actual
authorization votes. Evidence and integrity audits can produce document-level
failures without a unique line location, so the adapter must not fabricate the
line-bound findings required by a model Auditor's FAIL contract.

Instead, the composed adapter returns a deterministic Auditor protocol
attestation: `PASS`, no findings, and the exact unchanged Writer draft, using a
fresh identity such as `api:auditor.<run>.0.det`. Its purpose is only to bind the
candidate digest and preserve the existing receipt structure. It is not described
or logged as an independent content audit.

`max_editor_attempts` is zero, and the adapter refuses an Editor invocation as a
defense-in-depth assertion. The immediately following three fresh deterministic
authorization votes are the actual evidence, human-voice, and canonical-integrity
audit. A failed vote terminates with its stable rejection code and never triggers
another writing model.

The coordinator then performs its existing fresh, three-vote authorization pass.
Publication still requires unanimous evidence, human-voice, and canonical
integrity votes on the exact same draft, followed by receipt commit and verified
readback.

## Data flow

1. Authenticate, check tier, validate JD, and resolve the exact master resume.
2. Reserve the quota slot and durable run row.
3. Run candidate-fit preflight and the existing reasoning judge only when the
   deterministic fit policy rejects. The judge cannot write resume text.
4. Create a deterministic, evidence-bound Researcher artifact from the JD.
5. Ask the single Sonnet 5 Writer for anchored replacements.
6. Salvage the safe subset with the strict deterministic compiler.
7. Bind the draft with the deterministic Auditor protocol attestation, then run
   the fresh authorization votes.
8. Commit, verify, and read back the existing publication receipt.
9. Return the unchanged legacy JSON response with the verified draft and scores.

No Writer proposal reaches the user before step 8.

## Error behavior

- Provider, timeout, or malformed-JSON failures retain a non-sensitive 502.
- Unanchorable JD input retains the actionable 422 formatting response.
- Candidate-fit refusal retains the existing evidence-based 409 response.
- `REJECTED:NO_SAFE_CHANGES` returns an actionable 422 explaining that the model
  proposed no source-supported change and suggesting that the saved resume be
  updated if relevant experience is missing.
- Deterministic audit rejection returns an actionable 422 with a safe category,
  not the false “try again in a few minutes” outage message.
- Internal codes, model response text, resumes, and JDs are never returned.

## Observability

Existing run and milestone events remain. Add an optional adapter-stats seam that
`run_team` consumes when present, or record a separate post-run event from
`agent.tools`. Either route may add these safe fields, which contain no user text:

- `pipeline_mode = single_writer`
- `writer_proposed_count`
- `writer_accepted_count`
- `writer_rejected_count`
- stable rejection-category counts
- whether the Researcher and Auditor were deterministic

This makes it possible to distinguish provider failure, empty output, salvage,
audit rejection, and publication failure without logging personal data.

## Tests

The implementation starts from production-shaped failing tests:

1. A Writer response containing one safe replacement and one ownership-breaking
   replacement currently fails the whole batch; single-writer mode must retain
   the safe replacement and publish it.
2. A proposed batch with no safe replacement must return
   `REJECTED:NO_SAFE_CHANGES` and must not publish.
3. The adapter must call Anthropic only for `writer`; Researcher and the Auditor
   protocol attestation are deterministic, and Editor is never invoked.
4. The Writer transport must remain exactly `claude-sonnet-5` with thinking
   disabled.
5. Researcher output must be byte-for-byte evidence-bound to unique complete JD
   lines.
6. Every accepted replacement must pass the unchanged strict compiler and claim
   support checks.
7. Candidate-fit, quota, three deterministic votes, receipt identities, receipt
   commit, and readback
   tests must remain green.
8. The default/native four-role path must remain unchanged.
9. `/rewrite` must preserve its success response shape and map deterministic
   rejection to actionable non-502 copy.

Update `tests/test_team_via_api_host.py`, `tests/test_agent_tools.py`, and
`tests/test_rewrite_alias.py`; add coordinator coverage in
`tests/test_multi_agent_team.py` for the typed no-safe terminal, greedy salvage,
zero Editor attempts, receipt identities, and audit-failure/no-publication. Run
those focused suites, then the complete repository suite and an independent
review.

## Deployment verification

After deployment:

1. Confirm Fly reports the new machine version healthy.
2. Confirm the deployed Resume Team Writer still resolves to
   `claude-sonnet-5`.
3. Run a production-sized synthetic rewrite with no personal data.
4. Verify exactly one model role (`writer`) was invoked.
5. Verify the run reaches `writer_complete`, `audit_complete`, `pre_publish`, and
   `tailor_run_succeeded`.
6. Verify a mixed safe/unsafe synthetic batch publishes only the safe change.
7. Confirm the public `/health` endpoint remains healthy.

The change is complete only after the production-sized path publishes a verified
draft; a direct Writer response alone is not sufficient.
