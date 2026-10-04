"""Pins for browser retry + crawl pagination/classify edges. Deterministic, no network."""

import pytest
from unittest.mock import AsyncMock


class TestGetPageContent:
    @pytest.mark.asyncio
    async def test_returns_on_first_success(self):
        from sieve.browser import _get_page_content
        page = AsyncMock()
        page.content.return_value = "<html>hi</html>"
        assert await _get_page_content(page) == "<html>hi</html>"
        assert page.content.await_count == 1

    @pytest.mark.asyncio
    async def test_retries_then_returns(self):
        from sieve.browser import _get_page_content
        page = AsyncMock()
        page.content.side_effect = [Exception("boom"), Exception("boom"), "<html>ok</html>"]
        assert await _get_page_content(page, max_retries=5) == "<html>ok</html>"
        assert page.content.await_count == 3

    @pytest.mark.asyncio
    async def test_empty_after_max_retries(self):
        from sieve.browser import _get_page_content
        page = AsyncMock()
        page.content.side_effect = Exception("dead")
        assert await _get_page_content(page, max_retries=3) == ""
        assert page.content.await_count == 3


class TestPageSignals:
    def test_empty_html(self):
        from sieve.crawl import _page_signals
        assert _page_signals("") == (0, 0, 0, False, 0)

    def test_framework_root_detected(self):
        from sieve.crawl import _page_signals
        _, _, _, has_fw, _ = _page_signals('<html><body><div id="root"></div></body></html>')
        assert has_fw is True

    def test_plain_article_no_framework(self):
        from sieve.crawl import _page_signals
        text_len, scripts, links, has_fw, _ = _page_signals(
            "<html><body><p>Hello world</p><a href='/x'>click</a></body></html>")
        assert has_fw is False and links == 1 and text_len > 0


class TestNormalizePagination:
    def test_page_param_survives(self):
        from sieve.crawl import normalize_url
        assert "page=2" in normalize_url("https://example.com/list?page=2")

    def test_fragment_stripped(self):
        from sieve.crawl import normalize_url
        assert "#" not in normalize_url("https://example.com/a#section")
