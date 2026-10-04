"""BYOK key-cooling coverage (revision-audit findings T8/T9).

The audit's mutation battery: replacing ``pool.mark_invalid(key)`` with
``pass`` left the suite green — rate-limit rotation (a BYOK selling point)
had no failing-key assertion. Also pins the proxy-rotation contract from
``search_metasearch._get_search_proxy``: the pool is consulted and a cooled
pool falls back to direct.
"""

import json
import asyncio
from threading import Barrier

import pytest

from sieve import search_api_keys as sak
from sieve.search_api_keys import KeyPool
from sieve import search as public_search, search_metasearch as metasearch, search_proxy
from sieve.command_router import CapabilityRouter
from sieve.public_output import safe_public_json


@pytest.fixture
def offline_search(monkeypatch):
    proxies_used = []

    class Engine:
        disabled = False

        def __init__(self, proxy, **kwargs):
            proxies_used.append(proxy)

        def search(self, query, *args, **kwargs):
            return [metasearch.TextResult(title="Routing example", href="https://example.test/route", body="Routing example documentation")]

    async def no_reranker():
        return False

    monkeypatch.setattr(metasearch, "_TEXT_ENGINES", {"brave": Engine})
    monkeypatch.setattr(metasearch, "_register_api_backends", lambda: None)
    monkeypatch.setattr(metasearch, "_register_byok_backends", lambda: None)
    monkeypatch.setattr(metasearch, "_is_circuit_open", lambda name: False)
    monkeypatch.setattr(sak, "get_byok_engines", lambda: {})
    monkeypatch.setattr(public_search, "ensure_reranker", no_reranker)
    monkeypatch.setattr(public_search, "_rank", lambda query, ranked, mode: (ranked, [0.8] * len(ranked), "merge", ""))
    return Engine, proxies_used


@pytest.mark.asyncio
@pytest.mark.parametrize("configured,cooled,allow_direct,route", [
    (False, False, False, "no_proxy_config"),
    (True, False, False, "configured_proxy"),
    (True, True, True, "direct_fallback"),
    (True, True, False, "configured_proxy"),
])
async def test_search_reports_safe_proxy_route(offline_search, monkeypatch, configured, cooled, allow_direct, route):
    _, proxies_used = offline_search
    proxy = "http://private-user:private-password@127.0.0.1:8080"
    pool = search_proxy.ProxyPool([proxy]) if configured else None
    if cooled:
        pool.mark_failed(proxy)
    monkeypatch.setenv("SIEVE_PROXY_ALLOW_DIRECT", "1" if allow_direct else "0")
    monkeypatch.setattr(search_proxy, "get_proxy_pool", lambda: pool)

    class Server:
        async def smart_search(self, **kwargs):
            return await public_search.smart_search(None, **kwargs)

    content, result = await CapabilityRouter(Server()).dispatch(
        "smart_search", {"query": "routing example", "engines": ["brave"], "cache_ttl": 0}
    )
    assert result["proxy_routes"] == [route]
    assert result["results"]
    assert proxies_used == [proxy if route == "configured_proxy" else None]
    assert result == json.loads(content[0].text)
    for secret in (proxy, "private-user", "private-password", "127.0.0.1"):
        assert secret not in content[0].text
    assert search_proxy._proxy_routes() == []


@pytest.mark.asyncio
async def test_forced_proxy_failure_reports_route_without_credentials(offline_search, monkeypatch):
    Engine, proxies_used = offline_search
    proxy = "http://private-user:private-password@127.0.0.1:8080"
    pool = search_proxy.ProxyPool([proxy])
    pool.mark_failed(proxy)
    monkeypatch.setenv("SIEVE_PROXY_ALLOW_DIRECT", "0")
    monkeypatch.setattr(search_proxy, "get_proxy_pool", lambda: pool)

    def fail(self, query, *args, **kwargs):
        raise RuntimeError(proxy + " provider-token")

    monkeypatch.setattr(Engine, "search", fail)
    result = await public_search.smart_search(None, "routing example", engines=["brave"], cache_ttl=0)
    assert result.error and not result.results
    assert result.proxy_routes == ["configured_proxy"]
    assert proxies_used == [proxy]
    wire = safe_public_json(result)
    for secret in (proxy, "private-user", "private-password", "127.0.0.1", "provider-token"):
        assert secret not in wire


