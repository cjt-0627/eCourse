from __future__ import annotations

import asyncio
from urllib.parse import urlsplit


class ManagedPlatformBrowser:
    """A visible, worker-owned browser that never exports school cookies."""

    def __init__(self, browser_type, *, timeout_ms: int = 30_000, executable_path: str | None = None):
        self.browser_type = browser_type
        self.timeout_ms = timeout_ms
        self.executable_path = executable_path
        self.browser = None
        self.contexts = set()
        self._launch_lock = asyncio.Lock()

    async def open(self, storage_state: dict, target_url: str) -> dict:
        async with self._launch_lock:
            if self.browser is None or not self.browser.is_connected():
                options = {"headless": False, "chromium_sandbox": True}
                if self.executable_path:
                    options["executable_path"] = self.executable_path
                self.browser = await self.browser_type.launch(**options)
        context = await self.browser.new_context(storage_state=storage_state)
        self.contexts.add(context)
        page = await context.new_page()
        page.set_default_timeout(self.timeout_ms)

        def close_context(_page=None):
            try:
                asyncio.get_running_loop().create_task(self._close_context(context))
            except RuntimeError:
                pass

        page.on("close", close_context)
        try:
            # Bring up the visible window immediately. Waiting only for the
            # response commit lets the page continue rendering interactively.
            await page.bring_to_front()
            await page.goto(target_url, wait_until="commit")
        except Exception:
            await self._close_context(context)
            raise
        final = urlsplit(page.url)
        target = urlsplit(target_url)
        login_path = final.path.casefold()
        session_expired = (
            final.hostname != target.hostname
            or (target.hostname == "elearn.nthu.edu.tw" and "/login/" in login_path)
            or (target.hostname == "eeclass.nthu.edu.tw" and "/service/oauth" in login_path)
        )
        if session_expired:
            await self._close_context(context)
        return {"opened_url": page.url, "session_expired": session_expired}

    async def _close_context(self, context) -> None:
        if context not in self.contexts:
            return
        self.contexts.discard(context)
        try:
            await context.close()
        except Exception:
            pass

    async def close(self) -> None:
        for context in tuple(self.contexts):
            await self._close_context(context)
        if self.browser is not None:
            try:
                await self.browser.close()
            finally:
                self.browser = None
