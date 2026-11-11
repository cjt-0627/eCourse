"""Plain-HTTP access to eeclass pages for an already authenticated session.

Login still happens in a real browser (SSO, captcha).  Afterwards the stored
cookies are enough to read the server-rendered course pages, which is far faster
than driving a browser for every page.

Anything that is not a normal page (a bot-challenge page, an unexpected status or
host, a network failure) raises ``NeedsBrowser`` so the caller falls back to the
browser path.  This module never tries to get past such a challenge.
"""
from __future__ import annotations

import asyncio
import hashlib
import time
import weakref
from typing import Any
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup, NavigableString, Tag

from .nthu_sso import ALLOWED_HOSTS, SSO_HOST

EECLASS_HOST = "eeclass.nthu.edu.tw"
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
)
# Upper bound on simultaneous requests to the school server from this process.
MAX_CONCURRENT_REQUESTS = 6
PAGE_CACHE_SECONDS = 60.0


class NeedsBrowser(Exception):
    """The page cannot be read over plain HTTP; use the browser instead."""


class SessionExpired(Exception):
    pass


class UpstreamTimeout(Exception):
    pass


class RateLimited(Exception):
    pass


_semaphores: "weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Semaphore]" = weakref.WeakKeyDictionary()
_locks: "weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, dict]" = weakref.WeakKeyDictionary()
_page_cache: dict[tuple[str, str], tuple[float, str, str]] = {}


def _semaphore() -> asyncio.Semaphore:
    loop = asyncio.get_running_loop()
    semaphore = _semaphores.get(loop)
    if semaphore is None:
        semaphore = _semaphores[loop] = asyncio.Semaphore(MAX_CONCURRENT_REQUESTS)
    return semaphore


def _host_matches(cookie_domain: str, host: str) -> bool:
    domain = cookie_domain.lstrip(".").lower()
    return bool(domain) and (host == domain or host.endswith("." + domain))


def is_challenge_page(html: str) -> bool:
    return len(html) < 20_000 and (
        "Please enable JavaScript to view the page content" in html or "Your support ID is" in html
    )


def has_official_login_link(html: str) -> bool:
    if "/service/oauthRedir" not in html:
        return False
    soup = BeautifulSoup(html, "html.parser")
    for link in soup.select("a[href*='/service/oauthRedir']"):
        target = urlparse(urljoin(f"https://{EECLASS_HOST}/", str(link.get("href") or "")))
        if target.scheme == "https" and target.hostname == EECLASS_HOST and target.path.lower() == "/service/oauthredir":
            return True
    return False


_BLOCK_TAGS = frozenset({
    "address", "article", "aside", "blockquote", "dd", "div", "dl", "dt", "fieldset", "figure",
    "footer", "form", "h1", "h2", "h3", "h4", "h5", "h6", "header", "hr", "li", "main", "nav",
    "ol", "p", "pre", "section", "table", "tbody", "thead", "tfoot", "ul",
})
_SKIPPED_TAGS = frozenset({"script", "style", "noscript", "template", "head"})


def _flatten(node: Tag) -> str:
    return " ".join(node.get_text(" ").split())


def _emit(node, out: list[str]) -> None:
    if isinstance(node, NavigableString):
        if type(node).__name__ in {"Comment", "Doctype", "CData", "ProcessingInstruction", "Declaration"}:
            return
        out.append(str(node))
        return
    if not isinstance(node, Tag) or node.name in _SKIPPED_TAGS:
        return
    if node.name == "br":
        out.append("\n")
        return
    if node.name == "tr":
        cells = [_flatten(cell) for cell in node.find_all(["td", "th"], recursive=False)]
        out.append("\n" + "\t".join(cell for cell in cells if cell) + "\n")
        return
    block = node.name in _BLOCK_TAGS
    if block:
        out.append("\n")
    for child in node.children:
        _emit(child, out)
    if block:
        out.append("\n")


def html_to_text(node: Tag | BeautifulSoup, *, keep_paragraph_breaks: bool = False) -> str:
    """Approximate a browser's ``innerText``: block elements and table rows become lines.

    ``keep_paragraph_breaks`` keeps one blank line between paragraphs (for prose);
    the default drops blank lines, which is what label/value parsing wants.
    """
    out: list[str] = []
    _emit(node, out)
    lines: list[str] = []
    for raw in "".join(out).splitlines():
        line = "\t".join(" ".join(part.split()) for part in raw.split("\t")).strip("\t ")
        if line:
            lines.append(line)
        elif keep_paragraph_breaks and lines and lines[-1]:
            lines.append("")
    return "\n".join(lines).strip()


class EeclassHttp:
    """One authenticated HTTP session; create per fetch call and ``aclose`` it."""

    def __init__(self, storage_state: dict[str, Any], *, transport: httpx.AsyncBaseTransport | None = None, timeout: float = 20.0):
        jar = httpx.Cookies()
        fingerprint = hashlib.sha256()
        count = 0
        for cookie in storage_state.get("cookies", []):
            if not _host_matches(str(cookie.get("domain", "")), EECLASS_HOST):
                continue  # never send SSO or other hosts' cookies to eeclass
            jar.set(cookie["name"], cookie["value"], domain=cookie["domain"], path=cookie.get("path", "/"))
            fingerprint.update(f"{cookie['name']}={cookie['value']};".encode())
            count += 1
        self.usable = count > 0
        self.fingerprint = fingerprint.hexdigest()[:16]
        self._client = httpx.AsyncClient(
            cookies=jar, transport=transport, timeout=timeout, follow_redirects=True,
            headers={"User-Agent": USER_AGENT, "Accept-Language": "zh-TW,zh;q=0.9,en;q=0.8", "Accept": "text/html,application/xhtml+xml"},
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    @staticmethod
    def check_url(url: str) -> None:
        parsed = urlparse(url)
        if parsed.scheme != "https" or parsed.hostname != EECLASS_HOST:
            raise ValueError("unexpected_http_url")

    async def get_text(self, url: str) -> tuple[str, str]:
        """Return ``(html, final_url)`` for a normal eeclass page."""
        self.check_url(url)
        async with _semaphore():
            try:
                response = await self._client.get(url)
            except httpx.TimeoutException as exc:
                raise UpstreamTimeout from exc
            except httpx.HTTPError as exc:
                raise NeedsBrowser from exc
        final = response.url
        if final.host == SSO_HOST or "/service/oauth" in final.path.lower():
            raise SessionExpired
        if final.host not in ALLOWED_HOSTS or final.host != EECLASS_HOST:
            raise NeedsBrowser
        if response.status_code == 429:
            raise RateLimited
        if response.status_code != 200:
            raise NeedsBrowser
        html = response.text
        if is_challenge_page(html):
            raise NeedsBrowser
        if has_official_login_link(html):
            raise SessionExpired
        return html, str(final)

    async def get_cached_text(self, url: str) -> tuple[str, str]:
        """Like ``get_text`` but shares one recent copy among concurrent callers.

        Every announcement of a course is located on the same list page.
        """
        key = (self.fingerprint, url)
        locks = _locks.setdefault(asyncio.get_running_loop(), {})
        lock = locks.setdefault(key, asyncio.Lock())
        async with lock:
            hit = _page_cache.get(key)
            if hit and time.monotonic() - hit[0] < PAGE_CACHE_SECONDS:
                return hit[1], hit[2]
            html, final = await self.get_text(url)
            _page_cache[key] = (time.monotonic(), html, final)
            if len(_page_cache) > 64:
                for stale in sorted(_page_cache, key=lambda item: _page_cache[item][0])[:16]:
                    _page_cache.pop(stale, None)
            return html, final
