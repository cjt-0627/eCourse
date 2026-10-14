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
