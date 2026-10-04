import json, sys
import pytest
from sieve import server

def test_search_dispatch_emits_json(monkeypatch, capsys):
    async def fake_search(self, query, max_results=6, **kwargs):
        from sieve.search import SearchResponseModel
        return SearchResponseModel(query=query, results=[], total_results=0)
    monkeypatch.setattr(server.MasterFetchServer, "smart_search", fake_search)
    monkeypatch.setattr(sys, "argv", ["sieve", "search", "demo", "--max-results", "3"])
    assert server.main() in (0, None)
    assert json.loads(capsys.readouterr().out)["query"] == "demo"


def test_search_cache_controls_are_forwarded(monkeypatch, capsys):
    seen = {}
    async def fake_search(self, query, **kwargs):
        seen.update(kwargs)
        from sieve.search import SearchResponseModel
        return SearchResponseModel(query=query, results=[], total_results=0)
    monkeypatch.setattr(server.MasterFetchServer, "smart_search", fake_search)
    monkeypatch.setattr(sys, "argv", ["sieve", "search", "demo", "--cached", "--stale-fallback", "--stale-max-age", "120"])
    assert server.main() in (0, None)
    capsys.readouterr()
    assert seen["cached"] is True
    assert seen["refresh"] is False
    assert seen["stale_fallback"] is True
    assert seen["stale_max_age"] == 120


@pytest.mark.asyncio
async def test_server_search_wrapper_preserves_cache_controls(monkeypatch):
    """The extracted server wrapper must pass CLI cache flags to search."""
    seen = {}

    async def fake_search(*args, **kwargs):
        seen.update(kwargs)
        from sieve.search import SearchResponseModel
        return SearchResponseModel(query=args[1], results=[], total_results=0)

    monkeypatch.setattr("sieve.search.smart_search", fake_search)
    from sieve.server_search import smart_search

    await smart_search(object(), "demo", cached=True, refresh=False)
    assert seen["cached"] is True
    assert seen["refresh"] is False


@pytest.mark.asyncio
async def test_extracted_smart_fetch_resolves_request_context(monkeypatch):
    """The fetch module remains callable after transport extraction."""
    from sieve import server_fetch

    class DummyServer:
        def _validate_smart_fetch_params(self, url, extraction_type, css_selector, headers, timeout, proxy, useragent):
            return url, css_selector, headers, timeout, proxy, useragent

        async def _auto_escalate(self, *args, **kwargs):
            return "fetched"

    result = await server_fetch.smart_fetch(DummyServer(), "https://example.com", cache_ttl=0, focus="answer")
    assert result == "fetched"


def test_fetch_dispatch_emits_json(monkeypatch, capsys):
    async def fake_fetch(self, url, **kwargs):
        from sieve.server import ResponseModel
        return ResponseModel(url=url, status=200, content=["hi"])
    monkeypatch.setattr(server.MasterFetchServer, "smart_fetch", fake_fetch)
    monkeypatch.setattr(sys, "argv", ["sieve", "fetch", "https://example.com"])
    assert server.main() in (0, None)
    assert json.loads(capsys.readouterr().out)["status"] == 200


def test_generic_fetch_sleeper_surfaces_rendered_text(monkeypatch, capsys):
    from sieve import sleeper_bridge
    from sieve import server_fetch
    async def _noop_set(*a, **k):
        return None
    async def _noop_get(*a, **k):
        return None
    monkeypatch.setattr(server, "get_cached", _noop_get)
    monkeypatch.setattr(server, "set_cached", _noop_set)
    monkeypatch.setattr(server_fetch, "get_cached", _noop_get)
    monkeypatch.setattr(server_fetch, "set_cached", _noop_set)
    monkeypatch.setattr(sleeper_bridge, "sleeper_fetch", lambda *a, **k: {
        "ok": True, "url": a[0], "text": "rendered from the real browser",
    })
    monkeypatch.setattr(sys, "argv", ["sieve", "fetch", "https://spa-success-20260910.example/?case=rendered",
                                       "--reader", "browser", "--browser-backend", "sleeper", "--cache-ttl", "0"])
    assert server.main() in (0, None)
    result = json.loads(capsys.readouterr().out)
    assert result["fetcher_used"] == "sleeper"
    assert result["content"] == ["rendered from the real browser"]
    assert result["content_ok"] is True


