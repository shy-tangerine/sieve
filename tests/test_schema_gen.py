import pytest

from sieve.schema_gen import _build_prompt


def test_schema_prompt_advertises_only_supported_extraction_types():
    prompt = _build_prompt("<div class='item'>x</div>", ["name"])

    assert 'text|attribute|html|regex|nested"' in prompt
    assert "|links|image" not in prompt


@pytest.mark.parametrize("endpoint", [
    "file:///tmp/api", "ftp://example.org", "https:///missing-host",
    "https://user:password@example.org", "https://@example.org/v1",
    "http://example.org/v1", "http://localhost.evil/v1",
    "https://example.org/v1?key=private", "https://example.org:0/v1",
    "https://example.org/\r\nv1",
])
def test_schema_endpoint_rejected_before_transport(endpoint, monkeypatch):
    from sieve import schema_gen

    def unexpected_transport(*args, **kwargs):
        pytest.fail("invalid endpoint reached urllib")

    monkeypatch.setattr(schema_gen.urllib.request, "urlopen", unexpected_transport)
    monkeypatch.setattr(schema_gen.urllib.request, "build_opener", unexpected_transport)
    with pytest.raises(ValueError, match="base_url"):
        schema_gen.generate_schema("<article>fixture</article>", ["title"], api_key="fake-key", base_url=endpoint)


def test_schema_from_url_requires_fetch_before_generation(monkeypatch):
    from sieve import schema_gen

    def unexpected_generation(*args, **kwargs):
        pytest.fail("invalid fetch hook reached schema generation")

    monkeypatch.setattr(schema_gen, "generate_schema", unexpected_generation)
    with pytest.raises(TypeError, match="fetch"):
        schema_gen.generate_schema_from_url("https://example.com", ["title"])
    for fetch in (None, "https://example.com", 42):
        with pytest.raises(TypeError, match="fetch"):
            schema_gen.generate_schema_from_url("https://example.com", ["title"], fetch=fetch)


def test_schema_from_url_uses_injected_fetch_and_forwards_options(monkeypatch):
    from sieve import schema_gen

    calls = []
    html = "<article><h1>Sample</h1></article>"
    schema = {"baseSelector": "article", "fields": [{"name": "title", "selector": "h1", "type": "text"}]}

    def fetch(url):
        calls.append(("fetch", url))
        return html

    def generate(sample, fields, **options):
        calls.append(("generate", sample, fields, options))
        return schema

    monkeypatch.setattr(schema_gen, "generate_schema", generate)
    result = schema_gen.generate_schema_from_url(
        "https://example.com", ["title"], fetch=fetch, timeout=5, model="fixture", api_key="fake-key"
    )
    assert result is schema
    assert calls == [
        ("fetch", "https://example.com"),
        ("generate", html, ["title"], {"timeout": 5, "model": "fixture", "api_key": "fake-key"}),
    ]


def test_schema_from_url_rejects_non_text_sample(monkeypatch):
    from sieve import schema_gen

    def unexpected_generation(*args, **kwargs):
        pytest.fail("non-text sample reached schema generation")

    monkeypatch.setattr(schema_gen, "generate_schema", unexpected_generation)
    with pytest.raises(ValueError, match="expected str"):
        schema_gen.generate_schema_from_url("https://example.com", ["title"], fetch=lambda url: b"HTML")


def test_schema_from_url_propagates_fetch_failure(monkeypatch):
    from sieve import schema_gen

    failure = ValueError("redirect rejected")

    def fetch(url):
        raise failure

    def unexpected_generation(*args, **kwargs):
        pytest.fail("rejected fetch reached schema generation")

    monkeypatch.setattr(schema_gen, "generate_schema", unexpected_generation)
    with pytest.raises(ValueError) as caught:
        schema_gen.generate_schema_from_url("https://example.com", ["title"], fetch=fetch)
    assert caught.value is failure


