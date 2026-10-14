from __future__ import annotations

import asyncio
import fcntl
import hashlib
import json
import logging
import os
import socket
import secrets
import signal
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit

from sqlalchemy import func, or_, select, update

from ecourse.db import Database
from ecourse.db import models as m
from ecourse.services import Service
from ecourse.services.reminders import ReminderEngine
from ecourse.vault import Vault
from ecourse.ipc import IPCError, WorkerIPCServer, ipc_socket_path
from ecourse.parsing.html import normalize_course_code
from ecourse.platforms import managed_platform_url
from ecourse.adapters.base import CourseItem


PERIODIC_SYNC_INTERVAL = timedelta(hours=1)
JOB_TIMEOUT_SECONDS = 10 * 60


def _plain(value):
    if value is None or isinstance(value, (str, int, float, bool, bytes, datetime)):
        return value
    if isinstance(value, dict):
        return value
    if hasattr(value, "to_dict"):
        return value.to_dict()
    try:
        from dataclasses import asdict, is_dataclass
        if is_dataclass(value):
            return asdict(value)
    except TypeError:
        pass
    return vars(value)


class Worker:
    def __init__(self, settings, *, adapters=None, adapter_loader=None, worker_id=None, clock=None,
                 version="0.1.0", retry_delays=(1, 5, 15), periodic_sync=True,
                 platform_opener=None, job_timeout_seconds=JOB_TIMEOUT_SECONDS):
        self.settings = settings
        self.db = Database(settings)
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.service = Service(settings, clock=self.clock)
        self.adapters = adapters or {}
        self.adapter_loader = adapter_loader
        self.worker_id = worker_id or f"{socket.gethostname()}:{os.getpid()}"
        self.version = version
        self.retry_delays = tuple(retry_delays)
        self.periodic_sync = periodic_sync
        self.platform_opener = platform_opener
        self.job_timeout_seconds = job_timeout_seconds
        self.reminders = ReminderEngine(settings, clock=self.clock)
        self.stop_event = asyncio.Event()
        self.challenges = {}
        self.challenge_requests = {}
        self.challenge_tasks = set()
        self.ipc_server = None
        self._last_reminder_tick = None

    def heartbeat(self, active_job_count=0):
        with self.db.session() as session:
            heartbeat = session.get(m.WorkerHeartbeat, self.worker_id)
            if heartbeat is None:
                heartbeat = m.WorkerHeartbeat(worker_id=self.worker_id, version=self.version)
                session.add(heartbeat)
            heartbeat.last_seen_at = self.clock()
            heartbeat.active_job_count = active_job_count

    def invalidate_stale_challenges(self):
        """A restarted worker cannot resume browser contexts kept by its predecessor."""
        with self.db.session() as session:
            attempts = session.scalars(select(m.LoginAttempt).where(m.LoginAttempt.state.in_(("challenge_required", "authenticating")))).all()
            for attempt in attempts:
                waiting = session.scalar(select(m.SyncJob.id).where(m.SyncJob.connection_id == attempt.connection_id, m.SyncJob.generation == attempt.generation, m.SyncJob.state == "waiting_user"))
                if not waiting and attempt.state == "authenticating":
                    continue
                attempt.state = "expired"
                connection = session.get(m.PlatformConnection, attempt.connection_id)
                if connection and connection.generation == attempt.generation:
                    connection.auth_state, connection.error_code = "temporarily_unavailable", "challenge_expired"
                session.execute(update(m.SyncJob).where(m.SyncJob.connection_id == attempt.connection_id, m.SyncJob.generation == attempt.generation, m.SyncJob.state == "waiting_user").values(state="failed", error_code="challenge_expired", finished_at=self.clock(), lease_owner=None, lease_until=None))

    async def expire_challenges(self):
        now = self.clock()
        expired_ids = [attempt_id for attempt_id, challenge in self.challenges.items() if challenge["expires_at"] <= now]
        with self.db.session() as session:
            for attempt_id, challenge in self.challenges.items():
                connection = session.get(m.PlatformConnection, challenge["connection_id"])
                if not connection or connection.generation != challenge["generation"]:
                    expired_ids.append(attempt_id)
        expired_ids = list(set(expired_ids))
        if not expired_ids:
            return
        for attempt_id in expired_ids:
            challenge = self.challenges.get(attempt_id)
            adapter = self.adapters.get(challenge.get("platform")) if challenge else None
            discard = getattr(adapter, "discard_attempt", None) or getattr(adapter, "_discard_attempt", None)
            if discard and challenge:
                await discard(challenge["adapter_attempt_id"])
        for adapter in self.adapters.values():
            expire = getattr(adapter, "expire_attempts", None)
            if expire:
                await expire()
        with self.db.session() as session:
            for attempt_id in expired_ids:
                challenge = self.challenges.pop(attempt_id, None)
                attempt = session.get(m.LoginAttempt, attempt_id)
                if attempt:
                    attempt.state = "expired"
                if challenge:
                    connection = session.get(m.PlatformConnection, challenge["connection_id"])
                    if connection and connection.generation == challenge["generation"]:
                        connection.auth_state, connection.error_code = "temporarily_unavailable", "challenge_expired"
                    session.execute(update(m.SyncJob).where(m.SyncJob.connection_id == challenge["connection_id"], m.SyncJob.generation == challenge["generation"], m.SyncJob.state == "waiting_user").values(state="failed", error_code="challenge_expired", finished_at=now, lease_owner=None, lease_until=None))
        for request_key, (_payload_hash, response) in list(self.challenge_requests.items()):
            if response.get("attempt_id") in expired_ids:
                self.challenge_requests.pop(request_key, None)

    def claim_job(self, lease_seconds=90):
        now, until = self.clock(), self.clock() + timedelta(seconds=lease_seconds)
        with self.db.session() as session:
            candidate = session.scalar(select(m.SyncJob.id).where(m.SyncJob.state.in_(("queued", "running")), m.SyncJob.scheduled_at <= now, or_(m.SyncJob.lease_until.is_(None), m.SyncJob.lease_until < now)).order_by(m.SyncJob.scheduled_at).limit(1))
            if not candidate:
                return None
            statement = update(m.SyncJob).where(m.SyncJob.id == candidate, or_(m.SyncJob.lease_until.is_(None), m.SyncJob.lease_until < now)).values(state="running", lease_owner=self.worker_id, lease_until=until, started_at=now, attempts=m.SyncJob.attempts + 1).returning(m.SyncJob)
            return session.execute(statement).scalar_one_or_none()

    def renew_lease(self, job_id, lease_seconds=90) -> bool:
        with self.db.session() as session:
            result = session.execute(update(m.SyncJob).where(m.SyncJob.id == job_id, m.SyncJob.state == "running", m.SyncJob.lease_owner == self.worker_id).values(lease_until=self.clock() + timedelta(seconds=lease_seconds)))
            return result.rowcount == 1

    @staticmethod
    def _preferences_version(session, connection_id):
        connection = session.get(m.PlatformConnection, connection_id)
        return connection.preferences_version if connection else -1

    def schedule_periodic_sync(self):
        """Persist one hourly cursor per usable platform; missed periods coalesce."""
        now = self.clock()
        recoverable_errors = (
            "upstream_timeout", "upstream_rate_limited", "upstream_unavailable",
            "adapter_unavailable", "vault_unavailable", "job_timeout",
        )
        with self.db.session() as session:
            recoverable = or_(
                m.PlatformConnection.auth_state == "connected",
                (m.PlatformConnection.auth_state == "temporarily_unavailable")
                & m.PlatformConnection.error_code.in_(recoverable_errors),
            )
            for connection in session.scalars(select(m.PlatformConnection).where(
                recoverable, m.PlatformConnection.credential_id.is_not(None),
            )).all():
                key = f"sync:{connection.id}"
                cursor = session.scalar(select(m.ScheduleState).where(m.ScheduleState.owner_id == connection.owner_id, m.ScheduleState.schedule_key == key))
                if cursor is None:
                    cursor = m.ScheduleState(owner_id=connection.owner_id, schedule_key=key, next_run_at=now)
                    session.add(cursor)
                if cursor.next_run_at is None or cursor.next_run_at <= now:
                    active = session.scalar(select(m.SyncJob.id).where(m.SyncJob.connection_id == connection.id, m.SyncJob.generation == connection.generation, m.SyncJob.state.in_(("queued", "running", "waiting_user"))))
                    if not active:
                        session.add(m.SyncJob(owner_id=connection.owner_id, connection_id=connection.id, generation=connection.generation, preferences_version=connection.preferences_version, scheduled_at=now))
                    cursor.last_evaluated_at = now
                    cursor.next_run_at = now + PERIODIC_SYNC_INTERVAL

    async def process_job(self, job):
        with self.db.session() as session:
            current = session.get(m.SyncJob, job.id)
            connection = session.get(m.PlatformConnection, current.connection_id) if current else None
            if not current or not connection or current.generation != connection.generation:
                if current:
                    current.state, current.error_code, current.finished_at = "cancelled", "stale_generation", self.clock()
                return
            platform, owner_id, credential_id = connection.platform, connection.owner_id, connection.credential_id
        adapter = self.adapters.get(platform)
        if adapter is None and self.adapter_loader:
            try:
                loaded = await self.adapter_loader()
                self.adapters.update(loaded or {})
                adapter = self.adapters.get(platform)
            except Exception:
                adapter = None
        if adapter is None:
            self._finish(job.id, "failed", "adapter_unavailable")
            return
        try:
            storage_state = self._load_session_state(connection.id, platform, owner_id)
            verified = _plain(await adapter.verify_session(storage_state)) if storage_state else {"status": "expired"}
            verify_status = str(verified.get("status"))
            verify_error = verified.get("error_code")
            if verify_status in {"connected", "valid", "ok"}:
                verified_remote = verified.get("remote_account_id")
                if connection.remote_account_id and verified_remote != connection.remote_account_id:
                    self._finish(job.id, "failed", "identity_mismatch")
                    return
                refreshed_state = verified.get("storage_state")
                if refreshed_state:
                    if not self._save_session_state(
                        connection.id, platform, owner_id, job.generation,
                        refreshed_state, verified_remote,
                    ):
                        self._finish(job.id, "failed", "identity_mismatch")
                        return
                    storage_state = refreshed_state
                else:
                    self._mark_connection_verified(connection.id, job.generation, verified_remote)
            elif verify_status not in {"expired", "session_expired"} and verify_error != "protected_page_not_reached":
                self._finish(job.id, "failed", verify_error or verify_status or "upstream_unavailable")
                return
            else:
                if self.challenges:
                    with self.db.session() as session:
                        deferred = session.get(m.SyncJob, job.id)
                        if deferred:
                            deferred.state, deferred.scheduled_at = "queued", self.clock() + timedelta(minutes=1)
                            deferred.lease_owner = deferred.lease_until = None
                    return
                if not self.retry_delays and not self._reserve_single_login(connection.id, owner_id, job.generation):
                    self._finish(job.id, "failed", "session_expired")
                    return
                credentials = self.service.worker_load_credentials(owner_id, credential_id)
                try:
                    login = _plain(await adapter.begin_login(credentials["account"], credentials["password"], storage_state=storage_state, connection_generation=job.generation))
                finally:
                    credentials.clear()
                status = login.get("status")
                if status == "challenge_required":
                    self._record_challenge(connection.id, owner_id, job.generation, login)
                    self._finish(job.id, "waiting_user", "captcha_required")
                    return
                if status not in {"connected", "success", "ok"} or not login.get("storage_state"):
                    self._finish(job.id, "failed", login.get("error_code", "credentials_invalid"))
                    return
                if connection.remote_account_id and login.get("remote_account_id") != connection.remote_account_id and connection.identity_fingerprint is not None:
                    self._finish(job.id, "failed", "identity_mismatch")
                    return
                storage_state = login["storage_state"]
                if not self._save_session_state(connection.id, platform, owner_id, job.generation, storage_state, login.get("remote_account_id")):
                    self._finish(job.id, "failed", "identity_mismatch")
                    return
            discovered = _plain(await adapter.discover_courses(storage_state))
            courses = discovered.get("records", discovered.get("courses", discovered.get("data", [])))
            normalized = []
            for value in courses:
                course = _plain(value)
                normalized.append({"remote_course_id": course.get("remote_course_id", course.get("remote_id")), "title": course["title"], "term": course.get("term", course.get("semester")), "course_code": course.get("course_code"), "source_url": course.get("source_url", course.get("url"))})
            payload = {"courses": normalized, "coverage": discovered.get("coverage", ()), "complete": discovered.get("complete", True)}
            if not await self.commit_result(job.id, payload, finalize=False):
                return
            content_complete = await self._fetch_selected(job.id, adapter, storage_state)
            with self.db.session() as session:
                current = session.get(m.SyncJob, job.id)
                current_connection = session.get(m.PlatformConnection, current.connection_id) if current else None
                if current and current.state == "running":
                    if discovered.get("complete", True) and content_complete:
                        current.state, current.finished_at, current.lease_until = "succeeded", self.clock(), None
                        if current_connection and current_connection.generation == current.generation:
                            current_connection.last_success_at = self.clock()
                    else:
                        current.state, current.error_code, current.finished_at, current.lease_until = "partial", "partial_fetch", self.clock(), None
        except Exception as exc:
            logging.exception("platform sync failed during adapter fetch")
            code = getattr(exc, "code", "upstream_unavailable")
            self._finish(job.id, "failed", code)

    def _reserve_single_login(self, connection_id, owner_id, generation) -> bool:
        """Persist the no-retry login budget across worker restarts."""
        with self.db.session() as session:
            connection = session.get(m.PlatformConnection, connection_id)
            if not connection or connection.generation != generation:
                return False
            attempt = session.scalar(select(m.LoginAttempt).where(
                m.LoginAttempt.connection_id == connection_id,
                m.LoginAttempt.generation == generation,
            ).order_by(m.LoginAttempt.created_at.desc()))
            if attempt is None:
                attempt = m.LoginAttempt(
                    owner_id=owner_id, connection_id=connection_id,
                    generation=generation, state="authenticating",
                )
                session.add(attempt)
                session.flush()
            if attempt.attempt_count >= 1:
                return False
            attempt.attempt_count += 1
            return True

    async def _fetch_selected(self, job_id, adapter, storage_state):
        with self.db.session() as session:
            job = session.get(m.SyncJob, job_id)
            connection = session.get(m.PlatformConnection, job.connection_id) if job else None
            if not job or not connection or job.generation != connection.generation:
                return
            sources = session.scalars(select(m.CourseSource).join(m.CourseSelection, m.CourseSelection.source_id == m.CourseSource.id).where(m.CourseSource.connection_id == connection.id, m.CourseSelection.selection_state == "selected")).all()
            source_refs = [(x.id, x.remote_course_id) for x in sources]
            fast_refresh = connection.last_success_at is not None

        course_semaphore = asyncio.Semaphore(4)

        async def scan_source(source_id, remote_id):
            try:
                async with course_semaphore:
                    operation = adapter.fetch_course(storage_state, remote_id)
                    raw_fetched = (
                        await asyncio.wait_for(operation, timeout=6)
                        if fast_refresh else await operation
                    )
                return source_id, raw_fetched
            except TimeoutError:
                return source_id, {
                    "records": (), "complete": False,
                    "warnings": ("course_refresh_timeout",),
                }
            except Exception as exc:
                logging.exception("course scan failed during incremental sync")
                return source_id, {
                    "records": (), "complete": False,
                    "warnings": (getattr(exc, "code", "course_refresh_failed"),),
                }

        scanned_sources = await asyncio.gather(
            *(scan_source(source_id, remote_id) for source_id, remote_id in source_refs)
        )
        prepared = []
        complete = True
        for source_id, raw_fetched in scanned_sources:
            fetched = _plain(raw_fetched)
            raw_records = raw_fetched.get("records", ()) if isinstance(raw_fetched, dict) else getattr(raw_fetched, "records", ())
            source_complete = fetched.get("complete", False)
            complete = complete and source_complete
            warnings = list(fetched.get("warnings", ()))
            basic_details = [
                {"item": _plain(item_ref), "content_text": None, "published_at": None}
                for item_ref in raw_records
            ]
            if basic_details:
                await self._commit_content(
                    job_id, source_id, basic_details,
                    complete=False, warnings=[*warnings, "detail_enrichment_pending"],
                )
            with self.db.session() as session:
                cached_items = {
                    (item.kind, item.remote_item_id): item
                    for item in session.scalars(
                        select(m.LearningItem).where(m.LearningItem.source_course_id == source_id)
                    ).all()
                }
                cached_announcements = {
                    item.remote_id: item
                    for item in session.scalars(
                        select(m.Announcement).where(m.Announcement.source_course_id == source_id)
                    ).all()
                }
            existing_refs = {
                (getattr(item, "kind", None), str(getattr(item, "remote_id", "")))
                for item in raw_records
            }
            raw_records = tuple(raw_records) + tuple(
                CourseItem(
                    remote_id=item.remote_id, kind="announcement", title=item.title,
                    url=item.source_url or "",
                )
                for item in cached_announcements.values()
                if not item.body_sanitized and ("announcement", item.remote_id) not in existing_refs
                and item.source_url and item.title.strip() not in {"公告", "Announcement"}
            )
            prepared.append({
                "source_id": source_id, "records": raw_records,
                "cached": cached_items, "cached_announcements": cached_announcements,
                "complete": source_complete,
                "warnings": warnings, "details": [],
            })

        detail_semaphore = asyncio.Semaphore(8)

        async def fetch_detail(source, item_ref):
            item_kind = getattr(item_ref, "kind", None)
            if item_kind == "material":
                return source, None
            if not hasattr(adapter, "fetch_item_detail"):
                return source, {"records": ({"item": _plain(item_ref), "content_text": None, "published_at": None},), "complete": True, "warnings": ()}
            if item_kind == "announcement":
                cached_announcement = source["cached_announcements"].get(str(getattr(item_ref, "remote_id", "")))
                if not self._needs_announcement_refresh(cached_announcement):
                    return source, None
            cached = source["cached"].get((item_kind, str(getattr(item_ref, "remote_id", ""))))
            if item_kind != "announcement" and not self._needs_detail_refresh(cached):
                return source, None
            try:
                async with detail_semaphore:
                    raw_detail = await asyncio.wait_for(
                        adapter.fetch_item_detail(storage_state, item_ref),
                        timeout=12 if item_kind == "announcement" else (3 if fast_refresh else 8),
                    )
                return source, _plain(raw_detail)
            except TimeoutError:
                return source, {"records": (), "complete": False, "warnings": ("item_detail_timeout",)}
            except Exception as exc:
                logging.exception("item detail failed during incremental sync")
                return source, {"records": (), "complete": False, "warnings": (getattr(exc, "code", "item_detail_failed"),)}

        detail_results = await asyncio.gather(*(
            fetch_detail(source, item_ref)
            for source in prepared
            for item_ref in source["records"]
        ))
        for source, detail_result in detail_results:
            if detail_result is None:
                continue
            source["complete"] = source["complete"] and detail_result.get("complete", False)
            complete = complete and detail_result.get("complete", False)
            source["warnings"].extend(detail_result.get("warnings", ()))
            source["details"].extend(_plain(value) for value in detail_result.get("records", ()))

        for source in prepared:
            await self._commit_content(
                job_id, source["source_id"], source["details"],
                complete=source["complete"], warnings=source["warnings"],
                detail_pass=True,
            )
        if source_refs and not complete:
            self._finish(job_id, "partial", "partial_fetch")
        return complete

    def _needs_announcement_refresh(self, announcement) -> bool:
        if announcement is None or not announcement.body_sanitized or announcement.source_updated_at is None:
            return True
        checked_at = announcement.source_updated_at
        if checked_at.tzinfo is None:
            checked_at = checked_at.replace(tzinfo=timezone.utc)
        return checked_at < self.clock() - timedelta(hours=24)

    def _needs_detail_refresh(self, item) -> bool:
        """Keep repeat syncs incremental while open work and pending grades stay fresh."""
        if item is None or item.status_checked_at is None:
            return True
        now = self.clock()
        if now - item.status_checked_at < timedelta(minutes=10):
            return False
        if item.remote_status in {"graded", "completed", "passed"}:
            return False
        # Submitted work may receive a grade later, so continue checking it
        # until a grade is observed.
        if item.remote_status == "submitted":
            return not bool(item.grade_text)
        deadline = item.due_at or item.closes_at
        if deadline and deadline < now - timedelta(days=14):
            return False
        if item.due_date and item.due_date < now.date() - timedelta(days=14):
            return False
        return True

    async def _commit_content(self, job_id, source_id, records, *, complete, warnings, detail_pass=False):
        with self.db.session() as session:
            job = session.get(m.SyncJob, job_id)
            source = session.get(m.CourseSource, source_id)
            connection = session.get(m.PlatformConnection, job.connection_id) if job else None
            if not job or job.state != "running" or not source or not connection or job.generation != connection.generation:
                return False
            if job.preferences_version != self._preferences_version(session, connection.id):
                job.state, job.error_code, job.finished_at = "cancelled", "stale_generation", self.clock()
                return False
            selection = session.scalar(select(m.CourseSelection).where(m.CourseSelection.source_id == source_id, m.CourseSelection.selection_state == "selected"))
            if not selection:
                return False
            for detail in records:
                detail = _plain(detail)
                value = _plain(detail.get("item", detail))
                remote_id = str(value.get("remote_item_id", value.get("remote_id")))
                kind = value.get("kind", "assignment")
                if kind == "announcement":
                    announcement = session.scalar(select(m.Announcement).where(m.Announcement.source_course_id == source_id, m.Announcement.remote_id == remote_id))
                    if announcement is None:
                        announcement = m.Announcement(owner_id=connection.owner_id, source_course_id=source_id, remote_id=remote_id, title=value["title"])
                        session.add(announcement)
                    announcement.title = value["title"]
                    announcement.source_url = value.get("source_url", value.get("url"))
                    announcement.body_sanitized = detail.get("content_text")
                    announcement.published_at = detail.get("published_at")
                    if detail.get("content_text"):
                        announcement.source_updated_at = self.clock()
                    announcement.last_seen_at = self.clock()
                    continue
                if kind == "material":
                    material = session.scalar(select(m.CourseMaterial).where(m.CourseMaterial.source_course_id == source_id, m.CourseMaterial.remote_id == remote_id))
                    if material is None:
                        material = m.CourseMaterial(owner_id=connection.owner_id, source_course_id=source_id, remote_id=remote_id, title=value["title"])
                        session.add(material)
                    material.title = value["title"]
                    material.source_url = value.get("source_url", value.get("url"))
                    material.last_seen_at = self.clock()
                    continue
                if kind not in {"assignment", "quiz"}:
                    continue
                item = session.scalar(select(m.LearningItem).where(m.LearningItem.source_course_id == source_id, m.LearningItem.kind == kind, m.LearningItem.remote_item_id == remote_id))
                due_at, due_date, closes_at = value.get("due_at"), value.get("due_date"), value.get("closes_at")
                if item is None:
                    item = m.LearningItem(owner_id=connection.owner_id, source_course_id=source_id, remote_item_id=remote_id, kind=kind, title=value["title"])
                    session.add(item)
                elif (item.due_at, item.due_date, item.closes_at) != (due_at, due_date, closes_at):
                    item.deadline_revision += 1
                item.title = value["title"]
                item.source_url = value.get("source_url", value.get("url"))
                enriched = detail_pass or any((
                    value.get("status_evidence"), value.get("remote_status_text"),
                    value.get("grade_text"), due_at, due_date, closes_at,
                ))
                if enriched:
                    item.remote_status = value.get("remote_status", "unknown")
                    item.remote_status_text = value.get("remote_status_text")
                    item.grade_text = value.get("grade_text")
                    item.grading_status_text = value.get("grading_status_text")
                    item.status_checked_at = self.clock()
                    item.due_at, item.due_date = due_at, due_date
                    item.closes_at = closes_at
                    item.date_precision = str(value.get("date_precision", "unknown"))
                    item.date_original = value.get("date_original", value.get("source_date_text")) or value.get("closes_source_text")
                item.last_seen_at = self.clock()
            session.add(m.SyncScope(job_id=job_id, course_source_id=source_id, scope_type="course_content", complete=complete, fetched_count=len(records), last_success_at=self.clock() if complete else None, warnings=json.dumps(warnings, ensure_ascii=False)))
            return True

    def _load_session_state(self, connection_id, platform, owner_id):
        with self.db.session() as session:
            state = session.get(m.SessionState, connection_id)
            envelope = state.encrypted_state if state else None
        return Vault(self.settings.secret_dir).unseal(envelope, platform=platform, account_id=owner_id, purpose="session") if envelope else None

    def _save_session_state(self, connection_id, platform, owner_id, generation, storage_state, remote_account_id=None):
        envelope = Vault(self.settings.secret_dir).seal(storage_state, platform=platform, account_id=owner_id, purpose="session")
        with self.db.session() as session:
            connection = session.get(m.PlatformConnection, connection_id)
            if not connection or connection.generation != generation:
                return False
            credential = session.get(m.CredentialSecret, connection.credential_id) if connection.credential_id else None
            credential_fingerprint = credential.account_fingerprint if credential else None
            if connection.remote_account_id and remote_account_id and remote_account_id != connection.remote_account_id:
                if connection.identity_fingerprint is not None:
                    return False
                # Legacy rows have no blind index. Only after the replacement
                # account is verified may the old namespace be removed.
                Service._clear_remote_namespace(session, owner_id, connection)
            state = session.get(m.SessionState, connection_id)
            if state is None:
                state = m.SessionState(connection_id=connection_id, encrypted_state=envelope)
                session.add(state)
            else:
                state.encrypted_state, state.state_version = envelope, state.state_version + 1
            state.saved_at = self.clock()
            connection.remote_account_id = remote_account_id or connection.remote_account_id
            if credential_fingerprint:
                connection.identity_fingerprint = credential_fingerprint
            connection.auth_state, connection.last_verified_at, connection.error_code = "connected", self.clock(), None
            attempt = session.scalar(select(m.LoginAttempt).where(m.LoginAttempt.connection_id == connection_id, m.LoginAttempt.generation == generation).order_by(m.LoginAttempt.created_at.desc()))
            if attempt and attempt.state in {"queued", "authenticating"}:
                attempt.state = "succeeded"
            return True

    def _mark_connection_verified(self, connection_id, generation, remote_account_id=None):
        with self.db.session() as session:
            connection = session.get(m.PlatformConnection, connection_id)
            if not connection or connection.generation != generation:
                return False
            credential = session.get(m.CredentialSecret, connection.credential_id) if connection.credential_id else None
            if connection.identity_fingerprint is None and credential and credential.account_fingerprint:
                connection.identity_fingerprint = credential.account_fingerprint
            connection.remote_account_id = remote_account_id or connection.remote_account_id
            connection.auth_state, connection.last_verified_at, connection.error_code = "connected", self.clock(), None
            attempt = session.scalar(select(m.LoginAttempt).where(m.LoginAttempt.connection_id == connection_id, m.LoginAttempt.generation == generation).order_by(m.LoginAttempt.created_at.desc()))
            if attempt and attempt.state in {"queued", "authenticating"}:
                attempt.state = "succeeded"
            return True

    def _record_challenge(self, connection_id, owner_id, generation, login):
        """Only public metadata is persistent; image and browser context stay in adapter memory."""
        challenge = _plain(login.get("challenge") or {})
        with self.db.session() as session:
            attempt = session.scalar(select(m.LoginAttempt).where(m.LoginAttempt.connection_id == connection_id, m.LoginAttempt.generation == generation).order_by(m.LoginAttempt.created_at.desc()))
            if attempt is None:
                attempt = m.LoginAttempt(owner_id=owner_id, connection_id=connection_id, generation=generation)
                session.add(attempt)
                session.flush()
            attempt.state = "challenge_required"
            attempt.challenge_id = challenge.get("challenge_id") or challenge.get("id")
            attempt.challenge_type = challenge.get("type", "captcha")
            attempt.expires_at = challenge.get("expires_at")
            connection = session.get(m.PlatformConnection, connection_id)
            if connection and connection.generation == generation:
                connection.auth_state, connection.error_code = "challenge_required", "captcha_required"
            if challenge.get("image"):
                self.challenges[attempt.id] = {"owner_id": owner_id, "connection_id": connection_id, "generation": generation, "platform": connection.platform if connection else None, "adapter_attempt_id": login.get("attempt_id"), **challenge}

    async def ipc_handler(self, action, request):
        if action == "platform.open":
            return await self.open_platform(
                request.get("owner_id"), request.get("platform"), request.get("target_url"),
            )
        if action == "platform.prepare_handoff":
            return await self.open_platform(
                request.get("owner_id"), request.get("platform"), request.get("target_url"),
                prepare_only=True,
            )
        if action == "platform.export_cookies":
            return self.export_platform_cookies(
                request.get("owner_id"), request.get("platform"),
            )
        attempt_id, owner_id = request.get("attempt_id"), request.get("owner_id")
        challenge = self.challenges.get(attempt_id)
        if not challenge or challenge["owner_id"] != owner_id or challenge.get("challenge_id") != request.get("challenge_id"):
            raise IPCError("challenge_expired", "登入挑戰已失效。")
        if self.clock() >= challenge["expires_at"]:
            await self._terminate_challenge(attempt_id, challenge, "challenge_expired", expired=True, discard_context=True)
            raise IPCError("challenge_expired", "登入挑戰已過期。")
        with self.db.session() as session:
            connection = session.get(m.PlatformConnection, challenge["connection_id"])
            if not connection or connection.generation != challenge["generation"]:
                raise IPCError("stale_generation", "連線版本已改變。")
            platform = connection.platform
        if action == "challenge.get":
            import base64
            return {"status": "challenge_required", "image_b64": base64.b64encode(challenge["image"]).decode()}
        request_id = request.get("request_id") or f"refresh:{attempt_id}:{challenge['challenge_id']}"
        request_key = (owner_id, attempt_id, challenge["challenge_id"], action, request_id)
        payload_hash = hashlib.sha256((request.get("answer") or "").encode()).hexdigest()
        if request_key in self.challenge_requests:
            saved_hash, saved_response = self.challenge_requests[request_key]
            if saved_hash != payload_hash:
                raise IPCError("request_conflict", "相同 request_id 不可提交不同答案。")
            return saved_response
        if challenge.get("pending"):
            raise IPCError("challenge_pending", "驗證答案正在處理中。")
        challenge["pending"] = True
        accepted = {"status": "accepted", "attempt_id": attempt_id, "request_id": request_id}
        self.challenge_requests[request_key] = (payload_hash, accepted)
        with self.db.session() as session:
            attempt = session.get(m.LoginAttempt, attempt_id)
            connection = session.get(m.PlatformConnection, challenge["connection_id"])
            if attempt:
                attempt.state = "authenticating"
            if connection:
                connection.auth_state = "authenticating"
        task = asyncio.create_task(self._complete_challenge(action, request, request_id, challenge, platform))
        self.challenge_tasks.add(task)
        task.add_done_callback(self.challenge_tasks.discard)
        return accepted

    async def open_platform(self, owner_id, platform, target_url=None, *, prepare_only=False):
        try:
            target_url = managed_platform_url(platform, target_url)
        except ValueError as exc:
            raise IPCError("invalid_platform_url", str(exc)) from exc
        with self.db.session() as session:
            connection = session.scalar(select(m.PlatformConnection).where(
                m.PlatformConnection.owner_id == owner_id,
                m.PlatformConnection.platform == platform,
            ))
            if not connection or not connection.credential_id or connection.auth_state == "not_connected":
                raise IPCError("not_connected", "平台尚未連接。")
            connection_id, generation = connection.id, connection.generation
            expected_remote_id = connection.remote_account_id
            last_verified_at = connection.last_verified_at
            auth_state = connection.auth_state
        if last_verified_at and last_verified_at.tzinfo is None:
            last_verified_at = last_verified_at.replace(tzinfo=timezone.utc)
        recently_verified = (
            auth_state == "connected"
            and last_verified_at is not None
            and last_verified_at >= self.clock() - timedelta(minutes=65)
        )
        storage_state = self._load_session_state(connection_id, platform, owner_id)
        if not storage_state:
            attempt_id = self._queue_reauthentication(connection_id, owner_id, generation)
            return {"status": "reauthentication_required", "attempt_id": attempt_id, "platform": platform}
        # A browser handoff must export the cookie snapshot produced by its own
        # verification. Platform requests made after an hourly verification can
        # rotate Moodle/WAF cookies without updating the persisted snapshot, so
        # recency alone is not sufficient when cookies are about to leave the
        # isolated worker context for Brave.
        if prepare_only or not recently_verified:
            adapter = self.adapters.get(platform)
            if adapter is None and self.adapter_loader:
                try:
                    self.adapters.update(await self.adapter_loader() or {})
                    adapter = self.adapters.get(platform)
                except Exception as exc:
                    raise IPCError("adapter_unavailable", "平台登入元件無法啟動。") from exc
            if adapter is None:
                raise IPCError("adapter_unavailable", "平台登入元件無法啟動。")
            try:
                verified = _plain(await adapter.verify_session(storage_state))
            except Exception as exc:
                raise IPCError(getattr(exc, "code", "upstream_unavailable"), "暫時無法驗證平台登入狀態。") from exc
            status = str(verified.get("status"))
            if status in {"expired", "session_expired"} or verified.get("error_code") == "protected_page_not_reached":
                attempt_id = self._queue_reauthentication(connection_id, owner_id, generation)
                return {"status": "reauthentication_required", "attempt_id": attempt_id, "platform": platform}
            if status not in {"connected", "valid", "ok"}:
                raise IPCError(verified.get("error_code") or status, "平台登入狀態無法確認。")
            verified_remote = verified.get("remote_account_id")
            if expected_remote_id and verified_remote != expected_remote_id:
                raise IPCError("identity_mismatch", "平台登入身分與已連接帳號不符。")
            refreshed_state = verified.get("storage_state")
            if refreshed_state:
                if not self._save_session_state(
                    connection_id, platform, owner_id, generation,
                    refreshed_state, verified_remote,
                ):
                    raise IPCError("identity_mismatch", "平台登入身分與已連接帳號不符。")
                storage_state = refreshed_state
            else:
                self._mark_connection_verified(connection_id, generation, verified_remote)
        if prepare_only:
            return {"status": "ready", "platform": platform, "target_url": target_url}
        if self.platform_opener is None:
            raise IPCError("managed_browser_unavailable", "專用瀏覽器無法啟動。")
        try:
            opened = await self.platform_opener(storage_state, target_url)
        except Exception as exc:
            logging.exception("managed platform browser failed to open")
            raise IPCError("managed_browser_unavailable", "專用瀏覽器無法啟動。") from exc
        if opened and opened.pop("session_expired", False):
            attempt_id = self._queue_reauthentication(connection_id, owner_id, generation)
            return {"status": "reauthentication_required", "attempt_id": attempt_id, "platform": platform}
        return {"status": "opened", "platform": platform, **(opened or {})}

    def export_platform_cookies(self, owner_id, platform):
        try:
            managed_platform_url(platform)
        except ValueError as exc:
            raise IPCError("invalid_platform", str(exc)) from exc
        with self.db.session() as session:
            connection = session.scalar(select(m.PlatformConnection).where(
                m.PlatformConnection.owner_id == owner_id,
                m.PlatformConnection.platform == platform,
                m.PlatformConnection.auth_state == "connected",
            ))
            if not connection:
                raise IPCError("not_connected", "平台尚未連接。")
            connection_id = connection.id
        storage_state = self._load_session_state(connection_id, platform, owner_id)
        if not storage_state:
            raise IPCError("session_expired", "平台登入狀態不存在。")
        official_host = urlsplit(managed_platform_url(platform)).hostname
        # Replace the platform's complete cookie set as one snapshot. eLearn's
        # front-end protection cookies and Moodle session are observed and
        # refreshed together; mixing a new session with stale Brave WAF state
        # causes an immediate redirect back to the logged-out landing page.
        cookies = []
        for cookie in storage_state.get("cookies", []):
            domain = str(cookie.get("domain", "")).lstrip(".").casefold()
            if domain != official_host:
                continue
            cookies.append({key: cookie[key] for key in (
                "name", "value", "domain", "path", "expires", "httpOnly", "secure", "sameSite",
            ) if key in cookie})
        if not cookies:
            raise IPCError("session_expired", "平台登入 Cookie 不存在。")
        return {"status": "ready", "platform": platform, "cookies": cookies}

    def _queue_reauthentication(self, connection_id, owner_id, generation):
        with self.db.session() as session:
            connection = session.get(m.PlatformConnection, connection_id)
            if not connection or connection.owner_id != owner_id or connection.generation != generation:
                raise IPCError("stale_generation", "平台連接版本已改變。")
            attempt = session.scalar(select(m.LoginAttempt).where(
                m.LoginAttempt.connection_id == connection_id,
                m.LoginAttempt.generation == generation,
                m.LoginAttempt.state.in_(("queued", "authenticating", "challenge_required")),
            ).order_by(m.LoginAttempt.created_at.desc()))
            if attempt is None:
                attempt = m.LoginAttempt(
                    owner_id=owner_id, connection_id=connection_id,
                    generation=generation, state="queued",
                )
                session.add(attempt)
                session.flush()
            active_job = session.scalar(select(m.SyncJob.id).where(
                m.SyncJob.connection_id == connection_id,
                m.SyncJob.generation == generation,
                m.SyncJob.state.in_(("queued", "running", "waiting_user")),
            ))
            if not active_job:
                session.add(m.SyncJob(
                    owner_id=owner_id, connection_id=connection_id,
                    generation=generation, preferences_version=connection.preferences_version,
                    scheduled_at=self.clock(),
                ))
            connection.auth_state, connection.error_code = "authenticating", None
            return attempt.id

    async def _terminate_challenge(self, attempt_id, challenge, error_code, *, expired=False, discard_context=False):
        if discard_context:
            adapter = self.adapters.get(challenge.get("platform"))
            discard = getattr(adapter, "discard_attempt", None) or getattr(adapter, "_discard_attempt", None)
            if discard:
                await discard(challenge["adapter_attempt_id"])
        with self.db.session() as session:
            session.execute(update(m.LoginAttempt).where(m.LoginAttempt.id == attempt_id, m.LoginAttempt.generation == challenge["generation"]).values(state="expired" if expired else "failed"))
            session.execute(update(m.PlatformConnection).where(m.PlatformConnection.id == challenge["connection_id"], m.PlatformConnection.generation == challenge["generation"]).values(auth_state="temporarily_unavailable" if error_code not in {"credentials_invalid", "identity_mismatch"} else error_code, error_code=error_code))
            session.execute(update(m.SyncJob).where(m.SyncJob.connection_id == challenge["connection_id"], m.SyncJob.generation == challenge["generation"], m.SyncJob.state == "waiting_user").values(state="failed", finished_at=self.clock(), error_code=error_code, lease_owner=None, lease_until=None))
        self.challenges.pop(attempt_id, None)
        for request_key, (_payload_hash, response) in list(self.challenge_requests.items()):
            if response.get("attempt_id") == attempt_id:
                self.challenge_requests.pop(request_key, None)

    async def _complete_challenge(self, action, request, request_id, challenge, platform):
        attempt_id, owner_id = request["attempt_id"], request["owner_id"]
        adapter = self.adapters[platform]
        try:
            if action == "challenge.refresh":
                result = _plain(await adapter.refresh_challenge(challenge["adapter_attempt_id"], challenge["challenge_id"]))
            else:
                result = _plain(await adapter.answer_challenge(challenge["adapter_attempt_id"], challenge["challenge_id"], request.get("answer", ""), request_id))
        except Exception as exc:
            result = {"status": "upstream_unavailable", "error_code": getattr(exc, "code", "upstream_unavailable")}
        with self.db.session() as session:
            current = session.get(m.PlatformConnection, challenge["connection_id"])
            public_attempt = session.get(m.LoginAttempt, attempt_id)
            if not current or current.generation != challenge["generation"] or not public_attempt or public_attempt.generation != challenge["generation"]:
                self.challenges.pop(attempt_id, None)
                return
        status = result.get("status")
        if status == "challenge_required" and result.get("challenge"):
            self._record_challenge(challenge["connection_id"], owner_id, challenge["generation"], result)
        elif status in {"connected", "success", "ok"}:
            with self.db.session() as session:
                current = session.get(m.PlatformConnection, challenge["connection_id"])
                if current and current.remote_account_id and result.get("remote_account_id") != current.remote_account_id and current.identity_fingerprint is not None:
                    changed = session.execute(update(m.PlatformConnection).where(m.PlatformConnection.id == challenge["connection_id"], m.PlatformConnection.generation == challenge["generation"]).values(auth_state="identity_mismatch", error_code="identity_mismatch"))
                    if not changed.rowcount:
                        return
                    session.execute(update(m.LoginAttempt).where(m.LoginAttempt.id == attempt_id, m.LoginAttempt.generation == challenge["generation"]).values(state="failed"))
                    session.execute(update(m.SyncJob).where(m.SyncJob.connection_id == challenge["connection_id"], m.SyncJob.generation == challenge["generation"], m.SyncJob.state == "waiting_user").values(state="failed", finished_at=self.clock(), error_code="identity_mismatch", lease_owner=None, lease_until=None))
                    self.challenges.pop(attempt_id, None)
                    return
            if not self._save_session_state(challenge["connection_id"], platform, owner_id, challenge["generation"], result["storage_state"], result.get("remote_account_id")):
                self.challenges.pop(attempt_id, None)
                return
            self.challenges.pop(attempt_id, None)
            with self.db.session() as session:
                attempt = session.get(m.LoginAttempt, attempt_id)
                if attempt and attempt.generation == challenge["generation"]:
                    attempt.state = "succeeded"
                session.execute(update(m.SyncJob).where(m.SyncJob.connection_id == challenge["connection_id"], m.SyncJob.generation == challenge["generation"], m.SyncJob.state == "waiting_user").values(state="queued", scheduled_at=self.clock(), error_code=None, lease_owner=None, lease_until=None))
        else:
            error_code = result.get("error_code") or str(status or "upstream_unavailable")
            expired = error_code == "challenge_expired"
            with self.db.session() as session:
                changed = session.execute(update(m.PlatformConnection).where(m.PlatformConnection.id == challenge["connection_id"], m.PlatformConnection.generation == challenge["generation"]).values(auth_state="temporarily_unavailable" if error_code not in {"credentials_invalid", "identity_mismatch"} else error_code, error_code=error_code))
                if not changed.rowcount:
                    self.challenges.pop(attempt_id, None)
                    return
                session.execute(update(m.LoginAttempt).where(m.LoginAttempt.id == attempt_id, m.LoginAttempt.generation == challenge["generation"]).values(state="expired" if expired else "failed"))
                session.execute(update(m.SyncJob).where(m.SyncJob.connection_id == challenge["connection_id"], m.SyncJob.generation == challenge["generation"], m.SyncJob.state == "waiting_user").values(state="failed", finished_at=self.clock(), error_code=error_code, lease_owner=None, lease_until=None))
            self.challenges.pop(attempt_id, None)

    async def commit_result(self, job_id, result, *, finalize=True):
        """Commit only if generation still matches. Adapter results stay staged in memory."""
        payload = result if isinstance(result, dict) else (result.to_dict() if hasattr(result, "to_dict") else vars(result))
        with self.db.session() as session:
            job = session.get(m.SyncJob, job_id)
            connection = session.get(m.PlatformConnection, job.connection_id) if job else None
            if not job or job.state != "running" or job.lease_owner != self.worker_id or not connection or job.generation != connection.generation:
                if job:
                    job.state, job.error_code, job.finished_at = "cancelled", "stale_generation", self.clock()
                return False
            if job.preferences_version != self._preferences_version(session, connection.id):
                job.state, job.error_code, job.finished_at = "cancelled", "stale_generation", self.clock()
                return False
            # Full adapter persistence is intentionally normalized here rather than in parsers.
            for course in payload.get("courses", []):
                course = course if isinstance(course, dict) else vars(course)
                course_code = normalize_course_code(course.get("course_code")) or normalize_course_code(course.get("title"))
                source = session.scalar(select(m.CourseSource).where(m.CourseSource.connection_id == connection.id, m.CourseSource.remote_course_id == str(course["remote_course_id"])))
                if source is None:
                    source = m.CourseSource(owner_id=connection.owner_id, connection_id=connection.id, remote_course_id=str(course["remote_course_id"]), title=course["title"])
                    session.add(source); session.flush()
                    group = session.scalars(select(m.CourseGroup).where(
                        m.CourseGroup.owner_id == connection.owner_id,
                        m.CourseGroup.course_code == course_code,
                    ).order_by(m.CourseGroup.id)).first() if course_code else None
                    if group is None:
                        group = m.CourseGroup(
                            owner_id=connection.owner_id, term=course.get("term"),
                            course_code=course_code, display_name=course["title"],
                        )
                        session.add(group); session.flush()
                    existing = session.scalars(select(m.CourseSelection).where(m.CourseSelection.group_id == group.id)).all()
                    group_disabled = bool(existing) and all(item.selection_state == "disabled" for item in existing)
                    state = "disabled" if group_disabled else ("selected" if not existing else "pending")
                    if existing and not group_disabled:
                        for selection in existing:
                            selection.selection_state = "pending"
                            selection.preference_version += 1
                    session.add(m.CourseSelection(
                        owner_id=connection.owner_id, group_id=group.id,
                        source_id=source.id, selection_state=state,
                    ))
                else:
                    selection = session.scalar(select(m.CourseSelection).where(m.CourseSelection.source_id == source.id))
                    current_group = session.get(m.CourseGroup, selection.group_id) if selection else None
                    matching_groups = session.scalars(select(m.CourseGroup).where(
                        m.CourseGroup.owner_id == connection.owner_id,
                        m.CourseGroup.course_code == course_code,
                    ).order_by(m.CourseGroup.id)).all() if course_code else []
                    target_group = next(
                        (group for group in matching_groups if not current_group or group.id != current_group.id),
                        current_group if current_group in matching_groups else None,
                    )
                    if selection and target_group and target_group.id != selection.group_id:
                        old_group = current_group
                        other_selections = session.scalars(select(m.CourseSelection).where(m.CourseSelection.group_id == target_group.id)).all()
                        target_disabled = bool(other_selections) and all(other.selection_state == "disabled" for other in other_selections)
                        if not target_disabled:
                            for other in other_selections:
                                other.selection_state = "pending"
                                other.preference_version += 1
                        selection.group_id = target_group.id
                        selection.selection_state = "disabled" if target_disabled else "pending"
                        selection.preference_version += 1
                        session.flush()
                        if old_group and not session.scalar(select(m.CourseSelection.id).where(m.CourseSelection.group_id == old_group.id)):
                            session.delete(old_group)
                        current_group = target_group
                    elif current_group and course_code and current_group.course_code is None:
                        current_group.course_code = course_code
                    if current_group and len(course["title"]) > len(current_group.display_name):
                        current_group.display_name = course["title"]
                source.title, source.term, source.course_code, source.source_url, source.last_seen_at = course["title"], course.get("term"), course_code or source.course_code, course.get("source_url"), self.clock()
            job.coverage = json.dumps(payload.get("coverage", {}), ensure_ascii=False)
            if finalize:
                job.state = "partial" if payload.get("complete") is False else "succeeded"
                job.error_code = "partial_fetch" if job.state == "partial" else None
                job.finished_at, job.lease_until = self.clock(), None
            if finalize and payload.get("complete") is not False:
                connection.last_success_at = self.clock()
            return True

    def _finish(self, job_id, state, error_code=None):
        with self.db.session() as session:
            job = session.get(m.SyncJob, job_id)
            if job and job.lease_owner == self.worker_id:
                transient = error_code in {"upstream_timeout", "upstream_rate_limited", "upstream_unavailable", "adapter_unavailable", "vault_unavailable", "job_timeout"}
                if state == "failed" and transient and job.attempts <= len(self.retry_delays):
                    job.state = "queued"
                    job.error_code = error_code
                    job.scheduled_at = self.clock() + timedelta(minutes=self.retry_delays[job.attempts - 1])
                    job.lease_owner = job.lease_until = None
                    connection = session.get(m.PlatformConnection, job.connection_id)
                    if connection and connection.generation == job.generation:
                        connection.auth_state, connection.error_code = "temporarily_unavailable", error_code
                    return
                job.state, job.error_code, job.finished_at, job.lease_until = state, error_code, self.clock(), None
                connection = session.get(m.PlatformConnection, job.connection_id)
                if connection and connection.generation == job.generation and state in {"failed", "partial"}:
                    connection.error_code = error_code
                    if state == "failed":
                        if error_code in {"credentials_invalid", "identity_mismatch", "captcha_required"}:
                            connection.auth_state = "challenge_required" if error_code == "captcha_required" else error_code
                        else:
                            connection.auth_state = "temporarily_unavailable"
                        attempt = session.scalar(select(m.LoginAttempt).where(m.LoginAttempt.connection_id == connection.id, m.LoginAttempt.generation == connection.generation).order_by(m.LoginAttempt.created_at.desc()))
                        if attempt and attempt.state != "challenge_required":
                            attempt.state = "failed"

    async def _process_claimed_job(self, job):
        """Bound a whole sync so one platform cannot block the queue forever."""
        task = asyncio.create_task(self.process_job(job))
        done, _pending = await asyncio.wait({task}, timeout=self.job_timeout_seconds)
        if task in done:
            await task
            return
        logging.error("platform sync exceeded the overall job timeout", extra={"job_id": job.id})
        self._finish(job.id, "failed", "job_timeout")
        task.cancel()
        # Cancellation normally closes Playwright contexts immediately.  Do not
        # let a broken browser cleanup block later hourly checks as well.
        done, _pending = await asyncio.wait({task}, timeout=5)
        if task in done:
            try:
                task.result()
            except asyncio.CancelledError:
                pass
            except Exception:
                logging.exception("timed-out platform sync failed during cleanup")

    async def run(self, poll_seconds=2.0):
        lock_path = self.settings.data_dir / "worker.lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with lock_path.open("a+b") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise RuntimeError("已有 eCourse worker 正在執行。") from exc
            heartbeat_task = asyncio.create_task(self._heartbeat_loop())
            scheduler_task = asyncio.create_task(self._scheduler_loop())
            try:
                self.invalidate_stale_challenges()
                token_path = self.settings.secret_dir / "ipc.token"
                if not token_path.exists():
                    fd = os.open(token_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                    with os.fdopen(fd, "w") as stream:
                        stream.write(secrets.token_urlsafe(32))
                self.ipc_server = await WorkerIPCServer(ipc_socket_path(self.settings.data_dir), token_path.read_text().strip(), self.ipc_handler).start()
                while not self.stop_event.is_set():
                    job = self.claim_job()
                    if job:
                        await self._process_claimed_job(job)
                    try:
                        await asyncio.wait_for(self.stop_event.wait(), timeout=poll_seconds)
                    except asyncio.TimeoutError:
                        pass
            finally:
                if self.ipc_server:
                    await self.ipc_server.close()
                for task in self.challenge_tasks:
                    task.cancel()
                await asyncio.gather(*self.challenge_tasks, return_exceptions=True)
                heartbeat_task.cancel()
                scheduler_task.cancel()
                await asyncio.gather(heartbeat_task, scheduler_task, return_exceptions=True)
                with self.db.session() as session:
                    session.execute(update(m.SyncJob).where(m.SyncJob.lease_owner == self.worker_id, m.SyncJob.state == "running").values(state="queued", scheduled_at=self.clock(), lease_owner=None, lease_until=None))
                self.heartbeat(0)

    async def _heartbeat_loop(self):
        while True:
            active = self._active_job_ids()
            self.heartbeat(len(active))
            for job_id in active:
                self.renew_lease(job_id)
            await asyncio.sleep(30)

    def _active_job_ids(self):
        with self.db.session() as session:
            return list(session.scalars(select(m.SyncJob.id).where(m.SyncJob.lease_owner == self.worker_id, m.SyncJob.state == "running")).all())

    async def _scheduler_loop(self):
        while True:
            try:
                await self.scheduler_tick()
            except Exception:
                # Persistent cursors and dedupe make the next bounded retry safe.
                pass
            await asyncio.sleep(30)

    async def scheduler_tick(self):
        self.reminders.tick()
        if self.periodic_sync:
            self.schedule_periodic_sync()
        await self.expire_challenges()

    def stop(self):
        self.stop_event.set()


def main():
    from ecourse.config import Settings
    asyncio.run(run_worker(Settings.from_env()))


async def run_worker(settings, *, adapters=None, worker_id=None, stop_event=None,
                     retry_delays=(1, 5, 15), periodic_sync=True):
    """Stable CLI/dev entry point. Returns after cancellation or stop_event."""
    playwright = browser = captcha_solver = managed_browser = None
    async def load_adapters():
        nonlocal playwright, browser, captcha_solver
        if browser is None:
            from playwright.async_api import async_playwright
            from ecourse.adapters import create_platform_adapters
            from ecourse.decaptcha import OAuthCaptchaSolver
            if playwright is None:
                playwright = await async_playwright().start()
            browser = await playwright.chromium.launch(headless=True, chromium_sandbox=True)
            captcha_solver = OAuthCaptchaSolver(browser)
            return create_platform_adapters(browser, captcha_solver)
        return {}

    async def open_managed_platform(storage_state, target_url):
        nonlocal playwright, managed_browser
        if playwright is None:
            from playwright.async_api import async_playwright
            playwright = await async_playwright().start()
        if managed_browser is None:
            from ecourse.managed_browser import ManagedPlatformBrowser
            brave_path = "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser"
            managed_browser = ManagedPlatformBrowser(
                playwright.chromium,
                executable_path=brave_path if os.access(brave_path, os.X_OK) else None,
            )
        return await managed_browser.open(storage_state, target_url)

    worker = Worker(
        settings, adapters=adapters, adapter_loader=load_adapters if adapters is None else None,
        worker_id=worker_id, retry_delays=retry_delays, periodic_sync=periodic_sync,
        platform_opener=open_managed_platform,
    )
    shutdown = stop_event or asyncio.Event()
    loop = asyncio.get_running_loop()
    installed = []
    if stop_event is None:
        for signum in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(signum, shutdown.set)
                installed.append(signum)
            except (NotImplementedError, RuntimeError):
                pass
    try:
        runner = asyncio.create_task(worker.run())
        waiter = asyncio.create_task(shutdown.wait())
        done, _ = await asyncio.wait((runner, waiter), return_when=asyncio.FIRST_COMPLETED)
        if waiter in done:
            worker.stop()
            runner.cancel()
            await asyncio.gather(runner, return_exceptions=True)
        else:
            waiter.cancel()
            await asyncio.gather(waiter, return_exceptions=True)
            await runner
    finally:
        for signum in installed:
            loop.remove_signal_handler(signum)
        try:
            if captcha_solver:
                await captcha_solver.close()
        finally:
            try:
                if managed_browser:
                    await managed_browser.close()
            finally:
                try:
                    if browser:
                        await browser.close()
                finally:
                    if playwright:
                        await playwright.stop()


if __name__ == "__main__":
    main()
