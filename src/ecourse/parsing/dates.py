from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

from ecourse.adapters.base import DatePrecision

TAIPEI = ZoneInfo("Asia/Taipei")


@dataclass(frozen=True, slots=True)
class ParsedDate:
    precision: DatePrecision
    due_at: datetime | None = None
    due_date: date | None = None
    source_text: str | None = None


def parse_nthu_date(value: str | None) -> ParsedDate:
    """Parse only unambiguous NTHU dates; never invent a missing time/year."""
    source = " ".join((value or "").split())
    if not source or source in {"-", "--", "無", "未提供", "No due date"}:
        return ParsedDate(DatePrecision.UNKNOWN, source_text=source or None)
    normalized = source.replace("年", "/").replace("月", "/").replace("日", "")
    normalized = re.sub(r"\([^)]*(?:週|星期)[^)]*\)", " ", normalized)
    normalized = re.sub(r"\s*/\s*", "/", normalized)
    match = re.search(r"(?<!\d)(\d{4})[/-](\d{1,2})[/-](\d{1,2})(?:\s+(\d{1,2}):(\d{2})(?::(\d{2}))?)?", normalized)
    if not match:
        return ParsedDate(DatePrecision.UNKNOWN, source_text=source)
    year, month, day = map(int, match.group(1, 2, 3))
    try:
        if match.group(4) is None:
            return ParsedDate(DatePrecision.DATE_ONLY, due_date=date(year, month, day), source_text=source)
        local = datetime(year, month, day, int(match.group(4)), int(match.group(5)), int(match.group(6) or 0), tzinfo=TAIPEI)
    except ValueError:
        return ParsedDate(DatePrecision.UNKNOWN, source_text=source)
    return ParsedDate(DatePrecision.EXACT, due_at=local.astimezone(UTC), source_text=source)


def _taipei_date(now: datetime | date | None = None) -> date:
    if now is None:
        return datetime.now(TAIPEI).date()
    if isinstance(now, datetime):
        return (now.astimezone(TAIPEI) if now.tzinfo else now.replace(tzinfo=TAIPEI)).date()
    return now


def current_nthu_terms(now: datetime | date | None = None) -> tuple[str, ...]:
    """Return active 5-digit NTHU term prefixes (ROC year + 10/20/30) for ``now``."""
    today = _taipei_date(now)
    if today.month >= 8:
        roc_year = today.year - 1911
        return (f"{roc_year}10", f"{roc_year - 1}30") if today.month == 8 else (f"{roc_year}10",)
    if today.month == 1:
        roc_year = today.year - 1 - 1911
        return (f"{roc_year}10",)
    roc_year = today.year - 1 - 1911
    return (f"{roc_year}20", f"{roc_year}30") if today.month == 7 else (f"{roc_year}20",)


def current_nthu_term_window(now: datetime | date | None = None) -> tuple[date, date]:
    """Return a buffered (start_date, end_date) covering the active NTHU semester."""
    today = _taipei_date(now)
    if today.month >= 8:
        return date(today.year, 8, 1), date(today.year + 1, 2, 28)
    if today.month == 1:
        return date(today.year - 1, 8, 1), date(today.year, 2, 28)
    return date(today.year, 1, 1), date(today.year, 8, 31)


def is_current_nthu_course_code(course_code: str | None, now: datetime | date | None = None) -> bool:
    """True when ``course_code`` starts with an active 5-digit NTHU term code."""
    if not course_code:
        return False
    match = re.match(r"^(\d{5})[A-Z]", course_code.strip().upper())
    if not match:
        return False
    return match.group(1) in current_nthu_terms(now)


def is_within_current_nthu_term(value: datetime | date | None, now: datetime | date | None = None) -> bool:
    """True if ``value`` is undated or falls inside the buffered current NTHU semester."""
    if value is None:
        return True
    target = _taipei_date(value)
    start, end = current_nthu_term_window(now)
    return start <= target <= end
