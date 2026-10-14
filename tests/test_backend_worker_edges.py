import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from ecourse.db.models import Announcement, CourseGroup, CourseMaterial, CourseSelection, CourseSource, CredentialSecret, LearningItem, LoginAttempt, PlatformConnection, ScheduleState, SessionState, SyncJob
from ecourse.ipc import IPCError
from ecourse.services import Service
from ecourse.worker import Worker
from ecourse.adapters.base import Course, CourseItem, FetchResult, ItemDetail


def _connection_job(settings, db, owner):
    service = Service(settings)
    credential = service.save_credentials(owner.id, "account-a", "secret-a")
    public = service.connect(owner.id, "elearn", credential["id"])
    job = service.enqueue_sync(owner.id, "elearn")[0]
    return service, public, job


def test_adapter_challenge_binds_existing_public_attempt(backend):
    settings, db, owner = backend
    service, public, job = _connection_job(settings, db, owner)
    worker = Worker(settings, worker_id="test")
    connection_id = public["connection"]["id"]
    worker._record_challenge(connection_id, owner.id, public["connection"]["generation"], {
        "attempt_id": "adapter-private-id",
        "status": "challenge_required",
        "challenge": {"challenge_id": "captcha-1", "type": "image_captcha", "image": b"pixels", "expires_at": datetime.now(UTC) + timedelta(minutes=5)},
    })
    attempt = service.get_login_attempt(owner.id, public["attempt_id"])
    assert attempt["state"] == "challenge_required"
    assert attempt["challenge_id"] == "captcha-1"
    assert public["attempt_id"] in worker.challenges
    with db.session() as session:
        assert len(session.scalars(select(LoginAttempt)).all()) == 1


def test_challenge_image_checks_generation_before_returning_bytes(backend):
    settings, db, owner = backend
    _service, public, _job = _connection_job(settings, db, owner)
    worker = Worker(settings, worker_id="test")
    worker._record_challenge(public["connection"]["id"], owner.id, public["connection"]["generation"], {
        "attempt_id": "adapter-id", "challenge": {"challenge_id": "captcha-1", "image": b"pixels", "expires_at": datetime.now(UTC) + timedelta(minutes=5)}
    })
    with db.session() as session:
        session.get(PlatformConnection, public["connection"]["id"]).generation += 1
    with pytest.raises(IPCError) as error:
        asyncio.run(worker.ipc_handler("challenge.get", {"owner_id": owner.id, "attempt_id": public["attempt_id"], "challenge_id": "captcha-1"}))
    assert error.value.code == "stale_generation"


def test_session_network_failure_does_not_trigger_relogin(backend):
    settings, db, owner = backend
    service, public, job_data = _connection_job(settings, db, owner)

    class Adapter:
        began = False
        async def verify_session(self, state):
            return SimpleNamespace(status="upstream_unavailable", error_code="upstream_unavailable")
        async def begin_login(self, *args, **kwargs):
            self.began = True

    adapter = Adapter()
    worker = Worker(settings, adapters={"elearn": adapter}, worker_id="worker")
    worker._save_session_state(public["connection"]["id"], "elearn", owner.id, public["connection"]["generation"], {"cookies": []}, "remote-a")
    claimed = worker.claim_job()
    asyncio.run(worker.process_job(claimed))
    assert adapter.began is False
    result = service.get_sync_job(owner.id, job_data["id"])
    assert result["state"] == "queued" and result["error_code"] == "upstream_unavailable"
    assert datetime.fromisoformat(result["scheduled_at"]) > datetime.now(UTC)


def test_managed_platform_open_verifies_identity_and_keeps_cookie_server_side(backend):
    settings, db, owner = backend
    _service, public, _job = _connection_job(settings, db, owner)
    opened = []

    class Adapter:
        async def verify_session(self, state):
            assert state == {"cookies": [{"name": "session", "value": "opaque"}]}
            return SimpleNamespace(status="connected", remote_account_id="remote-a", error_code=None)

    async def opener(state, target_url):
        opened.append((state, target_url))
        return {"opened_url": target_url}

    worker = Worker(
        settings, adapters={"elearn": Adapter()}, worker_id="worker",
        platform_opener=opener,
    )
    worker._save_session_state(
        public["connection"]["id"], "elearn", owner.id,
        public["connection"]["generation"],
        {"cookies": [{"name": "session", "value": "opaque"}]}, "remote-a",
    )
    with db.session() as session:
        session.get(PlatformConnection, public["connection"]["id"]).last_verified_at = datetime.now(UTC) - timedelta(hours=2)
    result = asyncio.run(worker.ipc_handler("platform.open", {
        "owner_id": owner.id, "platform": "elearn",
        "target_url": "https://elearn.nthu.edu.tw/mod/assign/view.php?id=1",
    }))
    assert result == {
        "status": "opened", "platform": "elearn",
        "opened_url": "https://elearn.nthu.edu.tw/mod/assign/view.php?id=1",
    }
    assert opened[0][1].startswith("https://elearn.nthu.edu.tw/")


