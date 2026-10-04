"""Boundary validation of server_search wrappers before expensive imports (#128).

Invalid bounds (bool, NaN, out-of-range, oversize) must return stable
``category="validation"`` responses without executing or importing the
heavyweight search/crawl backends. Valid bounds pass through with shared
coercion, mirroring the downstream semantic validators in sieve.search.
"""
from __future__ import annotations

import pytest

from sieve.server_search import smart_search, smart_crawl


def _guard(name: str):
    async def _fail(*args, **kwargs):
        raise AssertionError(f"backend {name} executed for invalid input")
    return _fail


@pytest.fixture(autouse=True)
def _no_backend_execution(monkeypatch):
    """Any backend execution during these tests is a contract failure."""
    import sieve.search
    import sieve.crawl
    monkeypatch.setattr(sieve.search, "smart_search", _guard("sieve.search.smart_search"))
    monkeypatch.setattr(sieve.crawl, "smart_crawl", _guard("sieve.crawl.smart_crawl"))


# ── smart_search bounds ──────────────────────────────────────────────

class TestSmartSearchBounds:
    @pytest.mark.asyncio
    async def test_backend_exception_is_sanitized_and_categorized(self, monkeypatch):
        import sieve.search

        async def fail(*args, **kwargs):
            raise RuntimeError("SENTINEL_SECRET /private/path token=private")

        monkeypatch.setattr(sieve.search, "smart_search", fail)
        res = await smart_search(object(), "demo")

        assert res.category == "network"
        assert "SENTINEL_SECRET" not in res.error
        assert "/private/path" not in res.error
        assert "private" not in res.error

    @pytest.mark.asyncio
    async def test_bool_max_results_rejected(self):
        res = await smart_search(object(), "demo", max_results=True)
        assert res.category == "validation"
        assert "max_results" in res.error

    @pytest.mark.asyncio
    async def test_nan_max_results_rejected(self):
        res = await smart_search(object(), "demo", max_results=float("nan"))
        assert res.category == "validation"
        assert "max_results" in res.error

    @pytest.mark.asyncio
    @pytest.mark.parametrize("bad", [-1, 0, 101, 10**9])
    async def test_out_of_range_max_results_rejected(self, bad):
        res = await smart_search(object(), "demo", max_results=bad)
        assert res.category == "validation"
        assert "max_results" in res.error

    @pytest.mark.asyncio
    @pytest.mark.parametrize("bad", [-1, 11, 10**6])
    async def test_out_of_range_page_rejected(self, bad):
        res = await smart_search(object(), "demo", page=bad)
        assert res.category == "validation"
        assert "page" in res.error

    @pytest.mark.asyncio
    async def test_negative_cache_ttl_rejected(self):
        res = await smart_search(object(), "demo", cache_ttl=-1)
        assert res.category == "validation"
        assert "cache_ttl" in res.error

    @pytest.mark.asyncio
    async def test_oversize_cache_ttl_rejected(self):
        res = await smart_search(object(), "demo", cache_ttl=10**9)
        assert res.category == "validation"
        assert "cache_ttl" in res.error

    @pytest.mark.asyncio
    async def test_engines_string_not_list_rejected(self):
        res = await smart_search(object(), "demo", engines="ddg")
        assert res.category == "validation"
        assert "engines" in res.error

    @pytest.mark.asyncio
    async def test_engines_oversize_list_rejected(self):
        res = await smart_search(object(), "demo", engines=["ddg"] * 13)
        assert res.category == "validation"
        assert "engines" in res.error

    @pytest.mark.asyncio
    async def test_exclude_sites_oversize_list_rejected(self):
        res = await smart_search(object(), "demo", exclude_sites=[f"d{i}.com" for i in range(21)])
        assert res.category == "validation"
        assert "exclude_sites" in res.error

    @pytest.mark.asyncio
    async def test_site_non_string_rejected(self):
        res = await smart_search(object(), "demo", site=123)
        assert res.category == "validation"
        assert "site" in res.error

    @pytest.mark.asyncio
    async def test_site_oversize_rejected(self):
        res = await smart_search(object(), "demo", site="a" * 300)
        assert res.category == "validation"
        assert "site" in res.error

    @pytest.mark.asyncio
    async def test_invalid_query_still_stable(self):
        res = await smart_search(object(), "")
        assert res.category == "validation"

    @pytest.mark.asyncio
    async def test_valid_bounds_pass_through(self, monkeypatch):
        seen = {}

        async def fake_search(*args, **kwargs):
            seen.update(kwargs)
            seen["args"] = args
            from sieve.search import SearchResponseModel
            return SearchResponseModel(query=args[1], results=[], total_results=0)

        import sieve.search
        monkeypatch.setattr(sieve.search, "smart_search", fake_search)
        res = await smart_search(object(), "demo", max_results=6.0, page=2,
                                 cache_ttl=3600, engines=["ddg"], site="docs.python.org")
        assert res.total_results == 0
        assert seen["args"][1] == "demo"
        assert seen["page"] == 2
        assert seen["engines"] == ["ddg"]
        assert seen["site"] == "docs.python.org"


