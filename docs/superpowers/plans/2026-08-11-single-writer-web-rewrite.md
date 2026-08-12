# Single-Writer Web Rewrite Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `POST /rewrite` use one Sonnet 5 Writer with deterministic research, strict source-anchored salvage, deterministic authorization, and no AI Editor while preserving the existing audited publication protocol.

**Architecture:** Add a web-only composed adapter that synthesizes validated Researcher and Auditor protocol envelopes and calls `AnthropicHost` only for Writer. Add a coordinator-owned salvage compiler that greedily retains only replacements that pass the existing strict compiler plus canonical integrity. Select that adapter only when `/rewrite` passes `pipeline_mode="single_writer"`; the default `/agent/tailor` and native four-role paths remain unchanged.

**Tech Stack:** Python 3.11+, FastAPI, Anthropic Python SDK, SQLite, pytest, existing `multi_agent_team`, `resume_integrity_audit`, `CloudTrustedServices`, and Fly.io.

## Global Constraints

- The only hosted writing model is exactly `claude-sonnet-5`.
- Researcher and Auditor protocol steps make no model calls; Editor is disabled.
- Candidate-fit, quota, source attestation, three deterministic authorization votes, receipt commit, and verified readback remain mandatory.
- Invalid replacement proposals are discarded, never relaxed, rewritten, or exposed.
- The saved master resume remains immutable; every accepted change is anchored to one exact unique complete master line.
- Resumes, JDs, replacement text, provider replies, and exception messages containing them never enter logs or events.
- The public `/rewrite` success JSON shape remains unchanged.
- The existing default four-role adapter behavior remains unchanged for callers that do not request single-writer mode.
- Preserve the user-owned untracked `uv.lock`.

---

## File map

- `multi_agent_team.py`: typed no-safe-changes signal, strict greedy salvage function, coordinator terminal mapping, and optional safe Writer stats in milestone events.
- `agent/adapter.py`: `SingleWriterTeamAdapter`, deterministic Researcher and Auditor protocol envelopes, direct Writer host call, and Editor refusal.
- `agent/tools.py`: web-only pipeline mode selection, single-writer adapter factory, zero Editor attempts, token/run/event bookkeeping.
- `scorer_server.py`: request single-writer mode from `/rewrite` and map safety rejections to actionable 422 copy.
- `tests/test_multi_agent_team.py`: salvage, typed terminal, integrity, ordering, and observability regressions.
- `tests/test_single_writer_adapter.py`: composed-adapter role routing, identities, publication, and no semantic repair.
- `tests/test_team_via_api_host.py`: unchanged legacy hosted four-role parity coverage.
- `tests/test_agent_tools.py`: default-mode isolation and single-writer run bookkeeping.
- `tests/test_rewrite_alias.py`: endpoint mode selection, response compatibility, and honest rejection copy.

---

### Task 1: Strict greedy replacement salvage

**Files:**
- Modify: `multi_agent_team.py:150-180, 957-1030, 2250-2320, 2545-2565, 2700-2725`
- Test: `tests/test_multi_agent_team.py:610-1045, 1100-1160`

**Interfaces:**
- Produces: `NoSafeWriterChanges(stats: dict[str, Any])`.
- Produces: `compile_writer_replacements_salvage(master: str, replacements: Any) -> tuple[dict[str, Any], dict[str, Any]]`.
- Stats shape: exactly `{"proposed_count": int, "accepted_count": int, "rejected_count": int, "rejection_codes": dict[str, int]}`.
- Preserves: `_compile_writer_replacements(master, replacements)` unchanged for legacy/native normalization.

- [ ] **Step 1: Write failing salvage and integrity tests**

Add tests that use the existing production-shaped resume fixtures and assert:

