"""Resource-bound regression tests (OCR round-3 review findings).

run_batch's concurrency ceiling is enforced inside one() via `sem`
(batch_extract.py). A regression that moves the gather outside the
semaphore (or drops it) would silently spawn len(urls) simultaneous
extracts — pin the max-overlap contract so that can't happen quietly.
"""

import asyncio

import pytest

from sieve.batch_extract import run_batch


def test_run_batch_respects_concurrency_limit():
    """Extractor overlap must never exceed the concurrency limit."""
    state = {"active": 0, "max_active": 0}

    async def extract(url, schema):
        state["active"] += 1
        state["max_active"] = max(state["max_active"], state["active"])
        await asyncio.sleep(0.01)
        state["active"] -= 1
        return {"url": url}

    urls = [f"https://example.com/{i}" for i in range(12)]
    records = asyncio.run(run_batch(urls, {}, extractor=extract, concurrency=3))
    assert all(r["ok"] for r in records)
    assert state["max_active"] <= 3, (
        f"saw {state['max_active']} concurrent extractions, limit is 3"
    )


def test_run_batch_rejects_zero_concurrency():
    with pytest.raises(ValueError):
        asyncio.run(run_batch(["https://example.com"], {}, extractor=lambda *_: {}, concurrency=0))
