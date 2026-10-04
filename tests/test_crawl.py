from types import SimpleNamespace

import pytest

from sieve import crawl


def test_discovered_links_are_reserved_before_list_materialization():
    from sieve.resource_budget import ResourceBudget, budget_scope
    html = "".join(f'<a href="/{i}">link</a>' for i in range(100))
    account = ResourceBudget(limits={"items": 2})
    with budget_scope(account):
        links = crawl.extract_same_domain_links(html, "https://example.com", "https://example.com")
    assert len(links) == account.consumed["items"] == 2
    assert "items" in account.truncated


@pytest.mark.asyncio
async def test_sitemap_discovery_receives_remaining_item_capacity(monkeypatch):
    from sieve import sitemap
    from sieve.resource_budget import ResourceBudget, budget_scope
    capacities = []
    def discover(url, *, http_get, max_urls):
        capacities.append(max_urls)
        return sitemap.SitemapResult()
    monkeypatch.setattr(sitemap, "discover_sitemap", discover)
    account = ResourceBudget(limits={"items": 2})
    with budget_scope(account):
        await crawl._sitemap_map("https://example.com", None, None, max_pages=10, deadline_t=float("inf"))
    assert capacities == [2]


@pytest.mark.asyncio
async def test_sitemap_transport_refuses_private_redirect_before_second_get(monkeypatch):
    import io
    import urllib.request
    from urllib.error import HTTPError
    calls = []
    class OfflineOpener:
        def open(self, request, **kwargs):
            calls.append(request.full_url)
            raise HTTPError(request.full_url, 302, "Found", {"Location": "http://127.0.0.1/private"}, io.BytesIO())
    monkeypatch.setattr(crawl, "resolve_and_check", lambda *args: None)
    monkeypatch.setattr(urllib.request, "build_opener", lambda *args: OfflineOpener())
    result = await crawl._sitemap_map("https://example.com", None, None, max_pages=1, deadline_t=float("inf"))
    assert result is None
    assert calls and all(url.startswith("https://example.com/") for url in calls)


@pytest.mark.asyncio
async def test_sitemap_transport_reads_only_remaining_bytes_plus_sentinel(monkeypatch):
    import urllib.request
    from sieve.resource_budget import ResourceBudget, budget_scope
    reads = []
    class Response:
        status = 200
        headers = {}
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def read(self, amount):
            reads.append(amount)
            return b"x" * amount
    monkeypatch.setattr(crawl, "resolve_and_check", lambda *args: None)
    monkeypatch.setattr(urllib.request, "build_opener", lambda *args: SimpleNamespace(open=lambda *args, **kwargs: Response()))
    account = ResourceBudget(limits={"input_bytes": 10})
    with budget_scope(account):
        result = await crawl._sitemap_map("https://example.com", None, None, max_pages=1, deadline_t=float("inf"))
    assert result is None
    assert reads == [11]
    assert account.consumed["input_bytes"] == 10


