from __future__ import annotations

import io
import json
import sqlite3
import tarfile
from pathlib import Path

import pytest

from ecourse.auth.service import create_session
from ecourse.backup import BackupError, create_backup, restore_backup
from ecourse.config import Settings
from ecourse.db.models import LoginAttempt, PlatformConnection, SyncJob, WorkerHeartbeat
from ecourse.services import Service
from ecourse.vault import initialize_key


def _seed_runtime_state(settings, db, owner) -> None:
    create_session(db, owner.id, "backup-test")
    service = Service(settings)
    credential = service.save_credentials(owner.id, "backup-account", "backup-password")
    connected = service.connect(owner.id, "elearn", credential["id"])
    with db.session() as session:
        connection = session.get(PlatformConnection, connected["connection"]["id"])
        connection.auth_state = "challenge_required"
        attempt = session.query(LoginAttempt).filter(LoginAttempt.connection_id == connection.id).first()
        attempt.state = "challenge_required"
        attempt.challenge_id = "short-lived"
        session.add(WorkerHeartbeat(worker_id="old-worker", version="test"))


def _rewrite_archive(source: Path, destination: Path, transform) -> None:
    with tarfile.open(source, "r:gz") as archive:
        payloads = {member.name: archive.extractfile(member).read() for member in archive.getmembers()}
    payloads = transform(payloads)
    with tarfile.open(destination, "w:gz") as archive:
        for name, payload in payloads.items():
            info = tarfile.TarInfo(name)
            info.size = len(payload)
            info.mode = 0o600
            archive.addfile(info, io.BytesIO(payload))


def test_consistent_backup_and_safe_restore(backend, tmp_path) -> None:
    settings, db, owner = backend
    _seed_runtime_state(settings, db, owner)
    archive = tmp_path / "backup.tar.gz"
    info = create_backup(settings, archive)

    assert info.path == archive
    assert archive.stat().st_mode & 0o777 == 0o600
    with tarfile.open(archive, "r:gz") as bundle:
        assert {member.name for member in bundle.getmembers()} == {"manifest.json", "ecourse.sqlite3"}
        assert all("key" not in member.name and "token" not in member.name for member in bundle.getmembers())
        manifest = json.load(bundle.extractfile("manifest.json"))
        assert manifest["files"]["ecourse.sqlite3"]["sha256"] == info.database_sha256

    restored_data = tmp_path / "restored-data"
    restored = restore_backup(Settings(restored_data, settings.secret_dir), archive)
    assert restored.database_path == restored_data / "ecourse.sqlite3"
    assert restored.database_path.stat().st_mode & 0o777 == 0o600
    with sqlite3.connect(restored.database_path) as connection:
        assert connection.execute("SELECT count(*) FROM owners").fetchone()[0] == 1
        assert connection.execute("SELECT count(*) FROM credential_secrets").fetchone()[0] == 1
        assert connection.execute("SELECT count(*) FROM app_sessions WHERE revoked_at IS NULL").fetchone()[0] == 0
        assert connection.execute("SELECT state, challenge_id FROM login_attempts").fetchone() == ("expired", None)
        assert connection.execute("SELECT auth_state, error_code FROM platform_connections").fetchone() == ("expired", "restored_requires_reconnect")
        assert connection.execute("SELECT state, error_code FROM sync_jobs").fetchone() == ("cancelled", "restore_cancelled")
        assert connection.execute("SELECT count(*) FROM worker_heartbeats").fetchone()[0] == 0


def test_restore_rejects_wrong_master_key(backend, tmp_path) -> None:
    settings, _db, _owner = backend
    archive = tmp_path / "backup.tar.gz"
    create_backup(settings, archive)
    wrong_secret, key_seed, target = tmp_path / "wrong-secret", tmp_path / "key-seed", tmp_path / "target"
    initialize_key(wrong_secret, key_seed)
    with pytest.raises(BackupError, match="主金鑰") as error:
        restore_backup(Settings(target, wrong_secret), archive)
    assert error.value.code == "wrong_master_key"
    assert not target.exists()


def test_restore_rejects_checksum_tampering(backend, tmp_path) -> None:
    settings, _db, _owner = backend
    archive, tampered = tmp_path / "backup.tar.gz", tmp_path / "tampered.tar.gz"
    create_backup(settings, archive)
    _rewrite_archive(archive, tampered, lambda files: {**files, "ecourse.sqlite3": files["ecourse.sqlite3"] + b"tamper"})
    with pytest.raises(BackupError) as error:
        restore_backup(Settings(tmp_path / "target", settings.secret_dir), tampered)
    assert error.value.code in {"checksum_mismatch", "invalid_archive"}
    assert not (tmp_path / "target").exists()


def test_restore_rejects_non_whitelisted_or_traversal_members(backend, tmp_path) -> None:
    settings, _db, _owner = backend
    archive, malicious = tmp_path / "backup.tar.gz", tmp_path / "malicious.tar.gz"
    create_backup(settings, archive)
    _rewrite_archive(archive, malicious, lambda files: {**files, "../master-v1.key": b"not-a-key"})
    with pytest.raises(BackupError) as error:
        restore_backup(Settings(tmp_path / "target", settings.secret_dir), malicious)
    assert error.value.code == "invalid_archive"
    assert not (tmp_path / "master-v1.key").exists()


def test_restore_refuses_nonempty_data_directory(backend, tmp_path) -> None:
    settings, _db, _owner = backend
    archive = tmp_path / "backup.tar.gz"
    create_backup(settings, archive)
    target = tmp_path / "target"
    target.mkdir()
    marker = target / "keep.txt"
    marker.write_text("keep")
    with pytest.raises(BackupError) as error:
        restore_backup(Settings(target, settings.secret_dir), archive)
    assert error.value.code == "data_dir_not_empty"
    assert marker.read_text() == "keep"


def test_backup_refuses_overwrite_and_database_symlink(backend, tmp_path) -> None:
    settings, _db, _owner = backend
    archive = tmp_path / "backup.tar.gz"
    archive.write_bytes(b"existing")
    with pytest.raises(BackupError) as error:
        create_backup(settings, archive)
    assert error.value.code == "destination_exists"
    assert archive.read_bytes() == b"existing"

    fake_data = tmp_path / "fake-data"
    fake_data.mkdir()
    (fake_data / "ecourse.sqlite3").symlink_to(settings.data_dir / "ecourse.sqlite3")
    with pytest.raises(BackupError) as symlink_error:
        create_backup(Settings(fake_data, settings.secret_dir), tmp_path / "other.tar.gz")
    assert symlink_error.value.code == "database_unavailable"


def test_restore_rejects_malformed_archive_without_touching_target(backend, tmp_path) -> None:
    settings, _db, _owner = backend
    malformed = tmp_path / "malformed.tar.gz"
    malformed.write_bytes(b"not a gzip or tar archive")
    os_target = tmp_path / "target"
    with pytest.raises(BackupError) as error:
        restore_backup(Settings(os_target, settings.secret_dir), malformed)
    assert error.value.code == "invalid_archive"
    assert not os_target.exists()
