import json
from io import BytesIO

import pytest

from sieve import ArcticShiftClient
from sieve import arctic_shift


class FakeResponse:
    def __init__(self, payload, status=200, headers=None):
        self.status = status
        self.headers = headers or {}
        self._body = json.dumps(payload).encode()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self, amount=-1):
        return self._body if amount < 0 else self._body[:amount]


def test_search_preserves_raw_payload_provenance_and_builds_cursor(monkeypatch):
    calls = []

    def opener(request, timeout):
        calls.append((request.full_url, timeout))
        return FakeResponse({"data": [{"id": "a", "created_utc": 10, "title": "hello"}]})

    monkeypatch.setattr(arctic_shift, "urlopen", opener)
    page = ArcticShiftClient(base_url="https://example.test", timeout=3, retry_count=0).search_posts(
        "hello", limit=1, sort="asc", subreddit="askreddit"
    )

    assert page.items[0].payload == {"id": "a", "created_utc": 10, "title": "hello"}
    assert page.items[0].source.raw_payload["data"][0]["id"] == "a"
    assert page.items[0].source.api_metadata["availability"] == "best_effort"
    assert page.next_params == {
        "limit": 1, "sort": "asc", "subreddit": "askreddit", "query": "hello", "after": 10
    }
    assert "query=hello" in calls[0][0]
    assert calls[0][1] == 3


def test_id_lookup_and_thread_use_documented_endpoints(monkeypatch):
    urls = []

    def opener(request, timeout):
        urls.append(request.full_url)
        if "/tree" in request.full_url:
            return FakeResponse({"data": [{"kind": "t1", "data": {"id": "c"}}]})
        return FakeResponse({"data": [{"id": "p"}]})

    monkeypatch.setattr(arctic_shift, "urlopen", opener)
    client = ArcticShiftClient(retry_count=0)
    assert client.get_post("t3_p").id == "p"
    assert client.get_comment("t1_c").id == "p"
    thread = client.get_thread("t3_p", limit=50)

    assert thread.raw_payload["data"][0]["kind"] == "t1"
    assert "/api/posts/ids?ids=t3_p" in urls[0]
    assert "/api/comments/ids?ids=t1_c" in urls[1]
    assert "/api/comments/tree?link_id=t3_p&limit=50" in urls[2]


def test_retries_transient_status_and_timeout(monkeypatch):
    attempts = []
    sleeps = []

    def opener(request, timeout):
        attempts.append(1)
        if len(attempts) == 1:
            return FakeResponse({"error": "busy"}, status=503)
        return FakeResponse({"data": []})

    monkeypatch.setattr(arctic_shift, "urlopen", opener)
    client = ArcticShiftClient(retry_count=1, retry_backoff=0.25, sleep=sleeps.append)
    page = client.search_comments("x")

    assert page.items == ()
    assert len(attempts) == 2
    assert sleeps == [0.25]


def test_validation_and_non_transient_error(monkeypatch):
    def opener(request, timeout):
        raise arctic_shift.HTTPError(request.full_url, 404, "missing", {}, BytesIO(b""))

    monkeypatch.setattr(arctic_shift, "urlopen", opener)
    with pytest.raises(arctic_shift.ArcticShiftHTTPError) as exc:
        ArcticShiftClient(retry_count=2, sleep=lambda _: None).get_posts(["x"])
    assert exc.value.status == 404
    with pytest.raises(ValueError):
        ArcticShiftClient().get_posts([])


@pytest.mark.parametrize("filters", [{"limit": "auto"}, {"format": "rss"}, {"format": "jsonfeed"}])
def test_upstream_options_not_silently_accepted(filters):
    with pytest.raises(ValueError, match="currently|yet"):
        ArcticShiftClient._search_params(None, filters)
