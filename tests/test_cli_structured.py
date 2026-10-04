import json
import sys
import pytest

from sieve import cli


class _StructuredResponse:
    def model_dump(self):
        return {
            "url": "https://example.com/product",
            "status": 200,
            "content": ["{\"title\": \"Widget\"}"],
            "metadata": {
                "structured_data": [{
                    "@type": "Product",
                    "name": "Widget",
                    "offers": {"price": "12.00", "availability": "InStock"},
                }],
            },
        }


def test_structured_fetch_routes_to_existing_extraction(monkeypatch, capsys):
    seen = {}

    class FakeServer:
        def __init__(self, *, cache_ttl):
            seen["server_cache_ttl"] = cache_ttl

        async def smart_fetch(self, url, **kwargs):
            seen["url"] = url
            seen.update(kwargs)
            return _StructuredResponse()

    monkeypatch.setattr("sieve.server.MasterFetchServer", FakeServer)
    monkeypatch.setattr(sys, "argv", [
        "sieve", "fetch", "--structured", "https://example.com/product",
        "--timeout", "7", "--cache-ttl", "120",
    ])

    assert cli.main() == 0
    output = json.loads(capsys.readouterr().out)
    assert output["metadata"]["structured_data"][0]["@type"] == "Product"
    assert output["metadata"]["structured_data"][0]["offers"]["price"] == "12.00"
    assert seen == {
        "server_cache_ttl": 120,
        "url": "https://example.com/product",
        "extraction_type": "structured",
        "cache_ttl": 120,
        "timeout": 7000,
    }


def test_structured_fetch_is_opt_in():
    assert cli._run_structured_fetch(["fetch", "https://example.com"]) is None


def test_structured_cli_redacts_credentials_and_bounds_output(monkeypatch, capsys):
    from sieve.public_output import MAX_PUBLIC_OUTPUT_BYTES
    payload = {"url": "https://user:sentinel@example.org", "status": 200,
               "metadata": {"future_password": "sentinel"}, "blob": "x" * 5_000_000}

    class FakeServer:
        def __init__(self, **kwargs): pass
        async def smart_fetch(self, *args, **kwargs): return payload

    monkeypatch.setattr("sieve.server.MasterFetchServer", FakeServer)
    assert cli._run_structured_fetch(["fetch", "--structured", "https://example.org"]) == 0
    text = capsys.readouterr().out.strip()
    assert len(text.encode("utf-8")) <= MAX_PUBLIC_OUTPUT_BYTES
    assert "sentinel" not in text
    assert json.loads(text)["status"] == 200
    assert payload["metadata"]["future_password"] == "sentinel"


def test_structured_fetch_passes_multiple_urls_to_bulk_contract(monkeypatch, capsys):
    seen = {}

    class FakeServer:
        def __init__(self, *, cache_ttl):
            pass

        async def smart_fetch(self, url, **kwargs):
            seen["url"] = url
            seen.update(kwargs)
            return {"results": []}

    monkeypatch.setattr("sieve.server.MasterFetchServer", FakeServer)

    assert cli._run_structured_fetch([
        "fetch",
        "--structured",
        "https://example.com/one",
        "https://example.com/two",
    ]) == 0
    assert json.loads(capsys.readouterr().out) == {"results": []}
    assert seen == {
        "url": "https://example.com/one",
        "urls": ["https://example.com/one", "https://example.com/two"],
        "extraction_type": "structured",
        "cache_ttl": 3600,
        "timeout": 30000,
    }


def test_structured_fetch_failure_is_json_on_stdout(monkeypatch, capsys):
    class FakeServer:
        def __init__(self, *, cache_ttl):
            pass

        async def smart_fetch(self, url, **kwargs):
            raise ValueError("fetch failed")

    monkeypatch.setattr("sieve.server.MasterFetchServer", FakeServer)
    assert cli._run_structured_fetch(["fetch", "https://example.com", "--structured"]) == 1
    output = capsys.readouterr()
    assert output.err == ""
    payload = json.loads(output.out)
    assert {key: payload[key] for key in ("ok", "error", "category")} == {
        "ok": False,
        "error": "Invalid input.",
        "category": "input",
    }
    assert payload["diagnostic"]["category"] == "input"
    assert payload["diagnostic"]["retryable"] is False


@pytest.mark.parametrize("exception,category", [(RuntimeError, "internal"), (TimeoutError, "timeout"), (ValueError, "input")])
def test_cli_exception_values_never_reach_public_json(monkeypatch, capsys, exception, category):
    sentinel = "private=/private/session.json token=sk-live-secret-1234567890 ?key=private-query"

    class FakeServer:
        def __init__(self, **kwargs): pass
        async def smart_fetch(self, *args, **kwargs):
            raise exception(sentinel)

    monkeypatch.setattr("sieve.server.MasterFetchServer", FakeServer)
    assert cli._run_structured_fetch(["fetch", "--structured", "https://example.test"]) == 1
    output = capsys.readouterr()
    assert output.err == ""
    payload = json.loads(output.out)
    assert payload["ok"] is False and payload["category"] == category
    assert all(value not in output.out for value in ("alice", "sk-live-secret", "private-query"))


def test_cli_help_documents_structured_fetch(capsys):
    cli._print_cli_help()
    assert "sieve fetch [--structured] URL [...]" in capsys.readouterr().out


def test_structured_fetch_help_exposes_cache_ttl(capsys):
    try:
        cli._run_structured_fetch(["fetch", "--structured", "--help"])
    except SystemExit as exc:
        assert exc.code == 0
    assert "--cache-ttl" in capsys.readouterr().out
