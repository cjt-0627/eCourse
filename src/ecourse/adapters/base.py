from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from enum import StrEnum
from typing import Any, Protocol


class AdapterStatus(StrEnum):
    CONNECTED = "connected"
    CHALLENGE_REQUIRED = "challenge_required"
    CREDENTIALS_INVALID = "credentials_invalid"
    MFA_REQUIRED = "mfa_required"
    IDENTITY_MISMATCH = "identity_mismatch"
    SESSION_EXPIRED = "session_expired"
    UPSTREAM_TIMEOUT = "upstream_timeout"
    UPSTREAM_UNAVAILABLE = "upstream_unavailable"
    PARSER_CHANGED = "parser_changed"


class DatePrecision(StrEnum):
    EXACT = "exact"
    DATE_ONLY = "date_only"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class LoginChallenge:
    challenge_id: str
    challenge_type: str
    image: bytes | None = field(repr=False)
    expires_at: datetime
    prompt: str = "請輸入圖片中的驗證碼"


@dataclass(frozen=True, slots=True)
class LoginResult:
    status: AdapterStatus
    attempt_id: str | None = None
    challenge: LoginChallenge | None = None
    remote_account_id: str | None = None
    storage_state: dict[str, Any] | None = field(default=None, repr=False)
    error_code: str | None = None
    message: str | None = None
    retryable: bool = False
    evidence: str | None = None
    diagnostic_stage: str | None = None


@dataclass(frozen=True, slots=True)
class SessionVerification:
    status: AdapterStatus
    remote_account_id: str | None = None
    evidence: str | None = None
    error_code: str | None = None
    storage_state: dict[str, Any] | None = field(default=None, repr=False)


@dataclass(frozen=True, slots=True)
class Course:
    remote_id: str
    title: str
    url: str
    semester: str | None = None
    course_code: str | None = None


@dataclass(frozen=True, slots=True)
class CourseItem:
    remote_id: str
    kind: str
    title: str
    url: str
    due_at: datetime | None = None
    due_date: date | None = None
    date_precision: DatePrecision = DatePrecision.UNKNOWN
    source_date_text: str | None = None
    remote_status: str = "unknown"
    remote_status_text: str | None = None
    status_evidence: str | None = None
    grade_text: str | None = None
    grading_status_text: str | None = None
    closes_at: datetime | None = None
    closes_date: date | None = None
    closes_precision: DatePrecision = DatePrecision.UNKNOWN
    closes_source_text: str | None = None


@dataclass(frozen=True, slots=True)
class CourseContent:
    course_id: str
    items: tuple[CourseItem, ...]


@dataclass(frozen=True, slots=True)
class ItemDetail:
    item: CourseItem
    content_text: str | None = None
    published_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class FetchResult:
    records: tuple[Any, ...]
    coverage: tuple[str, ...]
    complete: bool
    warnings: tuple[str, ...] = field(default_factory=tuple)


class CaptchaSolver(Protocol):
    async def solve(self, image: bytes) -> "CaptchaSolution": ...


@dataclass(frozen=True, slots=True)
class CaptchaSolution:
    candidate: str | None = field(repr=False)
    confidence: float
    solver_version: str
    elapsed_ms: int
    reason: str | None = None


class ManualCaptchaSolver:
    async def solve(self, image: bytes) -> CaptchaSolution:
        return CaptchaSolution(None, 0.0, "manual-v1", 0, "manual_input_required")


class PlatformAdapter(Protocol):
    async def begin_login(
        self, account: str, password: str, storage_state: dict[str, Any] | None = None,
        connection_generation: int = 0,
    ) -> LoginResult: ...

    async def answer_challenge(
        self, attempt_id: str, challenge_id: str, answer: str,
        request_id: str | None = None,
    ) -> LoginResult: ...

    async def refresh_challenge(self, attempt_id: str, challenge_id: str) -> LoginResult: ...
    async def discard_attempt(self, attempt_id: str) -> bool: ...

    async def verify_session(self, storage_state: dict[str, Any]) -> SessionVerification: ...
    async def discover_courses(self, storage_state: dict[str, Any]) -> FetchResult: ...
    async def fetch_course(self, storage_state: dict[str, Any], remote_course_id: str) -> FetchResult: ...
    async def fetch_item_detail(self, storage_state: dict[str, Any], item_ref: CourseItem) -> FetchResult: ...
