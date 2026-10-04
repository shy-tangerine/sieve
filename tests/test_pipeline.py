import asyncio
import time

import pytest

from sieve.pipeline import run_pipeline, select_search_results, validate_spec


def spec(*nodes):
    return {"version": 1, "nodes": list(nodes)}


def test_valid_graph_is_stable_and_has_provenance():
    async def fetch(url):
        return {"url": url, "content": "ok"}

    value = asyncio.run(run_pipeline(spec(
        {"id": "fetch", "stage": "fetch", "url": "https://example.test"},
        {"id": "extract", "stage": "extract", "url": "https://example.test", "schema": {}, "depends_on": ["fetch"]},
    ), fetch=fetch, extract=lambda url, schema: {"items": []}))
    assert [item["id"] for item in value] == ["fetch", "extract"]
    assert value[1]["provenance"]["depends_on"] == ["fetch"]


def test_rejects_cycle_unknown_stage_and_malformed_spec():
    with pytest.raises(ValueError, match="cycle"):
        asyncio.run(run_pipeline(spec(
            {"id": "a", "stage": "fetch", "url": "https://a.test", "depends_on": ["b"]},
            {"id": "b", "stage": "fetch", "url": "https://b.test", "depends_on": ["a"]},
        ), fetch=lambda url: {}))
    with pytest.raises(ValueError, match="unknown stage"):
        validate_spec(spec({"id": "x", "stage": "shell", "url": "https://x.test"}))
    with pytest.raises(ValueError, match="forbidden"):
        validate_spec(spec({"id": "x", "stage": "fetch", "url": "https://x.test", "command": "whoami"}))


def test_bound_violation_and_partial_failure_skip_dependents():
    async def fetch(url):
        if url.endswith("bad"):
            raise RuntimeError("blocked")
        return {"items": [1, 2]}

    records = asyncio.run(run_pipeline(spec(
        {"id": "bad", "stage": "fetch", "url": "https://x.test/bad"},
        {"id": "after", "stage": "fetch", "url": "https://x.test/after", "depends_on": ["bad"]},
    ), fetch=fetch))
    assert records[0]["status"] == "failed"
    assert records[1]["status"] == "skipped"
    with pytest.raises(ValueError, match="between 1 and 1"):
        asyncio.run(run_pipeline(spec(
            {"id": "a", "stage": "fetch", "url": "https://a.test"},
            {"id": "b", "stage": "fetch", "url": "https://b.test"},
        ), fetch=fetch, max_nodes=1))
    records = asyncio.run(run_pipeline(spec({"id": "a", "stage": "fetch", "url": "https://a.test"}),
                                       fetch=lambda url: [1, 2], max_items=1))
    assert records[0]["ok"] is False


def test_stage_exception_is_sanitized_without_changing_pipeline_order():
    sentinel = "SENTINEL_SECRET /private/path token=private"

    def fail(url):
        raise RuntimeError(sentinel)

    records = asyncio.run(run_pipeline(spec(
        {"id": "first", "stage": "fetch", "url": "https://example.test/first"},
        {"id": "second", "stage": "fetch", "url": "https://example.test/second"},
    ), fetch=fail))

    assert [record["id"] for record in records] == ["first", "second"]
    assert len(records) == 2
    assert all(record["status"] == "failed" for record in records)
    assert all(record["category"] == "internal" for record in records)
    assert all(sentinel not in record["error"] for record in records)


def test_stage_timeout_is_reported():
    def slow(url):
        time.sleep(0.1)
        return {"url": url}

    records = asyncio.run(run_pipeline(spec({"id": "slow", "stage": "fetch", "url": "https://slow.test"}),
                                       fetch=slow, timeout=0.01))
    assert records[0]["status"] == "timeout"


def test_batch_stage_is_supported():
    async def batch(urls, schema):
        return [{"url": url, "schema": schema} for url in urls]

    records = asyncio.run(run_pipeline(spec({"id": "batch", "stage": "batch_extract",
                                             "urls": ["https://a.test", "https://b.test"], "schema": {"fields": []}}),
                                       batch_extract=batch))
    assert records[0]["ok"] and len(records[0]["result"]) == 2


def test_optional_schema_generation_is_one_stage_call():
    calls = []

    def generate(sample_html, fields):
        calls.append((sample_html, fields))
        return {"baseSelector": "article", "fields": [{"name": fields[0], "selector": "h1"}]}

    records = asyncio.run(run_pipeline(spec({"id": "schema", "stage": "schema_gen",
                                             "sample_html": "<article><h1>x</h1></article>", "fields": ["title"]}),
                                       schema_generator=generate))
    assert records[0]["ok"] and records[0]["result"]["baseSelector"] == "article"
    assert len(calls) == 1


def test_search_fetch_filters_deterministically_and_keeps_provenance():
    seen = []
    async def fetch(url):
        seen.append(url)
        return {"url": url, "content": "ok"}
    results = [
        {"url": "https://docs.example/a", "title": "A", "relevance_score": .9, "source_type": "docs", "source": "one"},
        {"url": "https://other.example/b", "relevance_score": .99, "source_type": "blog"},
        {"url": "https://docs.example/c", "title": "C", "relevance_score": .8, "source_type": "docs", "source": "two"},
    ]
    records = asyncio.run(run_pipeline(spec({"id": "search", "stage": "search_fetch", "results": results,
                                             "max_results": 1, "allowed_domains": ["example"],
                                             "allowed_source_types": ["docs"], "min_relevance": .5}), fetch=fetch))
    assert seen == ["https://docs.example/a"]
    item = records[0]["result"]["items"][0]
    assert item["provenance"]["search_index"] == 0
    assert item["provenance"]["search_provenance"]["title"] == "A"


def test_search_fetch_rejects_bad_urls_empty_results_and_partial_failures():
    assert select_search_results([{"url": "javascript:alert(1)"}, {"url": "not a url"}]) == []
    async def fetch(url):
        if url.endswith("bad"):
            raise RuntimeError("blocked")
        return "ok"
    results = [{"url": "https://example.test/good"}, {"url": "https://example.test/bad"}]
    records = asyncio.run(run_pipeline(spec({"id": "search", "stage": "search_fetch", "results": results}), fetch=fetch))
    items = records[0]["result"]["items"]
    assert [item["status"] for item in items] == ["completed", "failed"]
    assert items[1]["category"] == "network"
    assert "blocked" not in items[1]["error"]


def test_search_fetch_timeout_and_fanout_bound():
    seen = []
    async def fetch(url):
        seen.append(url)
        await asyncio.sleep(.05)
        return url
    results = [{"url": f"https://example.test/{i}"} for i in range(4)]
    records = asyncio.run(run_pipeline(spec({"id": "search", "stage": "search_fetch", "results": results,
                                             "max_results": 2}), fetch=fetch, timeout=.01))
    assert len(seen) <= 2
    assert all(item["status"] == "timeout" for item in records[0]["result"]["items"])
    assert all(item["category"] == "timeout" for item in records[0]["result"]["items"])
