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

### 1. Direct compiler-style web service — selected

Route synchronous web rewriting through `agent.web_rewrite`. Trusted code derives
exact job-requirement lines, calls the existing Sonnet 5 Writer once, compiles
anchored replacements, runs the shared deterministic policies, and commits only
a verified SQL readback. It does not manufacture Researcher/Auditor identities or
reuse the native package receipt schema.

This removes synchronous actor ceremony while preserving the safety boundary:
candidate fit, source anchoring, the strict replacement compiler, all three real
audits, atomic publication, and durable readback remain authoritative.

### 2. Hybrid single-writer adapter inside the coordinator — superseded

This was the tactical first implementation. It removed model-to-model calls but
still constructed deterministic Researcher and always-PASS Auditor envelopes only
to satisfy `multi_agent_team.run_team`. Those packets added identity, digest, and
receipt complexity without supplying a safety decision; the real audits ran in
the next deterministic step.

### 3. Call Sonnet directly from the FastAPI route — rejected

The route remains a thin HTTP adapter. Model invocation and policy live in a
separate service so `scorer_server.py` cannot return an unchecked Writer result,
and the SQL-backed trust boundary remains reusable and testable.

### 4. Keep all roles and loosen the Writer validator — rejected

This would preserve the same Researcher, Auditor, Editor, and repair failure
surfaces. Loosening whole-draft checks would also trade reliability for weaker
authenticity guarantees. The correct change is to remove unnecessary model roles
and salvage only replacements that already pass the strict compiler.

## Architecture

### Entry point and scope

`rewrite_resume_endpoint` still invokes the registered `run_resume_team` tool with
explicit `pipeline_mode="single_writer"` for HTTP compatibility. Inside the tool,
that mode dispatches directly to `agent.web_rewrite.run_web_rewrite`; it never
constructs a `run_team` request. The default mode still uses the four-role adapter
and coordinator, so native and asynchronous callers do not change.

### Deterministic requirement derivation

`agent.web_rewrite.derive_requirement_rubric` is a pure function, not a role or
handoff:

- Work only from complete, unique lines in the normalized job description.
- Exclude blank lines, separators, and heading-only lines.
- Classify lines containing mandatory cues such as `required`, `must`, or
  `minimum` as hard requirements.
- Include remaining substantive posting lines as soft context.
- Preserve every selected line byte-for-byte in the closed Writer payload.
- Fail with the existing job-description formatting response only when no unique
  substantive line can be selected.

There is no Researcher identity, artifact digest, role timeout, packet validation,
or model call. The exact normalized JD is already bound into the candidate-fit
report and final web receipt.
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

The direct service calls `AnthropicHost` itself. It does not invoke an adapter,
build a role context, or perform a second semantic-repair call after deterministic
validation—the failure loop this design removes.

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
4. Because the web path has no semantic Auditor, require mechanically
   equivalent wording: only the closed first-verb swaps are admitted. Every
   insertion, including a plausible acronym expansion, is rejected even though
   the native-team closure rule can defer additions to its semantic Auditor.
5. Sort candidates by their original source offsets.
6. Add one candidate at a time to the accepted set and compile the complete
   accepted set with `_compile_writer_replacements`.
7. Keep the candidate only if the strict compiler, ownership parser, claim
   support, Core Competencies rule, evidence normalization, and deterministic
   canonical-integrity comparison against the master all pass.
8. Build the final draft once from the accepted source-ordered set.

This is fail-closed salvage: invalid proposals are discarded, never relaxed or
rewritten. The result is the maximal greedy source-ordered subset accepted by all
gates. Two individually valid but mutually conflicting proposals may cause the
later proposal to be discarded; the ordering is deterministic and never grants
extra authority.

If at least one proposal is accepted, the compiled draft continues. If the Writer
explicitly returns an empty list, the byte-identical master may continue through
the audits. If it proposes changes but none are safe, the compiler's narrow typed
signal becomes the stable `REJECTED:NO_SAFE_CHANGES` terminal instead of
publishing an unchanged resume or claiming a transient outage.

Only counts and stable rejection categories are logged. Source and replacement
text never enter logs or events.

### Deterministic authorization and no Editor

The direct service calls `CloudTrustedServices.audit_draft` once after compiling
the candidate. That produces three fresh, distinct, same-draft votes from the
real evidence, human-voice, and canonical-integrity audit engines. The existing
strict authorization-report validator rechecks their structure and digest
binding before publication.

There is no protocol Auditor and no always-PASS attestation. A failed vote returns
a stable rejection and cannot trigger another model call. There is likewise no
Editor branch: the Writer's admitted proposal either passes all three audits or
the request fails closed.

Publication uses a web-specific receipt containing the exact candidate-fit report,
three-vote authorization report, input/draft digests, and publication ID. It
intentionally omits fictional Researcher/Auditor identities. The SQL publisher
commits the draft and metadata, then `read_publication` must return the exact
stored object before the service may return `PUBLISHED`.
## Data flow

1. Authenticate, check tier, validate JD, and resolve the exact master resume.
2. Reserve the quota slot and durable run row.
3. Run candidate-fit preflight and require the canonical v2 policy itself:
   trustworthy extraction, zero hard knockouts, score at least 70, `passed: true`,
   and no codes. A reasoning judge cannot override this web gate.
4. Derive exact requirement lines from the normalized JD in pure code.
5. Ask the single Sonnet 5 Writer for anchored replacements.
6. Salvage the safe subset with the strict deterministic compiler.
7. Run the three fresh deterministic authorization votes on the full draft.
8. Commit the web receipt metadata and verify the exact SQL readback.
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

Outer `tailor_run_succeeded` / `tailor_run_failed` events remain. The direct
service emits four meaningful, count/digest-only milestones:
`rewrite_requirements_ready`, `rewrite_draft_compiled`, `rewrite_authorized`, and
`rewrite_pre_publish`. Writer diagnostics contain only proposed/accepted/rejected
counts and allowlisted rejection-category counts. No resume, JD, source span,
replacement, or provider-response text enters logs or events.

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
