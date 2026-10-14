import re
import subprocess
from datetime import UTC, datetime

import pytest
from argon2 import PasswordHasher
from fastapi.testclient import TestClient

from ecourse.auth.service import create_session, initialize_owner
from ecourse.config import Settings
from ecourse.db import Database
from ecourse.db.models import Announcement, CourseGroup, CourseMaterial, CourseSelection, CourseSource, LearningItem, Owner, PlatformConnection, ScheduleState, SyncJob
from ecourse.vault import initialize_key
from ecourse.web import create_app
from ecourse.web.app import _school_url, _taipei


ORIGIN = "http://localhost:8765"


def test_external_url_allowlist_and_taipei_format():
    assert _school_url("https://elearn.nthu.edu.tw/course/1")
    assert _school_url("https://eeclass.nthu.edu.tw/course/1")
    assert _school_url("javascript:alert(1)") is None
    assert _school_url("https://elearn.nthu.edu.tw@evil.test/") is None
    assert _school_url("https://elearn.nthu.edu.tw.evil.test/") is None
    assert _school_url("https://user@elearn.nthu.edu.tw/") is None
    assert _taipei("2026-10-06T01:30:00Z") == "2026-10-06 09:30"


@pytest.fixture
def web(tmp_path):
    data, secrets = tmp_path / "data", tmp_path / "secrets"
    initialize_key(secrets, data)
    settings = Settings(data, secrets, base_url=ORIGIN)
    db = Database(settings)
    db.initialize()
    owner = initialize_owner(settings, "owner", "a-long-local-password")
    # Web login now uses the encrypted school credential. Fixtures use an
    # isolated synthetic value and never contact either school platform.
    from ecourse.services import Service
    Service(settings).save_credentials(owner.id, "owner", "a-long-local-password")
    with TestClient(create_app(settings), base_url=ORIGIN) as client:
        yield client, settings, db, owner


def login(client, name="owner", password="a-long-local-password"):
    page = client.get("/login")
    token = re.search(r'name="csrf_token" value="([^"]+)"', page.text).group(1)
    response = client.post("/api/v1/auth/login", headers={"Origin": ORIGIN}, json={"login_name": name, "password": password, "csrf_token": token})
    assert response.status_code == 200
    csrf = client.get("/api/v1/auth/me").json()["data"]["csrf_token"]
    return {"Origin": ORIGIN, "X-CSRF-Token": csrf}


def test_login_event_crud_reminders_and_logout(web):
    client, _settings, _db, _owner = web
    headers = login(client)
    for path in ("/", "/todos", "/courses", "/notifications", "/settings/connections", "/settings/reminders", "/status"):
        assert client.get(path).status_code == 200
    created = client.post("/api/v1/manual-events", headers=headers, json={"title": "期中考", "kind": "exam", "date_precision": "exact", "starts_at": "2026-11-01T09:00:00+08:00"})
    assert created.status_code == 201
    event_id = created.json()["data"]["id"]
    handled = client.put(f"/api/v1/manual-events/{event_id}/preference", headers=headers, json={"handled": True})
    assert handled.status_code == 200 and handled.json()["data"]["handled_at"]
    todos = client.get("/todos").text
    assert "我已處理" not in todos
    assert "手動建立的考試／事件" in todos
    restored = client.put(f"/api/v1/manual-events/{event_id}/preference", headers=headers, json={"handled": False})
    assert restored.status_code == 200 and restored.json()["data"]["handled_at"] is None
    todo_page = client.get("/todos").text
    assert "2026-11-01T01:00:00+00:00" in todo_page
    assert "期中考" in client.get(f"/manual-events/{event_id}").text
    edited = client.patch(f"/api/v1/manual-events/{event_id}", headers=headers, json={"title": "期中考（教室 101）"})
    assert edited.status_code == 200
    assert edited.json()["data"]["title"] == "期中考（教室 101）"
    saved = client.put("/api/v1/settings/reminders", headers=headers, json={"daily_enabled": True, "daily_time": "07:30", "weekly_enabled": False, "weekly_time": "20:00", "quiet_start": "23:00", "quiet_end": "07:00", "lead_hours": [24, 3]})
    assert saved.status_code == 200
    assert {x["kind"] for x in saved.json()["data"]} == {"daily", "weekly", "deadline"}
    assert client.delete(f"/api/v1/manual-events/{event_id}", headers=headers).status_code == 200
    assert client.post("/api/v1/auth/logout", headers=headers, json={}).status_code == 200
    assert client.get("/api/v1/dashboard").status_code == 401


