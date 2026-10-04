"""Characterization tests pinning behavior before complexity refactor.
Deterministic, no network — mocks transports. Covers search.py, crawl.py, browser.py helpers."""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch


# ─── search.py pins ─────────────────────────────────────────────────

class TestDomainBoost:
    def test_tech_domain_boosted_when_technical(self):
        from sieve.search import _domain_boost
        assert _domain_boost("https://github.com/foo/bar", "model architecture", True) == 0.15

    def test_tech_domain_not_boosted_when_not_technical(self):
        from sieve.search import _domain_boost
        assert _domain_boost("https://github.com/foo/bar", "hello world", False) == 0.0

    def test_reference_domain_always_boosted(self):
        from sieve.search import _domain_boost
        assert _domain_boost("https://en.wikipedia.org/wiki/Foo", "anything", False) == 0.05

    def test_subdomain_matches(self):
        from sieve.search import _domain_boost
        assert _domain_boost("https://en.wikipedia.org/wiki/Foo", "test", False) == 0.05
        assert _domain_boost("https://raw.githubusercontent.com/x", "model", True) == 0.15

    def test_unknown_domain_no_boost(self):
        from sieve.search import _domain_boost
        assert _domain_boost("https://example.com/page", "model", True) == 0.0

    def test_empty_url_no_boost(self):
        from sieve.search import _domain_boost
        assert _domain_boost("", "model", True) == 0.0
        assert _domain_boost("not-a-url", "model", True) == 0.0

    def test_www_stripped(self):
        from sieve.search import _domain_boost
        assert _domain_boost("https://www.github.com/foo", "model", True) == 0.15


class TestAnswerSignalScore:
    def test_digits_boost_for_technical(self):
        from sieve.search import _answer_signal_score
        assert _answer_signal_score("model", "d_model=12288 layers 96", True) >= 0.15

    def test_empty_snippet_zero(self):
        from sieve.search import _answer_signal_score
        assert _answer_signal_score("query", "", True) == 0.0

    def test_table_markers(self):
        from sieve.search import _answer_signal_score
        assert _answer_signal_score("table comparison", "col1 | col2 | row data", False) >= 0.10

    def test_code_markers(self):
        from sieve.search import _answer_signal_score
        assert _answer_signal_score("code example", "def foo():\n import os", False) >= 0.10

    def test_comparison_markers(self):
        from sieve.search import _answer_signal_score
        assert _answer_signal_score("compare vs", "better faster while whereas", False) >= 0.10

    def test_capped_at_030(self):
        from sieve.search import _answer_signal_score
        s = _answer_signal_score("code table compare vs dimension size", "12345 table | def foo vs better", True)
        assert s <= 0.30


class TestIsTechnicalQuery:
    def test_technical_detected(self):
        from sieve.search import _is_technical_query
        assert _is_technical_query("transformer architecture d_model") is True

    def test_non_technical(self):
        from sieve.search import _is_technical_query
        assert _is_technical_query("best pizza near me") is False


class TestSourceType:
    def test_docs(self):
        from sieve.search import _source_type
        assert _source_type("https://docs.python.org/3/library") == "docs"

    def test_paper(self):
        from sieve.search import _source_type
        assert _source_type("https://arxiv.org/abs/1234") == "paper"

    def test_repo(self):
        from sieve.search import _source_type
        assert _source_type("https://github.com/foo/bar") == "repo"

    def test_forum(self):
        from sieve.search import _source_type
        assert _source_type("https://stackoverflow.com/questions/1") == "forum"

    def test_reference(self):
        from sieve.search import _source_type
        assert _source_type("https://en.wikipedia.org/wiki/Foo") == "reference"

    def test_blog(self):
        from sieve.search import _source_type
        assert _source_type("https://medium.com/@foo/bar") == "blog"

    def test_news(self):
        from sieve.search import _source_type
        assert _source_type("https://techcrunch.com/2024/foo") == "news"

    def test_other(self):
        from sieve.search import _source_type
        assert _source_type("https://example.com/page") == "other"

    def test_subdomain_match(self):
        from sieve.search import _source_type
        assert _source_type("https://gist.github.com/foo") == "repo"

    def test_path_heuristic_docs(self):
        from sieve.search import _source_type
        assert _source_type("https://example.com/docs/api") == "docs"

    def test_path_heuristic_blog(self):
        from sieve.search import _source_type
        assert _source_type("https://example.com/blog/post") == "blog"

    def test_empty_url(self):
        from sieve.search import _source_type
        assert _source_type("") == "other"
        assert _source_type("not-a-url") == "other"


