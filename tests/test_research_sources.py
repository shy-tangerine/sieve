import pytest

from sieve import arctic_shift, reddit_rss
from sieve.research_sources import ingest_source


RSS = b'''<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom">
<entry><title>Post</title><id>t3_post</id><link href="https://www.reddit.com/r/x/comments/post/a"/>
<author><name>u/alice</name></author><content>body</content></entry>
<entry><title>Comment</title><id>t1_comment</id><link href="https://www.reddit.com/r/x/comments/post/a/comment/"/>
<author><name>u/bob</name></author><content>reply</content></entry></feed>'''


class FakeRSSResponse:
    status_code = 200
    content = RSS


class FakeRSSSession:
    async def get(self, url, **kwargs):
        assert url.endswith("/comments/post.rss")
        assert kwargs["timeout"] == 3
        return FakeRSSResponse()


@pytest.mark.asyncio
async def test_reddit_rss_delegates_thread_parser_and_keeps_comments():
    result = await ingest_source(
        "reddit_rss", target="https://www.reddit.com/comments/post/title",
        kind="thread", timeout=3, rss_session=FakeRSSSession(),
        include_raw=True,
    )
    assert [record.id for record in result.records] == ["t3_post", "t1_comment"]
    assert result.records[1].source_type == "reddit_rss"
    assert result.records[0].raw_payload == '''<entry><title>Post</title><id>t3_post</id><link href="https://www.reddit.com/r/x/comments/post/a"/>
<author><name>u/alice</name></author><content>body</content></entry>'''
    assert result.records[1].raw_payload == '''<entry><title>Comment</title><id>t1_comment</id><link href="https://www.reddit.com/r/x/comments/post/a/comment/"/>
<author><name>u/bob</name></author><content>reply</content></entry>'''
    assert result.raw_payload == [result.records[0].raw_payload, result.records[1].raw_payload]
    assert result.records[1].provenance["feed_type"] == "thread"


class FakeArcticClient:
    def search_comments(self, query, **filters):
        source = arctic_shift.ArcticShiftResponse(
            endpoint="/api/comments/search", request_url="https://example/comments/search",
            request_params=filters, raw_payload={"data": [{"id": "c1", "body": "reply", "author": "bob"}]},
            status_code=200, headers={}, fetched_at="2030-01-01T00:00:00+00:00",
        )
        item = arctic_shift.ArcticShiftItem("comments", source.raw_payload["data"][0], source)
        return arctic_shift.ArcticShiftPage("comments", (item,), source, None)


@pytest.mark.asyncio
async def test_arctic_comments_use_adapter_and_async_transport_seam():
    calls = []

    async def transport(operation):
        calls.append("called")
        return operation()

    result = await ingest_source(
        "arctic_shift", kind="comments", query="topic", arctic_client=FakeArcticClient(),
        arctic_transport=transport, include_raw=True,
    )
    assert calls == ["called"]
    assert result.records[0].id == "c1"
    assert result.records[0].body == "reply"
    assert result.records[0].raw_payload["id"] == "c1"
    assert result.provenance["adapter"] == "sieve.arctic_shift"


@pytest.mark.asyncio
async def test_arctic_thread_flattens_comment_replies_without_blocking_client():
    raw = {"data": [{"kind": "t1", "data": {"id": "c1", "body": "one", "replies": {"data": [{"kind": "t1", "data": {"id": "c2", "body": "two"}}]}}}]}
    response = arctic_shift.ArcticShiftResponse("/api/comments/tree", "https://example/tree", {"link_id": "p"}, raw, 200, {}, "2030-01-01T00:00:00+00:00")

    class Client:
        def get_thread(self, post_id, **options):
            return response

    result = await ingest_source("arctic_shift", kind="thread", post_id="p", arctic_client=Client())
    assert [record.id for record in result.records] == ["c1", "c2"]


@pytest.mark.asyncio
async def test_raw_payload_opt_in_and_caps():
    """#183/#190: raw payloads are opt-in and capped with truncation metadata."""
    result = await ingest_source(
        "reddit_rss", target="https://www.reddit.com/comments/post/title",
        kind="thread", timeout=3, rss_session=FakeRSSSession(),
    )
    # Default: normalized records only, no raw duplication.
    assert all(record.raw_payload is None for record in result.records)
    assert result.raw_payload is None
    assert result.records[0].title == "Post"

    capped = await ingest_source(
        "reddit_rss", target="https://www.reddit.com/comments/post/title",
        kind="thread", timeout=3, rss_session=FakeRSSSession(),
        include_raw=True, max_raw_chars=10,
    )
    raw = capped.records[0].raw_payload
    assert raw.startswith("<entry><ti")
    assert raw.endswith("[truncated: raw payload budget exceeded]")
