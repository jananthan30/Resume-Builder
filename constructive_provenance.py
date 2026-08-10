"""Conditional consistency checking for externally admitted claim ledgers.

This module is not a root of trust: a self-consistent ledger supplied by an
untrusted caller proves nothing about the underlying resume.  Callers must pass
an independently trusted ledger digest obtained from the application control
plane.  Without that out-of-band binding, authorization fails closed.
"""

import hashlib
import json

CONSTRUCTIVE_PROVENANCE_VERSION = "constructive-provenance/v1"

_REQUEST_VERSION = "constructive-provenance-request/v1"
_LEDGER_VERSION = "constructive-provenance-ledger/v1"
_PLAN_VERSION = "constructive-provenance-claim-plan/v1"
_RESULT_VERSION = "constructive-provenance-result/v1"

_REQUEST_KEYS = {"schema_version", "ledger", "claim_plan"}
_LEDGER_KEYS = {"schema_version", "ledger_id", "run_id", "entries"}
_ENTRY_KEYS = {
    "claim_id",
    "source_role_id",
    "source_ref",
    "claim_kind",
    "assertion_id",
    "approved_realizations",
    "metric",
}
_SOURCE_REF_KEYS = {
    "document_id",
    "document_sha256",
    "byte_start",
    "byte_end",
    "span_sha256",
}
_REALIZATION_KEYS = {"realization_id", "realization_sha256"}
_METRIC_KEYS = {
    "metric_type",
    "value",
    "semantic_unit",
    "period",
    "comparison",
    "currency",
}
_VALUE_KEYS = {"numerator", "denominator"}
_PLAN_KEYS = {
    "schema_version",
    "ledger_id",
    "ledger_sha256",
    "run_id",
    "output_role_id",
    "operator",
    "claim_refs",
}
_CLAIM_REF_KEYS = {
    "claim_id",
    "source_role_id",
    "source_ref",
    "claim_kind",
    "assertion_id",
    "realization_id",
    "realization_sha256",
    "metric",
}


def _result(verdict, code=None):
    return {
        "schema_version": _RESULT_VERSION,
        "verdict": verdict,
        "codes": [] if code is None else [code],
    }


def _unsupported_claim():
    return _result("REJECTED:UNSUPPORTED_CLAIM", "UNSUPPORTED_CLAIM")


def _unsupported_metric():
    return _result("REJECTED:UNSUPPORTED_METRIC", "UNSUPPORTED_METRIC")


def _exact_dict(value, keys):
    return type(value) is dict and set(value) == keys


def _text(value):
    return type(value) is str and bool(value)


