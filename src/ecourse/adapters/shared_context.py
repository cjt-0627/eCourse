"""Share one browser context among the pages of a sync.

Opening a fresh context for every page throws away the browser cache, so each
page re-downloads the same scripts and stylesheets.  Pages of one sync lease a
shared context instead; it is closed after it has been idle for a while.  Login
flows do not use this: they keep their own isolated contexts.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
from typing import Any

IDLE_SECONDS = 45.0
MAX_CONTEXTS = 4


class _Entry:
    __slots__ = ("context", "loop", "users", "timer", "broken")

    def __init__(self, context, loop):
        self.context = context
        self.loop = loop
        self.users = 0
        self.timer = None
        self.broken = False


class LeasedContext:
    """Looks like a browser context to the caller; ``close`` only returns the lease."""

    def __init__(self, pool: "SharedContextPool", key: str, entry: _Entry):
        self._pool, self._key, self._entry = pool, key, entry
        self._pages: list[Any] = []
        self._released = False

    async def new_page(self):
        try:
            page = await self._entry.context.new_page()
        except Exception:
            self._entry.broken = True  # a dead context must not be handed out again
            raise
        self._pages.append(page)
        return page

    async def close(self) -> None:
        if self._released:
            return
        self._released = True
        for page in self._pages:
            try:
                await page.close()
            except Exception:
                pass
        self._pool._release(self._key, self._entry)


class SharedContextPool:
    def __init__(self, idle_seconds: float = IDLE_SECONDS, max_contexts: int = MAX_CONTEXTS):
        self.idle_seconds = idle_seconds
        self.max_contexts = max_contexts
        self._entries: dict[str, _Entry] = {}

    @staticmethod
    def _key(storage_state: dict[str, Any]) -> str:
        return hashlib.sha256(json.dumps(storage_state, sort_keys=True, default=str).encode()).hexdigest()

    async def lease(self, browser, storage_state: dict[str, Any]) -> LeasedContext:
        loop = asyncio.get_running_loop()
        key = self._key(storage_state)
        entry = self._entries.get(key)
        if entry is not None and (entry.broken or entry.loop is not loop):
            self._entries.pop(key, None)
            self._discard(entry)
            entry = None
        if entry is None:
            if len(self._entries) >= self.max_contexts:
                for old_key, old in list(self._entries.items()):
                    if old.users == 0:
                        self._entries.pop(old_key, None)
                        self._discard(old)
                        break
            entry = _Entry(await browser.new_context(storage_state=storage_state), loop)
            self._entries[key] = entry
        if entry.timer is not None:
            entry.timer.cancel()
            entry.timer = None
        entry.users += 1
        return LeasedContext(self, key, entry)

    def _release(self, key: str, entry: _Entry) -> None:
        entry.users -= 1
        if entry.users > 0:
            return
        if entry.broken or entry.loop.is_closed():
            self._discard_entry(key, entry)
            return
        entry.timer = entry.loop.call_later(self.idle_seconds, self._expire, key, entry)

    def _expire(self, key: str, entry: _Entry) -> None:
        if entry.users == 0:
            self._discard_entry(key, entry)

    def _discard_entry(self, key: str, entry: _Entry) -> None:
        if self._entries.get(key) is entry:
            del self._entries[key]
        self._discard(entry)

    @staticmethod
    def _discard(entry: _Entry) -> None:
        if entry.timer is not None:
            entry.timer.cancel()
            entry.timer = None
        if entry.loop.is_closed():
            return

        async def close():
            try:
                await entry.context.close()
            except Exception:
                pass

        try:
            asyncio.get_running_loop()
            running = True
        except RuntimeError:
            running = False
        if running and asyncio.get_running_loop() is entry.loop:
            entry.loop.create_task(close())
        else:
            entry.loop.call_soon_threadsafe(lambda: entry.loop.create_task(close()))
