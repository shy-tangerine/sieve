"""Small, bounded declarative DAG runner for composing Sieve operations.

Specs contain data and references to named Sieve stages only.  They never
contain Python, shell commands, or import paths.  The runner is deliberately
boring: validate first, execute in stable topological order, and return JSON
friendly records even when one node fails.
"""
from __future__ import annotations

import asyncio
import inspect
import time
from collections import defaultdict, deque
from typing import Any, Callable
from urllib.parse import urlparse

from sieve.public_output import safe_error
from sieve.security import SecurityError, validate_url
from sieve.resource_budget import budgeted_collection, bound_output, current_budget, BudgetExceeded

Stage = Callable[..., Any]
STAGES = {"fetch", "extract", "batch_extract", "schema_gen", "search_fetch"}
_FORBIDDEN = {"command", "cmd", "code", "exec", "import", "module"}


def validate_spec(spec: dict[str, Any], *, max_nodes: int = 32) -> list[dict[str, Any]]:
    if not isinstance(spec, dict) or not isinstance(spec.get("nodes"), list):
        raise ValueError("pipeline spec must be an object with a nodes array")
    nodes = spec["nodes"]
    if not 1 <= len(nodes) <= max_nodes:
        raise ValueError(f"node count must be between 1 and {max_nodes}")
    ids: set[str] = set()
    normalized: list[dict[str, Any]] = []
    for node in nodes:
        if not isinstance(node, dict) or not isinstance(node.get("id"), str):
            raise ValueError("each node needs a string id")
        node_id = node["id"]
        if not node_id or node_id in ids:
            raise ValueError(f"duplicate or empty node id: {node_id!r}")
        if node.get("stage") not in STAGES:
            raise ValueError(f"unknown stage for node {node_id!r}")
        if any(key in node for key in _FORBIDDEN):
            raise ValueError(f"arbitrary execution fields are forbidden in node {node_id!r}")
        deps = node.get("depends_on", [])
        if isinstance(deps, str) or not isinstance(deps, list) or not all(isinstance(x, str) for x in deps):
            raise ValueError(f"depends_on must be a list of ids for node {node_id!r}")
        if node["stage"] in {"fetch", "extract"} and not isinstance(node.get("url"), str):
            raise ValueError(f"{node['stage']} node {node_id!r} requires a URL")
        if node["stage"] in {"extract", "batch_extract"} and not isinstance(node.get("schema"), dict):
            raise ValueError(f"{node['stage']} node {node_id!r} requires a schema object")
        if node["stage"] == "schema_gen" and (not isinstance(node.get("sample_html"), str)
                                                or not isinstance(node.get("fields"), list)
                                                or not node["fields"]
                                                or not all(isinstance(x, str) and x for x in node["fields"])):
            raise ValueError(f"schema_gen node {node_id!r} requires sample_html and fields")
        if node["stage"] == "batch_extract":
            urls = node.get("urls")
            if not isinstance(urls, list) or not urls or not all(isinstance(x, str) for x in urls):
                raise ValueError(f"batch_extract node {node_id!r} requires a non-empty urls array")
        if node["stage"] == "search_fetch":
            if not isinstance(node.get("results"), list):
                raise ValueError(f"search_fetch node {node_id!r} requires a results array")
            # Bound fan-out inputs at the pipeline boundary (issue #8):
            # declarative specs are agent-facing, so bools, zero, negative and
            # huge values must be rejected before any task set is created.
            if "max_results" in node and (
                not isinstance(node["max_results"], int) or isinstance(node["max_results"], bool)
                or not 1 <= node["max_results"] <= 1000
            ):
                raise ValueError(f"search_fetch node {node_id!r} requires integer max_results between 1 and 1000")
            if "max_concurrency" in node and (
                not isinstance(node["max_concurrency"], int) or isinstance(node["max_concurrency"], bool)
                or not 1 <= node["max_concurrency"] <= 32
            ):
                raise ValueError(f"search_fetch node {node_id!r} requires integer max_concurrency between 1 and 32")
            for key in ("allowed_domains", "allowed_source_types"):
                if key in node and (not isinstance(node[key], list) or not all(isinstance(x, str) for x in node[key])):
                    raise ValueError(f"search_fetch node {node_id!r} requires a string {key} array")
            if "min_relevance" in node and not isinstance(node["min_relevance"], (int, float)):
                raise ValueError(f"search_fetch node {node_id!r} requires numeric min_relevance")
        ids.add(node_id)
        normalized.append(node)
    for node in normalized:
        for dep in node.get("depends_on", []):
            if dep not in ids:
                raise ValueError(f"unknown dependency {dep!r} for node {node['id']!r}")
    return normalized