class TestTier:
    def test_high_by_score(self):
        from sieve.search import _tier
        assert _tier(0.6, 5, 10) == "high"

    def test_high_by_rank_one(self):
        from sieve.search import _tier
        assert _tier(0.01, 1, 10) == "high"

    def test_high_by_consensus_promotion(self):
        from sieve.search import _tier
        assert _tier(0.3, 3, 10, consensus=3) == "high"

    def test_high_by_domain_boost(self):
        from sieve.search import _tier
        assert _tier(0.3, 3, 10, domain_boosted=True) == "high"

    def test_med_threshold(self):
        from sieve.search import _tier
        assert _tier(0.2, 5, 10) == "med"

    def test_low(self):
        from sieve.search import _tier
        assert _tier(0.05, 8, 10) == "low"


class TestDiversify:
    def test_diversify_limits_domain(self):
        from sieve.search import _diversify
        from sieve.search_engines import RawResult
        r1 = RawResult(title="a", url="https://example.com/1", snippet="", source="duckduckgo", position=1)
        r2 = RawResult(title="b", url="https://example.com/2", snippet="", source="duckduckgo", position=2)
        r3 = RawResult(title="c", url="https://example.com/3", snippet="", source="duckduckgo", position=3)
        r4 = RawResult(title="d", url="https://other.com/1", snippet="", source="duckduckgo", position=4)
        ranked = [r1, r2, r3, r4]
        scores = [1.0, 0.9, 0.8, 0.7]
        new_ranked, new_scores = _diversify(ranked, scores, max_per_domain=2)
        # first 2 example.com kept, 3rd deferred after other.com
        assert new_ranked[0].url == "https://example.com/1"
        assert new_ranked[1].url == "https://example.com/2"
        assert new_ranked[2].url == "https://other.com/1"
        assert new_ranked[3].url == "https://example.com/3"

    def test_diversify_empty(self):
        from sieve.search import _diversify
        assert _diversify([], []) == ([], [])


class TestRelatedQueries:
    def test_mines_bigrams(self):
        from sieve.search import _related_queries, SearchResult
        results = [
            SearchResult(title="neural network training", url="https://a.com", snippet="neural network training uses gradient descent neural network training is key", position=1),
            SearchResult(title="neural network training guide", url="https://b.com", snippet="neural network training explained", position=2),
            SearchResult(title="neural network training tips", url="https://c.com", snippet="neural network training tutorial", position=3),
        ]
        qs = _related_queries("deep learning", results, n=3)
        # Should return bigrams that appear in 3+ docs and don't overlap query
        assert isinstance(qs, list)

    def test_empty_results(self):
        from sieve.search import _related_queries
        assert _related_queries("query", []) == []


class TestValidateFilters:
    def test_valid_site(self):
        from sieve.search import _validate_filters
        _validate_filters("docs.python.org", None, None, None, None)

    def test_invalid_site_raises(self):
        from sieve.search import _validate_filters
        from sieve.security import SecurityError
        with pytest.raises(SecurityError):
            _validate_filters("not a domain", None, None, None, None)

    def test_invalid_page_raises(self):
        from sieve.search import _validate_filters
        from sieve.security import SecurityError
        with pytest.raises(SecurityError):
            _validate_filters(None, None, None, None, 99)


class TestValidateEngines:
    def test_none_passthrough(self):
        from sieve.search import _validate_engines
        assert _validate_engines(None) is None

    def test_valid_engines(self):
        from sieve.search import _validate_engines
        assert _validate_engines(["duckduckgo"]) == ["duckduckgo"]

    def test_invalid_engine_raises(self):
        from sieve.search import _validate_engines
        from sieve.security import SecurityError
        with pytest.raises(SecurityError):
            _validate_engines(["notanengine"])