```python
def test_salvage_keeps_safe_replacement_when_sibling_breaks_ownership():
    safe = {
        "source_span_text": SAFE_SOURCE_BULLET,
        "replacement_text": SAFE_SUPPORTED_REWRITE,
    }
    unsafe = {
        "source_span_text": OTHER_SOURCE_BULLET,
        "replacement_text": SAFE_SUPPORTED_REWRITE,
    }
    payload, stats = compile_writer_replacements_salvage(
        master_resume(), [unsafe, safe]
    )
    assert SAFE_SUPPORTED_REWRITE in payload["draft"]
    assert OTHER_SOURCE_BULLET in payload["draft"]
    assert stats == {
        "proposed_count": 2,
        "accepted_count": 1,
        "rejected_count": 1,
        "rejection_codes": {"STRICT_COMPILER": 1},
    }


def test_salvage_rejects_protected_fact_change_before_publication():
    with pytest.raises(NoSafeWriterChanges) as caught:
        compile_writer_replacements_salvage(
            master_resume(),
            [{
                "source_span_text": "Senior Safety Scientist | Acme",
                "replacement_text": "Director of Safety | Acme",
            }],
        )
    assert caught.value.stats["rejection_codes"] == {"CANONICAL_INTEGRITY": 1}


def test_salvage_distinguishes_explicit_empty_list_from_all_rejected():
    payload, stats = compile_writer_replacements_salvage(master_resume(), [])
    assert payload == {"draft": master_resume(), "claim_evidence": []}
    assert stats["proposed_count"] == stats["accepted_count"] == 0

    with pytest.raises(NoSafeWriterChanges):
        compile_writer_replacements_salvage(master_resume(), [{"bad": "shape"}])
```

Also test source-order determinism by passing the same independent replacements in both input orders and asserting identical draft, evidence, and stats.

- [ ] **Step 2: Run the focused tests and capture RED**

Run:

```bash
pytest -q \
  tests/test_multi_agent_team.py::test_salvage_keeps_safe_replacement_when_sibling_breaks_ownership \
  tests/test_multi_agent_team.py::test_salvage_rejects_protected_fact_change_before_publication \
  tests/test_multi_agent_team.py::test_salvage_distinguishes_explicit_empty_list_from_all_rejected
```

Expected: collection/import failure because `NoSafeWriterChanges` and `compile_writer_replacements_salvage` do not exist.

- [ ] **Step 3: Add the typed signal and salvage implementation**

Add a public typed signal near `AgentInvocationFailure`:

```python
class NoSafeWriterChanges(RuntimeError):
    def __init__(self, stats: dict[str, Any]):
        self.stats = json.loads(_canonical_json(stats))
        super().__init__("writer proposed no safe source-supported changes")
```

Implement a private stable category mapper that emits only these values:
`INVALID_ITEM`, `INVALID_ANCHOR`, `STRICT_COMPILER`, and
`CANONICAL_INTEGRITY`. Implement salvage as follows:

```python
def compile_writer_replacements_salvage(
    master: str,
    replacements: Any,
) -> tuple[dict[str, Any], dict[str, Any]]:
    if not isinstance(master, str) or not isinstance(replacements, list):
        raise ValueError("invalid writer replacements")
    if not replacements:
        return _compile_writer_replacements(master, []), {
            "proposed_count": 0,
            "accepted_count": 0,
            "rejected_count": 0,
            "rejection_codes": {},
        }

    candidates: list[tuple[int, int, dict[str, str]]] = []
    rejected: Counter[str] = Counter()
    for ordinal, item in enumerate(replacements):
        if not isinstance(item, dict) or set(item) != {
            "source_span_text", "replacement_text"
        }:
            rejected["INVALID_ITEM"] += 1
            continue
        source = item["source_span_text"]
        if not isinstance(source, str):
            rejected["INVALID_ITEM"] += 1
            continue
        try:
            start, _, _ = _unique_span(master, source)
            _compile_writer_replacements(master, [item])
        except Exception:
            rejected["INVALID_ANCHOR"] += 1
            continue
        candidates.append((start, ordinal, item))

    accepted: list[dict[str, str]] = []
    accepted_starts: set[int] = set()
    compiled = _compile_writer_replacements(master, [])
    for start, _, item in sorted(candidates):
        if start in accepted_starts:
            rejected["INVALID_ANCHOR"] += 1
            continue
        try:
            proposed = _compile_writer_replacements(master, [*accepted, item])
        except Exception:
            rejected["STRICT_COMPILER"] += 1
            continue
        integrity = resume_integrity_audit.audit_resume_text(
            master, proposed["draft"]
        )
        if integrity.get("passed") is not True:
            rejected["CANONICAL_INTEGRITY"] += 1
            continue
        accepted.append(item)
        accepted_starts.add(start)
        compiled = proposed

    stats = {
        "proposed_count": len(replacements),
        "accepted_count": len(accepted),
        "rejected_count": len(replacements) - len(accepted),
        "rejection_codes": dict(sorted(rejected.items())),
    }
    if not accepted:
        raise NoSafeWriterChanges(stats)
    return compiled, stats
```

