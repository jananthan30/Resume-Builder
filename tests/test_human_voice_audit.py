"""Focused regressions for deterministic human-voice diagnostics."""

import hashlib
import json

import pytest

from human_voice_audit import (
    _DEFAULT_THRESHOLDS,
    audit_text,
    load_ai_tells,
    main,
)


def _resume(summary: str, bullets: list[str]) -> str:
    rendered_bullets = "\n".join(f"• {bullet}" for bullet in bullets)
    return (
        "PROFESSIONAL SUMMARY\n"
        f"{summary}\n\n"
        "PROFESSIONAL EXPERIENCE\n"
        "Medical Reviewer | Example Company\n"
        "2020 - Present\n"
        f"{rendered_bullets}\n"
    )


def _failure(report: dict, code: str) -> dict:
    return next(item for item in report["failures"] if item["code"] == code)


def test_banned_phrase_uses_real_word_boundaries():
    bullets = [
        "Reviewed reports.",
        "Wrote clear narratives for cases.",
        "Led medical review across several programs and study teams.",
    ]
    false_positive = audit_text(
        _resume("Plain summary of medical safety work.", bullets)
    )
    assert "banned_lexicon" not in {
        item["code"] for item in false_positive["failures"]
    }

    exact_phrase = audit_text(
        _resume("In summary, I reviewed medical safety reports.", bullets)
    )
    assert "banned_lexicon" in {
        item["code"] for item in exact_phrase["failures"]
    }


def test_actionable_failure_preserves_exact_full_source_line():
    long_bullet = "  • " + " ".join(f"token{index}" for index in range(1, 30))
    text = (
        "PROFESSIONAL SUMMARY\n"
        "Medical reviewer.\n\n"
        "PROFESSIONAL EXPERIENCE\n"
        "Medical Reviewer | Example Company\n"
        "2020 - Present\n"
        f"{long_bullet}\n"
    )

    failure = _failure(audit_text(text), "hard_bullet_cap")
    assert failure["actionable"] is True
    assert failure["locations"] == [
        {
            "line_number": 7,
            "source_line": long_bullet,
            "line_digest": hashlib.sha256(long_bullet.encode("utf-8")).hexdigest(),
        }
    ]
    assert len(failure["locations"][0]["source_line"]) > 90


def test_single_line_summary_failures_share_stable_exact_anchor():
    summary = " ".join(["Clinical and scientific"] + ["evidence"] * 68) + ". Next. More. Last."
    text = _resume(summary, ["Reviewed cases."])

    first = audit_text(text)
    second = audit_text(text)
    expected_location = {
        "line_number": 2,
        "source_line": summary,
        "line_digest": hashlib.sha256(summary.encode("utf-8")).hexdigest(),
    }
    for code in (
        "synonym_padding",
        "summary_too_long",
        "summary_too_many_sentences",
    ):
        first_failure = _failure(first, code)
        second_failure = _failure(second, code)
        assert first_failure["actionable"] is True
        assert first_failure["locations"] == [expected_location]
        assert second_failure["locations"] == first_failure["locations"]


def test_aggregate_failure_is_explicitly_non_actionable():
    report = audit_text(
        _resume(
            "Medical reviewer.",
            [
                "Reviewed individual safety case reports.",
                "Prepared individual aggregate safety reports.",
                "Drafted individual medical review comments.",
            ],
        )
    )
    failure = _failure(report, "low_burstiness")
    assert failure["actionable"] is False
    assert failure["locations"] == []


@pytest.mark.parametrize("separator", ["\r", "\v", "\f", "\x85", "\u2028", "\u2029"])
def test_non_lf_line_separators_fail_closed(separator):
    text = _resume(
        "Medical reviewer.",
        ["Reviewed reports.", "Wrote narratives.", "Led safety review."],
    ).replace("\n", separator)
    report = audit_text(text)
    failure = _failure(report, "unsafe_line_separators")
    assert report["passed"] is False
    assert failure["actionable"] is False
    assert failure["locations"] == []


def test_resume_without_experience_bullets_fails_closed():
    report = audit_text(
        "PROFESSIONAL SUMMARY\nMedical reviewer.\n\n"
        "PROFESSIONAL EXPERIENCE\nMedical Reviewer | Example Company\n"
        "2020 - Present\n"
    )
    failure = _failure(report, "no_experience_bullets")
    assert report["passed"] is False
    assert failure["actionable"] is False
    assert failure["locations"] == []


def test_empty_experience_does_not_borrow_other_section_bullets():
    report = audit_text(
        "PROFESSIONAL SUMMARY\nMedical reviewer.\n\n"
        "PROFESSIONAL EXPERIENCE\nMedical Reviewer | Example Company\n"
        "2020 - Present\n\n"
        "PUBLICATIONS\n• Reviewed medical safety case report.\n"
        "PROFESSIONAL MEMBERSHIPS\n• Example Society\n"
    )
    assert report["bullet_count"] == 0
    assert _failure(report, "no_experience_bullets")["actionable"] is False


def test_safety_thresholds_are_unchanged_and_behaviorally_enforced():
    expected = {
        "max_mean_bullet_words": 24,
        "hard_max_bullet_words": 28,
        "min_burstiness_cv": 0.25,
        "target_burstiness_cv": 0.30,
        "ideal_burstiness_cv": 0.40,
        "max_summary_words": 70,
        "max_summary_sentences": 3,
        "max_cliche_opener_ratio": 0.05,
        "max_phrase_repeat_in_bullets": 2,
        "max_skeleton_repeats": 2,
    }
    assert _DEFAULT_THRESHOLDS == expected
    configured = load_ai_tells()["thresholds"]
    assert {key: configured[key] for key in expected} == expected

    report = audit_text(
        _resume(
            "One. Two. Three. Four. " + " ".join(["word"] * 71),
            [" ".join(f"term{index}" for index in range(29))],
        )
    )
    codes = {item["code"] for item in report["failures"]}
    assert "hard_bullet_cap" in codes
    assert "summary_too_long" in codes
    assert "summary_too_many_sentences" in codes


def test_json_cli_is_machine_only_and_default_remains_human(tmp_path, capsys):
    path = tmp_path / "resume.md"
    path.write_text(
        _resume("In summary, reviewed cases.", ["Reviewed cases."]),
        encoding="utf-8",
    )

    assert main([str(path), "--json"]) == 1
    json_output = capsys.readouterr()
    report = json.loads(json_output.out)
    assert report["path"] == str(path)
    assert report["passed"] is False
    assert "HUMAN VOICE AUDIT" not in json_output.out
    assert json_output.err == ""

    assert main([str(path)]) == 1
    human_output = capsys.readouterr()
    assert "HUMAN VOICE AUDIT — FAILED" in human_output.out
    assert human_output.err == ""


def test_json_cli_rejects_unknown_arguments(tmp_path):
    path = tmp_path / "resume.md"
    path.write_text("Medical reviewer.", encoding="utf-8")
    with pytest.raises(SystemExit) as exc_info:
        main([str(path), "--unknown"])
    assert exc_info.value.code == 2