class TestSearchNextAction:
    def test_empty_results(self):
        from sieve.search import _search_next_action
        msg = _search_next_action([], [], "", "test query")
        assert "Rephrase" in msg or "No results" in msg

    def test_empty_rate_limited(self):
        from sieve.search import _search_next_action
        msg = _search_next_action([], ["duckduckgo"], "rate-limited", "q")
        assert "rate-limited" in msg.lower() or "Retry" in msg

    def test_with_results_general(self):
        from sieve.search import _search_next_action, SearchResult
        r = SearchResult(title="t", url="https://example.com/p", snippet="hello world", source="duckduckgo", position=1, fetch_relevance="high", source_type="other")
        msg = _search_next_action([r], [], "", "hello world")
        assert "smart_fetch" in msg.lower() or "fetch" in msg.lower()


class TestSmartSearchValidation:
    @pytest.mark.asyncio
    async def test_invalid_query_returns_error(self):
        from sieve.search import smart_search
        resp = await smart_search(MagicMock(), query="", max_results=5)
        assert resp.error != ""

    @pytest.mark.asyncio
    async def test_invalid_site_returns_error(self):
        from sieve.search import smart_search
        resp = await smart_search(MagicMock(), query="hello world test query", site="bad domain!")
        assert resp.error != ""

    @pytest.mark.asyncio
    async def test_find_similar_no_url_returns_error(self):
        from sieve.search import smart_search
        resp = await smart_search(MagicMock(), query="hello world test", mode="find_similar")
        assert "find_similar" in resp.error

    @pytest.mark.asyncio
    async def test_cache_hit_returns_cached(self):
        from sieve.search import smart_search
        import json
        mock_server = MagicMock()
        fake_data = json.dumps({"results": [{"title": "t", "url": "https://example.com", "snippet": "s", "source": "duckduckgo", "position": 1, "relevance_score": 0.9, "fetch_relevance": "high", "engines_consensus": "1 of 1", "source_type": "other"}], "engines_used": ["duckduckgo"], "engine_blocked": [], "rerank_mode": "merge", "related_queries": []})
        with patch("sieve.search.get_cached", new=AsyncMock(return_value={"content": [fake_data]})), patch("sieve.search_api_keys.get_byok_engines", return_value={}):
            resp = await smart_search(mock_server, query="hello world test query for cache", cache_ttl=300)
            assert resp.cached is True
            assert len(resp.results) == 1

    @pytest.mark.asyncio
    async def test_cache_only_miss_does_not_query_engines(self):
        from sieve.search import smart_search
        with patch("sieve.search.get_cached", new=AsyncMock(return_value=None)), \
             patch("sieve.search.multi_search", new=AsyncMock(side_effect=AssertionError("engine called"))):
            resp = await smart_search(object(), query="cache only", cached=True, cache_ttl=300)
        assert resp.error == "No fresh cached results found."
        assert resp.results == []

    @pytest.mark.asyncio
    async def test_live_search_mocked(self):
        from sieve.search import smart_search
        from sieve.search_engines import RawResult, EngineReport
        mock_server = MagicMock()
        rr = RawResult(title="Test", url="https://example.com/page", snippet="hello world", source="duckduckgo", position=1, sources=("duckduckgo",), consensus=1)
        report = EngineReport(name="duckduckgo", ok=True, blocked=False)
        with patch("sieve.search.get_cached", new=AsyncMock(return_value=None)), \
             patch("sieve.search.set_cached", new=AsyncMock(return_value=None)), \
             patch("sieve.search.multi_search", new=AsyncMock(return_value=([rr], [report]))), \
             patch("sieve.search.ensure_reranker", new=AsyncMock(return_value=None)), \
             patch("sieve.search.neural_rerank", return_value=None):
            resp = await smart_search(mock_server, query="hello world test query live", cache_ttl=0)
            assert len(resp.results) >= 1
            assert resp.results[0].url == "https://example.com/page"


# ─── crawl.py pins ──────────────────────────────────────────────────