@pytest.mark.parametrize("explicit,canonical,expected", [
    ("explicit-fixture", "canonical-fixture", "explicit-fixture"),
    (None, "canonical-fixture", "canonical-fixture"),
    (None, None, "file-fixture"),
])
def test_schema_generation_uses_canonical_llm_key(monkeypatch, explicit, canonical, expected):
    import io
    import json
    from types import SimpleNamespace

    from sieve import byok_config, schema_gen

    if canonical is None:
        monkeypatch.delenv("SIEVE_LLM_KEYS", raising=False)
    else:
        monkeypatch.setenv("SIEVE_LLM_KEYS", canonical)
    monkeypatch.setattr(byok_config, "_read_config_file", lambda: {"llm": ["file-fixture"]})
    schema = {"baseSelector": "article", "fields": [{"name": "title", "selector": "h1", "type": "text"}]}

    def transport(request, **kwargs):
        assert request.get_header("Authorization") == f"Bearer {expected}"
        return io.BytesIO(json.dumps({"choices": [{"message": {"content": json.dumps(schema)}}]}).encode())

    monkeypatch.setattr(schema_gen.urllib.request, "build_opener", lambda *a: SimpleNamespace(open=transport))
    assert schema_gen.generate_schema("<article>fixture</article>", ["title"], api_key=explicit) == schema


def test_schema_generation_missing_key_fails_before_transport(monkeypatch):
    from sieve import byok_config, schema_gen

    for name in ("SIEVE_LLM_KEYS", "SIEVE_LLM_API_KEY", "FREELMAPI_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(byok_config, "_read_config_file", lambda: {})

    def unexpected_transport(*args, **kwargs):
        pytest.fail("missing credential reached LLM transport")

    monkeypatch.setattr(schema_gen.urllib.request, "urlopen", unexpected_transport)
    with pytest.raises(ValueError, match="api_key is required"):
        schema_gen.generate_schema("<article>fixture</article>", ["title"])


@pytest.mark.parametrize("endpoint", ["https://api.example/v1", "http://localhost:3001/v1", "http://127.0.0.1/v1", "http://[::1]/v1"])
def test_schema_endpoint_sample_is_bounded_and_explicit(monkeypatch, endpoint):
    import io
    import json
    from types import SimpleNamespace
    from sieve import schema_gen

    seen = []
    schema = {"baseSelector": "article", "fields": [{"name": "title", "selector": "h1", "type": "text"}]}
    def transport(request, **kwargs):
        seen.append(request)
        payload = json.loads(request.data)
        prompt = payload["messages"][1]["content"]
        assert "x" * 12_000 in prompt
        assert "x" * 12_001 not in prompt
        return io.BytesIO(json.dumps({"choices": [{"message": {"content": json.dumps(schema)}}]}).encode())
    monkeypatch.setattr(schema_gen.urllib.request, "build_opener", lambda *a: SimpleNamespace(open=transport))
    assert schema_gen.generate_schema("x" * 100_000, ["title"], api_key="fixture", base_url=endpoint) == schema
    assert len(seen) == 1
    assert seen[0].full_url == endpoint + "/chat/completions"


def test_schema_redirect_cannot_forward_sample_or_key(monkeypatch):
    import io
    from email.message import Message
    from urllib.response import addinfourl
    from sieve import schema_gen

    seen = []
    class Transport(schema_gen.urllib.request.HTTPHandler):
        def http_open(self, request):
            seen.append(request)
            headers = Message()
            headers["Location"] = "http://127.0.0.2/different-endpoint"
            response = addinfourl(io.BytesIO(b"redirect"), headers, request.full_url, 302)
            response.msg = "Found"
            return response
    build = schema_gen.urllib.request.build_opener
    monkeypatch.setattr(schema_gen.urllib.request, "build_opener", lambda *handlers: build(*handlers, Transport()))
    with pytest.raises(ValueError, match="LLM endpoint failed"):
        schema_gen.generate_schema("private-page-sample", ["title"], api_key="fixture", base_url="http://127.0.0.1/v1")
    assert len(seen) == 1
    assert seen[0].full_url == "http://127.0.0.1/v1/chat/completions"
