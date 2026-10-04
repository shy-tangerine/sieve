"""CLI / MCP / HTTP surface contract tests.

Proves that equivalent operations produce equivalent JSON envelopes and
failure behavior across the three public surfaces.  The tests mock the
underlying server methods so they exercise the surface translation layer
without network calls.

Surface coverage rationale:
- CLI: exercises ``sieve.cli.main`` -> ``MasterFetchServer`` method calls
  -> JSON stdout serialization.
- MCP stdio: exercises ``CapabilityRouter.dispatch`` -> ``MasterFetchServer``
  method calls -> ``(content_list, structured_dict)`` return.
- MCP HTTP: shares the same ``CapabilityRouter.dispatch`` handler as stdio;
  the only difference is the transport wire (streamable HTTP vs stdin/stdout).
  Testing stdio therefore proves the same envelope contract for HTTP.
"""
from __future__ import annotations

import json
import sys
from unittest.mock import AsyncMock

import pytest

from sieve.server import ResponseModel, VersionInfoModel
from sieve.search import SearchResponseModel, SearchResult
from sieve.command_router import CapabilityRouter, CANONICAL_CAPABILITIES


# ── helpers ──────────────────────────────────────────────────────────

def _dummy_response(**overrides) -> ResponseModel:
    defaults = dict(
        url="https://example.com",
        status=200,
        content=["Hello world"],
        title="Example",
        content_type="text/html",
        fetcher_used="http",
        response_time_ms=12,
    )
    defaults.update(overrides)
    return ResponseModel(**defaults)


def _dummy_search_response(**overrides) -> SearchResponseModel:
    defaults = dict(
        query="test",
        results=[SearchResult(title="R1", url="https://a.com", snippet="s", tier="high")],
        total_results=1,
        duration_ms=50,
        engines_used=["google"],
        rerank_mode="auto",
    )
    defaults.update(overrides)
    return SearchResponseModel(**defaults)


# ── version envelope ─────────────────────────────────────────────────

class TestVersionEnvelope:
    """CLI, MCP, and SDK all expose the version; the envelope shape must match."""

    @pytest.mark.asyncio
    async def test_mcp_version_envelope(self):
        """MCP dispatch returns (content_list, structured_dict)."""
        class FakeServer:
            async def version(self):
                return VersionInfoModel(version="test", update_command="")
        router = CapabilityRouter(FakeServer())
        content, structured = await router.dispatch("version", {})
        assert structured["version"] == "test"
        # Content is a list of TextContent-like dicts
        assert isinstance(content, list)
        assert len(content) == 1
        parsed = json.loads(content[0].text)
        assert parsed["version"] == "test"

    def test_cli_version_emits_json(self, monkeypatch, capsys):
        """CLI --version prints 'Sieve X.Y.Z' (not JSON), but the server
        version method must be callable and return the same model."""
        from sieve import __version__
        monkeypatch.setattr(sys, "argv", ["sieve", "--version"])
        from sieve import cli
        cli.main()
        out = capsys.readouterr().out.strip()
        assert out == f"Sieve {__version__}"


# ── smart_fetch envelope ─────────────────────────────────────────────

