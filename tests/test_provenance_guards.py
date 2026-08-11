"""Regression tests for conservative claim and ledger trust boundaries."""

import pytest

from claim_provenance_audit import claim_supported_by_source
from constructive_provenance import _canonical_ledger_sha256, authorize_claim
from multi_agent_team import (
    build_context,
    build_handoff,
    canonical_digest,
    normalize_native_payload,
    validate_handoff,
)
from resume_integrity_audit import audit_resume_text


def _ledger_request():
    source_ref = {
        "document_id": "master-resume",
        "document_sha256": "1" * 64,
        "byte_start": 10,
        "byte_end": 30,
        "span_sha256": "2" * 64,
    }
    realization = {
        "realization_id": "plain-line",
        "realization_sha256": "3" * 64,
    }
    entry = {
        "claim_id": "claim-1",
        "source_role_id": "role-1",
        "source_ref": source_ref,
        "claim_kind": "experience",
        "assertion_id": "assertion-1",
        "approved_realizations": [realization],
        "metric": None,
    }
    ledger = {
        "schema_version": "constructive-provenance-ledger/v1",
        "ledger_id": "ledger-1",
        "run_id": "run-1",
        "entries": [entry],
    }
    claim_ref = {
        "claim_id": entry["claim_id"],
        "source_role_id": entry["source_role_id"],
        "source_ref": source_ref,
        "claim_kind": entry["claim_kind"],
        "assertion_id": entry["assertion_id"],
        "realization_id": realization["realization_id"],
        "realization_sha256": realization["realization_sha256"],
        "metric": None,
    }
    plan = {
        "schema_version": "constructive-provenance-claim-plan/v1",
        "ledger_id": ledger["ledger_id"],
        "ledger_sha256": _canonical_ledger_sha256(ledger),
        "run_id": ledger["run_id"],
        "output_role_id": entry["source_role_id"],
        "operator": "ATOMIC",
        "claim_refs": [claim_ref],
    }
    return {
        "schema_version": "constructive-provenance-request/v1",
        "ledger": ledger,
        "claim_plan": plan,
    }


def test_constructive_ledger_needs_out_of_band_trust_root():
    request = _ledger_request()
    assert authorize_claim(request)["verdict"] == "REJECTED:UNSUPPORTED_CLAIM"
    assert (
        authorize_claim(request, trusted_ledger_sha256="0" * 64)["verdict"]
        == "REJECTED:UNSUPPORTED_CLAIM"
    )
    assert (
        authorize_claim(
            request,
            trusted_ledger_sha256=request["claim_plan"]["ledger_sha256"],
        )["verdict"]
        == "AUTHORIZED"
    )


def test_claim_closure_rejects_semantic_and_metric_strengthening():
    unsafe_pairs = (
        ("Cured cancer.", "Worked as an analyst."),
        ("Cured cancer.", "Did not cure cancer."),
        ("Managed a $9T portfolio.", "Managed a $9B portfolio."),
        ("Improved accuracy 50-fold.", "Improved accuracy 50%."),
        ("Reached 50%.", "Reached up to 50%."),
        ("Reviewed 11,300 cases.", "Reviewed 11,300+ cases."),
        ("Drug treated patients.", "Patients treated drug."),
        ("Cut errors 20% and delays 10%.", "Cut errors 10% and delays 20%."),
        ("Audited FDA.", "Audited by FDA."),
        ("Worked at Acme.", "Worked for Acme."),
        ("Reviewed safety and quality.", "Reviewed safety or quality."),
        ("FDA reviewed records.", "Records were reviewed by FDA."),
        ("Managed 10 staff.", "Oversaw 10 staff."),
        ("Deployed one model.", "Implemented one model."),
        ("Designed one trial.", "Planned one trial."),
        ("Reviewed new reports.", "Reviewed news reports."),
        ("Managed build systems.", "Managed building systems."),
        ("Updated policy.", "Updated policies."),
        ("Reviewed 5 new reports.", "Reviewed 5 news reports."),
        ("Reviewed drafts.", "Reviewed authors."),
        ("Wrote checked reports.", "Wrote reviewed reports."),
        ("Review records.", "Check records."),
        ("Reported ROI of 5%.", "Reported ROI of -5%."),
        ("Reported ROI of 5%.", "Reported ROI of +5%."),
        ("Delivered 5 days.", "Delivered <5 days."),
        ("Delivered 5 days.", "Delivered >5 days."),
        ("Delivered 5 days.", "Delivered ≤5 days."),
        ("Delivered 5 days.", "Delivered ≥5 days."),
        ("Built C# systems.", "Built C++ systems."),
        ("Built C systems.", "Built R systems."),
        ("Used X.", "Used R."),
        ("Delivered 1-2 releases.", "Delivered 1/2 releases."),
        ("Improved 5%.", "Improved ~5%."),
        ("Managed 5.", "Managed €5."),
        ("Led research development.", "Led research & development."),
        ("Made IT work.", "Made it work."),
        ("Worked with US teams.", "Worked with us teams."),
        ("Used R for analysis.", "Used r for analysis."),
        ("Entered Polish market.", "Entered polish market."),
        ("US teams delivered.", "us teams delivered."),
    )
    for claim, source in unsafe_pairs:
        supported, _ = claim_supported_by_source(claim, source)
        assert supported is False