def test_managed_platform_open_rejects_cross_platform_url_and_queues_expired_session(backend):
    settings, db, owner = backend
    _service, public, _job = _connection_job(settings, db, owner)

    class Adapter:
        async def verify_session(self, _state):
            return SimpleNamespace(status="expired", remote_account_id=None, error_code="session_expired")

    worker = Worker(settings, adapters={"elearn": Adapter()}, worker_id="worker")
    with pytest.raises(IPCError) as error:
        asyncio.run(worker.ipc_handler("platform.open", {
            "owner_id": owner.id, "platform": "elearn",
            "target_url": "https://eeclass.nthu.edu.tw/dashboard",
        }))
    assert error.value.code == "invalid_platform_url"

    result = asyncio.run(worker.ipc_handler("platform.open", {
        "owner_id": owner.id, "platform": "elearn", "target_url": None,
    }))
    assert result["status"] == "reauthentication_required"
    with db.session() as session:
        attempt = session.get(LoginAttempt, result["attempt_id"])
        assert attempt.state in {"queued", "authenticating"}
        connection = session.get(PlatformConnection, public["connection"]["id"])
        assert connection.auth_state == "authenticating"


def test_extension_handoff_refreshes_recent_session_before_export(backend):
    settings, db, owner = backend
    _service, public, _job = _connection_job(settings, db, owner)

    class Adapter:
        calls = 0

        async def verify_session(self, state):
            self.calls += 1
            assert state["cookies"][0]["value"] == "stale"
            return SimpleNamespace(
                status="connected", remote_account_id="remote-a", error_code=None,
                storage_state={"cookies": [{
                    "name": "MoodleSession", "value": "fresh",
                    "domain": "elearn.nthu.edu.tw", "path": "/",
                    "secure": True, "httpOnly": True, "sameSite": "Lax",
                    "expires": -1,
                }]},
            )

    adapter = Adapter()
    worker = Worker(settings, adapters={"elearn": adapter}, worker_id="worker")
    worker._save_session_state(
        public["connection"]["id"], "elearn", owner.id,
        public["connection"]["generation"],
        {"cookies": [{
            "name": "MoodleSession", "value": "stale",
            "domain": "elearn.nthu.edu.tw", "path": "/",
            "secure": True, "httpOnly": True, "sameSite": "Lax",
            "expires": -1,
        }]},
        "remote-a",
    )
    result = asyncio.run(worker.ipc_handler("platform.prepare_handoff", {
        "owner_id": owner.id, "platform": "elearn", "target_url": None,
    }))
    exported = asyncio.run(worker.ipc_handler("platform.export_cookies", {
        "owner_id": owner.id, "platform": "elearn",
    }))
    assert adapter.calls == 1
    assert result["status"] == "ready"
    assert exported["cookies"][0]["value"] == "fresh"


def test_extension_handoff_reauthenticates_stale_expired_session(backend):
    settings, db, owner = backend
    _service, public, _job = _connection_job(settings, db, owner)

    class Adapter:
        calls = 0

        async def verify_session(self, _state):
            self.calls += 1
            return SimpleNamespace(status="expired", remote_account_id=None, error_code="session_expired")

    adapter = Adapter()
    worker = Worker(settings, adapters={"elearn": adapter}, worker_id="worker")
    worker._save_session_state(
        public["connection"]["id"], "elearn", owner.id,
        public["connection"]["generation"], {"cookies": []}, "remote-a",
    )
    with db.session() as session:
        session.get(PlatformConnection, public["connection"]["id"]).last_verified_at = datetime.now(UTC) - timedelta(hours=2)

    result = asyncio.run(worker.ipc_handler("platform.prepare_handoff", {
        "owner_id": owner.id, "platform": "elearn", "target_url": None,
    }))

    assert adapter.calls == 1
    assert result["status"] == "reauthentication_required"
    with db.session() as session:
        connection = session.get(PlatformConnection, public["connection"]["id"])
        assert connection.auth_state == "authenticating"


def test_extension_handoff_reauthenticates_when_protected_page_is_not_reached(backend):
    settings, db, owner = backend
    _service, public, _job = _connection_job(settings, db, owner)

    class Adapter:
        async def verify_session(self, _state):
            return SimpleNamespace(
                status="parser_changed", remote_account_id=None,
                error_code="protected_page_not_reached",
            )

    worker = Worker(settings, adapters={"elearn": Adapter()}, worker_id="worker")
    worker._save_session_state(
        public["connection"]["id"], "elearn", owner.id,
        public["connection"]["generation"], {"cookies": []}, "remote-a",
    )
    with db.session() as session:
        session.get(PlatformConnection, public["connection"]["id"]).last_verified_at = datetime.now(UTC) - timedelta(hours=2)

    result = asyncio.run(worker.ipc_handler("platform.prepare_handoff", {
        "owner_id": owner.id, "platform": "elearn", "target_url": None,
    }))

    assert result["status"] == "reauthentication_required"
    with db.session() as session:
        connection = session.get(PlatformConnection, public["connection"]["id"])
        assert connection.auth_state == "authenticating"


