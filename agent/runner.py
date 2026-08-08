"""Async run bookkeeping for the ResumeHQ standalone agent's ``/agent/*``
endpoints (see ``scorer_server.py``'s "ASYNC AGENT RUNS" section).

Small, generic ``agent_runs`` state-machine helpers -- create / advance /
finish / read one row, always through ``cloud.db.scoped()`` so every query
stays scoped to the caller's ``user_id`` (see ``cloud/db.py``'s guard: every
query here must carry a bare ``:user_id`` placeholder; no quoted literals,
no SQL comments -- every value, not only ``user_id``, is bound as a named
parameter).

``cloud.db`` is imported LAZILY, inside each function body, mirroring
``agent/tools.py``'s own documented reason: nothing in the ``agent`` package
should force a hard import-time dependency on the ``cloud`` package, even
though in practice this module is only ever imported from ``scorer_server.py``
after ``cloud`` has already been confirmed importable.

NOT used by ``agent.tools.run_resume_team`` / ``run_cover_letter``
--------------------------------------------------------------------------
Those two tools (see ``agent/tools.py``) already perform this exact
bookkeeping internally, end to end, against the SAME ``agent_runs`` table and
the SAME run id (``agent.tools.ToolContext.run_id`` doubles as the
``agent_runs.id`` primary key): ``run_resume_team`` inserts its own 'queued'
row, flips it to 'running' partway through the audited pipeline
(``CloudTrustedServices.claim_run``), and finishes it as
'succeeded'/'failed'; ``run_cover_letter`` inserts 'queued' and finishes
'succeeded'/'failed' directly -- it has no intermediate 'running' step, since
it is one synchronous generation call rather than a multi-role pipeline.

``scorer_server.py``'s ``POST /agent/tailor`` and ``POST /agent/cover-letter``
handlers therefore call ``agent.tools.dispatch(...)`` directly for their
background work and rely on ITS bookkeeping. Calling ``create_run()`` /
``finish_run()`` from this module around that same call would attempt a
second ``INSERT``/``UPDATE`` against the identical primary key and fail
(``agent_runs.id`` is a plain, non-upserting ``PRIMARY KEY`` -- see
``cloud/db.py``'s migrations). This module's ``create_run``/``mark_running``/
``finish_run`` exist as the general, directly-tested primitives Task 10 asks
for (and as the API any future ``agent_runs`` kind without its own internal
bookkeeping would use); ``get_run`` is what ``GET /agent/runs/{run_id}``
actually polls with -- it works identically no matter which code path
produced the row.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Any

__all__ = [
    "new_run_id", "create_run", "mark_running", "finish_run", "get_run",
    "reap_orphaned_runs", "INTERRUPTED_ERROR",
]

_VALID_KINDS = ("tailor", "cover_letter")
_VALID_FINISH_STATUSES = ("succeeded", "failed")

# Terminal class recorded for a run that was still in flight when the process
# died. Kept in the internal FAILED: namespace so _friendly_agent_error's
# generic transient copy ("try again in a few minutes") applies -- which for
# this cause is exactly true.
INTERRUPTED_ERROR = "FAILED:INTERRUPTED"


def reap_orphaned_runs(conn: Any) -> int:
    """Fail every run left 'queued' or 'running', and report how many.

    Runs execute in-process via FastAPI BackgroundTasks, so a row in a
    non-terminal state has exactly one owner: the process that enqueued it.
    Once that process is gone -- a deploy, an OOM, a health-check restart, all
    routine on Fly -- nothing will ever advance the row again. It would poll as
    "queued" forever, and because cloud.quotas.month_usage counts every row
    whose status is not 'failed', it would keep occupying a paid slot for the
    rest of the calendar month.

    Marking these failed at startup is therefore both the honest answer to the
    caller and the refund: the quota exclusion is the refund mechanism (see
    cloud/quotas.py). Failing an already-dead run cannot lose work, because
    no in-flight run can survive the restart that triggers this sweep.

    Deliberately NOT scoped to a user_id: this is a cross-tenant maintenance
    sweep at boot, not a request-scoped read, so cloud.db.scoped() -- which
    exists to force per-caller isolation on request paths -- does not apply.

    ASSUMPTION: one process owns this database. That holds for the current
    single-machine deployment (Fly volumes attach to a single machine, and
    min_machines_running is 1). If this ever runs multi-process against shared
    storage, this sweep would kill a peer's live runs and must instead key on
    an owner/heartbeat column.
    """
    cursor = conn.execute(
        """
        UPDATE agent_runs
        SET status = ?, error = ?, finished_at = ?
        WHERE status IN ('queued', 'running')
        """,
        ("failed", INTERRUPTED_ERROR, _now_utc_sql()),
    )
    conn.commit()
    return cursor.rowcount or 0


def new_run_id() -> str:
    """A fresh run identifier: 32 lowercase hex characters, no dashes."""
    return uuid.uuid4().hex


def _now_utc_sql() -> str:
    """UTC timestamp in SQLite's own 'YYYY-MM-DD HH:MM:SS' shape, matching
    ``agent_runs.created_at``'s column default and ``cloud.quotas``' own
    lexicographic range comparisons (see ``agent/tools.py``'s identical
    private helper)."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def create_run(
    conn: Any,
    user_id: int,
    kind: str,
    *,
    run_id: str | None = None,
    input_ref: str = "",
    instruction: str | None = None,
) -> str:
    """Insert a new 'queued' ``agent_runs`` row scoped to ``user_id``.

    Returns the run's id -- either the caller-supplied ``run_id`` or a fresh
    uuid4 hex id minted here when omitted. Raises ``ValueError`` up front for
    a ``kind`` outside ``agent_runs``' own ``CHECK`` constraint, rather than
    surfacing a raw ``sqlite3.IntegrityError`` from the insert below.
    """
    if kind not in _VALID_KINDS:
        raise ValueError(f"unsupported agent_runs kind: {kind!r}")
    from cloud.db import scoped

    run_id = run_id or new_run_id()
    scoped(conn, user_id).q(
        """
        INSERT INTO agent_runs
            (id, user_id, kind, status, input_ref, instruction, created_at)
        VALUES
            (:id, :user_id, :kind, :status, :input_ref, :instruction, :created_at)
        """,
        {
            "id": run_id,
            "kind": kind,
            "status": "queued",
            "input_ref": input_ref,
            "instruction": instruction,
            "created_at": _now_utc_sql(),
        },
    )
    conn.commit()
    return run_id