Import `resume_integrity_audit` at module scope; it is already a local deterministic module. Refine the candidate pre-validation so a structurally valid replacement rejected only by combined ownership is categorized `STRICT_COMPILER`, not `INVALID_ANCHOR`. Never include exception text in stats.

- [ ] **Step 4: Map the typed signal in the coordinator and expose safe stats**

In `run_team.invoke_role`, catch `NoSafeWriterChanges` before the broad
exception handlers. Return `fail("REJECTED:NO_SAFE_CHANGES")` only when
`role == "writer"`; map the signal from any other role to
`FAILED:AGENT_CRASH`.

Before `writer_complete`, read an optional `writer_stats` property from the
adapter. Admit stats only when the value has the exact key set above, all
counts are non-negative integers, count arithmetic agrees, and
`rejection_codes` contains only non-empty string keys with non-negative integer
values. Add the validated stats object under `writer_stats`; otherwise emit the
existing event unchanged.

- [ ] **Step 5: Run focused and coordinator tests GREEN**

Run:

```bash
pytest -q tests/test_multi_agent_team.py -k "salvage or no_safe or writer_complete"
```

Expected: all selected tests pass.

- [ ] **Step 6: Commit Task 1**

```bash
git add multi_agent_team.py tests/test_multi_agent_team.py
git commit -m "feat(team): salvage safe writer replacements"
```

---

### Task 2: Composed single-writer adapter

**Files:**
- Modify: `agent/adapter.py:70-260`
- Create: `tests/test_single_writer_adapter.py`
- Test unchanged behavior: `tests/test_team_via_api_host.py:1-430`

**Interfaces:**
- Consumes: `compile_writer_replacements_salvage`, `NoSafeWriterChanges`, `normalize_native_payload`, `build_handoff`, and `validate_handoff` from Task 1.
- Produces: `SingleWriterTeamAdapter(host: AnthropicHost)` with `.host`, `.invoke(role, context, timeout_seconds)`, and read-only `.writer_stats`.
- Deterministic IDs: `api:researcher.<run_id>.<attempt>.det` and `api:auditor.<run_id>.<attempt>.det`.

- [ ] **Step 1: Write failing composed-adapter tests**

Create `tests/test_single_writer_adapter.py`, reuse the production-shaped
fixtures from `tests/test_team_via_api_host.py` and
`tests/test_multi_agent_team.py`, and prove one complete run publishes with a
fake client scripted with only one Writer response:

```python
def test_single_writer_adapter_calls_anthropic_only_for_writer_and_publishes():
    client = FakeClient([make_response({"replacements": []})])
    adapter = SingleWriterTeamAdapter(host=AnthropicHost(client=client))
    req = request(master_resume=_TRACED_RESUME, max_editor_attempts=0)

    result = run_team(req, adapter, Services())

    assert result["terminal_class"] == "PUBLISHED"
    assert [call["model"] for call in client.calls] == ["claude-sonnet-5"]
    assert len(client.calls) == 1
    receipt = result["authorization_receipt"]
    assert receipt["researcher_agent_id"].endswith(".det")
    assert receipt["auditor_attestation"]["agent_id"].endswith(".det")
    assert receipt["researcher_agent_id"].startswith("api:researcher.")
    assert receipt["auditor_attestation"]["agent_id"].startswith("api:auditor.")
```

Add tests that:

- the deterministic Researcher includes only unique complete byte-identical JD lines;
- Writer mixed safe/unsafe proposals publish the safe subset with one API call;
- proposed-but-none-safe returns `REJECTED:NO_SAFE_CHANGES` without semantic repair;
- direct Editor invocation raises `PermissionError` without a host call;
- a malformed JSON Writer reply may use only the host's syntax repair and never the adapter's semantic repair;
- invocation identity reuse fails closed;
- `writer_stats` contains counts only and no source/replacement text.

