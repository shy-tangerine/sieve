"""Contract tests for SQLite TTLs and cache namespaces.

Search results and fetched page content intentionally share the SQLite
implementation, but use different cache-key dimensions.  Keep these tests at
the cache boundary so a change in either caller cannot silently make one kind
of cached value satisfy the other.
"""

from types import SimpleNamespace
import time

import pytest

from sieve import cache
from sieve import search
from sieve.search_engines import EngineReport, RawResult


@pytest.mark.asyncio
async def test_cache_entry_expires_after_stored_ttl(tmp_path, monkeypatch):
    """An entry older than its stored TTL is a miss and can be purged."""
    await cache.set_cached(
        "https://example.test/article",
        "html",
        ["cached body"],
        200,
        ttl=1,
        cache_dir=tmp_path,
    )

    # Advance only the cache module's clock.  This avoids a sleep-based test
    # while exercising the same timestamp comparison used in production.
    now = time.time()
    monkeypatch.setattr(cache, "time", SimpleNamespace(time=lambda: now + 2))

    assert await cache.get_cached(
        "https://example.test/article",
        "html",
        ttl=1,
        cache_dir=tmp_path,
    ) is None
    assert await cache.clear_cache(cache_dir=tmp_path) == 1


@pytest.mark.asyncio
async def test_search_and_http_cache_entries_are_isolated(tmp_path):
    """Search and HTTP entries for the same URL do not cross-hit."""
    url = "https://example.test/shared"
    search_type = "search:v12:6::::US:en:0::auto:shared"

    await cache.set_cached(
        url,
        "html",
        ["HTTP page content"],
        200,
        ttl=300,
        cache_dir=tmp_path,
    )

    # A search lookup cannot consume an HTTP/content entry from the shared DB.
    assert await cache.get_cached(url, search_type, ttl=300, cache_dir=tmp_path) is None

    await cache.set_cached(
        url,
        search_type,
        ['{"results": [{"url": "https://example.test/result"}]}'],
        200,
        ttl=300,
        cache_dir=tmp_path,
    )

    http_entry = await cache.get_cached(url, "html", ttl=300, cache_dir=tmp_path)
    search_entry = await cache.get_cached(url, search_type, ttl=300, cache_dir=tmp_path)

    assert http_entry is not None
    assert http_entry["content"] == ["HTTP page content"]
    assert search_entry is not None
    assert search_entry["content"] == ['{"results": [{"url": "https://example.test/result"}]}']


@pytest.fixture
def isolated_search_cache(tmp_path, monkeypatch):
    """Point real search cache I/O at one temporary SQLite database."""
    monkeypatch.setattr(cache, "_CACHE_DIR", tmp_path)
    cache._db_initialized.clear()
    return tmp_path


@pytest.mark.asyncio
@pytest.mark.parametrize("case,expected_stale", [
    ("outage", True), ("default", False), ("refresh", False),
    ("cached", False), ("empty", False), ("too_old", False), ("partial", False), ("unknown_credentials", False),
])
async def test_stale_search_fallback_is_explicit_bounded_and_preserves_provenance(
    isolated_search_cache, monkeypatch, case, expected_stale,
):
    from sieve import search_api_keys
    monkeypatch.setattr(search_api_keys, "get_byok_engines", lambda: {})
    monkeypatch.setattr(search, "get_reranker", lambda: None)
    async def no_reranker(**kwargs):
        return None
    monkeypatch.setattr(search, "ensure_reranker", no_reranker)
    async def seed(*args, **kwargs):
        return [RawResult("Old", "https://example.test/old", "snippet", "bing")], [EngineReport("bing", ok=True)]
    monkeypatch.setattr(search, "multi_search", seed)
    monkeypatch.setattr(search, "_proxy_routes", lambda: ["configured_proxy"])
    options = {"server": object(), "query": "stale lifecycle", "max_results": 1,
               "cache_ttl": 1, "engines": ["bing"]}
    await search.smart_search(**options)
    async with cache._connect(isolated_search_cache / "cache.db") as db:
        before = (await (await db.execute("SELECT fetched_at FROM cache")).fetchone())[0]
    now = time.time()
    monkeypatch.setattr(cache, "time", SimpleNamespace(time=lambda: now + 2))
    async def live(*args, **kwargs):
        if case == "partial":
            return [RawResult("New", "https://example.test/new", "snippet", "bing")], [EngineReport("bing", ok=True)]
        if case == "empty":
            return [], [EngineReport("bing", error="no results")]
        return [], [EngineReport("bing", blocked=True, error="timed out")]
    monkeypatch.setattr(search, "multi_search", live)
    if case == "unknown_credentials":
        def unavailable():
            raise OSError("credential store unavailable")
        monkeypatch.setattr(search_api_keys, "get_byok_engines", unavailable)
        async def forbidden_cache(*args, **kwargs):
            pytest.fail("unknown credential scope touched cache")
        monkeypatch.setattr(search, "get_cached", forbidden_cache)
        monkeypatch.setattr(search, "set_cached", forbidden_cache)
    result = await search.smart_search(**options, stale_fallback=case != "default",
                                      stale_max_age=1 if case == "too_old" else 3600,
                                      refresh=case == "refresh", cached=case == "cached")
    assert result.stale is expected_stale
    if expected_stale:
        assert result.cached and result.stale_reason == "timeout"
        assert result.cache_age_seconds >= 2
        assert result.engines_used == ["bing"]
        assert result.proxy_routes == ["configured_proxy"]
        assert result.results[0].url == "https://example.test/old"
        async with cache._connect(isolated_search_cache / "cache.db") as db:
            after = (await (await db.execute("SELECT fetched_at FROM cache")).fetchone())[0]
        assert after == before
    elif case == "partial":
        assert result.results[0].url == "https://example.test/new"
    else:
        assert result.results == []