def test_whole_job_timeout_releases_queue_for_retry(backend, monkeypatch):
    settings, db, owner = backend
    service, public, job_data = _connection_job(settings, db, owner)
    worker = Worker(
        settings, worker_id="worker", job_timeout_seconds=0.01,
        retry_delays=(1,),
    )

    async def never_finishes(_job):
        await asyncio.Event().wait()

    monkeypatch.setattr(worker, "process_job", never_finishes)
    asyncio.run(worker._process_claimed_job(worker.claim_job()))

    result = service.get_sync_job(owner.id, job_data["id"])
    assert result["state"] == "queued"
    assert result["error_code"] == "job_timeout"
    assert datetime.fromisoformat(result["scheduled_at"]) > datetime.now(UTC)
    with db.session() as session:
        connection = session.get(PlatformConnection, public["connection"]["id"])
        assert connection.auth_state == "temporarily_unavailable"


def test_extension_cookie_export_is_owner_scoped_and_platform_filtered(backend):
    settings, _db, owner = backend
    _service, public, _job = _connection_job(settings, _db, owner)
    worker = Worker(settings, worker_id="worker")
    worker._save_session_state(
        public["connection"]["id"], "elearn", owner.id,
        public["connection"]["generation"],
        {"cookies": [
            {"name": "MoodleSession", "value": "opaque", "domain": "elearn.nthu.edu.tw", "path": "/", "secure": True, "httpOnly": True, "sameSite": "Lax", "expires": -1},
            {"name": "TS01910f95", "value": "browser-bound", "domain": ".elearn.nthu.edu.tw", "path": "/", "secure": True},
            {"name": "sso", "value": "must-not-export", "domain": "oauth.ccxp.nthu.edu.tw", "path": "/", "secure": True},
        ]},
        "remote-a",
    )
    result = asyncio.run(worker.ipc_handler("platform.export_cookies", {
        "owner_id": owner.id, "platform": "elearn",
    }))
    assert [cookie["name"] for cookie in result["cookies"]] == ["MoodleSession", "TS01910f95"]
    assert "must-not-export" not in str(result)
    assert "browser-bound" in str(result)


def test_extension_cookie_export_accepts_current_elearn_session_name(backend):
    settings, db, owner = backend
    _service, public, _job = _connection_job(settings, db, owner)
    worker = Worker(settings, worker_id="worker")
    worker._save_session_state(
        public["connection"]["id"], "elearn", owner.id,
        public["connection"]["generation"],
        {"cookies": [
            {"name": "MoodleSessionM41", "value": "opaque", "domain": "elearn.nthu.edu.tw", "path": "/", "secure": True, "httpOnly": True, "sameSite": "None", "expires": -1},
        ]},
        "remote-a",
    )
    result = asyncio.run(worker.ipc_handler("platform.export_cookies", {
        "owner_id": owner.id, "platform": "elearn",
    }))
    assert [cookie["name"] for cookie in result["cookies"]] == ["MoodleSessionM41"]


def test_partial_catalog_never_records_full_success(backend):
    settings, db, owner = backend
    service, public, job_data = _connection_job(settings, db, owner)

    class Adapter:
        async def verify_session(self, state):
            return SimpleNamespace(status="connected", remote_account_id="remote-a", error_code=None)
        async def discover_courses(self, state):
            return SimpleNamespace(records=(), coverage=("course_catalog",), complete=False, warnings=("parser_changed",))

    worker = Worker(settings, adapters={"elearn": Adapter()}, worker_id="worker")
    worker._save_session_state(public["connection"]["id"], "elearn", owner.id, public["connection"]["generation"], {"cookies": []}, "remote-a")
    asyncio.run(worker.process_job(worker.claim_job()))
    result = service.get_sync_job(owner.id, job_data["id"])
    assert result["state"] == "partial" and result["error_code"] == "partial_fetch"
    with db.session() as session:
        assert session.get(PlatformConnection, public["connection"]["id"]).last_success_at is None


def test_partial_content_does_not_overwrite_successful_login_attempt(backend):
    settings, db, owner = backend
    service, public, _job_data = _connection_job(settings, db, owner)
    worker = Worker(settings, adapters={}, worker_id="worker")
    claimed = worker.claim_job()
    with db.session() as session:
        connection = session.get(PlatformConnection, public["connection"]["id"])
        connection.auth_state = "connected"
        attempt = session.get(LoginAttempt, public["attempt_id"])
        attempt.state = "succeeded"

    worker._finish(claimed.id, "partial", "partial_fetch")

    assert service.get_login_attempt(owner.id, public["attempt_id"])["state"] == "succeeded"
    assert service.list_connections(owner.id)[0]["auth_state"] == "connected"


def test_switching_credentials_removes_old_remote_namespace(backend):
    settings, db, owner = backend
    service, public, _job = _connection_job(settings, db, owner)
    with db.session() as session:
        connection = session.get(PlatformConnection, public["connection"]["id"])
        connection.remote_account_id = "remote-a"
        session.add(CourseSource(owner_id=owner.id, connection_id=connection.id, remote_course_id="old-course", title="舊帳號課程"))
    replacement = service.save_credentials(owner.id, "account-b", "secret-b")
    service.connect(owner.id, "elearn", replacement["id"])
    with db.session() as session:
        assert session.scalars(select(CourseSource).where(CourseSource.connection_id == public["connection"]["id"])).all() == []
        connection = session.get(PlatformConnection, public["connection"]["id"])
        assert connection.remote_account_id is None