- [ ] **Step 2: Run the focused adapter tests and capture RED**

Run:

```bash
pytest -q tests/test_single_writer_adapter.py
```

Expected: import failure because `SingleWriterTeamAdapter` does not exist.

- [ ] **Step 3: Add shared context and envelope helpers**

Add private helpers used only by the new adapter; do not refactor or alter
`AnthropicTeamAdapter` in this task:

```python
def _context_fields(role: str, context: dict[str, Any]) -> tuple[str, str, int, dict[str, Any]]:
    if (
        role not in ROLE_ORDER
        or not isinstance(context, dict)
        or context.get("role") != role
    ):
        raise ValueError("invalid role context")
    run_id = context.get("run_id")
    case_id = context.get("case_id")
    attempt = context.get("attempt")
    payload = context.get("payload")
    if (
        not isinstance(run_id, str)
        or not run_id
        or not isinstance(case_id, str)
        or not case_id
        or type(attempt) is not int
        or not isinstance(payload, dict)
    ):
        raise ValueError("invalid role context")
    return run_id, case_id, attempt, payload


def _validated_envelope(
    role: str,
    context: dict[str, Any],
    agent_id: str,
    normalized: dict[str, Any],
) -> dict[str, Any]:
    envelope = build_handoff(
        context=context, role=role, agent_id=agent_id, payload=normalized
    )
    if validate_handoff(role, envelope, context).get("valid") is not True:
        raise AgentInvocationFailure("AGENT_PAYLOAD_SCHEMA")
    return envelope
```

- [ ] **Step 4: Implement deterministic Researcher payload construction**

Add `_deterministic_researcher_raw(job_description: str) -> dict[str, Any]`:

- split only on LF;
- retain nonblank, alphanumeric, non-separator lines;
- retain only lines whose stripped value appears exactly once in the JD;
- exclude heading-only lines matching common `requirements`, `qualifications`, `responsibilities`, `about`, and `benefits` headings;
- classify mandatory cues with a compiled case-insensitive regex into hard requirements;
- put all other retained lines into soft requirements so the Writer receives complete source context;
- preserve line bodies byte-for-byte;
- require at least one retained line or raise `AgentInvocationFailure("AGENT_PAYLOAD_SCHEMA")`;
- return one matching `{"evidence_text": line}` entry per hard-then-soft requirement.

Pass the raw object through `normalize_native_payload("researcher", raw, context)` before building the deterministic envelope.

- [ ] **Step 5: Implement `SingleWriterTeamAdapter`**

Implement role routing:

```python
class SingleWriterTeamAdapter:
    def __init__(self, host: AnthropicHost) -> None:
        if not isinstance(host, AnthropicHost):
            raise ValueError("host must be an AnthropicHost")
        self.host = host
        self._seen_invocations: set[str] = set()
        self._writer_stats: dict[str, Any] | None = None

    @property
    def writer_stats(self) -> dict[str, Any] | None:
        return json.loads(json.dumps(self._writer_stats)) if self._writer_stats else None

    def invoke(self, role, context, timeout_seconds):
        run_id, case_id, attempt, payload = _context_fields(role, context)
        if role == "editor":
            raise PermissionError("single-writer mode does not permit Editor")
        if role == "researcher":
            raw = _deterministic_researcher_raw(payload["job_description"])
            normalized = normalize_native_payload(role, raw, context)
            agent_id = f"api:researcher.{run_id}.{attempt}.det"
        elif role == "auditor":
            raw = {
                "verdict": "PASS",
                "findings": [],
                "audited_draft": payload["writer_draft"],
            }
            normalized = normalize_native_payload(role, raw, context)
            agent_id = f"api:auditor.{run_id}.{attempt}.det"
        else:
            try:
                raw = self.host.run_role(
                    "writer", payload, case_id=case_id, run_id=run_id
                )
            except (HostRefusal, BudgetExceeded) as error:
                raise ConnectionError("Anthropic Writer unavailable") from error
            if not isinstance(raw, dict) or set(raw) != {"replacements"}:
                raise AgentInvocationFailure("AGENT_PAYLOAD_SCHEMA")
            normalized, self._writer_stats = compile_writer_replacements_salvage(
                payload["master_resume"], raw["replacements"]
            )
            agent_id = f"api:writer.{run_id}.{attempt}"
        if agent_id in self._seen_invocations:
            raise LookupError("reused api invocation identity")
        envelope = _validated_envelope(role, context, agent_id, normalized)
        self._seen_invocations.add(agent_id)
        return envelope
```

