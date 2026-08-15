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

- Do not weaken exact source anchoring; replace lexical claim closure with an independent, digest-bound semantic-support decision.
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
exact job-requirement lines, calls the Sonnet 5 Writer once, sends proposed changes
to two read-only Sonnet 5 factual-review lenses, compiles only unanimously supported
anchored replacements, runs shared deterministic policies, and commits only verified
SQL readback. It does not manufacture role envelopes or reuse the native receipt.

This removes synchronous actor ceremony while preserving the safety boundary:
candidate fit, exact source anchoring, independent semantic support, structural
compilation, all three code-owned audits, atomic publication, and durable readback.

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
### Sonnet 5 Writer and factual reviewer

The Writer is the only AI role allowed to propose resume text. Its contract is
short: use only facts stated in the full master resume, treat the JD only as
relevance guidance, return exact source-anchored single-line replacements, freely
paraphrase supported meaning, and never invent or strengthen facts. It aims for
three to five useful changes and returns an empty list only when no truthful
improvement exists.

`AnthropicHost` remains pinned to `claude-sonnet-5`, disables adaptive thinking,
and retains syntax-only JSON repair. The service never asks the Writer to repair a
policy rejection and never returns intermediate model text.

For non-empty proposals, two read-only Sonnet 5 calls use structurally distinct
claim-entailment and skeptical-recruiter lenses. They receive only the full master
and proposed pairs—never the JD—so requirements cannot become candidate evidence.
Both must independently PASS every admitted change. Each PASS cites exact, complete
master lines; code verifies citation existence and same-role locality. Reports echo
fresh invocation IDs and bind run, case, source digest, ordered proposal-set digest,
per-pair digests, and distinct raw-review digests. Reviewer text is never published.
Malformed, stale, replayed,
missing, reordered, contradictory, or non-unanimous decisions fail closed.

### Anchored semantic salvage

1. Validate exact item keys and value types.
2. Require each source to be one unique, complete master line; reject duplicate
   anchors, no-ops, deletion, unsafe characters, and multiline text. Preserve exact
   bullet prefixes, require substantive replacement bodies, and forbid content-to-heading
   reclassification under the real audit parser. Freeze all headings plus role, employer,
   date, education, publication, certification, and membership rows byte-for-byte before
   model review.
3. Bind every pair and ordered set to the exact run, case, and source digest, then
   request both independent factual reviews with fresh invocation IDs.
4. Validate exact evidence citations and discard every pair not unanimously marked
   supported with `code: PASS`.
5. Compile supported pairs in source order while retaining ownership, role locality,
   Core Competencies, document format, and canonical-integrity checks.
6. Run the complete draft through evidence, human-voice, and canonical-integrity
   audits before publication.

The old visible-token identity, zero-insertion rule, opener allowlist, insertion
budget, and polarity lexicon are removed from the web path. Truthful paraphrase,
reordering, shortening, and standard acronym use are now allowed. Native-team
compilation remains unchanged and continues to use its existing semantic Auditor.

If at least one proposal survives, the compiled draft continues. An explicit empty
Writer list may still publish the unchanged master for API compatibility. If the
Writer proposes changes but none receive semantic and structural authorization,
the run returns `REJECTED:NO_SAFE_CHANGES`. Only counts, codes, and digests enter
logs or events.

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
digest-bound semantic review, three-vote authorization report, input/draft digests,
and publication ID. It
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
6. Run two independent, citation-bound factual reviews for every non-empty proposal.
7. Structurally compile the supported subset.
8. Run the three fresh code-owned authorization votes on the full draft.
9. Commit the web receipt metadata and verify the exact SQL readback.
10. Return the unchanged legacy JSON response with the verified draft and scores.

No Writer proposal reaches the user before step 9.

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
