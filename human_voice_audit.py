"""
human_voice_audit.py — detect AI-sounding resume/cover-letter prose.

Hard gate (like evidence_audit.py) before DOCX generation. Exit 0 = pass,
exit 1 = fail with a fix list.

Checks:
  - Cliché AI openers on bullets (spearheaded, leveraged, …)
  - Banned lexicon / transitional fluff
  - Formulaic summary openers (Results-driven …)
  - Mean bullet length + hard word cap
  - Burstiness (coefficient of variation of bullet word counts)
  - Repeated sentence skeletons
  - Synonym-pair padding (biomedical and scientific, …)
  - Multi-word phrase over-repetition in experience bullets
  - Summary length / sentence count

Usage:
    from human_voice_audit import audit_resume_md, format_audit_report
    report = audit_resume_md('applications/Foo - Bar/resume.md')
    print(format_audit_report(report))

CLI:
    python human_voice_audit.py path/to/resume.md
    python human_voice_audit.py path/to/resume.md --json
    python human_voice_audit.py path/to/cover_letter.md --mode cover_letter
    # exits 0 if passed, 1 if human-voice failures present
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from collections import Counter
from pathlib import Path
from typing import Any

DATA_DIR = Path(__file__).parent / "data"
DEFAULT_TELLS_PATH = DATA_DIR / "ai_tells.json"

# Fallback thresholds if JSON missing
_DEFAULT_THRESHOLDS = {
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

# Markdown artifacts use LF as their only line separator.  Python's
# ``str.splitlines`` recognizes several additional Unicode/control separators
# that the audit's Markdown parser intentionally does not.  Reject them so a
# visually multi-line artifact cannot collapse into one unaudited logical line.
_UNSAFE_LINE_SEPARATORS = ("\r", "\v", "\f", "\x85", "\u2028", "\u2029")


def _unsafe_line_separator_codes(text: str) -> list[str]:
    return [
        f"U+{ord(separator):04X}"
        for separator in _UNSAFE_LINE_SEPARATORS
        if separator in text
    ]


def load_ai_tells(path: str | Path | None = None) -> dict[str, Any]:
    """Load shared AI-tell lexicon; return sensible defaults if missing."""
    p = Path(path) if path else DEFAULT_TELLS_PATH
    if p.exists():
        try:
            with open(p, "r", encoding="utf-8") as f:
                data = json.load(f)
            if "thresholds" not in data:
                data["thresholds"] = dict(_DEFAULT_THRESHOLDS)
            return data
        except Exception:
            pass
    return {
        "cliche_openers": [
            "spearheaded", "leveraged", "utilized", "facilitated", "ensured",
            "demonstrated", "collaborated", "streamlined", "championed", "fostered",
            "harnessed", "navigated", "liaised", "interfaced", "orchestrated",
            "pioneered", "revolutionized", "architected",
        ],
        "banned_words": [
            "delve", "tapestry", "robust", "seamless", "multifaceted", "holistic",
            "synergy", "cutting-edge", "transformative",
        ],
        "banned_phrases": [
            "results-driven", "proven track record", "moreover", "furthermore",
            "in conclusion", "it's not just", "passionate about",
        ],
        "formulaic_summary_openers": [
            "results-driven", "highly motivated", "dynamic professional",
            "seasoned professional", "accomplished professional",
        ],
        "padding_pairs": [
            ["biomedical", "scientific"],
            ["internal", "external"],
            ["complex", "nuanced"],
            ["diverse", "multidisciplinary"],
        ],
        "repeated_skeletons": [
            "translating .+ into",
            "ensuring .+ across",
            "partnering with .+ to",
            "driving .+ through",
            "delivering .+ while",
        ],
        "thresholds": dict(_DEFAULT_THRESHOLDS),
    }


def _is_section_header(line: str) -> bool:
    stripped = line.strip()
    return bool(
        stripped
        and 3 < len(stripped) < 60
        and stripped == stripped.upper()
        and any(c.isalpha() for c in stripped)
        and not stripped.startswith(("•", "-", "*", "_", "─", "═"))
    )


def _extract_sections(md: str) -> dict[str, str]:
    """Parse resume markdown into ALL-CAPS named sections."""
    sections: dict[str, str] = {}
    current: str | None = None
    buffer: list[str] = []
    for line in md.split("\n"):
        stripped = line.strip()
        if _is_section_header(line):
            if current is not None:
                sections[current] = "\n".join(buffer).strip()
            current = stripped
            buffer = []
            continue
        buffer.append(line)
    if current is not None:
        sections[current] = "\n".join(buffer).strip()
    return sections


def _extract_bullets(text: str) -> list[str]:
    bullets: list[str] = []
    for line in text.split("\n"):
        line = line.strip()
        if line.startswith(("•", "-", "*")):
            cleaned = re.sub(r"^[•\-*]\s*", "", line).strip()
            if cleaned:
                bullets.append(cleaned)
    return bullets


def _word_count(text: str) -> int:
    return len(re.findall(r"\b[\w']+\b", text))


def _sentence_count(text: str) -> int:
    # Split on . ! ? while keeping decimals like 4.00 intact enough for summary use
    parts = re.split(r"(?<=[.!?])\s+", text.strip())
    return len([p for p in parts if p.strip()])


def _burstiness_cv(word_counts: list[int]) -> float:
    if len(word_counts) < 3:
        return 0.0
    mean = sum(word_counts) / len(word_counts)
    if mean == 0:
        return 0.0
    variance = sum((w - mean) ** 2 for w in word_counts) / len(word_counts)
    return (variance ** 0.5) / mean


def _first_word(bullet: str) -> str:
    m = re.match(r"^[\W_]*([A-Za-z][\w'-]*)", bullet.strip())
    return m.group(1).lower() if m else ""


def _term_pattern(term: str) -> re.Pattern[str]:
    """Match a lexicon term only at real word boundaries."""
    return re.compile(r"(?<!\w)" + re.escape(term) + r"(?!\w)", re.IGNORECASE)


def _contains_term(text: str, term: str) -> bool:
    return bool(_term_pattern(term).search(text))


def _find_banned_hits(text: str, words: list[str], phrases: list[str]) -> list[str]:
    hits: list[str] = []
    for phrase in phrases:
        if _contains_term(text, phrase):
            hits.append(phrase)
    for word in words:
        if _contains_term(text, word):
            hits.append(word)
    # de-dupe preserving order
    seen: set[str] = set()
    out: list[str] = []
    for h in hits:
        key = h.lower()
        if key not in seen:
            seen.add(key)
            out.append(h)
    return out


def _padding_pair_hits(text: str, pairs: list[list[str]]) -> list[str]:
    lower = text.lower()
    found: list[str] = []
    for pair in pairs:
        if len(pair) < 2:
            continue
        a, b = pair[0].lower(), pair[1].lower()
        # "a and b" or "a & b" within a short window
        pat = rf"\b{re.escape(a)}\b\s*(?:and|&|/)\s*\b{re.escape(b)}\b"
        if re.search(pat, lower):
            found.append(f"{pair[0]} and {pair[1]}")
    return found


def _skeleton_hits(bullets: list[str], patterns: list[str], max_repeats: int) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for pat in patterns:
        try:
            rx = re.compile(pat, re.IGNORECASE)
        except re.error:
            continue
        matches = [b for b in bullets if rx.search(b)]
        if len(matches) > max_repeats:
            results.append({
                "pattern": pat,
                "count": len(matches),
                "examples": [m[:80] for m in matches[:3]],
            })
    return results


def _multiword_phrase_overuse(bullets: list[str], max_repeat: int) -> list[dict[str, Any]]:
    """
    Flag multi-word (2–4 token) phrases that appear more than max_repeat times
    across experience bullets. Filters common stop/noise.
    """
    stop = {
        "the", "and", "for", "with", "from", "into", "across", "through",
        "that", "this", "their", "have", "were", "been", "will", "while",
        "using", "based", "per", "via", "over", "under", "between",
        "clinical", "scientific", "medical", "research", "data", "team",
        "teams", "patient", "patients", "study", "studies", "phase",
    }
    ngram_counts: Counter[str] = Counter()
    for b in bullets:
        tokens = re.findall(r"\b[a-zA-Z][\w'-]*\b", b.lower())
        for n in (2, 3, 4):
            for i in range(len(tokens) - n + 1):
                gram = tokens[i : i + n]
                if any(t in stop or len(t) < 3 for t in gram):
                    continue
                # skip pure number-ish / metric ngrams loosely
                if any(t.isdigit() for t in gram):
                    continue
                ngram_counts[" ".join(gram)] += 1

    overused: list[dict[str, Any]] = []
    for phrase, count in ngram_counts.most_common(40):
        if count > max_repeat:
            overused.append({"phrase": phrase, "count": count})
        if len(overused) >= 10:
            break
    return overused


def _line_record(line_number: int, source_line: str) -> dict[str, Any]:
    return {
        "line_number": line_number,
        "source_line": source_line,
        "line_digest": hashlib.sha256(source_line.encode("utf-8")).hexdigest(),
    }


def _diagnostic_line_sets(text: str, mode: str) -> dict[str, list[dict[str, Any]]]:
    """Return exact source lines used by each human-voice check."""
    records: list[dict[str, Any]] = []
    current_section: str | None = None
    for line_number, source_line in enumerate(text.split("\n"), start=1):
        stripped = source_line.strip()
        if _is_section_header(source_line):
            current_section = stripped
            continue
        if not stripped:
            continue
        if not any(character.isalnum() for character in stripped):
            # Decorative dividers are not editable prose and must not make an
            # otherwise single-line section look multi-line.
            continue
        record = _line_record(line_number, source_line)
        record["stripped"] = stripped
        record["section"] = current_section
        if stripped.startswith(("•", "-", "*")):
            record["bullet"] = re.sub(r"^[•\-*]\s*", "", stripped).strip()
        records.append(record)

    if mode == "cover_letter":
        return {"body": records, "summary": records, "bullets": []}

    summary_records = [
        record
        for record in records
        if record["section"]
        and any(
            label in record["section"]
            for label in ("SUMMARY", "PROFILE", "OBJECTIVE")
        )
    ]
    experience_records = [
        record
        for record in records
        if record["section"] and "EXPERIENCE" in record["section"]
    ]
    bullet_records = [record for record in experience_records if record.get("bullet")]
    if not bullet_records:
        bullet_records = [record for record in records if record.get("bullet")]

    if not any(record["section"] for record in records):
        # Match audit_text's no-section fallback closely while retaining whole lines.
        summary_records = []
        consumed = 0
        for record in records:
            consumed += len(record["source_line"]) + 1
            if consumed - len(record["source_line"]) - 1 < 500:
                summary_records.append(record)

    return {
        "body": records,
        "summary": summary_records,
        "bullets": bullet_records,
    }


def _public_locations(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    locations: list[dict[str, Any]] = []
    seen: set[tuple[int, str]] = set()
    for record in sorted(records, key=lambda item: item["line_number"]):
        key = (record["line_number"], record["line_digest"])
        if key in seen:
            continue
        seen.add(key)
        locations.append({
            "line_number": record["line_number"],
            "source_line": record["source_line"],
            "line_digest": record["line_digest"],
        })
    return locations


def _annotate_failure_diagnostics(
    failures: list[dict[str, Any]],
    text: str,
    mode: str,
    tells: dict[str, Any],
    thresholds: dict[str, Any],
) -> None:
    """Attach conservative, exact-line correction scope to every failure."""
    line_sets = _diagnostic_line_sets(text, mode)
    body_records = line_sets["body"]
    summary_records = line_sets["summary"]
    bullet_records = line_sets["bullets"]
    summary_and_bullets = summary_records + bullet_records
    cliche = {word.lower() for word in tells.get("cliche_openers", [])}
    formulaic = [term.lower() for term in tells.get("formulaic_summary_openers", [])]
    padding_pairs = tells.get("padding_pairs", [])

    for failure in failures:
        code = failure.get("code")
        records: list[dict[str, Any]] = []

        if code == "cliche_openers":
            records = [
                record
                for record in bullet_records
                if _first_word(record.get("bullet", "")) in cliche
            ]
        elif code == "banned_lexicon":
            terms = [term for term in failure.get("fix", []) if isinstance(term, str)]
            candidates = body_records if mode == "cover_letter" else summary_and_bullets
            records = [
                record
                for record in candidates
                if any(_contains_term(record["source_line"], term) for term in terms)
            ]
        elif code == "formulaic_summary":
            records = [
                record
                for record in summary_records
                if any(_contains_term(record["source_line"], term) for term in formulaic)
            ]
        elif code == "formulaic_opener":
            opener_terms = ("i am writing to express", "i am excited to apply")
            records = [
                record
                for record in body_records
                if any(_contains_term(record["source_line"], term) for term in opener_terms)
            ]
        elif code == "hard_bullet_cap":
            records = [
                record
                for record in bullet_records
                if _word_count(record.get("bullet", ""))
                > thresholds["hard_max_bullet_words"]
            ]
        elif code == "repeated_skeletons":
            patterns = [
                item.get("pattern")
                for item in failure.get("fix", [])
                if isinstance(item, dict) and isinstance(item.get("pattern"), str)
            ]
            compiled: list[re.Pattern[str]] = []
            for pattern in patterns:
                try:
                    compiled.append(re.compile(pattern, re.IGNORECASE))
                except re.error:
                    continue
            records = [
                record
                for record in bullet_records
                if any(rx.search(record.get("bullet", "")) for rx in compiled)
            ]
        elif code == "synonym_padding":
            records = [
                record
                for record in summary_and_bullets
                if _padding_pair_hits(record["source_line"], padding_pairs)
            ]
        elif code == "phrase_stuffing":
            phrases = [
                item.get("phrase")
                for item in failure.get("fix", [])
                if isinstance(item, dict) and isinstance(item.get("phrase"), str)
            ]
            records = [
                record
                for record in bullet_records
                if any(_contains_term(record.get("bullet", ""), phrase) for phrase in phrases)
            ]
        elif code in ("summary_too_long", "summary_too_many_sentences"):
            # An aggregate summary failure is safely editable only when its entire
            # scope is one exact source line. Multi-line summaries remain closed.
            if len(summary_records) == 1:
                records = summary_records
        elif code == "transition_fluff":
            terms = ("moreover", "furthermore", "in conclusion", "additionally")
            records = [
                record
                for record in body_records
                if any(_contains_term(record["source_line"], term) for term in terms)
            ]

        # Mean length, burstiness, document length, and any unknown/new code are
        # aggregate or unbound. They must never authorize a scoped line edit.
        locations = _public_locations(records)
        failure["actionable"] = bool(locations)
        failure["locations"] = locations


def audit_text(
    text: str,
    mode: str = "resume",
    tells: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """
    Audit raw markdown/text for human-voice failures.

    mode: 'resume' | 'cover_letter'
    """
    tells = tells or load_ai_tells()
    thr = {**_DEFAULT_THRESHOLDS, **tells.get("thresholds", {})}
    cliche = {w.lower() for w in tells.get("cliche_openers", [])}
    banned_words = tells.get("banned_words", [])
    banned_phrases = tells.get("banned_phrases", [])
    formulaic = [f.lower() for f in tells.get("formulaic_summary_openers", [])]
    padding_pairs = tells.get("padding_pairs", [])
    skeletons = tells.get("repeated_skeletons", [])

    failures: list[dict[str, Any]] = []
    warnings: list[str] = []
    stats: dict[str, Any] = {}

    unsafe_separators = _unsafe_line_separator_codes(text)
    if unsafe_separators:
        failures.append({
            "code": "unsafe_line_separators",
            "message": (
                "Resume text contains non-LF line separators: "
                + ", ".join(unsafe_separators)
                + ". Normalize the artifact to UTF-8 with LF line endings."
            ),
        })

    if mode == "cover_letter":
        body = text.strip()
        bullets: list[str] = []
        summary = body
        # Treat paragraphs as pseudo-bullets for length/burstiness soft checks
        paras = [p.strip() for p in re.split(r"\n\s*\n", body) if p.strip()]
        banned = _find_banned_hits(body, banned_words, banned_phrases)
        if banned:
            failures.append({
                "code": "banned_lexicon",
                "message": f"Banned AI lexicon found: {', '.join(banned[:12])}",
                "fix": banned[:12],
            })
        wc = _word_count(body)
        stats["word_count"] = wc
        if wc > 420:
            failures.append({
                "code": "cover_letter_too_long",
                "message": f"Cover letter is {wc} words (target ≤ 400).",
            })
        # Formulaic openers in first 120 chars
        head = body[:200].lower()
        for f in formulaic + ["i am writing to express", "i am excited to apply", "dear hiring manager"]:
            if f in head and f in ("i am writing to express", "i am excited to apply"):
                failures.append({
                    "code": "formulaic_opener",
                    "message": f"Formulaic cover-letter opener: '{f}'",
                })
                break
        # Transition fluff density
        fluff = sum(1 for p in ("moreover", "furthermore", "in conclusion", "additionally") if p in body.lower())
        if fluff >= 2:
            failures.append({
                "code": "transition_fluff",
                "message": f"Too many AI transition markers ({fluff}). Cut or rewrite.",
            })
        padding = _padding_pair_hits(body, padding_pairs)
        if padding:
            warnings.append(f"Synonym-pair padding: {', '.join(padding)}")
        stats["paragraphs"] = len(paras)
        _annotate_failure_diagnostics(failures, text, mode, tells, thr)
        return {
            "passed": len(failures) == 0,
            "mode": mode,
            "failures": failures,
            "warnings": warnings,
            "stats": stats,
            "human_voice_score": _score_from_failures(failures, warnings, stats, thr, mode="cover_letter"),
        }

    # ---- resume mode ----
    sections = _extract_sections(text)
    summary = ""
    experience = ""
    has_experience_section = False
    for k, v in sections.items():
        if "SUMMARY" in k or "PROFILE" in k or "OBJECTIVE" in k:
            summary = v
        elif "EXPERIENCE" in k:
            has_experience_section = True
            if not experience:
                experience = v
            else:
                experience += "\n" + v

    # Fallback: if no section headers, treat whole text
    if not sections:
        bullets = _extract_bullets(text)
        if not bullets:
            # line-ish bullets without markers
            bullets = [ln.strip() for ln in text.split("\n") if ln.strip().startswith(("•", "-", "*"))]
        summary = text[:500]
    else:
        bullets = _extract_bullets(experience)
        # also collect bullets from any EXPERIENCE-like section already merged

    if not bullets and not has_experience_section:
        # Last resort only when the document has no explicit experience
        # section.  An explicit but empty section must not borrow publication,
        # project, certification, or membership bullets and appear auditable.
        bullets = _extract_bullets(text)

    word_counts = [_word_count(b) for b in bullets]
    mean_wc = (sum(word_counts) / len(word_counts)) if word_counts else 0.0
    cv = _burstiness_cv(word_counts)
    stats.update({
        "bullet_count": len(bullets),
        "word_counts": word_counts,
        "mean_bullet_words": round(mean_wc, 1),
        "burstiness_cv": round(cv, 3),
        "summary_words": _word_count(summary) if summary else 0,
        "summary_sentences": _sentence_count(summary) if summary else 0,
    })

    # 1) Cliché openers
    cliche_hits = []
    for b in bullets:
        fw = _first_word(b)
        if fw in cliche:
            cliche_hits.append({"opener": fw, "bullet": b[:90]})
    cliche_ratio = (len(cliche_hits) / len(bullets)) if bullets else 0.0
    stats["cliche_opener_count"] = len(cliche_hits)
    stats["cliche_opener_ratio"] = round(cliche_ratio, 3)
    if cliche_ratio > thr["max_cliche_opener_ratio"] or (cliche_hits and thr["max_cliche_opener_ratio"] == 0):
        # allow up to ratio; if any and ratio > threshold
        if len(cliche_hits) > max(0, int(len(bullets) * thr["max_cliche_opener_ratio"])):
            failures.append({
                "code": "cliche_openers",
                "message": (
                    f"{len(cliche_hits)} bullet(s) open with AI-cliché verbs "
                    f"(max allowed ≈ {thr['max_cliche_opener_ratio']:.0%} of bullets). "
                    f"Use plain verbs: Led, Built, Wrote, Cut, Reviewed, Ran…"
                ),
                "fix": cliche_hits[:5],
            })

    # 2) Banned lexicon in summary + bullets
    body_for_ban = (summary + "\n" + "\n".join(bullets)).strip()
    banned = _find_banned_hits(body_for_ban, banned_words, banned_phrases)
    # Don't double-count opener verbs already reported if they're only openers
    banned_filtered = [b for b in banned if b.lower() not in cliche]
    if banned_filtered:
        failures.append({
            "code": "banned_lexicon",
            "message": f"Banned AI lexicon: {', '.join(banned_filtered[:12])}",
            "fix": banned_filtered[:12],
        })

    # 3) Formulaic summary openers
    if summary:
        head = summary.strip().lower()[:120]
        for f in formulaic:
            if head.startswith(f) or f in head[:80]:
                failures.append({
                    "code": "formulaic_summary",
                    "message": (
                        f"Formulaic summary opener ('{f}'). "
                        f"Lead with plain identity + one concrete proof, not a template."
                    ),
                })
                break

    # 4) Bullet length
    if bullets and mean_wc > thr["max_mean_bullet_words"]:
        failures.append({
            "code": "mean_bullet_length",
            "message": (
                f"Mean bullet length is {mean_wc:.1f} words "
                f"(max {thr['max_mean_bullet_words']}). Prefer 12–22 words."
            ),
        })
    long_bullets = [
        {"words": wc, "bullet": b[:90]}
        for b, wc in zip(bullets, word_counts)
        if wc > thr["hard_max_bullet_words"]
    ]
    if long_bullets:
        failures.append({
            "code": "hard_bullet_cap",
            "message": (
                f"{len(long_bullets)} bullet(s) exceed {thr['hard_max_bullet_words']} words. "
                f"Split or cut."
            ),
            "fix": long_bullets[:5],
        })

    # 5) Burstiness
    if len(bullets) >= 3 and cv < thr["min_burstiness_cv"]:
        failures.append({
            "code": "low_burstiness",
            "message": (
                f"Bullet length CV is {cv:.3f} (min {thr['min_burstiness_cv']}). "
                f"Mix short punchy bullets (6–12 words) with medium ones — avoid metronome length."
            ),
        })
    elif len(bullets) >= 3 and cv < thr["target_burstiness_cv"]:
        warnings.append(
            f"Burstiness CV {cv:.3f} is below target {thr['target_burstiness_cv']}. "
            f"Add 1–2 short punch bullets."
        )

    # 6) Repeated skeletons
    skel = _skeleton_hits(bullets, skeletons, int(thr["max_skeleton_repeats"]))
    if skel:
        failures.append({
            "code": "repeated_skeletons",
            "message": "Same sentence skeleton repeated too often (AI metronome).",
            "fix": skel,
        })

    # 7) Padding pairs
    padding = _padding_pair_hits(body_for_ban, padding_pairs)
    if padding:
        failures.append({
            "code": "synonym_padding",
            "message": f"Synonym-pair padding (pick one word): {', '.join(padding)}",
            "fix": padding,
        })

    # 8) Phrase overuse in bullets
    overused = _multiword_phrase_overuse(bullets, int(thr["max_phrase_repeat_in_bullets"]))
    # only fail on clear stuffing (count >= 4 or many phrases)
    severe = [o for o in overused if o["count"] >= 4]
    if severe:
        failures.append({
            "code": "phrase_stuffing",
            "message": (
                "Multi-word phrases repeated too often in experience bullets. "
                "Move keywords to Core Competencies; keep bullets factual."
            ),
            "fix": severe[:5],
        })
    elif overused:
        warnings.append(
            "Repeated multi-word phrases in bullets: "
            + ", ".join(f"{o['phrase']}×{o['count']}" for o in overused[:5])
        )

    # 9) Summary length
    if summary:
        sw = _word_count(summary)
        ss = _sentence_count(summary)
        if sw > thr["max_summary_words"]:
            failures.append({
                "code": "summary_too_long",
                "message": f"Summary is {sw} words (max {thr['max_summary_words']}). Target 2–3 short sentences.",
            })
        if ss > thr["max_summary_sentences"]:
            failures.append({
                "code": "summary_too_many_sentences",
                "message": f"Summary has {ss} sentences (max {thr['max_summary_sentences']}).",
            })

    if not bullets and mode == "resume":
        failures.append({
            "code": "no_experience_bullets",
            "message": "No experience bullets found; the resume is not auditable.",
        })

    _annotate_failure_diagnostics(failures, text, mode, tells, thr)
    hv_score = _score_from_failures(failures, warnings, stats, thr, mode="resume")
    return {
        "passed": len(failures) == 0,
        "mode": mode,
        "failures": failures,
        "warnings": warnings,
        "stats": stats,
        "human_voice_score": hv_score,
        "bullet_count": len(bullets),
    }


def _score_from_failures(
    failures: list[dict[str, Any]],
    warnings: list[str],
    stats: dict[str, Any],
    thr: dict[str, Any],
    mode: str,
) -> float:
    """Rough 1–10 human-voice score for reports."""
    score = 10.0
    score -= 1.5 * len(failures)
    score -= 0.3 * len(warnings)
    if mode == "resume":
        cv = stats.get("burstiness_cv", 0) or 0
        if cv >= thr.get("ideal_burstiness_cv", 0.40):
            score += 0.5
        elif cv < thr.get("min_burstiness_cv", 0.25):
            score -= 0.5
        mean = stats.get("mean_bullet_words", 0) or 0
        if 12 <= mean <= 22:
            score += 0.5
    return round(max(1.0, min(10.0, score)), 1)


def audit_resume_md(resume_path: str, tells_path: str | None = None) -> dict[str, Any]:
    with open(resume_path, "r", encoding="utf-8") as f:
        text = f.read()
    tells = load_ai_tells(tells_path)
    report = audit_text(text, mode="resume", tells=tells)
    report["path"] = resume_path
    return report


def audit_cover_letter_md(path: str, tells_path: str | None = None) -> dict[str, Any]:
    with open(path, "r", encoding="utf-8") as f:
        text = f.read()
    tells = load_ai_tells(tells_path)
    report = audit_text(text, mode="cover_letter", tells=tells)
    report["path"] = path
    return report


def format_audit_report(report: dict[str, Any]) -> str:
    lines: list[str] = []
    status = "PASSED" if report.get("passed") else "FAILED"
    lines.append("=" * 70)
    lines.append(f"  HUMAN VOICE AUDIT — {status}")
    lines.append("=" * 70)
    if report.get("path"):
        lines.append(f"  File: {report['path']}")
    lines.append(f"  Mode: {report.get('mode', 'resume')}")
    lines.append(f"  Human Voice Score: {report.get('human_voice_score', '?')}/10")
    stats = report.get("stats") or {}
    if stats:
        lines.append("-" * 70)
        lines.append("  STATS")
        for k in (
            "bullet_count", "mean_bullet_words", "burstiness_cv",
            "cliche_opener_count", "cliche_opener_ratio",
            "summary_words", "summary_sentences", "word_count", "paragraphs",
        ):
            if k in stats:
                lines.append(f"    {k}: {stats[k]}")
    failures = report.get("failures") or []
    if failures:
        lines.append("-" * 70)
        lines.append(f"  FAILURES ({len(failures)})")
        for i, f in enumerate(failures, 1):
            lines.append(f"  {i}. [{f.get('code', '?')}] {f.get('message', '')}")
            fix = f.get("fix")
            if isinstance(fix, list) and fix:
                for item in fix[:5]:
                    lines.append(f"       - {item}")
    warnings = report.get("warnings") or []
    if warnings:
        lines.append("-" * 70)
        lines.append(f"  WARNINGS ({len(warnings)})")
        for w in warnings:
            lines.append(f"    • {w}")
    if report.get("passed"):
        lines.append("-" * 70)
        lines.append("  All hard human-voice checks passed. Safe to proceed to DOCX.")
    else:
        lines.append("-" * 70)
        lines.append("  FIX failures, then re-run: python human_voice_audit.py <file>")
        lines.append("  Do NOT generate DOCX until this audit passes.")
    lines.append("=" * 70)
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Audit resume or cover-letter prose for human-voice failures."
    )
    parser.add_argument("path", help="Markdown/text file to audit")
    parser.add_argument(
        "--mode",
        choices=("resume", "cover_letter"),
        default="resume",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        dest="json_output",
        help="emit only the machine-readable audit report",
    )
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)

    path = args.path
    mode = args.mode

    if not Path(path).exists():
        print(f"File not found: {path}", file=sys.stderr)
        return 2

    if mode == "cover_letter":
        report = audit_cover_letter_md(path)
    else:
        # auto-detect cover letter filenames
        name = Path(path).name.lower()
        if "cover" in name:
            report = audit_cover_letter_md(path)
        else:
            report = audit_resume_md(path)

    if args.json_output:
        print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    else:
        print(format_audit_report(report))
    return 0 if report.get("passed") else 1


if __name__ == "__main__":
    sys.exit(main())
