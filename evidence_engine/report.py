# evidence_engine/report.py
"""Human-readable rendering. Every row cites exact evidence (spec §5.7)."""
from __future__ import annotations

from evidence_engine.models import MatchResult


def _bar(score: float | None) -> str:
    if score is None:
        return "----------"
    filled = round(score * 10)
    return "█" * filled + "░" * (10 - filled)


def render(result: MatchResult) -> str:
    if result.status == "ERROR":
        return f"EVIDENCE MATCH — ERROR\n{result.error}\n"

    bd = result.score_breakdown
    lines = [
        f"QUALIFICATION EVIDENCE MATCH        {_bar(bd.qualification_evidence_score)} "
        f"{bd.qualification_evidence_score:.0%}",
        f"Eligibility: {result.eligibility.status.value}"
        + ("" if not result.eligibility.gates else
           "  (" + ", ".join(f"{g.requirement}={g.status.value}"
                            for g in result.eligibility.gates) + ")"),
        f"Must-have evidence: {bd.must_have_score:.0%}   "
        f"Evidence quality: {bd.evidence_quality:.0%}   "
        f"Role similarity (diagnostic): {bd.role_similarity:.0%}",
        f"Relevant experience: {result.durations.strict_relevant_years}y strict, "
        f"{result.durations.adjacent_relevant_years}y adjacent, "
        f"{result.durations.recent_relevant_years}y recent",
        "",
    ]
    spans_by_id = {s.evidence_span_id: s for s in result.evidence_spans}
    reqs_by_id = {r.requirement_id: r for r in result.requirements}
    for m in result.matches:
        req = reqs_by_id[m.requirement_id]
        lines.append(f"[{m.status.value}] {req.normalized_text} "
                     f"({req.importance.value})"
                     + (f" — {m.score:.0%}" if m.score is not None else ""))
        lines.append(f"    {m.reason}")
        for sid in m.evidence_span_ids:
            span = spans_by_id[sid]
            lines.append(f"    ↳ “{span.text}”"
                         + (f" [{span.start_date}–{span.end_date or 'Present'}]"
                            if span.start_date else ""))
        if m.gap:
            lines.append(f"    gap: {m.gap}")
    if result.rewrite_recommendations:
        lines += ["", "RECOMMENDATIONS (truth-confirmed only):"]
        for rec in result.rewrite_recommendations:
            lines.append(f"- [{rec.priority}] {rec.type.value}: {rec.recommendation}")
    lines += ["", f"policy={result.scoring_policy_version} "
                  f"prompts={result.prompt_version} "
                  f"model={result.judge_model}"]
    lines += ["", "WARNINGS:"] + [f"- {w}" for w in result.warnings]
    return "\n".join(lines)
