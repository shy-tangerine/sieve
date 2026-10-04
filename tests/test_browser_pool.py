import asyncio

import pytest

from sieve.browser import DynamicBrowser


class FakePage:
    def __init__(self):
        self.closed = False
        self.close_calls = 0

    def is_closed(self):
        return self.closed

    async def close(self):
        self.closed = True
        self.close_calls += 1


class FakeContext:
    def __init__(self):
        self.pages = []

    async def new_page(self):
        page = FakePage()
        self.pages.append(page)
        return page


@pytest.mark.asyncio
async def test_browser_session_reuses_ready_tab():
    session = DynamicBrowser(max_pages=1)
    session._context = FakeContext()
    first = await session._acquire_page()
    await session._release_page(first)
    second = await session._acquire_page()

    assert second is first
    assert len(session._context.pages) == 1


@pytest.mark.asyncio
async def test_failed_tab_is_discarded_and_replaced():
    session = DynamicBrowser(max_pages=1)
    session._context = FakeContext()
    first = await session._acquire_page()
    await session._release_page(first, discard=True)
    second = await session._acquire_page()

    assert second is not first
    assert first.closed
    assert len(session._context.pages) == 2


@pytest.mark.asyncio
async def test_pool_waits_at_capacity_and_close_waits_for_lease():
    session = DynamicBrowser(max_pages=1)
    session._context = FakeContext()
    first = await session._acquire_page()

    waiting = asyncio.create_task(session._acquire_page())
    await asyncio.sleep(0)
    assert not waiting.done()

    closing = asyncio.create_task(session.close())
    await asyncio.sleep(0)
    assert not closing.done()

    await session._release_page(first)
    await closing
    with pytest.raises(RuntimeError, match="closing"):
        await waiting


def test_browser_session_rejects_invalid_page_limit():
    with pytest.raises(ValueError, match="max_pages"):
        DynamicBrowser(max_pages=0)


def test_browser_session_reads_optional_persistent_profile(monkeypatch, tmp_path):
    profile = tmp_path / "browser-profile"
    monkeypatch.setenv("SIEVE_BROWSER_PROFILE_DIR", str(profile))

    session = DynamicBrowser()

    assert session._profile_dir == str(profile)


def test_browser_session_defaults_to_unowned_profile_setting(monkeypatch):
    monkeypatch.delenv("SIEVE_BROWSER_PROFILE_DIR", raising=False)

    session = DynamicBrowser()

    assert session._profile_dir is None
    assert session._owns_user_data_dir is False


@pytest.mark.asyncio
async def test_configured_profile_survives_session_close(tmp_path):
    profile = tmp_path / "persistent"
    profile.mkdir()
    session = DynamicBrowser()
    session._user_data_dir = str(profile)
    session._is_alive = True

    await session.close()

    assert profile.is_dir()


@pytest.mark.asyncio
async def test_temporary_profile_is_removed_on_session_close(tmp_path):
    profile = tmp_path / "temporary"
    profile.mkdir()
    session = DynamicBrowser()
    session._user_data_dir = str(profile)
    session._owns_user_data_dir = True
    session._is_alive = True

    await session.close()

    assert not profile.exists()
