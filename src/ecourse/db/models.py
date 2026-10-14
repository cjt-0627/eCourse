from __future__ import annotations

import uuid
from datetime import date, datetime, timezone

from sqlalchemy import Boolean, Date, DateTime as SADateTime, ForeignKey, Index, Integer, LargeBinary, String, Text, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.types import TypeDecorator


def new_id() -> str:
    return str(uuid.uuid4())


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class DateTime(TypeDecorator):
    """Persist UTC in SQLite and always return aware UTC datetimes."""
    impl = SADateTime
    cache_ok = True

    def __init__(self, timezone=True, **kwargs):
        super().__init__(timezone=False, **kwargs)

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError("datetime must include timezone")
        return value.astimezone(timezone.utc).replace(tzinfo=None) if dialect.name == "sqlite" else value.astimezone(timezone.utc)

    def process_result_value(self, value, dialect):
        if value is None:
            return None
        return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


class Base(DeclarativeBase):
    pass


class Owner(Base):
    __tablename__ = "owners"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    login_name: Mapped[str] = mapped_column(String(160), unique=True, nullable=False)
    password_hash: Mapped[str | None] = mapped_column(Text)
    timezone: Mapped[str] = mapped_column(String(64), default="Asia/Taipei", nullable=False)
    locale: Mapped[str] = mapped_column(String(16), default="zh-Hant", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class AppSession(Base):
    __tablename__ = "app_sessions"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    owner_id: Mapped[str] = mapped_column(ForeignKey("owners.id", ondelete="CASCADE"), index=True)
    token_hash: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    csrf_token: Mapped[str] = mapped_column(String(128), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    last_used_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True, nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    device_label: Mapped[str | None] = mapped_column(String(160))


class SchoolLoginExchange(Base):
    __tablename__ = "school_login_exchanges"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    owner_id: Mapped[str] = mapped_column(ForeignKey("owners.id", ondelete="CASCADE"), index=True)
    attempt_id: Mapped[str] = mapped_column(ForeignKey("login_attempts.id", ondelete="CASCADE"), unique=True)
    credential_id: Mapped[str] = mapped_column(ForeignKey("credential_secrets.id", ondelete="CASCADE"))
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True, nullable=False)
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    device_label: Mapped[str | None] = mapped_column(String(160))


class CredentialSecret(Base):
    __tablename__ = "credential_secrets"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    owner_id: Mapped[str] = mapped_column(ForeignKey("owners.id", ondelete="CASCADE"), index=True)
    encrypted_payload: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    account_hint: Mapped[str] = mapped_column(String(32), nullable=False)
    account_fingerprint: Mapped[str | None] = mapped_column(String(64), index=True)
    key_version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class PlatformConnection(Base):
    __tablename__ = "platform_connections"
    __table_args__ = (UniqueConstraint("owner_id", "platform"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    owner_id: Mapped[str] = mapped_column(ForeignKey("owners.id", ondelete="CASCADE"), index=True)
    platform: Mapped[str] = mapped_column(String(32), nullable=False)
    credential_id: Mapped[str | None] = mapped_column(ForeignKey("credential_secrets.id", ondelete="SET NULL"))
    remote_account_id: Mapped[str | None] = mapped_column(String(255))
    identity_fingerprint: Mapped[str | None] = mapped_column(String(128))
    auth_state: Mapped[str] = mapped_column(String(32), default="not_connected", nullable=False)
    generation: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    preferences_version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    last_verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error_code: Mapped[str | None] = mapped_column(String(64))


class SessionState(Base):
    __tablename__ = "session_states"
    connection_id: Mapped[str] = mapped_column(ForeignKey("platform_connections.id", ondelete="CASCADE"), primary_key=True)
    encrypted_state: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    key_version: Mapped[int] = mapped_column(Integer, default=1)
    state_version: Mapped[int] = mapped_column(Integer, default=1)
    saved_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    observed_expiry: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class LoginAttempt(Base):
    __tablename__ = "login_attempts"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    owner_id: Mapped[str] = mapped_column(ForeignKey("owners.id", ondelete="CASCADE"), index=True)
    connection_id: Mapped[str] = mapped_column(ForeignKey("platform_connections.id", ondelete="CASCADE"), index=True)
    generation: Mapped[int] = mapped_column(Integer, nullable=False)
    state: Mapped[str] = mapped_column(String(32), default="queued")
    challenge_id: Mapped[str | None] = mapped_column(String(64))
    challenge_type: Mapped[str | None] = mapped_column(String(32))
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    attempt_count: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class CourseGroup(Base):
    __tablename__ = "course_groups"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    owner_id: Mapped[str] = mapped_column(ForeignKey("owners.id", ondelete="CASCADE"), index=True)
    term: Mapped[str | None] = mapped_column(String(64))
    course_code: Mapped[str | None] = mapped_column(String(128), index=True)
    display_name: Mapped[str] = mapped_column(String(300), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)


class CourseSource(Base):
    __tablename__ = "course_sources"
    __table_args__ = (UniqueConstraint("connection_id", "remote_course_id"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    owner_id: Mapped[str] = mapped_column(ForeignKey("owners.id", ondelete="CASCADE"), index=True)
    connection_id: Mapped[str] = mapped_column(ForeignKey("platform_connections.id", ondelete="CASCADE"), index=True)
    remote_course_id: Mapped[str] = mapped_column(String(255))
    course_code: Mapped[str | None] = mapped_column(String(128))
    term: Mapped[str | None] = mapped_column(String(64))
    title: Mapped[str] = mapped_column(String(300))
    source_url: Mapped[str | None] = mapped_column(Text)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class CourseSelection(Base):
    __tablename__ = "course_selections"
    __table_args__ = (UniqueConstraint("group_id", "source_id"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    owner_id: Mapped[str] = mapped_column(ForeignKey("owners.id", ondelete="CASCADE"), index=True)
    group_id: Mapped[str] = mapped_column(ForeignKey("course_groups.id", ondelete="CASCADE"))
    source_id: Mapped[str] = mapped_column(ForeignKey("course_sources.id", ondelete="CASCADE"))
    selection_state: Mapped[str] = mapped_column(String(32), default="pending")
    preference_version: Mapped[int] = mapped_column(Integer, default=1)


class LearningItem(Base):
    __tablename__ = "learning_items"
    __table_args__ = (UniqueConstraint("source_course_id", "kind", "remote_item_id"), Index("ix_items_owner_due", "owner_id", "due_at"))
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    owner_id: Mapped[str] = mapped_column(ForeignKey("owners.id", ondelete="CASCADE"), index=True)
    source_course_id: Mapped[str] = mapped_column(ForeignKey("course_sources.id", ondelete="CASCADE"), index=True)
    remote_item_id: Mapped[str] = mapped_column(String(255))
    kind: Mapped[str] = mapped_column(String(32))
    title: Mapped[str] = mapped_column(String(500))
    body_sanitized: Mapped[str | None] = mapped_column(Text)
    source_url: Mapped[str | None] = mapped_column(Text)
    remote_status: Mapped[str] = mapped_column(String(32), default="unknown")
    remote_status_text: Mapped[str | None] = mapped_column(Text)
    grade_text: Mapped[str | None] = mapped_column(Text)
    grading_status_text: Mapped[str | None] = mapped_column(Text)
    status_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    due_date: Mapped[date | None] = mapped_column(Date)
    date_precision: Mapped[str] = mapped_column(String(16), default="unknown")
    source_timezone: Mapped[str | None] = mapped_column(String(64))
    date_original: Mapped[str | None] = mapped_column(Text)
    opens_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    starts_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    closes_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    duration_minutes: Mapped[int | None] = mapped_column(Integer)
    deadline_revision: Mapped[int] = mapped_column(Integer, default=1)
    content_hash: Mapped[str | None] = mapped_column(String(128))
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Announcement(Base):
    __tablename__ = "announcements"
    __table_args__ = (UniqueConstraint("source_course_id", "remote_id"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    owner_id: Mapped[str] = mapped_column(ForeignKey("owners.id", ondelete="CASCADE"), index=True)
    source_course_id: Mapped[str] = mapped_column(ForeignKey("course_sources.id", ondelete="CASCADE"), index=True)
    remote_id: Mapped[str] = mapped_column(String(255))
    title: Mapped[str] = mapped_column(String(500))
    body_sanitized: Mapped[str | None] = mapped_column(Text)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    source_updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    source_url: Mapped[str | None] = mapped_column(Text)
    content_hash: Mapped[str | None] = mapped_column(String(128))
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class CourseMaterial(Base):
    __tablename__ = "course_materials"
    __table_args__ = (UniqueConstraint("source_course_id", "remote_id"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    owner_id: Mapped[str] = mapped_column(ForeignKey("owners.id", ondelete="CASCADE"), index=True)
    source_course_id: Mapped[str] = mapped_column(ForeignKey("course_sources.id", ondelete="CASCADE"))
    remote_id: Mapped[str] = mapped_column(String(255))
    section_title: Mapped[str | None] = mapped_column(String(300))
    kind: Mapped[str | None] = mapped_column(String(64))
    title: Mapped[str] = mapped_column(String(500))
    source_url: Mapped[str | None] = mapped_column(Text)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ManualEvent(Base):
    __tablename__ = "manual_events"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    owner_id: Mapped[str] = mapped_column(ForeignKey("owners.id", ondelete="CASCADE"), index=True)
    group_id: Mapped[str | None] = mapped_column(ForeignKey("course_groups.id", ondelete="SET NULL"))
    kind: Mapped[str] = mapped_column(String(32), default="exam")
    title: Mapped[str] = mapped_column(String(500))
    starts_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    event_date: Mapped[date | None] = mapped_column(Date, index=True)
    date_precision: Mapped[str] = mapped_column(String(16), default="unknown")
    source_timezone: Mapped[str | None] = mapped_column(String(64))
    date_original: Mapped[str | None] = mapped_column(Text)
    source_announcement_id: Mapped[str | None] = mapped_column(ForeignKey("announcements.id", ondelete="SET NULL"))
    source_url: Mapped[str | None] = mapped_column(Text)
    confirmed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    handled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revision: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class ItemPreference(Base):
    __tablename__ = "item_preferences"
    __table_args__ = (UniqueConstraint("owner_id", "item_id"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    owner_id: Mapped[str] = mapped_column(ForeignKey("owners.id", ondelete="CASCADE"), index=True)
    item_id: Mapped[str] = mapped_column(ForeignKey("learning_items.id", ondelete="CASCADE"))
    handled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    muted_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AnnouncementRead(Base):
    __tablename__ = "announcement_reads"
    __table_args__ = (UniqueConstraint("owner_id", "announcement_id"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    owner_id: Mapped[str] = mapped_column(ForeignKey("owners.id", ondelete="CASCADE"), index=True)
    announcement_id: Mapped[str] = mapped_column(ForeignKey("announcements.id", ondelete="CASCADE"))
    read_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    read_content_hash: Mapped[str | None] = mapped_column(String(128))


class SyncJob(Base):
    __tablename__ = "sync_jobs"
    __table_args__ = (Index("ix_jobs_claim", "state", "scheduled_at", "lease_until"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    owner_id: Mapped[str] = mapped_column(ForeignKey("owners.id", ondelete="CASCADE"), index=True)
    connection_id: Mapped[str] = mapped_column(ForeignKey("platform_connections.id", ondelete="CASCADE"), index=True)
    generation: Mapped[int] = mapped_column(Integer)
    preferences_version: Mapped[int] = mapped_column(Integer, default=1)
    state: Mapped[str] = mapped_column(String(24), default="queued")
    scheduled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    lease_owner: Mapped[str | None] = mapped_column(String(128))
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error_code: Mapped[str | None] = mapped_column(String(64))
    coverage: Mapped[str | None] = mapped_column(Text)


class SyncScope(Base):
    __tablename__ = "sync_scopes"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    job_id: Mapped[str] = mapped_column(ForeignKey("sync_jobs.id", ondelete="CASCADE"), index=True)
    course_source_id: Mapped[str | None] = mapped_column(ForeignKey("course_sources.id", ondelete="CASCADE"))
    scope_type: Mapped[str] = mapped_column(String(64))
    complete: Mapped[bool] = mapped_column(Boolean, default=False)
    fetched_count: Mapped[int] = mapped_column(Integer, default=0)
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    warnings: Mapped[str | None] = mapped_column(Text)


class ScheduleState(Base):
    __tablename__ = "schedule_states"
    __table_args__ = (UniqueConstraint("owner_id", "schedule_key"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    owner_id: Mapped[str] = mapped_column(ForeignKey("owners.id", ondelete="CASCADE"), index=True)
    schedule_key: Mapped[str] = mapped_column(String(128))
    next_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_evaluated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_period_end: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    configuration_version: Mapped[int] = mapped_column(Integer, default=1)


class ItemChange(Base):
    __tablename__ = "item_changes"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    owner_id: Mapped[str] = mapped_column(ForeignKey("owners.id", ondelete="CASCADE"), index=True)
    item_id: Mapped[str] = mapped_column(ForeignKey("learning_items.id", ondelete="CASCADE"), index=True)
    change_type: Mapped[str] = mapped_column(String(64))
    old_value: Mapped[str | None] = mapped_column(Text)
    new_value: Mapped[str | None] = mapped_column(Text)
    revision: Mapped[int] = mapped_column(Integer)
    detected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ReminderRule(Base):
    __tablename__ = "reminder_rules"
    __table_args__ = (UniqueConstraint("owner_id", "kind"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    owner_id: Mapped[str] = mapped_column(ForeignKey("owners.id", ondelete="CASCADE"), index=True)
    kind: Mapped[str] = mapped_column(String(32))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    offsets: Mapped[str] = mapped_column(Text, default="[]")
    local_time: Mapped[str | None] = mapped_column(String(8))
    weekday: Mapped[int | None] = mapped_column(Integer)
    quiet_start: Mapped[str] = mapped_column(String(8), default="23:00")
    quiet_end: Mapped[str] = mapped_column(String(8), default="07:00")
    privacy_mode: Mapped[str] = mapped_column(String(32), default="counts")


class DigestSnapshot(Base):
    __tablename__ = "digest_snapshots"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    owner_id: Mapped[str] = mapped_column(ForeignKey("owners.id", ondelete="CASCADE"), index=True)
    type: Mapped[str] = mapped_column(String(32))
    period_start: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    period_end: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    generated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    content: Mapped[str] = mapped_column(Text)
    freshness_summary: Mapped[str] = mapped_column(Text, default="{}")


class NotificationEvent(Base):
    __tablename__ = "notification_events"
    __table_args__ = (UniqueConstraint("owner_id", "dedupe_key"), Index("ix_events_due", "state", "scheduled_at"))
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    owner_id: Mapped[str] = mapped_column(ForeignKey("owners.id", ondelete="CASCADE"), index=True)
    dedupe_key: Mapped[str] = mapped_column(String(500))
    type: Mapped[str] = mapped_column(String(64))
    entity_id: Mapped[str | None] = mapped_column(String(36))
    entity_revision: Mapped[int | None] = mapped_column(Integer)
    scheduled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    state: Mapped[str] = mapped_column(String(24), default="pending")


class Notification(Base):
    __tablename__ = "notifications"
    __table_args__ = (UniqueConstraint("owner_id", "event_id"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    owner_id: Mapped[str] = mapped_column(ForeignKey("owners.id", ondelete="CASCADE"), index=True)
    event_id: Mapped[str] = mapped_column(ForeignKey("notification_events.id", ondelete="CASCADE"))
    title: Mapped[str] = mapped_column(String(300))
    body: Mapped[str] = mapped_column(Text)
    internal_path: Mapped[str] = mapped_column(String(500), default="/notifications")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    read_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class PushSubscription(Base):
    __tablename__ = "push_subscriptions"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    owner_id: Mapped[str] = mapped_column(ForeignKey("owners.id", ondelete="CASCADE"), index=True)
    device_id: Mapped[str] = mapped_column(String(128))
    encrypted_endpoint: Mapped[bytes] = mapped_column(LargeBinary)
    endpoint_hash: Mapped[str] = mapped_column(String(128), unique=True)
    encrypted_keys: Mapped[bytes] = mapped_column(LargeBinary)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    invalidated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class PushDelivery(Base):
    __tablename__ = "push_deliveries"
    __table_args__ = (UniqueConstraint("event_id", "subscription_id"), Index("ix_deliveries_due", "state", "next_attempt_at"))
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    event_id: Mapped[str] = mapped_column(ForeignKey("notification_events.id", ondelete="CASCADE"), index=True)
    subscription_id: Mapped[str] = mapped_column(ForeignKey("push_subscriptions.id", ondelete="CASCADE"), index=True)
    state: Mapped[str] = mapped_column(String(24), default="pending")
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    next_attempt_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    accepted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    display_reported_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    clicked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error_code: Mapped[str | None] = mapped_column(String(64))


class NotificationDedupe(Base):
    __tablename__ = "notification_dedupe"
    __table_args__ = (UniqueConstraint("owner_id", "dedupe_key"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    owner_id: Mapped[str] = mapped_column(ForeignKey("owners.id", ondelete="CASCADE"), index=True)
    dedupe_key: Mapped[str] = mapped_column(String(500))
    processed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    retain_until: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class WorkerHeartbeat(Base):
    __tablename__ = "worker_heartbeats"
    worker_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    version: Mapped[str] = mapped_column(String(64))
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    active_job_count: Mapped[int] = mapped_column(Integer, default=0)


class AuditEvent(Base):
    __tablename__ = "audit_events"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    owner_id: Mapped[str | None] = mapped_column(ForeignKey("owners.id", ondelete="SET NULL"), index=True)
    actor: Mapped[str] = mapped_column(String(64))
    action: Mapped[str] = mapped_column(String(128))
    entity_type: Mapped[str] = mapped_column(String(64))
    entity_id: Mapped[str | None] = mapped_column(String(64))
    result: Mapped[str] = mapped_column(String(32))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