@pytest.mark.asyncio
async def test_sitemap_dns_guard_runs_before_transport(monkeypatch):
    import socket
    import urllib.request
    calls = []
    monkeypatch.setattr(socket, "getaddrinfo", lambda *args, **kwargs: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.1", 443))])
    monkeypatch.setattr(urllib.request, "build_opener", lambda *args: SimpleNamespace(open=lambda *args, **kwargs: calls.append(args)))
    result = await crawl._sitemap_map("https://example.com", None, None, max_pages=1, deadline_t=float("inf"))
    assert result is None and calls == []


class _FakeServer:
    async def smart_fetch(self, **kwargs):
        return SimpleNamespace(
            url=kwargs["url"],
            status=200,
            content=["<html><head><title>Page</title></head><body><p>content</p></body></html>"],
            content_ok=True,
            fetcher_used="http",
            error="",
            summary="",
            fetched_at="",
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("shared_priority", [10, -10])
async def test_later_shallow_route_updates_depth_without_duplicate_work(monkeypatch, shared_priority):
    from urllib.parse import urlparse
    graph = {"/": ["/deep", "/short"], "/deep": ["/middle"],
             "/middle": ["/shared"], "/short": ["/shared"],
             "/shared": ["/child"], "/child": []}
    calls = []
    parsed = []
    original = crawl.extract_same_domain_links

    def links(html, page_url, *args):
        parsed.append(urlparse(page_url).path)
        return original(html, page_url, *args)

    class Server(_FakeServer):
        async def smart_fetch(self, **kwargs):
            path = urlparse(kwargs["url"]).path
            calls.append(path)
            response = await super().smart_fetch(**kwargs)
            response.content = ["<html><body>" + "".join(
                f'<a href="{link}">link</a>' for link in graph[path]) + "</body></html>"]
            return response

    monkeypatch.setattr(crawl, "extract_same_domain_links", links)
    monkeypatch.setattr(crawl, "score_link", lambda url, *_: {
        "/deep": 10, "/middle": 10, "/shared": shared_priority,
        "/short": 0, "/child": 0}.get(urlparse(url).path, 0))
    result = await crawl.smart_crawl(Server(), "https://example.test/", max_pages=10,
                                     max_depth=3, concurrency=1, discover_only=True)
    pages = {urlparse(page.url).path: page for page in result.pages}
    assert set(pages) == set(graph)
    assert pages["/shared"].depth == 2
    assert pages["/child"].depth == 3
    assert calls.count("/shared") == parsed.count("/shared") == 1
    assert len(calls) == len(result.pages) == result.resource_budget["consumed"]["pages"] == 6


@pytest.mark.asyncio
async def test_auto_throttle_paces_requests_per_domain(monkeypatch):
    sleeps = []

    async def fake_sleep(delay):
        sleeps.append(delay)

    monkeypatch.setattr(crawl.asyncio, "sleep", fake_sleep)
    result = await crawl.smart_crawl(
        _FakeServer(),
        "https://example.test/",
        crawl_urls=["https://example.test/one", "https://example.test/two"],
        max_pages=2,
        concurrency=2,
        max_content_chars_per=500,
        auto_throttle=True,
        throttle_min_delay=0.1,
    )

    assert result.pages_crawled == 2
    assert sleeps
    assert 0.05 < sleeps[0] <= 0.1


@pytest.mark.asyncio
async def test_crawl_aggregate_content_and_shared_request_budget(monkeypatch):
    from sieve.resource_budget import current_budget
    accounts = []

    class Server(_FakeServer):
        async def smart_fetch(self, **kwargs):
            accounts.append(current_budget())
            return await super().smart_fetch(**kwargs)

    monkeypatch.setattr(crawl, "_classify_and_extract", lambda *args: ("x" * 500, "article", True))
    result = await crawl.smart_crawl(
        Server(), "https://example.test/", max_pages=2, concurrency=2,
        crawl_urls=["/one", "/two"], max_content_chars_per=500, max_total_chars=600,
    )
    assert sum(page.content_chars for page in result.pages) == 600
    assert result.truncated_by_budget
    assert result.pages[-1].is_truncated
    assert accounts[0] is accounts[1]
    assert result.resource_budget["consumed"]["output_chars"] == 600
    assert "output_chars" in result.resource_budget["truncated"]


@pytest.mark.asyncio
async def test_crawl_deadline_cancels_inflight_fetch(monkeypatch):
    import asyncio
    cancelled = []
    monkeypatch.setattr("sieve.security.validate_url", lambda url: url)

    class SlowServer:
        async def smart_fetch(self, **kwargs):
            try:
                await asyncio.sleep(10)
            finally:
                cancelled.append(True)

    result = await crawl.smart_crawl(SlowServer(), "https://example.test", deadline_ms=100)
    assert result.truncated_by_time
    assert result.pages == []
    assert cancelled == [True]


@pytest.mark.asyncio
@pytest.mark.parametrize("remaining,expected", [(1, 1), (0, 0)])
async def test_shared_page_budget_preserves_partial_crawl(monkeypatch, remaining, expected):
    from sieve.resource_budget import ResourceBudget, budget_scope
    monkeypatch.setattr("sieve.security.validate_url", lambda url: url)
    account = ResourceBudget(limits={"pages": 2})
    account.charge("pages", 2 - remaining)
    with budget_scope(account):
        result = await crawl.smart_crawl(_FakeServer(), "https://example.test/",
                                         crawl_urls=["/one", "/two"], max_pages=2)
    assert len(result.pages) == expected
    assert result.truncated_by_budget
    assert "pages" in result.resource_budget["truncated"]
    assert not result.error


@pytest.mark.asyncio
async def test_sitemap_map_shares_item_budget(monkeypatch):
    from sieve.resource_budget import ResourceBudget, budget_scope
    monkeypatch.setattr("sieve.security.validate_url", lambda url: url)

    async def mapped(*args, **kwargs):
        return crawl.CrawlResponseModel(start_url="https://example.test", discover_only=True,
            pages=[crawl.CrawlPage(url="https://example.test/" + str(i), depth=0, status=200,
                                   content_ok=True, fetcher_used="sitemap") for i in range(5)])

    monkeypatch.setattr(crawl, "_sitemap_map", mapped)
    with budget_scope(ResourceBudget(limits={"items": 2})):
        result = await crawl.smart_crawl(_FakeServer(), "https://example.test", sitemap=True)
    assert len(result.pages) == result.pages_discovered == 2
    assert result.truncated_by_budget
    assert result.resource_budget["truncated"] == ["items"]
