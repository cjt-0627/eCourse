from __future__ import annotations

import hashlib
import hmac
import secrets
from datetime import UTC, datetime, timedelta
from urllib.parse import urlsplit

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError
from sqlalchemy import update

from ecourse.db import Database
from ecourse.db.models import AppSession, CredentialSecret, LoginAttempt, Owner, PlatformConnection, SchoolLoginExchange


_hasher = PasswordHasher(time_cost=3, memory_cost=65536, parallelism=4)
IDLE_LIFETIME = timedelta(days=7)
ABSOLUTE_LIFETIME = timedelta(days=30)
SCHOOL_EXCHANGE_LIFETIME = timedelta(minutes=10)
SCHOOL_AUTH_DISABLED_PASSWORD = "!school-auth-disabled-v1!"


def _now() -> datetime:
    return datetime.now(UTC)


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def initialize_owner(settings, login_name: str, password: str) -> Owner:
    """Create the single local owner. The database schema must already exist."""
    login_name = login_name.strip()
    if not login_name or len(login_name) > 120:
        raise ValueError("登入名稱不可空白，且不可超過 120 個字元。")
    if len(password) < 12:
        raise ValueError("密碼至少需要 12 個字元。")
    db = Database(settings)
    with db.session() as session:
        if session.query(Owner).first() is not None:
            raise ValueError("擁有者帳號已建立；請使用密碼重設指令。")
        owner = Owner(login_name=login_name, password_hash=_hasher.hash(password), timezone="Asia/Taipei", created_at=_now())
        session.add(owner)
        session.flush()
        return owner


def initialize_internal_owner(settings) -> Owner:
    """Create the non-login local owner used before first school verification."""
    db = Database(settings)
    with db.session() as session:
        existing = session.query(Owner).first()
        if existing is not None:
            session.expunge(existing)
            return existing
        owner = Owner(
            login_name="school-owner",
            password_hash=SCHOOL_AUTH_DISABLED_PASSWORD,
            timezone="Asia/Taipei",
            created_at=_now(),
        )
        session.add(owner)
        session.flush()
        session.expunge(owner)
        return owner


def reset_owner_password(settings, password: str) -> Owner:
    """Replace the sole owner's password and revoke every app session."""
    if len(password) < 12:
        raise ValueError("密碼至少需要 12 個字元。")
    db = Database(settings)
    with db.session() as session:
        owner = session.query(Owner).first()
        if owner is None:
            raise ValueError("尚未建立擁有者帳號。")
        if owner.password_hash in {None, SCHOOL_AUTH_DISABLED_PASSWORD}:
            raise ValueError("已啟用校務帳密登入，不可重新啟用獨立本機密碼。")
        owner.password_hash = _hasher.hash(password)
        session.query(AppSession).filter(AppSession.owner_id == owner.id, AppSession.revoked_at.is_(None)).update({AppSession.revoked_at: _now()})
        session.flush()
        session.expunge(owner)
        return owner


def verify_owner(db: Database, login_name: str, password: str) -> Owner | None:
    """Return one generic failure result so callers cannot enumerate accounts."""
    with db.session() as session:
        owner = session.query(Owner).filter(Owner.login_name == login_name.strip()).first()
        encoded = owner.password_hash if owner and owner.password_hash not in {None, SCHOOL_AUTH_DISABLED_PASSWORD} else _hasher.hash(secrets.token_urlsafe(24))
        try:
            valid = _hasher.verify(encoded, password)
        except (VerifyMismatchError, InvalidHashError):
            valid = False
        if not owner or not valid:
            return None
        session.expunge(owner)
        return owner


def create_session(db: Database, owner_id, device_label: str | None = None) -> tuple[str, AppSession]:
    now = _now()
    raw = secrets.token_urlsafe(48)
    app_session = AppSession(
        owner_id=owner_id,
        token_hash=token_hash(raw),
        csrf_token=secrets.token_urlsafe(32),
        created_at=now,
        last_used_at=now,
        expires_at=now + ABSOLUTE_LIFETIME,
        device_label=(device_label or "瀏覽器")[:120],
    )
    with db.session() as session:
        session.add(app_session)
        session.flush()
        session.expunge(app_session)
    return raw, app_session