@pytest.mark.asyncio
async def test_authenticated_search_never_reads_or_writes_cache(monkeypatch):
    from sieve import search_api_keys
    monkeypatch.setattr(search_api_keys, "get_byok_engines", lambda: {"tavily": object()})
    async def forbidden(*args, **kwargs):
        pytest.fail("authenticated search touched cache")
    async def live(*args, **kwargs):
        return [RawResult("Live", "https://example.test/live", "snippet", "tavily")], [EngineReport("tavily", ok=True)]
    async def no_reranker(**kwargs):
        return None
    monkeypatch.setattr(search, "get_cached", forbidden)
    monkeypatch.setattr(search, "set_cached", forbidden)
    monkeypatch.setattr(search, "multi_search", live)
    monkeypatch.setattr(search, "get_reranker", lambda: None)
    monkeypatch.setattr(search, "ensure_reranker", no_reranker)
    result = await search.smart_search(object(), "credentials isolated", stale_fallback=True)
    assert result.results and not result.cached and not result.stale


@pytest.mark.asyncio
async def test_search_writes_sqlite_then_serves_cached_hit(isolated_search_cache, monkeypatch):
    calls = 0

    async def fake_multi_search(*args, **kwargs):
        nonlocal calls
        calls += 1
        return [RawResult("Fresh", "https://example.test/fresh", "snippet", "fake")], [EngineReport("fake", ok=True)]

    monkeypatch.setattr(search, "multi_search", fake_multi_search)
    monkeypatch.setattr(search, "get_reranker", lambda: None)
    async def no_reranker(**kwargs):
        return None
    monkeypatch.setattr(search, "ensure_reranker", no_reranker)

    first = await search.smart_search(object(), "sqlite lifecycle", max_results=1,
                                      cache_ttl=300, mode="auto", engines=["bing"])
    assert first.error == ""
    assert first.cached is False
    assert calls == 1
    assert (isolated_search_cache / "cache.db").exists()

    hit = await search.smart_search(object(), "sqlite lifecycle", max_results=1,
                                    cache_ttl=300, mode="auto", engines=["bing"])
    assert hit.cached is True
    assert hit.results[0].url == "https://example.test/fresh"
    assert calls == 1


@pytest.mark.asyncio
async def test_search_refresh_replaces_sqlite_cached_result(isolated_search_cache, monkeypatch):
    responses = ["old", "new"]

    async def fake_multi_search(*args, **kwargs):
        value = responses.pop(0)
        return [RawResult(value, f"https://example.test/{value}", "snippet", "fake")], [EngineReport("fake", ok=True)]

    monkeypatch.setattr(search, "multi_search", fake_multi_search)
    monkeypatch.setattr(search, "get_reranker", lambda: None)
    async def no_reranker(**kwargs):
        return None
    monkeypatch.setattr(search, "ensure_reranker", no_reranker)

    await search.smart_search(object(), "replace me", max_results=1, cache_ttl=300,
                              mode="auto", engines=["bing"])
    refreshed = await search.smart_search(object(), "replace me", max_results=1,
                                          cache_ttl=300, mode="auto", engines=["bing"],
                                          refresh=True)
    assert refreshed.cached is False
    assert refreshed.results[0].url == "https://example.test/new"

    hit = await search.smart_search(object(), "replace me", max_results=1, cache_ttl=300,
                                    mode="auto", engines=["bing"])
    assert hit.cached is True
    assert hit.results[0].url == "https://example.test/new"