def test_dashboard_can_open_each_connected_platform_with_saved_session(web):
    client, _settings, db, owner = web
    login(client)
    with db.session() as session:
        session.add_all([
            PlatformConnection(owner_id=owner.id, platform="elearn", auth_state="connected", generation=1),
            PlatformConnection(owner_id=owner.id, platform="eeclass", auth_state="connected", generation=1),
        ])

    page = client.get("/").text
    assert page.count('data-action="open-platform"') == 2
    assert 'data-platform="elearn"' in page
    assert 'data-platform="eeclass"' in page


def test_language_setting_is_persisted_and_renders_english_ui(web):
    client, _settings, db, owner = web
    headers = login(client)
    appearance = client.get("/settings/appearance")
    assert appearance.status_code == 200
    assert '<html lang="zh-Hant">' in appearance.text
    assert 'name="locale" value="zh-Hant" checked' in appearance.text
    assert 'name="locale" value="en"' in appearance.text

    changed = client.put("/api/v1/settings/language", headers=headers, json={"locale": "en"})
    assert changed.status_code == 200
    assert changed.json()["data"] == {"locale": "en"}
    with db.session() as session:
        session.add(PlatformConnection(owner_id=owner.id, platform="elearn", auth_state="connected", generation=1))
    page = client.get("/")
    assert '<html lang="en">' in page.text
    assert "Overview" in page.text
    assert "Platform status" in page.text
    assert "Open with signed-in session" in page.text
    appearance = client.get("/settings/appearance").text
    assert "Appearance settings" in appearance
    assert 'name="locale" value="en" checked' in appearance
    assert client.get("/api/v1/auth/me").json()["data"]["locale"] == "en"
    with db.session() as session:
        assert session.get(Owner, owner.id).locale == "en"

    invalid = client.put("/api/v1/settings/language", headers=headers, json={"locale": "fr"})
    assert invalid.status_code == 422


