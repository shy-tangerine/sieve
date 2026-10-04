import json
from contextlib import contextmanager

import httpx

from sieve import cli
from sieve.image_search import ImageSearchResponse, search_images


def _brave_payload_stream(payload, status_code=200):
    """Build a fake httpx.stream context manager serving `payload` as JSON."""
    body = json.dumps(payload).encode("utf-8")

    @contextmanager
    def fake_stream(method, url, **kwargs):
        request = httpx.Request("GET", "https://api.search.brave.com/res/v1/images/search")
        response = httpx.Response(status_code, content=body, request=request)
        response.num_downloaded = lambda: len(body)
        yield response

    return fake_stream


def _install(monkeypatch, payload, status_code=200, seen=None):
    stream = _brave_payload_stream(payload, status_code)

    def fake_stream_wrapper(method, url, **kwargs):
        if seen is not None:
            seen.update(url=url, **kwargs)
        return stream(method, url, **kwargs)

    monkeypatch.setattr("sieve.image_search.httpx.stream", fake_stream_wrapper)


def test_no_provider_is_structured_and_does_not_make_request(monkeypatch):
    monkeypatch.setattr("sieve.image_search.load_byok_keys", lambda: {})
    called = []
    monkeypatch.setattr("sieve.image_search.httpx.stream", lambda *a, **k: called.append((a, k)))

    result = search_images("cats")

    assert result.to_dict() == {
        "ok": False,
        "query": "cats",
        "results": [],
        "provider": None,
        "safe_search": "strict",
        "error": "no Brave image-search key is configured",
        "category": "no_provider",
    }
    assert called == []


def test_brave_request_is_bounded_and_defaults_to_strict(monkeypatch):
    monkeypatch.setattr("sieve.image_search.load_byok_keys", lambda: {"brave": ["test-key"]})
    seen = {}
    _install(monkeypatch, {"results": [{
        "title": "A cat",
        "url": "https://images.example/cat.jpg",
        "source": "https://example.test/cats",
        "thumbnail": {"src": "https://images.example/cat-thumb.jpg"},
        "properties": {"url": "https://images.example/cat.jpg"},
    }]}, seen=seen)
    result = search_images("cats", max_results=999, timeout=999)

    assert result.ok is True
    assert result.provider == "brave"
    assert result.safe_search == "strict"
    assert len(result.results) == 1
    assert result.results[0].thumbnail == "https://images.example/cat-thumb.jpg"
    assert result.results[0].provenance == "https://example.test/cats"
    assert seen["params"] == {"q": "cats", "count": 50, "safesearch": "strict"}
    assert seen["headers"]["X-Subscription-Token"] == "test-key"
    assert seen["timeout"] == 60


def test_safe_search_modes_are_forwarded_without_affecting_text_search(monkeypatch):
    monkeypatch.setattr("sieve.image_search.load_byok_keys", lambda: {"brave": ["test-key"]})
    requests = []

    def capture(method, url, **kwargs):
        requests.append(kwargs)
        return _brave_payload_stream({"results": []})(method, url)

    monkeypatch.setattr("sieve.image_search.httpx.stream", capture)
    assert search_images("cats", safe_search="moderate").safe_search == "moderate"
    assert search_images("cats", safe_search="off").safe_search == "off"
    assert [request["params"]["safesearch"] for request in requests] == ["moderate", "off"]


def test_invalid_safe_search_is_rejected_before_provider_request(monkeypatch):
    monkeypatch.setattr("sieve.image_search.load_byok_keys", lambda: {"brave": ["test-key"]})
    called = []
    monkeypatch.setattr("sieve.image_search.httpx.stream", lambda *a, **k: called.append((a, k)))

    result = search_images("cats", safe_search="unsafe")

    assert not result.ok
    assert result.category == "input"
    assert called == []


def test_rate_limit_and_timeout_are_structured(monkeypatch):
    monkeypatch.setattr("sieve.image_search.load_byok_keys", lambda: {"brave": ["a", "b"]})
    _install(monkeypatch, {}, status_code=429)
    limited = search_images("cats")
    assert not limited.ok and limited.category == "rate_limited"

    def timeout(*args, **kwargs):
        raise httpx.ReadTimeout("slow")

    monkeypatch.setattr("sieve.image_search.httpx.stream", timeout)
    timed_out = search_images("cats")
    assert not timed_out.ok and timed_out.category == "timeout"


