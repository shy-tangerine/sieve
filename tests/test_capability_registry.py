"""Structural routing contracts independent of implementation source strings."""
from dataclasses import replace

import pytest

from sieve.command_router import CAPABILITY_REGISTRY, CapabilityRouter


@pytest.mark.asyncio
@pytest.mark.parametrize("name", sorted(CAPABILITY_REGISTRY))
async def test_every_registered_handler_is_dispatched(monkeypatch, name):
    server = object()
    arguments = {"options": {"test": True}}
    calls = []

    async def handler(adapter, args, options):
        calls.append((adapter, args, options))
        return "dispatched"

    monkeypatch.setitem(CAPABILITY_REGISTRY, name, replace(CAPABILITY_REGISTRY[name], handler=handler))
    assert await CapabilityRouter(server).dispatch(name, arguments) == "dispatched"
    assert calls == [(server, arguments, {"test": True})]


def test_public_visibility_and_definitions_share_registry():
    from sieve.server import _PUBLIC_MCP_TOOLS
    assert _PUBLIC_MCP_TOOLS == {"smart_fetch", "smart_crawl", "extract", "screenshot", "smart_search",
                                 "detect_block", "extract_jsonl", "schema_gen", "sitemap_harvest", "research_ingest"}
    assert _PUBLIC_MCP_TOOLS == {n for n, c in CAPABILITY_REGISTRY.items() if c.definition is not None}
    assert not {"sleeper_api", "sleeper_fetch"} & _PUBLIC_MCP_TOOLS
    for name, capability in CAPABILITY_REGISTRY.items():
        assert callable(capability.handler)
        if capability.definition is not None:
            assert capability.definition["name"] == name
            assert capability.definition["inputSchema"]["type"] == "object"