def test_course_source_selection_workflow(web):
    client, _settings, db, owner = web
    headers = login(client)
    with db.session() as session:
        connection = PlatformConnection(owner_id=owner.id, platform="elearn", auth_state="connected", generation=1)
        group = CourseGroup(owner_id=owner.id, display_name="測試課程", term="115-1")
        session.add_all([connection, group]); session.flush()
        source = CourseSource(owner_id=owner.id, connection_id=connection.id, remote_course_id="remote-1", title="測試課程 eLearn")
        session.add(source); session.flush()
        selection = CourseSelection(owner_id=owner.id, group_id=group.id, source_id=source.id, selection_state="pending", preference_version=1)
        item = LearningItem(owner_id=owner.id, source_course_id=source.id, remote_item_id="item-1", kind="assignment", title="只有日期作業", due_date=__import__("datetime").date(2026, 11, 3), date_precision="date_only", source_url="https://elearn.nthu.edu.tw/course/item", remote_status="graded", remote_status_text="已繳交供評分", grade_text="92 / 100")
        quiz = LearningItem(owner_id=owner.id, source_course_id=source.id, remote_item_id="quiz-1", kind="quiz", title="測驗關閉時間", closes_at=datetime(2026, 10, 6, 1, 30, tzinfo=UTC), date_precision="exact")
        unsubmitted = LearningItem(owner_id=owner.id, source_course_id=source.id, remote_item_id="item-2", kind="assignment", title="尚未繳交作業", remote_status="not_submitted")
        material = CourseMaterial(owner_id=owner.id, source_course_id=source.id, remote_id="material-1", section_title="第一週", kind="file", title="課程講義.pdf", source_url="https://elearn.nthu.edu.tw/mod/resource/view.php?id=9")
        announcement = Announcement(owner_id=owner.id, source_course_id=source.id, remote_id="announcement-1", title="開學公告", body_sanitized="請於第一週完成課程分組。", source_url="https://elearn.nthu.edu.tw/mod/forum/discuss.php?d=9")
        session.add_all([selection, item, quiz, unsubmitted, material, announcement]); session.flush()
        group_id, source_id, item_id, quiz_id = group.id, source.id, item.id, quiz.id
    detail = client.get(f"/courses/{group_id}")
    assert detail.status_code == 200 and "使用此來源" in detail.text
    selected = client.put(f"/api/v1/courses/{group_id}/selection", headers=headers, json={"source_id": source_id, "selection_state": "selected", "expected_version": 1})
    assert selected.status_code == 200
    assert selected.json()["data"]["selection_state"] == "selected"
    loaded = client.get(f"/api/v1/courses/{group_id}").json()["data"]
    assert any(x["selection_state"] == "selected" for x in loaded["sources"])
    assert loaded["materials"][0]["title"] == "課程講義.pdf"
    assert loaded["grades"][0]["grade_text"] == "92 / 100"
    course_page = client.get(f"/courses/{group_id}").text
    assert all(text in course_page for text in ("教材", "作業與測驗", "成績", "課程講義.pdf", "開學公告", "請於第一週完成課程分組。", "開啟／下載"))
    assert 'data-target-url="https://elearn.nthu.edu.tw/mod/resource/view.php?id=9"' in course_page
    item_detail = client.get(f"/items/{item_id}").text
    assert "期限日期" in item_detail and "2026-11-03" in item_detail
    assert 'data-action="open-platform"' in item_detail
    assert 'data-platform="elearn"' in item_detail
    assert 'data-target-url="https://elearn.nthu.edu.tw/course/item"' in item_detail
    quiz_detail = client.get(f"/items/{quiz_id}").text
    assert "測驗關閉" in quiz_detail and "2026-10-06 09:30" in quiz_detail
    assert "測驗關閉時間" in client.get("/todos").text
    todo_page = client.get("/todos").text
    assert "已繳交" in todo_page and "未繳交" in todo_page
    assert "92 / 100" in todo_page and "submission-check submitted" in todo_page
    assert "submission-check unsubmitted" in todo_page and "submission-label unsubmitted" in todo_page
    assert 'id="todo-status"' in todo_page and 'value="submitted"' in todo_page
    assert 'id="todo-course"' in todo_page and f'value="{group_id}"' in todo_page
    assert f'data-course="{group_id}"' in todo_page
    status_page = client.get("/status").text
    assert "eLearn" in status_page and "工作：" in status_page


