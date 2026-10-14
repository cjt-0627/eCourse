from __future__ import annotations

import hashlib
import hmac
import io
import json
import os
import sqlite3
import stat
import tarfile
import tempfile
from contextlib import closing, contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


FORMAT = "ecourse-backup"
FORMAT_VERSION = 1
MANIFEST_NAME = "manifest.json"
DATABASE_NAME = "ecourse.sqlite3"
ALLOWED_MEMBERS = {MANIFEST_NAME, DATABASE_NAME}
KEY_CHECK_CONTEXT = b"ecourse-backup-key-check-v1"
MAX_MANIFEST_BYTES = 64 * 1024
MAX_DATABASE_BYTES = 16 * 1024 * 1024 * 1024


class BackupError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class BackupInfo:
    path: Path
    created_at: datetime
    database_sha256: str
    database_bytes: int
    schema_revision: str


@dataclass(frozen=True, slots=True)
class RestoreInfo:
    database_path: Path
    restored_at: datetime
    schema_revision: str


def create_backup(settings: Any, destination: Path) -> BackupInfo:
    """Create an atomic 0600 archive from a SQLite online-backup snapshot."""
    source = Path(settings.data_dir) / DATABASE_NAME
    destination = Path(destination)
    if destination.exists() or destination.is_symlink():
        raise BackupError("destination_exists", "備份檔已存在；拒絕覆寫。")
    _require_regular(source, "database_unavailable")
    key = _read_master_key(Path(settings.secret_dir))
    destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if destination.parent.is_symlink():
        raise BackupError("unsafe_destination", "備份目的目錄不可為符號連結。")

    created_at = datetime.now(UTC)
    with tempfile.TemporaryDirectory(prefix=".ecourse-backup-", dir=destination.parent) as temporary:
        temporary_path = Path(temporary)
        os.chmod(temporary_path, 0o700)
        snapshot = temporary_path / DATABASE_NAME
        _online_snapshot(source, snapshot)
        digest, size = _sha256(snapshot)
        revision = _schema_revision(snapshot)
        manifest = {
            "format": FORMAT,
            "version": FORMAT_VERSION,
            "created_at": created_at.isoformat(),
            "schema_revision": revision,
            "key_check": hmac.new(key, KEY_CHECK_CONTEXT, hashlib.sha256).hexdigest(),
            "files": {DATABASE_NAME: {"sha256": digest, "size": size}},
        }
        archive_tmp = temporary_path / "archive.tmp"
        with tarfile.open(archive_tmp, "w:gz", format=tarfile.PAX_FORMAT) as archive:
            _add_bytes(archive, MANIFEST_NAME, json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(), created_at)
            _add_file(archive, snapshot, DATABASE_NAME, created_at)
        os.chmod(archive_tmp, 0o600)
        with archive_tmp.open("rb") as stream:
            os.fsync(stream.fileno())
        try:
            # The temporary directory is below destination.parent, so a hard
            # link publishes the fully fsynced inode atomically and refuses a
            # racing destination without overwriting it.
            os.link(archive_tmp, destination, follow_symlinks=False)
        except FileExistsError:
            raise BackupError("destination_exists", "備份檔已存在；拒絕覆寫。") from None
        except OSError as exc:
            raise BackupError("backup_write_failed", "無法原子建立備份檔。") from exc
        _fsync_directory(destination.parent)
    return BackupInfo(destination, created_at, digest, size, revision)


