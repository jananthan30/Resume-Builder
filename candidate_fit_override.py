"""Manual user override for the candidate-fit gate (candidate-fit-override/v1).

The gate is advisory tooling for the user's own job search; the final call
belongs to the user. An override records a deliberate, attributable decision
to proceed DESPITE a gate rejection. It changes who decides — never what is
true. It therefore:

  - REQUIRES the deterministic gate (and any review report supplied) to have
    rejected the application; overriding a pass is meaningless and errors.
  - REQUIRES an explicit reason from the user (attestation, not a shrug).
  - BINDS the exact gate/review reports and document digests it overrules,
    so the record shows precisely what evidence was set aside.
  - AUTHORIZES ONLY the coordinator-built manual package path: truthful
    content from the configured master, evidence/human-voice/integrity
    audits, non-authorized DOCX renderers, files labeled MANUAL_DRAFT.
  - NEVER authorizes the native runtime, authorization receipts, authorized
    DOCX/tracker wrappers, or any claim that candidate fit passed.

Exit codes: 0 = override recorded; 1 = refused (gate passed, or reason
missing); 2 = input/validation failure.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import stat
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from candidate_fit_preflight import (
    CANDIDATE_FIT_POLICY_VERSION,
    CANDIDATE_FIT_THRESHOLD,
    assess_candidate_fit,
)
from candidate_fit_review import REVIEW_SCHEMA_VERSION
from final_receipt_verifier import canonical_digest

OVERRIDE_SCHEMA_VERSION = "candidate-fit-override/v1"
OVERRIDE_DECISION = "PROCEED_MANUAL"
_MAX_INPUT_BYTES = 2 * 1024 * 1024
_MIN_REASON_CHARS = 20


def _load_bytes(path: str) -> tuple[bytes, str]:
    resolved = Path(path).expanduser()
    before = os.lstat(resolved)
    if stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode):
        raise ValueError(f"input is not a regular non-symlink file: {path}")
    if before.st_size > _MAX_INPUT_BYTES:
        raise ValueError(f"input exceeds {_MAX_INPUT_BYTES} bytes: {path}")
    raw = resolved.read_bytes()
    return raw, hashlib.sha256(raw).hexdigest()


def _load_review_report(path: str) -> dict[str, Any]:
    raw, _ = _load_bytes(path)
    report = json.loads(raw.decode("utf-8"))
    if not isinstance(report, dict):
        raise ValueError("review report is not a JSON object")
    if report.get("schema_version") != REVIEW_SCHEMA_VERSION:
        raise ValueError(f"review report must be {REVIEW_SCHEMA_VERSION}")
    return report


def build_override(
    *,
    resume_path: str,
    job_description_path: str,
    run_id: str,
    case_id: str,
    as_of_date: str,
    reason: str,
    review_report_path: str | None = None,
) -> dict[str, Any]:
    """Record a manual override against a rejected gate. Fail closed otherwise."""
    reason = (reason or "").strip()
    if len(reason) < _MIN_REASON_CHARS:
        raise ValueError(
            f"override requires an explicit reason of at least "
            f"{_MIN_REASON_CHARS} characters — a deliberate decision, not a shrug"
        )

    resume_raw, resume_digest = _load_bytes(resume_path)
    jd_raw, jd_digest = _load_bytes(job_description_path)
    gate_report = assess_candidate_fit(
        resume_raw.decode("utf-8"),
        jd_raw.decode("utf-8"),
        run_id=run_id,
        case_id=case_id,
        as_of_date=as_of_date,
        resume_digest=resume_digest,
        job_description_digest=jd_digest,
        extraction_trustworthy=True,
    )
    if gate_report["passed"]:
        raise ValueError("gate already passes; there is nothing to override")

    review_report: dict[str, Any] | None = None
    review_digest = ""
    if review_report_path:
        review_report = _load_review_report(review_report_path)
        review_digest = canonical_digest(review_report)

    return {
        "schema_version": OVERRIDE_SCHEMA_VERSION,
        "policy_version": CANDIDATE_FIT_POLICY_VERSION,
        "run_id": run_id,
        "case_id": case_id,
        "as_of_date": as_of_date,
        "recorded_at": datetime.now(timezone.utc).isoformat(),
        "resume_digest": resume_digest,
        "job_description_digest": jd_digest,
        "gate_report": gate_report,
        "gate_report_digest": canonical_digest(gate_report),
        "review_report": review_report,
        "review_report_digest": review_digest,
        "override": {
            "decision": OVERRIDE_DECISION,
            "decided_by": "user",
            "reason": reason,
            "gate_threshold": CANDIDATE_FIT_THRESHOLD,
            "gate_score": gate_report["score"],
            "gate_hard_knockouts": [
                knockout["requirement"] for knockout in gate_report["hard_knockouts"]
            ],
            "review_outcome": (review_report or {}).get("outcome", ""),
            "scope": (
                "manual package only: truthful content, deterministic audits, "
                "non-authorized renderers; never the native runtime, receipts, "
                "authorized wrappers, or tracker"
            ),
        },
        "codes": ["MANUAL_OVERRIDE"],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Record a manual user override of a rejected candidate-fit gate"
    )
    parser.add_argument("--resume", required=True)
    parser.add_argument("--job-description", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--case-id", required=True)
    parser.add_argument("--as-of-date", required=True)
    parser.add_argument("--reason", required=True)
    parser.add_argument("--review-report", default=None)
    parser.add_argument("--output", default=None, help="write the record to this path")
    args = parser.parse_args(argv)

    try:
        record = build_override(
            resume_path=args.resume,
            job_description_path=args.job_description,
            run_id=args.run_id,
            case_id=args.case_id,
            as_of_date=args.as_of_date,
            reason=args.reason,
            review_report_path=args.review_report,
        )
    except (OSError, ValueError) as exc:
        print(json.dumps({"error": str(exc)}, sort_keys=True))
        if "nothing to override" in str(exc) or "explicit reason" in str(exc):
            return 1
        return 2

    rendered = json.dumps(record, indent=2, sort_keys=True)
    if args.output:
        destination = Path(args.output)
        destination.write_text(rendered + "\n", encoding="utf-8")
    print(rendered)
    gate = record["gate_report"]
    print(
        "\nWARNING: manual override recorded. The gate recommended REJECT "
        f"(score {gate['score']}/{record['override']['gate_threshold']}, "
        f"hard knockouts: {len(gate['hard_knockouts'])}). Proceeding is your "
        "decision against that recommendation. The package must be labeled "
        "manual, its content must remain truthful, and no authorization "
        "receipt, runtime publication, or authorized tracker row may be "
        "created for it."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
