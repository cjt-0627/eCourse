from datetime import UTC, date, datetime, timedelta

from sqlalchemy import select

from ecourse.db.models import CourseGroup, CourseSelection, CourseSource, DigestSnapshot, LearningItem, ManualEvent, Notification, NotificationEvent, PlatformConnection
from ecourse.services import Service
from ecourse.services.reminders import ReminderEngine


def test_exact_manual_reminder_is_delivered_and_deduped(backend):
    settings, db, owner = backend
    due = datetime(2026, 10, 7, 12, tzinfo=UTC)
    service = Service(settings, clock=lambda: due - timedelta(days=2))
    event = service.create_manual_event(owner.id, title="離線也要提醒", date_precision="exact", starts_at=due)
    engine = ReminderEngine(settings, clock=lambda: due - timedelta(hours=24))
    first = engine.tick()
    second = engine.tick()
    assert first == {"scheduled": 2, "digests": 0, "delivered": 1}
    assert second == {"scheduled": 0, "digests": 0, "delivered": 0}
    notes = service.list_notifications(owner.id)
    assert len(notes) == 1 and notes[0]["title"] == "事件提醒：離線也要提醒"


def test_date_only_uses_owner_local_18_and_08_without_invented_deadline(backend):
    settings, db, owner = backend
    Service(settings).create_manual_event(owner.id, title="只有日期", date_precision="date_only", event_date="2026-10-06")
    engine = ReminderEngine(settings, clock=lambda: datetime(2026, 10, 5, 10, tzinfo=UTC))  # Taipei 18:00
    assert engine.tick()["delivered"] == 1
    with db.session() as session:
        event = session.scalar(select(ManualEvent).where(ManualEvent.title == "只有日期"))
        assert event.starts_at is None
        schedules = session.scalars(select(NotificationEvent).where(NotificationEvent.entity_id == event.id).order_by(NotificationEvent.scheduled_at)).all()
        assert [x.scheduled_at for x in schedules] == [datetime(2026, 10, 5, 10, tzinfo=UTC), datetime(2026, 10, 6, 0, tzinfo=UTC)]


def test_handled_or_deleted_subject_cancels_pending_delivery(backend):
    settings, db, owner = backend
    due = datetime(2026, 10, 5, 12, tzinfo=UTC)
    service = Service(settings)
    event = service.create_manual_event(owner.id, title="會刪除", date_precision="exact", starts_at=due)
    engine = ReminderEngine(settings, clock=lambda: due - timedelta(days=2))
    engine.schedule_reminders(due - timedelta(days=2))
    service.delete_manual_event(owner.id, event["id"])
    assert ReminderEngine(settings, clock=lambda: due - timedelta(hours=24)).dispatch_due(due - timedelta(hours=24)) == 0
    assert service.list_notifications(owner.id) == []


def test_daily_and_weekly_digest_persist_snapshot_and_do_not_replay(backend):
    settings, db, owner = backend
    daily_now = datetime(2026, 10, 4, 0, tzinfo=UTC)  # 08:00 Taipei
    engine = ReminderEngine(settings, clock=lambda: daily_now)
    assert engine.tick()["digests"] == 1
    weekly_now = datetime(2026, 10, 4, 12, tzinfo=UTC)  # Sunday 20:00 Taipei
    weekly = ReminderEngine(settings, clock=lambda: weekly_now)
    assert weekly.tick()["digests"] == 1
    assert weekly.tick()["digests"] == 0
    with db.session() as session:
        snapshots = session.scalars(select(DigestSnapshot).where(DigestSnapshot.owner_id == owner.id)).all()
        assert {x.type for x in snapshots} == {"daily", "weekly"}
        assert len(session.scalars(select(Notification).where(Notification.owner_id == owner.id)).all()) == 2
        digest_id = snapshots[0].id
    digest = Service(settings).get_digest(owner.id, digest_id)
    assert digest["type"] in {"daily", "weekly"}
    assert digest["period_start"] and digest["entries"] == []


def test_quiet_hours_do_not_suppress_in_app_notification(backend):
    settings, _db, owner = backend
    # In-app notices are created during quiet hours; only external push is delayed.
    due = datetime(2026, 10, 5, 16, tzinfo=UTC)  # Taipei midnight
    Service(settings).create_manual_event(owner.id, title="午夜事件", date_precision="exact", starts_at=due + timedelta(hours=1))
    engine = ReminderEngine(settings, clock=lambda: due)
    engine.schedule_reminders(due)
    assert engine.dispatch_due(due) == 1


def test_saved_rules_control_enabled_time_weekday_and_offsets(backend):
    settings, db, owner = backend
    service = Service(settings)
    service.update_reminder_settings(owner.id, {"rules": [
        {"kind": "daily", "enabled": False, "local_time": "09:00"},
        {"kind": "weekly", "enabled": True, "weekday": 6, "local_time": "20:00"},
        {"kind": "deadline", "enabled": True, "offsets": [2]},
    ]})
    due = datetime(2026, 10, 6, 3, tzinfo=UTC)
    event = service.create_manual_event(owner.id, title="自訂提前量", date_precision="exact", starts_at=due)
    engine = ReminderEngine(settings, clock=lambda: due - timedelta(hours=2))
    engine.schedule_reminders(due - timedelta(hours=2))
    with db.session() as session:
        reminders = session.scalars(select(NotificationEvent).where(NotificationEvent.entity_id == event["id"])).all()
        assert len(reminders) == 1
        assert reminders[0].dedupe_key.endswith(":2h")
    # Monday 00:00 Taipei is four hours after the configured Sunday summary.
    monday_midnight = datetime(2026, 10, 4, 16, tzinfo=UTC)
    result = ReminderEngine(settings, clock=lambda: monday_midnight).tick()
    assert result["digests"] == 1
    with db.session() as session:
        assert {x.type for x in session.scalars(select(DigestSnapshot)).all()} == {"weekly"}


