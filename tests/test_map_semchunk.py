"""Tests for /map flags + semantic chunking."""
from unittest.mock import patch as mock_patch
from sieve.domain_discovery import discover_domains_sync

def _sitemap_get(url):
    if "sitemap" in url:
        xml = b'<?xml version="1.0"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"><url><loc>https://example.com/a</loc></url><url><loc>https://example.com/blog/b</loc></url><url><loc>https://sub.example.com/c</loc></url><url><loc>https://external.com/d</loc></url></urlset>'
        return (200, xml)
    if url.endswith("/robots.txt"):
        return (200, b"Sitemap: https://example.com/sitemap.xml\nDisallow: /admin\n")
    if url.endswith("/"):
        return (200, b'<html><a href="/extra">extra</a><a href="https://sub.example.com/extra2">sub</a></html>')
    return (404, b"")

def test_sitemap_only():
    res = discover_domains_sync("https://example.com", sources=["sitemap","robots","homepage"], sitemap_only=True, max_urls=20, http_get=_sitemap_get)
    assert "sitemap" in res.sources_used
    assert "robots" not in res.sources_used
    assert "homepage" not in res.sources_used

def test_include_subdomains():
    r0 = discover_domains_sync("https://example.com", sources=["sitemap"], include_subdomains=False, max_urls=20, http_get=_sitemap_get)
    assert not any("sub.example.com" in u for u in r0.urls)
    r1 = discover_domains_sync("https://example.com", sources=["sitemap"], include_subdomains=True, max_urls=20, http_get=_sitemap_get)
    assert any("sub.example.com" in u for u in r1.urls)

def test_allow_external():
    r0 = discover_domains_sync("https://example.com", sources=["sitemap"], allow_external=False, max_urls=20, http_get=_sitemap_get)
    assert not any("external.com" in u for u in r0.urls)
    r1 = discover_domains_sync("https://example.com", sources=["sitemap"], allow_external=True, max_urls=20, http_get=_sitemap_get)
    assert any("external.com" in u for u in r1.urls)

def test_map_search():
    res = discover_domains_sync("https://example.com", sources=["sitemap"], map_search="blog", max_urls=20, http_get=_sitemap_get)
    assert all("blog" in u.lower() for u in res.urls)
    assert len(res.urls) >= 1
    res2 = discover_domains_sync("https://example.com", sources=["sitemap"], map_search="nonexistent123", max_urls=20, http_get=_sitemap_get)
    assert len(res2.urls) == 0

def test_semantic_chunking_basic():
    try:
        import tiktoken
    except ImportError:
        import pytest; pytest.skip("tiktoken not installed")
    from sieve.semchunk import SemanticChunking
    c = SemanticChunking(max_tokens=20, overlap_tokens=5)
    text = "Hello world. This is sentence two. And this is sentence three which is a bit longer. Short one. Another sentence here."
    chunks = c.chunk(text)
    assert len(chunks) >= 2
    # token bound respected
    enc = tiktoken.get_encoding("cl100k_base")
    for ch in chunks:
        assert len(enc.encode(ch)) <= 20

def test_semantic_missing_tiktoken():
    from sieve.semchunk import SemanticChunking
    import builtins
    real = builtins.__import__
    def fake(name, *a, **kw):
        if name == "tiktoken":
            raise ImportError("no tiktoken")
        return real(name, *a, **kw)
    c = SemanticChunking(max_tokens=60, overlap_tokens=10)
    try:
        builtins.__import__ = fake
        try:
            c.chunk("Hello world. Bye.")
        except RuntimeError as e:
            assert ".[nlp]" in str(e)
        else:
            assert False, "expected RuntimeError"
    finally:
        builtins.__import__ = real

def test_semantic_chunking_empty():
    try:
        import tiktoken
    except ImportError:
        import pytest; pytest.skip("tiktoken not installed")
    from sieve.semchunk import SemanticChunking
    # overlap_tokens must stay below max_tokens (constructor validation).
    assert SemanticChunking(max_tokens=10, overlap_tokens=5).chunk("") == []
    assert SemanticChunking(max_tokens=10, overlap_tokens=5).chunk("   ") == []

