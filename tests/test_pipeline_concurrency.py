"""Concurrency-contract tests for pipeline search_fetch fan-out (#205).

Offline counter-based tests: a counting fetch records peak in-flight
requests so the semaphore bound is proven, invalid max_concurrency inputs
(0, 33, bool) are rejected before any work, and the runtime-budget
cancellation path reports per-item timeouts. No network access.
"""
from __future__ import annotations

import asyncio

import pytest

from sieve.pipeline import _search_fetch, run_pipeline, validate_spec


def _spec(node_overrides: dict) -> dict:
    node = {"id": "search", "stage": "search_fetch",
            "results": [{"url": f"https://example.test/{i}"} for i in range(6)]}
    node.update(node_overrides)
    return {"version": 1, "nodes": [node]}


class TestPeakInFlightBound:
    @staticmethod
    async def _counting_fetch(url, in_flight, peak, delay=0.02):
        in_flight[0] += 1
        peak[0] = max(peak[0], in_flight[0])
        try:
            await asyncio.sleep(delay)
            return url
        finally:
            in_flight[0] -= 1

    @pytest.mark.parametrize("limit", [1, 2, 3])
    def test_peak_in_flight_never_exceeds_node_max_concurrency(self, limit):
        in_flight, peak = [0], [0]

        async def fetch(url):
            return await self._counting_fetch(url, in_flight, peak)

        items = asyncio.run(_search_fetch(
            [{"url": f"https://example.test/{i}"} for i in range(8)],
            fetch, max_results=8, allowed_domains=None, allowed_source_types=None,
            min_relevance=None, timeout=10.0, max_concurrency=limit))
        assert peak[0] <= limit
        assert items["selected"] == 8
        assert all(item["status"] == "completed" for item in items["items"])

    def test_default_runner_concurrency_bounds_fanout(self):
        """run_pipeline's search_max_concurrency default applies when the node
        does not override max_concurrency (documented default: 4)."""
        in_flight, peak = [0], [0]

        async def fetch(url):
            return await self._counting_fetch(url, in_flight, peak, delay=0.05)

        nodes = [{"id": "search", "stage": "search_fetch",
                  "results": [{"url": f"https://example.test/{i}"} for i in range(8)],
                  "max_results": 8}]
        records = asyncio.run(run_pipeline({"version": 1, "nodes": nodes},
                                           fetch=fetch, timeout=30))
        assert records[0]["ok"]
        # 8 items with the default concurrency of 4: at least two waves.
        assert 1 < peak[0] <= 4


class TestInvalidConcurrencyInputs:
    @pytest.mark.parametrize("bad", [0, -1, 33, 100, True, False, 2.5, "3"])
    def test_spec_rejects_invalid_max_concurrency_before_work(self, bad):
        with pytest.raises(ValueError, match="max_concurrency"):
            validate_spec(_spec({"max_concurrency": bad}))

    def test_runner_rejects_invalid_search_max_concurrency(self):
        with pytest.raises(ValueError):
            asyncio.run(run_pipeline(_spec({}), fetch=lambda u: u,
                                     search_max_concurrency=0))
        with pytest.raises(ValueError):
            asyncio.run(run_pipeline(_spec({}), fetch=lambda u: u,
                                     search_max_concurrency=True))

    @pytest.mark.asyncio
    async def test_search_fetch_defense_in_depth_rejects_bad_value(self):
        with pytest.raises(ValueError, match="max_concurrency must be an integer"):
            await _search_fetch([{"url": "https://example.test/0"}], lambda u: u,
                                max_results=1, allowed_domains=None,
                                allowed_source_types=None, min_relevance=None,
                                timeout=1.0, max_concurrency=True)


class TestCancellationAndTimeout:
    @pytest.mark.asyncio
    async def test_runtime_budget_cancels_inflight_and_reports_timeouts(self):
        started = asyncio.Event()

        async def slow_fetch(url):
            if not started.is_set():
                started.set()
            await asyncio.sleep(30)  # would exceed any runtime budget
            return url

        items = await _search_fetch(
            [{"url": f"https://example.test/{i}"} for i in range(4)],
            slow_fetch, max_results=4, allowed_domains=None,
            allowed_source_types=None, min_relevance=None,
            timeout=0.05, max_concurrency=2)
        statuses = {item["status"] for item in items["items"]}
        # Every item is reported; none is silently dropped.
        assert len(items["items"]) == 4
        assert statuses == {"timeout"}
        assert started.is_set()