def test_reconnect_with_same_credential_invalidates_old_session_generation(backend):
    settings, db, owner = backend
    service, public, _job = _connection_job(settings, db, owner)
    connection_id = public["connection"]["id"]
    generation = public["connection"]["generation"]
    with db.session() as session:
        credential_id = session.get(PlatformConnection, connection_id).credential_id
    worker = Worker(settings, worker_id="session-fixture")
    assert worker._save_session_state(
        connection_id, "elearn", owner.id, generation,
        {"cookies": [{"name": "old-session", "value": "opaque"}]},
    )

    changed = service.connect(owner.id, "elearn", credential_id)

    assert changed["connection"]["generation"] == generation + 1
    with db.session() as session:
        assert session.get(SessionState, connection_id) is None


def test_retry_connection_invalidates_old_session_before_new_login(backend):
    settings, db, owner = backend
    service, public, _job = _connection_job(settings, db, owner)
    connection_id = public["connection"]["id"]
    generation = public["connection"]["generation"]
    worker = Worker(settings, worker_id="session-fixture")
    assert worker._save_session_state(
        connection_id, "elearn", owner.id, generation,
        {"cookies": [{"name": "old-session", "value": "opaque"}]},
    )
    with db.session() as session:
        for attempt in session.scalars(select(LoginAttempt).where(LoginAttempt.connection_id == connection_id)):
            attempt.state = "failed"

    changed = service.retry_connection(owner.id, "elearn")

    assert changed["connection"]["generation"] == generation + 1
    with db.session() as session:
        assert session.get(SessionState, connection_id) is None


def test_same_account_password_change_preserves_courses_and_preferences(backend):
    settings, db, owner = backend
    service, public, _job = _connection_job(settings, db, owner)
    with db.session() as session:
        connection = session.get(PlatformConnection, public["connection"]["id"])
        connection.remote_account_id = "remote-a"
        group = CourseGroup(owner_id=owner.id, display_name="課程")
        source = CourseSource(owner_id=owner.id, connection_id=connection.id, remote_course_id="course", title="保留課程")
        session.add_all((group, source)); session.flush()
        selection = CourseSelection(owner_id=owner.id, group_id=group.id, source_id=source.id, selection_state="selected")
        session.add(selection); session.flush()
        source_id, selection_id = source.id, selection.id
        original_fingerprint = connection.identity_fingerprint
    replacement = service.save_credentials(owner.id, "  ACCOUNT-A  ", "new-password")
    service.connect(owner.id, "elearn", replacement["id"])
    with db.session() as session:
        connection = session.get(PlatformConnection, public["connection"]["id"])
        assert session.get(CourseSource, source_id).title == "保留課程"
        assert session.get(CourseSelection, selection_id).selection_state == "selected"
        assert connection.remote_account_id == "remote-a"
        assert connection.identity_fingerprint == original_fingerprint


def test_legacy_namespace_is_switched_only_after_verified_remote_identity(backend):
    settings, db, owner = backend
    service, public, _job = _connection_job(settings, db, owner)
    with db.session() as session:
        connection = session.get(PlatformConnection, public["connection"]["id"])
        connection.remote_account_id = "legacy-remote"
        connection.identity_fingerprint = None
        session.get(CredentialSecret, connection.credential_id).account_fingerprint = None
        source = CourseSource(owner_id=owner.id, connection_id=connection.id, remote_course_id="legacy-course", title="舊課程")
        session.add(source); session.flush()
        source_id = source.id
    replacement = service.save_credentials(owner.id, "new-account", "new-password")
    changed = service.connect(owner.id, "elearn", replacement["id"])
    with db.session() as session:
        assert session.get(CourseSource, source_id) is not None
    # The worker is the only component that decrypts and verifies the candidate.
    loaded = service.worker_load_credentials(owner.id, replacement["id"])
    loaded.clear()
    worker = Worker(settings, worker_id="worker")
    assert worker._save_session_state(
        public["connection"]["id"], "elearn", owner.id,
        changed["connection"]["generation"], {"cookies": []}, "new-remote",
    )
    with db.session() as session:
        connection = session.get(PlatformConnection, public["connection"]["id"])
        assert session.get(CourseSource, source_id) is None
        assert connection.remote_account_id == "new-remote"
        assert connection.identity_fingerprint is not None