def authenticate_session(db: Database, raw_token: str | None) -> tuple[Owner, AppSession] | None:
    if not raw_token:
        return None
    now = _now()
    with db.session() as session:
        app_session = session.query(AppSession).filter(AppSession.token_hash == token_hash(raw_token)).first()
        if not app_session or app_session.revoked_at is not None:
            return None
        expires = app_session.expires_at
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=UTC)
        if expires <= now:
            return None
        last_used = app_session.last_used_at
        if last_used.tzinfo is None:
            last_used = last_used.replace(tzinfo=UTC)
        if last_used + IDLE_LIFETIME <= now:
            app_session.revoked_at = now
            return None
        owner = session.query(Owner).filter(Owner.id == app_session.owner_id).first()
        if owner is None:
            return None
        app_session.last_used_at = now
        session.flush()
        session.expunge(owner)
        session.expunge(app_session)
        return owner, app_session


def revoke_session(db: Database, session_id, owner_id) -> bool:
    with db.session() as session:
        item = session.query(AppSession).filter(AppSession.id == session_id, AppSession.owner_id == owner_id).first()
        if not item:
            return False
        item.revoked_at = _now()
        return True


def begin_school_auth(settings, account: str, password: str, *, platform: str = "elearn", device_label: str | None = None) -> dict:
    """Start school verification without issuing an application session."""
    from ecourse.services import Service

    account = account.strip()
    if not account or not password or platform not in {"elearn", "eeclass"}:
        raise ValueError("校務登入資料無效。")
    db = Database(settings)
    with db.session() as session:
        owner = session.query(Owner).order_by(Owner.created_at).first()
        if owner is None:
            local_host = urlsplit(settings.base_url).hostname in {"localhost", "127.0.0.1", "::1"}
            if settings.environment != "local" or not local_host:
                raise ValueError("正式環境尚未安全初始化擁有者。")
            owner = Owner(login_name="school-owner", password_hash=None, timezone="Asia/Taipei", created_at=_now())
            session.add(owner)
            session.flush()
        elif session.query(CredentialSecret.id).filter(
            CredentialSecret.owner_id == owner.id,
            CredentialSecret.verified_at.is_not(None),
        ).first() is not None:
            # Existing installations authenticate against the encrypted saved
            # credential. Public callers must never rotate an active connection.
            raise ValueError("校務登入失敗。")
        owner_id = owner.id
    service = Service(settings)
    credential = service.save_credentials(owner_id, account, password, verified=False)
    started = service.connect(owner_id, platform, credential["id"])
    raw_exchange = secrets.token_urlsafe(48)
    expires_at = _now() + SCHOOL_EXCHANGE_LIFETIME
    with db.session() as session:
        session.add(SchoolLoginExchange(
            owner_id=owner_id,
            attempt_id=started["attempt_id"],
            credential_id=credential["id"],
            token_hash=token_hash(raw_exchange),
            created_at=_now(),
            expires_at=expires_at,
            device_label=(device_label or "瀏覽器")[:120],
        ))
    return {
        "exchange_token": raw_exchange,
        "attempt_id": started["attempt_id"],
        "state": "queued",
        "expires_at": expires_at.isoformat(),
    }


def authenticate_school_credential(settings, account: str, password: str) -> Owner | None:
    """Verify the sole owner's saved encrypted school credential in memory."""
    from ecourse.vault import Vault, VaultError

    account = account.strip()
    if not account or not password:
        return None
    db = Database(settings)
    with db.session() as session:
        owner = session.query(Owner).order_by(Owner.created_at).first()
        if owner is None:
            return None
        records = session.query(CredentialSecret).filter(
            CredentialSecret.owner_id == owner.id,
            CredentialSecret.verified_at.is_not(None),
        ).order_by(CredentialSecret.updated_at.desc()).all()
        owner_id = owner.id
        session.expunge(owner)
    try:
        vault = Vault(settings.secret_dir)
        candidate_fingerprint = vault.account_fingerprint(account, owner_id=owner_id)
        matched = False
        for record in records:
            saved = vault.unseal(
                record.encrypted_payload, platform="elearn",
                account_id=owner_id, purpose="credential",
            )
            saved_account = str(saved.get("account", "")).strip().casefold()
            fingerprint_matches = bool(record.account_fingerprint) and hmac.compare_digest(record.account_fingerprint, candidate_fingerprint)
            account_matches = hmac.compare_digest(saved_account.encode(), account.casefold().encode())
            password_matches = hmac.compare_digest(str(saved.get("password", "")).encode(), password.encode())
            saved.clear()
            matched |= fingerprint_matches and account_matches and password_matches
        return owner if matched else None
    except (VaultError, UnicodeError):
        return None
    return None