def test_claim_closure_allows_only_closed_plain_rewording():
    assert claim_supported_by_source(
        "Wrote 12 reports.", "Authored 12 reports."
    ) == (True, "AUTHORIZED")


@pytest.mark.parametrize(
    ("master", "draft", "sliced_span"),
    [
        ("No experience with Python", "Experience with Python", "experience with Python"),
        ("Worked on non-compliant sites", "Compliant sites", "compliant sites"),
        ("Operated without FDA approval", "FDA approval", "FDA approval"),
    ],
)
def test_model_cannot_slice_context_away_from_source_line(master, draft, sliced_span):
    context = build_context(
        run_id="source-boundary",
        case_id="context-slice",
        role="writer",
        attempt=0,
        payload={"master_resume": master, "researcher_artifact": {}},
    )
    with pytest.raises(ValueError, match="exact complete line"):
        normalize_native_payload(
            "writer",
            {
                "replacements": [
                    {
                        "source_span_text": sliced_span,
                        "replacement_text": draft,
                    }
                ],
            },
            context,
        )

    source_start = master.index(sliced_span)
    forged = build_handoff(
        context=context,
        role="writer",
        agent_id="codex:forged-writer-partial-span",
        payload={
            "draft": draft,
            "claim_evidence": [
                {
                    "claim_digest": canonical_digest(draft),
                    "source_span_digest": canonical_digest(sliced_span),
                    "source_start": source_start,
                    "source_end": source_start + len(sliced_span),
                }
            ],
        },
    )
    assert validate_handoff("writer", forged, context)["code"] == "WRITER_SCHEMA"


@pytest.mark.parametrize(
    ("replacements", "expected_error"),
    [
        (
            [
                {
                    "source_span_text": "Reviewed 12 cases.",
                    "replacement_text": "Checked 12 cases.",
                },
                {
                    "source_span_text": "Reviewed 12 cases.",
                    "replacement_text": "Examined 12 cases.",
                },
            ],
            "reuses one source line",
        ),
        (
            [
                {
                    "source_span_text": "Reviewed 12 cases.",
                    "replacement_text": (
                        "Reviewed 12 cases.\nChecked 12 cases."
                    ),
                }
            ],
            "one safe line",
        ),
    ],
)
def test_one_source_line_cannot_be_reused_or_retained_as_a_duplicate(
    replacements,
    expected_error,
):
    master = "Reviewed 12 cases."
    context = build_context(
        run_id="source-multiplicity",
        case_id="duplicate-claim",
        role="writer",
        attempt=0,
        payload={"master_resume": master, "researcher_artifact": {}},
    )
    with pytest.raises(ValueError, match=expected_error):
        normalize_native_payload(
            "writer",
            {"replacements": replacements},
            context,
        )


@pytest.mark.parametrize(
    ("draft", "claims"),
    [
        (
            "Checked 12 cases.\nExamined 12 cases.",
            ["Checked 12 cases.", "Examined 12 cases."],
        ),
        (
            "Reviewed 12 cases.\nChecked 12 cases.",
            ["Checked 12 cases."],
        ),
    ],
)
def test_forged_writer_handoff_rejects_reused_or_retained_source_line(
    draft,
    claims,
):
    master = "Reviewed 12 cases."
    context = build_context(
        run_id="forged-source-multiplicity",
        case_id="normalized-defense",
        role="writer",
        attempt=0,
        payload={"master_resume": master, "researcher_artifact": {}},
    )
    evidence = [
        {
            "claim_digest": canonical_digest(claim),
            "source_span_digest": canonical_digest(master),
            "source_start": 0,
            "source_end": len(master),
        }
        for claim in claims
    ]
    forged = build_handoff(
        context=context,
        role="writer",
        agent_id="codex:forged-writer-multiplicity",
        payload={"draft": draft, "claim_evidence": evidence},
    )

    assert validate_handoff("writer", forged, context)["code"] == "WRITER_SCHEMA"