def test_periodic_sync_cursor_coalesces_missed_intervals(backend):
    settings, db, owner = backend
    service, public, _job = _connection_job(settings, db, owner)
    now = datetime(2026, 10, 4, 0, tzinfo=UTC)
    with db.session() as session:
        connection = session.get(PlatformConnection, public["connection"]["id"])
        connection.auth_state = "connected"
        # Remove the initial login job so periodic scheduling is observable.
        for job in session.scalars(select(SyncJob)).all():
            session.delete(job)
    worker = Worker(settings, worker_id="scheduler", clock=lambda: now)
    worker.schedule_periodic_sync()
    worker.schedule_periodic_sync()
    with db.session() as session:
        jobs = session.scalars(select(SyncJob)).all()
        assert len(jobs) == 1
        cursor = session.scalar(select(ScheduleState))
        assert cursor.next_run_at == now + timedelta(hours=1)


def test_periodic_sync_recovers_transient_terminal_state_but_not_expired_challenge(backend):
    settings, db, owner = backend
    _service, public, _job = _connection_job(settings, db, owner)
    now = datetime(2026, 10, 4, 0, tzinfo=UTC)
    with db.session() as session:
        connection = session.get(PlatformConnection, public["connection"]["id"])
        connection.auth_state = "temporarily_unavailable"
        connection.error_code = "upstream_timeout"
        for job in session.scalars(select(SyncJob)).all():
            session.delete(job)
    worker = Worker(settings, worker_id="scheduler", clock=lambda: now)
    worker.schedule_periodic_sync()
    with db.session() as session:
        recovery = session.scalar(select(SyncJob))
        assert recovery is not None and recovery.generation == public["connection"]["generation"]
        session.delete(recovery)
        connection = session.get(PlatformConnection, public["connection"]["id"])
        connection.error_code = "challenge_expired"
        cursor = session.scalar(select(ScheduleState))
        cursor.next_run_at = now
    worker.schedule_periodic_sync()
    with db.session() as session:
        assert session.scalar(select(SyncJob)) is None


def test_restart_uses_persisted_credentials_when_saved_session_expires(backend):
    settings, db, owner = backend
    service, public, _job = _connection_job(settings, db, owner)

    class Adapter:
        login_calls = 0

        async def verify_session(self, _state):
            return SimpleNamespace(status="expired")

        async def begin_login(self, account, password, **_kwargs):
            assert account == "account-a"
            assert password == "secret-a"
            self.login_calls += 1
            return SimpleNamespace(
                status="connected", storage_state={"cookies": [{"name": "renewed"}]},
                remote_account_id="remote-a", error_code=None,
            )

        async def discover_courses(self, _state):
            return FetchResult((), ("course_catalog",), True)

    initial = Worker(settings, worker_id="before-restart")
    assert initial._save_session_state(
        public["connection"]["id"], "elearn", owner.id,
        public["connection"]["generation"], {"cookies": [{"name": "expired"}]}, "remote-a",
    )
    adapter = Adapter()
    restarted = Worker(settings, adapters={"elearn": adapter}, worker_id="after-restart")
    asyncio.run(restarted.process_job(restarted.claim_job()))
    assert adapter.login_calls == 1
    connection = service.list_connections(owner.id)[0]
    assert connection["auth_state"] == "connected"
    assert connection["last_success_at"] is not None


def test_timeout_exposes_temporarily_unavailable_connection_state(backend):
    settings, db, owner = backend
    service, public, _job = _connection_job(settings, db, owner)
    worker = Worker(settings, worker_id="worker")
    claimed = worker.claim_job()
    worker._finish(claimed.id, "failed", "upstream_timeout")
    connection = service.list_connections(owner.id)[0]
    assert connection["auth_state"] == "temporarily_unavailable"
    assert connection["error_code"] == "upstream_timeout"


def test_selected_course_details_are_classified_into_separate_tables(backend):
    settings, db, owner = backend
    service, public, job_data = _connection_job(settings, db, owner)
    with db.session() as session:
        group = CourseGroup(owner_id=owner.id, display_name="課程", term="2026")
        source = CourseSource(owner_id=owner.id, connection_id=public["connection"]["id"], remote_course_id="1", title="課程")
        session.add_all((group, source)); session.flush()
        session.add(CourseSelection(owner_id=owner.id, group_id=group.id, source_id=source.id, selection_state="selected", preference_version=1))
        session.get(SyncJob, job_data["id"]).preferences_version = 1

    refs = tuple(CourseItem(remote_id=remote_id, kind=kind, title=title, url=f"https://example.test/{remote_id}") for remote_id, kind, title in (("a", "assignment", "作業"), ("n", "announcement", "公告"), ("m", "material", "教材")))

    class Adapter:
        async def verify_session(self, state): return SimpleNamespace(status="connected", remote_account_id="remote-a", error_code=None)
        async def discover_courses(self, state): return FetchResult((Course("1", "課程", "https://example.test/course", "2026"),), ("course_catalog",), True)
        async def fetch_course(self, state, remote_id): return FetchResult(refs, ("content",), True)
        async def fetch_item_detail(self, state, ref):
            assert isinstance(ref, CourseItem)
            return FetchResult((ItemDetail(ref, "公告內文" if ref.kind == "announcement" else None),), (f"{ref.kind}_detail",), True)

    worker = Worker(settings, adapters={"elearn": Adapter()}, worker_id="worker")
    worker._save_session_state(public["connection"]["id"], "elearn", owner.id, public["connection"]["generation"], {"cookies": []}, "remote-a")
    asyncio.run(worker.process_job(worker.claim_job()))
    with db.session() as session:
        assert session.scalar(select(LearningItem).where(LearningItem.remote_item_id == "a")) is not None
        assert session.scalar(select(Announcement).where(Announcement.remote_id == "n")).body_sanitized == "公告內文"
        assert session.scalar(select(CourseMaterial).where(CourseMaterial.remote_id == "m")) is not None