def test_selecting_one_cross_platform_course_source_disables_the_other(web):
    client, _settings, db, owner = web
    headers = login(client)
    with db.session() as session:
        elearn = PlatformConnection(owner_id=owner.id, platform="elearn", auth_state="connected", generation=1)
        eeclass = PlatformConnection(owner_id=owner.id, platform="eeclass", auth_state="connected", generation=1)
        group = CourseGroup(owner_id=owner.id, display_name="離散數學", course_code="11510CS135601")
        session.add_all((elearn, eeclass, group)); session.flush()
        first = CourseSource(owner_id=owner.id, connection_id=elearn.id, remote_course_id="e-1", course_code=group.course_code, title="離散數學")
        second = CourseSource(owner_id=owner.id, connection_id=eeclass.id, remote_course_id="c-1", course_code=group.course_code, title="離散數學")
        session.add_all((first, second)); session.flush()
        first_selection = CourseSelection(owner_id=owner.id, group_id=group.id, source_id=first.id, selection_state="pending")
        second_selection = CourseSelection(owner_id=owner.id, group_id=group.id, source_id=second.id, selection_state="pending")
        session.add_all((first_selection, second_selection)); session.flush()
        group_id, first_id = group.id, first.id
    page = client.get("/courses").text
    assert "11510CS135601" in page and "待設定" in page
    chosen = client.put(f"/api/v1/courses/{group_id}/selection", headers=headers, json={"source_id": first_id, "selection_state": "selected", "expected_version": 1})
    assert chosen.status_code == 200
    loaded = client.get(f"/api/v1/courses/{group_id}").json()["data"]
    states = {source["platform"]: source["selection_state"] for source in loaded["sources"]}
    assert states == {"elearn": "selected", "eeclass": "disabled"}
    assert loaded["selection_required"] is False

    disabled = client.put(f"/api/v1/courses/{group_id}/selection", headers=headers, json={"source_id": first_id, "selection_state": "none", "expected_version": 2})
    assert disabled.status_code == 200
    loaded = client.get(f"/api/v1/courses/{group_id}").json()["data"]
    assert loaded["disabled"] is True
    assert loaded["selection_required"] is False
    assert {source["selection_state"] for source in loaded["sources"]} == {"disabled"}
    page = client.get("/courses").text
    assert 'data-action="course-selection"' not in page
    assert "不使用線上平台" in page
    detail = client.get(f"/courses/{group_id}").text
    assert 'data-state="none"' in detail
    assert 'data-course-grid' in page


def test_taipei_time_editor_and_new_dialog_reset_are_shipped(web):
    client, *_ = web
    script = client.get("/static/app.js").text
    assert 'timeZone: "Asia/Taipei"' in script
    assert "delete form.dataset.id" in script
    assert '`${raw.starts_at}:00+08:00`' in script


def test_todo_filter_can_hide_flex_rows_and_shows_empty_state(web):
    client, *_ = web
    login(client)
    script = client.get("/static/app.js").text
    stylesheet = client.get("/static/app.css").text
    page = client.get("/todos").text
    assert "element.hidden = !matches" in script
    assert "filterEmpty.hidden = visible !== 0" in script
    assert "[hidden]{display:none!important}" in stylesheet
    assert 'id="todo-filter-empty" hidden' in page
    assert ".submission-check.unsubmitted" in stylesheet
    assert ".submission-label.unsubmitted" in stylesheet


def test_sync_pages_distinguish_latest_attempt_from_last_complete_success(web):
    client, _settings, db, owner = web
    login(client)
    with db.session() as session:
        connection = PlatformConnection(
            owner_id=owner.id, platform="elearn", auth_state="connected", generation=1,
            last_success_at=datetime(2026, 10, 5, 5, 38, tzinfo=UTC),
        )
        session.add(connection); session.flush()
        session.add(SyncJob(
            owner_id=owner.id, connection_id=connection.id, generation=1,
            preferences_version=1, state="partial",
            scheduled_at=datetime(2026, 10, 5, 6, 38, tzinfo=UTC),
            started_at=datetime(2026, 10, 5, 6, 38, tzinfo=UTC),
            finished_at=datetime(2026, 10, 5, 6, 39, tzinfo=UTC), error_code="partial_fetch",
        ))
        session.add(ScheduleState(
            owner_id=owner.id, schedule_key=f"sync:{connection.id}",
            next_run_at=datetime(2026, 10, 5, 7, 38, tzinfo=UTC),
        ))
    for path in ("/", "/settings/connections", "/status"):
        page = client.get(path).text
        assert "最近同步嘗試" in page and "2026-10-05 14:39" in page
        assert "最近完整成功" in page and "2026-10-05 13:38" in page
        assert "下次排程" in page and "2026-10-05 15:38" in page
        assert "部分完成" in page