# Backward-compatible internal spelling while web routes adopt the explicit API.
authenticate_school_secret = authenticate_school_credential


def school_auth_status(settings, exchange_token: str) -> dict:
    db = Database(settings)
    with db.session() as session:
        exchange = _active_exchange(session, exchange_token)
        attempt = session.get(LoginAttempt, exchange.attempt_id)
        if not attempt:
            raise ValueError("校務登入交易無效或已過期。")
        return {
            "attempt_id": attempt.id,
            "state": attempt.state,
            "challenge_id": attempt.challenge_id,
            "challenge_type": attempt.challenge_type,
            "challenge_expires_at": attempt.expires_at.isoformat() if attempt.expires_at else None,
            "expires_at": exchange.expires_at.isoformat(),
        }


async def school_auth_challenge_image(settings, exchange_token: str) -> bytes:
    from ecourse.services import Service

    owner_id, attempt_id = _exchange_identity(settings, exchange_token)
    return await Service(settings).get_challenge_image(owner_id, attempt_id)


async def answer_school_auth_challenge(settings, exchange_token: str, answer: str, request_id: str) -> dict:
    from ecourse.services import Service

    owner_id, attempt_id = _exchange_identity(settings, exchange_token)
    return await Service(settings).submit_challenge(owner_id, attempt_id, answer, request_id)


async def refresh_school_auth_challenge(settings, exchange_token: str) -> dict:
    from ecourse.services import Service

    owner_id, attempt_id = _exchange_identity(settings, exchange_token)
    return await Service(settings).refresh_challenge(owner_id, attempt_id)


def complete_school_auth(settings, exchange_token: str) -> tuple[str, AppSession]:
    """Atomically consume a verified exchange and issue one browser session."""
    db = Database(settings)
    raw_session = secrets.token_urlsafe(48)
    now = _now()
    with db.session() as session:
        exchange = _active_exchange(session, exchange_token)
        attempt = session.get(LoginAttempt, exchange.attempt_id)
        connection = session.get(PlatformConnection, attempt.connection_id) if attempt else None
        if (
            not attempt or not connection or attempt.owner_id != exchange.owner_id
            or attempt.state != "succeeded" or connection.generation != attempt.generation
            or connection.auth_state != "connected"
        ):
            raise ValueError("校務身分尚未驗證完成。")
        consumed = session.execute(update(SchoolLoginExchange).where(
            SchoolLoginExchange.id == exchange.id,
            SchoolLoginExchange.consumed_at.is_(None),
        ).values(consumed_at=now))
        if consumed.rowcount != 1:
            raise ValueError("校務登入交易無效或已過期。")
        owner = session.get(Owner, exchange.owner_id)
        # Keep legacy SQLite schemas with NOT NULL password_hash compatible
        # without rebuilding the parent table (which can cascade-delete all
        # child data). The sentinel is deliberately not a valid Argon2 hash.
        owner.password_hash = SCHOOL_AUTH_DISABLED_PASSWORD
        credential = session.get(CredentialSecret, exchange.credential_id)
        if not credential:
            raise ValueError("校務登入交易無效或已過期。")
        credential.verified_at = now
        app_session = AppSession(
            owner_id=owner.id,
            token_hash=token_hash(raw_session),
            csrf_token=secrets.token_urlsafe(32),
            created_at=now,
            last_used_at=now,
            expires_at=now + ABSOLUTE_LIFETIME,
            device_label=exchange.device_label,
        )
        session.add(app_session)
        session.flush()
        session.expunge(app_session)
    return raw_session, app_session


def _exchange_identity(settings, exchange_token: str) -> tuple[str, str]:
    db = Database(settings)
    with db.session() as session:
        exchange = _active_exchange(session, exchange_token)
        return exchange.owner_id, exchange.attempt_id


def _active_exchange(session, raw_token: str) -> SchoolLoginExchange:
    if not raw_token:
        raise ValueError("校務登入交易無效或已過期。")
    exchange = session.query(SchoolLoginExchange).filter(
        SchoolLoginExchange.token_hash == token_hash(raw_token),
    ).first()
    if not exchange or exchange.consumed_at is not None or exchange.expires_at <= _now():
        raise ValueError("校務登入交易無效或已過期。")
    return exchange