def test_generic_fetch_sleeper_daemon_down_fails_closed(monkeypatch, capsys):
    from sieve import sleeper_bridge
    monkeypatch.setattr(sleeper_bridge, "sleeper_fetch", lambda *a, **k: {
        "ok": False, "url": a[0], "error": "Sleeper daemon not reachable on :8790",
    })
    monkeypatch.setattr(sys, "argv", ["sieve", "fetch", "https://spa-down-unique-20260910.example/?case=daemon-down",
                                       "--reader", "browser", "--browser-backend", "sleeper", "--cache-ttl", "0"])
    assert server.main() in (0, 1, None)
    result = json.loads(capsys.readouterr().out)
    assert result["content_ok"] is False
    assert result["error"] == "The network request failed."


def test_generic_fetch_sleeper_extension_failure_returns_safe_error(monkeypatch, capsys):
    from sieve import sleeper_bridge
    monkeypatch.setattr(sleeper_bridge, "sleeper_fetch", lambda *a, **k: {
        "ok": False, "url": a[0], "error": "no client connected: extension/tab unavailable",
    })
    monkeypatch.setattr(sys, "argv", ["sieve", "fetch", "https://spa-tab-failure-20260910.example/?case=extension",
                                       "--reader", "browser", "--browser-backend", "sleeper", "--cache-ttl", "0"])
    assert server.main() == 1
    result = json.loads(capsys.readouterr().out)
    assert result["content_ok"] is False
    assert result["error"] == "The network request failed."

def test_crawl_dispatch_emits_json(monkeypatch, capsys):
    from sieve.crawl import CrawlResponseModel
    async def fake_crawl(self, url, **kwargs):
        return CrawlResponseModel(start_url=url, pages=[])
    monkeypatch.setattr(server.MasterFetchServer, "smart_crawl", fake_crawl)
    monkeypatch.setattr(sys, "argv", ["sieve", "crawl", "https://example.com"])
    assert server.main() in (0, None)
    assert json.loads(capsys.readouterr().out)["start_url"] == "https://example.com"


def test_crawl_sleeper_failure_exits_nonzero(monkeypatch, capsys):
    from sieve.crawl import CrawlPage, CrawlResponseModel
    async def failed(self, url, **kwargs):
        return CrawlResponseModel(start_url=url, pages=[CrawlPage(
            url=url, fetcher_used="sleeper", error="Sleeper daemon not reachable on :8790"
        )])
    monkeypatch.setattr(server.MasterFetchServer, "smart_crawl", failed)
    monkeypatch.setattr(sys, "argv", ["sieve", "crawl", "https://example.com",
                                       "--reader", "browser", "--browser-backend", "sleeper"])
    assert server.main() == 1
    assert "Sleeper daemon not reachable" in capsys.readouterr().out

def test_extract_dispatch_emits_json(monkeypatch, capsys):
    async def fake_fetch(self, url, **kwargs):
        return server.ResponseModel(url=url, status=200, content_ok=True,
                                    content=['<div><h1>hi</h1></div>'])
    monkeypatch.setattr(server.MasterFetchServer, "smart_fetch", fake_fetch)
    monkeypatch.setattr(sys, "argv", ["sieve", "extract", "https://example.com", "--schema", '{"baseSelector":"div","fields":[{"name":"title","selector":"h1","type":"text"}]}'])
    assert server.main() in (0, None)
    assert json.loads(capsys.readouterr().out)["items"][0]["title"] == "hi"


def test_extract_sleeper_failure_exits_nonzero(monkeypatch, capsys):
    async def failed(self, url, schema, **kwargs):
        return {"url": url, "status": 0, "content_ok": False,
                "items": [], "error": "Sleeper daemon not reachable on :8790"}
    monkeypatch.setattr(server.MasterFetchServer, "extract", failed)
    monkeypatch.setattr(sys, "argv", ["sieve", "extract", "https://example.com",
                                       "--schema", "{}", "--reader", "browser",
                                       "--browser-backend", "sleeper"])
    assert server.main() == 1
    assert "Sleeper daemon not reachable" in capsys.readouterr().out


@pytest.mark.asyncio
async def test_extract_preserves_bridge_error_from_smart_fetch(monkeypatch):
    from sieve.server import MasterFetchServer, ResponseModel
    async def failed(self, **kwargs):
        return ResponseModel(
            url=kwargs["url"], status=0, content=[], fetcher_used="sleeper",
            error="no client connected: extension/tab unavailable",
        )
    monkeypatch.setattr(MasterFetchServer, "smart_fetch", failed)
    result = await MasterFetchServer(cache_ttl=0).extract(
        "https://example.com", schema={"baseSelector": "body", "fields": []},
        force_fetcher="sleeper",
    )
    assert result["content_ok"] is False
    assert "extension/tab unavailable" in result["error"]


