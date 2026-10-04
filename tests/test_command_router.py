import json
import sys
from types import SimpleNamespace

import pytest

from sieve.command_router import CANONICAL_CAPABILITIES, CapabilityRouter, _primp_replay_fetch


class DummyServer:
    async def version(self):
        from sieve.server import VersionInfoModel
        return VersionInfoModel(version="test")

    async def smart_crawl(self, url, **kwargs):
        from sieve.crawl import CrawlResponseModel
        assert url == "https://example.com"
        assert kwargs == {"max_pages": 2, "respect_robots": True}
        return CrawlResponseModel(start_url=url, pages=[])


@pytest.mark.asyncio
async def test_router_rejects_prefixed_and_unknown_capabilities():
    router = CapabilityRouter(DummyServer())

    with pytest.raises(ValueError, match="canonical bare"):
        await router.dispatch("mcp_version", {})
    with pytest.raises(ValueError, match="Unknown tool"):
        await router.dispatch("retired_alias", {})


@pytest.mark.asyncio
async def test_router_preserves_structured_result_envelope():
    router = CapabilityRouter(DummyServer())

    content, structured = await router.dispatch("version", {})

    assert "version" in CANONICAL_CAPABILITIES
    assert structured["version"] == "test"
    assert json.loads(content[0].text)["version"] == "test"


@pytest.mark.asyncio
async def test_router_smart_crawl_rejects_sensitive_and_unknown_inputs_without_values():
    router = CapabilityRouter(DummyServer())
    for args in (
        {"url": "https://example.com", "cookies": "secret-cookie"},
        {"url": "https://example.com", "options": {"proxy": "http://secret"}},
        {"url": "https://example.com", "options": {"extra_headers": {"Authorization": "secret"}}},
        {"url": "https://example.com", "options": {"useragent": "secret-agent"}},
        {"url": "https://example.com", "options": {"arbitrary": "secret-value"}},
    ):
        with pytest.raises(ValueError) as error:
            await router.dispatch("smart_crawl", args)
        assert "secret" not in str(error.value)


@pytest.mark.asyncio
async def test_router_smart_crawl_routes_declared_top_level_and_nested_inputs():
    router = CapabilityRouter(DummyServer())
    content, structured = await router.dispatch(
        "smart_crawl",
        {"url": "https://example.com", "options": {"max_pages": 2, "respect_robots": True}},
    )
    assert structured["start_url"] == "https://example.com"
    assert json.loads(content[0].text)["start_url"] == "https://example.com"


def test_primp_replay_error_is_sanitized(monkeypatch):
    sentinel = "SENTINEL_SECRET /private/path token=private"

    class Client:
        def __init__(self, **kwargs):
            pass

        def get(self, url):
            raise RuntimeError(sentinel)

    monkeypatch.setitem(sys.modules, "primp", SimpleNamespace(Client=Client))
    result = _primp_replay_fetch("https://example.com")

    assert result["ok"] is False
    assert result["status"] is None
    assert result["text"] == ""
    assert result["category"] == "network"
    assert "SENTINEL_SECRET" not in result["error"]
    assert "/private/path" not in result["error"]
    assert "private" not in result["error"]