class TestSmartFetchEnvelope:
    """Both CLI and MCP smart_fetch must produce a ResponseModel-compatible
    JSON envelope with the same required top-level keys."""

    @pytest.mark.asyncio
    async def test_mcp_smart_fetch_returns_response_model_dict(self):
        """MCP smart_fetch structured result must serialize to ResponseModel fields."""
        resp = _dummy_response()

        class FakeServer:
            async def smart_fetch(self, url, **kw):
                return resp

        router = CapabilityRouter(FakeServer())
        content, structured = await router.dispatch("smart_fetch", {"url": "https://example.com"})
        # Structured result is the Pydantic model dict
        assert structured["url"] == "https://example.com"
        assert structured["status"] == 200
        assert structured["fetcher_used"] == "http"
        # Content is JSON text
        parsed = json.loads(content[0].text)
        assert parsed["status"] == 200

    def test_cli_fetch_json_has_response_model_keys(self, monkeypatch, capsys):
        """CLI `sieve fetch` emits ResponseModel as JSON on stdout."""
        resp = _dummy_response()

        class FakeServer:
            def __init__(self, **kw):
                pass
            async def smart_fetch(self, url, **kw):
                return resp

        monkeypatch.setattr("sieve.server.MasterFetchServer", FakeServer)
        monkeypatch.setattr(sys, "argv", ["sieve", "fetch", "https://example.com"])
        from sieve import cli
        cli.main()
        out = json.loads(capsys.readouterr().out)
        # Must contain all ResponseModel top-level keys
        for key in ("url", "status", "content", "fetcher_used", "duration_ms"):
            assert key in out, f"missing key: {key}"
        assert out["status"] == 200

    def test_cli_search_json_has_search_response_model_keys(self, monkeypatch, capsys):
        """CLI `sieve search` emits SearchResponseModel as JSON."""
        sr = _dummy_search_response()

        class FakeServer:
            def __init__(self, **kw):
                pass
            async def smart_search(self, query, **kw):
                return sr

        monkeypatch.setattr("sieve.server.MasterFetchServer", FakeServer)
        monkeypatch.setattr(sys, "argv", ["sieve", "search", "test query"])
        from sieve import cli
        cli.main()
        out = json.loads(capsys.readouterr().out)
        for key in ("query", "results", "total_results", "duration_ms"):
            assert key in out, f"missing key: {key}"
        assert out["query"] == "test"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("key", ["cookies", "proxy", "extra_headers", "useragent"])
    async def test_mcp_smart_fetch_rejects_undeclared_sensitive_inputs(self, key):
        router = CapabilityRouter(type("S", (), {})())
        with pytest.raises(ValueError, match=rf"Unknown arguments for smart_fetch: {key}"):
            await router.dispatch("smart_fetch", {"url": "https://example.com", key: "secret"})

    @pytest.mark.asyncio
    async def test_mcp_smart_fetch_rejects_undeclared_option(self):
        router = CapabilityRouter(type("S", (), {})())
        with pytest.raises(ValueError, match="Unknown options for smart_fetch: proxy"):
            await router.dispatch("smart_fetch", {"url": "https://example.com", "options": {"proxy": "secret"}})

    @pytest.mark.asyncio
    @pytest.mark.parametrize("key", ["cookies", "proxy", "extra_headers", "useragent"])
    async def test_mcp_extract_rejects_undeclared_sensitive_inputs(self, key):
        router = CapabilityRouter(type("S", (), {})())
        with pytest.raises(ValueError, match=rf"Unknown arguments for extract: {key}"):
            await router.dispatch("extract", {"url": "https://example.com", "schema": {"fields": []}, key: "secret"})

    @pytest.mark.asyncio
    async def test_mcp_extract_rejects_undeclared_option(self):
        router = CapabilityRouter(type("S", (), {})())
        with pytest.raises(ValueError, match="Unknown options for extract: useragent"):
            await router.dispatch("extract", {"url": "https://example.com", "schema": {"fields": []}, "options": {"useragent": "secret"}})

    def test_public_schemas_reject_undeclared_sensitive_fields(self):
        from sieve.command_router import CAPABILITY_REGISTRY

        defs = {name: capability.definition for name, capability in CAPABILITY_REGISTRY.items()}
        for name in ("smart_fetch", "extract"):
            schema = defs[name]["inputSchema"]
            assert schema["additionalProperties"] is False
            assert "cookies" not in schema["properties"]
            assert "proxy" not in schema["properties"]
            assert "extra_headers" not in schema["properties"]
            assert schema["properties"]["options"]["additionalProperties"] is False
            option_props = schema["properties"]["options"]["properties"]
            assert not {"cookies", "proxy", "extra_headers", "useragent"} & set(option_props)

    def test_public_schema_descriptions_do_not_advertise_forbidden_options(self):
        from sieve.command_router import CAPABILITY_REGISTRY

        defs = {name: capability.definition for name, capability in CAPABILITY_REGISTRY.items()}
        forbidden = {"cookies", "proxy", "extra_headers", "useragent"}
        expected = {
            "smart_fetch": {"include_links", "capture_xhr", "respect_robots"},
            "smart_crawl": {"sitemap", "max_pages", "force_fetcher"},
            "extract": {"timeout", "force_fetcher", "cache_ttl"},
        }
        for name, legitimate_terms in expected.items():
            description = defs[name]["inputSchema"]["properties"]["options"]["description"].lower()
            assert not any(term in description for term in forbidden)
            for term in legitimate_terms:
                assert term in description

    def test_public_mcp_fetch_tiers_include_sleeper(self):
        from sieve.command_router import CAPABILITY_REGISTRY

        defs = {name: capability.definition for name, capability in CAPABILITY_REGISTRY.items()}
        fetch_tiers = defs["smart_fetch"]["inputSchema"]["properties"]["force_fetcher"]["enum"]
        crawl_tiers = defs["smart_crawl"]["inputSchema"]["properties"]["options"]["properties"]["force_fetcher"]["enum"]
        extract_tiers = defs["extract"]["inputSchema"]["properties"]["options"]["properties"]["force_fetcher"]["enum"]
        assert "sleeper" in fetch_tiers
        assert "sleeper" in crawl_tiers
        assert "sleeper" in extract_tiers