def test_generic_fetch_auto_uses_sleeper_when_available(monkeypatch, capsys):
    seen = {}
    async def fetched(self, url, **kwargs):
        seen.update(kwargs)
        from sieve.server import ResponseModel
        return ResponseModel(url=url, status=200, content=["real browser"])
    monkeypatch.setattr(server.MasterFetchServer, "smart_fetch", fetched)
    monkeypatch.setattr("sieve.social.resolve_browser_backend", lambda backend: "sleeper")
    monkeypatch.setattr(sys, "argv", ["sieve", "fetch", "https://example.com",
                                       "--browser-backend", "auto"])
    assert server.main() == 0
    assert seen["force_fetcher"] == "sleeper"
    assert json.loads(capsys.readouterr().out)["content"] == ["real browser"]


@pytest.mark.parametrize("backend", ["auto", "pool"])
def test_generic_fetch_pool_backend_forces_stealthy(monkeypatch, capsys, backend):
    seen = {}
    async def fetched(self, url, **kwargs):
        seen.update(kwargs)
        from sieve.server import ResponseModel
        return ResponseModel(url=url, status=200, content=["pool browser"])
    monkeypatch.setattr(server.MasterFetchServer, "smart_fetch", fetched)
    if backend == "auto":
        monkeypatch.setattr("sieve.social.resolve_browser_backend", lambda value: "pool")
    monkeypatch.setattr(sys, "argv", ["sieve", "fetch", "https://example.com",
                                       "--browser-backend", backend])
    assert server.main() == 0
    assert seen["force_fetcher"] == "stealthy"
    assert json.loads(capsys.readouterr().out)["content"] == ["pool browser"]


def test_crawl_pool_backend_forces_stealthy(monkeypatch, capsys):
    from sieve.crawl import CrawlResponseModel
    seen = {}
    async def crawled(self, url, **kwargs):
        seen.update(kwargs)
        return CrawlResponseModel(start_url=url, pages=[])
    monkeypatch.setattr(server.MasterFetchServer, "smart_crawl", crawled)
    monkeypatch.setattr(sys, "argv", ["sieve", "crawl", "https://example.com",
                                       "--browser-backend", "pool"])
    assert server.main() == 0
    assert seen["force_fetcher"] == "stealthy"
    capsys.readouterr()


def test_extract_pool_backend_forces_stealthy(monkeypatch, capsys):
    seen = {}
    async def extracted(self, url, schema, **kwargs):
        seen.update(kwargs)
        return {"url": url, "status": 200, "content_ok": True, "items": [], "error": ""}
    monkeypatch.setattr(server.MasterFetchServer, "extract", extracted)
    monkeypatch.setattr(sys, "argv", ["sieve", "extract", "https://example.com",
                                       "--schema", "{}", "--browser-backend", "pool"])
    assert server.main() == 0
    assert seen["force_fetcher"] == "stealthy"
    capsys.readouterr()

def test_cache_clear_dispatch(monkeypatch, capsys):
    async def fake_clear(self, all=False):
        from sieve.server import CacheInfoModel
        return CacheInfoModel(message="ok", purged=1)
    monkeypatch.setattr(server.MasterFetchServer, "cache_clear", fake_clear)
    monkeypatch.setattr(sys, "argv", ["sieve", "cache", "clear"])
    assert server.main() in (0, None)
    assert json.loads(capsys.readouterr().out)["purged"] == 1

def test_prefixed_crawl_name_is_rejected(monkeypatch, capsys):
    from sieve.crawl import CrawlResponseModel
    async def fake_crawl(self, url, **kwargs):
        return CrawlResponseModel(start_url=url, pages=[])
    monkeypatch.setattr(server.MasterFetchServer, "smart_crawl", fake_crawl)
    monkeypatch.setattr(sys, "argv", ["sieve", "mcp-smart-crawl", "https://example.com"])
    with pytest.raises(SystemExit):
        server.main()


def test_cli_media_layer_passes_through_server_verbs():
    from sieve import cli
    for verb in ["search", "fetch", "crawl", "extract", "screenshot", "cache", "mcp", "keys", "proxy"]:
        assert cli._run_media_command([verb, "anything"]) is None
