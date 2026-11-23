"""Teacher and class-period lookup backed by NTHU's public course open data.

The registrar publishes every course of the current semester (科號, 授課教師,
教室與上課時間 …) as a JSON file that is refreshed daily. eCourse keeps a
compact local index of it and refreshes that index in the background, so page
renders never wait on the network.
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from pathlib import Path
from typing import Any, Callable

import httpx

log = logging.getLogger(__name__)

DEFAULT_URL = "https://www.ccxp.nthu.edu.tw/ccxp/INQUIRE/JH/OPENDATA/open_course_data.json"
REFRESH_SECONDS = 24 * 3600
RETRY_SECONDS = 15 * 60
PERIOD_ORDER = "1234n56789abc"
WEEKDAYS = {
    "M": ("一", "Mon"), "T": ("二", "Tue"), "W": ("三", "Wed"), "R": ("四", "Thu"),
    "F": ("五", "Fri"), "S": ("六", "Sat"), "U": ("日", "Sun"),
}
_SLOT = re.compile(r"([MTWRFSU])([1-9nabc])")


def _code_key(value: str | None) -> str:
    return re.sub(r"\s+", "", value or "").upper()


def build_index(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Reduce the raw open-data rows to ``code -> {teachers, slots}``."""
    index: dict[str, dict[str, Any]] = {}
    for row in rows:
        code = _code_key(row.get("科號"))
        if not code:
            continue
        teachers: list[dict[str, str]] = []
        for line in str(row.get("授課教師") or "").splitlines():
            parts = [part.strip() for part in line.split("\t")]
            if parts and parts[0]:
                teachers.append({"zh": parts[0], "en": parts[1] if len(parts) > 1 and parts[1] else parts[0]})
        slots: list[str] = []
        for line in str(row.get("教室與上課時間") or "").splitlines():
            time_text = line.split("\t")[1] if "\t" in line else line
            slots.extend(day + period for day, period in _SLOT.findall(time_text) if day + period not in slots)
        index[code] = {"teachers": teachers, "slots": slots}
    return index


def format_periods(slots: list[str], locale: str = "zh-Hant") -> str:
    """``["T3", "T4", "R3", "R4"]`` -> ``"二 3-4、四 3-4"``."""
    english = locale == "en"
    by_day: dict[str, list[str]] = {}
    for slot in slots:
        by_day.setdefault(slot[0], []).append(slot[1])
    parts = []
    for day in "MTWRFSU":
        if day not in by_day:
            continue
        periods = sorted(set(by_day[day]), key=PERIOD_ORDER.index)
        runs: list[list[str]] = []
        for period in periods:
            if runs and PERIOD_ORDER.index(period) == PERIOD_ORDER.index(runs[-1][-1]) + 1:
                runs[-1].append(period)
            else:
                runs.append([period])
        text = ",".join(run[0] if len(run) == 1 else f"{run[0]}-{run[-1]}" for run in runs)
        label = WEEKDAYS[day][1 if english else 0]
        parts.append(f"{label} {text}")
    return ", ".join(parts) if english else "、".join(parts)


class CourseCatalog:
    def __init__(self, cache_path: Path, *, url: str | None = None,
                 fetcher: Callable[[str], list[dict[str, Any]]] | None = None) -> None:
        self.cache_path = Path(cache_path)
        self.url = os.environ.get("ECOURSE_COURSE_CATALOG_URL", DEFAULT_URL) if url is None else url
        self._fetcher = fetcher or self._download
        self._lock = threading.Lock()
        self._index: dict[str, dict[str, Any]] | None = None
        self._loaded_at = 0.0
        self._refreshing = False
        self._next_attempt = 0.0

    @staticmethod
    def _download(url: str) -> list[dict[str, Any]]:
        response = httpx.get(url, timeout=60, follow_redirects=True)
        response.raise_for_status()
        return response.json()

    def _load_cache(self) -> None:
        if self._index is not None:
            return
        try:
            payload = json.loads(self.cache_path.read_text(encoding="utf-8"))
            self._index, self._loaded_at = payload["courses"], float(payload["fetched_at"])
        except (OSError, ValueError, KeyError, TypeError):
            self._index, self._loaded_at = {}, 0.0

    def refresh(self) -> None:
        """Download the open data and rewrite the local index (blocking)."""
        try:
            index = build_index(self._fetcher(self.url))
            if not index:
                raise ValueError("empty course catalog")
            fetched_at = time.time()
            self.cache_path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.cache_path.with_suffix(".tmp")
            temporary.write_text(json.dumps({"fetched_at": fetched_at, "courses": index}, ensure_ascii=False), encoding="utf-8")
            temporary.replace(self.cache_path)
            with self._lock:
                self._index, self._loaded_at = index, fetched_at
        except Exception as error:  # network or format problems must never break pages
            log.warning("course catalog refresh failed: %s", error)
            with self._lock:
                self._next_attempt = time.time() + RETRY_SECONDS
        finally:
            with self._lock:
                self._refreshing = False

    def _maybe_refresh(self) -> None:
        now = time.time()
        if not self.url or self._refreshing or now - self._loaded_at < REFRESH_SECONDS or now < self._next_attempt:
            return
        self._refreshing = True
        threading.Thread(target=self.refresh, name="course-catalog-refresh", daemon=True).start()

    def lookup(self, course_code: str | None) -> dict[str, Any] | None:
        with self._lock:
            self._load_cache()
            self._maybe_refresh()
            index = self._index or {}
        key = _code_key(course_code)
        if not key:
            return None
        if key in index:
            return index[key]
        # Some platforms omit the 5-digit term prefix (e.g. ``CS135500``).
        if not re.match(r"^\d{5}", key):
            matches = [value for code, value in index.items() if code[5:] == key]
            if len(matches) == 1:
                return matches[0]
        return None

    def describe(self, course_code: str | None, locale: str = "zh-Hant") -> dict[str, str]:
        found = self.lookup(course_code)
        if not found:
            return {}
        key = "en" if locale == "en" else "zh"
        separator = ", " if locale == "en" else "、"
        return {
            "teacher": separator.join(teacher[key] for teacher in found.get("teachers", [])),
            "periods": format_periods(found.get("slots", []), locale),
        }
