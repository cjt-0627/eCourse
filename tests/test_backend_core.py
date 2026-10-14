from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from ecourse.auth.service import initialize_owner
from ecourse.config import Settings
from ecourse.db import Database
from ecourse.db.models import NotificationEvent, PlatformConnection, SyncJob
from ecourse.services import GoneError, NotFoundError, Service
from ecourse.services.reminders import ReminderEngine
from ecourse.vault import initialize_key
from ecourse.worker import Worker


@pytest.fixture
def backend(tmp_path):
    data, secrets = tmp_path / "data", tmp_path / "secrets"
    initialize_key(secrets, data)
    settings = Settings(data, secrets)
    db = Database(settings)
    db.initialize()
    owner = initialize_owner(settings, "owner", "a-long-local-password")
    return settings, db, owner


def test_migration_ready_and_utc_roundtrip(backend):
    settings, db, owner = backend
    assert db.ready()
    with db.session() as session:
        loaded = session.get(type(owner), owner.id)
        assert loaded.created_at.tzinfo is UTC


def test_manual_events_work_without_school_connection_and_cancel_reminder(backend):
    settings, db, owner = backend
    service = Service(settings)
    starts = datetime.now(UTC) + timedelta(days=3)
    event = service.create_manual_event(owner.id, {"title": "期中考", "kind": "exam", "date_precision": "exact", "starts_at": starts.isoformat()})
    assert service.get_manual_event(owner.id, event["id"])["starts_at"] == starts.isoformat()
    ReminderEngine(settings).schedule_reminders(datetime.now(UTC))
    with db.session() as session:
        reminder = session.scalar(select(NotificationEvent).where(NotificationEvent.entity_id == event["id"]))
        assert reminder.state == "pending"
    assert service.delete_manual_event(owner.id, event["id"])["deleted"]
    with pytest.raises(NotFoundError):
        service.get_manual_event(owner.id, event["id"])
    with db.session() as session:
        assert session.scalar(select(NotificationEvent).where(NotificationEvent.entity_id == event["id"])).state == "cancelled"


def test_credentials_are_encrypted_and_generation_invalidates_old_work(backend):
    settings, db, owner = backend
    service = Service(settings)
    credential = service.save_credentials(owner.id, "student-id", "school-password")
    attempt = service.connect(owner.id, "elearn", credential["id"])
    jobs = service.enqueue_sync(owner.id, "elearn")
    assert len(jobs) == 1
    assert b"school-password" not in settings.data_dir.joinpath("ecourse.sqlite3").read_bytes()
    replacement = service.save_credentials(owner.id, "student-id", "new-school-password")
    with db.session() as session:
        connection = session.scalar(select(PlatformConnection).where(PlatformConnection.owner_id == owner.id))
        old_job = session.get(SyncJob, jobs[0]["id"])
        assert connection.generation == attempt["connection"]["generation"]
        assert old_job.state == "queued"
    service.connect(owner.id, "elearn", replacement["id"])
    with db.session() as session:
        connection = session.scalar(select(PlatformConnection).where(PlatformConnection.owner_id == owner.id))
        old_job = session.get(SyncJob, jobs[0]["id"])
        assert connection.generation == attempt["connection"]["generation"] + 1
        assert old_job.state == "cancelled" and old_job.error_code == "stale_generation"
    with pytest.raises(GoneError):
        service.get_login_attempt(owner.id, attempt["attempt_id"])
    assert service.worker_load_credentials(owner.id, replacement["id"])["password"] == "new-school-password"


def test_notification_dedupe_and_owner_isolation(backend):
    settings, _db, owner = backend
    service = Service(settings)
    first = service.create_notification(owner.id, dedupe_key="digest:one", type="daily", title="摘要", body="內容")
    second = service.create_notification(owner.id, dedupe_key="digest:one", type="daily", title="另一個", body="不應建立")
    assert first["id"] == second["id"]
    assert len(service.list_notifications(owner.id)) == 1
    with pytest.raises(NotFoundError):
        service.mark_notification_read("00000000-0000-0000-0000-000000000000", first["id"])


def test_atomic_claim_and_generation_guard(backend):
    settings, db, owner = backend
    service = Service(settings)
    credential = service.save_credentials(owner.id, "student-id", "school-password")
    service.connect(owner.id, "elearn", credential["id"])
    queued = service.enqueue_sync(owner.id, "elearn")[0]
    first, second = Worker(settings, worker_id="one"), Worker(settings, worker_id="two")
    claimed = first.claim_job()
    assert claimed.id == queued["id"]
    assert second.claim_job() is None
    with db.session() as session:
        connection = session.scalar(select(PlatformConnection).where(PlatformConnection.owner_id == owner.id))
        connection.generation += 1
    import asyncio
    assert asyncio.run(first.commit_result(claimed.id, {"courses": [], "complete": True})) is False
    assert service.get_sync_job(owner.id, claimed.id)["error_code"] == "stale_generation"