def test_role_locality_blocks_cross_job_metric_migration_and_duplicates():
    master = """PROFESSIONAL EXPERIENCE
ROLE A | COMPANY A
2020 - 2021
• Managed a $9B portfolio.

ROLE B | COMPANY B
2021 - 2022
• Reviewed analyst reports.

EDUCATION
Example University
"""
    context = build_context(
        run_id="role-locality",
        case_id="cross-role",
        role="writer",
        attempt=0,
        payload={"master_resume": master, "researcher_artifact": {}},
    )

    with pytest.raises(ValueError, match="moves source claims"):
        normalize_native_payload(
            "writer",
            {
                "replacements": [
                    {
                        "source_span_text": "• Managed a $9B portfolio.",
                        "replacement_text": "",
                    },
                    {
                        "source_span_text": "• Reviewed analyst reports.",
                        "replacement_text": "• Managed a $9B portfolio.",
                    },
                ]
            },
            context,
        )

    with pytest.raises(
        ValueError,
        match="UNSUPPORTED_(?:CLAIM|METRIC)|duplicates one source line",
    ):
        normalize_native_payload(
            "writer",
            {
                "replacements": [
                    {
                        "source_span_text": "• Reviewed analyst reports.",
                        "replacement_text": "• Led a $9B portfolio.",
                    }
                ],
            },
            context,
        )

    forged_cross_role_draft = """PROFESSIONAL EXPERIENCE
ROLE A | COMPANY A
2020 - 2021
• Managed a $9B portfolio.

ROLE B | COMPANY B
2021 - 2022
• Led a $9B portfolio.

EDUCATION
Example University
"""
    source_text = "• Managed a $9B portfolio."
    source_start = master.index(source_text)
    forged = build_handoff(
        context=context,
        role="writer",
        agent_id="codex:forged-writer-cross-role",
        payload={
            "draft": forged_cross_role_draft,
            "claim_evidence": [
                {
                    "claim_digest": canonical_digest("• Led a $9B portfolio."),
                    "source_span_digest": canonical_digest(source_text),
                    "source_start": source_start,
                    "source_end": source_start + len(source_text),
                }
            ],
        },
    )
    assert validate_handoff("writer", forged, context)["code"] == "WRITER_SCHEMA"

    with pytest.raises(ValueError, match="one safe line"):
        normalize_native_payload(
            "writer",
            {
                "replacements": [
                    {
                        "source_span_text": "• Managed a $9B portfolio.",
                        "replacement_text": (
                            "• Managed a $9B portfolio.\n"
                            "• Managed a $9B portfolio."
                        ),
                    }
                ]
            },
            context,
        )


def test_shared_verbatim_line_cannot_shift_multiplicity_between_roles():
    master = """PROFESSIONAL EXPERIENCE
ROLE A | COMPANY A
2020 - 2021
• Reviewed shared records.

ROLE B | COMPANY B
2021 - 2022
• Reviewed shared records.

EDUCATION
Example University
"""
    context = build_context(
        run_id="role-multiplicity",
        case_id="shared-line",
        role="writer",
        attempt=0,
        payload={"master_resume": master, "researcher_artifact": {}},
    )
    with pytest.raises(ValueError, match="one unique source span"):
        normalize_native_payload(
            "writer",
            {
                "replacements": [
                    {
                        "source_span_text": "• Reviewed shared records.",
                        "replacement_text": "• Checked shared records.",
                    }
                ]
            },
            context,
        )

    forged_draft = """PROFESSIONAL EXPERIENCE
ROLE A | COMPANY A
2020 - 2021
• Reviewed shared records.
• Reviewed shared records.

ROLE B | COMPANY B
2021 - 2022

EDUCATION
Example University
"""
    forged = build_handoff(
        context=context,
        role="writer",
        agent_id="codex:forged-writer-role-multiplicity",
        payload={"draft": forged_draft, "claim_evidence": []},
    )
    assert validate_handoff("writer", forged, context)["code"] == "WRITER_SCHEMA"