def test_csrf_origin_cookie_headers_and_generic_errors(web):
    client, _settings, _db, _owner = web
    page = client.get("/login")
    token = re.search(r'name="csrf_token" value="([^"]+)"', page.text).group(1)
    response = client.post("/api/v1/auth/login", headers={"Origin": ORIGIN}, json={"login_name": "owner", "password": "a-long-local-password", "csrf_token": token})
    cookie = response.headers["set-cookie"]
    assert "HttpOnly" in cookie and "SameSite=lax" in cookie
    csrf = client.get("/api/v1/auth/me").json()["data"]["csrf_token"]
    assert client.post("/api/v1/manual-events", json={"title": "x", "date_precision": "unknown"}).status_code == 403
    assert client.post("/api/v1/manual-events", headers={"Origin": "http://evil.test", "X-CSRF-Token": csrf}, json={"title": "x", "date_precision": "unknown"}).status_code == 403
    invalid = client.post("/api/v1/credentials", headers={"Origin": ORIGIN, "X-CSRF-Token": csrf}, json={"account": "student", "password": "school-secret"})
    assert invalid.status_code == 201
    serialized = invalid.text
    assert "school-secret" not in serialized and "password" not in serialized
    replacement = client.post("/api/v1/credentials", headers={"Origin": ORIGIN, "X-CSRF-Token": csrf}, json={"account": "newest-account", "password": "new-school-secret"})
    assert replacement.status_code == 201
    connections = client.get("/settings/connections").text
    assert connections.count('data-action="connect"') >= 2
    assert "ne***nt" in connections and "st***nt" not in connections
    assert "new-school-secret" not in connections and "school-secret" not in connections
    bad = client.post("/api/v1/manual-events", headers={"Origin": ORIGIN, "X-CSRF-Token": csrf}, json={"title": ""})
    assert bad.json().keys() == {"code", "message", "retryable", "request_id"}


def test_owner_isolation_and_host_allowlist(web):
    client, settings, db, first = web
    service = client.app.state.service
    event = service.create_manual_event(first.id, {"title": "私人考試", "date_precision": "unknown"})
    with db.session() as session:
        second = Owner(login_name="second", password_hash=PasswordHasher().hash("another-long-password"), timezone="Asia/Taipei")
        session.add(second)
        session.flush()
        second_id = second.id
    raw, second_session = create_session(db, second_id, "test")
    outsider = TestClient(client.app, base_url=ORIGIN, cookies={"ecourse_session": raw})
    response = outsider.get(f"/api/v1/manual-events/{event['id']}")
    assert response.status_code == 404
    assert "私人考試" not in response.text
    assert client.get("/health/live", headers={"Host": "evil.test"}).status_code == 400


def test_uninitialized_root_does_not_offer_public_setup(tmp_path):
    settings = Settings(tmp_path / "data", tmp_path / "secrets", base_url=ORIGIN)
    with TestClient(create_app(settings), base_url=ORIGIN) as client:
        response = client.get("/", follow_redirects=False)
        assert response.status_code == 303
        page = client.get("/login")
        assert "ecourse init" in page.text
        assert "建立擁有者" not in [route.path for route in client.app.routes]


def test_login_and_settings_only_present_school_credentials(web, monkeypatch):
    client, *_ = web
    login_page = client.get("/login").text
    assert "校務帳號" in login_page and "校務密碼" in login_page
    assert "eCourse 帳號" not in login_page and "eCourse 密碼" not in login_page
    headers = login(client)
    settings_page = client.get("/settings/connections").text
    assert "更新並重新連接" in settings_page
    assert "review" not in settings_page
    service = client.app.state.service
    monkeypatch.setattr(service, "replace_credentials_and_reconnect", lambda owner_id, account, password: {
        "credential": {"id": "new-credential", "account_hint": "st***nt"},
        "attempts": {"elearn": {"attempt_id": "attempt-e"}, "eeclass": {"attempt_id": "attempt-c"}},
    })
    updated = client.put("/api/v1/settings/credentials", headers=headers, json={"account": "student", "password": "synthetic-secret"})
    assert updated.status_code == 202
    assert set(updated.json()["data"]["attempts"]) == {"elearn", "eeclass"}
    assert "synthetic-secret" not in updated.text


