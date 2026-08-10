#!/usr/bin/env python3
"""Code-bound verification for native Resume Team authorization receipts.

This module is the downstream trust gate between an authorized ``resume.md``
draft and side-effecting finalization such as DOCX generation or tracker
mutation.  It verifies the public JSON Schema and the cross-object invariants
that JSON Schema cannot express by itself.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
from pathlib import Path
from typing import Any

FINAL_RECEIPT_VERSION = "resume-team-final-receipt/v2"
AUTHORIZATION_VERSION = "resume-team-authorization/v1"
VOTE_VERSION = "resume-team-vote/v1"
CHECK_VERSION = "resume-team-final-receipt-check/v1"
SCHEMA_PATH = Path(__file__).resolve().parent / "schemas" / "resume-team-final-receipt.schema.json"
MAX_RESUME_BYTES = 20 * 1024 * 1024
MAX_JOB_DESCRIPTION_BYTES = 2 * 1024 * 1024
MAX_RECEIPT_BYTES = 1024 * 1024
MAX_SCHEMA_BYTES = 1024 * 1024
_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
# hosts: api = hosted product runtime; codex/claude = legacy local CLI hosts
_NATIVE_AGENT_ID_RE = re.compile(
    r"^(?:codex|claude|api):[A-Za-z0-9._:-]{1,128}$"
)
_RECEIPT_NAME_RE = re.compile(r"^resume-team-receipt\.([0-9a-f]{64})\.json$")
_VOTE_NAMES = ("evidence", "human_voice", "canonical_integrity")
_RECEIPT_KEYS = {
    "schema_version",
    "run_id",
    "case_id",
    "draft_digest",
    "source_digest",
    "job_description_digest",
    "candidate_fit_report",
    "candidate_fit_report_digest",
    "researcher_agent_id",
    "researcher_artifact_digest",
    "auditor_attestation",
    "authorization_digest",
    "authorization_report",
    "vote_invocation_ids",
    "publication_id",
    "verified_target_digest",
}


class FinalReceiptVerificationError(ValueError):
    """A stable, non-sensitive final-receipt verification failure."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    )


def canonical_digest(value: Any) -> str:
    if isinstance(value, bytes):
        encoded = value
    elif isinstance(value, str):
        encoded = value.encode("utf-8")
    else:
        encoded = _canonical_json(value).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _absolute_unresolved(path: str | os.PathLike[str]) -> Path:
    return Path(os.path.abspath(os.path.expanduser(os.fspath(path))))


