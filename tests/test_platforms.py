import asyncio
import base64
import hashlib
import json
from pathlib import Path

import pytest

from ecourse.managed_browser import ManagedPlatformBrowser
from ecourse.platforms import managed_platform_url


def test_managed_platform_url_defaults_and_strict_host_binding():
    assert managed_platform_url("elearn") == "https://elearn.nthu.edu.tw/my/index.php"
    assert managed_platform_url("eeclass") == "https://eeclass.nthu.edu.tw/dashboard"
    assert managed_platform_url("elearn", "https://elearn.nthu.edu.tw/course/view.php?id=1")
    for value in (
        "http://elearn.nthu.edu.tw/",
        "https://elearn.nthu.edu.tw.evil.test/",
        "https://user@elearn.nthu.edu.tw/",
        "https://eeclass.nthu.edu.tw/dashboard",
    ):
        with pytest.raises(ValueError):
            managed_platform_url("elearn", value)


def test_managed_browser_loads_state_into_visible_isolated_context():
    calls = []

    class Page:
        url = ""
        def set_default_timeout(self, value): calls.append(("timeout", value))
        def on(self, event, callback): calls.append(("event", event))
        async def goto(self, url, **kwargs): self.url = url; calls.append(("goto", url, kwargs))
        async def bring_to_front(self): calls.append(("front",))

    class Context:
        def __init__(self): self.page = Page()
        async def new_page(self): return self.page
        async def close(self): calls.append(("context-close",))

    class Browser:
        def is_connected(self): return True
        async def new_context(self, **kwargs): calls.append(("context", kwargs)); return Context()
        async def close(self): calls.append(("browser-close",))

    class BrowserType:
        async def launch(self, **kwargs): calls.append(("launch", kwargs)); return Browser()

    async def scenario():
        managed = ManagedPlatformBrowser(BrowserType())
        result = await managed.open({"cookies": [{"name": "session", "value": "opaque"}]}, "https://elearn.nthu.edu.tw/my/index.php")
        assert result == {"opened_url": "https://elearn.nthu.edu.tw/my/index.php", "session_expired": False}
        await managed.close()

    asyncio.run(scenario())
    assert ("launch", {"headless": False, "chromium_sandbox": True}) in calls
    assert any(call[0] == "context" and call[1]["storage_state"]["cookies"] for call in calls)
    assert any(call[0] == "goto" and call[2]["wait_until"] == "commit" for call in calls)


def test_brave_extension_manifest_has_stable_expected_id_and_narrow_hosts():
    root = Path(__file__).parents[1] / "brave-extension"
    manifest = json.loads((root / "manifest.json").read_text())
    public_key = base64.b64decode(manifest["key"])
    digest = hashlib.sha256(public_key).digest()[:16]
    extension_id = "".join(
        chr(ord("a") + (byte >> 4)) + chr(ord("a") + (byte & 15))
        for byte in digest
    )
    assert extension_id == "amecmikkjicakbcboajnfmbhekmkpahk"
    assert set(manifest["host_permissions"]) == {
        "http://localhost:8765/*", "http://127.0.0.1:8765/*",
        "https://elearn.nthu.edu.tw/*", "https://eeclass.nthu.edu.tw/*",
    }
    assert "cookies" in manifest["permissions"]
    assert (root / "background.js").is_file() and (root / "bridge.js").is_file()
    background = (root / "background.js").read_text()
    assert 'url: `https://${host}${path}`' in background
    assert '"http" : "https"' not in background
    assert "chrome.cookies.getAll" in background
    assert "written.value !== cookie.value" in background
    assert "isRequiredSessionCookie(cookie)" in background
    assert "return false" in background
    assert "clearPlatformCookies" in background
    assert 'cookie.domain || ""' in background
    assert 'url: `https://${host}${cookie.path || "/"}`' in background
    assert "chrome.cookies.getAll(query)" in background
    assert 'name.startsWith("moodlesession")' in background
    assert "cookie.partitionKey" in background
    assert "chrome.cookies.getAllCookieStores" in background
    assert "consumeHandoff(message.handoffToken, sender.tab)" in background
    assert "details.storeId = storeId" in background
    assert 'searchParams.set("ecourse_handoff", crypto.randomUUID())' in background
    assert 'cookie.secure ? "https" : "http"' not in background
    assert "verifyCookieHandoff" not in background