def test_school_login_pending_exchange_completes_with_http_only_cookie(tmp_path, monkeypatch):
    import ecourse.web.app as web_app

    data, secrets = tmp_path / "data", tmp_path / "secrets"
    initialize_key(secrets, data)
    settings = Settings(data, secrets, base_url=ORIGIN)
    db = Database(settings)
    db.initialize()
    owner = initialize_owner(settings, "hidden-owner", "unused-local-password")
    monkeypatch.setattr(web_app, "authenticate_school_credential", lambda *_args: None)
    monkeypatch.setattr(web_app, "begin_school_auth", lambda *_args, **_kwargs: {
        "exchange_token": "private-exchange", "attempt_id": "attempt-1", "state": "queued",
        "expires_at": "2026-10-05T10:00:00+00:00",
    })
    monkeypatch.setattr(web_app, "school_auth_status", lambda *_args: {"attempt_id": "attempt-1", "state": "succeeded"})
    monkeypatch.setattr(web_app, "complete_school_auth", lambda *_args: create_session(db, owner.id, "test"))
    with TestClient(create_app(settings), base_url=ORIGIN) as client:
        page = client.get("/login")
        token = re.search(r'name="csrf_token" value="([^"]+)"', page.text).group(1)
        started = client.post("/api/v1/auth/login", headers={"Origin": ORIGIN}, json={"login_name": "school-id", "password": "school-password", "csrf_token": token})
        assert started.status_code == 202
        assert "private-exchange" not in started.text and "school-password" not in started.text
        assert "HttpOnly" in started.headers["set-cookie"]
        status = client.get("/api/v1/auth/school/status", headers={"Referer": ORIGIN + "/login"})
        assert status.json()["data"]["state"] == "succeeded"
        completed = client.post("/api/v1/auth/school/complete", headers={"Origin": ORIGIN, "X-CSRF-Token": token}, json={})
        assert completed.status_code == 200
        assert client.get("/api/v1/auth/me").status_code == 200


def test_page_does_not_turn_database_failure_into_empty_state(web, monkeypatch):
    client, *_ = web
    login(client)
    monkeypatch.setattr(client.app.state.service, "list_items", lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("db unavailable")))
    with TestClient(client.app, base_url=ORIGIN, raise_server_exceptions=False, cookies=client.cookies) as quiet_client:
        response = quiet_client.get("/todos")
    assert response.status_code == 500
    assert response.json()["code"] == "internal_error"
    assert "沒有符合條件的待辦" not in response.text


def test_connection_retry_and_sync_status_contracts(web, monkeypatch):
    client, _settings, _db, owner = web
    headers = login(client)
    service = client.app.state.service
    monkeypatch.setattr(service, "retry_connection", lambda owner_id, platform: {"attempt_id": "attempt-1", "platform": platform})
    monkeypatch.setattr(service, "get_login_attempt", lambda owner_id, attempt_id: {"id": attempt_id, "state": "expired", "error_code": "challenge_expired"})
    monkeypatch.setattr(service, "enqueue_sync", lambda owner_id: [{"id": "job-1", "state": "queued"}])
    monkeypatch.setattr(service, "get_sync_job", lambda owner_id, job_id: {"id": job_id, "state": "succeeded", "finished_at": "2026-10-04T01:00:00Z"})
    retried = client.post("/api/v1/connections/elearn/retry", headers=headers, json={})
    assert retried.status_code == 202 and retried.json()["data"]["attempt_id"] == "attempt-1"
    assert client.get("/api/v1/login-attempts/attempt-1").json()["data"]["state"] == "expired"
    jobs = client.post("/api/v1/sync", headers=headers, json={}).json()["data"]
    assert client.get(f"/api/v1/sync/jobs/{jobs[0]['id']}").json()["data"]["state"] == "succeeded"


