import pytest

from sieve.reddit_rss import RSSFetchError, feed_url, fetch_reddit_rss, parse_reddit_rss


XML = b'''<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom">
<entry><title>First &amp; useful</title><id>t3_abc123</id>
<link rel="alternate" href="https://www.reddit.com/r/python/comments/abc123/first/"/>
<updated>2026-08-28T12:00:00+00:00</updated><author><name>/u/alice</name></author>
<category term="reddit.com/r/python"/><content type="html">&lt;p>Hello &lt;b>world&lt;/b>&lt;/p></content></entry>
<entry><title>Second</title><id>t3_def456</id><link href="https://www.reddit.com/r/python/comments/def456/second/"/>
<author><name>u/bob</name></author><summary type="html">A summary</summary></entry></feed>'''


def test_parse_subreddit_feed_normalizes_records_and_provenance():
    records = parse_reddit_rss(XML, feed_url="https://www.reddit.com/r/python/.rss", limit=1)
    assert len(records) == 1
    assert records[0].title == "First & useful"
    assert records[0].source_id == "t3_abc123"
    assert records[0].content == "Hello world"
    assert records[0].raw_payload == '''<entry><title>First &amp; useful</title><id>t3_abc123</id>
<link rel="alternate" href="https://www.reddit.com/r/python/comments/abc123/first/"/>
<updated>2026-08-28T12:00:00+00:00</updated><author><name>/u/alice</name></author>
<category term="reddit.com/r/python"/><content type="html">&lt;p>Hello &lt;b>world&lt;/b>&lt;/p></content></entry>'''


def test_feed_url_supports_search_and_thread():
    assert feed_url("python", kind="search", query="async io") == "https://www.reddit.com/r/python/search.rss?q=async+io"
    assert feed_url("https://www.reddit.com/comments/abc123/title", kind="thread") == "https://www.reddit.com/comments/abc123.rss"


def test_parse_rss_item_shape_as_well_as_atom():
    xml = """<rss><channel><item><title>Legacy item</title>
    <guid>t3_legacy</guid><link>https://www.reddit.com/r/python/comments/legacy/item/</link>
    <pubDate>Fri, 28 Aug 2026 12:00:00 GMT</pubDate><description>&lt;p>Body&lt;/p></description>
    </item></channel></rss>"""
    record = parse_reddit_rss(xml, feed_url="https://www.reddit.com/r/python/.rss")[0]
    assert record.source_id == "t3_legacy"
    assert record.content == "Body"
    assert record.raw_payload == '''<item><title>Legacy item</title>
    <guid>t3_legacy</guid><link>https://www.reddit.com/r/python/comments/legacy/item/</link>
    <pubDate>Fri, 28 Aug 2026 12:00:00 GMT</pubDate><description>&lt;p>Body&lt;/p></description>
    </item>'''


def test_parse_thread_comments_and_malformed_xml():
    xml = XML.replace(b"t3_abc123", b"t1_comment").replace(b"First &amp; useful", b"A comment")
    records = parse_reddit_rss(xml, feed_url="https://www.reddit.com/comments/abc123.rss")
    assert records[0].kind == "comment"
    with pytest.raises(RSSFetchError):
        parse_reddit_rss("<feed>", feed_url="https://www.reddit.com/r/python/.rss")


class FakeResponse:
    def __init__(self, status_code, content=b""):
        self.status_code = status_code
        self.content = content


class FakeSession:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.calls = []

    async def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return next(self.responses)


@pytest.mark.asyncio
async def test_fetch_retries_transient_failures_and_applies_limit():
    session = FakeSession([FakeResponse(503), FakeResponse(200, XML)])
    delays = []

    async def record_sleep(delay):
        delays.append(delay)

    records = await fetch_reddit_rss("https://www.reddit.com/r/python/.rss", session=session,
                                     limit=1, retries=1, retry_delay=0.5, sleep=record_sleep)
    assert len(records) == 1
    assert len(session.calls) == 2
    assert delays == [0.5]
    assert session.calls[0][1]["timeout"] == 10.0


@pytest.mark.asyncio
async def test_fetch_does_not_retry_permanent_http_errors():
    session = FakeSession([FakeResponse(404)])
    with pytest.raises(RSSFetchError):
        await fetch_reddit_rss("https://www.reddit.com/r/python/.rss", session=session, retries=3)
    assert len(session.calls) == 1