def restore_backup(settings: Any, archive_path: Path) -> RestoreInfo:
    """Verify and restore into an empty data directory, leaving services paused."""
    archive_path = Path(archive_path)
    _require_regular(archive_path, "backup_unavailable")
    data_dir = Path(settings.data_dir)
    if data_dir.is_symlink():
        raise BackupError("unsafe_data_dir", "資料目錄不可為符號連結。")
    if data_dir.exists() and (not data_dir.is_dir() or any(data_dir.iterdir())):
        raise BackupError("data_dir_not_empty", "還原只允許空的資料目錄。")
    key = _read_master_key(Path(settings.secret_dir))

    with _read_archive(archive_path) as archive:
        members = archive.getmembers()
        names = [member.name for member in members]
        if set(names) != ALLOWED_MEMBERS or len(names) != len(ALLOWED_MEMBERS):
            raise BackupError("invalid_archive", "備份檔案清單不符合白名單。")
        if any(not member.isfile() or member.issym() or member.islnk() for member in members):
            raise BackupError("invalid_archive", "備份只能包含普通檔案。")
        manifest_member = archive.getmember(MANIFEST_NAME)
        database_member = archive.getmember(DATABASE_NAME)
        if manifest_member.size > MAX_MANIFEST_BYTES or database_member.size > MAX_DATABASE_BYTES:
            raise BackupError("invalid_archive", "備份內容大小超過限制。")
        manifest = _read_manifest(archive, manifest_member)
        expected_key_check = hmac.new(key, KEY_CHECK_CONTEXT, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(manifest["key_check"], expected_key_check):
            raise BackupError("wrong_master_key", "目前 secret_dir 的主金鑰與備份不符。")
        expected = manifest["files"][DATABASE_NAME]
        if database_member.size != expected["size"]:
            raise BackupError("checksum_mismatch", "資料庫大小與 manifest 不符。")
        with tempfile.TemporaryDirectory(prefix=".ecourse-restore-", dir=data_dir.parent) as temporary:
            temporary_path = Path(temporary)
            os.chmod(temporary_path, 0o700)
            restored = temporary_path / DATABASE_NAME
            source = archive.extractfile(database_member)
            if source is None:
                raise BackupError("invalid_archive", "備份缺少資料庫內容。")
            fd = os.open(restored, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as output, source:
                while chunk := source.read(1024 * 1024):
                    output.write(chunk)
                output.flush()
                os.fsync(output.fileno())
            digest, size = _sha256(restored)
            if size != expected["size"] or not hmac.compare_digest(digest, expected["sha256"]):
                raise BackupError("checksum_mismatch", "資料庫雜湊與 manifest 不符。")
            if _schema_revision(restored) != manifest["schema_revision"]:
                raise BackupError("schema_mismatch", "資料庫 migration 版本與 manifest 不符。")
            _prepare_restored_database(restored)
            data_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
            if data_dir.is_symlink() or any(data_dir.iterdir()):
                raise BackupError("data_dir_not_empty", "還原期間資料目錄已被寫入。")
            os.chmod(data_dir, 0o700)
            target = data_dir / DATABASE_NAME
            try:
                os.link(restored, target, follow_symlinks=False)
            except FileExistsError:
                raise BackupError("data_dir_not_empty", "還原目標已存在；拒絕覆寫。") from None
            os.chmod(target, 0o600)
            _fsync_directory(data_dir)
    return RestoreInfo(data_dir / DATABASE_NAME, datetime.now(UTC), manifest["schema_revision"])


def _read_master_key(secret_dir: Path) -> bytes:
    try:
        if secret_dir.is_symlink() or secret_dir.stat().st_mode & 0o077:
            raise BackupError("unsafe_secret_dir", "secret_dir 必須為 0700 且不可為符號連結。")
        fd = os.open(secret_dir / "master-v1.key", os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077:
                raise BackupError("unsafe_master_key", "主金鑰必須為 0600 普通檔案。")
            key = stream.read(33)
    except BackupError:
        raise
    except OSError:
        raise BackupError("master_key_unavailable", "缺少原主金鑰；不會自動建立。") from None
    if len(key) != 32:
        raise BackupError("invalid_master_key", "主金鑰格式錯誤。")
    return key


def _online_snapshot(source: Path, destination: Path) -> None:
    try:
        with closing(sqlite3.connect(f"file:{source}?mode=ro", uri=True)) as original, closing(sqlite3.connect(destination)) as snapshot:
            original.backup(snapshot)
            if snapshot.execute("PRAGMA integrity_check").fetchone() != ("ok",):
                raise BackupError("database_corrupt", "SQLite snapshot 完整性檢查失敗。")
    except BackupError:
        raise
    except sqlite3.Error as exc:
        raise BackupError("database_unavailable", "無法建立 SQLite 一致快照。") from exc
    os.chmod(destination, 0o600)


def _prepare_restored_database(path: Path) -> None:
    now = datetime.now(UTC).isoformat()
    try:
        with closing(sqlite3.connect(path)) as connection:
            connection.execute("PRAGMA foreign_keys=ON")
            # The snapshot may remember WAL mode. The temporary WAL would not
            # follow the database through the final atomic rename, so force all
            # restore-safety mutations into the database file itself.
            connection.execute("PRAGMA journal_mode=DELETE")
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("UPDATE app_sessions SET revoked_at = COALESCE(revoked_at, ?)", (now,))
            tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if "school_login_exchanges" in tables:
                connection.execute(
                    "UPDATE school_login_exchanges SET consumed_at = COALESCE(consumed_at, ?)",
                    (now,),
                )
            connection.execute(
                "UPDATE login_attempts SET state='expired', challenge_id=NULL, challenge_type=NULL, expires_at=NULL "
                "WHERE state IN ('queued','authenticating','challenge_required')"
            )
            connection.execute(
                "UPDATE platform_connections SET auth_state='expired', error_code='restored_requires_reconnect' "
                "WHERE auth_state != 'not_connected'"
            )
            connection.execute(
                "UPDATE sync_jobs SET state='cancelled', error_code='restore_cancelled', finished_at=?, lease_owner=NULL, lease_until=NULL "
                "WHERE state IN ('queued','running','waiting_user')",
                (now,),
            )
            connection.execute("DELETE FROM worker_heartbeats")
            connection.commit()
            if connection.execute("PRAGMA integrity_check").fetchone() != ("ok",):
                raise BackupError("database_corrupt", "還原後 SQLite 完整性檢查失敗。")
    except BackupError:
        raise
    except sqlite3.Error as exc:
        raise BackupError("restore_failed", "無法安全重設還原後的執行狀態。") from exc


def _read_manifest(archive: tarfile.TarFile, member: tarfile.TarInfo) -> dict[str, Any]:
    stream = archive.extractfile(member)
    if stream is None:
        raise BackupError("invalid_manifest", "備份缺少 manifest。")
    try:
        manifest = json.loads(stream.read(MAX_MANIFEST_BYTES + 1))
        file_info = manifest["files"][DATABASE_NAME]
        if (
            manifest["format"] != FORMAT or manifest["version"] != FORMAT_VERSION
            or not isinstance(manifest["schema_revision"], str)
            or not isinstance(manifest["key_check"], str) or len(manifest["key_check"]) != 64
            or not isinstance(file_info["sha256"], str) or len(file_info["sha256"]) != 64
            or not isinstance(file_info["size"], int) or file_info["size"] < 1
        ):
            raise ValueError()
        return manifest
    except (json.JSONDecodeError, KeyError, TypeError, ValueError):
        raise BackupError("invalid_manifest", "備份 manifest 格式無效。") from None


@contextmanager
def _read_archive(path: Path):
    try:
        with tarfile.open(path, "r:gz") as archive:
            yield archive
    except BackupError:
        raise
    except (tarfile.TarError, OSError, EOFError):
        raise BackupError("invalid_archive", "備份壓縮檔已損毀或格式不符。") from None


def _schema_revision(path: Path) -> str:
    try:
        with closing(sqlite3.connect(f"file:{path}?mode=ro", uri=True)) as connection:
            revision = connection.execute("SELECT version_num FROM alembic_version").fetchone()
            if not revision or not isinstance(revision[0], str):
                raise BackupError("schema_unavailable", "資料庫缺少 migration 版本。")
            return revision[0]
    except BackupError:
        raise
    except sqlite3.Error as exc:
        raise BackupError("schema_unavailable", "無法讀取資料庫 migration 版本。") from exc


def _sha256(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


def _require_regular(path: Path, code: str) -> None:
    try:
        if path.is_symlink() or not path.is_file() or not stat.S_ISREG(path.stat().st_mode):
            raise BackupError(code, "檔案不存在或不是普通檔案。")
    except OSError:
        raise BackupError(code, "無法讀取檔案。") from None


def _add_bytes(archive: tarfile.TarFile, name: str, payload: bytes, created_at: datetime) -> None:
    info = tarfile.TarInfo(name)
    info.size, info.mode, info.mtime = len(payload), 0o600, int(created_at.timestamp())
    info.uid = info.gid = 0
    info.uname = info.gname = ""
    archive.addfile(info, io.BytesIO(payload))


def _add_file(archive: tarfile.TarFile, source: Path, name: str, created_at: datetime) -> None:
    info = tarfile.TarInfo(name)
    info.size, info.mode, info.mtime = source.stat().st_size, 0o600, int(created_at.timestamp())
    info.uid = info.gid = 0
    info.uname = info.gname = ""
    with source.open("rb") as stream:
        archive.addfile(info, stream)


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