def test_same_official_course_code_merges_platforms_and_requires_source_choice(backend):
    settings, db, owner = backend
    service = Service(settings)
    credential = service.save_credentials(owner.id, "account", "secret")
    service.connect(owner.id, "elearn", credential["id"])
    service.connect(owner.id, "eeclass", credential["id"])
    worker = Worker(settings, worker_id="merge-worker")

    first = worker.claim_job()
    assert first is not None
    asyncio.run(worker.commit_result(first.id, {"courses": [{
        "remote_course_id": "e-1", "title": "離散數學",
        "course_code": "11510CS135601", "term": "11510",
    }]}))
    second = worker.claim_job()
    assert second is not None
    asyncio.run(worker.commit_result(second.id, {"courses": [{
        "remote_course_id": "c-1", "title": "Discrete Mathematics",
        "course_code": "11510 CS 135601", "term": "11510",
    }]}))

    courses = service.list_courses(owner.id)
    assert len(courses) == 1
    assert courses[0]["course_code"] == "11510CS135601"
    assert {source["platform"] for source in courses[0]["sources"]} == {"elearn", "eeclass"}
    assert {source["selection_state"] for source in courses[0]["sources"]} == {"pending"}
    assert courses[0]["selection_required"] is True


def test_incremental_sync_skips_recent_and_finalized_item_details(backend):
    settings, db, owner = backend
    worker = Worker(settings, worker_id="worker", clock=lambda: datetime(2026, 10, 5, 8, tzinfo=UTC))
    recent = LearningItem(
        owner_id=owner.id, source_course_id="source", remote_item_id="recent",
        kind="assignment", title="recent", remote_status="not_submitted",
        status_checked_at=datetime(2026, 10, 5, 7, 55, tzinfo=UTC),
    )
    graded = LearningItem(
        owner_id=owner.id, source_course_id="source", remote_item_id="graded",
        kind="assignment", title="graded", remote_status="graded",
        status_checked_at=datetime(2026, 10, 1, tzinfo=UTC), grade_text="95",
    )
    pending_grade = LearningItem(
        owner_id=owner.id, source_course_id="source", remote_item_id="submitted",
        kind="assignment", title="submitted", remote_status="submitted",
        status_checked_at=datetime(2026, 10, 5, 7, tzinfo=UTC),
    )
    assert worker._needs_detail_refresh(None) is True
    assert worker._needs_detail_refresh(recent) is False
    assert worker._needs_detail_refresh(graded) is False
    assert worker._needs_detail_refresh(pending_grade) is True


def test_challenge_answer_is_immediately_accepted_and_request_id_is_idempotent(backend):
    settings, db, owner = backend
    _service, public, _job = _connection_job(settings, db, owner)
    gate = asyncio.Event()

    class Adapter:
        async def answer_challenge(self, *args):
            await gate.wait()
            return SimpleNamespace(status="credentials_invalid", error_code="credentials_invalid", challenge=None, storage_state=None)

    worker = Worker(settings, adapters={"elearn": Adapter()}, worker_id="worker")
    worker._record_challenge(public["connection"]["id"], owner.id, public["connection"]["generation"], {"attempt_id": "private", "challenge": {"challenge_id": "c", "image": b"x", "expires_at": datetime.now(UTC) + timedelta(minutes=5)}})

    async def scenario():
        request = {"owner_id": owner.id, "attempt_id": public["attempt_id"], "challenge_id": "c", "answer": "1234", "request_id": "request-123"}
        first = await worker.ipc_handler("challenge.answer", request)
        second = await worker.ipc_handler("challenge.answer", request)
        assert first == second and first["status"] == "accepted"
        with pytest.raises(IPCError) as conflict:
            await worker.ipc_handler("challenge.answer", {**request, "answer": "different"})
        assert conflict.value.code == "request_conflict"
        gate.set()
        await asyncio.gather(*worker.challenge_tasks)
    asyncio.run(scenario())
    with db.session() as session:
        assert session.get(LoginAttempt, public["attempt_id"]).state == "failed"
        assert session.get(PlatformConnection, public["connection"]["id"]).auth_state == "credentials_invalid"


def test_worker_restart_invalidates_unresumable_challenge(backend):
    settings, db, owner = backend
    _service, public, job = _connection_job(settings, db, owner)
    with db.session() as session:
        attempt = session.get(LoginAttempt, public["attempt_id"])
        attempt.state = "challenge_required"
        sync = session.get(SyncJob, job["id"])
        sync.state = "waiting_user"
        connection = session.get(PlatformConnection, public["connection"]["id"])
        connection.auth_state = "challenge_required"
    Worker(settings, worker_id="restarted").invalidate_stale_challenges()
    with db.session() as session:
        assert session.get(LoginAttempt, public["attempt_id"]).state == "expired"
        assert session.get(SyncJob, job["id"]).error_code == "challenge_expired"