# ── failure envelope ─────────────────────────────────────────────────

class TestFailureEnvelope:
    """Failures must be JSON on stdout (CLI) or structured error (MCP),
    never raw tracebacks."""

    @pytest.mark.asyncio
    async def test_real_mcp_dispatch_rejects_hidden_sleeper_fetch(self):
        from mcp.types import CallToolRequestParams
        from sieve.server import MasterFetchServer

        server = MasterFetchServer(use_trafilatura=False).build_mcp_server()
        handler = server._request_handlers["tools/call"].handler
        result = await handler(None, CallToolRequestParams(name="sleeper_fetch", arguments={"url": "https://example.com"}))
        assert result.is_error is True
        payload = json.loads(result.content[0].text)
        assert {key: payload[key] for key in ("category", "error")} == {"category": "input", "error": "Invalid input."}
        assert payload["diagnostic"]["category"] == "input"
        assert payload["diagnostic"]["retryable"] is False

    @pytest.mark.asyncio
    async def test_real_mcp_smart_crawl_rejects_sensitive_options_without_echoing_values(self, monkeypatch):
        from mcp.types import CallToolRequestParams
        from sieve.server import MasterFetchServer

        impl = MasterFetchServer(use_trafilatura=False)
        monkeypatch.setattr(impl, "smart_crawl", lambda **kwargs: pytest.fail("must reject before handler"))
        server = impl.build_mcp_server()
        handler = server._request_handlers["tools/call"].handler
        result = await handler(None, CallToolRequestParams(
            name="smart_crawl",
            arguments={"url": "https://example.com", "options": {"cookies": "secret-cookie"}},
        ))
        assert result.is_error is True
        error = json.loads(result.content[0].text)["error"]
        assert error == "Invalid input."
        assert "secret-cookie" not in error

    @pytest.mark.asyncio
    async def test_real_mcp_smart_crawl_accepts_declared_options(self, monkeypatch):
        from mcp.types import CallToolRequestParams
        from sieve.crawl import CrawlResponseModel
        from sieve.server import MasterFetchServer

        impl = MasterFetchServer(use_trafilatura=False)
        async def fake_crawl(**kwargs):
            assert kwargs == {"url": "https://example.com", "max_pages": 2}
            return CrawlResponseModel(start_url=kwargs["url"], pages=[])
        monkeypatch.setattr(impl, "smart_crawl", fake_crawl)
        server = impl.build_mcp_server()
        handler = server._request_handlers["tools/call"].handler
        result = await handler(None, CallToolRequestParams(
            name="smart_crawl",
            arguments={"url": "https://example.com", "options": {"max_pages": 2}},
        ))
        assert result.is_error is False
        assert result.structured_content["start_url"] == "https://example.com"

    def test_cli_fetch_failure_is_json_on_stderr(self, monkeypatch, capsys):
        """Server parser routes fetch failures to stderr as JSON."""
        class FakeServer:
            def __init__(self, **kw):
                pass
            async def smart_fetch(self, url, **kw):
                raise TimeoutError("private=/private/session.json token=sk-live-secret-1234567890")

        monkeypatch.setattr("sieve.server.MasterFetchServer", FakeServer)
        monkeypatch.setattr(sys, "argv", ["sieve", "fetch", "https://fail.test"])
        from sieve import cli
        cli.main()
        err = capsys.readouterr().err
        out = json.loads(err)
        assert out["ok"] is False
        assert out["category"] == "timeout"
        assert out["error"] == "The operation timed out."
        assert "alice" not in err and "sk-live-secret" not in err

    def test_cli_search_failure_is_json_on_stderr(self, monkeypatch, capsys):
        """Server parser routes search failures to stderr as JSON."""
        class FakeServer:
            def __init__(self, **kw):
                pass
            async def smart_search(self, query, **kw):
                failure = RuntimeError("private=/private/session.json token=sk-live-secret-1234567890")
                failure.category = "blocked"
                raise failure

        monkeypatch.setattr("sieve.server.MasterFetchServer", FakeServer)
        monkeypatch.setattr(sys, "argv", ["sieve", "search", "fail"])
        from sieve import cli
        cli.main()
        err = capsys.readouterr().err
        out = json.loads(err)
        assert out["ok"] is False
        assert out["category"] == "blocked"
        assert out["error"] == "Access was blocked."
        assert "alice" not in err and "sk-live-secret" not in err

    @pytest.mark.asyncio
    async def test_real_mcp_failure_never_echoes_backend_values(self, monkeypatch):
        from mcp.types import CallToolRequestParams
        from sieve.server import MasterFetchServer

        impl = MasterFetchServer()
        async def fail(**kwargs):
            raise RuntimeError("private=/private/session.json token=sk-live-secret-1234567890")
        monkeypatch.setattr(impl, "smart_fetch", fail)
        handler = impl.build_mcp_server()._request_handlers["tools/call"].handler
        result = await handler(None, CallToolRequestParams(
            name="smart_fetch", arguments={"url": "https://example.test"},
        ))
        assert result.is_error is True
        payload = json.loads(result.content[0].text)
        assert {key: payload[key] for key in ("category", "error")} == {
            "category": "internal", "error": "The operation failed.",
        }
        assert payload["diagnostic"]["safe_message"] == "The operation failed."
        assert "/private/" not in result.content[0].text
        assert "sk-live-secret" not in result.content[0].text

    @pytest.mark.asyncio
    async def test_mcp_smart_fetch_error_propagates(self):
        """MCP router lets server errors propagate to the transport layer."""
        class FakeServer:
            async def smart_fetch(self, url, **kw):
                raise ValueError("blocked")

        router = CapabilityRouter(FakeServer())
        with pytest.raises(ValueError, match="blocked"):
            await router.dispatch("smart_fetch", {"url": "https://x.test"})