Accept `timeout_seconds` for interface compatibility. Do not log `raw`, `payload`, exception text, source anchors, or replacement text. Add `SingleWriterTeamAdapter` to `__all__`.

- [ ] **Step 6: Run adapter and legacy parity suites GREEN**

Run:

```bash
pytest -q \
  tests/test_single_writer_adapter.py \
  tests/test_team_via_api_host.py \
  tests/test_host_anthropic_runtime.py
```

Expected: all tests pass, including legacy `AnthropicTeamAdapter` repair behavior and new one-model-role behavior.

- [ ] **Step 7: Commit Task 2**

```bash
git add -f agent/adapter.py tests/test_single_writer_adapter.py
git commit -m "feat(agent): add deterministic single-writer adapter"
```

---

### Task 3: Web-only mode and honest error mapping

**Files:**
- Modify: `agent/tools.py:50-55, 710-730, 862-1010, 1470-1490`
- Modify: `scorer_server.py:2125-2140, 2159-2355`
- Test: `tests/test_agent_tools.py:414-540`
- Test: `tests/test_rewrite_alias.py:120-180, 310-410`

**Interfaces:**
- Consumes: `SingleWriterTeamAdapter` from Task 2.
- Produces: `run_resume_team(ctx: ToolContext, jd_text: str, instruction: str | None = None, resume_text: str | None = None, pipeline_mode: str = "four_role") -> dict` where allowed mode values are exactly `four_role` and `single_writer`.
- Produces: `_default_single_writer_adapter() -> SingleWriterTeamAdapter`.
- Preserves: `_default_team_adapter()` and default callers unchanged.

- [ ] **Step 1: Write failing mode-isolation and endpoint tests**

In `tests/test_agent_tools.py`, add tests asserting:

```python
def test_single_writer_mode_uses_single_adapter_and_zero_editor_attempts(
    conn, monkeypatch
):
    user_id = _make_user(conn)
    monkeypatch.setattr(
        cloud.auth,
        "get_resume",
        lambda uid: {"resume_text": _TRACED_RESUME, "filename": "r.txt"},
    )
    budget = SimpleNamespace(input_tokens=0, output_tokens=0)
    single_adapter = SimpleNamespace(host=SimpleNamespace(budget=budget))
    monkeypatch.setattr(
        tools, "_default_single_writer_adapter", lambda: single_adapter
    )
    monkeypatch.setattr(
        tools,
        "_default_team_adapter",
        lambda: pytest.fail("four-role factory used in single-writer mode"),
    )
    captured = {}

    def fake_run_team(request, adapter, services):
        captured.update(request)
        assert adapter is single_adapter
        return {"terminal_class": "FAILED:WRITER_SCHEMA"}

    monkeypatch.setattr(tools.multi_agent_team, "run_team", fake_run_team)
    ctx = ToolContext(
        user_id=user_id, tier="pro", conn=conn, run_id=_fresh_run_id()
    )
    result = dispatch(
        "run_resume_team", ctx, jd_text=PASSING_JD,
        pipeline_mode="single_writer",
    )
    assert result["status"] == "failed"
    assert captured["max_editor_attempts"] == 0


def test_default_run_resume_team_still_uses_four_role_adapter(conn, monkeypatch):
    user_id = _make_user(conn)
    monkeypatch.setattr(
        cloud.auth,
        "get_resume",
        lambda uid: {"resume_text": _TRACED_RESUME, "filename": "r.txt"},
    )
    calls = {"team": 0, "single": 0}
    budget = SimpleNamespace(input_tokens=0, output_tokens=0)
    team_adapter = SimpleNamespace(host=SimpleNamespace(budget=budget))
    monkeypatch.setattr(
        tools,
        "_default_team_adapter",
        lambda: (calls.__setitem__("team", calls["team"] + 1) or team_adapter),
    )
    monkeypatch.setattr(
        tools,
        "_default_single_writer_adapter",
        lambda: (calls.__setitem__("single", calls["single"] + 1) or team_adapter),
    )
    monkeypatch.setattr(
        tools.multi_agent_team,
        "run_team",
        lambda request, adapter, services: {"terminal_class": "FAILED:WRITER_SCHEMA"},
    )
    ctx = ToolContext(
        user_id=user_id, tier="pro", conn=conn, run_id=_fresh_run_id()
    )
    dispatch("run_resume_team", ctx, jd_text=PASSING_JD)
    assert calls == {"team": 1, "single": 0}


@pytest.mark.parametrize("mode", ["", "single-writer", "unknown", None])
def test_run_resume_team_rejects_unknown_pipeline_mode_before_claiming_slot(
    mode, conn
):
    user_id = _make_user(conn)
    ctx = ToolContext(
        user_id=user_id, tier="pro", conn=conn, run_id=_fresh_run_id()
    )
    with pytest.raises(ValueError, match="pipeline_mode"):
        dispatch("run_resume_team", ctx, jd_text=PASSING_JD, pipeline_mode=mode)
    count = conn.execute(
        "SELECT COUNT(*) FROM agent_runs WHERE id = ?", (ctx.run_id,)
    ).fetchone()[0]
    assert count == 0
```

