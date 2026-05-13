"""
evidence_audit.py — verify Core Competencies trace to bullet evidence.

Used by the resume tailoring workflow to prevent the "I list X in Core
Competencies but have no bullet demonstrating X" pattern — the failure mode
that produces strong tool scores but blows up in 5-minute interview defense.

Strategy: for each Core Competency item, check whether at least one of its
key phrases appears (word-boundary match) in the resume's Summary or
Professional Experience bullets. Items can opt out by adding an exposure
qualifier — (exposure), (coursework), (trainable), (familiar), (in progress),
(rapid trainability) — to be transparent about depth.

Usage:
    from evidence_audit import audit_resume_md, format_audit_report
    report = audit_resume_md('applications/Foo - Bar/resume.md')
    print(format_audit_report(report))

CLI:
    python evidence_audit.py path/to/resume.md
    # exits 0 if passed, 1 if any item is unsupported
"""

from __future__ import annotations

import re
import sys
from typing import Any


_EXPOSURE_MARKERS = [
    r'\(exposure\)',
    r'\(coursework\)',
    r'\(trainable\)',
    r'\(familiar\)',
    r'\(rapid\s+trainability\)',
    r'\(in\s+progress\)',
    r'\(learning\)',
]
_EXPOSURE_RE = re.compile('|'.join(_EXPOSURE_MARKERS), re.IGNORECASE)


def _extract_sections(md: str) -> dict[str, str]:
    """Parse a resume markdown file into named ALL-CAPS sections."""
    sections: dict[str, str] = {}
    current: str | None = None
    buffer: list[str] = []
    for line in md.split('\n'):
        stripped = line.strip()
        # ALL-CAPS header line (allow & | , — and digits)
        if (
            stripped
            and len(stripped) < 60
            and len(stripped) > 3
            and stripped == stripped.upper()
            and any(c.isalpha() for c in stripped)
            and not stripped.startswith(('•', '-', '*', '_', '─', '═'))
        ):
            # Heuristic: skip lines that look like names/contact (NAME, CREDENTIALS)
            if ',' in stripped and any(t in stripped.upper() for t in ['MD', 'PHD', 'PHARMD', 'MPH', 'DO', 'RN']):
                # Could be a header credentials line — only treat as a section if no comma
                pass
            if current is not None:
                sections[current] = '\n'.join(buffer).strip()
            current = stripped
            buffer = []
            continue
        buffer.append(line)
    if current is not None:
        sections[current] = '\n'.join(buffer).strip()
    return sections


def _extract_core_comp_items(core_comp_text: str) -> list[str]:
    """Split Core Competencies into individual items."""
    items: list[str] = []
    for line in core_comp_text.split('\n'):
        line = line.strip()
        if not line:
            continue
        # Accept lines starting with bullet markers or plain text
        line = re.sub(r'^[•\-*]\s*', '', line)
        if not line or line.startswith('___') or line.startswith('==='):
            continue
        # Split on pipe separator
        for item in line.split('|'):
            item = item.strip()
            if item and len(item) > 2:
                items.append(item)
    return items


def _extract_bullets(experience_text: str) -> list[str]:
    """Extract bullets from Professional Experience section."""
    bullets: list[str] = []
    for line in experience_text.split('\n'):
        line = line.strip()
        if line.startswith(('•', '-', '*')):
            cleaned = re.sub(r'^[•\-*]\s*', '', line)
            if cleaned:
                bullets.append(cleaned)
    return bullets


def _extract_phrases(item: str) -> list[str]:
    """Split a Core Comp item into searchable key phrases."""
    # Drop parenthetical qualifiers — those describe scope, not the skill
    cleaned = re.sub(r'\([^)]*\)', '', item).strip()
    # Split on conjunctions, em/en dashes, commas, semicolons
    parts = re.split(r'\s*(?:&|\band\b|—|–|,|;|/)\s*', cleaned, flags=re.IGNORECASE)
    out: list[str] = []
    for p in parts:
        p = p.strip().rstrip('.,;:').strip()
        # Drop articles / generic adjectives at start
        p = re.sub(
            r'^(?:the|a|an|advanced|strategic|cross-functional)\s+',
            '',
            p,
            flags=re.IGNORECASE,
        ).strip()
        if p and len(p) > 2:
            out.append(p)
    return out