def test_experience_line_cannot_move_or_reword_into_summary():
    master = """PROFESSIONAL SUMMARY
Plain summary.

PROFESSIONAL EXPERIENCE
ROLE A | COMPANY A
2020 - 2021
• Reviewed attributed records.

EDUCATION
Example University
"""
    context = build_context(
        run_id="role-to-summary",
        case_id="attribution",
        role="writer",
        attempt=0,
        payload={"master_resume": master, "researcher_artifact": {}},
    )
    with pytest.raises(ValueError, match="duplicates or moves"):
        normalize_native_payload(
            "writer",
            {
                "replacements": [
                    {
                        "source_span_text": "Plain summary.",
                        "replacement_text": "• Reviewed attributed records.",
                    },
                    {
                        "source_span_text": "• Reviewed attributed records.",
                        "replacement_text": "",
                    },
                ]
            },
            context,
        )

    # The role now fails the stronger completeness invariant first because its
    # only genuine bullet was moved into Summary.  It must still fail closed.
    with pytest.raises(ValueError, match="duplicates or moves"):
        normalize_native_payload(
            "writer",
            {
                "replacements": [
                    {
                        "source_span_text": "Plain summary.",
                        "replacement_text": "• Checked attributed records.",
                    },
                    {
                        "source_span_text": "• Reviewed attributed records.",
                        "replacement_text": "",
                    },
                ],
            },
            context,
        )


def test_experience_aliases_preserve_roles_and_per_role_ownership():
    work_master = """WORK EXPERIENCE
ROLE A | COMPANY A
2020 - 2021
• Reviewed alpha records.

EDUCATION
Example University
"""
    work_context = build_context(
        run_id="work-experience-alias",
        case_id="role-removal",
        role="writer",
        attempt=0,
        payload={"master_resume": work_master, "researcher_artifact": {}},
    )
    with pytest.raises(ValueError, match="duplicates or moves"):
        normalize_native_payload(
            "writer",
            {
                "replacements": [
                    {
                        "source_span_text": "• Reviewed alpha records.",
                        "replacement_text": "",
                    }
                ]
            },
            work_context,
        )

    experience_master = """EXPERIENCE
ROLE A | COMPANY A
2020 - 2021
• Reviewed alpha records.

ROLE B | COMPANY B
2021 - 2022
• Reviewed beta records.

EDUCATION
Example University
"""
    experience_context = build_context(
        run_id="experience-alias",
        case_id="role-swap",
        role="writer",
        attempt=0,
        payload={"master_resume": experience_master, "researcher_artifact": {}},
    )
    with pytest.raises(ValueError, match="duplicates or moves"):
        normalize_native_payload(
            "writer",
            {
                "replacements": [
                    {
                        "source_span_text": "• Reviewed alpha records.",
                        "replacement_text": "• Reviewed beta records.",
                    },
                    {
                        "source_span_text": "• Reviewed beta records.",
                        "replacement_text": "• Reviewed alpha records.",
                    },
                ]
            },
            experience_context,
        )


@pytest.mark.parametrize(
    "heading",
    [
        "## PROFESSIONAL EXPERIENCE",
        "- WORK EXPERIENCE:",
        "* EXPERIENCE",
        "+ PROFESSIONAL EXPERIENCE",
        "• WORK EXPERIENCE",
        "1. EXPERIENCE",
    ],
)
@pytest.mark.parametrize("layout", ["rendered", "standalone"])
def test_all_supported_role_layouts_and_heading_forms_block_cross_role_swaps(
    heading,
    layout,
):
    if layout == "rendered":
        role_a = "ROLE A | 2020 - 2021\nCOMPANY A | City"
        role_b = "ROLE B | 2021 - 2022\nCOMPANY B | City"
    else:
        role_a = "ROLE A\nCOMPANY A | City\n2020 - 2021"
        role_b = "ROLE B\nCOMPANY B | City\n2021 - 2022"
    master = f"""{heading}
{role_a}
• Reviewed alpha records.

{role_b}
• Reviewed beta records.

## EDUCATION:
Example University
"""
    context = build_context(
        run_id=f"{layout}-heading-grammar",
        case_id="role-swap",
        role="writer",
        attempt=0,
        payload={"master_resume": master, "researcher_artifact": {}},
    )
    with pytest.raises(ValueError, match="duplicates or moves"):
        normalize_native_payload(
            "writer",
            {
                "replacements": [
                    {
                        "source_span_text": "• Reviewed alpha records.",
                        "replacement_text": "• Reviewed beta records.",
                    },
                    {
                        "source_span_text": "• Reviewed beta records.",
                        "replacement_text": "• Reviewed alpha records.",
                    },
                ]
            },
            context,
        )


