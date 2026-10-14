from datetime import UTC, datetime, timedelta

from ecourse.auth.service import (
    authenticate_session, create_session, reset_owner_password, verify_owner,
)
from ecourse.db.models import AppSession


def test_password_reset_revokes_all_devices_but_allows_new_login(backend):
    settings, db, owner = backend
    first, _ = create_session(db, owner.id, "desktop")
    second, _ = create_session(db, owner.id, "phone")
    reset_owner_password(settings, "new-synthetic-password")
    assert authenticate_session(db, first) is None
    assert authenticate_session(db, second) is None
    assert verify_owner(db, owner.login_name, "new-synthetic-password") is not None
    new_token, _ = create_session(db, owner.id)
    assert authenticate_session(db, new_token) is not None


def test_idle_and_absolute_session_limits_are_both_enforced(backend):
    _settings, db, owner = backend
    idle, idle_row = create_session(db, owner.id)
    expired, expired_row = create_session(db, owner.id)
    now = datetime.now(UTC)
    with db.session() as session:
        session.get(AppSession, idle_row.id).last_used_at = now - timedelta(days=8)
        session.get(AppSession, expired_row.id).expires_at = now - timedelta(seconds=1)
    assert authenticate_session(db, idle) is None
    assert authenticate_session(db, expired) is None