def _phrase_in_text(phrase: str, text: str) -> bool:
    """Word-boundary match of `phrase` in lowercased text."""
    pat = r'\b' + re.escape(phrase.lower()).replace(r'\ ', r'\s+') + r'\b'
    return bool(re.search(pat, text))


_LOOSE_STOP = {
    'with', 'into', 'from', 'this', 'that', 'these', 'those', 'have',
    'will', 'your', 'their', 'about', 'which', 'they', 'them', 'than',
    'when', 'while', 'where', 'over', 'under',
    # generic adjectives that shouldn't carry evidence
    'strong', 'effective', 'proven', 'advanced', 'strategic',
    # very short noise
    'and', 'the', 'for', 'in', 'of', 'to', 'on', 'at', 'by',
}


def _stem_variants(token: str) -> list[str]:
    """Tiny stem-aware variant generator: handle s/no-s singular/plural pairs.

    Just enough to keep the audit from rejecting "publications" when the
    resume uses "publication", or "materials" when it uses "material".
    """
    t = token.lower()
    out = [t]
    if t.endswith('ies') and len(t) > 4:
        out.append(t[:-3] + 'y')  # "competencies" → "competency"
    elif t.endswith('es') and len(t) > 3:
        out.append(t[:-2])         # "reviewes" → "review"
        out.append(t[:-1])         # "processes" → "process"
    elif t.endswith('s') and len(t) > 3:
        out.append(t[:-1])         # "publications" → "publication"
    else:
        out.append(t + 's')        # "publication" → "publications"
        if t.endswith('y') and len(t) > 3:
            out.append(t[:-1] + 'ies')
    return out


def _token_appears(token: str, text: str) -> bool:
    """Check stem variants of a token; True if any variant is found."""
    return any(_phrase_in_text(v, text) for v in _stem_variants(token))


def _has_evidence_loose(phrase: str, text: str) -> bool:
    """Looser fallback: all key tokens of `phrase` must appear somewhere in
    text (not necessarily adjacent). Uses stem variants so plural/singular
    mismatches don't cause false negatives."""
    tokens = re.findall(r'\b\w[\w\-]*\b', phrase.lower())
    keys = [t for t in tokens if len(t) >= 4 and t not in _LOOSE_STOP]
    if not keys or len(keys) == 1:
        return False  # one-token phrase: strict already covered it
    return all(_token_appears(k, text) for k in keys)


def _has_evidence(item: str, bullets: list[str], summary: str, extras: str = '') -> bool:
    """True if at least one key phrase of `item` is backed by bullets, summary,
    or extras (Publications / Education / Certifications). Tries adjacent
    phrase first, then a looser all-key-tokens-present check."""
    phrases = _extract_phrases(item)
    if not phrases:
        return True  # nothing to check (item was all parentheticals)
    haystack = (summary + ' ' + ' '.join(bullets) + ' ' + extras).lower()
    # 1) strict: full phrase as adjacent tokens
    if any(_phrase_in_text(p, haystack) for p in phrases):
        return True
    # 2) loose: all key tokens present (order/distance OK) — catches things like
    #    "Peer-Reviewed Publications" backed by "peer-reviewed case reports"
    #    + the Publications section.
    if any(_has_evidence_loose(p, haystack) for p in phrases):
        return True
    return False


