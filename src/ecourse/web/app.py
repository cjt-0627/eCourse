from __future__ import annotations

import secrets
import time
import uuid
from collections import defaultdict, deque
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from jinja2 import Environment, FileSystemLoader, select_autoescape
from pydantic import BaseModel, Field
from sqlalchemy import select

from ecourse.auth.service import (
    answer_school_auth_challenge, authenticate_school_credential, authenticate_session,
    begin_school_auth, complete_school_auth, create_session,
    refresh_school_auth_challenge, revoke_session,
    school_auth_challenge_image, school_auth_status,
)
from ecourse.config import Settings
from ecourse.db import Database
from ecourse.db.models import AppSession, Owner, PlatformConnection, ScheduleState, SyncJob
from ecourse.platforms import BRAVE_EXTENSION_ID
from ecourse.services import Service, ServiceError
from ecourse.web.i18n import normalize_locale, translator


ROOT = Path(__file__).parent
SESSION_COOKIE = "ecourse_session"
PRE_CSRF_COOKIE = "ecourse_pre_csrf"
SCHOOL_AUTH_COOKIE = "ecourse_school_auth"
SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
PLATFORMS = {"elearn", "eeclass"}
SYNC_STATE_LABELS = {
    "queued": "等待中", "running": "同步中", "waiting_user": "等待驗證",
    "succeeded": "成功", "partial": "部分完成", "failed": "失敗", "cancelled": "已取消",
}


class LoginBody(BaseModel):
    login_name: str = Field(min_length=1, max_length=120)
    password: str = Field(min_length=1, max_length=1024)
    csrf_token: str


class CredentialsBody(BaseModel):
    account: str = Field(min_length=1, max_length=200)
    password: str = Field(min_length=1, max_length=1024)


class ConnectBody(BaseModel):
    credential_id: str


class PreferenceBody(BaseModel):
    handled: bool | None = None
    muted_until: str | None = None


class HandledBody(BaseModel):
    handled: bool


class CourseSelectionBody(BaseModel):
    source_id: str
    selection_state: str
    expected_version: int = Field(ge=1)


class ManualEventBody(BaseModel):
    title: str = Field(min_length=1, max_length=300)
    kind: str = "exam"
    group_id: str | None = None
    source_announcement_id: str | None = None
    source_url: str | None = None
    date_precision: str = "exact"
    starts_at: str | None = None
    event_date: str | None = None


class ReminderBody(BaseModel):
    daily_enabled: bool = True
    daily_time: str = "08:00"
    weekly_enabled: bool = True
    weekly_day: int = Field(default=6, ge=0, le=6)
    weekly_time: str = "20:00"
    quiet_start: str = "23:00"
    quiet_end: str = "07:00"
    lead_hours: list[int] = [24, 3]


class LanguageBody(BaseModel):
    locale: Literal["zh-Hant", "en"]


class ChallengeBody(BaseModel):
    answer: str = Field(min_length=1, max_length=100)
    request_id: str = Field(min_length=8, max_length=128)


class SlidingLimiter:
    def __init__(self, limit: int, seconds: int):
        self.limit, self.seconds = limit, seconds
        self.events: dict[str, deque[float]] = defaultdict(deque)

    def allow(self, key: str) -> bool:
        now = time.monotonic()
        queue = self.events[key]
        while queue and queue[0] <= now - self.seconds:
            queue.popleft()
        if len(queue) >= self.limit:
            return False
        queue.append(now)
        return True


def _error(request: Request, status: int, code: str, message: str, retryable: bool = False) -> JSONResponse:
    return JSONResponse({"code": code, "message": message, "retryable": retryable, "request_id": request.state.request_id}, status_code=status)


def _data(request: Request, value, status_code: int = 200) -> JSONResponse:
    return JSONResponse({"data": value, "meta": {"request_id": request.state.request_id}}, status_code=status_code)


def _origin(settings) -> str:
    return str(getattr(settings, "base_url", "http://localhost:8765")).rstrip("/")


def _school_url(value: str | None) -> str | None:
    if not value:
        return None
    try:
        parsed = urlsplit(value)
        if parsed.scheme == "https" and parsed.hostname in {"elearn.nthu.edu.tw", "eeclass.nthu.edu.tw"} and not parsed.username and not parsed.password and parsed.port in {None, 443}:
            return value
    except ValueError:
        pass
    return None