class TestNormalizeUrl:
    def test_lowercase_host(self):
        from sieve.crawl import normalize_url
        assert normalize_url("https://Example.COM/page") == "https://example.com/page"

    def test_strip_default_port(self):
        from sieve.crawl import normalize_url
        assert normalize_url("https://example.com:443/page") == "https://example.com/page"
        assert normalize_url("http://example.com:80/page") == "http://example.com/page"

    def test_collapse_trailing_slash(self):
        from sieve.crawl import normalize_url
        assert normalize_url("https://example.com/docs/") == "https://example.com/docs"
        assert normalize_url("https://example.com/") == "https://example.com/"

    def test_strip_tracking_params(self):
        from sieve.crawl import normalize_url
        u = normalize_url("https://example.com/page?utm_source=foo&x=1&fbclid=bar")
        assert "utm_source" not in u
        assert "fbclid" not in u
        assert "x=1" in u

    def test_preserve_real_params(self):
        from sieve.crawl import normalize_url
        u = normalize_url("https://example.com/page?page=2&sort=asc")
        assert "page=2" in u

    def test_drop_fragment(self):
        from sieve.crawl import normalize_url
        u = normalize_url("https://example.com/page#section")
        assert "#section" not in u


class TestExtractSameDomainLinks:
    def test_extracts_same_domain(self):
        from sieve.crawl import extract_same_domain_links
        html = '<a href="/page2">Page 2</a><a href="https://example.com/page3">Page 3</a>'
        links = extract_same_domain_links(html, "https://example.com/", "https://example.com/")
        assert len(links) == 2

    def test_drops_external(self):
        from sieve.crawl import extract_same_domain_links
        html = '<a href="https://other.com/page">External</a><a href="/local">Local</a>'
        links = extract_same_domain_links(html, "https://example.com/", "https://example.com/")
        assert all("other.com" not in u for u, _ in links)
        assert len(links) == 1

    def test_drops_assets(self):
        from sieve.crawl import extract_same_domain_links
        html = '<a href="/image.png">img</a><a href="/page">page</a>'
        links = extract_same_domain_links(html, "https://example.com/", "https://example.com/")
        assert len(links) == 1
        assert links[0][0].endswith("/page")

    def test_dedup_normalized(self):
        from sieve.crawl import extract_same_domain_links
        html = '<a href="/docs">a</a><a href="/docs/">b</a>'
        links = extract_same_domain_links(html, "https://example.com/", "https://example.com/")
        assert len(links) == 1

    def test_path_filter(self):
        from sieve.crawl import extract_same_domain_links
        html = '<a href="/docs/a">a</a><a href="/blog/b">b</a>'
        links = extract_same_domain_links(html, "https://example.com/", "https://example.com/", path_include=["/docs"])
        assert len(links) == 1


class TestClassifyAndExtract:
    def test_article_with_content(self):
        from sieve.crawl import _classify_and_extract
        html = "<html><head><title>T</title></head><body><article>" + ("<p>Hello world content. </p>" * 30) + "</article></body></html>"
        with patch("sieve.trafilatura_extractor.extract_content_from_html", return_value="Hello world content. " * 30):
            md, kind, ok = _classify_and_extract(html, "https://example.com/a", "https://example.com/", None, 8000)
            assert kind in ("article", "fallback", "list")
            assert ok is True or len(md) > 0

    def test_js_shell(self):
        from sieve.crawl import _classify_and_extract
        html = '<html><body><div id="root"></div>' + "<script>app</script>" * 10 + "</body></html>"
        with patch("sieve.trafilatura_extractor.extract_content_from_html", return_value=""):
            md, kind, ok = _classify_and_extract(html, "https://example.com/a", "https://example.com/", None, 8000)
            assert kind == "js_shell" or ok is False


class TestScoreLink:
    def test_focus_relevance(self):
        from sieve.crawl import score_link
        s1 = score_link("https://example.com/docs/api", "API docs", "api docs")
        s2 = score_link("https://example.com/login", "Login", "api docs")
        assert s1 > s2

    def test_junk_penalty(self):
        from sieve.crawl import score_link
        s = score_link("https://example.com/login", "Login page", "")
        assert s < 0


class TestCrawlNormalizeEdge:
    @pytest.mark.asyncio
    async def test_smart_crawl_invalid_url(self):
        from sieve.crawl import smart_crawl
        resp = await smart_crawl(MagicMock(), url="not-a-url", max_pages=2)
        assert resp.error != "" or len(resp.pages) == 0


# ─── browser.py pins ────────────────────────────────────────────────

