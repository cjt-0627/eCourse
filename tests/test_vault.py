import base64
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from ecourse.config import Settings
from ecourse.vault import Vault, VaultError, initialize_key


@pytest.fixture
def storage(tmp_path):
    data, secrets = tmp_path / "data", tmp_path / "secrets"
    initialize_key(secrets, data)
    return data, secrets


def test_restart_reads_encrypted_state_in_separate_process(storage):
    data, secrets = storage
    state = {"cookies": [{"name": "session", "value": "private-test-value"}], "origins": []}
    saved = data / "session.enc"
    Vault(secrets).save(saved, state, platform="elearn", account_id="owner-1")
    assert b"private-test-value" not in saved.read_bytes()
    script = """
import sys
from pathlib import Path
from ecourse.vault import Vault
state = Vault(Path(sys.argv[1])).load(Path(sys.argv[2]), platform='elearn', account_id='owner-1')
assert state['cookies'][0]['value'] == 'private-test-value'
"""
    completed = subprocess.run([sys.executable, "-c", script, str(secrets), str(saved)], capture_output=True)
    assert completed.returncode == 0, completed.stderr.decode()
    assert saved.stat().st_mode & 0o777 == 0o600
    assert (secrets / "master-v1.key").stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize("platform,account", [("eeclass", "owner-1"), ("elearn", "owner-2")])
def test_wrong_identity_cannot_decrypt(storage, platform, account):
    _, secrets = storage
    vault = Vault(secrets)
    envelope = vault.seal({"cookies": []}, platform="elearn", account_id="owner-1")
    with pytest.raises(VaultError):
        vault.unseal(envelope, platform=platform, account_id=account)


def test_tampering_rejected(storage):
    _, secrets = storage
    vault = Vault(secrets)
    record = json.loads(vault.seal({"cookies": []}, platform="elearn", account_id="a"))
    damaged = bytearray(base64.b64decode(record["ciphertext"]))
    damaged[0] ^= 1
    record["ciphertext"] = base64.b64encode(damaged).decode()
    with pytest.raises(VaultError):
        vault.unseal(json.dumps(record).encode(), platform="elearn", account_id="a")


def test_fresh_nonce_for_every_save(storage):
    _, secrets = storage
    vault = Vault(secrets)
    assert vault.seal({}, platform="elearn", account_id="a") != vault.seal({}, platform="elearn", account_id="a")


def test_missing_key_never_recreated(storage):
    data, secrets = storage
    Vault(secrets).save(data / "session.enc", {}, platform="elearn", account_id="a")
    key = secrets / "master-v1.key"
    key.unlink()
    with pytest.raises(VaultError):
        Vault(secrets)
    with pytest.raises(VaultError):
        initialize_key(secrets, data)
    assert not key.exists()


def test_init_refuses_to_overwrite(storage):
    data, secrets = storage
    original = (secrets / "master-v1.key").read_bytes()
    with pytest.raises(VaultError):
        initialize_key(secrets, data)
    assert (secrets / "master-v1.key").read_bytes() == original


def test_wrong_key_rejected(storage, tmp_path):
    _, secrets = storage
    envelope = Vault(secrets).seal({}, platform="elearn", account_id="a")
    other = tmp_path / "other-secrets"
    initialize_key(other, tmp_path / "other-data")
    with pytest.raises(VaultError):
        Vault(other).unseal(envelope, platform="elearn", account_id="a")


def test_permissive_or_symlink_key_rejected(storage):
    _, secrets = storage
    key = secrets / "master-v1.key"
    key.chmod(0o644)
    with pytest.raises(VaultError):
        Vault(secrets)
    key.chmod(0o600)
    moved = secrets / "moved.key"
    key.rename(moved)
    key.symlink_to(moved)
    with pytest.raises(VaultError):
        Vault(secrets)


@pytest.mark.parametrize("envelope", [b"bad-json", b"[]", b"null", b"{}", b'{"version": 999}', b"\xff"])
def test_invalid_envelope_has_safe_error(storage, envelope):
    with pytest.raises(VaultError, match="無法解密"):
        Vault(storage[1]).unseal(envelope, platform="elearn", account_id="a")


def test_interrupted_save_keeps_previous_state(storage, monkeypatch):
    data, secrets = storage
    vault = Vault(secrets)
    saved = data / "session.enc"
    vault.save(saved, {"old": True}, platform="elearn", account_id="a")

    def failure(*args):
        raise OSError("simulated disk error")

    monkeypatch.setattr(os, "replace", failure)
    with pytest.raises(OSError):
        vault.save(saved, {"new": True}, platform="elearn", account_id="a")
    assert vault.load(saved, platform="elearn", account_id="a") == {"old": True}
    assert list(data.iterdir()) == [saved]


def test_relative_and_nested_configuration_rejected(monkeypatch, tmp_path):
    monkeypatch.setenv("ECOURSE_DATA_DIR", "relative")
    with pytest.raises(ValueError):
        Settings.from_env()
    monkeypatch.setenv("ECOURSE_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("ECOURSE_SECRET_DIR", str(tmp_path / "secrets"))
    with pytest.raises(ValueError):
        Settings.from_env()


def test_configuration_is_independent_of_working_directory(monkeypatch, tmp_path):
    monkeypatch.delenv("ECOURSE_DATA_DIR", raising=False)
    monkeypatch.delenv("ECOURSE_SECRET_DIR", raising=False)
    settings = Settings.from_env()
    monkeypatch.chdir(tmp_path)
    assert Settings.from_env() == settings
