"""Synthetic regression coverage for the canonical resume identity gate."""

from pathlib import Path

import pytest

from docx_generator import create_ats_resume
from resume_integrity_audit import audit_resume_files, audit_resume_text, resolve_master_path

SEP = "_" * 79


def resume(*, title="Signal Keeper", punctuation=".", duplicate=False):
    role = f"{title} | Paper Harbor Co.\n2020 – Present\n\n• Mutable bullet."
    roles = f"{role}\n\n{role}" if duplicate else role
    return f"""Synthetic Person
Neutral, ZZ | neutral@example.invalid

{SEP}
PROFESSIONAL EXPERIENCE

{roles}

{SEP}
EDUCATION

Diploma in Paper Systems
Fixture School, Neutral, ZZ | 2018

{SEP}
CERTIFICATIONS & LICENSURE

• Paper Safety Permit | PSP-1

{SEP}
PUBLICATIONS

1. Reed A. "Paper Signals{punctuation}" Fixture Journal. 2024;1:1-2.

Full publication list: https://example.invalid/paper

{SEP}
PROFESSIONAL MEMBERSHIPS

• Society of Paper Fixtures
"""


@pytest.mark.parametrize(
    "mutator,code",
    [
        (lambda value: value.replace("Signal Keeper", "Signal Reader", 1), "ROLE_TITLE_CHANGED"),
        (lambda value: value.replace("Paper Harbor Co.", "Paper Harbor Inc.", 1), "ROLE_COMPANY_CHANGED"),
        (lambda value: value.replace("2020 – Present", "2021 – Present", 1), "ROLE_DATES_CHANGED"),
        (lambda value: value.replace("PSP-1", "PSP-2", 1), "CERTIFICATION_CHANGED"),
        (lambda value: value.replace("Signals.", "Signals:"), "PUBLICATION_CHANGED"),
    ],
)
def test_semantic_changes_have_specific_single_code(mutator, code):
    report = audit_resume_text(resume(), mutator(resume()))
    assert report["difference_codes"] == [code]


def test_formatting_and_bullet_rewording_pass():
    tailored = resume().replace("2020 – Present", "2020 - Present")
    tailored = tailored.replace("• Mutable bullet.", "- A wholly different bullet.")
    tailored = tailored.replace("• Paper Safety Permit", "- Paper Safety Permit")
    assert audit_resume_text(resume(), tailored)["passed"]


def test_certifications_training_alias_matches_rendered_licensure_heading():
    master = resume().replace(
        "CERTIFICATIONS & LICENSURE", "CERTIFICATIONS & TRAINING"
    )
    report = audit_resume_text(master, resume())

    assert report["passed"] is True
    assert report["counts"]["master"]["education"] == 1
    assert report["counts"]["master"]["certifications"] == 1


def test_certifications_training_alias_matches_generated_docx(tmp_path):
    master = tmp_path / "master.md"
    rendered = tmp_path / "resume.docx"
    master.write_text(
        resume().replace(
            "CERTIFICATIONS & LICENSURE", "CERTIFICATIONS & TRAINING"
        ),
        encoding="utf-8",
    )
    create_ats_resume(
        output_path=str(rendered),
        name="Synthetic Person",
        contact_info={"email": "neutral@example.invalid"},
        summary="",
        core_competencies=[],
        experience=[
            {
                "title": "Signal Keeper",
                "company": "Paper Harbor Co.",
                "location": "",
                "dates": "2020 - Present",
                "bullets": ["Mutable bullet."],
            }
        ],
        education=[
            {
                "degree": "Diploma in Paper Systems",
                "school": "Fixture School",
                "location": "Neutral, ZZ",
                "dates": "2018",
            }
        ],
        certifications=["Paper Safety Permit | PSP-1"],
        publications=[
            'Reed A. "Paper Signals." Fixture Journal. 2024;1:1-2.'
        ],
        publications_footer="Full publication list: https://example.invalid/paper",
        professional_memberships=["Society of Paper Fixtures"],
    )

    report = audit_resume_files(tailored_path=rendered, master_path=master)

    assert report["passed"] is True
    assert report["difference_codes"] == []
    assert report["counts"]["master"]["education"] == 1
    assert report["counts"]["tailored"]["education"] == 1
    assert report["counts"]["master"]["certifications"] == 1
    assert report["counts"]["tailored"]["certifications"] == 1


def test_work_experience_alias_protects_role_presence():
    master = """WORK EXPERIENCE
ROLE A | COMPANY A
2020 - 2021
• Reviewed records.

EDUCATION
Example University
"""
    tailored = """WORK EXPERIENCE

EDUCATION
Example University
"""
    report = audit_resume_text(master, tailored)
    assert report["passed"] is False
    assert "ROLE_MISSING" in report["difference_codes"]


def test_duplicate_count_is_not_collapsed():
    report = audit_resume_text(resume(duplicate=True), resume())
    assert report["difference_codes"] == ["ROLE_MISSING"]


@pytest.mark.parametrize(
    "tailored,code",
    [
        ("", "EMPTY_ARTIFACT"),
        (f"Synthetic\n{SEP}\nEDUCATION\n", "NO_CANONICAL_CONTENT"),
        (resume() + f"\n{SEP}\nEDUCATION\nExtra\nSchool | 2020\n", "DUPLICATE_PROTECTED_SECTION"),
    ],
)
def test_malformed_text_fails_closed(tailored, code):
    assert audit_resume_text(resume(), tailored)["difference_codes"] == [code]


