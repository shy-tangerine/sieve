import json

import httpx
import pytest

from sieve.integrations import (
    IntegrationError,
    invoke_integration,
    route_integration,
    scaffold_integration,
    validate_integration,
)


def spec(**overrides):
    value = {
        "name": "weather",
        "base_url": "https://api.example.test",
        "allowed_domains": ["api.example.test"],
        "auth": {"type": "bearer", "secret_ref": "env:WEATHER_TOKEN"},
        "workflows": {"lookup": {"method": "GET", "path": "/v1/weather", "params": {"city": "{city}"},
                                    "response": {"select": "data", "mapping": {"temp": "temperature"}}}},
    }
    value.update(overrides)
    return value


def test_validate_named_workflow_and_reject_code_like_templates():
    assert validate_integration(spec())["name"] == "weather"
    with pytest.raises(IntegrationError, match="whole"):
        validate_integration(spec(workflows={"x": {"path": "/", "params": {"q": "x{city}"}}}))
    with pytest.raises(IntegrationError, match="dotted"):
        validate_integration(spec(workflows={"x": {"path": "/", "response": {"mapping": {"out": "a[0]"}}}}))


def test_invoke_resolves_auth_without_returning_secret(monkeypatch):
    monkeypatch.setenv("WEATHER_TOKEN", "super-secret")
    monkeypatch.setattr("sieve.integrations.resolve_and_check", lambda *args: None)
    seen = {}

    def handler(request):
        seen["auth"] = request.headers["authorization"]
        return httpx.Response(200, json={"data": {"temperature": 21, "debug": "super-secret"}})

    result = invoke_integration(spec(), "lookup", {"city": "Lisbon"}, client=httpx.Client(transport=httpx.MockTransport(handler)))
    assert result["data"] == {"temp": 21}
    assert seen["auth"] == "Bearer super-secret"
    assert "super-secret" not in json.dumps(result)


def test_invoke_rejects_non_object_arguments(monkeypatch):
    monkeypatch.setenv("WEATHER_TOKEN", "test-token")
    monkeypatch.setattr("sieve.integrations.resolve_and_check", lambda *args: None)
    with pytest.raises(IntegrationError, match="arguments"):
        invoke_integration(spec(), "lookup", ["Lisbon"])


def test_rejects_http_and_redirect_outside_allowlist(monkeypatch):
    monkeypatch.setenv("WEATHER_TOKEN", "test-token")
    monkeypatch.setattr("sieve.integrations.resolve_and_check", lambda *args: None)
    with pytest.raises(IntegrationError, match="HTTPS"):
        validate_integration(spec(base_url="http://api.example.test"))

    def handler(request):
        return httpx.Response(302, headers={"location": "https://evil.example/steal"})

    with pytest.raises(IntegrationError, match="redirect"):
        invoke_integration(spec(), "lookup", client=httpx.Client(transport=httpx.MockTransport(handler)))


def test_rejects_oversized_and_malformed_response(monkeypatch):
    monkeypatch.setenv("WEATHER_TOKEN", "test-token")
    monkeypatch.setattr("sieve.integrations.resolve_and_check", lambda *args: None)
    def malformed(request):
        return httpx.Response(200, content=b"not-json")

    with pytest.raises(IntegrationError, match="valid JSON"):
        invoke_integration(spec(), "lookup", client=httpx.Client(transport=httpx.MockTransport(malformed)))

    def huge(request):
        return httpx.Response(200, content=b"{" + b"x" * 100 + b"}")

    with pytest.raises(IntegrationError, match="body"):
        invoke_integration(spec(), "lookup", client=httpx.Client(transport=httpx.MockTransport(huge)), max_body_bytes=10)


def test_streaming_read_stops_at_body_cap(monkeypatch):
    """Issue #16: an oversized response fails the body cap through the
    streaming path, and the bounded read never hands the caller the full 1 MiB
    body (only the capped prefix is materialized)."""
    monkeypatch.setenv("WEATHER_TOKEN", "test-token")
    monkeypatch.setattr("sieve.integrations.resolve_and_check", lambda *a: None)
    def huge(request):
        return httpx.Response(200, content=b"x" * (1024 * 1024))

    with pytest.raises(IntegrationError, match="body exceeds"):
        invoke_integration(spec(), "lookup",
                           client=httpx.Client(transport=httpx.MockTransport(huge)),
                           max_body_bytes=1024)


def test_bounded_response_keeps_json_contract(monkeypatch):
    """The capped response still validates as JSON when within the cap."""
    monkeypatch.setenv("WEATHER_TOKEN", "test-token")
    monkeypatch.setattr("sieve.integrations.resolve_and_check", lambda *a: None)
    def handler(request):
        return httpx.Response(200, json={"data": {"temperature": 21}})
    result = invoke_integration(spec(), "lookup",
                                client=httpx.Client(transport=httpx.MockTransport(handler)),
                                max_body_bytes=1024)
    assert result["data"] == {"temp": 21}


