import asyncio
import json

import pytest

from sieve.sdk import SieveClient, SieveError


class Backend:
    async def smart_search(self, query, **kwargs):
        return {"query": query, "kwargs": kwargs}

    async def smart_fetch(self, url, **kwargs):
        return {"url": url, "kwargs": kwargs}

    async def smart_crawl(self, url, **kwargs):
        return {"url": url, "kwargs": kwargs}

    async def extract(self, url, **kwargs):
        if "bad" in url:
            raise RuntimeError("blocked")
        return {"url": url, "kwargs": kwargs}


def test_async_operations_forward_consistent_limits():
    async def go():
        client = SieveClient(Backend(), timeout=12, concurrency=2, max_pages=4)
        search = await client.search("sieve", max_results=4)
        fetch = await client.fetch("https://a.example")
        crawl = await client.crawl("https://a.example")
        assert "timeout" not in search["kwargs"]
        assert fetch["kwargs"]["timeout"] == 12000
        assert crawl["kwargs"]["max_pages"] == 4
        assert crawl["kwargs"]["timeout"] == 12000
    asyncio.run(go())


def test_failures_are_stable_and_batch_partial_failure():
    async def go():
        client = SieveClient(Backend(), max_pages=3)
        with pytest.raises(SieveError) as caught:
            await client.extract("https://bad.example", {})
        assert caught.value.operation == "extract"
        records = await client.batch(["https://a.example", "https://bad.example"], {})
        assert [item["ok"] for item in records] == [True, False]
    asyncio.run(go())


def test_bounds_timeout_and_no_secret_persistence(tmp_path):
    with pytest.raises(ValueError):
        SieveClient(timeout=0)
    with pytest.raises(ValueError):
        SieveClient(concurrency=33)
    for bad_timeout in (True, float("nan"), float("inf")):
        with pytest.raises((TypeError, ValueError)):
            SieveClient(timeout=bad_timeout)
    for bad_concurrency in (True, 0, 34):
        with pytest.raises((TypeError, ValueError)):
            SieveClient(concurrency=bad_concurrency)

    async def slow(url, schema, **kwargs):
        await asyncio.sleep(0.05)
        return {}
    client = SieveClient(type("B", (), {"extract": slow})(), timeout=0.01)
    # Batch reports a per-item timeout as a structured failure.
    records = asyncio.run(client.batch(["https://a.example"], {}, checkpoint=str(tmp_path / "state.json"), checkpoint_key="fixture"))
    assert records[0]["ok"] is False
    state = (tmp_path / "state.json").read_text()
    assert "secret" not in state and "result" not in state


def test_async_context_closes_backend():
    class Closable(Backend):
        def __init__(self):
            self.closed = False

        async def close(self):
            self.closed = True

    async def go():
        backend = Closable()
        async with SieveClient(backend) as client:
            await client.fetch("https://a.example")
        assert backend.closed

    asyncio.run(go())


def test_sdk_and_batch_errors_keep_partial_success_without_exception_values():
    sentinel = "private=/private/session.json token=sk-live-secret-1234567890"

    class FailingBackend(Backend):
        async def extract(self, url, **kwargs):
            if "bad" in url:
                raise TimeoutError(sentinel)
            return {"content": ["Good content"], "is_truncated": True}

    async def go():
        client = SieveClient(FailingBackend())
        with pytest.raises(SieveError) as caught:
            await client.extract("https://bad.example", {})
        assert "alice" not in str(caught.value)
        assert "sk-live-secret" not in str(caught.value)
        records = await client.batch(["https://good.example", "https://bad.example"], {})
        assert [item["ok"] for item in records] == [True, False]
        assert records[0]["result"]["is_truncated"] is True
        assert records[0]["diagnostic"]["truncated"] == ["output_chars"]
        assert records[1]["category"] == "timeout"
        assert records[1]["diagnostic"]["retryable"] is True
        assert caught.value.diagnostic["category"] == "timeout"
        assert "alice" not in json.dumps(records)
        assert "sk-live-secret" not in json.dumps(records)

    asyncio.run(go())