def test_late_challenge_result_cannot_mutate_new_generation(backend):
    settings, db, owner = backend
    _service, public, _job = _connection_job(settings, db, owner)
    gate = asyncio.Event()

    class Adapter:
        async def answer_challenge(self, *args):
            await gate.wait()
            return SimpleNamespace(status="connected", storage_state={"cookies": []}, remote_account_id="old-remote", challenge=None, error_code=None)

    worker = Worker(settings, adapters={"elearn": Adapter()}, worker_id="worker")
    worker._record_challenge(public["connection"]["id"], owner.id, public["connection"]["generation"], {"attempt_id": "private", "challenge": {"challenge_id": "c", "image": b"x", "expires_at": datetime.now(UTC) + timedelta(minutes=5)}})

    async def scenario():
        await worker.ipc_handler("challenge.answer", {"owner_id": owner.id, "attempt_id": public["attempt_id"], "challenge_id": "c", "answer": "ok", "request_id": "late-request"})
        with db.session() as session:
            connection = session.get(PlatformConnection, public["connection"]["id"])
            connection.generation += 1
            connection.auth_state = "not_connected"
            connection.remote_account_id = None
        gate.set()
        await asyncio.gather(*worker.challenge_tasks)
    asyncio.run(scenario())
    with db.session() as session:
        connection = session.get(PlatformConnection, public["connection"]["id"])
        assert connection.auth_state == "not_connected"
        assert connection.remote_account_id is None


def test_course_selection_requeues_only_its_connection(backend):
    settings, db, owner = backend
    service = Service(settings)
    first_credential = service.save_credentials(owner.id, "account", "secret")
    first = service.connect(owner.id, "elearn", first_credential["id"])
    second = service.connect(owner.id, "eeclass", first_credential["id"])
    with db.session() as session:
        first_connection = session.get(PlatformConnection, first["connection"]["id"])
        second_connection = session.get(PlatformConnection, second["connection"]["id"])
        first_connection.auth_state = second_connection.auth_state = "connected"
        group = CourseGroup(owner_id=owner.id, display_name="課程")
        source = CourseSource(owner_id=owner.id, connection_id=first_connection.id, remote_course_id="course", title="課程")
        session.add_all((group, source)); session.flush()
        selection = CourseSelection(owner_id=owner.id, group_id=group.id, source_id=source.id, selection_state="pending")
        session.add(selection); session.flush()
        group_id, source_id = group.id, source.id
    result = service.set_course_selection(owner.id, group_id, source_id, "selected", 1)
    assert result["preference_version"] == 2
    with db.session() as session:
        first_jobs = session.scalars(select(SyncJob).where(SyncJob.connection_id == first["connection"]["id"]).order_by(SyncJob.scheduled_at)).all()
        second_jobs = session.scalars(select(SyncJob).where(SyncJob.connection_id == second["connection"]["id"])).all()
        assert [job.state for job in first_jobs].count("queued") == 1
        assert any(job.state == "cancelled" for job in first_jobs)
        assert all(job.state == "queued" for job in second_jobs)


def test_expired_ipc_request_terminates_public_attempt_and_waiting_job(backend):
    settings, db, owner = backend
    _service, public, job = _connection_job(settings, db, owner)
    worker = Worker(settings, adapters={"elearn": SimpleNamespace()}, worker_id="worker", clock=lambda: datetime(2026, 10, 4, 1, tzinfo=UTC))
    worker._record_challenge(public["connection"]["id"], owner.id, public["connection"]["generation"], {"attempt_id": "private", "challenge": {"challenge_id": "expired", "image": b"x", "expires_at": datetime(2026, 10, 4, 0, tzinfo=UTC)}})
    with db.session() as session:
        session.get(SyncJob, job["id"]).state = "waiting_user"
    with pytest.raises(IPCError):
        asyncio.run(worker.ipc_handler("challenge.get", {"owner_id": owner.id, "attempt_id": public["attempt_id"], "challenge_id": "expired"}))
    with db.session() as session:
        assert session.get(LoginAttempt, public["attempt_id"]).state == "expired"
        waiting = session.get(SyncJob, job["id"])
        assert waiting.state == "failed" and waiting.error_code == "challenge_expired"


def test_transient_retry_is_bounded_with_persistent_backoff(backend):
    settings, db, owner = backend
    _service, _public, job_data = _connection_job(settings, db, owner)
    current = [datetime(2026, 10, 4, 0, tzinfo=UTC)]
    with db.session() as session:
        session.get(SyncJob, job_data["id"]).scheduled_at = current[0]
    worker = Worker(settings, worker_id="retry", clock=lambda: current[0])
    expected_delays = (1, 5, 15)
    for delay in expected_delays:
        job = worker.claim_job()
        worker._finish(job.id, "failed", "upstream_timeout")
        with db.session() as session:
            saved = session.get(SyncJob, job_data["id"])
            assert saved.state == "queued"
            assert saved.scheduled_at == current[0] + timedelta(minutes=delay)
        current[0] += timedelta(minutes=delay)
    final = worker.claim_job()
    worker._finish(final.id, "failed", "upstream_timeout")
    with db.session() as session:
        saved = session.get(SyncJob, job_data["id"])
        assert saved.state == "failed" and saved.attempts == 4


