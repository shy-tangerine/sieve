"""Tests for domain_discovery + similar ports."""
from sieve.domain_discovery import discover_domains_sync
from sieve.similar import find_similar

def test_discover_domains_sitemap():
    def fake_get(url):
        if "sitemap" in url:
            xml = b'<?xml version="1.0"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"><url><loc>https://example.com/a</loc></url><url><loc>https://example.com/b</loc></url></urlset>'
            return (200, xml)
        if url.endswith("/robots.txt"):
            return (200, b"User-agent: *\nDisallow:")
        return (404, b"")
    res = discover_domains_sync("https://example.com", sources=["sitemap"], max_urls=10, http_get=fake_get)
    assert res.domain == "example.com"
    assert len(res.urls) >= 1

def test_discover_domains_robots():
    def fake_get(url):
        if url.endswith("/robots.txt"):
            return (200, b"Sitemap: https://example.com/sitemap.xml\nDisallow: /admin\nAllow: /docs\n")
        return (404, b"")
    res = discover_domains_sync("https://example.com", sources=["robots"], max_urls=10, http_get=fake_get)
    assert any("admin" in u or "docs" in u or "sitemap" in u for u in res.urls) or "robots" in res.sources_failed or "robots" in res.sources_used

def test_find_similar_basic():
    html = '<html><body><div class="p"><span>a</span></div><div class="p"><span>b</span></div><div class="p"><span>c</span></div></body></html>'
    items = find_similar(html, "div.p")
    # first is original, so 2 similar
    assert len(items) == 2

def test_find_similar_no_match():
    html = '<html><body><div class="x">hi</div></body></html>'
    items = find_similar(html, "div.missing")
    assert items == []

def test_find_similar_text_node_safe():
    html = '<html><body>hello</body></html>'
    items = find_similar(html, "body")
    assert isinstance(items, list)


def test_similar_uses_first_prototype_and_exact_depth_tag_path():
    source = ('<html><body><section><a class="item" href="/first">first</a>'
              '<a class="item" href="/second">second</a></section>'
              '<aside><a class="item">wrong parent</a></aside>'
              '<div><section><a class="item">wrong depth</a></section></div></body></html>')
    items = find_similar(source, "a.item", threshold=1)
    assert [item["text"] for item in items] == ["second"]
    assert items[0]["attributes"]["href"] == "/second"
    assert "second" in items[0]["html"]
    assert find_similar(source, "a.item", threshold=1, ignore_attrs=[]) == []
    assert find_similar(source, "a.item", threshold=1, match_text=True) == []


def test_similar_excludes_src_by_default_and_rejects_invalid_inputs():
    import pytest

    source = '<main><img class="photo" src="a.png"><img class="photo" src="b.png"></main>'
    assert len(find_similar(source, "img", threshold=1)) == 1
    assert find_similar(source, "[") == []
    for selector in ("", "p" * 2001):
        with pytest.raises(ValueError):
            find_similar(source, selector)
    with pytest.raises(ValueError):
        find_similar("x" * 10_000_001, "p")
    for threshold in (-1, 2, float("nan"), True):
        with pytest.raises(ValueError):
            find_similar(source, "img", threshold=threshold)


def test_domain_filter_rejects_lookalike_suffix_host():
    def fake_get(url):
        if url.endswith("/robots.txt"):
            return (200, b"Sitemap: https://notexample.com/sitemap.xml\nAllow: https://sub.example.com/docs\n")
        return (404, b"")

    res = discover_domains_sync(
        "https://example.com",
        sources=["robots"],
        include_subdomains=True,
        max_urls=10,
        http_get=fake_get,
    )
    assert "https://notexample.com/sitemap.xml" not in res.urls
    assert "https://sub.example.com/docs" in res.urls


def test_discovery_feed_homepage_caps_and_unimplemented_sources_are_reported():
    calls = []

    def fake_get(url):
        calls.append(url)
        if url.endswith("/feed"):
            return 200, ("<rss>" + "".join(
                f"<link>https://example.com/feed/{i}</link>" for i in range(60)
            ) + "</rss>").encode()
        if url.endswith("/"):
            return 200, "".join(f'<a href="/landing/{i}#part">item</a>' for i in range(110)).encode()
        return 404, b""

    result = discover_domains_sync(
        "https://example.com", sources=["feed", "homepage", "wayback", "crt", "cc", "probe"],
        max_urls=500, http_get=fake_get,
    )
    assert calls == ["https://example.com/feed", "https://example.com/"]
    assert len(result.urls) == 150
    assert result.sources_used == ["feed", "homepage"]
    assert result.sources_failed == ["wayback", "crt", "cc", "probe"]
    assert all("#" not in url for url in result.urls)
    limited = discover_domains_sync("https://example.com", sources=["homepage"], max_urls=3,
                                    map_search="LANDING/", http_get=fake_get)
    assert limited.urls == [f"https://example.com/landing/{i}" for i in range(3)]


def test_discovery_default_transport_caps_body_and_timeout(monkeypatch):
    import urllib.request
    import sieve.domain_discovery as discovery

    reads = []
    opens = []

    class Response:
        status = 200
        def __enter__(self):
            return self
        def __exit__(self, *_):
            pass
        def read(self, size):
            reads.append(size)
            return b"x" * size

    class Opener:
        def open(self, request, *, timeout):
            opens.append((request.full_url, timeout))
            return Response()

    monkeypatch.setattr(discovery, "validate_url", lambda url: url)
    monkeypatch.setattr(urllib.request, "build_opener", lambda *_: Opener())
    assert discovery._stdlib_http_get("https://example.com/") is None
    assert opens == [("https://example.com/", 10)]
    assert reads == [512 * 1024 + 1]


def test_discovery_unknown_and_unimplemented_sources_make_no_requests():
    calls = []
    for sources in (["unknown"], [], ["wayback", "crt", "cc", "probe"]):
        result = discover_domains_sync("https://example.com", sources=sources,
                                        http_get=lambda url: calls.append(url))
        assert result.urls == [] and result.sources_used == [] and result.via == "none"
        assert result.sources_failed == [source for source in sources if source != "unknown"]
    assert calls == []