def test_search_cli_cache_failures_emit_json_and_nonzero(monkeypatch, capsys):
    from sieve import server

    monkeypatch.setattr(server.sys, "argv", ["sieve", "search", "missing", "--cached"])
    assert server.main() == 1
    payload = capsys.readouterr().out
    assert '"error":"No fresh cached results found.' in payload

    monkeypatch.setattr(server.sys, "argv", ["sieve", "search", "query", "--cached", "--refresh"])
    assert server.main() == 1
    payload = capsys.readouterr().out
    assert "mutually exclusive" in payload


@pytest.mark.asyncio
@pytest.mark.parametrize("credential_kind", ["cookies", "Authorization", "Cookie", "X-Api-Key"])
async def test_credentialed_fetches_bypass_anonymous_cache(tmp_path, monkeypatch, credential_kind):
    """Real SQLite reads/writes cannot cross-hit two request identities."""
    import sqlite3

    from sieve import server

    monkeypatch.setattr(cache, "_CACHE_DIR", tmp_path)
    cache._db_initialized.clear()
    # Keep input validation except DNS resolution offline; transport is stubbed.
    monkeypatch.setattr(server, "validate_url", lambda url: url)
    backend = object.__new__(server.MasterFetchServer)
    calls = []
    secrets = ["fixture-user-one-secret", "fixture-user-two-secret", "fixture-rotated-secret"]

    async def transport(url, **kwargs):
        identity = (kwargs.get("headers") or {}).get(credential_kind)
        if credential_kind == "cookies":
            identity = (kwargs.get("cookies") or {}).get("session")
        identity = identity or "anonymous"
        calls.append((url, identity))
        # Make the content unambiguously usable so real finalization caches it.
        body = (f"Research article for {identity}. Evidence and observations. " * 30)
        return server.ResponseModel(
            url=url, status=200, content=[body], content_type="text/plain",
            extracted_type="text", fetcher_used="http",
        )

    monkeypatch.setattr(backend, "get", transport)

    async def fetch(url, secret=None):
        options = {}
        if secret is not None:
            if credential_kind == "cookies":
                options["cookies"] = [{"name": "session", "value": secret, "domain": "example.test"}]
            else:
                options["extra_headers"] = {credential_kind: secret}
        return await backend.smart_fetch(
            url, extraction_type="text", cache_ttl=300, **options,
        )

    url = "https://example.test/anonymous-first"
    first = await fetch(url)
    assert not first.cached
    hit = await fetch(url)
    assert hit.cached and hit.content == first.content
    assert len(calls) == 1

    for secret in secrets:
        for _ in range(2):
            result = await fetch(url, secret)
            assert not result.cached
            assert secret in result.content[0]
    assert len(calls) == 7  # repeats and rotation also force transport
    hit = await fetch(url)
    assert hit.cached and hit.content == first.content
    assert len(calls) == 7

    # Starting with an authenticated response cannot seed an anonymous entry.
    other_url = "https://example.test/credential-first"
    await fetch(other_url, secrets[0])
    anonymous = await fetch(other_url)
    assert not anonymous.cached and "anonymous" in anonymous.content[0]
    assert (await fetch(other_url)).cached

    database = tmp_path / "cache.db"
    with sqlite3.connect(database) as connection:
        rows = connection.execute("SELECT * FROM cache").fetchall()
    assert len(rows) == 2
    for secret in secrets:
        assert secret not in repr(rows)
        assert secret.encode() not in database.read_bytes()