def test_config_relative_resolution_is_independent_of_cwd(tmp_path, monkeypatch):
    package = tmp_path / "package"
    package.mkdir()
    master = package / "master.md"
    master.write_text(resume(), encoding="utf-8")
    config = package / "config.json"
    config.write_text('{"master_resume_path":"master.md"}', encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    assert resolve_master_path(str(config)) == master.resolve()


def test_filename_and_decoy_words_do_not_affect_result(tmp_path):
    master = tmp_path / "changed-title-master.md"
    tailored = tmp_path / "canonical-integrity-passed.md"
    master.write_text(resume(), encoding="utf-8")
    tailored.write_text(resume(), encoding="utf-8")
    assert audit_resume_files(tailored_path=tailored, master_path=master)["passed"]


def test_direct_generator_is_retired_and_creates_no_output(tmp_path):
    from docx_generator import create_resume_from_md
    from legacy_rewrite_guard import (
        NATIVE_RESUME_TEAM_REQUIRED,
        NativeResumeTeamRequiredError,
    )

    master = tmp_path / "master.md"
    source = tmp_path / "tailored.md"
    output = tmp_path / "resume.docx"
    master.write_text(resume(), encoding="utf-8")
    source.write_text(resume(), encoding="utf-8")
    with pytest.raises(NativeResumeTeamRequiredError) as failure:
        create_resume_from_md(str(source), str(output), master_path=str(master))
    assert failure.value.code == NATIVE_RESUME_TEAM_REQUIRED
    assert not output.exists()
    assert not list(tmp_path.glob("*.tmp.docx"))


def test_diagnostics_do_not_disclose_facts():
    report = audit_resume_text(resume(), resume(title="Changed Keeper"))
    rendered = str(report)
    assert "Signal Keeper" not in rendered
    assert "Changed Keeper" not in rendered


def test_command_mirrors_gate_before_docx_and_tracker_after_docx():
    root = Path(__file__).resolve().parents[1]
    for command_path in sorted((root / "commands").glob("*.md")):
        mirror_path = root / ".claude" / "commands" / command_path.name
        assert mirror_path.is_file()
        assert command_path.read_bytes() == mirror_path.read_bytes()

    for name in ("resume", "tailor-resume", "batch-resume"):
        command = (root / "commands" / f"{name}.md").read_text(encoding="utf-8")
        docx_gate = command.find("create_resume_from_md_authorized(")
        tracker_gate = command.find("add_application_authorized(", docx_gate)
        audit_gate = command.rfind("resume_integrity_audit.py", 0, docx_gate)
        receipt_gate = command.rfind("verify_final_receipt", 0, docx_gate)
        assert docx_gate >= 0
        assert tracker_gate > docx_gate
        assert 0 <= audit_gate < docx_gate
        assert 0 <= receipt_gate < docx_gate
        assert "create_resume_from_md(" not in command
        assert "add_application(" not in command


def test_candidate_fit_preflight_precedes_every_resume_workflow_boundary():
    root = Path(__file__).resolve().parents[1]
    command_names = ("resume-team", "resume", "tailor-resume", "batch-resume", "job-fit")

    for name in command_names:
        command = (root / "commands" / f"{name}.md").read_text(encoding="utf-8")
        fit_gate = command.find("python candidate_fit_preflight.py --resume")
        assert fit_gate >= 0, name
        assert "candidate-fit-policy-v3" in command
        assert "threshold" in command and "70.0" in command
        assert "REJECTED:CANDIDATE_FIT" in command
        assert "FAILED:CANDIDATE_FIT_PREFLIGHT" in command
        assert "previous" in command and "tailored" in command and "resume" in command
        assert "no" in command.lower() and "bypass" in command.lower()

        for boundary in (
            "native_resume_team.py",
            "create_resume_from_md_authorized(",
            "add_application_authorized(",
            "init_state(",
        ):
            boundary_offset = command.find(boundary)
            if boundary_offset >= 0:
                assert fit_gate < boundary_offset, (name, boundary)

    for name in ("resume", "tailor-resume", "batch-resume"):
        command = (root / "commands" / f"{name}.md").read_text(encoding="utf-8")
        assert "best_matching_resume" not in command
        assert "base_template_path" not in command
        assert "resume-team-result/v2" in command
        assert "resume-team-final-receipt/v2" in command
        assert "candidate_fit_report_digest" in command

    job_fit = (root / "commands" / "job-fit.md").read_text(encoding="utf-8")
    assert "`60–69`" in job_fit
    assert "blocked for manual reconsideration" in job_fit
    assert "must not automatically tailor or offer a bypass" in job_fit


def test_candidate_fit_contract_is_documented_by_all_runtime_surfaces():
    root = Path(__file__).resolve().parents[1]
    surface_paths = (
        root / "AGENTS.md",
        root / "CLAUDE.md",
        root / "README.md",
        root / "skills" / "resume-team" / "SKILL.md",
    )

    for path in surface_paths:
        content = path.read_text(encoding="utf-8")
        assert "candidate-fit-policy-v3" in content, path.name
        assert "REJECTED:CANDIDATE_FIT" in content, path.name
        assert "FAILED:CANDIDATE_FIT_PREFLIGHT" in content, path.name
        assert "candidate_fit_report" in content, path.name
        assert "resume-team-result/v2" in content, path.name
        assert "resume-team-final-receipt/v2" in content, path.name

    skill = (root / "skills" / "resume-team" / "SKILL.md").read_text(encoding="utf-8")
    fit_gate = skill.find("python candidate_fit_preflight.py --resume")
    native_runtime = skill.find("python native_resume_team.py --host codex --job-description-file")
    assert 0 <= fit_gate < native_runtime
