import asyncio
import sqlite3

import pytest
from sqlalchemy import select

from ecourse.auth.service import (
    answer_school_auth_challenge,
    authenticate_school_credential,
    authenticate_session,
    begin_school_auth,
    complete_school_auth,
    reset_owner_password,
    school_auth_challenge_image,
    school_auth_status,
    verify_owner,
    SCHOOL_AUTH_DISABLED_PASSWORD,
)
from ecourse.config import Settings
from ecourse.db import Database
from ecourse.db.models import LoginAttempt, Owner, PlatformConnection, SchoolLoginExchange
from ecourse.services import Service


def _mark_verified(db, attempt_id):
    with db.session() as session:
        attempt = session.get(LoginAttempt, attempt_id)
        attempt.state = "succeeded"
        connection = session.get(PlatformConnection, attempt.connection_id)
        connection.auth_state = "connected"
        connection.remote_account_id = "verified-profile"


def test_school_credentials_create_session_only_after_worker_verification(backend):
    settings, db, owner = backend
    started = begin_school_auth(
        settings, "student-id", "school-password", device_label="test browser",
    )
    assert started["state"] == "queued"
    assert authenticate_school_credential(settings, "student-id", "school-password") is None
    assert school_auth_status(settings, started["exchange_token"])["state"] == "queued"
    with pytest.raises(ValueError, match="尚未驗證"):
        complete_school_auth(settings, started["exchange_token"])

    _mark_verified(db, started["attempt_id"])
    raw_session, app_session = complete_school_auth(settings, started["exchange_token"])
    assert authenticate_school_credential(settings, "student-id", "school-password").id == owner.id
    authenticated = authenticate_session(db, raw_session)
    assert authenticated is not None and authenticated[0].id == owner.id
    assert app_session.device_label == "test browser"
    assert verify_owner(db, owner.login_name, "a-long-local-password") is None
    with db.session() as session:
        assert session.get(Owner, owner.id).password_hash == SCHOOL_AUTH_DISABLED_PASSWORD
    with pytest.raises(ValueError, match="不可重新啟用"):
        reset_owner_password(settings, "another-local-password")
    with pytest.raises(ValueError, match="無效或已過期"):
        complete_school_auth(settings, started["exchange_token"])


def test_school_password_and_exchange_token_never_persist_as_plaintext(backend):
    settings, db, owner = backend
    started = begin_school_auth(settings, "private-student", "private-school-password")
    database_bytes = settings.data_dir.joinpath("ecourse.sqlite3").read_bytes()
    assert b"private-school-password" not in database_bytes
    assert started["exchange_token"].encode() not in database_bytes
    with db.session() as session:
        exchange = session.scalar(select(SchoolLoginExchange))
        assert exchange.token_hash != started["exchange_token"]
    loaded = Service(settings).worker_load_credentials(owner.id, session_connection_credential(db, started["attempt_id"]))
    assert loaded == {
        "account": "private-student",
        "password": "private-school-password",
        "account_fingerprint": loaded["account_fingerprint"],
    }


def test_existing_school_credential_authenticates_without_mutating_connections(backend):
    settings, db, owner = backend
    service = Service(settings)
    credential = service.save_credentials(owner.id, "student-id", "school-password")
    connected = service.connect(owner.id, "elearn", credential["id"])
    with db.session() as session:
        connection = session.get(PlatformConnection, connected["connection"]["id"])
        before = (connection.credential_id, connection.generation, connection.auth_state)
    assert authenticate_school_credential(settings, " STUDENT-ID ", "school-password").id == owner.id
    assert authenticate_school_credential(settings, "student-id", "wrong-password") is None
    assert authenticate_school_credential(settings, "other-account", "school-password") is None
    with pytest.raises(ValueError, match="校務登入失敗"):
        begin_school_auth(settings, "other-account", "attacker-password")
    with db.session() as session:
        connection = session.get(PlatformConnection, connected["connection"]["id"])
        after = (connection.credential_id, connection.generation, connection.auth_state)
        assert after == before
        assert session.query(SchoolLoginExchange).count() == 0


