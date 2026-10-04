import pytest

from sieve.server import MasterFetchServer
from sieve.reddit_rss import RedditRSSRecord


@pytest.mark.asyncio
async def test_public_dispatch_exposes_source_neutral_ingest(monkeypatch):
    server = MasterFetchServer()
    record = RedditRSSRecord(
        title="T", url="https://www.reddit.com/r/test/comments/x/t/",
        source="reddit", source_id="x", kind="post", published_at="",
        author="fixture", summary="summary", content="body", subreddit="test",
        raw_payload="<entry>raw fixture</entry>", provenance={"feed_url": "rss"},
    )

    async def fetch(url, **kwargs):
        assert url == "https://www.reddit.com/r/test.rss"
        return [record]

    import sieve.research_sources as sources
    monkeypatch.setattr(sources, "fetch_reddit_rss", fetch)
    content, structured = await server._dispatch("research_ingest", {"source": "reddit_rss", "subreddit": "test"})

    assert structured["records"][0]["source_type"] == "reddit_rss"
    assert structured["records"][0]["raw_payload"] is None
    assert structured["raw_payload"] is None
    assert "raw fixture" not in content[0].text
    assert structured["records"][0]["provenance"] == {"feed_url": "rss"}
    assert "research_ingest" in {tool.name for tool in (await _list_tools(server))}


def test_mcp_ingest_schema_and_description_match_normalized_policy():
    definition = server_module_tool_def()
    assert "normalized records" in definition["description"]
    assert "only through the Python" in definition["description"]
    assert {"include_raw", "max_raw_chars"}.isdisjoint(definition["inputSchema"]["properties"])


async def _list_tools(server):
    from sieve.command_router import CAPABILITY_REGISTRY
    return [type("Tool", (), {"name": c.definition["name"]}) for c in CAPABILITY_REGISTRY.values() if c.definition is not None]


def server_module_tool_def():
    from sieve.command_router import CAPABILITY_REGISTRY
    return CAPABILITY_REGISTRY["research_ingest"].definition