def audit_resume_md(resume_path: str) -> dict[str, Any]:
    """
    Check that every Core Competencies item is backed by evidence in Summary
    or Professional Experience bullets.

    Returns a dict:
        passed (bool)       — True iff every non-exposure-marked item has evidence
        total_items (int)
        supported (list)    — items found in bullets/summary
        unsupported (list)  — items with no anchor (action required)
        exposure_marked (list) — items with (exposure)/(coursework)/etc.
        warnings (list[str])
    """
    with open(resume_path, 'r', encoding='utf-8') as f:
        md = f.read()

    sections = _extract_sections(md)
    core_comp = ''
    summary = ''
    experience = ''
    for k, v in sections.items():
        if 'CORE COMPETENC' in k:
            core_comp = v
        elif 'SUMMARY' in k or 'PROFILE' in k or 'OBJECTIVE' in k:
            summary = v
        elif 'EXPERIENCE' in k and 'EARLIER' not in k:
            # Take the first match (PROFESSIONAL EXPERIENCE) — Earlier rolled in via bullet scan
            if not experience:
                experience = v

    items = _extract_core_comp_items(core_comp)
    bullets = _extract_bullets(experience)
    # Also scan bullets from any other ALL-CAPS section with EXPERIENCE in name
    for k, v in sections.items():
        if 'EXPERIENCE' in k and v is not experience:
            bullets.extend(_extract_bullets(v))

    # Publications / Education / Certifications / Projects can also back
    # Core Comp items (e.g., "Peer-Reviewed Publications", "Board Certification").
    # Include the section HEADER as evidence too — e.g., a populated
    # PUBLICATIONS section is itself evidence the candidate has publications,
    # even if individual citation lines don't contain the word "publications".
    extras_parts: list[str] = []
    for k, v in sections.items():
        if any(t in k for t in ('PUBLICATION', 'EDUCATION', 'CERTIFICATION', 'LICENSURE', 'PROJECT', 'MEMBERSHIP')):
            if v.strip():
                extras_parts.append(k.lower())  # header word ("publications")
                extras_parts.append(v)
    extras = ' '.join(extras_parts)

    supported: list[str] = []
    unsupported: list[str] = []
    exposure_marked: list[str] = []

    for item in items:
        if _EXPOSURE_RE.search(item):
            exposure_marked.append(item)
            continue
        if _has_evidence(item, bullets, summary, extras):
            supported.append(item)
        else:
            unsupported.append(item)

    warnings: list[str] = []
    if unsupported:
        warnings.append(
            f"{len(unsupported)} Core Competency item(s) lack evidence in bullets "
            f"or summary. For each: add a bullet demonstrating the skill, OR add an "
            f"exposure qualifier — (exposure)/(coursework)/(trainable)/(familiar) — "
            f"to be transparent, OR remove the item."
        )
        for u in unsupported:
            warnings.append(f"  - UNSUPPORTED: {u}")

    return {
        'passed': len(unsupported) == 0,
        'total_items': len(items),
        'supported': supported,
        'unsupported': unsupported,
        'exposure_marked': exposure_marked,
        'warnings': warnings,
    }


def format_audit_report(report: dict[str, Any]) -> str:
    """Render an audit dict as a readable string for CLI / log output."""
    lines: list[str] = []
    lines.append('═══ EVIDENCE AUDIT ═══════════════════════════════════════════════')
    lines.append(f'Core Competency items total : {report["total_items"]}')
    lines.append(f'Backed by bullets/summary   : {len(report["supported"])}')
    lines.append(f'Marked as exposure-only     : {len(report["exposure_marked"])}')
    lines.append(f'UNSUPPORTED (action needed) : {len(report["unsupported"])}')
    lines.append('')
    if report['unsupported']:
        lines.append('Unsupported items:')
        for item in report['unsupported']:
            lines.append(f'  ✗ {item}')
        lines.append('')
        lines.append('Fix each by ONE of:')
        lines.append('  1. Add a bullet to Professional Experience that demonstrates the skill')
        lines.append('  2. Add an exposure qualifier:  Veeva Vault (trainable)')
        lines.append('  3. Remove the item from Core Competencies')
    else:
        lines.append('✓ All Core Competency items are evidence-backed.')
    return '\n'.join(lines)


if __name__ == '__main__':
    if len(sys.argv) != 2:
        print('Usage: python evidence_audit.py <resume.md>')
        sys.exit(2)
    report = audit_resume_md(sys.argv[1])
    print(format_audit_report(report))
    sys.exit(0 if report['passed'] else 1)