In `tests/test_rewrite_alias.py`, prove `/rewrite` passes
`pipeline_mode="single_writer"`, a successful call preserves the existing JSON
shape, `REJECTED:NO_SAFE_CHANGES` returns 422 without “few minutes,” and a
generic deterministic `REJECTED:HUMAN_VOICE_AUDIT_FAILED` also returns honest
422 safety copy without exposing the code.

- [ ] **Step 2: Run focused tests and capture RED**

Run:

```bash
pytest -q \
  tests/test_agent_tools.py -k "single_writer or pipeline_mode" \
  tests/test_rewrite_alias.py -k "single_writer or no_safe or safety_rejection"
```

Expected: failures because the mode, factory, and error mapping do not exist.

- [ ] **Step 3: Add the single-writer factory and mode selection**

Import `SingleWriterTeamAdapter`. Add:

```python
def _default_single_writer_adapter() -> SingleWriterTeamAdapter:
    return SingleWriterTeamAdapter(host=AnthropicHost())
```

Extend `run_resume_team` with `pipeline_mode: str = "four_role"`. Validate the
mode before quota lookup, resume loading, DB connection mutation, or run-slot
claim. Choose:

```python
single_writer = pipeline_mode == "single_writer"
request["max_editor_attempts"] = 0 if single_writer else _TAILOR_MAX_EDITOR_ATTEMPTS
adapter = (
    _default_single_writer_adapter()
    if single_writer
    else _default_team_adapter()
)
```

Do not publish `pipeline_mode` in the external tool schema; it is an internal,
explicit caller option. Keep `.host.budget` token accounting unchanged.

Add `pipeline_mode` to `tailor_run_succeeded` and `tailor_run_failed` event
payloads. It is a fixed enum, not user data.

- [ ] **Step 4: Wire only `/rewrite` to single-writer mode**

Change only the synchronous endpoint dispatch:

```python
result = agent_tools.dispatch(
    "run_resume_team",
    ctx,
    jd_text=jd_text,
    resume_text=provided_resume or None,
    pipeline_mode="single_writer",
)
```

Do not change `_run_tailor_in_background`; `/agent/tailor` keeps the default
four-role path.

- [ ] **Step 5: Add actionable safety-rejection copy**

Add a constant that contains no internal terminology:

```python
_NO_SAFE_TAILOR_DETAIL = (
    "We reviewed the job against your resume, but couldn't make a safe "
    "source-supported change. Your resume was left unchanged. If it is "
    "missing relevant experience, update it and try again."
)
```

After the candidate-fit and Researcher special cases, map
`REJECTED:NO_SAFE_CHANGES` to 422 with this copy. Map other non-fit
`REJECTED:*` terminals to 422 copy stating that deterministic safety checks
stopped the draft and the saved resume was unchanged. Never return the raw
terminal.

Mirror this behavior in `_friendly_agent_error` so future polling clients do
not receive false transient advice.

- [ ] **Step 6: Run endpoint and tool suites GREEN**

Run:

```bash
pytest -q tests/test_agent_tools.py tests/test_rewrite_alias.py
```