def test_authenticated_settings_replaces_credentials_and_reconnects_both_platforms(backend):
    settings, db, owner = backend
    service = Service(settings)
    first = service.save_credentials(owner.id, "old-account", "old-password")
    service.connect(owner.id, "elearn", first["id"])
    updated = service.replace_credentials_and_reconnect(owner.id, "new-account", "new-password")
    assert set(updated["attempts"]) == {"elearn", "eeclass"}
    credential_id = updated["credential"]["id"]
    assert service.worker_load_credentials(owner.id, credential_id)["password"] == "new-password"
    with db.session() as session:
        connections = session.scalars(select(PlatformConnection).where(PlatformConnection.owner_id == owner.id)).all()
        assert len(connections) == 2
        assert all(connection.credential_id == credential_id for connection in connections)


def session_connection_credential(db, attempt_id):
    with db.session() as session:
        attempt = session.get(LoginAttempt, attempt_id)
        return session.get(PlatformConnection, attempt.connection_id).credential_id


def test_school_auth_exchange_scopes_challenge_ipc_to_bound_owner_and_attempt(backend):
    settings, _db, _owner = backend

    class IPC:
        async def challenge_image(self, owner_id, attempt_id, challenge_id):
            self.bound = (owner_id, attempt_id, challenge_id)
            return b"captcha"

        async def answer(self, owner_id, attempt_id, challenge_id, answer, request_id):
            assert answer == "1234" and request_id == "request-1"
            return {"accepted": True, "attempt_id": attempt_id}

    started = begin_school_auth(settings, "student-id", "school-password")
    ipc = IPC()
    # Service normally discovers the running worker from its protected token;
    # inject the same boundary here without exposing owner_id to the caller.
    original = Service.__init__

    def configured(self, service_settings, **kwargs):
        original(self, service_settings, ipc_client=ipc, **kwargs)

    from unittest.mock import patch
    with patch.object(Service, "__init__", configured):
        assert asyncio.run(school_auth_challenge_image(settings, started["exchange_token"])) == b"captcha"
        assert asyncio.run(answer_school_auth_challenge(
            settings, started["exchange_token"], "1234", "request-1",
        ))["accepted"]


def test_production_cannot_claim_uninitialized_owner(tmp_path):
    settings = Settings(
        tmp_path / "data", tmp_path / "secrets",
        base_url="https://course.example", allowed_hosts=("course.example",), environment="production",
    )
    from ecourse.vault import initialize_key
    initialize_key(settings.secret_dir, settings.data_dir)
    Database(settings).initialize()
    with pytest.raises(ValueError, match="尚未安全初始化"):
        begin_school_auth(settings, "student", "password")
    with Database(settings).session() as session:
        assert session.scalar(select(Owner)) is None