def test_no_retry_is_terminal_and_begin_login_runs_once_per_generation(backend):
    settings, db, owner = backend
    _service, public, _job = _connection_job(settings, db, owner)

    class Adapter:
        calls = 0

        async def verify_session(self, _state):
            return SimpleNamespace(status="expired")

        async def begin_login(self, *_args, **_kwargs):
            self.calls += 1
            return SimpleNamespace(status="upstream_unavailable", error_code="upstream_timeout", storage_state=None)

    adapter = Adapter()
    worker = Worker(settings, adapters={"elearn": adapter}, worker_id="acceptance", retry_delays=())
    first = worker.claim_job()
    asyncio.run(worker.process_job(first))
    with db.session() as session:
        saved = session.get(SyncJob, first.id)
        assert saved.state == "failed" and saved.error_code == "upstream_timeout"
        connection = session.get(PlatformConnection, public["connection"]["id"])
        session.add(SyncJob(
            owner_id=owner.id, connection_id=connection.id, generation=connection.generation,
            preferences_version=connection.preferences_version, scheduled_at=datetime.now(UTC),
        ))
    restarted = Worker(settings, adapters={"elearn": adapter}, worker_id="acceptance-restarted", retry_delays=())
    second = restarted.claim_job()
    asyncio.run(restarted.process_job(second))
    with db.session() as session:
        saved = session.get(SyncJob, second.id)
        assert saved.state == "failed" and saved.error_code == "session_expired"
    assert adapter.calls == 1


def test_manual_sync_only_scheduler_skips_periodic_enqueue(backend, monkeypatch):
    settings, _db, _owner = backend
    worker = Worker(settings, worker_id="acceptance", periodic_sync=False)
    calls = []
    monkeypatch.setattr(worker.reminders, "tick", lambda: calls.append("reminders"))
    monkeypatch.setattr(worker, "schedule_periodic_sync", lambda: calls.append("periodic"))

    async def expire():
        calls.append("expiry")

    monkeypatch.setattr(worker, "expire_challenges", expire)
    asyncio.run(worker.scheduler_tick())
    assert calls == ["reminders", "expiry"]


def test_valid_restored_session_updates_public_connection_and_attempt(backend):
    settings, db, owner = backend
    service, public, job_data = _connection_job(settings, db, owner)

    class Adapter:
        async def verify_session(self, state): return SimpleNamespace(status="connected", remote_account_id="remote-a", error_code=None)
        async def discover_courses(self, state): return FetchResult((), ("course_catalog",), True)

    worker = Worker(settings, adapters={"elearn": Adapter()}, worker_id="worker")
    worker._save_session_state(public["connection"]["id"], "elearn", owner.id, public["connection"]["generation"], {"cookies": []}, "remote-a")
    # Simulate connect()'s new public attempt while the saved school session remains valid.
    with db.session() as session:
        attempt = session.get(LoginAttempt, public["attempt_id"])
        attempt.state = "queued"
        connection = session.get(PlatformConnection, public["connection"]["id"])
        connection.auth_state = "authenticating"
    asyncio.run(worker.process_job(worker.claim_job()))
    connection = service.list_connections(owner.id)[0]
    assert connection["auth_state"] == "connected"
    assert connection["last_verified_at"] is not None
    assert service.get_login_attempt(owner.id, public["attempt_id"])["state"] == "succeeded"


def test_identity_mismatch_terminates_waiting_job(backend):
    settings, db, owner = backend
    _service, public, job = _connection_job(settings, db, owner)

    class Adapter:
        async def answer_challenge(self, *args):
            return SimpleNamespace(status="connected", storage_state={"cookies": []}, remote_account_id="different", challenge=None, error_code=None)

    worker = Worker(settings, adapters={"elearn": Adapter()}, worker_id="worker")
    with db.session() as session:
        connection = session.get(PlatformConnection, public["connection"]["id"])
        connection.remote_account_id = "expected"
        session.get(SyncJob, job["id"]).state = "waiting_user"
    worker._record_challenge(public["connection"]["id"], owner.id, public["connection"]["generation"], {"attempt_id": "private", "challenge": {"challenge_id": "c", "image": b"x", "expires_at": datetime.now(UTC) + timedelta(minutes=5)}})

    async def scenario():
        await worker.ipc_handler("challenge.answer", {"owner_id": owner.id, "attempt_id": public["attempt_id"], "challenge_id": "c", "answer": "x", "request_id": "identity-request"})
        await asyncio.gather(*worker.challenge_tasks)
    asyncio.run(scenario())
    with db.session() as session:
        waiting = session.get(SyncJob, job["id"])
        assert waiting.state == "failed" and waiting.error_code == "identity_mismatch"
