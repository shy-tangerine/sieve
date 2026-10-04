"""Public fetch envelopes never include backend exception values."""

from types import SimpleNamespace

import pytest

from sieve import server_fetch
from sieve import server


SENTINEL = "Bearer fetch-error-sentinel-secret"


def _assert_safe_response(response, expected_fetcher):
    assert response.status == 0
    assert response.fetcher_used == expected_fetcher
    assert SENTINEL not in repr(response.model_dump())
    assert response.error
    assert response.error != SENTINEL


def _assert_safe_bulk(response, expected_fetcher):
    assert response.total == 1
    assert response.successful == 0
    _assert_safe_response(response.results[0], expected_fetcher)
    assert SENTINEL not in repr(response.model_dump())


@pytest.mark.asyncio
async def test_bulk_get_exception_uses_safe_public_diagnostic(monkeypatch):
    import sieve.fetcher

    server_fetch._ensure_server_symbols()
    monkeypatch.setattr(server_fetch, "validate_url", lambda url: url)

    class Session:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            return False

    class Adapter:
        def __init__(self, _session):
            pass

        async def fetch(self, *_args, **_kwargs):
            raise RuntimeError(SENTINEL)

    monkeypatch.setattr(sieve.fetcher, "HTTPSession", Session)
    monkeypatch.setattr(server_fetch, "HTTPRetrievalAdapter", Adapter)

    response = await server_fetch.bulk_get(["https://example.test/fetch"])
    _assert_safe_bulk(response, "http")


@pytest.mark.asyncio
@pytest.mark.parametrize(("fetcher", "bulk_method"), [
    ("dynamic", "bulk_fetch"),
    ("stealthy", "bulk_stealthy_fetch"),
])
async def test_browser_bulk_exception_uses_safe_public_diagnostic(monkeypatch, fetcher, bulk_method):
    server_fetch._ensure_server_symbols()
    monkeypatch.setattr(server_fetch, "validate_url", lambda url: url)
    monkeypatch.setattr(server_fetch, "_browser_deps_available", lambda: True)

    backend = object.__new__(server.MasterFetchServer)

    async def get_session(_session_id, _session_type):
        return SimpleNamespace(session=object())

    monkeypatch.setattr(backend, "_get_session", get_session)

    class Adapter:
        def __init__(self, _session):
            pass

        async def fetch(self, *_args, **_kwargs):
            raise RuntimeError(SENTINEL)

    monkeypatch.setattr(server_fetch, "BrowserRetrievalAdapter", Adapter)
    fetch = getattr(server_fetch, bulk_method)
    response = await fetch(
        backend, ["https://example.test/fetch"], extraction_type="text",
        session_id="fixture-session",
    )
    _assert_safe_bulk(response, fetcher)


@pytest.mark.asyncio
async def test_smart_bulk_exception_uses_safe_public_diagnostic(monkeypatch):
    backend = object.__new__(server.MasterFetchServer)

    async def fail(**_kwargs):
        raise RuntimeError(SENTINEL)

    monkeypatch.setattr(backend, "smart_fetch", fail)
    response = await server_fetch._smart_fetch_bulk(
        backend, ["https://example.test/fetch"], "text", None, True, True,
        0, None, False, True, False, 0, None, 30000, False, True, True,
        True, None, None, None,
    )
    _assert_safe_bulk(response, "none")


@pytest.mark.asyncio
async def test_forced_sleeper_failure_uses_safe_public_diagnostic(monkeypatch):
    import sieve.sleeper_bridge

    backend = object.__new__(server.MasterFetchServer)
    monkeypatch.setattr(
        sieve.sleeper_bridge, "sleeper_fetch",
        lambda *_args, **_kwargs: {"ok": False, "error": SENTINEL},
    )

    async def finalize(result, *_args):
        return result

    monkeypatch.setattr(backend, "_finalize_result", finalize)
    response = await server_fetch._force_fetch(
        backend, "https://example.test/fetch", "sleeper", "text", None,
        True, True, 0, 0, True, False, 0, None, 30000, False, True, True,
        True, None, None, None,
    )
    _assert_safe_response(response, "sleeper")


@pytest.mark.asyncio
async def test_auto_escalation_browser_exception_is_sanitized(monkeypatch):
    server_fetch._ensure_server_symbols()
    backend = object.__new__(server.MasterFetchServer)

    async def http_failure(*_args, **_kwargs):
        return server.ResponseModel(
            url="https://example.test/fetch", status=403, content=[], fetcher_used="http",
        )

    async def auto_session(_kind):
        return "fixture-session"

    async def browser_failure(*_args, **_kwargs):
        raise RuntimeError(SENTINEL)

    async def finalize(result, *_args):
        return result

    monkeypatch.setattr(backend, "_http_with_retry", http_failure)
    monkeypatch.setattr(backend, "_ensure_auto_session", auto_session)
    monkeypatch.setattr(backend, "stealthy_fetch", browser_failure)
    monkeypatch.setattr(backend, "_finalize_result", finalize)
    monkeypatch.setattr(server_fetch, "_browser_deps_available", lambda: True)

    response = await server_fetch._auto_escalate(
        backend, "https://example.test/fetch", "text", None, True, True,
        0, 0, True, False, 0, None, 30000, False, True, True, True,
        None, None, None,
    )
    _assert_safe_response(response, "stealthy")
