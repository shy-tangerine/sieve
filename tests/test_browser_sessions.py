"""Contract tests for the browser lifecycle registry.

These tests use the registry's injected factory so session-state races and
cleanup stay deterministic without launching a browser.
"""

from __future__ import annotations

import asyncio

import pytest

from sieve.browser_sessions import BrowserSessionRegistry


class FakeBrowser:
    def __init__(self, *, fail_start: bool = False, **kwargs):
        self.kwargs = kwargs
        self.fail_start = fail_start
        self._is_alive = False
        self.started = 0
        self.closed = 0

    async def start(self):
        self.started += 1
        if self.fail_start:
            raise RuntimeError("start failed")
        self._is_alive = True

    async def close(self):
        self.closed += 1
        self._is_alive = False


def make_registry(factory):
    return BrowserSessionRegistry(
        browser_available=lambda: True,
        browser_error=lambda: None,
        idle_timeout=0,
        idle_check_interval=60,
        browser_factory=factory,
    )


@pytest.mark.asyncio
async def test_open_get_close_preserves_type_and_browser_options():
    created = []

    def factory(session_type, **kwargs):
        browser = FakeBrowser(**kwargs)
        created.append((session_type, browser))
        return browser

    registry = make_registry(factory)
    opened = await registry.open(
        "stealthy",
        session_id="ig",
        real_chrome=True,
        max_pages=3,
        hide_canvas=True,
    )

    assert opened.session_id == "ig"
    assert opened.entry.session_type == "stealthy"
    assert await registry.get("ig", "stealthy") is opened.entry
    assert created[0][0] == "stealthy"
    assert created[0][1].kwargs["real_chrome"] is True
    assert created[0][1].kwargs["max_pages"] == 3
    assert created[0][1].kwargs["hide_canvas"] is True

    await registry.close("ig")
    assert created[0][1].closed == 1
    with pytest.raises(ValueError, match="not found"):
        await registry.get("ig")


@pytest.mark.asyncio
async def test_failed_start_is_removed_from_registry():
    created = []

    def factory(_session_type, **kwargs):
        browser = FakeBrowser(fail_start=True, **kwargs)
        created.append(browser)
        return browser

    registry = make_registry(factory)
    with pytest.raises(RuntimeError, match="start failed"):
        await registry.open("dynamic", session_id="broken")

    assert registry.sessions == {}
    assert created[0]._is_alive is False


@pytest.mark.asyncio
async def test_concurrent_auto_requests_share_one_browser():
    created = []

    def factory(_session_type, **kwargs):
        browser = FakeBrowser(**kwargs)
        created.append(browser)
        return browser

    registry = make_registry(factory)
    ids = await asyncio.gather(
        *(registry.ensure_auto_session("stealthy") for _ in range(8))
    )

    assert ids == [ids[0]] * len(ids)
    assert len(created) == 1
    assert len(registry.sessions) == 1

    await registry.shutdown()
    assert created[0].closed == 1
    assert registry.sessions == {}


@pytest.mark.asyncio
async def test_auto_session_type_mismatch_is_rejected():
    registry = make_registry(lambda _session_type, **kwargs: FakeBrowser(**kwargs))
    await registry.ensure_auto_session("dynamic")

    with pytest.raises(ValueError, match="requires a 'stealthy' session"):
        await registry.get(registry._auto_ids["dynamic"], "stealthy")

    await registry.shutdown()