def _taipei(value) -> str:
    if value is None or value == "": return ""
    if isinstance(value, date) and not isinstance(value, datetime): return value.isoformat()
    if isinstance(value, str) and len(value) == 10:
        try: return date.fromisoformat(value).isoformat()
        except ValueError: pass
    try:
        parsed = value if isinstance(value, datetime) else datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None: parsed = parsed.replace(tzinfo=ZoneInfo("UTC"))
        return parsed.astimezone(ZoneInfo("Asia/Taipei")).strftime("%Y-%m-%d %H:%M")
    except (TypeError, ValueError): return str(value)


def create_app(settings=None) -> FastAPI:
    settings = settings or Settings.from_env()
    db, service = Database(settings), Service(settings)
    app = FastAPI(title="eCourse", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.settings, app.state.db, app.state.service = settings, db, service
    app.state.login_limiter = SlidingLimiter(8, 300)
    app.state.browser_handoffs = {}
    templates = Environment(loader=FileSystemLoader(ROOT / "templates"), autoescape=select_autoescape(["html", "xml"]))
    templates.filters["taipei"] = _taipei
    app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")

    @app.middleware("http")
    async def security(request: Request, call_next):
        request.state.request_id = request.headers.get("x-request-id", str(uuid.uuid4()))[:128]
        host = request.headers.get("host", "").split(":", 1)[0].lower().rstrip(".")
        allowed = {str(x).lower() for x in getattr(settings, "allowed_hosts", ("localhost", "127.0.0.1"))}
        if host not in allowed:
            response = _error(request, 400, "invalid_host", "此 Host 不在允許清單中。")
        else:
            response = await call_next(request)
        response.headers["X-Request-ID"] = request.state.request_id
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "same-origin"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Content-Security-Policy"] = "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'"
        if request.url.path != "/health/live":
            response.headers["Cache-Control"] = "no-store"
        extension_origin = f"chrome-extension://{BRAVE_EXTENSION_ID}"
        if request.url.path.startswith("/api/v1/browser-extension/handoffs/") and request.headers.get("origin") == extension_origin:
            response.headers["Access-Control-Allow-Origin"] = extension_origin
            response.headers["Access-Control-Allow-Methods"] = "POST, OPTIONS"
            response.headers["Vary"] = "Origin"
        return response

    def current(request: Request) -> tuple[Owner, AppSession]:
        auth = authenticate_session(db, request.cookies.get(SESSION_COOKIE))
        if auth is None:
            raise HTTPException(401, "authentication_required")
        return auth

    def owner(auth=Depends(current)) -> Owner:
        return auth[0]

    async def require_write(request: Request, auth=Depends(current)):
        supplied = request.headers.get("x-csrf-token")
        if not supplied or not secrets.compare_digest(supplied, auth[1].csrf_token):
            raise HTTPException(403, "csrf_failed")
        if request.headers.get("origin") != _origin(settings):
            raise HTTPException(403, "origin_failed")
        return auth

    @app.exception_handler(HTTPException)
    async def http_error(request: Request, exc: HTTPException):
        messages = {"authentication_required": "請先登入。", "csrf_failed": "安全驗證已失效，請重新整理後再試。", "origin_failed": "請求來源不受信任。"}
        code = str(exc.detail)
        return _error(request, exc.status_code, code, messages.get(code, "無法完成請求。"))

    @app.exception_handler(ServiceError)
    async def service_error(request: Request, exc: ServiceError):
        name = type(exc).__name__
        mapping = {"NotFoundError": (404, "not_found", "找不到資料。", False), "ConflictError": (409, "conflict", "資料已變更，請重新整理。", False), "ValidationError": (422, "validation_error", str(exc), False), "GoneError": (410, "challenge_expired", "登入挑戰已失效，請重新連接。", False), "ServiceUnavailableError": (503, "worker_unavailable", "背景 worker 暫時無法連線。", True)}
        if name in mapping:
            status, code, message, retryable = mapping[name]
            return _error(request, status, code, message, retryable)
        return _error(request, 500, "service_error", "服務暫時無法完成請求。", True)

    @app.exception_handler(Exception)
    async def unexpected(request: Request, _exc: Exception):
        return _error(request, 500, "internal_error", "伺服器暫時無法完成請求。", True)

    @app.exception_handler(RequestValidationError)
    async def invalid_request(request: Request, _exc: RequestValidationError):
        return _error(request, 422, "validation_error", "請檢查輸入欄位。")

    @app.get("/health/live")
    def live(): return {"status": "ok"}

    @app.get("/health/ready")
    def ready(request: Request):
        try:
            okay = bool(db.ready())
        except Exception:
            okay = False
        return JSONResponse({"status": "ok" if okay else "failed"}, status_code=200 if okay else 503)

    @app.get("/login", response_class=HTMLResponse)
    def login_page(request: Request):
        csrf = secrets.token_urlsafe(32)
        response = HTMLResponse(templates.get_template("login.html").render(csrf_token=csrf, initialized=_ready(db)))
        response.set_cookie(PRE_CSRF_COOKIE, csrf, httponly=True, samesite="lax", secure=_origin(settings).startswith("https://"), max_age=600)
        return response

    @app.post("/api/v1/auth/login")
    def login(request: Request, body: LoginBody):
        cookie = request.cookies.get(PRE_CSRF_COOKIE, "")
        if request.headers.get("origin") != _origin(settings) or not cookie or not secrets.compare_digest(cookie, body.csrf_token):
            return _error(request, 403, "csrf_failed", "安全驗證已失效，請重新整理後再試。")
        ip = request.client.host if request.client else "unknown"
        if not app.state.login_limiter.allow(f"ip:{ip}") or not app.state.login_limiter.allow(f"account:{body.login_name.casefold()}"):
            return _error(request, 429, "rate_limited", "嘗試次數過多，請稍後再試。", True)
        device_label = request.headers.get("user-agent", "瀏覽器")[:120]
        found = authenticate_school_credential(settings, body.login_name, body.password)
        if found is not None:
            raw, session = create_session(db, found.id, device_label)
            response = _data(request, {"signed_in": True})
            response.set_cookie(SESSION_COOKIE, raw, httponly=True, samesite="lax", secure=_origin(settings).startswith("https://"), max_age=30 * 86400, path="/")
            response.delete_cookie(PRE_CSRF_COOKIE)
            return response
        try:
            result = begin_school_auth(settings, body.login_name, body.password, device_label=device_label)
        except ValueError:
            return _error(request, 401, "invalid_credentials", "登入資料不正確。")
        exchange = result.pop("exchange_token")
        response = _data(request, result, 202)
        response.set_cookie(SCHOOL_AUTH_COOKIE, exchange, httponly=True, samesite="lax", secure=_origin(settings).startswith("https://"), max_age=600, path="/api/v1/auth/school")
        return response

    def school_exchange(request: Request, *, write: bool = False) -> str:
        origin_ok = request.headers.get("origin") == _origin(settings)
        referer = request.headers.get("referer", "")
        referer_ok = not write and referer.startswith(_origin(settings) + "/")
        if not origin_ok and not referer_ok:
            raise HTTPException(403, "origin_failed")
        if write:
            cookie = request.cookies.get(PRE_CSRF_COOKIE, "")
            supplied = request.headers.get("x-csrf-token", "")
            if not cookie or not supplied or not secrets.compare_digest(cookie, supplied):
                raise HTTPException(403, "csrf_failed")
        token = request.cookies.get(SCHOOL_AUTH_COOKIE, "")
        if not token:
            raise HTTPException(401, "authentication_required")
        return token

    @app.get("/api/v1/auth/school/status")
    def school_status(request: Request):
        return _data(request, school_auth_status(settings, school_exchange(request)))

    @app.get("/api/v1/auth/school/challenge-image")
    async def school_challenge_image(request: Request):
        image = await school_auth_challenge_image(settings, school_exchange(request))
        return Response(image, media_type="image/png", headers={"Cache-Control": "no-store"})

    @app.post("/api/v1/auth/school/challenge-answer")
    async def school_challenge_answer(body: ChallengeBody, request: Request):
        return _data(request, await answer_school_auth_challenge(settings, school_exchange(request, write=True), body.answer, body.request_id), 202)

    @app.post("/api/v1/auth/school/challenge-refresh")
    async def school_challenge_refresh(request: Request):
        return _data(request, await refresh_school_auth_challenge(settings, school_exchange(request, write=True)), 202)

    @app.post("/api/v1/auth/school/complete")
    def school_complete(request: Request):
        raw, session = complete_school_auth(settings, school_exchange(request, write=True))
        response = _data(request, {"signed_in": True})
        response.set_cookie(SESSION_COOKIE, raw, httponly=True, samesite="lax", secure=_origin(settings).startswith("https://"), max_age=30 * 86400, path="/")
        response.delete_cookie(SCHOOL_AUTH_COOKIE, path="/api/v1/auth/school")
        response.delete_cookie(PRE_CSRF_COOKIE)
        return response

    @app.post("/api/v1/auth/logout")
    def logout(request: Request, auth=Depends(require_write)):
        revoke_session(db, auth[1].id, auth[0].id)
        response = _data(request, {"signed_out": True})
        response.delete_cookie(SESSION_COOKIE, path="/")
        return response

    @app.get("/api/v1/auth/me")
    def me(request: Request, auth=Depends(current)):
        return _data(request, {"id": str(auth[0].id), "login_name": auth[0].login_name, "timezone": auth[0].timezone, "locale": normalize_locale(auth[0].locale), "csrf_token": auth[1].csrf_token})

    @app.get("/api/v1/auth/sessions")
    def sessions(request: Request, auth=Depends(current)):
        with db.session() as s:
            rows = s.query(AppSession).filter(AppSession.owner_id == auth[0].id, AppSession.revoked_at.is_(None)).all()
            data = [{"id": str(x.id), "device_label": x.device_label, "created_at": str(x.created_at), "last_used_at": str(x.last_used_at), "expires_at": str(x.expires_at), "current": x.id == auth[1].id} for x in rows]
        return _data(request, data)

    @app.delete("/api/v1/auth/sessions/{session_id}")
    def delete_session(session_id: str, request: Request, auth=Depends(require_write)):
        if not revoke_session(db, session_id, auth[0].id):
            raise HTTPException(404, "not_found")
        return _data(request, {"revoked": True})

    def get_api(name):
        async def endpoint(request: Request, user=Depends(owner)):
            return _data(request, getattr(service, name)(user.id))
        return endpoint

    def digest_data(owner_id, digest_id):
        value = service.get_digest(owner_id, digest_id)
        for entry in value.get("entries", []):
            identifier = entry.get("id") or entry.get("item_id") or entry.get("event_id")
            if identifier: entry["detail_path"] = f"/manual-events/{identifier}" if entry.get("manual") else f"/items/{identifier}"
        return value

    app.add_api_route("/api/v1/dashboard", get_api("dashboard"), methods=["GET"])
    app.add_api_route("/api/v1/connections", get_api("list_connections"), methods=["GET"])
    app.add_api_route("/api/v1/courses", get_api("list_courses"), methods=["GET"])
    app.add_api_route("/api/v1/manual-events", get_api("list_manual_events"), methods=["GET"])
    app.add_api_route("/api/v1/notifications", get_api("list_notifications"), methods=["GET"])
    app.add_api_route("/api/v1/settings/reminders", get_api("get_reminder_settings"), methods=["GET"])
    app.add_api_route("/api/v1/system/status", get_api("system_status"), methods=["GET"])

    @app.get("/api/v1/digests/{digest_id}")
    def digest(digest_id: str, request: Request, user=Depends(owner)):
        return _data(request, digest_data(user.id, digest_id))

    @app.get("/api/v1/courses/{item_id}")
    def course(item_id: str, request: Request, user=Depends(owner)): return _data(request, service.get_course_workspace(user.id, item_id))

    @app.get("/api/v1/courses/{course_id}/materials")
    def course_materials(course_id: str, request: Request, user=Depends(owner)):
        return _data(request, service.list_course_materials(user.id, course_id))

    @app.put("/api/v1/courses/{course_id}/selection")
    def course_selection(course_id: str, body: CourseSelectionBody, request: Request, auth=Depends(require_write)):
        if body.selection_state not in {"selected", "disabled", "pending", "none"}: raise HTTPException(422, "validation_error")
        return _data(request, service.set_course_selection(auth[0].id, course_id, body.source_id, body.selection_state, body.expected_version))

    @app.get("/api/v1/items")
    def items(request: Request, user=Depends(owner)):
        allowed = {"type", "course", "platform", "status", "from", "to", "q", "cursor"}
        return _data(request, service.list_items(user.id, {k: v for k, v in request.query_params.items() if k in allowed}))

    @app.get("/api/v1/items/{item_id}")
    def item(item_id: str, request: Request, user=Depends(owner)): return _data(request, service.get_item(user.id, item_id))

    @app.put("/api/v1/items/{item_id}/preference")
    def preference(item_id: str, body: PreferenceBody, request: Request, auth=Depends(require_write)):
        return _data(request, service.set_item_preference(auth[0].id, item_id, handled=body.handled, muted_until=body.muted_until))

    @app.get("/api/v1/announcements")
    def announcements(request: Request, user=Depends(owner)): return _data(request, service.list_announcements(user.id))

    @app.get("/api/v1/announcements/{item_id}")
    def announcement(item_id: str, request: Request, user=Depends(owner)): return _data(request, service.get_announcement(user.id, item_id))

    @app.put("/api/v1/announcements/{item_id}/read")
    def read_announcement(item_id: str, request: Request, auth=Depends(require_write)): return _data(request, service.mark_announcement_read(auth[0].id, item_id))

    @app.post("/api/v1/manual-events")
    def create_event(body: ManualEventBody, request: Request, auth=Depends(require_write)): return _data(request, service.create_manual_event(auth[0].id, body.model_dump()), 201)

    @app.get("/api/v1/manual-events/{event_id}")
    def get_event(event_id: str, request: Request, user=Depends(owner)): return _data(request, service.get_manual_event(user.id, event_id))

    @app.patch("/api/v1/manual-events/{event_id}")
    def update_event(event_id: str, body: ManualEventBody, request: Request, auth=Depends(require_write)): return _data(request, service.update_manual_event(auth[0].id, event_id, **body.model_dump(exclude_unset=True)))

    @app.delete("/api/v1/manual-events/{event_id}")
    def delete_event(event_id: str, request: Request, auth=Depends(require_write)): return _data(request, service.delete_manual_event(auth[0].id, event_id))

    @app.put("/api/v1/manual-events/{event_id}/preference")
    def manual_preference(event_id: str, body: HandledBody, request: Request, auth=Depends(require_write)):
        return _data(request, service.set_manual_event_preference(auth[0].id, event_id, handled=body.handled))

    @app.put("/api/v1/notifications/{notification_id}/read")
    def read_notification(notification_id: str, request: Request, auth=Depends(require_write)): return _data(request, service.mark_notification_read(auth[0].id, notification_id))

    @app.post("/api/v1/credentials")
    def credentials(body: CredentialsBody, request: Request, auth=Depends(require_write)):
        result = service.save_credentials(auth[0].id, body.account, body.password)
        return _data(request, result, 201)

    @app.put("/api/v1/settings/credentials")
    def replace_credentials(body: CredentialsBody, request: Request, auth=Depends(require_write)):
        return _data(request, service.replace_credentials_and_reconnect(auth[0].id, body.account, body.password), 202)

    @app.post("/api/v1/connections/{platform}/connect")
    def connect(platform: str, body: ConnectBody, request: Request, auth=Depends(require_write)):
        if platform not in PLATFORMS: raise HTTPException(404, "not_found")
        return _data(request, service.connect(auth[0].id, platform, body.credential_id), 202)

    @app.get("/api/v1/login-attempts/{attempt_id}")
    def login_attempt(attempt_id: str, request: Request, user=Depends(owner)):
        return _data(request, service.get_login_attempt(user.id, attempt_id))

    @app.get("/api/v1/login-attempts/{attempt_id}/challenge-image")
    async def challenge_image(attempt_id: str, request: Request, user=Depends(owner)):
        image = await service.get_challenge_image(user.id, attempt_id)
        return Response(image, media_type="image/png", headers={"Cache-Control": "no-store"})

    @app.post("/api/v1/login-attempts/{attempt_id}/answer")
    async def challenge_answer(attempt_id: str, body: ChallengeBody, request: Request, auth=Depends(require_write)):
        return _data(request, await service.submit_challenge(auth[0].id, attempt_id, body.answer, body.request_id), 202)

    @app.post("/api/v1/login-attempts/{attempt_id}/refresh")
    async def refresh_challenge(attempt_id: str, request: Request, auth=Depends(require_write)):
        return _data(request, await service.refresh_challenge(auth[0].id, attempt_id), 202)

    @app.post("/api/v1/connections/{platform}/retry")
    def retry_connection(platform: str, request: Request, auth=Depends(require_write)):
        if platform not in PLATFORMS: raise HTTPException(404, "not_found")
        return _data(request, service.retry_connection(auth[0].id, platform), 202)

    @app.post("/api/v1/connections/{platform}/open")
    async def open_platform(platform: str, request: Request, auth=Depends(require_write)):
        if platform not in PLATFORMS: raise HTTPException(404, "not_found")
        payload = await request.json()
        result = await service.open_platform(auth[0].id, platform, payload.get("target_url"))
        return _data(request, result, 202 if result.get("status") == "reauthentication_required" else 200)

    @app.post("/api/v1/connections/{platform}/extension-handoff")
    async def prepare_extension_handoff(platform: str, request: Request, auth=Depends(require_write)):
        if platform not in PLATFORMS: raise HTTPException(404, "not_found")
        payload = await request.json()
        result = await service.prepare_platform_handoff(auth[0].id, platform, payload.get("target_url"))
        if result.get("status") != "ready":
            return _data(request, result, 202)
        token = secrets.token_urlsafe(32)
        app.state.browser_handoffs[token] = {
            "owner_id": auth[0].id,
            "platform": platform,
            "target_url": result["target_url"],
            "expires_at": time.monotonic() + 15,
        }
        return _data(request, {
            "status": "ready", "handoff_token": token,
            "extension_id": BRAVE_EXTENSION_ID,
        })

    @app.options("/api/v1/browser-extension/handoffs/{token}")
    def extension_handoff_options(token: str, request: Request):
        if request.headers.get("origin") != f"chrome-extension://{BRAVE_EXTENSION_ID}":
            return _error(request, 403, "forbidden", "扩充功能来源无效。")
        return Response(status_code=204)

    @app.post("/api/v1/browser-extension/handoffs/{token}")
    async def consume_extension_handoff(token: str, request: Request):
        if request.headers.get("origin") != f"chrome-extension://{BRAVE_EXTENSION_ID}":
            return _error(request, 403, "forbidden", "扩充功能来源无效。")
        handoff = app.state.browser_handoffs.pop(token, None)
        if not handoff or handoff["expires_at"] < time.monotonic():
            return _error(request, 410, "handoff_expired", "浏览器交接码已失效，请重新点击。")
        cookies = await service.export_platform_cookies(handoff["owner_id"], handoff["platform"])
        return _data(request, {
            "platform": handoff["platform"],
            "target_url": handoff["target_url"],
            "cookies": cookies,
        })

    @app.delete("/api/v1/connections/{platform}")
    def disconnect(platform: str, request: Request, auth=Depends(require_write)):
        if platform not in PLATFORMS: raise HTTPException(404, "not_found")
        return _data(request, service.disconnect(auth[0].id, platform))

    @app.post("/api/v1/sync")
    def sync(request: Request, auth=Depends(require_write)):
        return _data(request, service.enqueue_sync(auth[0].id), 202)

    @app.get("/api/v1/sync/jobs/{job_id}")
    def sync_job(job_id: str, request: Request, user=Depends(owner)): return _data(request, service.get_sync_job(user.id, job_id))

    @app.put("/api/v1/settings/reminders")
    def reminders(body: ReminderBody, request: Request, auth=Depends(require_write)):
        rules = [
            {"kind": "daily", "enabled": body.daily_enabled, "local_time": body.daily_time, "quiet_start": body.quiet_start, "quiet_end": body.quiet_end},
            {"kind": "weekly", "enabled": body.weekly_enabled, "weekday": body.weekly_day, "local_time": body.weekly_time, "quiet_start": body.quiet_start, "quiet_end": body.quiet_end},
            {"kind": "deadline", "enabled": True, "offsets": body.lead_hours, "quiet_start": body.quiet_start, "quiet_end": body.quiet_end},
        ]
        return _data(request, service.update_reminder_settings(auth[0].id, {"rules": rules}))

    @app.put("/api/v1/settings/language")
    def language(body: LanguageBody, request: Request, auth=Depends(require_write)):
        return _data(request, service.update_locale(auth[0].id, body.locale))

    def render(template: str, *, title: str, active: str, data, auth) -> HTMLResponse:
        locale = normalize_locale(auth[0].locale)
        t = translator(locale)
        return HTMLResponse(templates.get_template(template).render(
            title=t(title), active=active, data=data, user=auth[0],
            csrf_token=auth[1].csrf_token, locale=locale, t=t,
        ))

    def page(template: str, title: str, active: str, load):
        async def endpoint(request: Request):
            auth = authenticate_session(db, request.cookies.get(SESSION_COOKIE))
            if auth is None: return RedirectResponse("/login", 303)
            data = load(auth[0].id)
            return render(template, title=title, active=active, data=data, auth=auth)
        return endpoint

    def todo_data(owner_id):
        synced = service.list_items(owner_id, {})
        completed_states = {"submitted", "graded", "completed", "passed"}
        for item in synced:
            item["display_at"] = item.get("due_at") or (item.get("closes_at") if item.get("kind") == "quiz" else None) or item.get("due_date")
            item["submitted"] = item.get("remote_status") in completed_states
            item["submission_state"] = "submitted" if item["submitted"] else ("unsubmitted" if item.get("remote_status") in {"not_submitted", "draft"} else "unknown")
        manual = [{**item, "manual": True, "handled": bool(item.get("handled_at")), "due_at": item.get("starts_at")} for item in service.list_manual_events(owner_id)]
        for item in manual: item["display_at"] = item.get("starts_at") or item.get("event_date")
        return {"items": synced + manual, "courses": service.list_courses(owner_id)}

    def dashboard_data(owner_id):
        value = service.dashboard(owner_id)
        labels = {"not_connected": "尚未連接", "authenticating": "正在登入，自動辨識驗證碼中", "challenge_required": "自動辨識未完成，請輸入驗證碼", "connected": "已登入", "credentials_invalid": "帳密不正確", "temporarily_unavailable": "平台暫時無法使用", "expired": "登入已過期", "partial": "部分完成"}
        for connection in value.get("connections", []):
            connection["auth_label"] = labels.get(connection.get("auth_state"), "狀態未知")
            connection["last_attempt_label"] = SYNC_STATE_LABELS.get(connection.get("last_attempt_state"), "尚無記錄")
        rows = todo_data(owner_id)["items"]
        zone, now = ZoneInfo("Asia/Taipei"), datetime.now(ZoneInfo("Asia/Taipei"))
        today, week_end = now.date(), now.date() + timedelta(days=7)
        counts = {"today": 0, "next_seven_days": 0, "overdue": 0, "needs_confirmation": 0}
        upcoming = []
        for item in rows:
            if item.get("remote_status") in {"submitted", "graded", "completed", "passed"}: continue
            if item.get("date_precision") == "unknown": counts["needs_confirmation"] += 1; continue
            raw = item.get("starts_at") or item.get("due_at") or (item.get("closes_at") if item.get("kind") == "quiz" else None)
            if raw:
                moment = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
                if moment.tzinfo is None: moment = moment.replace(tzinfo=zone)
                local_date, expired = moment.astimezone(zone).date(), moment < now
            elif item.get("event_date") or item.get("due_date"):
                local_date = date.fromisoformat(item.get("event_date") or item.get("due_date")); expired = local_date < today
            else:
                counts["needs_confirmation"] += 1; continue
            if expired: counts["overdue"] += 1
            if local_date == today: counts["today"] += 1
            if today <= local_date < week_end: counts["next_seven_days"] += 1; upcoming.append(item)
        value["counts"].update(counts)
        value["upcoming"] = upcoming[:8]
        value["announcements"] = service.list_announcements(owner_id)[:5]
        return value

    def reminder_data(owner_id):
        rows = {row["kind"]: row for row in service.get_reminder_settings(owner_id)}
        daily, weekly, deadline = rows.get("daily", {}), rows.get("weekly", {}), rows.get("deadline", {})
        return {"daily_enabled": daily.get("enabled", True), "daily_time": daily.get("local_time") or "08:00", "weekly_enabled": weekly.get("enabled", True), "weekly_time": weekly.get("local_time") or "20:00", "quiet_start": daily.get("quiet_start") or "23:00", "quiet_end": daily.get("quiet_end") or "07:00", "lead_hours": deadline.get("offsets") or [24, 3]}

    def connection_data(owner_id):
        existing = {row["platform"]: row for row in service.list_connections(owner_id)}
        credentials = service.list_credentials(owner_id)
        credential_id = credentials[0]["id"] if credentials else None
        labels = {"not_connected": "尚未連接", "authenticating": "正在登入，自動辨識驗證碼中", "challenge_required": "自動辨識未完成，請輸入驗證碼", "connected": "已登入", "credentials_invalid": "帳密不正確", "temporarily_unavailable": "平台暫時無法使用", "expired": "登入已過期", "partial": "部分完成"}
        rows = []
        for platform in ("elearn", "eeclass"):
            item = {"platform": platform, "auth_state": "not_connected", **existing.get(platform, {})}
            item["auth_label"] = labels.get(item["auth_state"], "狀態未知")
            item["last_attempt_label"] = SYNC_STATE_LABELS.get(item.get("last_attempt_state"), "尚無記錄")
            item["connect_credential_id"] = credential_id
            rows.append(item)
        return {"connections": rows, "credential_hint": credentials[0].get("account_hint") if credentials else None}

    def status_data(owner_id):
        value = service.system_status(owner_id)
        value["database"] = {"ready": "正常", "not_ready": "尚未就緒"}.get(value.get("database"), "狀態未知")
        value["worker"] = {"healthy": "背景同步正常", "stopped": "背景同步停止"}.get(value.get("worker"), "狀態未知")
        with db.session() as session:
            rows = []
            for connection in session.scalars(select(PlatformConnection).where(PlatformConnection.owner_id == owner_id).order_by(PlatformConnection.platform)).all():
                job = session.scalars(select(SyncJob).where(SyncJob.owner_id == owner_id, SyncJob.connection_id == connection.id).order_by(SyncJob.scheduled_at.desc()).limit(1)).first()
                cursor = session.scalar(select(ScheduleState).where(ScheduleState.owner_id == owner_id, ScheduleState.schedule_key == f"sync:{connection.id}"))
                rows.append({"platform": connection.platform, "auth_state": connection.auth_state, "last_success_at": connection.last_success_at, "error_code": connection.error_code, "job_state": job.state if job else None, "job_label": SYNC_STATE_LABELS.get(job.state, "尚無工作") if job else "尚無工作", "job_error": job.error_code if job else None, "last_attempt_at": (job.finished_at or job.started_at) if job else None, "next_sync_at": cursor.next_run_at if cursor else None})
            value["platforms"] = rows
        return value

    app.add_api_route("/", page("dashboard.html", "總覽", "dashboard", dashboard_data), methods=["GET"])
    app.add_api_route("/todos", page("todos.html", "待辦", "todos", todo_data), methods=["GET"])
    app.add_api_route("/courses", page("courses.html", "課程", "courses", lambda oid: {"courses": service.list_courses(oid)}), methods=["GET"])
    app.add_api_route("/notifications", page("notifications.html", "通知", "notifications", lambda oid: {"notifications": service.list_notifications(oid)}), methods=["GET"])
    app.add_api_route("/settings/connections", page("connections.html", "連接設定", "settings", connection_data), methods=["GET"])
    app.add_api_route("/settings/reminders", page("reminders.html", "提醒設定", "settings", reminder_data), methods=["GET"])
    app.add_api_route("/settings/appearance", page("appearance.html", "外觀設定", "settings", lambda oid: {}), methods=["GET"])
    app.add_api_route("/status", page("status.html", "系統狀態", "settings", status_data), methods=["GET"])
    @app.get("/courses/{course_id}", response_class=HTMLResponse)
    def course_page(course_id: str, request: Request):
        auth = authenticate_session(db, request.cookies.get(SESSION_COOKIE))
        if auth is None: return RedirectResponse("/login", 303)
        data = service.get_course_workspace(auth[0].id, course_id)
        for material in data.get("materials", []):
            material["external_url"] = _school_url(material.get("source_url"))
        for source in data.get("sources", []):
            source["external_url"] = _school_url(source.get("source_url"))
        for announcement in data.get("announcements", []):
            announcement["external_url"] = _school_url(announcement.get("source_url"))
        return render("course_detail.html", title="課程詳情", active="courses", data=data, auth=auth)

    @app.get("/items/{item_id}", response_class=HTMLResponse)
    def item_page(item_id: str, request: Request):
        return _detail_page(request, "課務詳情", "todos", lambda oid: service.get_item(oid, item_id))

    @app.get("/manual-events/{event_id}", response_class=HTMLResponse)
    def manual_page(event_id: str, request: Request):
        return _detail_page(request, "手動事件", "todos", lambda oid: service.get_manual_event(oid, event_id))

    @app.get("/announcements/{announcement_id}", response_class=HTMLResponse)
    def announcement_page(announcement_id: str, request: Request):
        return _detail_page(request, "公告", "dashboard", lambda oid: service.get_announcement(oid, announcement_id))

    @app.get("/digests/{digest_id}", response_class=HTMLResponse)
    def digest_page(digest_id: str, request: Request):
        auth = authenticate_session(db, request.cookies.get(SESSION_COOKIE))
        if auth is None: return RedirectResponse("/login", 303)
        data = digest_data(auth[0].id, digest_id)
        return render("digest.html", title="課務摘要", active="notifications", data=data, auth=auth)

    def _detail_page(request: Request, title: str, active: str, loader):
        auth = authenticate_session(db, request.cookies.get(SESSION_COOKIE))
        if auth is None: return RedirectResponse("/login", 303)
        data = loader(auth[0].id)
        data["external_url"] = _school_url(data.get("source_url"))
        return render("detail.html", title=title, active=active, data=data, auth=auth)
    return app


def _ready(db: Database) -> bool:
    try: return bool(db.ready())
    except Exception: return False
