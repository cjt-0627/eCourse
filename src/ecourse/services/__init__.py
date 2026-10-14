from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, is_dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any

from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.exc import IntegrityError

from ecourse.db import Database
from ecourse.db import models as m
from ecourse.platforms import managed_platform_url
from ecourse.vault import Vault


class ServiceError(Exception):
    code = "service_error"


class NotFoundError(ServiceError):
    code = "not_found"


class ConflictError(ServiceError):
    code = "conflict"


class ValidationError(ServiceError):
    code = "validation_error"


class GoneError(ServiceError):
    code = "gone"


class ServiceUnavailableError(ServiceError):
    code = "service_unavailable"


def _iso(value):
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return value


def _dict(record, *, exclude=()) -> dict:
    return {column.name: _iso(getattr(record, column.name)) for column in record.__table__.columns if column.name not in exclude}


def _dt(value: str | datetime | None) -> datetime | None:
    if value is None or isinstance(value, datetime):
        result = value
    else:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result is not None and result.tzinfo is None:
        raise ValidationError("時間必須包含時區。")
    return result


def _date(value: str | date | None) -> date | None:
    return date.fromisoformat(value) if isinstance(value, str) else value


class Service:
    def __init__(self, settings, *, ipc_client=None, clock=None):
        self.settings = settings
        self.db = Database(settings)
        if ipc_client is None:
            token_path = settings.secret_dir / "ipc.token"
            if token_path.exists():
                from ecourse.ipc import WorkerIPCClient, ipc_socket_path
                ipc_client = WorkerIPCClient(ipc_socket_path(settings.data_dir), token_path.read_text().strip())
        self.ipc_client = ipc_client
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def dashboard(self, owner_id: str) -> dict:
        now = self.clock()
        with self.db.session() as session:
            item_count = session.scalar(select(func.count()).select_from(m.LearningItem).where(m.LearningItem.owner_id == owner_id, m.LearningItem.archived_at.is_(None))) or 0
            unread = session.scalar(select(func.count()).select_from(m.Notification).where(m.Notification.owner_id == owner_id, m.Notification.read_at.is_(None))) or 0
            manual = session.scalar(select(func.count()).select_from(m.ManualEvent).where(m.ManualEvent.owner_id == owner_id)) or 0
            connections = session.scalars(select(m.PlatformConnection).where(m.PlatformConnection.owner_id == owner_id)).all()
            return {"counts": {"items": item_count, "manual_events": manual, "unread_notifications": unread}, "connections": [self._connection_with_sync(session, c) for c in connections], "generated_at": now.isoformat()}

    def update_locale(self, owner_id: str, locale: str) -> dict:
        if locale not in {"zh-Hant", "en"}:
            raise ValidationError("不支援的介面語言。")
        with self.db.session() as session:
            owner = session.scalar(select(m.Owner).where(m.Owner.id == owner_id))
            if owner is None:
                raise NotFoundError("找不到使用者。")
            owner.locale = locale
        return {"locale": locale}

    def save_credentials(self, owner_id: str, account: str, password: str, *, verified: bool = True) -> dict:
        account = account.strip()
        if not account or not password:
            raise ValidationError("帳號與密碼不可空白。")
        vault = Vault(self.settings.secret_dir)
        envelope = vault.seal({"account": account, "password": password}, platform="elearn", account_id=owner_id, purpose="credential")
        fingerprint = vault.account_fingerprint(account, owner_id=owner_id)
        hint = (account[:2] + "***" + account[-2:]) if len(account) > 4 else "***"
        with self.db.session() as session:
            secret = m.CredentialSecret(
                owner_id=owner_id, encrypted_payload=envelope, account_hint=hint,
                account_fingerprint=fingerprint, verified_at=self.clock() if verified else None,
            )
            session.add(secret)
            session.flush()
            # Saving a candidate credential must not interrupt currently connected
            # platforms. Generation changes only when connect() explicitly selects it.
            return {"id": secret.id, "account_hint": hint, "updated_at": secret.updated_at.isoformat()}

    def list_credentials(self, owner_id: str) -> list[dict]:
        with self.db.session() as session:
            records = session.scalars(select(m.CredentialSecret).where(m.CredentialSecret.owner_id == owner_id).order_by(m.CredentialSecret.updated_at.desc())).all()
            return [{"id": x.id, "account_hint": x.account_hint, "updated_at": x.updated_at.isoformat()} for x in records]

    def worker_load_credentials(self, owner_id: str, credential_id: str) -> dict:
        """Worker-only boundary. Web routes must never expose this return value."""
        with self.db.session() as session:
            secret = session.scalar(select(m.CredentialSecret).where(m.CredentialSecret.id == credential_id, m.CredentialSecret.owner_id == owner_id))
            if not secret:
                raise NotFoundError("找不到憑證。")
            envelope = secret.encrypted_payload
        vault = Vault(self.settings.secret_dir)
        credentials = vault.unseal(envelope, platform="elearn", account_id=owner_id, purpose="credential")
        fingerprint = vault.account_fingerprint(credentials["account"], owner_id=owner_id)
        with self.db.session() as session:
            secret = session.scalar(select(m.CredentialSecret).where(m.CredentialSecret.id == credential_id, m.CredentialSecret.owner_id == owner_id))
            if secret and secret.account_fingerprint is None:
                secret.account_fingerprint = fingerprint
        credentials["account_fingerprint"] = fingerprint
        return credentials

    @staticmethod
    def _clear_remote_namespace(session, owner_id: str, connection: m.PlatformConnection) -> None:
        old_item_ids = select(m.LearningItem.id).join(m.CourseSource).where(m.CourseSource.connection_id == connection.id)
        session.execute(update(m.NotificationEvent).where(m.NotificationEvent.owner_id == owner_id, m.NotificationEvent.entity_id.in_(old_item_ids), m.NotificationEvent.state == "pending").values(state="cancelled"))
        session.execute(m.CourseSource.__table__.delete().where(m.CourseSource.connection_id == connection.id))
        session.execute(m.CourseGroup.__table__.delete().where(m.CourseGroup.owner_id == owner_id, ~m.CourseGroup.id.in_(select(m.CourseSelection.group_id))))
        connection.remote_account_id = None

    def connect(self, owner_id: str, platform: str, credential_id: str) -> dict:
        if platform not in {"elearn", "eeclass"}:
            raise ValidationError("不支援的平台。")
        with self.db.session() as session:
            credential = session.scalar(select(m.CredentialSecret).where(m.CredentialSecret.id == credential_id, m.CredentialSecret.owner_id == owner_id))
            if credential is None:
                raise NotFoundError("找不到憑證。")
            return self._connect_in_session(session, owner_id, platform, credential)

    def replace_credentials_and_reconnect(self, owner_id: str, account: str, password: str) -> dict:
        """Atomically encrypt new credentials and rotate both platform generations."""
        account = account.strip()
        if not account or not password:
            raise ValidationError("帳號與密碼不可空白。")
        vault = Vault(self.settings.secret_dir)
        envelope = vault.seal({"account": account, "password": password}, platform="elearn", account_id=owner_id, purpose="credential")
        fingerprint = vault.account_fingerprint(account, owner_id=owner_id)
        hint = (account[:2] + "***" + account[-2:]) if len(account) > 4 else "***"
        with self.db.session() as session:
            if session.get(m.Owner, owner_id) is None:
                raise NotFoundError("找不到擁有者。")
            credential = m.CredentialSecret(
                owner_id=owner_id, encrypted_payload=envelope,
                account_hint=hint, account_fingerprint=fingerprint, verified_at=self.clock(),
            )
            session.add(credential)
            session.flush()
            attempts = {
                platform: self._connect_in_session(session, owner_id, platform, credential)
                for platform in ("elearn", "eeclass")
            }
            return {
                "credential": {"id": credential.id, "account_hint": hint, "updated_at": credential.updated_at.isoformat()},
                "attempts": attempts,
            }

    def _connect_in_session(self, session, owner_id: str, platform: str, credential: m.CredentialSecret) -> dict:
            credential_id = credential.id
            connection = session.scalar(select(m.PlatformConnection).where(m.PlatformConnection.owner_id == owner_id, m.PlatformConnection.platform == platform))
            if connection is None:
                connection = m.PlatformConnection(
                    owner_id=owner_id, platform=platform, credential_id=credential_id,
                    identity_fingerprint=credential.account_fingerprint, auth_state="authenticating",
                )
                session.add(connection)
                session.flush()
            else:
                session.execute(update(m.SyncJob).where(m.SyncJob.connection_id == connection.id, m.SyncJob.state.in_(("queued", "running", "waiting_user"))).values(state="cancelled", finished_at=self.clock(), error_code="stale_generation", lease_owner=None, lease_until=None))
                if connection.credential_id and connection.credential_id != credential_id:
                    if connection.identity_fingerprint and connection.identity_fingerprint != credential.account_fingerprint:
                        self._clear_remote_namespace(session, owner_id, connection)
                        connection.identity_fingerprint = credential.account_fingerprint
                # connect() is an explicit authentication-settings change,
                # including when the same credential row is selected again.
                # SessionState is keyed only by connection id (not generation),
                # so retaining it would let the next generation silently reuse
                # the old school session and skip the requested login.
                state = session.get(m.SessionState, connection.id)
                if state:
                    session.delete(state)
                connection.generation += 1
                connection.credential_id = credential_id
                connection.auth_state = "authenticating"
                connection.error_code = None
            attempt = m.LoginAttempt(owner_id=owner_id, connection_id=connection.id, generation=connection.generation, state="queued")
            session.add(attempt)
            session.flush()
            session.add(m.SyncJob(owner_id=owner_id, connection_id=connection.id, generation=connection.generation, preferences_version=connection.preferences_version, scheduled_at=self.clock()))
            return {"attempt_id": attempt.id, "connection": self._connection(connection)}

    def disconnect(self, owner_id: str, platform: str) -> dict:
        with self.db.session() as session:
            connection = session.scalar(select(m.PlatformConnection).where(m.PlatformConnection.owner_id == owner_id, m.PlatformConnection.platform == platform))
            if not connection:
                raise NotFoundError("找不到連線。")
            connection.generation += 1
            connection.auth_state = "not_connected"
            # Retain namespace identity while disconnected so a later account
            # replacement cannot display old courses under a different account.
            session.execute(m.SyncJob.__table__.update().where(m.SyncJob.connection_id == connection.id, m.SyncJob.state.in_(("queued", "running", "waiting_user"))).values(state="cancelled", finished_at=self.clock(), error_code="stale_generation"))
            state = session.get(m.SessionState, connection.id)
            if state:
                session.delete(state)
            return self._connection(connection)

    def list_connections(self, owner_id: str) -> list[dict]:
        with self.db.session() as session:
            rows = session.scalars(select(m.PlatformConnection).where(m.PlatformConnection.owner_id == owner_id).order_by(m.PlatformConnection.platform)).all()
            result = []
            for connection in rows:
                value = self._connection_with_sync(session, connection)
                attempt = session.scalar(select(m.LoginAttempt).where(m.LoginAttempt.connection_id == connection.id, m.LoginAttempt.generation == connection.generation, m.LoginAttempt.state.in_(("queued", "authenticating", "challenge_required"))).order_by(m.LoginAttempt.created_at.desc()))
                value["active_attempt"] = ({"active_attempt_id": attempt.id, "state": attempt.state, "challenge_id": attempt.challenge_id, "expires_at": _iso(attempt.expires_at)} if attempt else None)
                result.append(value)
            return result

    @staticmethod
    def _connection(connection) -> dict:
        return {key: _iso(getattr(connection, key)) for key in ("id", "platform", "auth_state", "generation", "last_verified_at", "last_success_at", "error_code")}

    def _connection_with_sync(self, session, connection) -> dict:
        value = self._connection(connection)
        job = session.scalars(
            select(m.SyncJob)
            .where(m.SyncJob.connection_id == connection.id, m.SyncJob.generation == connection.generation)
            .order_by(m.SyncJob.scheduled_at.desc())
            .limit(1)
        ).first()
        cursor = session.scalar(select(m.ScheduleState).where(
            m.ScheduleState.owner_id == connection.owner_id,
            m.ScheduleState.schedule_key == f"sync:{connection.id}",
        ))
        value.update({
            "last_attempt_at": _iso((job.finished_at or job.started_at) if job else None),
            "last_attempt_state": job.state if job else None,
            "last_attempt_error": job.error_code if job else None,
            "next_sync_at": _iso(cursor.next_run_at) if cursor else None,
        })
        return value

    def get_login_attempt(self, owner_id: str, attempt_id: str) -> dict:
        with self.db.session() as session:
            attempt = session.scalar(select(m.LoginAttempt).where(m.LoginAttempt.id == attempt_id, m.LoginAttempt.owner_id == owner_id))
            if not attempt:
                raise NotFoundError("找不到登入交易。")
            connection = session.get(m.PlatformConnection, attempt.connection_id)
            if not connection or connection.generation != attempt.generation:
                raise GoneError("登入交易已失效。")
            result = _dict(attempt, exclude=("owner_id", "connection_id"))
            result["challenge_available"] = attempt.state == "challenge_required" and self._ipc_client() is not None
            return result

    async def get_challenge_image(self, owner_id: str, attempt_id: str) -> bytes:
        attempt = self.get_login_attempt(owner_id, attempt_id)
        client = self._ipc_client()
        if client is None:
            raise ServiceUnavailableError("背景 worker 無法連線。")
        try:
            return await client.challenge_image(owner_id, attempt_id, attempt.get("challenge_id"))
        except Exception as exc:
            self._raise_ipc(exc)

    async def submit_challenge(self, owner_id: str, attempt_id: str, answer: str, request_id: str) -> dict:
        attempt = self.get_login_attempt(owner_id, attempt_id)
        if not answer or not request_id:
            raise ValidationError("答案與 request_id 不可空白。")
        client = self._ipc_client()
        if client is None:
            raise ServiceUnavailableError("背景 worker 無法連線。")
        try:
            return await client.answer(owner_id, attempt_id, attempt.get("challenge_id"), answer, request_id)
        except Exception as exc:
            self._raise_ipc(exc)

    def retry_connection(self, owner_id: str, platform: str) -> dict:
        with self.db.session() as session:
            connection = session.scalar(select(m.PlatformConnection).where(m.PlatformConnection.owner_id == owner_id, m.PlatformConnection.platform == platform))
            if not connection or not connection.credential_id:
                raise NotFoundError("找不到可重試的連線。")
            active = session.scalar(select(m.LoginAttempt).where(m.LoginAttempt.connection_id == connection.id, m.LoginAttempt.generation == connection.generation, m.LoginAttempt.state.in_(("queued", "authenticating", "challenge_required"))).order_by(m.LoginAttempt.created_at.desc()))
            if active:
                return {"attempt_id": active.id, "connection": self._connection(connection)}
            connection.generation += 1
            connection.auth_state, connection.error_code = "authenticating", None
            state = session.get(m.SessionState, connection.id)
            if state:
                session.delete(state)
            attempt = m.LoginAttempt(owner_id=owner_id, connection_id=connection.id, generation=connection.generation, state="queued")
            session.add(attempt); session.flush()
            session.add(m.SyncJob(owner_id=owner_id, connection_id=connection.id, generation=connection.generation, preferences_version=connection.preferences_version, scheduled_at=self.clock()))
            return {"attempt_id": attempt.id, "connection": self._connection(connection)}

    async def open_platform(self, owner_id: str, platform: str, target_url: str | None = None) -> dict:
        try:
            target_url = managed_platform_url(platform, target_url)
        except ValueError as exc:
            raise ValidationError(str(exc)) from exc
        with self.db.session() as session:
            connection = session.scalar(select(m.PlatformConnection).where(
                m.PlatformConnection.owner_id == owner_id,
                m.PlatformConnection.platform == platform,
            ))
            if not connection or not connection.credential_id or connection.auth_state == "not_connected":
                raise ConflictError("平台尚未連接。")
        client = self._ipc_client()
        if client is None:
            raise ServiceUnavailableError("背景 worker 無法連線。")
        try:
            return await client.open_platform(owner_id, platform, target_url)
        except Exception as exc:
            self._raise_ipc(exc)

    async def prepare_platform_handoff(self, owner_id: str, platform: str, target_url: str | None = None) -> dict:
        try:
            target_url = managed_platform_url(platform, target_url)
        except ValueError as exc:
            raise ValidationError(str(exc)) from exc
        with self.db.session() as session:
            connection = session.scalar(select(m.PlatformConnection).where(
                m.PlatformConnection.owner_id == owner_id,
                m.PlatformConnection.platform == platform,
            ))
            if not connection or not connection.credential_id or connection.auth_state == "not_connected":
                raise ConflictError("平台尚未連接。")
        client = self._ipc_client()
        if client is None:
            raise ServiceUnavailableError("背景 worker 無法連線。")
        try:
            return await client.prepare_platform_handoff(owner_id, platform, target_url)
        except Exception as exc:
            self._raise_ipc(exc)

    async def export_platform_cookies(self, owner_id: str, platform: str) -> list[dict]:
        client = self._ipc_client()
        if client is None:
            raise ServiceUnavailableError("背景 worker 無法連線。")
        try:
            result = await client.export_platform_cookies(owner_id, platform)
            return result.get("cookies", [])
        except Exception as exc:
            self._raise_ipc(exc)

    async def refresh_challenge(self, owner_id: str, attempt_id: str) -> dict:
        attempt = self.get_login_attempt(owner_id, attempt_id)
        client = self._ipc_client()
        if client is None:
            raise ServiceUnavailableError("背景 worker 無法連線。")
        try:
            return await client.refresh(owner_id, attempt_id, attempt.get("challenge_id"))
        except Exception as exc:
            self._raise_ipc(exc)

    @staticmethod
    def _raise_ipc(exc):
        code = getattr(exc, "code", "worker_unavailable")
        if code in {"challenge_expired", "stale_generation"}:
            raise GoneError(str(exc)) from exc
        raise ServiceUnavailableError(str(exc)) from exc

    def _ipc_client(self):
        if self.ipc_client is None:
            token_path = self.settings.secret_dir / "ipc.token"
            if token_path.exists():
                from ecourse.ipc import WorkerIPCClient, ipc_socket_path
                self.ipc_client = WorkerIPCClient(ipc_socket_path(self.settings.data_dir), token_path.read_text().strip())
        return self.ipc_client

    def list_courses(self, owner_id: str) -> list[dict]:
        from ecourse.parsing.html import is_platform_tutorial

        with self.db.session() as session:
            groups = session.scalars(select(m.CourseGroup).where(m.CourseGroup.owner_id == owner_id).order_by(m.CourseGroup.display_name)).all()
            result = []
            for group in groups:
                sources = session.execute(
                    select(m.CourseSource, m.CourseSelection, m.PlatformConnection)
                    .join(m.CourseSelection, m.CourseSelection.source_id == m.CourseSource.id)
                    .join(m.PlatformConnection, m.PlatformConnection.id == m.CourseSource.connection_id)
                    .where(m.CourseSelection.group_id == group.id, m.CourseSource.owner_id == owner_id)
                    .order_by(m.PlatformConnection.platform)
                ).all()
                # Hide previously imported platform help links without deleting user data.
                if sources and all(is_platform_tutorial(source.title, connection.platform) and not source.course_code
                                   for source, _selection, connection in sources):
                    continue
                item = _dict(group, exclude=("owner_id",))
                item["sources"] = [{
                    **_dict(source, exclude=("owner_id", "connection_id")),
                    "platform": connection.platform,
                    "selection_state": selection.selection_state,
                    "preference_version": selection.preference_version,
                } for source, selection, connection in sources]
                selected_count = sum(source["selection_state"] == "selected" for source in item["sources"])
                item["disabled"] = bool(item["sources"]) and all(
                    source["selection_state"] == "disabled" for source in item["sources"]
                )
                item["selection_required"] = bool(item["sources"]) and selected_count != 1 and not item["disabled"]
                result.append(item)
            return result

    def get_course(self, owner_id: str, group_id: str) -> dict:
        courses = self.list_courses(owner_id)
        course = next((x for x in courses if x["id"] == group_id), None)
        if not course:
            raise NotFoundError("找不到課程。")
        return course

    def get_course_workspace(self, owner_id: str, group_id: str) -> dict:
        """Return everything a user needs to browse one course in eCourse."""
        course = self.get_course(owner_id, group_id)
        items = self.list_items(owner_id, {"course": group_id, "limit": 100})
        with self.db.session() as session:
            materials = session.execute(
                select(m.CourseMaterial, m.PlatformConnection)
                .join(m.CourseSource, m.CourseSource.id == m.CourseMaterial.source_course_id)
                .join(m.CourseSelection, m.CourseSelection.source_id == m.CourseSource.id)
                .join(m.PlatformConnection, m.PlatformConnection.id == m.CourseSource.connection_id)
                .where(
                    m.CourseMaterial.owner_id == owner_id,
                    m.CourseSelection.group_id == group_id,
                    m.CourseSelection.selection_state == "selected",
                )
                .order_by(m.CourseMaterial.section_title, m.CourseMaterial.title)
            ).all()
            announcements = session.execute(
                select(m.Announcement, m.PlatformConnection)
                .join(m.CourseSource, m.CourseSource.id == m.Announcement.source_course_id)
                .join(m.CourseSelection, m.CourseSelection.source_id == m.CourseSource.id)
                .join(m.PlatformConnection, m.PlatformConnection.id == m.CourseSource.connection_id)
                .where(
                    m.Announcement.owner_id == owner_id,
                    m.CourseSelection.group_id == group_id,
                    m.CourseSelection.selection_state == "selected",
                    ~m.Announcement.title.in_(("公告", "Announcement")),
                )
                .order_by(m.Announcement.published_at.desc())
                .limit(50)
            ).all()
        course["materials"] = [
            {**_dict(material, exclude=("owner_id", "source_course_id")), "platform": connection.platform}
            for material, connection in materials
        ]
        course["items"] = items
        course["grades"] = [item for item in items if item.get("grade_text") or item.get("grading_status_text")]
        course["announcements"] = [
            {**_dict(row, exclude=("owner_id", "source_course_id")), "platform": connection.platform}
            for row, connection in announcements
        ]
        return course

    def list_course_materials(self, owner_id: str, group_id: str) -> list[dict]:
        return self.get_course_workspace(owner_id, group_id)["materials"]

    def set_course_selection(self, owner_id: str, group_id: str, source_id: str, selection_state: str, expected_version: int) -> dict:
        if selection_state not in {"selected", "disabled", "pending", "none"}:
            raise ValidationError("課程來源選擇無效。")
        with self.db.session() as session:
            selection = session.scalar(select(m.CourseSelection).join(m.CourseGroup, m.CourseGroup.id == m.CourseSelection.group_id).where(m.CourseSelection.group_id == group_id, m.CourseSelection.source_id == source_id, m.CourseGroup.owner_id == owner_id))
            if not selection:
                raise NotFoundError("找不到課程來源。")
            if selection.preference_version != expected_version:
                raise ConflictError("課程來源設定已被更新。")
            group_rows = session.execute(
                select(m.CourseSelection, m.CourseSource, m.PlatformConnection)
                .join(m.CourseSource, m.CourseSource.id == m.CourseSelection.source_id)
                .join(m.PlatformConnection, m.PlatformConnection.id == m.CourseSource.connection_id)
                .where(m.CourseSelection.group_id == group_id)
            ).all()
            affected_connections = {}
            for candidate, _candidate_source, candidate_connection in group_rows:
                wanted = "disabled" if selection_state == "none" else (
                    "selected" if candidate.source_id == source_id else "disabled"
                ) if selection_state == "selected" else (
                    selection_state if candidate.source_id == source_id else candidate.selection_state
                )
                if candidate.selection_state != wanted or candidate.id == selection.id:
                    candidate.selection_state = wanted
                    candidate.preference_version += 1
                    affected_connections[candidate_connection.id] = candidate_connection
            for connection in affected_connections.values():
                connection.preferences_version += 1
                session.execute(update(m.SyncJob).where(m.SyncJob.connection_id == connection.id, m.SyncJob.state.in_(("queued", "running", "waiting_user"))).values(state="cancelled", finished_at=self.clock(), error_code="stale_generation", lease_owner=None, lease_until=None))
                if connection.auth_state == "connected":
                    session.add(m.SyncJob(owner_id=owner_id, connection_id=connection.id, generation=connection.generation, preferences_version=connection.preferences_version, scheduled_at=self.clock()))
            return {"group_id": group_id, "source_id": source_id, "selection_state": selection_state, "preference_version": selection.preference_version}

    def list_items(self, owner_id: str, filters: dict | None = None, **kwargs) -> list[dict]:
        filters = {**(filters or {}), **kwargs}
        with self.db.session() as session:
            query = (
                select(m.LearningItem, m.ItemPreference, m.CourseSource, m.PlatformConnection, m.CourseGroup)
                .join(m.CourseSource, m.CourseSource.id == m.LearningItem.source_course_id)
                .join(m.PlatformConnection, m.PlatformConnection.id == m.CourseSource.connection_id)
                .outerjoin(m.ItemPreference, and_(m.ItemPreference.item_id == m.LearningItem.id, m.ItemPreference.owner_id == owner_id))
                .outerjoin(m.CourseSelection, m.CourseSelection.source_id == m.CourseSource.id)
                .outerjoin(m.CourseGroup, m.CourseGroup.id == m.CourseSelection.group_id)
                .where(
                    m.LearningItem.owner_id == owner_id,
                    m.LearningItem.archived_at.is_(None),
                    m.CourseSelection.selection_state == "selected",
                )
            )
            if filters.get("kind") or filters.get("type"):
                query = query.where(m.LearningItem.kind == (filters.get("kind") or filters["type"]))
            if filters.get("status"):
                query = query.where(m.LearningItem.remote_status == filters["status"])
            if filters.get("course"):
                query = query.where(m.CourseGroup.id == filters["course"])
            if filters.get("platform"):
                query = query.where(m.PlatformConnection.platform == filters["platform"])
            if filters.get("q"):
                query = query.where(or_(m.LearningItem.title.ilike(f"%{filters['q']}%"), m.CourseGroup.display_name.ilike(f"%{filters['q']}%")))
            query = query.order_by(m.LearningItem.due_at.is_(None), m.LearningItem.due_at, m.LearningItem.due_date, m.LearningItem.title).limit(min(int(filters.get("limit", 50)), 100))
            return [self._item(item, pref, source, connection, group) for item, pref, source, connection, group in session.execute(query).all()]

    def get_item(self, owner_id: str, item_id: str) -> dict:
        with self.db.session() as session:
            row = session.execute(
                select(m.LearningItem, m.ItemPreference, m.CourseSource, m.PlatformConnection, m.CourseGroup)
                .join(m.CourseSource, m.CourseSource.id == m.LearningItem.source_course_id)
                .join(m.PlatformConnection, m.PlatformConnection.id == m.CourseSource.connection_id)
                .outerjoin(m.ItemPreference, and_(m.ItemPreference.item_id == m.LearningItem.id, m.ItemPreference.owner_id == owner_id))
                .outerjoin(m.CourseSelection, m.CourseSelection.source_id == m.CourseSource.id)
                .outerjoin(m.CourseGroup, m.CourseGroup.id == m.CourseSelection.group_id)
                .where(m.LearningItem.id == item_id, m.LearningItem.owner_id == owner_id)
            ).one_or_none()
            if not row:
                raise NotFoundError("找不到課務。")
            return self._item(*row)

    @staticmethod
    def _item(item, preference=None, source=None, connection=None, group=None):
        result = _dict(item, exclude=("owner_id",))
        result["handled"] = bool(preference and preference.handled_at)
        result["handled_at"] = _iso(preference.handled_at) if preference else None
        result["muted_until"] = _iso(preference.muted_until) if preference else None
        result["course_name"] = group.display_name if group else (source.title if source else None)
        result["course_id"] = group.id if group else None
        result["platform"] = connection.platform if connection else None
        return result

    def set_item_preference(self, owner_id: str, item_id: str, *, handled: bool | None = None, muted_until=None) -> dict:
        with self.db.session() as session:
            item = session.scalar(select(m.LearningItem).where(m.LearningItem.id == item_id, m.LearningItem.owner_id == owner_id))
            if not item:
                raise NotFoundError("找不到課務。")
            pref = session.scalar(select(m.ItemPreference).where(m.ItemPreference.owner_id == owner_id, m.ItemPreference.item_id == item_id))
            if pref is None:
                pref = m.ItemPreference(owner_id=owner_id, item_id=item_id)
                session.add(pref)
            if handled is not None:
                pref.handled_at = self.clock() if handled else None
                if handled:
                    session.execute(m.NotificationEvent.__table__.update().where(m.NotificationEvent.owner_id == owner_id, m.NotificationEvent.entity_id == item_id, m.NotificationEvent.state == "pending").values(state="cancelled"))
            pref.muted_until = _dt(muted_until)
            session.flush()
            return self._item(item, pref)

    def list_announcements(self, owner_id: str, **_filters) -> list[dict]:
        with self.db.session() as session:
            rows = session.execute(
                select(m.Announcement, m.AnnouncementRead)
                .join(m.CourseSource, m.CourseSource.id == m.Announcement.source_course_id)
                .join(m.CourseSelection, m.CourseSelection.source_id == m.CourseSource.id)
                .outerjoin(m.AnnouncementRead, and_(m.AnnouncementRead.announcement_id == m.Announcement.id, m.AnnouncementRead.owner_id == owner_id))
                .where(m.Announcement.owner_id == owner_id, m.CourseSelection.selection_state == "selected")
                .order_by(m.Announcement.published_at.desc()).limit(100)
            ).all()
            return [{**_dict(a, exclude=("owner_id", "body_sanitized")), "read_at": _iso(r.read_at) if r else None} for a, r in rows]

    def get_announcement(self, owner_id: str, announcement_id: str) -> dict:
        with self.db.session() as session:
            announcement = session.scalar(select(m.Announcement).where(m.Announcement.id == announcement_id, m.Announcement.owner_id == owner_id))
            if not announcement:
                raise NotFoundError("找不到公告。")
            return _dict(announcement, exclude=("owner_id",))

    def mark_announcement_read(self, owner_id: str, announcement_id: str) -> dict:
        with self.db.session() as session:
            announcement = session.scalar(select(m.Announcement).where(m.Announcement.id == announcement_id, m.Announcement.owner_id == owner_id))
            if not announcement:
                raise NotFoundError("找不到公告。")
            read = session.scalar(select(m.AnnouncementRead).where(m.AnnouncementRead.owner_id == owner_id, m.AnnouncementRead.announcement_id == announcement_id))
            if read is None:
                read = m.AnnouncementRead(owner_id=owner_id, announcement_id=announcement_id)
                session.add(read)
            read.read_at, read.read_content_hash = self.clock(), announcement.content_hash
            return {"id": announcement_id, "read_at": read.read_at.isoformat()}

    def create_manual_event(self, owner_id: str, payload: dict | None = None, **kwargs) -> dict:
        data = {**(payload or {}), **kwargs}
        values = self._manual_values(owner_id, data)
        with self.db.session() as session:
            event = m.ManualEvent(owner_id=owner_id, **values)
            session.add(event)
            session.flush()
            return _dict(event, exclude=("owner_id",))

    def list_manual_events(self, owner_id: str, **filters) -> list[dict]:
        with self.db.session() as session:
            query = select(m.ManualEvent).where(m.ManualEvent.owner_id == owner_id)
            if filters.get("group_id"):
                query = query.where(m.ManualEvent.group_id == filters["group_id"])
            rows = session.scalars(query.order_by(m.ManualEvent.starts_at.is_(None), m.ManualEvent.starts_at, m.ManualEvent.event_date).limit(min(int(filters.get("limit", 50)), 100))).all()
            return [_dict(x, exclude=("owner_id",)) for x in rows]

    def get_manual_event(self, owner_id: str, event_id: str) -> dict:
        with self.db.session() as session:
            event = session.scalar(select(m.ManualEvent).where(m.ManualEvent.id == event_id, m.ManualEvent.owner_id == owner_id))
            if not event:
                raise NotFoundError("找不到手動事件。")
            return _dict(event, exclude=("owner_id",))

    def update_manual_event(self, owner_id: str, event_id: str, payload: dict | None = None, **changes) -> dict:
        data = {**(payload or {}), **changes}
        with self.db.session() as session:
            event = session.scalar(select(m.ManualEvent).where(m.ManualEvent.id == event_id, m.ManualEvent.owner_id == owner_id))
            if not event:
                raise NotFoundError("找不到手動事件。")
            current = _dict(event)
            current.update(data)
            values = self._manual_values(owner_id, current)
            old_deadline = (event.date_precision, event.starts_at, event.event_date, event.source_timezone)
            new_deadline = (values["date_precision"], values["starts_at"], values["event_date"], values["source_timezone"])
            if new_deadline != old_deadline:
                session.execute(m.NotificationEvent.__table__.update().where(m.NotificationEvent.owner_id == owner_id, m.NotificationEvent.entity_id == event_id, m.NotificationEvent.state == "pending").values(state="cancelled"))
            for key, value in values.items():
                setattr(event, key, value)
            if new_deadline != old_deadline:
                event.revision += 1
            event.updated_at = self.clock()
            return _dict(event, exclude=("owner_id",))

    def set_manual_event_preference(self, owner_id: str, event_id: str, *, handled: bool) -> dict:
        with self.db.session() as session:
            event = session.scalar(select(m.ManualEvent).where(m.ManualEvent.id == event_id, m.ManualEvent.owner_id == owner_id))
            if not event:
                raise NotFoundError("找不到手動事件。")
            event.handled_at = self.clock() if handled else None
            if handled:
                session.execute(update(m.NotificationEvent).where(m.NotificationEvent.owner_id == owner_id, m.NotificationEvent.entity_id == event_id, m.NotificationEvent.state == "pending").values(state="cancelled"))
            else:
                # Handling is personal state, not a deadline revision. Restore only
                # unsent reminders from this exact deadline version. Processed events
                # remain deduped and are never emitted a second time.
                session.execute(update(m.NotificationEvent).where(
                    m.NotificationEvent.owner_id == owner_id,
                    m.NotificationEvent.entity_id == event_id,
                    m.NotificationEvent.entity_revision == event.revision,
                    m.NotificationEvent.type == "manual_event_due",
                    m.NotificationEvent.state == "cancelled",
                    or_(m.NotificationEvent.expires_at.is_(None), m.NotificationEvent.expires_at > self.clock()),
                ).values(state="pending"))
            return _dict(event, exclude=("owner_id",))

    def delete_manual_event(self, owner_id: str, event_id: str) -> dict:
        with self.db.session() as session:
            event = session.scalar(select(m.ManualEvent).where(m.ManualEvent.id == event_id, m.ManualEvent.owner_id == owner_id))
            if not event:
                raise NotFoundError("找不到手動事件。")
            session.execute(m.NotificationEvent.__table__.update().where(m.NotificationEvent.owner_id == owner_id, m.NotificationEvent.entity_id == event_id, m.NotificationEvent.state == "pending").values(state="cancelled"))
            session.delete(event)
            return {"id": event_id, "deleted": True}

    def _manual_values(self, owner_id: str, data: dict) -> dict:
        title = str(data.get("title", "")).strip()
        precision = data.get("date_precision", "unknown")
        if not title or precision not in {"exact", "date_only", "unknown"}:
            raise ValidationError("標題或日期精度無效。")
        starts_at, event_date = _dt(data.get("starts_at")), _date(data.get("event_date"))
        if precision == "exact" and starts_at is None:
            raise ValidationError("精確事件需要 starts_at。")
        if precision == "date_only" and event_date is None:
            raise ValidationError("日期事件需要 event_date。")
        if precision == "unknown":
            starts_at = event_date = None
        group_id = data.get("group_id")
        if group_id:
            with self.db.session() as session:
                if not session.scalar(select(m.CourseGroup.id).where(m.CourseGroup.id == group_id, m.CourseGroup.owner_id == owner_id)):
                    raise NotFoundError("找不到課程。")
        return {"group_id": group_id, "kind": data.get("kind", "exam"), "title": title, "starts_at": starts_at, "event_date": event_date, "date_precision": precision, "source_timezone": data.get("source_timezone"), "date_original": data.get("date_original"), "source_announcement_id": data.get("source_announcement_id"), "source_url": data.get("source_url")}

    def enqueue_sync(self, owner_id: str, platforms: list[str] | str | None = None) -> list[dict]:
        wanted = [platforms] if isinstance(platforms, str) else platforms
        with self.db.session() as session:
            query = select(m.PlatformConnection).where(m.PlatformConnection.owner_id == owner_id, m.PlatformConnection.auth_state != "not_connected")
            if wanted:
                query = query.where(m.PlatformConnection.platform.in_(wanted))
            result = []
            for connection in session.scalars(query).all():
                active = session.scalar(select(m.SyncJob).where(m.SyncJob.connection_id == connection.id, m.SyncJob.generation == connection.generation, m.SyncJob.state.in_(("queued", "running", "waiting_user"))).order_by(m.SyncJob.scheduled_at))
                job = active or m.SyncJob(owner_id=owner_id, connection_id=connection.id, generation=connection.generation, preferences_version=connection.preferences_version, scheduled_at=self.clock())
                if active is None:
                    session.add(job)
                    session.flush()
                result.append(self._job(job, connection.platform))
            return result

    def get_sync_job(self, owner_id: str, job_id: str) -> dict:
        with self.db.session() as session:
            row = session.execute(select(m.SyncJob, m.PlatformConnection.platform).join(m.PlatformConnection).where(m.SyncJob.id == job_id, m.SyncJob.owner_id == owner_id)).one_or_none()
            if not row:
                raise NotFoundError("找不到同步工作。")
            return self._job(*row)

    @staticmethod
    def _job(job, platform=None):
        return {**_dict(job, exclude=("owner_id", "connection_id")), "platform": platform, "coverage": json.loads(job.coverage or "{}")}

    def create_notification(self, owner_id: str, *, dedupe_key: str, type: str, title: str, body: str, internal_path="/notifications", entity_id=None, entity_revision=None, scheduled_at=None, expires_at=None) -> dict:
        with self.db.session() as session:
            existing = session.scalar(select(m.NotificationEvent).where(m.NotificationEvent.owner_id == owner_id, m.NotificationEvent.dedupe_key == dedupe_key))
            if existing:
                notification = session.scalar(select(m.Notification).where(m.Notification.owner_id == owner_id, m.Notification.event_id == existing.id))
                return _dict(notification, exclude=("owner_id",))
            event = m.NotificationEvent(owner_id=owner_id, dedupe_key=dedupe_key, type=type, entity_id=entity_id, entity_revision=entity_revision, scheduled_at=scheduled_at or self.clock(), expires_at=expires_at)
            session.add(event); session.flush()
            notification = m.Notification(owner_id=owner_id, event_id=event.id, title=title, body=body, internal_path=internal_path)
            session.add(notification); session.flush()
            return _dict(notification, exclude=("owner_id",))

    def list_notifications(self, owner_id: str, *, unread_only=False, limit=50) -> list[dict]:
        with self.db.session() as session:
            query = select(m.Notification).where(m.Notification.owner_id == owner_id)
            if unread_only:
                query = query.where(m.Notification.read_at.is_(None))
            return [_dict(x, exclude=("owner_id",)) for x in session.scalars(query.order_by(m.Notification.created_at.desc()).limit(min(limit, 100))).all()]

    def get_digest(self, owner_id: str, digest_id: str) -> dict:
        with self.db.session() as session:
            digest = session.scalar(select(m.DigestSnapshot).where(m.DigestSnapshot.id == digest_id, m.DigestSnapshot.owner_id == owner_id))
            if not digest:
                raise NotFoundError("找不到摘要。")
            content = json.loads(digest.content)
            entries = []
            for entry in content.get("entries", []):
                value = dict(entry)
                value["internal_path"] = f"/todos?{'manual' if value.get('manual') else 'item'}={value['id']}"
                entries.append(value)
            return {
                "id": digest.id,
                "type": digest.type,
                "period_start": digest.period_start.isoformat(),
                "period_end": digest.period_end.isoformat(),
                "generated_at": digest.generated_at.isoformat(),
                "entries": entries,
                "count": content.get("count", len(entries)),
                "freshness_summary": json.loads(digest.freshness_summary or "{}"),
            }

    def mark_notification_read(self, owner_id: str, notification_id: str) -> dict:
        with self.db.session() as session:
            notification = session.scalar(select(m.Notification).where(m.Notification.id == notification_id, m.Notification.owner_id == owner_id))
            if not notification:
                raise NotFoundError("找不到通知。")
            notification.read_at = notification.read_at or self.clock()
            return _dict(notification, exclude=("owner_id",))

    def get_reminder_settings(self, owner_id: str) -> list[dict]:
        with self.db.session() as session:
            rows = session.scalars(select(m.ReminderRule).where(m.ReminderRule.owner_id == owner_id).order_by(m.ReminderRule.kind)).all()
            return [{**_dict(x, exclude=("owner_id",)), "offsets": json.loads(x.offsets)} for x in rows]

    def update_reminder_settings(self, owner_id: str, rules: list[dict] | dict) -> list[dict]:
        if isinstance(rules, dict):
            rules = rules.get("rules", [rules] if rules.get("kind") else [])
        allowed = {"deadline", "exam", "daily", "weekly", "connection"}
        with self.db.session() as session:
            for data in rules:
                if data.get("kind") not in allowed:
                    raise ValidationError("提醒類型無效。")
                if "local_time" in data and data["local_time"] is not None:
                    try:
                        hour, minute = map(int, data["local_time"].split(":"))
                        if not (0 <= hour <= 23 and 0 <= minute <= 59):
                            raise ValueError()
                    except (ValueError, AttributeError):
                        raise ValidationError("提醒時間格式無效。") from None
                if "weekday" in data and data["weekday"] is not None and not 0 <= int(data["weekday"]) <= 6:
                    raise ValidationError("星期設定無效。")
                if "offsets" in data:
                    if not isinstance(data["offsets"], list) or any(not isinstance(x, int) or x <= 0 or x > 24 * 30 for x in data["offsets"]):
                        raise ValidationError("提前提醒時數無效。")
                rule = session.scalar(select(m.ReminderRule).where(m.ReminderRule.owner_id == owner_id, m.ReminderRule.kind == data["kind"]))
                if rule is None:
                    rule = m.ReminderRule(owner_id=owner_id, kind=data["kind"]); session.add(rule)
                for field in ("enabled", "local_time", "weekday", "quiet_start", "quiet_end", "privacy_mode"):
                    if field in data:
                        setattr(rule, field, data[field])
                if "offsets" in data:
                    rule.offsets = json.dumps(data["offsets"])
            # Settings are configuration, not history. Rebuild only unsent outbox
            # rows; delivered notifications and their dedupe evidence remain intact.
            pending = session.scalars(select(m.NotificationEvent).where(m.NotificationEvent.owner_id == owner_id, m.NotificationEvent.state == "pending", m.NotificationEvent.type.in_(("item_due", "manual_event_due", "daily_digest", "weekly_digest")))).all()
            for event in pending:
                session.delete(event)
        return self.get_reminder_settings(owner_id)

    def system_status(self, owner_id: str) -> dict:
        now = self.clock()
        with self.db.session() as session:
            heartbeat = session.scalar(select(m.WorkerHeartbeat).order_by(m.WorkerHeartbeat.last_seen_at.desc()))
            pending = session.scalar(select(func.count()).select_from(m.SyncJob).where(m.SyncJob.owner_id == owner_id, m.SyncJob.state == "queued")) or 0
            last_seen = heartbeat.last_seen_at if heartbeat else None
            if last_seen and last_seen.tzinfo is None:
                last_seen = last_seen.replace(tzinfo=timezone.utc)
            return {"database": "ready" if self.db.ready() else "not_ready", "worker": "healthy" if last_seen and now - last_seen <= timedelta(seconds=90) else "stopped", "worker_last_seen_at": _iso(last_seen), "active_job_count": heartbeat.active_job_count if heartbeat else 0, "pending_jobs": pending}


__all__ = ["ConflictError", "GoneError", "NotFoundError", "Service", "ServiceError", "ServiceUnavailableError", "ValidationError"]