class TestConstructProxyDict:
    def test_string_proxy(self):
        from sieve.browser import _construct_proxy_dict
        d = _construct_proxy_dict("http://user:pass@proxy.example.com:8080")
        assert d["server"] == "http://proxy.example.com:8080"
        assert d["username"] == "user"

    def test_dict_passthrough(self):
        from sieve.browser import _construct_proxy_dict
        assert _construct_proxy_dict({"server": "http://x:8080"}) == {"server": "http://x:8080"}

    def test_invalid_scheme(self):
        from sieve.browser import _construct_proxy_dict
        with pytest.raises(ValueError):
            _construct_proxy_dict("ftp://proxy.example.com:21")

    @pytest.mark.parametrize("proxy", [
        {}, {"server": None}, {"server": "http://proxy.test:80", "extra": "x"},
        {"server": "http://fixture:secret@proxy.example.com:80"},
        {"server": "http://proxy.test:80", "password": 42},
        {"server": "http://proxy.test:80", "bypass": "local\nfixture"},
    ])
    def test_malformed_proxy_dict_rejected_without_mutation(self, proxy):
        from sieve.browser import _construct_proxy_dict
        original = dict(proxy)
        with pytest.raises(ValueError):
            _construct_proxy_dict(proxy)
        assert proxy == original

    @pytest.mark.parametrize("server", [
        "ftp://proxy.test:21", "http://:80", "http://proxy.test",
        "http://proxy.test:bad", "http://proxy.test:0", "http://proxy.test:65536",
        "http://proxy.test:-1", "http://[broken:80", "http://proxy.test:80\n",
        "http://pro\txy.test:80", "http://proxy.test:80\x00", " http://proxy.test:80",
    ])
    @pytest.mark.parametrize("as_dict", [False, True])
    def test_proxy_url_validation_parity(self, server, as_dict):
        from sieve.browser import _construct_proxy_dict
        with pytest.raises(ValueError):
            _construct_proxy_dict({"server": server} if as_dict else server)

    @pytest.mark.parametrize("scheme", ["http", "https", "socks4", "socks5"])
    def test_ipv6_proxy_credentials_and_mapping_copy(self, scheme):
        from sieve.browser import _construct_proxy_dict
        server = f"{scheme}://[2001:db8::1]:8080"
        mapping = {"server": server, "username": "fixture", "password": "fake-password", "bypass": "localhost"}
        result = _construct_proxy_dict(mapping)
        assert result == mapping
        assert result is not mapping
        assert _construct_proxy_dict(f"{scheme}://fixture:fake-password@[2001:db8::1]:8080") == {
            "server": server, "username": "fixture", "password": "fake-password",
        }


class TestDetectCloudflare:
    def test_non_interactive(self):
        from sieve.browser import _detect_cloudflare
        assert _detect_cloudflare("cType: 'non-interactive'") == "non-interactive"

    def test_managed(self):
        from sieve.browser import _detect_cloudflare
        assert _detect_cloudflare("cType: 'managed'") == "managed"

    def test_embedded(self):
        from sieve.browser import _detect_cloudflare
        assert _detect_cloudflare("challenges.cloudflare.com/turnstile/v0") == "embedded"

    def test_none(self):
        from sieve.browser import _detect_cloudflare
        assert _detect_cloudflare("<html>normal page</html>") is None


class TestChromeChannel:
    def test_detect_chrome_channel_cached(self):
        from sieve.browser import _detect_chrome_channel
        import sieve.browser as b
        orig = b._chrome_channel_cache
        b._chrome_channel_cache = "chromium"
        try:
            assert _detect_chrome_channel() == "chromium"
        finally:
            b._chrome_channel_cache = orig


class TestOsFromUa:
    def test_windows(self):
        from sieve.browser import _os_from_ua
        assert _os_from_ua("Mozilla/5.0 (Windows NT 10.0)") == "windows"

    def test_mac(self):
        from sieve.browser import _os_from_ua
        assert _os_from_ua("Mozilla/5.0 (Macintosh; Intel Mac OS X)") == "mac"

    def test_linux(self):
        from sieve.browser import _os_from_ua
        assert _os_from_ua("Mozilla/5.0 (X11; Linux x86_64)") == "linux"

    def test_none(self):
        from sieve.browser import _os_from_ua
        assert _os_from_ua("") is None
        assert _os_from_ua(None) is None