@pytest.mark.asyncio
async def test_proxy_construction_failure_does_not_disclose_proxy(offline_search, monkeypatch):
    Engine, _ = offline_search
    proxy = "http://private-user:private-password@127.0.0.1:8080"
    monkeypatch.setattr(search_proxy, "get_proxy_pool", lambda: search_proxy.ProxyPool([proxy]))

    def fail(self, **kwargs):
        raise RuntimeError(proxy + " provider-token")

    monkeypatch.setattr(Engine, "__init__", fail)
    result = await public_search.smart_search(None, "routing example", engines=["brave"], cache_ttl=0)
    assert result.proxy_routes == ["configured_proxy"]
    assert "No search engines could start" in result.error
    for secret in (proxy, "private-user", "private-password", "127.0.0.1", "provider-token"):
        assert secret not in safe_public_json(result)


@pytest.mark.asyncio
async def test_concurrent_search_proxy_provenance_is_isolated(offline_search, monkeypatch):
    Engine, proxies_used = offline_search
    proxy = "http://proxy.test:8080"
    pool = search_proxy.ProxyPool([proxy])
    monkeypatch.setenv("SIEVE_PROXY_ALLOW_DIRECT", "1")
    monkeypatch.setattr(search_proxy, "get_proxy_pool", lambda: pool)
    barrier = Barrier(2, timeout=5)
    original_init, original_search = Engine.__init__, Engine.search

    def init(self, **kwargs):
        original_init(self, **kwargs)
        pool.mark_failed(proxy)

    def search(self, *args, **kwargs):
        barrier.wait()
        return original_search(self, *args, **kwargs)

    monkeypatch.setattr(Engine, "__init__", init)
    monkeypatch.setattr(Engine, "search", search)
    first, second = await asyncio.gather(*[
        public_search.smart_search(None, "routing example", engines=["brave"], cache_ttl=0)
        for _ in range(2)
    ])
    assert proxies_used == [proxy, None]
    assert first.proxy_routes == ["configured_proxy"]
    assert second.proxy_routes == ["direct_fallback"]
    assert search_proxy._proxy_routes() == []


@pytest.mark.asyncio
async def test_cached_search_keeps_original_proxy_provenance(offline_search, monkeypatch):
    proxy = "http://proxy.test:8080"
    monkeypatch.setattr(search_proxy, "get_proxy_pool", lambda: search_proxy.ProxyPool([proxy]))
    stored = {}

    async def get(*args, **kwargs):
        return stored or None

    async def put(url, kind, content, *args):
        stored["content"] = content

    monkeypatch.setattr(public_search, "get_cached", get)
    monkeypatch.setattr(public_search, "set_cached", put)
    original = await public_search.smart_search(None, "routing example", engines=["brave"], cache_ttl=60)
    monkeypatch.setattr(search_proxy, "get_proxy_pool", lambda: None)
    cached = await public_search.smart_search(None, "routing example", engines=["brave"], cache_ttl=60)
    assert original.proxy_routes == cached.proxy_routes == ["configured_proxy"]
    assert cached.cached is True
    data = json.loads(stored["content"][0])
    data.pop("proxy_routes")
    stored["content"] = [json.dumps(data)]
    old_cached = await public_search.smart_search(None, "routing example", engines=["brave"], cache_ttl=60)
    assert old_cached.cached and old_cached.proxy_routes == []


# ── T9: failing keys get cooled, rotation continues ──────────────────


class _FakeResponse:
    def __init__(self, status_code: int, payload: dict | None = None):
        self.status_code = status_code
        self.text = json.dumps(payload or {})