def test_manual_handled_toggle_and_revision_do_not_reuse_dedupe_key(backend):
    settings, db, owner = backend
    frozen = datetime(2026, 10, 4, 0, tzinfo=UTC)
    service = Service(settings, clock=lambda: frozen)
    event = service.create_manual_event(owner.id, title="可處理", date_precision="exact", starts_at=frozen + timedelta(days=2))
    engine = ReminderEngine(settings, clock=lambda: frozen)
    engine.schedule_reminders(frozen)
    first_revision = event["revision"]
    handled = service.set_manual_event_preference(owner.id, event["id"], handled=True)
    assert handled["handled_at"] == frozen.isoformat()
    assert engine.schedule_reminders(frozen) == 0
    restored = service.set_manual_event_preference(owner.id, event["id"], handled=False)
    assert restored["revision"] == first_revision
    assert engine.schedule_reminders(frozen) == 0
    updated = service.update_manual_event(owner.id, event["id"], title="同秒更新")
    assert updated["revision"] == restored["revision"]
    assert engine.dispatch_due(frozen) == 0


def test_title_edit_keeps_pending_identity_and_uses_latest_title(backend):
    settings, db, owner = backend
    now = datetime(2026, 10, 4, 7, tzinfo=UTC)
    service = Service(settings, clock=lambda: now)
    event = service.create_manual_event(owner.id, title="舊標題", date_precision="exact", starts_at=now + timedelta(hours=48))
    engine = ReminderEngine(settings, clock=lambda: now)
    engine.schedule_reminders(now)
    changed = service.update_manual_event(owner.id, event["id"], title="新標題")
    assert changed["revision"] == event["revision"]
    due_tick = ReminderEngine(settings, clock=lambda: now + timedelta(hours=24))
    assert due_tick.dispatch_due(now + timedelta(hours=24)) == 1
    notices = service.list_notifications(owner.id)
    assert len(notices) == 1 and notices[0]["title"] == "事件提醒：新標題"


def test_handled_roundtrip_never_replays_already_delivered_manual_reminder(backend):
    settings, db, owner = backend
    now = datetime(2026, 10, 4, 7, tzinfo=UTC)
    service = Service(settings, clock=lambda: now)
    event = service.create_manual_event(owner.id, title="只提醒一次", date_precision="exact", starts_at=now + timedelta(hours=24))
    engine = ReminderEngine(settings, clock=lambda: now)
    assert engine.tick()["delivered"] == 1
    service.set_manual_event_preference(owner.id, event["id"], handled=True)
    service.set_manual_event_preference(owner.id, event["id"], handled=False)
    # An unrelated daily digest setting change must not alter deadline identity.
    service.update_reminder_settings(owner.id, {"rules": [{"kind": "daily", "enabled": True, "local_time": "07:30"}, {"kind": "deadline", "enabled": True, "offsets": [24, 3]}]})
    assert engine.tick()["delivered"] == 0
    with db.session() as session:
        reminders = session.scalars(select(Notification).join(NotificationEvent, Notification.event_id == NotificationEvent.id).where(NotificationEvent.entity_id == event["id"], NotificationEvent.type == "manual_event_due")).all()
        assert len(reminders) == 1


def test_real_deadline_change_creates_new_manual_reminder_identity(backend):
    settings, db, owner = backend
    now = datetime(2026, 10, 4, 7, tzinfo=UTC)
    service = Service(settings, clock=lambda: now)
    event = service.create_manual_event(owner.id, title="期限會變", date_precision="exact", starts_at=now + timedelta(hours=24))
    engine = ReminderEngine(settings, clock=lambda: now)
    assert engine.tick()["delivered"] == 1
    changed = service.update_manual_event(owner.id, event["id"], starts_at=(now + timedelta(hours=48)).isoformat())
    assert changed["revision"] == event["revision"] + 1
    later = ReminderEngine(settings, clock=lambda: now + timedelta(hours=24))
    assert later.tick()["delivered"] == 1


def test_disabled_or_disconnected_course_cannot_deliver_deadline_reminder(backend):
    settings, db, owner = backend
    now = datetime(2026, 10, 4, 0, tzinfo=UTC)
    with db.session() as session:
        connection = PlatformConnection(owner_id=owner.id, platform="elearn", auth_state="connected")
        group = CourseGroup(owner_id=owner.id, display_name="課程")
        session.add_all((connection, group)); session.flush()
        source = CourseSource(owner_id=owner.id, connection_id=connection.id, remote_course_id="course", title="課程")
        session.add(source); session.flush()
        selection = CourseSelection(owner_id=owner.id, group_id=group.id, source_id=source.id, selection_state="selected")
        item = LearningItem(owner_id=owner.id, source_course_id=source.id, remote_item_id="item", kind="assignment", title="截止作業", due_at=now + timedelta(hours=24), date_precision="exact")
        session.add_all((selection, item)); session.flush()
        group_id, source_id, item_id = group.id, source.id, item.id
    engine = ReminderEngine(settings, clock=lambda: now)
    assert engine.schedule_reminders(now) == 2
    Service(settings, clock=lambda: now).set_course_selection(owner.id, group_id, source_id, "disabled", 1)
    assert engine.dispatch_due(now) == 0
    with db.session() as session:
        assert session.scalar(select(NotificationEvent).where(NotificationEvent.entity_id == item_id, NotificationEvent.scheduled_at == now)).state == "cancelled"
