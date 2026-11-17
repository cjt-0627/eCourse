"""Plain-HTTP access to elearn pages for an already authenticated session.

Login still happens in a real browser (SSO, captcha).  Afterwards the stored
cookies are enough to read the server-rendered Moodle pages, which is far
faster than driving a browser for every page.

The elearn server is protected by an F5 BIG-IP / Shape Security JavaScript
challenge.  When a challenge page is detected, we solve it by running the
obfuscated scripts in Duktape (via dukpy) with a minimal browser-environment
mock, then replay the computed cookies back through httpx.

Anything that is not a normal page (an unsolvable challenge, an unexpected
status or host, a network failure) raises ``NeedsBrowser`` so the caller falls
back to the browser path.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import re
import weakref
from functools import partial
from typing import Any
from urllib.parse import parse_qs, urljoin, urlparse

import httpx

from .nthu_sso import ALLOWED_HOSTS, SSO_HOST

ELEARN_HOST = "elearn.nthu.edu.tw"
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
)
MAX_CONCURRENT_REQUESTS = 4
MAX_CHALLENGE_RETRIES = 3


class NeedsBrowser(Exception):
    """The page cannot be read over plain HTTP; use the browser instead."""


class SessionExpired(Exception):
    pass


class UpstreamTimeout(Exception):
    pass


_semaphores: "weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Semaphore]" = weakref.WeakKeyDictionary()


def _semaphore() -> asyncio.Semaphore:
    loop = asyncio.get_running_loop()
    semaphore = _semaphores.get(loop)
    if semaphore is None:
        semaphore = _semaphores[loop] = asyncio.Semaphore(MAX_CONCURRENT_REQUESTS)
    return semaphore


def _host_matches(cookie_domain: str, host: str) -> bool:
    domain = cookie_domain.lstrip(".").lower()
    return bool(domain) and (host == domain or host.endswith("." + domain))


# ---------------------------------------------------------------------------
# F5 challenge detection & solving
# ---------------------------------------------------------------------------

def is_challenge_page(html: str) -> bool:
    if len(html) < 10_000 and "bobcmn" in html and "/TSPD/" in html:
        return True
    if len(html) < 10_000 and "Please enable JavaScript" in html:
        return True
    return False


def _is_session_expired_page(html: str, final_url: str) -> bool:
    parsed = urlparse(final_url)
    if parsed.hostname == SSO_HOST or "/login/" in parsed.path:
        return True
    if parsed.hostname != ELEARN_HOST:
        return False
    if (
        parsed.scheme == "https"
        and parsed.path == "/"
        and parse_qs(parsed.query) == {"redirect": ["0"]}
        and not parsed.fragment
    ):
        return True
    if "body.notloggedin" in html or 'class="notloggedin"' in html:
        return True
    if re.search(r'href="[^"]*?/auth/oauth2/login\.php\?id=5[&"]', html):
        return True
    return False


def _parse_js_cookies(cookie_strings: list[str]) -> dict[str, str]:
    cookies: dict[str, str] = {}
    for cookie_str in cookie_strings:
        kv = cookie_str.split(";")[0]
        if "=" not in kv:
            continue
        name, _, value = kv.partition("=")
        name = name.strip()
        value = value.strip()
        if not name or not value or "expires" in name.lower():
            continue
        if value == "0" or name == "TSURLCK_test":
            continue
        cookies[name] = value
    return cookies


# The JS mock and runner scripts are large string constants; keep them in a
# helper module to avoid cluttering this file.  Inline them on first import.
_MOCK_BROWSER_JS: str | None = None
_RUNNER_PHASE1: str | None = None
_RUNNER_PHASE2: str | None = None


def _load_js_constants() -> None:
    global _MOCK_BROWSER_JS, _RUNNER_PHASE1, _RUNNER_PHASE2
    if _MOCK_BROWSER_JS is not None:
        return
    from ecourse.adapters._f5_challenge_js import MOCK_BROWSER_JS, RUNNER_PHASE1, RUNNER_PHASE2
    _MOCK_BROWSER_JS = MOCK_BROWSER_JS
    _RUNNER_PHASE1 = RUNNER_PHASE1
    _RUNNER_PHASE2 = RUNNER_PHASE2


def _run_challenge_js(
    inline_scripts: list[str],
    type8_js: str,
    type12_js: str,
    ua: str,
) -> dict[str, Any]:
    """Execute the F5 challenge scripts in Duktape and return phase 1 results."""
    import dukpy

    _load_js_constants()

    mock = _MOCK_BROWSER_JS.replace("@@UA@@", ua)
    interp = dukpy.JSInterpreter()

    def _run(code: str, need_result: bool = False) -> str | None:
        js = code if need_result else code + '\n"ok";'
        return interp.evaljs(js)

    _run(mock)
    for sc in inline_scripts:
        _run(sc)
    if type12_js:
        _run(type12_js)
    _run(type8_js)

    raw1 = _run(_RUNNER_PHASE1, need_result=True)
    result1 = json.loads(raw1)

    type13_url = None
    for xhr in result1.get("xhr_calls", []):
        if "type=13" in xhr.get("url", ""):
            type13_url = xhr["url"]
            break

    cookies = _parse_js_cookies(result1.get("cookies", []))
    return {
        "type13_url": type13_url,
        "cookies": cookies,
        "has_pending": result1.get("has_pending", False),
        "interp": interp,
    }


def _run_challenge_phase2(
    interp,
    status_code: int,
    response_text: str,
) -> dict[str, str]:
    """Feed the type=13 response back into the JS callback."""
    _load_js_constants()

    escaped = response_text.replace("\\", "\\\\").replace('"', '\\"')
    phase2_js = _RUNNER_PHASE2.replace("@@STATUS@@", str(status_code))
    phase2_js = phase2_js.replace("@@RESPONSE@@", escaped)

    raw2 = interp.evaljs(phase2_js)
    result2 = json.loads(raw2)
    return _parse_js_cookies(result2.get("cookies", []))


# ---------------------------------------------------------------------------
# HTTP client
# ---------------------------------------------------------------------------

class ElearnHttp:
    """One authenticated HTTP session for elearn; create per fetch call and ``aclose`` it."""

    def __init__(
        self,
        storage_state: dict[str, Any],
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout: float = 25.0,
    ):
        jar = httpx.Cookies()
        fingerprint = hashlib.sha256()
        count = 0
        for cookie in storage_state.get("cookies", []):
            if not _host_matches(str(cookie.get("domain", "")), ELEARN_HOST):
                continue
            jar.set(
                cookie["name"], cookie["value"],
                domain=cookie["domain"], path=cookie.get("path", "/"),
            )
            fingerprint.update(f"{cookie['name']}={cookie['value']};".encode())
            count += 1
        self.usable = count > 0
        self.fingerprint = fingerprint.hexdigest()[:16]
        self._client = httpx.AsyncClient(
            cookies=jar,
            transport=transport,
            timeout=timeout,
            follow_redirects=True,
            headers={
                "User-Agent": USER_AGENT,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "zh-TW,zh;q=0.9,en-US;q=0.8,en;q=0.7",
                "Accept-Encoding": "gzip, deflate, br",
                "Connection": "keep-alive",
                "Upgrade-Insecure-Requests": "1",
            },
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    @staticmethod
    def check_url(url: str) -> None:
        parsed = urlparse(url)
        if parsed.scheme != "https" or parsed.hostname != ELEARN_HOST:
            raise ValueError("unexpected_http_url")

    async def get_text(self, url: str) -> tuple[str, str]:
        """Return ``(html, final_url)`` for a normal elearn page.

        Transparently solves F5 BIG-IP challenges when encountered.
        """
        self.check_url(url)
        async with _semaphore():
            try:
                response = await self._client.get(url)
            except httpx.TimeoutException as exc:
                raise UpstreamTimeout from exc
            except httpx.HTTPError as exc:
                raise NeedsBrowser from exc

        final = str(response.url)
        final_parsed = urlparse(final)
        if final_parsed.hostname == SSO_HOST or "/login/" in final_parsed.path:
            raise SessionExpired
        if final_parsed.hostname not in ALLOWED_HOSTS or final_parsed.hostname != ELEARN_HOST:
            raise NeedsBrowser
        if response.status_code != 200:
            raise NeedsBrowser

        html = response.text
        if not is_challenge_page(html):
            if _is_session_expired_page(html, final):
                raise SessionExpired
            return html, final

        # F5 challenge detected — solve it
        html = await self._solve_challenge(url, html)
        final = url  # after solving, the final URL is the originally requested one
        if _is_session_expired_page(html, final):
            raise SessionExpired
        return html, final

    async def _solve_challenge(self, url: str, challenge_html: str) -> str:
        """Solve the F5 BIG-IP challenge and return the real page HTML."""
        base_origin = re.match(r"https?://[^/]+", url).group(0)
        loop = asyncio.get_running_loop()

        for attempt in range(MAX_CHALLENGE_RETRIES):
            # Phase 0: parse the challenge page for script references
            script_blocks = re.findall(
                r"<script[^>]*>\s*(.*?)\s*</script>", challenge_html, re.DOTALL,
            )
            inline_scripts = [s for s in script_blocks if s.strip() and "src=" not in s]

            type8_m = re.search(r'src="(/TSPD/[^"]+type=8)"', challenge_html)
            type12_m = re.search(r'src="(/TSPD/[^"]+type=12)"', challenge_html)

            if not type8_m:
                raise NeedsBrowser

            # Fetch external challenge scripts
            async with _semaphore():
                try:
                    type8_resp = await self._client.get(
                        base_origin + type8_m.group(1),
                    )
                    type8_js = type8_resp.text
                except (httpx.TimeoutException, httpx.HTTPError):
                    raise NeedsBrowser

            type12_js = ""
            if type12_m:
                async with _semaphore():
                    try:
                        type12_resp = await self._client.get(
                            base_origin + type12_m.group(1),
                        )
                        type12_js = type12_resp.text
                    except (httpx.TimeoutException, httpx.HTTPError):
                        raise NeedsBrowser

            # Phase 1: run scripts in Duktape (CPU-bound, offload to thread)
            try:
                result = await loop.run_in_executor(
                    None,
                    partial(
                        _run_challenge_js,
                        inline_scripts, type8_js, type12_js, USER_AGENT,
                    ),
                )
            except Exception:
                raise NeedsBrowser

            # Apply cookies from phase 1
            for name, value in result["cookies"].items():
                self._client.cookies.set(
                    name, value, domain=f".{ELEARN_HOST}", path="/",
                )

            # Phase 2: if there's a pending type=13 XHR, make it and feed response back
            if result.get("has_pending") and result.get("type13_url"):
                full_url = base_origin.rstrip("/") + result["type13_url"]
                async with _semaphore():
                    try:
                        r13 = await self._client.get(full_url)
                    except (httpx.TimeoutException, httpx.HTTPError):
                        raise NeedsBrowser

                try:
                    phase2_cookies = await loop.run_in_executor(
                        None,
                        partial(
                            _run_challenge_phase2,
                            result["interp"],
                            r13.status_code,
                            r13.text,
                        ),
                    )
                except Exception:
                    raise NeedsBrowser

                for name, value in phase2_cookies.items():
                    self._client.cookies.set(
                        name, value, domain=f".{ELEARN_HOST}", path="/",
                    )

            # Retry the original page with the solved cookies
            async with _semaphore():
                try:
                    response = await self._client.get(url)
                except httpx.TimeoutException as exc:
                    raise UpstreamTimeout from exc
                except httpx.HTTPError as exc:
                    raise NeedsBrowser from exc

            html = response.text
            if not is_challenge_page(html):
                return html

        # Exhausted retries
        raise NeedsBrowser