def _sha256(value):
    return (
        type(value) is str
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _integer(value):
    return type(value) is int


def _gcd(left, right):
    while right:
        left, right = right, left % right
    return left


def _valid_source_ref(source_ref):
    if not _exact_dict(source_ref, _SOURCE_REF_KEYS):
        return False
    if not _text(source_ref["document_id"]):
        return False
    if not all(_sha256(source_ref[key]) for key in ("document_sha256", "span_sha256")):
        return False
    byte_start = source_ref["byte_start"]
    byte_end = source_ref["byte_end"]
    return (
        _integer(byte_start)
        and _integer(byte_end)
        and byte_start >= 0
        and byte_end > byte_start
    )


def _valid_value(value):
    if not _exact_dict(value, _VALUE_KEYS):
        return False
    numerator = value["numerator"]
    denominator = value["denominator"]
    return (
        _integer(numerator)
        and _integer(denominator)
        and denominator > 0
        and _gcd(abs(numerator), denominator) == 1
    )


def _valid_metric(metric):
    if metric is None:
        return True
    if not _exact_dict(metric, _METRIC_KEYS) or not _valid_value(metric["value"]):
        return False
    if not _text(metric["metric_type"]) or not _text(metric["semantic_unit"]):
        return False
    return all(
        value is None or type(value) is str
        for value in (metric["period"], metric["comparison"], metric["currency"])
    )


def _valid_realization(realization):
    return (
        _exact_dict(realization, _REALIZATION_KEYS)
        and _text(realization["realization_id"])
        and _sha256(realization["realization_sha256"])
    )


def _valid_entry(entry):
    if not _exact_dict(entry, _ENTRY_KEYS):
        return False
    if not all(
        _text(entry[key])
        for key in ("claim_id", "source_role_id", "claim_kind", "assertion_id")
    ):
        return False
    realizations = entry["approved_realizations"]
    if type(realizations) is not list or not realizations:
        return False
    if not all(_valid_realization(item) for item in realizations):
        return False
    realization_ids = [item["realization_id"] for item in realizations]
    return (
        len(realization_ids) == len(set(realization_ids))
        and _valid_source_ref(entry["source_ref"])
        and _valid_metric(entry["metric"])
    )


def _valid_claim_ref(claim_ref):
    if not _exact_dict(claim_ref, _CLAIM_REF_KEYS):
        return False
    if not all(
        _text(claim_ref[key])
        for key in (
            "claim_id",
            "source_role_id",
            "claim_kind",
            "assertion_id",
            "realization_id",
        )
    ):
        return False
    return (
        _sha256(claim_ref["realization_sha256"])
        and _valid_source_ref(claim_ref["source_ref"])
        and _valid_metric(claim_ref["metric"])
    )


def _canonical_ledger_sha256(ledger):
    canonical = dict(ledger)
    canonical["entries"] = sorted(ledger["entries"], key=lambda item: item["claim_id"])
    encoded = json.dumps(
        canonical,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _same_rational(left, right):
    return (
        left["numerator"] * right["denominator"]
        == right["numerator"] * left["denominator"]
    )


def _duration_equivalent(left, right):
    if left["metric_type"] != "duration" or right["metric_type"] != "duration":
        return False
    left_unit = left["semantic_unit"]
    right_unit = right["semantic_unit"]
    month_units = {"month", "months"}
    year_units = {"year", "years"}
    left_value = left["value"]
    right_value = right["value"]
    if left_unit in month_units and right_unit in year_units:
        return (
            left_value["numerator"] * right_value["denominator"]
            == 12 * right_value["numerator"] * left_value["denominator"]
        )
    if left_unit in year_units and right_unit in month_units:
        return (
            12 * left_value["numerator"] * right_value["denominator"]
            == right_value["numerator"] * left_value["denominator"]
        )
    return False


def _metrics_match(admitted, requested):
    if admitted is None or requested is None:
        return admitted is requested
    if admitted["metric_type"] != requested["metric_type"]:
        return False
    for key in ("period", "comparison", "currency"):
        if admitted[key] != requested[key]:
            return False
    if admitted["semantic_unit"] == requested["semantic_unit"]:
        return _same_rational(admitted["value"], requested["value"])
    return _duration_equivalent(admitted, requested)


def authorize_claim(
    request: dict,
    *,
    trusted_ledger_sha256: str | None = None,
) -> dict:
    """Authorize one plan only against an independently admitted ledger hash."""
    try:
        if not _sha256(trusted_ledger_sha256):
            return _unsupported_claim()
        if not _exact_dict(request, _REQUEST_KEYS):
            return _unsupported_claim()
        if request["schema_version"] != _REQUEST_VERSION:
            return _unsupported_claim()

        ledger = request["ledger"]
        plan = request["claim_plan"]
        if not _exact_dict(ledger, _LEDGER_KEYS) or not _exact_dict(plan, _PLAN_KEYS):
            return _unsupported_claim()
        if ledger["schema_version"] != _LEDGER_VERSION:
            return _unsupported_claim()
        if plan["schema_version"] != _PLAN_VERSION:
            return _unsupported_claim()
        if not all(_text(ledger[key]) for key in ("ledger_id", "run_id")):
            return _unsupported_claim()
        if not all(
            _text(plan[key])
            for key in ("ledger_id", "run_id", "output_role_id", "operator")
        ):
            return _unsupported_claim()
        if not _sha256(plan["ledger_sha256"]):
            return _unsupported_claim()

        entries = ledger["entries"]
        claim_refs = plan["claim_refs"]
        if type(entries) is not list or not entries:
            return _unsupported_claim()
        if not all(_valid_entry(entry) for entry in entries):
            return _unsupported_claim()
        claim_ids = [entry["claim_id"] for entry in entries]
        if len(claim_ids) != len(set(claim_ids)):
            return _unsupported_claim()
        if type(claim_refs) is not list or len(claim_refs) != 1:
            return _unsupported_claim()
        if plan["operator"] != "ATOMIC" or not _valid_claim_ref(claim_refs[0]):
            return _unsupported_claim()

        if plan["ledger_id"] != ledger["ledger_id"]:
            return _unsupported_claim()
        if plan["run_id"] != ledger["run_id"]:
            return _unsupported_claim()
        canonical_ledger_sha256 = _canonical_ledger_sha256(ledger)
        if plan["ledger_sha256"] != canonical_ledger_sha256:
            return _unsupported_claim()
        if trusted_ledger_sha256 != canonical_ledger_sha256:
            return _unsupported_claim()

        claim_ref = claim_refs[0]
        entries_by_id = {entry["claim_id"]: entry for entry in entries}
        admitted = entries_by_id.get(claim_ref["claim_id"])
        if admitted is None:
            return _unsupported_claim()

        if claim_ref["source_role_id"] != admitted["source_role_id"]:
            return _unsupported_claim()
        if plan["output_role_id"] != admitted["source_role_id"]:
            return _unsupported_claim()
        if claim_ref["source_ref"] != admitted["source_ref"]:
            return _unsupported_claim()
        if claim_ref["claim_kind"] != admitted["claim_kind"]:
            return _unsupported_claim()
        if claim_ref["assertion_id"] != admitted["assertion_id"]:
            return _unsupported_claim()

        realization = next(
            (
                item
                for item in admitted["approved_realizations"]
                if item["realization_id"] == claim_ref["realization_id"]
            ),
            None,
        )
        if realization is None:
            return _unsupported_claim()
        if realization["realization_sha256"] != claim_ref["realization_sha256"]:
            return _unsupported_claim()

        if not _metrics_match(admitted["metric"], claim_ref["metric"]):
            return _unsupported_metric()
        return _result("AUTHORIZED")
    except Exception:
        return _unsupported_claim()
