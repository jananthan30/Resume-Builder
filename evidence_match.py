# evidence_match.py
"""CLI: evidence-based resume–job match.

Usage:
  python evidence_match.py --resume <path> --jd <path> [--json out.json]
                           [--no-dense] [--verify] [--audit-log PATH]
                           [--cache-dir DIR]
  python evidence_match.py --resume <path> --jd <path> --interactive
  python evidence_match.py --replay <result.json>
"""
from __future__ import annotations

import argparse
import json
import sys


def _verify(result, resume_path: str, jd_text: str) -> list[str]:
    """Re-check every offset and citation in the result against source text."""
    from text_extractor import extract_text

    from evidence_engine.evidence.provenance import verify_match_result
    return verify_match_result(result, extract_text(resume_path), jd_text)


class NoTerminal(RuntimeError):
    """``--interactive`` was requested somewhere nobody can answer."""


def _ask(result) -> list:
    """Put the engine's open questions to the user and collect answers.

    Only asked about gaps the engine already judged worth interrupting for.
    An answer authorises a rewrite; it never changes the score.
    """
    from evidence_engine.models import AnswerChoice, GapAnswer
    from evidence_engine.questions import build_questions

    questions = build_questions(result)
    if not questions:
        return []

    # Refuse rather than read EOF and return nothing. When this runs from an
    # agent, a skill, or CI there is no terminal, and silently collecting zero
    # answers is indistinguishable from "the candidate had no hidden
    # experience" — a gate that passes without ever asking. Surfaces that
    # cannot prompt must use the tool or the endpoint instead.
    if not sys.stdin.isatty():
        raise NoTerminal(
            f"--interactive has {len(questions)} question(s) but stdin is not a "
            "terminal, so nobody can answer them. Use the confirm_gaps tool or "
            "POST /api/match/confirm from a non-interactive caller."
        )

    print("\nA few things your resume does not currently show.")
    print("Answering lets the writer surface experience you actually have.")
    print("It does not change your score.\n")

    answers = []
    for index, question in enumerate(questions, start=1):
        print(f"{index}. {question.question}")
        print(f"   {question.why_it_matters}")
        try:
            reply = input("   [y]es / [n]o / [s]kip: ").strip().lower()
        except EOFError:
            break
        choice = ({"y": AnswerChoice.YES, "yes": AnswerChoice.YES,
                   "n": AnswerChoice.NO, "no": AnswerChoice.NO}
                  .get(reply, AnswerChoice.UNSURE))
        detail = ""
        if choice is AnswerChoice.YES:
            try:
                detail = input("   Briefly, where did you do this? ").strip()
            except EOFError:
                detail = ""
        answers.append(GapAnswer(
            requirement_id=question.requirement_id, answer=choice,
            detail=detail, resume_sha256=result.resume_sha256,
            job_sha256=result.job_sha256))
        print()
    return answers


def _replay(path: str) -> int:
    """Recompute a stored result's score from its recorded judgments."""
    from evidence_engine.audit import replay_scoring
    from evidence_engine.models import MatchResult

    with open(path, encoding="utf-8") as handle:
        result = MatchResult(**json.load(handle))
    drift = replay_scoring(result)
    if drift:
        print("REPLAY FAILED — stored score does not reproduce:", file=sys.stderr)
        for item in drift:
            print(f"  - {item}", file=sys.stderr)
        return 1
    print(f"REPLAY OK — {result.match_id} reproduces exactly from stored "
          f"judgments under {result.scoring_policy_version}.")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Evidence-based resume–job match")
    ap.add_argument("--resume")
    ap.add_argument("--jd", help="Path to a JD text file")
    ap.add_argument("--json", dest="json_out", help="Write full MatchResult JSON")
    ap.add_argument("--no-dense", action="store_true", help="Lexical-only retrieval")
    ap.add_argument("--verify", action="store_true",
                    help="Re-check all offsets; exit non-zero on mismatch")
    ap.add_argument("--audit-log", help="Append the audit record to this JSONL file")
    ap.add_argument("--cache-dir", help="Reuse results for identical inputs+versions")
    ap.add_argument("--interactive", action="store_true",
                    help="Ask about the gaps only you can resolve, then show "
                         "what may now be written")
    ap.add_argument("--replay", metavar="RESULT_JSON",
                    help="Recompute a stored result and report any drift")
    args = ap.parse_args()

    if args.replay:
        return _replay(args.replay)
    if not (args.resume and args.jd):
        ap.error("--resume and --jd are required unless --replay is used")

    from evidence_engine.audit import ResultCache
    from evidence_engine.engine import match_resume_to_job
    from evidence_engine.report import render

    with open(args.jd, encoding="utf-8") as f:
        jd_text = f.read()

    result = match_resume_to_job(
        args.resume, jd_text, use_dense=not args.no_dense,
        cache=ResultCache(args.cache_dir) if args.cache_dir else None,
        audit_path=args.audit_log)
    print(render(result))

    if args.interactive and result.status == "OK":
        from evidence_engine.questions import apply_answers

        try:
            answers = _ask(result)
        except NoTerminal as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 2
        if answers:
            updated, rejected = apply_answers(result, answers)
            for problem in rejected:
                print(f"  ignored: {problem}", file=sys.stderr)
            # The score is deliberately unchanged; only what may be written is.
            print("WHAT MAY NOW BE WRITTEN "
                  f"(score unchanged at "
                  f"{result.score_breakdown.qualification_evidence_score:.0%}):")
            answered = {a.requirement_id for a in answers}
            for rec in updated:
                if rec.requirement_id in answered:
                    print(f"- [{rec.type.value}] {rec.recommendation}")
            result.rewrite_recommendations = updated

    if args.json_out:
        with open(args.json_out, "w", encoding="utf-8") as f:
            json.dump(result.model_dump(mode="json"), f, indent=2)

    if args.verify:
        errors = _verify(result, args.resume, jd_text)
        if errors:
            print("\nVERIFY FAILED:", file=sys.stderr)
            for e in errors:
                print(f"  - {e}", file=sys.stderr)
            return 1
        print("\nVERIFY OK — all offsets and citations resolve exactly.")

    return 0 if result.status == "OK" else 2


if __name__ == "__main__":
    raise SystemExit(main())
