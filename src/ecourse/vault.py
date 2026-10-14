"""P0 encrypted storage. No network, school login or automatic key replacement."""

import base64
import hashlib
import hmac
import json
import os
import stat
import tempfile
from pathlib import Path

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM


class VaultError(Exception):
    pass


def private_directory(path: Path) -> None:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.is_symlink() or not path.is_dir() or path.stat().st_mode & 0o077:
        raise VaultError("目錄必須只允許擁有者存取（0700），且不可為符號連結。")


def initialize_key(secret_dir: Path, data_dir: Path) -> Path:
    """Explicit initialization only; never replace missing keys over existing data."""
    private_directory(secret_dir)
    private_directory(data_dir)
    key_path = secret_dir / "master-v1.key"
    if key_path.exists() or key_path.is_symlink():
        raise VaultError("主金鑰已存在；不會覆寫。")
    if any(data_dir.iterdir()):
        raise VaultError("資料目錄非空；拒絕建立新金鑰，請先恢復原金鑰。")
    descriptor = os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(AESGCM.generate_key(bit_length=256))
        stream.flush()
        os.fsync(stream.fileno())
    return key_path


class Vault:
    def __init__(self, secret_dir: Path):
        # Reading must not implicitly initialize anything.
        try:
            if secret_dir.is_symlink() or secret_dir.stat().st_mode & 0o077:
                raise VaultError("金鑰目錄權限必須為 0700。")
            fd = os.open(secret_dir / "master-v1.key", os.O_RDONLY | os.O_NOFOLLOW)
            with os.fdopen(fd, "rb") as stream:
                info = os.fstat(stream.fileno())
                if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077:
                    raise VaultError("金鑰必須為僅擁有者可讀寫的普通檔案（0600）。")
                key = stream.read(33)
            if len(key) != 32:
                raise VaultError("主金鑰格式錯誤。")
        except OSError:
            raise VaultError("無法讀取主金鑰；請初始化或恢復原金鑰，不會自動重建。") from None
        self._cipher = AESGCM(key)
        self._account_index_key = hmac.new(key, b"ecourse-account-fingerprint-v1", hashlib.sha256).digest()

    def account_fingerprint(self, account: str, *, owner_id: str) -> str:
        """Return an owner-scoped blind index without exposing the vault key."""
        normalized = account.strip().casefold()
        if not normalized or not owner_id.strip():
            raise VaultError("帳號指紋命名空間無效。")
        message = b"ecourse-account-v1\0" + owner_id.encode() + b"\0" + normalized.encode()
        return hmac.new(self._account_index_key, message, hashlib.sha256).hexdigest()

    @staticmethod
    def _context(platform: str, account_id: str, purpose: str = "session") -> bytes:
        if platform not in {"elearn", "eeclass"} or not account_id.strip():
            raise VaultError("平台或帳號命名空間無效。")
        if purpose not in {"session", "credential", "push", "ipc"}:
            raise VaultError("加密資料用途無效。")
        return json.dumps([f"ecourse-{purpose}", 1, platform, account_id], separators=(",", ":")).encode()

    def seal(self, state: dict, *, platform: str, account_id: str, purpose: str = "session") -> bytes:
        aad = self._context(platform, account_id, purpose)
        nonce = os.urandom(12)
        plaintext = json.dumps(state, ensure_ascii=False, allow_nan=False).encode()
        ciphertext = self._cipher.encrypt(nonce, plaintext, aad)
        return json.dumps({
            "version": 1,
            "key_version": 1,
            "nonce": base64.b64encode(nonce).decode(),
            "ciphertext": base64.b64encode(ciphertext).decode(),
        }).encode()

    def unseal(self, envelope: bytes, *, platform: str, account_id: str, purpose: str = "session") -> dict:
        aad = self._context(platform, account_id, purpose)
        try:
            record = json.loads(envelope)
            if record["version"] != 1 or record["key_version"] != 1:
                raise ValueError()
            nonce = base64.b64decode(record["nonce"], validate=True)
            if len(nonce) != 12:
                raise ValueError()
            ciphertext = base64.b64decode(record["ciphertext"], validate=True)
            state = json.loads(self._cipher.decrypt(nonce, ciphertext, aad))
            if not isinstance(state, dict):
                raise ValueError()
            return state
        except (ValueError, KeyError, TypeError, InvalidTag):
            raise VaultError("登入狀態無法解密：金鑰、平台、帳號不符，或資料已損毀。") from None

    def save(self, path: Path, state: dict, *, platform: str, account_id: str, purpose: str = "session") -> None:
        envelope = self.seal(state, platform=platform, account_id=account_id, purpose=purpose)
        private_directory(path.parent)
        fd, temporary = tempfile.mkstemp(prefix=".session-", dir=path.parent)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(envelope)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally:
            Path(temporary).unlink(missing_ok=True)

    def load(self, path: Path, *, platform: str, account_id: str, purpose: str = "session") -> dict:
        try:
            envelope = path.read_bytes()
        except OSError:
            raise VaultError("無法讀取已保存的登入狀態。") from None
        return self.unseal(envelope, platform=platform, account_id=account_id, purpose=purpose)