class _Engine(sak.BaseBYOKEngine):
    """Minimal engine replaying a fixed status queue per HTTP request."""

    provider_name = "testprov"
    search_url = "https://api.test/search"
    search_method = "GET"

    def __init__(self, statuses, **kwargs):
        super().__init__(**kwargs)
        self._statuses = list(statuses)

    def _build_request(self, query, key, **kwargs):
        return {"q": query}, {}

    def _parse_results(self, data: dict) -> list:
        from sieve.search_metasearch import TextResult
        return [TextResult(title="ok", href="https://example.com/", body="")]


def test_failing_key_is_cooled_and_rotation_continues(monkeypatch):
    """First key 429s → must be cooled; result comes from the second key."""
    pool = KeyPool(["key1", "key2"])
    monkeypatch.setattr(sak, "_get_pool", lambda provider: pool)

    engine = _Engine(statuses=[429, 200])
    monkeypatch.setattr(
        engine.http_client, "request",
        lambda method, url, **kw: _FakeResponse(engine._statuses.pop(0)),
    )

    results = engine.search("test query")

    assert results and results[0].title == "ok"
    assert results[0].href == "https://example.com/"
    # key1 must carry rate-limit state after the 429
    assert pool._state["key1"].get("error") == "rate_limited"
    assert "rate_limited_until" in pool._state["key1"]
    # key2 must be clean (mark_success clears any state)
    assert pool._state.get("key2", {}).get("error") is None


def test_invalid_key_gets_invalid_cooldown(monkeypatch):
    """401 marks the key invalid (long cooldown), not merely rate-limited."""
    pool = KeyPool(["badkey"])
    monkeypatch.setattr(sak, "_get_pool", lambda provider: pool)

    engine = _Engine(statuses=[401])
    monkeypatch.setattr(
        engine.http_client, "request",
        lambda method, url, **kw: _FakeResponse(engine._statuses.pop(0)),
    )

    with pytest.raises(sak.MetaBlockedException):
        engine.search("test query")

    assert pool._state["badkey"].get("error") == "invalid"


def test_cooled_key_is_not_handed_out():
    """After mark_rate_limited, get_key must skip the cooled key."""
    pool = KeyPool(["k1", "k2"])
    pool.mark_rate_limited("k1")

    keys = {pool.get_key() for _ in range(3)}
    assert "k1" not in keys
    assert keys == {"k2"}


# ── T8: proxy rotation consulted, cooled pool falls back to direct ───


def test_search_proxy_pool_consulted_and_fallback(monkeypatch):
    """_get_search_proxy asks the pool; a cooled pool yields None (direct)."""
    from sieve import search_metasearch as sms

    class _Pool:
        def __init__(self):
            self.calls = 0

        def get_proxy(self):
            self.calls += 1
            return None  # everything cooled

    pool = _Pool()
    monkeypatch.setattr(sms, "_proxy_pool", pool, raising=False)
    monkeypatch.setattr(
        "sieve.search_proxy.get_proxy_pool", lambda: pool,
    )

    assert sms._get_search_proxy() is None
    assert pool.calls == 1, "pool was never consulted"
    assert sms._PROXY is None


def test_cooled_pool_fails_closed_without_direct_optin(monkeypatch):
    """Issue #28: a configured proxy pool is a privacy boundary — when every
    proxy is cooling, get_proxy() must NOT silently return None (direct)."""
    from sieve.search_proxy import ProxyPool

    monkeypatch.delenv("SIEVE_PROXY_ALLOW_DIRECT", raising=False)
    pool = ProxyPool(["http://p1:8080", "http://p2:8080"])
    pool.mark_failed("http://p1:8080")
    pool.mark_failed("http://p2:8080")
    proxy = pool.get_proxy()
    assert proxy is not None, "cooled pool must not downgrade to direct by default"
    assert proxy in ("http://p1:8080", "http://p2:8080")


def test_cooled_pool_allows_direct_with_explicit_optin(monkeypatch):
    from sieve.search_proxy import ProxyPool

    monkeypatch.setenv("SIEVE_PROXY_ALLOW_DIRECT", "1")
    pool = ProxyPool(["http://p1:8080"])
    pool.mark_failed("http://p1:8080")
    assert pool.get_proxy() is None
