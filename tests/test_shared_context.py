from __future__ import annotations

import asyncio

import pytest

from ecourse.adapters.shared_context import SharedContextPool


class Page:
    def __init__(self):
        self.closed = False

    async def close(self):
        self.closed = True


class Context:
    def __init__(self, fail=False):
        self.closed, self.pages, self.fail = False, [], fail

    async def new_page(self):
        if self.fail:
            raise RuntimeError("Target closed")
        page = Page()
        self.pages.append(page)
        return page

    async def close(self):
        self.closed = True


class Browser:
    def __init__(self):
        self.contexts = []
        self.fail_next = False

    async def new_context(self, storage_state=None):
        context = Context(fail=self.fail_next)
        self.fail_next = False
        self.contexts.append(context)
        return context


def run(coro):
    return asyncio.run(coro)


def test_same_session_shares_one_context_and_leases_only_close_their_pages():
    async def scenario():
        browser, pool = Browser(), SharedContextPool(idle_seconds=60)
        first = await pool.lease(browser, {"cookies": [{"name": "a"}]})
        second = await pool.lease(browser, {"cookies": [{"name": "a"}]})
        page_one, page_two = await first.new_page(), await second.new_page()
        assert len(browser.contexts) == 1
        await first.close()
        await first.close()  # closing twice must not release twice
        assert page_one.closed and not page_two.closed and not browser.contexts[0].closed
        await second.close()
        assert not browser.contexts[0].closed  # kept for the next page of the sync
    run(scenario())


def test_different_sessions_do_not_share_a_context():
    async def scenario():
        browser, pool = Browser(), SharedContextPool(idle_seconds=60)
        await pool.lease(browser, {"cookies": [{"name": "a"}]})
        await pool.lease(browser, {"cookies": [{"name": "b"}]})
        assert len(browser.contexts) == 2
    run(scenario())


def test_idle_context_is_closed_but_a_new_lease_cancels_the_timer():
    async def scenario():
        browser, pool = Browser(), SharedContextPool(idle_seconds=0.05)
        lease = await pool.lease(browser, {})
        await lease.close()
        again = await pool.lease(browser, {})  # before the timer fires
        await asyncio.sleep(0.12)
        assert not browser.contexts[0].closed and len(browser.contexts) == 1
        await again.close()
        await asyncio.sleep(0.12)
        assert browser.contexts[0].closed
        fresh = await pool.lease(browser, {})
        assert len(browser.contexts) == 2
        await fresh.close()
    run(scenario())


def test_a_dead_context_is_replaced_on_the_next_lease():
    async def scenario():
        browser, pool = Browser(), SharedContextPool(idle_seconds=60)
        browser.fail_next = True
        lease = await pool.lease(browser, {})
        with pytest.raises(RuntimeError):
            await lease.new_page()
        await lease.close()
        replacement = await pool.lease(browser, {})
        assert len(browser.contexts) == 2
        assert await replacement.new_page() is not None
    run(scenario())


def test_context_from_a_finished_event_loop_is_not_reused():
    browser, pool = Browser(), SharedContextPool(idle_seconds=60)

    async def lease_once():
        lease = await pool.lease(browser, {})
        await lease.close()

    run(lease_once())
    run(lease_once())
    assert len(browser.contexts) == 2