# ── smart_crawl bounds ───────────────────────────────────────────────

class TestSmartCrawlBounds:
    @pytest.mark.asyncio
    async def test_backend_exception_is_sanitized(self, monkeypatch):
        import sieve.crawl

        async def fail(*args, **kwargs):
            raise RuntimeError("SENTINEL_SECRET /private/path token=private")

        monkeypatch.setattr(sieve.crawl, "smart_crawl", fail)
        res = await smart_crawl(object(), "https://example.com")

        assert res.pages == []
        assert "SENTINEL_SECRET" not in res.error
        assert "/private/path" not in res.error
        assert "private" not in res.error
    @pytest.mark.asyncio
    async def test_invalid_url_rejected(self):
        res = await smart_crawl(object(), "")
        assert res.pages == []
        assert res.error

    @pytest.mark.asyncio
    @pytest.mark.parametrize("bad", [0, -3, 201, 10**6])
    async def test_out_of_range_max_pages_rejected(self, bad):
        res = await smart_crawl(object(), "https://example.com", max_pages=bad)
        assert res.pages == []
        assert "max_pages" in res.error

    @pytest.mark.asyncio
    async def test_bool_concurrency_rejected(self):
        res = await smart_crawl(object(), "https://example.com", concurrency=True)
        assert res.pages == []
        assert "concurrency" in res.error

    @pytest.mark.asyncio
    async def test_out_of_range_concurrency_rejected(self):
        res = await smart_crawl(object(), "https://example.com", concurrency=50)
        assert res.pages == []
        assert "concurrency" in res.error

    @pytest.mark.asyncio
    async def test_oversize_timeout_rejected(self):
        res = await smart_crawl(object(), "https://example.com", timeout=10**7)
        assert res.pages == []
        assert "timeout" in res.error

    @pytest.mark.asyncio
    async def test_oversize_deadline_rejected(self):
        res = await smart_crawl(object(), "https://example.com", deadline_ms=10**8)
        assert res.pages == []
        assert "deadline_ms" in res.error

    @pytest.mark.asyncio
    async def test_nan_throttle_delay_rejected(self):
        res = await smart_crawl(object(), "https://example.com",
                                throttle_min_delay=float("nan"))
        assert res.pages == []
        assert "throttle_min_delay" in res.error

    @pytest.mark.asyncio
    async def test_path_include_string_not_list_rejected(self):
        res = await smart_crawl(object(), "https://example.com", path_include="/docs")
        assert res.pages == []
        assert "path_include" in res.error

    @pytest.mark.asyncio
    async def test_undersized_max_total_chars_rejected(self):
        res = await smart_crawl(object(), "https://example.com", max_total_chars=10)
        assert res.pages == []
        assert "max_total_chars" in res.error

    @pytest.mark.asyncio
    async def test_private_target_rejected_before_backend(self):
        res = await smart_crawl(object(), "http://127.0.0.1:8080/x")
        assert res.pages == []
        assert res.error
        assert "concurrency" not in res.error

    @pytest.mark.asyncio
    async def test_valid_bounds_pass_through(self, monkeypatch):
        seen = {}

        async def fake_crawl(*args, **kwargs):
            seen.update(kwargs)
            seen["args"] = args
            from sieve.crawl import CrawlResponseModel
            return CrawlResponseModel(start_url=args[1], pages=[])

        import sieve.crawl
        monkeypatch.setattr(sieve.crawl, "smart_crawl", fake_crawl)
        res = await smart_crawl(object(), "https://example.com", max_pages=5.0,
                                max_depth=3, concurrency=4, timeout=20000,
                                deadline_ms=60000, max_total_chars=50000)
        assert res.pages == []
        assert seen["args"][1] == "https://example.com"
        assert seen["max_pages"] == 5
        assert seen["max_depth"] == 3
        assert seen["concurrency"] == 4
        assert seen["timeout"] == 20000
        assert seen["deadline_ms"] == 60000
        assert seen["max_total_chars"] == 50000