def test_client_workflows_poll_and_refresh_without_duplicate_submit(web):
    client, *_ = web
    script = client.get("/static/app.js").text
    assert "watchSync" in script and "/api/v1/sync/jobs/" in script
    assert 'saveFlash("同步完成。")' not in script  # outcome is selected from real job states
    assert "location.reload();" in script and "sessionStorage.setItem" in script
    assert "retryChallenge" in script and script.count('document.addEventListener("click"') == 1
    assert "async function withForm" in script and "finally { if (button) button.disabled = false; }" in script
    assert "pendingWrites.has" in script
    assert "HTTP ${response.status}" in script and "body.detail" in script
    assert "URLSearchParams(location.search)" in script and "scrollIntoView" in script


def test_connection_copy_states_real_credential_and_sync_limits(web):
    client, *_ = web
    login(client)
    page = client.get("/settings/connections").text
    assert "帳密加密儲存於本機" in page
    assert "更新後會重新連接平台" in page
    assert "每小時檢查並同步" in page
    assert "登入失效會使用本機加密保存的帳密自動重新登入" in page
    assert "過期後需重試" in page
    assert "永久" not in page and "保證" not in page


def test_managed_platform_open_route_is_csrf_protected_and_scoped(web, monkeypatch):
    client, *_ = web
    headers = login(client)
    service = client.app.state.service
    calls = []

    async def opened(owner_id, platform, target_url):
        calls.append((owner_id, platform, target_url))
        return {"status": "opened", "platform": platform}

    monkeypatch.setattr(service, "open_platform", opened)
    assert client.post("/api/v1/connections/elearn/open", json={}).status_code == 403
    response = client.post(
        "/api/v1/connections/elearn/open", headers=headers,
        json={"target_url": "https://elearn.nthu.edu.tw/course/view.php?id=1"},
    )
    assert response.status_code == 200
    assert response.json()["data"]["status"] == "opened"
    assert calls and calls[0][1:] == ("elearn", "https://elearn.nthu.edu.tw/course/view.php?id=1")


def test_extension_handoff_is_short_lived_origin_bound_and_one_time(web, monkeypatch):
    client, *_ = web
    headers = login(client)
    service = client.app.state.service

    async def prepare(owner_id, platform, target_url):
        return {"status": "ready", "platform": platform, "target_url": target_url or "https://elearn.nthu.edu.tw/my/index.php"}

    async def export(owner_id, platform):
        return [{"name": "MoodleSession", "value": "opaque", "domain": "elearn.nthu.edu.tw", "path": "/", "secure": True, "httpOnly": True}]

    monkeypatch.setattr(service, "prepare_platform_handoff", prepare)
    monkeypatch.setattr(service, "export_platform_cookies", export)
    prepared = client.post("/api/v1/connections/elearn/extension-handoff", headers=headers, json={})
    assert prepared.status_code == 200
    token = prepared.json()["data"]["handoff_token"]
    assert prepared.json()["data"]["extension_id"] == "amecmikkjicakbcboajnfmbhekmkpahk"

    path = f"/api/v1/browser-extension/handoffs/{token}"
    assert client.post(path).status_code == 403
    extension_headers = {"Origin": "chrome-extension://amecmikkjicakbcboajnfmbhekmkpahk"}
    consumed = client.post(path, headers=extension_headers)
    assert consumed.status_code == 200
    assert consumed.headers["access-control-allow-origin"] == extension_headers["Origin"]
    assert consumed.json()["data"]["cookies"][0]["value"] == "opaque"
    assert client.post(path, headers=extension_headers).status_code == 410


def test_connection_captcha_copy_tracks_real_attempt_states(web, monkeypatch):
    client, *_ = web
    login(client)
    service = client.app.state.service
    monkeypatch.setattr(service, "list_connections", lambda _owner_id: [
        {"platform": "elearn", "auth_state": "authenticating", "active_attempt": None},
        {"platform": "eeclass", "auth_state": "challenge_required", "active_attempt": None},
    ])
    page = client.get("/settings/connections").text
    assert "正在登入，自動辨識驗證碼中" in page
    assert "自動辨識未完成，請輸入驗證碼" in page
    assert "自動辨識成功" not in page