Expected: all tests pass; existing async/default-mode assertions remain unchanged.

- [ ] **Step 7: Commit Task 3**

```bash
git add agent/tools.py scorer_server.py tests/test_agent_tools.py tests/test_rewrite_alias.py
git commit -m "feat(rewrite): use one hosted writer"
```

---

### Task 4: Full verification and independent review

**Files:**
- Modify only if tests or review reveal a requirement-level defect.

**Interfaces:**
- Verifies every interface produced by Tasks 1-3 as one production-shaped path.

- [ ] **Step 1: Run focused production-shaped regression**

Run:

```bash
pytest -q \
  tests/test_multi_agent_team.py \
  tests/test_team_via_api_host.py \
  tests/test_agent_tools.py \
  tests/test_rewrite_alias.py \
  tests/test_host_anthropic_runtime.py
```

Expected: all pass.

- [ ] **Step 2: Run the complete repository suite**

Use the project Python 3.11 environment:

```bash
python3.11 -m pytest -q
```

Expected: all configured tests pass, with only the repository's documented skip.

- [ ] **Step 3: Run syntax and diff checks**

```bash
python3.11 -m py_compile multi_agent_team.py agent/adapter.py agent/tools.py scorer_server.py
git diff --check
```

Expected: both commands exit 0.

- [ ] **Step 4: Request independent code review**

Ask a fresh reviewer to verify:

- exactly one hosted model role is invoked in single-writer mode;
- all final publication gates remain mandatory;
- invalid replacements cannot bypass strict claim support or canonical integrity;
- default four-role paths are unchanged;
- no PII enters logs/stats;
- no unchanged draft is published after proposed-but-rejected changes;
- all deterministic rejection copy is honest and code-free.

Fix every Critical or Important finding with a new failing regression test and rerun Steps 1-3.

- [ ] **Step 5: Commit review fixes if any**

```bash
git add multi_agent_team.py agent/adapter.py agent/tools.py scorer_server.py tests
git commit -m "fix(rewrite): close single-writer review gaps"
```

Skip this commit only when the reviewer reports PASS with no code changes.

---

### Task 5: Deploy and prove the web path

**Files:**
- No source changes expected.

**Interfaces:**
- Deploys the verified commit to Fly app `resume-scorer`.

- [ ] **Step 1: Push the verified commit**

```bash
git push origin master
```

Expected: remote `master` advances to the verified implementation commit.

- [ ] **Step 2: Deploy without the stalled Depot builder**

```bash
fly deploy -a resume-scorer --remote-only --depot=false
```

Expected: image push succeeds, machine reaches a good state, and Fly reports DNS verified.

- [ ] **Step 3: Verify release and health**

```bash
fly status -a resume-scorer
curl -fsS https://resume-scorer.fly.dev/health
```

Expected: one started machine, 1/1 passing check, and health JSON with
`"status":"ok"` and `agent.features_available=true`.

- [ ] **Step 4: Run a production-sized synthetic single-writer pipeline**

Through `fly ssh console`, use a synthetic 9,000-12,000 character resume and a
synthetic JD. Invoke the same `run_resume_team(ctx, jd_text,
resume_text=synthetic_resume, pipeline_mode="single_writer")` service seam with an isolated temporary SQLite
database or the production-sized adapter/controller test harness; do not read or
write a real user's resume, JD, quota, or run row.

Print only:

```json
{
  "terminal_class": "PUBLISHED",
  "model": "claude-sonnet-5",
  "model_role_calls": ["writer"],
  "writer_proposed_count": 1,
  "writer_accepted_count": 1,
  "published": true
}
```

Expected: exactly one model role, verified publication, and no personal content in output.

- [ ] **Step 5: Verify milestones and final status**

Inspect only the synthetic run's safe metadata. Require ordered milestones
`research_complete`, `writer_complete`, `audit_complete`, `pre_publish`, and
`tailor_run_succeeded`. Confirm the Writer event reports
`pipeline_mode=single_writer` and no resume/JD/replacement fields.

- [ ] **Step 6: Final repository check**

```bash
git status --short --branch
```

Expected: local `master` equals `origin/master`, with only the pre-existing user-owned
untracked `uv.lock`.