def _order(nodes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_id = {n["id"]: n for n in nodes}
    incoming = {n["id"]: set(n.get("depends_on", [])) for n in nodes}
    children: dict[str, list[str]] = defaultdict(list)
    for node_id, deps in incoming.items():
        for dep in deps:
            children[dep].append(node_id)
    ready = deque(n["id"] for n in nodes if not incoming[n["id"]])
    result = []
    while ready:
        node_id = ready.popleft()
        result.append(by_id[node_id])
        for child in sorted(children[node_id], key=lambda x: next(i for i, n in enumerate(nodes) if n["id"] == x)):
            incoming[child].remove(node_id)
            if not incoming[child]:
                ready.append(child)
    if len(result) != len(nodes):
        raise ValueError("pipeline contains a cycle")
    return result


def select_search_results(
    results: list[dict[str, Any]], *, max_results: int = 5,
    allowed_domains: list[str] | None = None,
    allowed_source_types: list[str] | None = None,
    min_relevance: float | None = None,
) -> list[dict[str, Any]]:
    """Select safe URLs from normalized search results in their input order.

    Search ranking is supplied by the search engine and is never inferred from
    page content. Invalid URLs and records failing explicit filters are
    dropped; the returned records retain the original result and its index so
    callers can audit provenance.
    """
    if not isinstance(results, list):
        raise ValueError("search results must be an array")
    if max_results < 1 or max_results > 1000:
        raise ValueError("max_results must be between 1 and 1000")
    if min_relevance is not None and (not isinstance(min_relevance, (int, float)) or not 0 <= min_relevance <= 1):
        raise ValueError("min_relevance must be between 0 and 1")
    domains = {str(d).strip().lower().removeprefix("www.").rstrip(".") for d in (allowed_domains or []) if str(d).strip()}
    source_types = {str(s).strip().lower() for s in (allowed_source_types or []) if str(s).strip()}
    selected: list[dict[str, Any]] = []
    for index, item in enumerate(results):
        if not isinstance(item, dict) or not isinstance(item.get("url"), str):
            continue
        score = item.get("relevance_score")
        if min_relevance is not None and (not isinstance(score, (int, float)) or score < min_relevance):
            continue
        source_type = str(item.get("source_type", "other")).lower()
        if source_types and source_type not in source_types:
            continue
        try:
            url = validate_url(item["url"])
            host = (urlparse(url).hostname or "").lower().removeprefix("www.").rstrip(".")
        except (SecurityError, ValueError):
            continue
        if domains and not any(host == domain or host.endswith("." + domain) for domain in domains):
            continue
        selected.append({
            "url": url,
            "search_index": index,
            "search_provenance": {key: item[key] for key in (
                "title", "snippet", "source", "sources", "position", "consensus",
                "relevance_score", "fetch_relevance", "source_type",
            ) if key in item},
        })
        if len(selected) >= max_results:
            break
    return selected


async def _search_fetch(
    results: list[dict[str, Any]], fetch: Stage | None, *, max_results: int,
    allowed_domains: list[str] | None, allowed_source_types: list[str] | None,
    min_relevance: float | None, timeout: float, max_concurrency: int,
) -> dict[str, Any]:
    # Defense in depth: validate_spec rejects out-of-range values, but
    # _search_fetch can also be reached with handler-supplied nodes.
    if isinstance(max_concurrency, bool) or not isinstance(max_concurrency, int) or not 1 <= max_concurrency <= 32:
        raise ValueError("max_concurrency must be an integer between 1 and 32")
    selected = select_search_results(results, max_results=max_results,
                                     allowed_domains=allowed_domains,
                                     allowed_source_types=allowed_source_types,
                                     min_relevance=min_relevance)
    semaphore = asyncio.Semaphore(max_concurrency)
    started = time.monotonic()

    async def one(item: dict[str, Any]) -> dict[str, Any]:
        remaining = timeout - (time.monotonic() - started)
        if remaining <= 0:
            return {"ok": False, "status": "timeout", "url": item["url"],
                    **safe_error(TimeoutError()), "provenance": item}
        try:
            async with semaphore:
                value = await _call(fetch, item["url"], timeout=remaining)
            return {"ok": True, "status": "completed", "url": item["url"], "result": value, "provenance": item}
        except asyncio.TimeoutError as exc:
            return {"ok": False, "status": "timeout", "url": item["url"],
                    **safe_error(exc), "provenance": item}
        except Exception as exc:
            return {"ok": False, "status": "failed", "url": item["url"],
                    **safe_error(exc, fallback_category="network"), "provenance": item}

    items = await asyncio.gather(*(one(item) for item in selected))
    return {"items": items, "selected": len(selected),
            "provenance": {"stage": "search_fetch", "selection": {
                "max_results": max_results, "allowed_domains": allowed_domains or [],
                "allowed_source_types": allowed_source_types or [], "min_relevance": min_relevance}}}


async def _call(fn: Stage | None, *args: Any, timeout: float, **kwargs: Any) -> Any:
    if fn is None:
        raise ValueError("pipeline stage requires an implementation")
    account = current_budget()
    if account is not None:
        timeout = min(timeout, account.time_remaining)
    if inspect.iscoroutinefunction(fn):
        return await asyncio.wait_for(fn(*args, **kwargs), timeout=timeout)
    value = await asyncio.wait_for(asyncio.to_thread(fn, *args, **kwargs), timeout=timeout)
    if inspect.isawaitable(value):
        return await asyncio.wait_for(value, timeout=min(timeout, account.time_remaining) if account else timeout)
    return value


@budgeted_collection
async def run_pipeline(
    spec: dict[str, Any], *, fetch: Stage | None = None, extract: Stage | None = None,
    batch_extract: Stage | None = None, schema_generator: Stage | None = None,
    max_nodes: int = 32, max_items: int = 1000, search_max_concurrency: int = 4,
    timeout: float = 300,
) -> list[dict[str, Any]]:
    if (timeout <= 0 or timeout > 3600 or max_items < 1
            or isinstance(search_max_concurrency, bool)
            or not isinstance(search_max_concurrency, int)
            or not 1 <= search_max_concurrency <= 32):
        raise ValueError(
            "timeout must be between 0 and 3600, max_items must be positive, "
            "and search_max_concurrency must be an integer between 1 and 32")
    nodes = _order(validate_spec(spec, max_nodes=max_nodes))
    if not all((fetch if n["stage"] in {"fetch", "search_fetch"} else extract if n["stage"] == "extract" else batch_extract if n["stage"] == "batch_extract" else schema_generator) for n in nodes):
        raise ValueError("all stages require an installed Sieve handler")
    started = time.monotonic()
    results: dict[str, dict[str, Any]] = {}
    for node in nodes:
        node_id, stage = node["id"], node["stage"]
        deps = node.get("depends_on", [])
        provenance = {"node": node_id, "stage": stage, "depends_on": deps}
        failed = next((results[d] for d in deps if not results[d]["ok"]), None)
        if failed:
            results[node_id] = {"ok": False, "status": "skipped", "error": "dependency failed", "provenance": provenance}
            continue
        remaining = timeout - (time.monotonic() - started)
        if remaining <= 0:
            results[node_id] = {"ok": False, "status": "timeout",
                                **safe_error(TimeoutError()), "provenance": provenance}
            continue
        try:
            account = current_budget()
            if account is not None and (account.expired or not account.charge("items", 1)):
                raise BudgetExceeded("pipeline resource limit")
            if stage == "fetch":
                value = await _call(fetch, node["url"], timeout=remaining)
            elif stage == "extract":
                value = await _call(extract, node["url"], node["schema"], timeout=remaining)
            elif stage == "batch_extract":
                value = await _call(batch_extract, node["urls"], node["schema"], timeout=remaining)
            elif stage == "search_fetch":
                value = await _search_fetch(
                    node["results"], fetch, max_results=node.get("max_results", 5),
                    allowed_domains=node.get("allowed_domains"),
                    allowed_source_types=node.get("allowed_source_types"),
                    min_relevance=node.get("min_relevance"), timeout=remaining,
                    max_concurrency=node.get("max_concurrency", search_max_concurrency))
            else:
                # The optional handler is supplied by an integration; one call
                # produces a reusable schema, never one model call per page.
                value = await _call(schema_generator, node["sample_html"], node["fields"], timeout=remaining)
            if isinstance(value, list) and len(value) > max_items:
                raise ValueError(f"output item count exceeds max_items={max_items}")
            if isinstance(value, dict) and isinstance(value.get("items"), list) and len(value["items"]) > max_items:
                raise ValueError(f"output item count exceeds max_items={max_items}")
            value = bound_output(value, owner=run_pipeline)
            results[node_id] = {"ok": True, "status": "completed", "result": value, "provenance": provenance}
            if account is not None:
                results[node_id]["resource_budget"] = account.report()
        except asyncio.TimeoutError as exc:
            results[node_id] = {"ok": False, "status": "timeout",
                                **safe_error(exc), "provenance": provenance}
        except Exception as exc:
            # Rationale: supplied handlers may raise arbitrary exceptions; isolate the failed node.
            results[node_id] = {"ok": False, "status": "failed",
                                **safe_error(exc, fallback_category="internal"),
                                "provenance": provenance}
    return [results[n["id"]] | {"id": n["id"]} for n in nodes]