# ── capability router invariants ─────────────────────────────────────

class TestCapabilityRouterInvariants:
    """The router must enforce canonical naming and reject retired forms."""

    @pytest.mark.asyncio
    async def test_prefixed_mcp_name_rejected(self):
        router = CapabilityRouter(type("S", (), {"version": AsyncMock(return_value=VersionInfoModel(version="x"))})())
        with pytest.raises(ValueError, match="canonical bare"):
            await router.dispatch("mcp_version", {})

    @pytest.mark.asyncio
    async def test_unknown_name_rejected(self):
        router = CapabilityRouter(type("S", (), {})())
        with pytest.raises(ValueError, match="Unknown tool"):
            await router.dispatch("retired_tool", {})

    @pytest.mark.asyncio
    async def test_hyphenated_name_rejected(self):
        router = CapabilityRouter(type("S", (), {})())
        with pytest.raises(ValueError, match="Unknown tool"):
            await router.dispatch("smart-fetch", {})

    def test_canonical_set_matches_public_tools(self):
        """Every public MCP tool must be in the canonical set."""
        from sieve.server import _PUBLIC_MCP_TOOLS
        assert _PUBLIC_MCP_TOOLS <= CANONICAL_CAPABILITIES


# ── non-JSON argument rejection ──────────────────────────────────────

class TestArgumentValidation:
    """MCP dispatch must reject non-object arguments (legacy positional calls)."""

    @pytest.mark.asyncio
    async def test_string_argument_rejected(self):
        router = CapabilityRouter(type("S", (), {})())
        with pytest.raises((ValueError, TypeError)):
            await router.dispatch("smart_fetch", "not a dict")

    @pytest.mark.asyncio
    async def test_list_argument_rejected(self):
        router = CapabilityRouter(type("S", (), {})())
        with pytest.raises((ValueError, TypeError)):
            await router.dispatch("smart_fetch", ["url"])