@pytest.mark.parametrize(
    "master",
    [
        pytest.param("Reviewed.\u2028", id="unsafe-format"),
        pytest.param(
            """PROFESSIONAL EXPERIENCE
An intentionally unstructured role without canonical dates.

EDUCATION
Example University
""",
            id="unrecognized-experience-layout",
        ),
    ],
)
def test_empty_writer_passthrough_fails_closed_at_normalized_handoff(master):
    context = build_context(
        run_id="unknown-role-layout",
        case_id="fail-closed",
        role="writer",
        attempt=0,
        payload={"master_resume": master, "researcher_artifact": {}},
    )
    handoff = build_handoff(
        context=context,
        role="writer",
        agent_id="codex:untrusted-empty-passthrough",
        payload={"draft": master, "claim_evidence": []},
    )

    assert validate_handoff("writer", handoff, context)["code"] == "WRITER_SCHEMA"


def test_partial_role_parse_cannot_disable_role_locality():
    master = """PROFESSIONAL EXPERIENCE
ROLE A | COMPANY A
2020 - 2021
• Reviewed alpha records.

ROLE B | COMPANY B
• Reviewed beta records.
2021 - 2022

EDUCATION
Example University
"""
    context = build_context(
        run_id="partial-role-layout",
        case_id="canonical-parser-alignment",
        role="writer",
        attempt=0,
        payload={"master_resume": master, "researcher_artifact": {}},
    )
    with pytest.raises(ValueError, match="duplicates or moves"):
        normalize_native_payload(
            "writer",
            {
                "replacements": [
                    {
                        "source_span_text": "• Reviewed alpha records.",
                        "replacement_text": "• Reviewed beta records.",
                    },
                    {
                        "source_span_text": "• Reviewed beta records.",
                        "replacement_text": "• Reviewed alpha records.",
                    },
                ]
            },
            context,
        )


def test_rendered_role_header_cannot_be_consumed_as_prior_role_date():
    master = """PROFESSIONAL EXPERIENCE
ROLE A | COMPANY A
• Reviewed alpha records.
ROLE B | 2022 - Present
COMPANY B
• Reviewed beta records.

EDUCATION
Example University
"""
    report = audit_resume_text(master, master)
    assert report["passed"] is False
    assert report["difference_codes"] == ["AMBIGUOUS_ROLE_LAYOUT"]
    context = build_context(
        run_id="mixed-role-layout",
        case_id="role-header-is-not-date",
        role="writer",
        attempt=0,
        payload={"master_resume": master, "researcher_artifact": {}},
    )
    with pytest.raises(ValueError, match="duplicates or moves"):
        normalize_native_payload(
            "writer",
            {
                "replacements": [
                    {
                        "source_span_text": "• Reviewed alpha records.",
                        "replacement_text": "• Reviewed beta records.",
                    },
                    {
                        "source_span_text": "• Reviewed beta records.",
                        "replacement_text": "• Reviewed alpha records.",
                    },
                ]
            },
            context,
        )


def test_semicolon_separated_role_date_ranges_remain_valid():
    master = """PROFESSIONAL EXPERIENCE
ROLE A | COMPANY A
Oct 2021 - Jul 2022; Jul 2023 - Jan 2024
• Reviewed alpha records.

EDUCATION
Example University
"""
    assert audit_resume_text(master, master)["passed"] is True
    context = build_context(
        run_id="split-tenure-dates",
        case_id="exact-date-grammar",
        role="writer",
        attempt=0,
        payload={"master_resume": master, "researcher_artifact": {}},
    )
    normalized = normalize_native_payload(
        "writer",
        {"replacements": []},
        context,
    )
    assert normalized["draft"] == master


@pytest.mark.parametrize(
    "role_block",
    [
        "PROGRAM 2020 LEAD | STUDIO 2020\n2022 - Present",
        "PROGRAM 2020 LEAD | 2022 - Present\nSTUDIO 2020 | City",
        "PROGRAM 2020 LEAD\nSTUDIO 2020 | City\n2022 - Present",
    ],
)
def test_years_in_titles_or_companies_are_not_mistaken_for_role_dates(role_block):
    master = f"""PROFESSIONAL EXPERIENCE
{role_block}
• Reviewed alpha records.

EDUCATION
Example University
"""
    assert audit_resume_text(master, master)["passed"] is True
    context = build_context(
        run_id="year-in-role-identity",
        case_id="structural-date-detection",
        role="writer",
        attempt=0,
        payload={"master_resume": master, "researcher_artifact": {}},
    )
    normalized = normalize_native_payload(
        "writer",
        {"replacements": []},
        context,
    )
    assert normalized["draft"] == master
