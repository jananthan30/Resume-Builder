# evidence_engine/parsing/dates.py
"""Month-precision date parsing and interval math (spec §5.1, §5.5).

Never count mentions of a skill. Experience durations come from the UNION
of month-level intervals; overlapping and adjacent intervals merge.
"""
from __future__ import annotations

import datetime
import re
from typing import Optional

_MONTHS = {
    "jan": 1, "january": 1, "feb": 2, "february": 2, "mar": 3, "march": 3,
    "apr": 4, "april": 4, "may": 5, "jun": 6, "june": 6, "jul": 7,
    "july": 7, "aug": 8, "august": 8, "sep": 9, "sept": 9, "september": 9,
    "oct": 10, "october": 10, "nov": 11, "november": 11, "dec": 12,
    "december": 12,
}

_today = datetime.date.today()
NOW_INDEX: int = _today.year * 12 + (_today.month - 1)


def month_index(year: int, month: int) -> int:
    return year * 12 + (month - 1)


def parse_month(value: str) -> Optional[tuple[int, int]]:
    """Parse a date token to (year, month). None when unparseable — never guess."""
    v = value.strip().lower().rstrip(".")
    if v in {"present", "current", "today", "now"}:
        return (_today.year, _today.month)
    m = re.fullmatch(r"([a-z]+)\s+(\d{4})", v)
    if m and m.group(1) in _MONTHS:
        return (int(m.group(2)), _MONTHS[m.group(1)])
    m = re.fullmatch(r"(\d{1,2})/(\d{4})", v)
    if m and 1 <= int(m.group(1)) <= 12:
        return (int(m.group(2)), int(m.group(1)))
    m = re.fullmatch(r"(\d{4})-(\d{2})", v)
    if m and 1 <= int(m.group(2)) <= 12:
        return (int(m.group(1)), int(m.group(2)))
    m = re.fullmatch(r"(\d{4})", v)
    if m:
        return (int(m.group(1)), 1)
    return None


def merge_intervals(intervals: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """Union of inclusive month-index intervals; overlaps and adjacency merge."""
    if not intervals:
        return []
    ordered = sorted(intervals)
    merged = [ordered[0]]
    for start, end in ordered[1:]:
        last_start, last_end = merged[-1]
        if start <= last_end + 1:  # overlap or adjacent
            merged[-1] = (last_start, max(last_end, end))
        else:
            merged.append((start, end))
    return merged


def total_months(intervals: list[tuple[int, int]]) -> int:
    return sum(end - start + 1 for start, end in intervals)


def months_within_window(
    intervals: list[tuple[int, int]], window_months: int, now_index: int
) -> int:
    floor = now_index - window_months + 1
    clipped = [(max(s, floor), e) for s, e in intervals if e >= floor]
    return total_months(merge_intervals(clipped))


def longest_contiguous_months(intervals: list[tuple[int, int]]) -> int:
    merged = merge_intervals(intervals)
    return max((e - s + 1 for s, e in merged), default=0)