def test_oversized_body_fails_as_provider_error(monkeypatch):
    """A body over the cap fails before JSON parsing (#91)."""
    from sieve.image_search import MAX_RESPONSE_BYTES

    monkeypatch.setattr("sieve.image_search.load_byok_keys", lambda: {"brave": ["key"]})

    @contextmanager
    def huge_stream(method, url, **kwargs):
        request = httpx.Request("GET", "https://api.search.brave.com/res/v1/images/search")
        response = httpx.Response(200, content=b"x", request=request)
        chunks = iter([b"x" * (MAX_RESPONSE_BYTES + 1)])

        def aiter():
            return chunks

        response.iter_bytes = lambda *_a, **_k: chunks
        yield response

    monkeypatch.setattr("sieve.image_search.httpx.stream", huge_stream)
    result = search_images("cats")
    assert not result.ok and result.category == "provider_error"
    assert "exceeds" in result.error


def test_malformed_and_missing_result_urls_are_skipped(monkeypatch):
    monkeypatch.setattr("sieve.image_search.load_byok_keys", lambda: {"brave": ["key"]})
    payload = {"results": [
        {"title": "missing image", "source": "https://example.test"},
        {"title": "bad urls", "url": "javascript:bad", "source": "not-url"},
        {"title": "valid", "properties": {"url": "https://cdn.test/full.jpg"},
         "source": "https://example.test/page"},
    ]}
    _install(monkeypatch, payload)
    result = search_images("cats")
    assert [item.title for item in result.results] == ["valid"]
    assert result.results[0].thumbnail_url == result.results[0].image_url


def test_invalid_payload_and_json_are_structured(monkeypatch):
    monkeypatch.setattr("sieve.image_search.load_byok_keys", lambda: {"brave": ["key"]})
    _install(monkeypatch, [])
    assert search_images("cats").ok is True

    @contextmanager
    def bad_json_stream(method, url, **kwargs):
        request = httpx.Request("GET", "https://api.search.brave.com/res/v1/images/search")
        yield httpx.Response(200, content=b"not-json{{", request=request)

    monkeypatch.setattr("sieve.image_search.httpx.stream", bad_json_stream)
    invalid = search_images("cats")
    assert invalid.category == "provider_error"
    assert "invalid JSON" in invalid.error


def test_query_and_numeric_validation_happens_before_request(monkeypatch):
    monkeypatch.setattr("sieve.image_search.load_byok_keys", lambda: {"brave": ["key"]})
    called = []
    monkeypatch.setattr("sieve.image_search.httpx.stream", lambda *a, **k: called.append(1))
    assert search_images("").category == "input"
    assert search_images("cats", max_results="nan").category == "input"
    assert search_images("cats", timeout=float("inf")).category == "input"
    assert called == []


def test_auth_rotation_tries_next_key(monkeypatch):
    monkeypatch.setattr("sieve.image_search.load_byok_keys", lambda: {"brave": ["bad", "good"]})
    seen = []

    def rotating(method, url, **kwargs):
        seen.append(kwargs["headers"]["X-Subscription-Token"])
        return _brave_payload_stream({"results": []}, 401 if len(seen) == 1 else 200)(method, url)

    monkeypatch.setattr("sieve.image_search.httpx.stream", rotating)
    result = search_images("cats")
    assert result.ok and seen == ["bad", "good"]


def test_response_is_capped_even_when_provider_returns_many(monkeypatch):
    monkeypatch.setattr("sieve.image_search.load_byok_keys", lambda: {"brave": ["key"]})
    payload = {"results": [{"title": str(i), "url": f"https://img.test/{i}",
                             "source": "https://source.test"} for i in range(100)]}
    _install(monkeypatch, payload)
    result = search_images("cats", max_results=3)
    assert len(result.results) == 3


def test_cli_images_emits_json_and_failure_exit(monkeypatch, capsys):
    monkeypatch.setattr("sieve.image_search.load_byok_keys", lambda: {})
    assert cli._run_images_command(["images", "cats"]) == 1
    output = json.loads(capsys.readouterr().out)
    assert output["category"] == "no_provider"
    assert output["safe_search"] == "strict"


def test_cli_images_success_emits_json(monkeypatch, capsys):
    monkeypatch.setattr(cli, "_run_images_command", cli._run_images_command)
    monkeypatch.setattr("sieve.image_search.search_images", lambda *a, **k: ImageSearchResponse(
        ok=True, query="cats", provider="brave", results=[]))
    assert cli._run_images_command(["images", "cats", "--timeout", "4"]) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["ok"] is True and output["provider"] == "brave"


def test_non_image_commands_are_not_intercepted():
    assert cli._run_images_command(["search", "cats"]) is None
