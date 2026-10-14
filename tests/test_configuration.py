from pathlib import Path

import pytest

from ecourse.cli import configure_tailscale_serve, server_settings, tailscale_mobile_settings
from ecourse.config import Settings
from ecourse.vault import Vault, VaultError, initialize_key


@pytest.mark.parametrize("url", ["https://example.test/private", "https://user:pass@example.test", "http://example.test", "javascript:alert(1)", "https://example.test?query=1"])
def test_reject_unsafe_origin(tmp_path, url):
    with pytest.raises(ValueError):
        Settings(tmp_path / "data", tmp_path / "secrets", base_url=url, allowed_hosts=("example.test",))


def test_production_requires_https_and_exact_host(tmp_path):
    with pytest.raises(ValueError):
        Settings(tmp_path, tmp_path, environment="production")
    with pytest.raises(ValueError):
        Settings(tmp_path, tmp_path, base_url="https://example.test", allowed_hosts=("*",), environment="production")
    configured = Settings(tmp_path, tmp_path, base_url="https://example.test/", allowed_hosts=("example.test",), environment="production")
    assert configured.base_url == "https://example.test"


def test_local_server_port_updates_origin_and_forbids_public_bind(tmp_path):
    settings = Settings(tmp_path / "data", tmp_path / "secrets")
    updated, port = server_settings(settings, "127.0.0.1", 8976)
    assert updated.base_url == "http://localhost:8976"
    assert port == 8976
    with pytest.raises(ValueError):
        server_settings(settings, "0.0.0.0", 8765)


def test_mobile_settings_require_online_tailscale_and_use_private_https(tmp_path, monkeypatch):
    from types import SimpleNamespace
    settings = Settings(tmp_path / "data", tmp_path / "secrets")
    online = {
        "BackendState": "Running", "Self": {
            "Online": True, "DNSName": "my-mac.example-tailnet.ts.net.",
        }, "MagicDNSSuffix": "example-tailnet.ts.net",
    }
    monkeypatch.setattr("ecourse.cli.subprocess.run", lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout=__import__("json").dumps(online), stderr=""))
    configured = tailscale_mobile_settings(settings, executable="/usr/local/bin/tailscale")
    assert configured.base_url == "https://my-mac.example-tailnet.ts.net"
    assert configured.allowed_hosts == ("my-mac.example-tailnet.ts.net",)
    assert configured.environment == "production"

    offline = {"BackendState": "Stopped", "Self": {"Online": False}}
    monkeypatch.setattr("ecourse.cli.subprocess.run", lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout=__import__("json").dumps(offline), stderr=""))
    with pytest.raises(ValueError, match="尚未連線"):
        tailscale_mobile_settings(settings, executable="/usr/local/bin/tailscale")


def test_mobile_serve_proxies_only_to_loopback(monkeypatch):
    from types import SimpleNamespace
    calls = []
    monkeypatch.setattr("ecourse.cli.subprocess.run", lambda args, **kwargs: calls.append(args) or SimpleNamespace(returncode=0, stdout="", stderr=""))
    configure_tailscale_serve(8765, executable="/usr/local/bin/tailscale")
    assert calls == [["/usr/local/bin/tailscale", "serve", "--bg", "http://127.0.0.1:8765"]]


def test_ciphertext_cannot_cross_purposes(tmp_path):
    data, secrets = tmp_path / "data", tmp_path / "secrets"
    initialize_key(secrets, data)
    vault = Vault(secrets)
    encrypted = vault.seal({"account": "synthetic"}, platform="elearn", account_id="owner", purpose="credential")
    assert vault.unseal(encrypted, platform="elearn", account_id="owner", purpose="credential")["account"] == "synthetic"
    with pytest.raises(VaultError):
        vault.unseal(encrypted, platform="elearn", account_id="owner", purpose="session")
