"""Fail-closed structural regressions for the evidence audit."""

from pathlib import Path

from evidence_audit import audit_resume_md


def _write(tmp_path: Path, text: str) -> dict:
    path = tmp_path / "resume.md"
    path.write_text(text, encoding="utf-8")
    return audit_resume_md(str(path))


def _valid_resume() -> str:
    return (
        "PROFESSIONAL SUMMARY\nMedical reviewer.\n\n"
        "CORE COMPETENCIES\n• Medical Review\n\n"
        "PROFESSIONAL EXPERIENCE\nMedical Reviewer | Example Company\n"
        "2020 - Present\n• Completed medical review for safety cases.\n"
    )


def test_non_lf_unicode_separator_fails_closed(tmp_path):
    report = _write(tmp_path, _valid_resume().replace("\n", "\u2028"))
    assert report["passed"] is False
    assert report["structural_errors"] == [
        "UNSAFE_LINE_SEPARATORS:U+2028",
        "CORE_COMPETENCIES_SECTION_MISSING",
        "EXPERIENCE_SECTION_MISSING",
    ]


def test_missing_core_competencies_is_not_zero_item_success(tmp_path):
    text = _valid_resume().replace(
        "CORE COMPETENCIES\n• Medical Review\n\n",
        "",
    )
    report = _write(tmp_path, text)
    assert report["passed"] is False
    assert "CORE_COMPETENCIES_SECTION_MISSING" in report["structural_errors"]


def test_empty_experience_cannot_borrow_publication_bullets(tmp_path):
    text = (
        "PROFESSIONAL SUMMARY\nMedical reviewer.\n\n"
        "CORE COMPETENCIES\n• Medical Review\n\n"
        "PROFESSIONAL EXPERIENCE\nMedical Reviewer | Example Company\n"
        "2020 - Present\n\n"
        "PUBLICATIONS\n• Medical Review case report.\n"
    )
    report = _write(tmp_path, text)
    assert report["passed"] is False
    assert "EXPERIENCE_BULLETS_MISSING" in report["structural_errors"]