def mark_running(conn: Any, user_id: int, run_id: str) -> bool:
    """Flip a 'queued' row to 'running'.

    A guarded compare-and-swap (only a ``status = 'queued'`` row owned by
    ``user_id`` moves), so calling this twice, calling it after the run has
    already finished, or calling it as a different user, is a safe no-op.
    Returns whether a row actually transitioned.
    """
    from cloud.db import scoped

    cursor = scoped(conn, user_id).q(
        """
        UPDATE agent_runs
        SET status = :new_status
        WHERE id = :id AND user_id = :user_id AND status = :old_status
        """,
        {"id": run_id, "new_status": "running", "old_status": "queued"},
    )
    conn.commit()
    return cursor.rowcount == 1


def finish_run(
    conn: Any,
    user_id: int,
    run_id: str,
    *,
    status: str,
    result: dict[str, Any] | None = None,
    error: str | None = None,
    tokens_in: int = 0,
    tokens_out: int = 0,
) -> bool:
    """Record a terminal outcome ('succeeded' or 'failed') for a run.

    ``result`` is JSON-encoded (mirrors ``agent/tools.py``'s private
    ``_finish_agent_run``); pass it only on success -- a failure should pass
    ``error`` instead and leave ``result`` as ``None``. Returns whether a row
    matching ``(run_id, user_id)`` actually existed to update.
    """
    if status not in _VALID_FINISH_STATUSES:
        raise ValueError(
            f"finish_run status must be one of {_VALID_FINISH_STATUSES}; got {status!r}"
        )
    from cloud.db import scoped

    cursor = scoped(conn, user_id).q(
        """
        UPDATE agent_runs
        SET status = :status,
            result = :result,
            error = :error,
            tokens_in = :tokens_in,
            tokens_out = :tokens_out,
            finished_at = :finished_at
        WHERE id = :id AND user_id = :user_id
        """,
        {
            "id": run_id,
            "status": status,
            "result": json.dumps(result) if result is not None else None,
            "error": error,
            "tokens_in": int(tokens_in or 0),
            "tokens_out": int(tokens_out or 0),
            "finished_at": _now_utc_sql(),
        },
    )
    conn.commit()
    return cursor.rowcount == 1


def get_run(conn: Any, user_id: int, run_id: str) -> dict[str, Any] | None:
    """Scoped read of one ``agent_runs`` row, or ``None`` if it does not
    exist OR belongs to a different user -- deliberately indistinguishable,
    so a caller (e.g. ``GET /agent/runs/{run_id}``) can map both to one 404
    without ever confirming or denying that a foreign run_id exists.

    ``result`` is JSON-decoded back into a dict when present.
    """
    from cloud.db import scoped

    row = scoped(conn, user_id).q(
        """
        SELECT id, user_id, kind, status, input_ref, instruction, result,
               error, tokens_in, tokens_out, created_at, finished_at
        FROM agent_runs
        WHERE id = :id AND user_id = :user_id
        """,
        {"id": run_id},
    ).fetchone()
    if row is None:
        return None
    data = dict(row)
    if data.get("result"):
        try:
            data["result"] = json.loads(data["result"])
        except (TypeError, ValueError):
            pass
    return data
