import pytest

from sieve.retrieval import InMemoryRetrievalAdapter


@pytest.mark.asyncio
async def test_in_memory_adapter_exposes_retrieval_interface_and_records_options():
    adapter = InMemoryRetrievalAdapter({"https://example.test": "page"})

    assert await adapter.fetch("https://example.test", timeout=3) == "page"
    assert adapter.requests == [("https://example.test", {"timeout": 3})]


@pytest.mark.asyncio
async def test_in_memory_adapter_preserves_transport_failures():
    adapter = InMemoryRetrievalAdapter({"https://example.test": RuntimeError("blocked")})

    with pytest.raises(RuntimeError, match="blocked"):
        await adapter.fetch("https://example.test")