@pytest.mark.asyncio
@pytest.mark.parametrize("context_kind", ["proxy", "useragent", "force_fetcher", "browser_profile"])
async def test_request_contexts_bypass_shared_fetch_cache(tmp_path, monkeypatch, context_kind):
    """Transport and persistent-profile contexts cannot read or seed shared entries."""
    import sqlite3

    from sieve import server

    monkeypatch.setattr(cache, "_CACHE_DIR", tmp_path)
    cache._db_initialized.clear()
    monkeypatch.delenv("SIEVE_BROWSER_PROFILE_DIR", raising=False)
    monkeypatch.setattr(server, "validate_url", lambda url: url)
    backend = object.__new__(server.MasterFetchServer)
    calls = []
    context_secret = "fixture-context-secret"
    profile_path = str(tmp_path / context_secret)

    async def transport(url, **kwargs):
        count = len(calls) + 1
        calls.append((url, kwargs))
        return server.ResponseModel(
            url=url, status=200,
            content=[f"Research response {count}. " + "Evidence and observations. " * 30],
            content_type="text/plain", extracted_type="text", fetcher_used="http",
        )

    monkeypatch.setattr(backend, "get", transport)

    async def fetch(url, *, contextual=False):
        options = {}
        profile_enabled = False
        if contextual:
            if context_kind == "proxy":
                options["proxy"] = f"http://proxy-user:{context_secret}@proxy.example.test:8080"
            elif context_kind == "useragent":
                options["useragent"] = f"FixtureAgent/{context_secret}"
            elif context_kind == "force_fetcher":
                options["force_fetcher"] = "http"
            else:
                monkeypatch.setenv("SIEVE_BROWSER_PROFILE_DIR", profile_path)
                profile_enabled = True
        try:
            return await backend.smart_fetch(
                url, extraction_type="text", cache_ttl=300, **options,
            )
        finally:
            if profile_enabled:
                monkeypatch.delenv("SIEVE_BROWSER_PROFILE_DIR", raising=False)

    url = "https://example.test/context-first"
    anonymous = await fetch(url)
    assert not anonymous.cached
    assert (await fetch(url)).cached
    assert len(calls) == 1

    for _ in range(2):
        result = await fetch(url, contextual=True)
        assert not result.cached
        assert result.content[0] != anonymous.content[0]
    assert len(calls) == 3
    # A context-specific response did not replace the anonymous baseline.
    assert (await fetch(url)).cached
    assert len(calls) == 3

    # The reverse order must also avoid seeding an anonymous cache entry.
    other_url = "https://example.test/anonymous-after-context"
    context_response = await fetch(other_url, contextual=True)
    fresh_anonymous = await fetch(other_url)
    assert not fresh_anonymous.cached
    assert fresh_anonymous.content[0] != context_response.content[0]
    assert (await fetch(other_url)).cached

    database = tmp_path / "cache.db"
    with sqlite3.connect(database) as connection:
        rows = connection.execute("SELECT * FROM cache").fetchall()
    assert len(rows) == 2
    assert context_secret not in repr(rows)
    assert context_secret.encode() not in database.read_bytes()
    assert profile_path.encode() not in database.read_bytes()


@pytest.mark.asyncio
@pytest.mark.parametrize("context_attr", ["_profile_dir", "_cdp_url"])
async def test_retained_auto_browser_identity_bypasses_cache(tmp_path, monkeypatch, context_attr):
    """An auto-session keeps its cache bypass after its configuring env is unset."""
    import sqlite3

    from sieve import server

    monkeypatch.setattr(cache, "_CACHE_DIR", tmp_path)
    cache._db_initialized.clear()
    monkeypatch.delenv("SIEVE_BROWSER_PROFILE_DIR", raising=False)
    monkeypatch.setattr(server, "validate_url", lambda url: url)
    backend = object.__new__(server.MasterFetchServer)
    secret = "fixture-browser-secret"
    context_value = (
        str(tmp_path / secret) if context_attr == "_profile_dir"
        else f"http://user:{secret}@127.0.0.1:9222"
    )
    if context_attr == "_profile_dir":
        # The auto-session captured the configured profile before the env changed.
        monkeypatch.setenv("SIEVE_BROWSER_PROFILE_DIR", context_value)
    session = SimpleNamespace(**{context_attr: context_value})
    backend._browser_sessions = SimpleNamespace(
        _auto_ids={"stealthy": "existing"},
        _sessions={"existing": SimpleNamespace(session=session)},
    )
    if context_attr == "_profile_dir":
        monkeypatch.delenv("SIEVE_BROWSER_PROFILE_DIR", raising=False)

    calls = []

    async def transport(url, **kwargs):
        calls.append(url)
        return server.ResponseModel(
            url=url, status=200,
            content=[f"Fresh response {len(calls)}. " + "Evidence and observations. " * 30],
            content_type="text/plain", extracted_type="text", fetcher_used="http",
        )

    monkeypatch.setattr(backend, "get", transport)

    async def fetch():
        return await backend.smart_fetch(
            "https://example.test/retained-profile", extraction_type="text", cache_ttl=300,
        )

    first = await fetch()
    second = await fetch()
    assert not first.cached and not second.cached
    assert first.content != second.content
    assert len(calls) == 2

    # Once the auto session is gone, only a new anonymous response may seed cache.
    del backend._browser_sessions
    anonymous = await fetch()
    assert not anonymous.cached
    assert anonymous.content != second.content
    assert (await fetch()).cached
    assert len(calls) == 3

    database = tmp_path / "cache.db"
    with sqlite3.connect(database) as connection:
        rows = connection.execute("SELECT * FROM cache").fetchall()
    assert len(rows) == 1
    assert secret not in repr(rows)
    assert secret.encode() not in database.read_bytes()