def test_timeout_is_a_normalized_failure(monkeypatch):
    monkeypatch.setenv("WEATHER_TOKEN", "test-token")
    monkeypatch.setattr("sieve.integrations.resolve_and_check", lambda *args: None)

    def timeout(request):
        raise httpx.ReadTimeout("provider timeout", request=request)

    result = invoke_integration(spec(), "lookup", client=httpx.Client(transport=httpx.MockTransport(timeout)))
    assert result["ok"] is False
    assert result["error"] == "request timed out"


def test_scaffold_writes_reviewable_json_without_secret_values(tmp_path):
    path = scaffold_integration(
        "books",
        "https://api.example.test",
        directory=tmp_path,
        workflow="search",
        path="/v1/books",
        secret_ref="env:BOOKS_API_KEY",
    )
    generated = json.loads(path.read_text())
    assert generated["name"] == "books"
    assert generated["auth"]["secret_ref"] == "env:BOOKS_API_KEY"
    assert "BOOKS_API_KEY" in path.read_text()
    assert "super-secret" not in path.read_text()
    assert validate_integration(generated)["workflows"]["search"]["path"] == "/v1/books"


def test_route_prefer_uses_api_then_scraper_on_failure(monkeypatch):
    monkeypatch.setenv("WEATHER_TOKEN", "test-token")
    monkeypatch.setattr("sieve.integrations.resolve_and_check", lambda *args: None)
    def unavailable(request):
        return httpx.Response(503, json={"error": "busy"})

    result = route_integration(
        spec(), "lookup", {"city": "Lisbon"},
        fallback=lambda: {"temperature": 19},
        client=httpx.Client(transport=httpx.MockTransport(unavailable)),
    )
    assert result["ok"] is True
    assert result["data"] == {"temperature": 19}
    assert result["provenance"]["adapter"] == "sieve.integration.fallback"
    assert result["provenance"]["api_attempted"] is True


def test_route_fallback_and_off_modes_are_explicit(monkeypatch):
    monkeypatch.setenv("WEATHER_TOKEN", "test-token")
    monkeypatch.setattr("sieve.integrations.resolve_and_check", lambda *args: None)
    seen = []
    def api(request):
        seen.append("api")
        return httpx.Response(200, json={"data": {"temperature": 21}})
    def scrape():
        seen.append("scrape")
        return {"temperature": 18}

    fallback_result = route_integration(
        spec(routing="fallback"), "lookup", {"city": "Lisbon"},
        fallback=scrape, client=httpx.Client(transport=httpx.MockTransport(api)),
    )
    assert fallback_result["data"] == {"temperature": 18}
    assert seen == ["scrape"]
    seen.clear()
    off_result = route_integration(
        spec(routing="off"), "lookup", {"city": "Lisbon"},
        fallback=scrape, client=httpx.Client(transport=httpx.MockTransport(api)),
    )
    assert off_result["data"] == {"temperature": 18}
    assert seen == ["scrape"]


def test_paginated_workflow_aggregates_items_and_records_sources(monkeypatch):
    monkeypatch.setenv("WEATHER_TOKEN", "test-token")
    monkeypatch.setattr("sieve.integrations.resolve_and_check", lambda *args: None)
    pages = []
    def handler(request):
        page = request.url.params.get("page", "1")
        pages.append(page)
        payload = {"items": [{"name": f"book-{page}"}], "next": "2" if page == "1" else None}
        return httpx.Response(200, json=payload)
    value = spec(workflows={"lookup": {
        "method": "GET", "path": "/v1/books", "params": {"page": "1"},
        "pagination": {"param": "page", "next": "next", "items": "items", "max_pages": 3},
        "response": {"mapping": {"title": "name"}},
    }})
    result = invoke_integration(value, "lookup", client=httpx.Client(transport=httpx.MockTransport(handler)))
    assert pages == ["1", "2"]
    assert result["data"] == [{"title": "book-1"}, {"title": "book-2"}]
    assert result["provenance"]["pagination"]["pages_fetched"] == 2
    assert len(result["provenance"]["sources"]) == 2


def test_invoke_calls_resolve_and_check_on_endpoint(monkeypatch):
    """T11: the SSRF pre-resolve hook must actually run on the endpoint.

    Every happy-path test stubs resolve_and_check to a no-op; nothing
    asserted it is CALLED. A refactor dropping the guard would pass CI.
    """
    monkeypatch.setenv("WEATHER_TOKEN", "test-token")
    calls = []
    monkeypatch.setattr(
        "sieve.integrations.resolve_and_check",
        lambda host, port=None: calls.append((host, port)),
    )

    invoke_integration(
        spec(), "lookup", {"city": "Lisbon"},
        client=httpx.Client(transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json={"data": {"temperature": 21}}),
        )),
    )

    assert calls, "resolve_and_check was never called during invoke"
    assert any("api.example.test" in str(host) for host, _ in calls), (
        f"endpoint host not pre-resolved; calls: {calls}"
    )