def _read_regular_file(path: Path, maximum: int, code: str) -> bytes:
    """Read one inode without following a final-component symlink."""

    try:
        before = os.lstat(path)
    except OSError as exc:
        raise FinalReceiptVerificationError(code) from exc
    if stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode):
        raise FinalReceiptVerificationError(code)
    if before.st_size > maximum:
        raise FinalReceiptVerificationError(code)
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise FinalReceiptVerificationError(code) from exc
    try:
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino)
            or opened.st_size > maximum
        ):
            raise FinalReceiptVerificationError(code)
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(descriptor, min(1024 * 1024, maximum + 1 - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > maximum:
                raise FinalReceiptVerificationError(code)
        after = os.fstat(descriptor)
        if (
            (after.st_dev, after.st_ino) != (opened.st_dev, opened.st_ino)
            or after.st_size != opened.st_size
            or after.st_mtime_ns != opened.st_mtime_ns
        ):
            raise FinalReceiptVerificationError(code)
        return b"".join(chunks)
    finally:
        os.close(descriptor)


def _load_public_schema() -> dict[str, Any]:
    raw = _read_regular_file(SCHEMA_PATH, MAX_SCHEMA_BYTES, "SCHEMA_UNAVAILABLE")
    try:
        schema = json.loads(raw.decode("utf-8"))
    except Exception as exc:
        raise FinalReceiptVerificationError("SCHEMA_INVALID") from exc
    if (
        not isinstance(schema, dict)
        or schema.get("$id") != FINAL_RECEIPT_VERSION
        or schema.get("type") != "object"
        or schema.get("additionalProperties") is not False
        or set(schema.get("required", [])) != _RECEIPT_KEYS
    ):
        raise FinalReceiptVerificationError("SCHEMA_INVALID")
    try:
        from jsonschema import Draft202012Validator

        Draft202012Validator.check_schema(schema)
    except ImportError as exc:
        raise FinalReceiptVerificationError("SCHEMA_VALIDATOR_UNAVAILABLE") from exc
    except Exception as exc:
        raise FinalReceiptVerificationError("SCHEMA_INVALID") from exc
    return schema


def _validate_schema(receipt: dict[str, Any], schema: dict[str, Any]) -> None:
    try:
        from jsonschema import Draft202012Validator

        Draft202012Validator(schema).validate(receipt)
    except Exception as exc:
        raise FinalReceiptVerificationError("RECEIPT_SCHEMA") from exc


def verify_final_receipt(
    *,
    resume_path: str | os.PathLike[str],
    receipt_path: str | os.PathLike[str],
    expected_receipt_digest: str,
    config_path: str | os.PathLike[str] = "config.json",
) -> dict[str, Any]:
    """Verify and return one final receipt or raise a stable failure code.

    The resume and receipt must be regular, non-symlink sibling files.  The
    caller-provided digest is mandatory and must be the canonical JSON digest
    returned by the native Resume Team result.  The current configured master
    resume and the fixed sibling ``job_description.txt`` are independently
    re-read and must match the source digests committed by that receipt.
    """

    if not isinstance(expected_receipt_digest, str) or not _DIGEST_RE.fullmatch(
        expected_receipt_digest
    ):
        raise FinalReceiptVerificationError("EXPECTED_DIGEST_INVALID")
    resume = _absolute_unresolved(resume_path)
    receipt_file = _absolute_unresolved(receipt_path)
    try:
        resume_parent = resume.parent.resolve(strict=True)
        receipt_parent = receipt_file.parent.resolve(strict=True)
    except OSError as exc:
        raise FinalReceiptVerificationError("PATH_CONTAINMENT") from exc
    if resume_parent != receipt_parent:
        raise FinalReceiptVerificationError("PATH_CONTAINMENT")
    filename_match = _RECEIPT_NAME_RE.fullmatch(receipt_file.name)
    if filename_match is None:
        raise FinalReceiptVerificationError("RECEIPT_FILENAME")

    resume_bytes = _read_regular_file(resume, MAX_RESUME_BYTES, "RESUME_ARTIFACT")
    receipt_bytes = _read_regular_file(
        receipt_file, MAX_RECEIPT_BYTES, "RECEIPT_ARTIFACT"
    )
    try:
        resume_bytes.decode("utf-8")
        receipt = json.loads(receipt_bytes.decode("utf-8"))
    except Exception as exc:
        raise FinalReceiptVerificationError("ARTIFACT_ENCODING") from exc
    if not isinstance(receipt, dict):
        raise FinalReceiptVerificationError("RECEIPT_SCHEMA")
    schema = _load_public_schema()
    _validate_schema(receipt, schema)
    try:
        canonical_receipt_bytes = _canonical_json(receipt).encode("utf-8")
    except Exception as exc:
        raise FinalReceiptVerificationError("RECEIPT_CANONICALIZATION") from exc
    if receipt_bytes != canonical_receipt_bytes:
        raise FinalReceiptVerificationError("RECEIPT_NOT_CANONICAL")
    receipt_digest = canonical_digest(receipt)
    if receipt_digest != expected_receipt_digest:
        raise FinalReceiptVerificationError("RECEIPT_DIGEST_MISMATCH")

    expected_filename_digest = canonical_digest(
        {"run_id": receipt["run_id"], "case_id": receipt["case_id"]}
    )
    if filename_match.group(1) != expected_filename_digest:
        raise FinalReceiptVerificationError("RECEIPT_FILENAME_BINDING")

    resume_digest = canonical_digest(resume_bytes)
    if (
        receipt["draft_digest"] != resume_digest
        or receipt["verified_target_digest"] != resume_digest
    ):
        raise FinalReceiptVerificationError("RESUME_DIGEST_MISMATCH")

    try:
        # Reuse the runtime's safe, format-aware snapshot loader so DOCX master
        # extraction has exactly the same byte-to-text semantics at publication
        # and at every downstream side-effect boundary.
        from native_resume_team import load_master_snapshot

        current_master = load_master_snapshot(
            config_path,
            require_canonical_layout=False,
        )
    except Exception as exc:
        raise FinalReceiptVerificationError("SOURCE_ARTIFACT") from exc
    if receipt["source_digest"] != current_master.text_digest:
        raise FinalReceiptVerificationError("SOURCE_DIGEST_MISMATCH")

    job_path = resume_parent / "job_description.txt"
    job_bytes = _read_regular_file(
        job_path,
        MAX_JOB_DESCRIPTION_BYTES,
        "JOB_DESCRIPTION_ARTIFACT",
    )
    try:
        job_text = job_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise FinalReceiptVerificationError("JOB_DESCRIPTION_ARTIFACT") from exc
    if not job_text.strip():
        raise FinalReceiptVerificationError("JOB_DESCRIPTION_ARTIFACT")
    if receipt["job_description_digest"] != canonical_digest(job_text):
        raise FinalReceiptVerificationError("JOB_DESCRIPTION_DIGEST_MISMATCH")

    fit_report = receipt["candidate_fit_report"]
    if canonical_digest(fit_report) != receipt["candidate_fit_report_digest"]:
        raise FinalReceiptVerificationError("CANDIDATE_FIT_REPORT_MISMATCH")
    try:
        from multi_agent_team import validate_recomputed_candidate_fit_report

        fit_valid, fit_passed = validate_recomputed_candidate_fit_report(
            fit_report,
            run_id=receipt["run_id"],
            case_id=receipt["case_id"],
            master_resume=current_master.text,
            job_description=job_text,
        )
    except Exception as exc:
        raise FinalReceiptVerificationError(
            "CANDIDATE_FIT_REPORT_MISMATCH"
        ) from exc
    if not fit_valid or not fit_passed:
        raise FinalReceiptVerificationError("CANDIDATE_FIT_REPORT_MISMATCH")

    researcher_agent_id = receipt["researcher_agent_id"]
    researcher_artifact_digest = receipt["researcher_artifact_digest"]
    auditor_agent_id = receipt["auditor_attestation"]["agent_id"]
    if (
        _NATIVE_AGENT_ID_RE.fullmatch(researcher_agent_id) is None
        or _NATIVE_AGENT_ID_RE.fullmatch(auditor_agent_id) is None
        or researcher_agent_id.split(":", 1)[0]
        != auditor_agent_id.split(":", 1)[0]
        or researcher_agent_id == auditor_agent_id
        or _DIGEST_RE.fullmatch(researcher_artifact_digest) is None
    ):
        raise FinalReceiptVerificationError("RESEARCHER_ATTESTATION_MISMATCH")
    auditor = receipt["auditor_attestation"]
    if auditor["verdict"] != "PASS" or auditor["draft_digest"] != resume_digest:
        raise FinalReceiptVerificationError("AUDITOR_ATTESTATION_MISMATCH")

    report = receipt["authorization_report"]
    if (
        report["schema_version"] != AUTHORIZATION_VERSION
        or report["passed"] is not True
        or report["codes"] != []
        or report["findings"] != []
        or report["draft_digest"] != resume_digest
        or canonical_digest(report) != receipt["authorization_digest"]
    ):
        raise FinalReceiptVerificationError("AUTHORIZATION_REPORT_MISMATCH")
    votes = report["votes"]
    if len(votes) != 3:
        raise FinalReceiptVerificationError("AUTHORIZATION_VOTES_MISMATCH")
    vote_ids: list[str] = []
    for expected_name, vote in zip(_VOTE_NAMES, votes):
        if (
            vote["schema_version"] != VOTE_VERSION
            or vote["name"] != expected_name
            or vote["passed"] is not True
            or vote["codes"] != []
            or vote["draft_digest"] != resume_digest
        ):
            raise FinalReceiptVerificationError("AUTHORIZATION_VOTES_MISMATCH")
        vote_ids.append(vote["invocation_id"])
    if len(set(vote_ids)) != 3 or receipt["vote_invocation_ids"] != vote_ids:
        raise FinalReceiptVerificationError("AUTHORIZATION_VOTE_IDS_MISMATCH")
    return receipt


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Verify a native Resume Team receipt")
    parser.add_argument("--resume", required=True)
    parser.add_argument("--receipt", required=True)
    parser.add_argument("--expected-receipt-digest", required=True)
    parser.add_argument("--config", default="config.json")
    args = parser.parse_args(argv)
    try:
        receipt = verify_final_receipt(
            resume_path=args.resume,
            receipt_path=args.receipt,
            expected_receipt_digest=args.expected_receipt_digest,
            config_path=args.config,
        )
        result = {
            "schema_version": CHECK_VERSION,
            "verified": True,
            "code": "OK",
            "draft_digest": receipt["draft_digest"],
            "authorization_receipt_digest": canonical_digest(receipt),
        }
    except FinalReceiptVerificationError as exc:
        result = {
            "schema_version": CHECK_VERSION,
            "verified": False,
            "code": exc.code,
            "draft_digest": "",
            "authorization_receipt_digest": "",
        }
    print(_canonical_json(result))
    return 0 if result["verified"] else 1


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "CHECK_VERSION",
    "FinalReceiptVerificationError",
    "canonical_digest",
    "verify_final_receipt",
]