def test_0004_upgrade_preserves_legacy_owner_child_tree(tmp_path):
    from ecourse.vault import initialize_key

    settings = Settings(tmp_path / "data", tmp_path / "secrets")
    initialize_key(settings.secret_dir, settings.data_dir)
    database_path = settings.data_dir / "ecourse.sqlite3"
    with sqlite3.connect(database_path) as connection:
        connection.executescript("""
            PRAGMA foreign_keys=ON;
            CREATE TABLE alembic_version (version_num VARCHAR(32) PRIMARY KEY);
            INSERT INTO alembic_version VALUES ('0004');
            CREATE TABLE owners (
                id VARCHAR(36) PRIMARY KEY, login_name VARCHAR(160) NOT NULL UNIQUE,
                password_hash TEXT NOT NULL, timezone VARCHAR(64) NOT NULL, created_at DATETIME NOT NULL
            );
            CREATE TABLE app_sessions (
                id VARCHAR(36) PRIMARY KEY, owner_id VARCHAR(36) NOT NULL REFERENCES owners(id) ON DELETE CASCADE,
                token_hash VARCHAR(128) NOT NULL UNIQUE, csrf_token VARCHAR(128) NOT NULL,
                created_at DATETIME NOT NULL, last_used_at DATETIME NOT NULL, expires_at DATETIME NOT NULL,
                revoked_at DATETIME, device_label VARCHAR(160)
            );
            CREATE TABLE credential_secrets (
                id VARCHAR(36) PRIMARY KEY, owner_id VARCHAR(36) NOT NULL REFERENCES owners(id) ON DELETE CASCADE,
                encrypted_payload BLOB NOT NULL, account_hint VARCHAR(32) NOT NULL,
                account_fingerprint VARCHAR(64), key_version INTEGER NOT NULL, updated_at DATETIME
            );
            CREATE TABLE platform_connections (
                id VARCHAR(36) PRIMARY KEY, owner_id VARCHAR(36) NOT NULL REFERENCES owners(id) ON DELETE CASCADE,
                platform VARCHAR(32) NOT NULL, credential_id VARCHAR(36) REFERENCES credential_secrets(id) ON DELETE SET NULL,
                remote_account_id VARCHAR(255), identity_fingerprint VARCHAR(128), auth_state VARCHAR(32) NOT NULL,
                generation INTEGER NOT NULL, preferences_version INTEGER NOT NULL,
                last_verified_at DATETIME, last_success_at DATETIME, error_code VARCHAR(64)
            );
            CREATE TABLE login_attempts (
                id VARCHAR(36) PRIMARY KEY, owner_id VARCHAR(36) NOT NULL REFERENCES owners(id) ON DELETE CASCADE,
                connection_id VARCHAR(36) NOT NULL REFERENCES platform_connections(id) ON DELETE CASCADE,
                generation INTEGER NOT NULL, state VARCHAR(32), challenge_id VARCHAR(64), challenge_type VARCHAR(32),
                expires_at DATETIME, attempt_count INTEGER, created_at DATETIME
            );
            CREATE TABLE course_sources (
                id VARCHAR(36) PRIMARY KEY, owner_id VARCHAR(36) NOT NULL REFERENCES owners(id) ON DELETE CASCADE,
                connection_id VARCHAR(36) NOT NULL REFERENCES platform_connections(id) ON DELETE CASCADE,
                remote_course_id VARCHAR(255), title VARCHAR(300)
            );
            CREATE TABLE learning_items (
                id VARCHAR(36) PRIMARY KEY, owner_id VARCHAR(36) NOT NULL REFERENCES owners(id) ON DELETE CASCADE,
                source_course_id VARCHAR(36) NOT NULL REFERENCES course_sources(id) ON DELETE CASCADE,
                remote_item_id VARCHAR(255), kind VARCHAR(32), title VARCHAR(500)
            );
            INSERT INTO owners VALUES ('owner','legacy','argon-hash','Asia/Taipei','2026-01-01');
            INSERT INTO app_sessions VALUES ('session','owner','token','csrf','2026-01-01','2026-01-01','2027-01-01',NULL,'browser');
            INSERT INTO credential_secrets VALUES ('credential','owner',X'0102','ab***cd','fingerprint',1,'2026-01-01');
            INSERT INTO platform_connections VALUES ('connection','owner','elearn','credential','remote','fingerprint','connected',3,2,NULL,NULL,NULL);
            INSERT INTO login_attempts VALUES ('attempt','owner','connection',3,'succeeded',NULL,NULL,NULL,1,'2026-01-01');
            INSERT INTO course_sources VALUES ('course','owner','connection','remote-course','Course');
            INSERT INTO learning_items VALUES ('item','owner','course','remote-item','assignment','Item');
        """)
    Database(settings).initialize()
    with sqlite3.connect(database_path) as connection:
        for table in ("owners", "app_sessions", "credential_secrets", "platform_connections", "login_attempts", "course_sources", "learning_items"):
            assert connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 1
        assert connection.execute("SELECT credential_id FROM platform_connections").fetchone()[0] == "credential"
        assert connection.execute("SELECT verified_at IS NOT NULL FROM credential_secrets").fetchone()[0] == 1
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone()[0] == "0008"
        assert connection.execute("SELECT locale FROM owners").fetchone()[0] == "zh-Hant"
