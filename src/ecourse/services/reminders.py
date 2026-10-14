from __future__ import annotations

import json
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import and_, select

from ecourse.db import Database
from ecourse.db import models as m


DONE_STATUSES = {"submitted", "graded", "completed"}


def _zone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError:
        return ZoneInfo("Asia/Taipei")


class ReminderEngine:
    """Persistent in-app reminders and digest scheduler; no browser or network dependency."""

    def __init__(self, settings, *, clock=None):
        self.db = Database(settings)
        self.clock = clock or (lambda: datetime.now(UTC))

    def tick(self) -> dict:
        now = self.clock()
        created = self.schedule_reminders(now)
        digests = self.generate_due_digests(now)
        delivered = self.dispatch_due(now)
        return {"scheduled": created, "digests": digests, "delivered": delivered}

    def _event(self, session, *, owner_id, dedupe_key, kind, entity_id, revision, scheduled_at, expires_at=None) -> bool:
        if session.scalar(select(m.NotificationEvent.id).where(m.NotificationEvent.owner_id == owner_id, m.NotificationEvent.dedupe_key == dedupe_key)):
            return False
        session.add(m.NotificationEvent(owner_id=owner_id, dedupe_key=dedupe_key, type=kind, entity_id=entity_id, entity_revision=revision, scheduled_at=scheduled_at, expires_at=expires_at))
        return True

    def schedule_reminders(self, now: datetime) -> int:
        count = 0
        with self.db.session() as session:
            items = session.execute(select(m.LearningItem, m.ItemPreference).join(m.CourseSource, m.CourseSource.id == m.LearningItem.source_course_id).join(m.CourseSelection, m.CourseSelection.source_id == m.CourseSource.id).join(m.PlatformConnection, m.PlatformConnection.id == m.CourseSource.connection_id).outerjoin(m.ItemPreference, and_(m.ItemPreference.item_id == m.LearningItem.id, m.ItemPreference.owner_id == m.LearningItem.owner_id)).where(m.LearningItem.archived_at.is_(None), ~m.LearningItem.remote_status.in_(DONE_STATUSES), m.CourseSelection.selection_state == "selected", m.PlatformConnection.auth_state == "connected")).all()
            owners = {x.id: x for x in session.scalars(select(m.Owner)).all()}
            rules = self._rules(session)
            for item, preference in items:
                if preference and (preference.handled_at or (preference.muted_until and preference.muted_until > now)):
                    continue
                owner = owners.get(item.owner_id)
                if not owner:
                    continue
                rule = rules.get((item.owner_id, "deadline"))
                if rule and not rule.enabled:
                    continue
                offsets = json.loads(rule.offsets) if rule else [24, 3]
                effective_at = item.due_at or (item.closes_at if item.kind == "quiz" else None)
                points = self._points("exact" if effective_at and item.due_at is None else item.date_precision, effective_at, item.due_date, _zone(owner.timezone), offsets=offsets)
                points = self._relevant_points(points, now)
                expires = effective_at or (datetime.combine(item.due_date + timedelta(days=1), time.min, _zone(owner.timezone)).astimezone(UTC) if item.due_date else None)
                for rule, scheduled in points:
                    key = f"item:{item.id}:deadline:{item.deadline_revision}:{rule}"
                    count += self._event(session, owner_id=item.owner_id, dedupe_key=key, kind="item_due", entity_id=item.id, revision=item.deadline_revision, scheduled_at=scheduled, expires_at=expires)
            events = session.scalars(select(m.ManualEvent)).all()
            for event in events:
                owner = owners.get(event.owner_id)
                if not owner or event.handled_at:
                    continue
                rule = rules.get((event.owner_id, "exam")) or rules.get((event.owner_id, "deadline"))
                if rule and not rule.enabled:
                    continue
                offsets = json.loads(rule.offsets) if rule else [24, 1]
                revision = event.revision
                points = self._relevant_points(self._points(event.date_precision, event.starts_at, event.event_date, _zone(owner.timezone), offsets=offsets), now)
                expires = event.starts_at or (datetime.combine(event.event_date + timedelta(days=1), time.min, _zone(owner.timezone)).astimezone(UTC) if event.event_date else None)
                for rule, scheduled in points:
                    key = f"manual:{event.id}:{revision}:{rule}"
                    count += self._event(session, owner_id=event.owner_id, dedupe_key=key, kind="manual_event_due", entity_id=event.id, revision=revision, scheduled_at=scheduled, expires_at=expires)
        return count

    @staticmethod
    def _points(precision, exact, day, zone, *, offsets):
        if precision == "exact" and exact:
            return [(f"{hours}h", exact - timedelta(hours=hours)) for hours in sorted(set(offsets), reverse=True)]
        if precision == "date_only" and day:
            first = datetime.combine(day - timedelta(days=1), time(18), zone).astimezone(UTC)
            second = datetime.combine(day, time(8), zone).astimezone(UTC)
            return [("date-previous-18", first), ("date-day-08", second)]
        return []

    @staticmethod
    def _relevant_points(points, now):
        future = [(key, value) for key, value in points if value > now]
        passed = [(key, value) for key, value in points if value <= now]
        # A subject first discovered after reminder points produces at most one
        # catch-up reminder instead of replaying every historical offset.
        return future + ([max(passed, key=lambda pair: pair[1])] if passed else [])

    def generate_due_digests(self, now: datetime) -> int:
        made = 0
        with self.db.session() as session:
            rules = self._rules(session)
            for owner in session.scalars(select(m.Owner)).all():
                local = now.astimezone(_zone(owner.timezone))
                daily_rule = rules.get((owner.id, "daily"))
                daily_clock = self._local_time(daily_rule.local_time if daily_rule else None, time(8))
                daily_target = datetime.combine(local.date(), daily_clock, local.tzinfo)
                if daily_target > local:
                    daily_target -= timedelta(days=1)
                if (daily_rule is None or daily_rule.enabled) and daily_target <= local < daily_target + timedelta(hours=6):
                    digest_day = daily_target.date()
                    made += self._digest(session, owner, "daily", daily_target.astimezone(UTC), digest_day, digest_day + timedelta(days=2))
                weekly_rule = rules.get((owner.id, "weekly"))
                weekday = weekly_rule.weekday if weekly_rule and weekly_rule.weekday is not None else 6
                weekly_clock = self._local_time(weekly_rule.local_time if weekly_rule else None, time(20))
                summary_day = local.date() - timedelta(days=(local.weekday() - weekday) % 7)
                weekly_target = datetime.combine(summary_day, weekly_clock, local.tzinfo)
                if weekly_target > local:
                    weekly_target -= timedelta(days=7)
                    summary_day -= timedelta(days=7)
                if (weekly_rule is None or weekly_rule.enabled) and weekly_target <= local < weekly_target + timedelta(hours=6):
                    days_to_monday = (7 - summary_day.weekday()) % 7 or 7
                    monday = summary_day + timedelta(days=days_to_monday)
                    made += self._digest(session, owner, "weekly", weekly_target.astimezone(UTC), monday, monday + timedelta(days=7))
        return made

    @staticmethod
    def _local_time(value, default):
        if not value:
            return default
        hour, minute = map(int, value.split(":"))
        return time(hour, minute)

    @staticmethod
    def _rules(session):
        return {(rule.owner_id, rule.kind): rule for rule in session.scalars(select(m.ReminderRule)).all()}

    def _digest(self, session, owner, kind, target, period_start: date, period_end: date) -> int:
        key = f"digest:{kind}:{owner.timezone}:{period_start.isoformat()}:{period_end.isoformat()}"
        if session.scalar(select(m.NotificationEvent.id).where(m.NotificationEvent.owner_id == owner.id, m.NotificationEvent.dedupe_key == key)):
            return 0
        zone = _zone(owner.timezone)
        start_utc = datetime.combine(period_start, time.min, zone).astimezone(UTC)
        end_utc = datetime.combine(period_end, time.min, zone).astimezone(UTC)
        item_rows = session.execute(select(m.LearningItem, m.ItemPreference).outerjoin(m.ItemPreference, and_(m.ItemPreference.item_id == m.LearningItem.id, m.ItemPreference.owner_id == owner.id)).where(m.LearningItem.owner_id == owner.id, m.LearningItem.archived_at.is_(None), ~m.LearningItem.remote_status.in_(DONE_STATUSES))).all()
        entries = []
        for item, pref in item_rows:
            if pref and pref.handled_at:
                continue
            effective_at = item.due_at or (item.closes_at if item.kind == "quiz" else None)
            occurs = effective_at and start_utc <= effective_at < end_utc
            occurs = occurs or (item.due_date and period_start <= item.due_date < period_end)
            if occurs:
                entries.append({"id": item.id, "kind": item.kind, "title": item.title, "due_at": item.due_at.isoformat() if item.due_at else None, "due_date": item.due_date.isoformat() if item.due_date else None, "closes_at": item.closes_at.isoformat() if item.closes_at else None, "date_precision": item.date_precision, "remote_status": item.remote_status})
        manuals = session.scalars(select(m.ManualEvent).where(m.ManualEvent.owner_id == owner.id, m.ManualEvent.handled_at.is_(None))).all()
        for event in manuals:
            occurs = event.starts_at and start_utc <= event.starts_at < end_utc
            occurs = occurs or (event.event_date and period_start <= event.event_date < period_end)
            if occurs:
                entries.append({"id": event.id, "kind": event.kind, "title": event.title, "starts_at": event.starts_at.isoformat() if event.starts_at else None, "event_date": event.event_date.isoformat() if event.event_date else None, "date_precision": event.date_precision, "manual": True})
        snapshot = m.DigestSnapshot(owner_id=owner.id, type=kind, period_start=start_utc, period_end=end_utc, generated_at=self.clock(), content=json.dumps({"entries": entries, "count": len(entries)}, ensure_ascii=False), freshness_summary=json.dumps({"status": "cached_data"}))
        session.add(snapshot); session.flush()
        self._event(session, owner_id=owner.id, dedupe_key=key, kind=f"{kind}_digest", entity_id=snapshot.id, revision=1, scheduled_at=target, expires_at=target + timedelta(hours=6))
        schedule_key = f"digest:{kind}"
        cursor = session.scalar(select(m.ScheduleState).where(m.ScheduleState.owner_id == owner.id, m.ScheduleState.schedule_key == schedule_key))
        if cursor is None:
            cursor = m.ScheduleState(owner_id=owner.id, schedule_key=schedule_key); session.add(cursor)
        cursor.last_evaluated_at, cursor.last_period_end = self.clock(), end_utc
        return 1

    def dispatch_due(self, now: datetime) -> int:
        count = 0
        with self.db.session() as session:
            due = session.scalars(select(m.NotificationEvent).where(m.NotificationEvent.state == "pending", m.NotificationEvent.scheduled_at <= now).order_by(m.NotificationEvent.scheduled_at)).all()
            for event in due:
                if event.expires_at and now >= event.expires_at:
                    event.state = "cancelled"
                    continue
                title = body = path = None
                if event.type == "item_due":
                    item = session.scalar(select(m.LearningItem).join(m.CourseSource, m.CourseSource.id == m.LearningItem.source_course_id).join(m.CourseSelection, m.CourseSelection.source_id == m.CourseSource.id).join(m.PlatformConnection, m.PlatformConnection.id == m.CourseSource.connection_id).where(m.LearningItem.id == event.entity_id, m.LearningItem.owner_id == event.owner_id, m.CourseSelection.selection_state == "selected", m.PlatformConnection.auth_state == "connected"))
                    pref = session.scalar(select(m.ItemPreference).where(m.ItemPreference.owner_id == event.owner_id, m.ItemPreference.item_id == event.entity_id))
                    if not item or item.archived_at or item.remote_status in DONE_STATUSES or (pref and (pref.handled_at or (pref.muted_until and pref.muted_until > now))) or item.deadline_revision != event.entity_revision:
                        event.state = "cancelled"; continue
                    title, body, path = f"課務提醒：{item.title}", "期限即將到達；完成狀態請以最近同步資料為準。", f"/todos?item={item.id}"
                elif event.type == "manual_event_due":
                    manual = session.scalar(select(m.ManualEvent).where(m.ManualEvent.id == event.entity_id, m.ManualEvent.owner_id == event.owner_id))
                    if not manual or manual.handled_at or manual.revision != event.entity_revision:
                        event.state = "cancelled"; continue
                    title, body, path = f"事件提醒：{manual.title}", "這是你建立的手動事件。", f"/todos?manual={manual.id}"
                elif event.type.endswith("_digest"):
                    snapshot = session.scalar(select(m.DigestSnapshot).where(m.DigestSnapshot.id == event.entity_id, m.DigestSnapshot.owner_id == event.owner_id))
                    if not snapshot:
                        event.state = "cancelled"; continue
                    content = json.loads(snapshot.content)
                    label = "每日" if event.type == "daily_digest" else "每週"
                    title, body, path = f"{label}摘要", f"共有 {content['count']} 項課務與事件。", f"/digests/{snapshot.id}"
                if title:
                    if not session.scalar(select(m.Notification.id).where(m.Notification.owner_id == event.owner_id, m.Notification.event_id == event.id)):
                        session.add(m.Notification(owner_id=event.owner_id, event_id=event.id, title=title, body=body, internal_path=path))
                    event.state = "processed"
                    count += 1
        return count