def test_javascript_sync_and_form_lifecycle_with_node():
    source = open("src/ecourse/web/static/app.js", encoding="utf-8").read()
    prelude = r'''
const noticeNode={textContent:"",hidden:true,classList:{toggle(_n,v){noticeNode.error=v}},append(){}};
const boxes={elearn:{children:[],replaceChildren(){this.children=[]},append(x){this.children.push(x)}},eeclass:{children:[],replaceChildren(){this.children=[]},append(x){this.children.push(x)}}};
globalThis.document={body:{dataset:{}},querySelector(s){if(s==="#notice")return noticeNode;const m=s.match(/data-challenge-platform="([^"]+)/);return m?boxes[m[1]]:null},querySelectorAll(){return[]},addEventListener(){},createElement(tag){return{tag,textContent:"",classList:{add(){}},append(){},addEventListener(){},dataset:{}}},getElementById(){return null}};
globalThis.sessionStorage={v:new Map(),setItem(k,v){this.v.set(k,v)},getItem(k){return this.v.get(k)||null},removeItem(k){this.v.delete(k)}};
globalThis.location={search:"",reloads:0,reload(){this.reloads++}};globalThis.CSS={escape:x=>x};globalThis.confirm=()=>false;
let replies=[],byPath={};globalThis.fetch=async path=>({ok:true,async json(){return{data:byPath[path]||replies.shift()}}});
'''
    scenario = r'''
(async()=>{
 replies=[{id:"j",state:"partial"}];await watchSync([{id:"j"}]);
 const flash=JSON.parse(sessionStorage.getItem("ecourse_flash"));if(!flash.error||!flash.message.includes("部分完成")||location.reloads!==1)throw Error("partial outcome");
 location.reloads=0;noticeNode.textContent="";replies=[{id:"j",state:"waiting_user"}];await watchSync([{id:"j"}]);if(!noticeNode.textContent.includes("等待驗證碼")||location.reloads)throw Error("waiting user");
 const button={disabled:false};const form={querySelector(){return button}};let release;const work=new Promise(r=>release=r);const pending=withForm(form,()=>work);await Promise.resolve();if(!button.disabled)throw Error("form unlocked early");release();await pending;if(button.disabled)throw Error("form stayed locked");
 byPath={"/api/v1/login-attempts/e":{id:"e",state:"failed",error_code:"e_fail"},"/api/v1/login-attempts/c":{id:"c",state:"failed",error_code:"c_fail"}};
 await Promise.all([beginAttempt("e","elearn"),beginAttempt("c","eeclass")]);
 if(!boxes.elearn.children[0].textContent.includes("e_fail")||!boxes.eeclass.children[0].textContent.includes("c_fail"))throw Error("attempt boxes overlapped");
 console.log("ok");
})().catch(e=>{console.error(e);process.exit(1)});
'''
    result = subprocess.run(["node", "--unhandled-rejections=strict", "-"], input=prelude + "\n" + source + "\n" + scenario, text=True, capture_output=True)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "ok"


def test_course_overview_excludes_cached_platform_tutorial_and_marks_single_pending(web):
    client, _settings, db, owner = web
    login(client)
    with db.session() as session:
        connection = PlatformConnection(owner_id=owner.id, platform="elearn", auth_state="connected", generation=1)
        session.add(connection); session.flush()
        for title in ("操作教學影片", "影片製作"):
            group = CourseGroup(owner_id=owner.id, display_name=title)
            source = CourseSource(owner_id=owner.id, connection_id=connection.id, remote_course_id=title, title=title)
            session.add_all((group, source)); session.flush()
            session.add(CourseSelection(owner_id=owner.id, group_id=group.id, source_id=source.id, selection_state="pending"))
            if title == "影片製作":
                course_id = group.id
    page = client.get("/courses").text
    assert "操作教學影片" not in page
    assert "影片製作" in page and "待設定" in page
    assert 'data-action="course-selection"' not in page
    detail = client.get(f"/courses/{course_id}").text
    assert 'source-settings" open' in detail
    assert 'data-state="none"' in detail
