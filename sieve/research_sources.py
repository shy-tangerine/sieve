"""Source-neutral orchestration over Sieve's Reddit source adapters."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, Field

from sieve.arctic_shift import ArcticShiftClient, ArcticShiftItem, ArcticShiftPage, ArcticShiftResponse
from sieve.reddit_rss import RedditRSSRecord, feed_url, fetch_reddit_rss


class SourceRecord(BaseModel):
    """Stable source-neutral record shared by RSS and Arctic Shift.

    ``raw_payload`` is None by default (#183/#190): large untrusted source
    bodies are not automatically duplicated into every record sent to an
    agent. Pass ``include_raw=True`` to :func:`ingest_source` to opt in,
    optionally with a per-record character cap.
    """

    id: str = ""
    title: str = ""
    body: str = ""
    author: str = ""
    url: str = ""
    published_at: str = ""
    source: str
    source_type: str
    raw_payload: Any = None
    provenance: dict[str, Any] = Field(default_factory=dict)


class SourceIngestResponse(BaseModel):
    """Reproducible ingestion result, including adapter payloads/provenance."""

    source: str
    request_url: str = ""
    fetched_at: str = ""
    records: list[SourceRecord] = Field(default_factory=list)
    raw_payload: Any = None
    provenance: dict[str, Any] = Field(default_factory=dict)
    next_params: dict[str, Any] | None = None
    error: str = ""


def _rss_record(record: RedditRSSRecord, *, include_raw: bool,
                max_raw_chars: int) -> SourceRecord:
    raw = record.raw_payload if include_raw else None
    if isinstance(raw, str) and max_raw_chars and len(raw) > max_raw_chars:
        raw = raw[:max_raw_chars] + "\n[truncated: raw payload budget exceeded]"
    return SourceRecord(
        id=record.source_id, title=record.title,
        body=record.content or record.summary, author=record.author,
        url=record.url, published_at=record.published_at,
        source=record.source, source_type="reddit_rss",
        raw_payload=raw,
        provenance=dict(record.provenance),
    )


def _arctic_item(item: ArcticShiftItem, *, include_raw: bool,
                 max_raw_chars: int) -> SourceRecord:
    # Normalized fields always come from the payload; ``include_raw`` only
    # controls whether the payload is *also* duplicated into raw_payload.
    payload = dict(item.payload)
    permalink = str(payload.get("permalink") or payload.get("url") or "")
    if permalink.startswith("/"):
        permalink = "https://www.reddit.com" + permalink
    source = item.source
    return SourceRecord(
        id=str(payload.get("id") or payload.get("name") or ""),
        title=str(payload.get("title") or ""),
        body=str(payload.get("selftext") or payload.get("body") or ""),
        author=str(payload.get("author") or payload.get("author_fullname") or ""),
        url=permalink,
        published_at=str(payload.get("created_utc") or payload.get("created") or ""),
        source="reddit", source_type="arctic_shift",
        raw_payload=(_cap_raw(payload, max_raw_chars) if include_raw else None),
        provenance={
            "provider": "Arctic Shift", "endpoint": source.endpoint,
            "request_url": source.request_url,
            "request_params": dict(source.request_params),
            "fetched_at": source.fetched_at,
            "api_metadata": dict(source.api_metadata), "kind": item.kind,
        },
    )


def _tree_items(value: Any) -> list[dict[str, Any]]:
    """Flatten Arctic Shift comment-tree nodes without dropping replies."""
    if not isinstance(value, list):
        return []
    flattened: list[dict[str, Any]] = []
    for node in value:
        if not isinstance(node, dict):
            continue
        payload = node.get("data") if isinstance(node.get("data"), dict) else node
        flattened.append({"kind": node.get("kind", "comment"), "payload": payload})
        replies = payload.get("replies") if isinstance(payload, dict) else None
        if isinstance(replies, dict):
            replies = replies.get("data")
        flattened.extend(_tree_items(replies))
    return flattened


def _cap_raw(value: Any, max_raw_chars: int) -> Any:
    """Cap a raw payload's serialized size, appending a truncation marker."""
    if not max_raw_chars:
        return value
    try:
        import json as _json
        serialized = _json.dumps(value, default=str) if not isinstance(value, str) else value
    except Exception:
        return value
    if len(serialized) <= max_raw_chars:
        return value
    return serialized[:max_raw_chars] + "\n[truncated: raw payload budget exceeded]"


def _tree_records(response: ArcticShiftResponse, *, include_raw: bool,
                  max_raw_chars: int) -> list[SourceRecord]:
    data = response.raw_payload.get("data") if isinstance(response.raw_payload, dict) else None
    return [_arctic_item(ArcticShiftItem(str(node["kind"]), node["payload"], response),
                         include_raw=include_raw, max_raw_chars=max_raw_chars)
            for node in _tree_items(data)]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


async def _call_arctic(operation: Callable[[], Any]) -> Any:
    """Run Arctic Shift's synchronous client away from the MCP event loop."""
    return await asyncio.to_thread(operation)


async def ingest_source(
    source: str, *, target: str | None = None, kind: str = "posts",
    subreddit: str | None = None, query: str | None = None,
    ids: list[str] | tuple[str, ...] | None = None, post_id: str | None = None,
    limit: int = 25, after: str | None = None, before: str | None = None,
    sort: str = "desc", max_pages: int = 1, timeout: float = 15.0,
    retries: int = 2, rss_session: Any = None,
    include_raw: bool = False, max_raw_chars: int = 20_000,
    arctic_client: ArcticShiftClient | None = None,
    arctic_transport: Callable[[Callable[[], Any]], Awaitable[Any]] | None = None,
) -> SourceIngestResponse:
    """Ingest Reddit RSS or Arctic Shift while preserving adapter semantics.

    Normalized bounded records are the default public schema (#190). Raw
    adapter payloads are opt-in via ``include_raw`` and capped at
    ``max_raw_chars`` with truncation metadata (#183).
    """
    if source not in {"reddit_rss", "arctic_shift"}:
        raise ValueError("source must be 'reddit_rss' or 'arctic_shift'")
    if max_pages < 1:
        raise ValueError("max_pages must be positive")

    if source == "reddit_rss":
        target = target or subreddit or ""
        url = feed_url(target, kind="thread" if kind == "thread" else ("search" if query else None), query=query)
        records = await fetch_reddit_rss(url, session=rss_session, limit=limit, timeout=timeout, retries=retries)
        return SourceIngestResponse(
            source=source, request_url=url,
            fetched_at=_now(),
        records=[_rss_record(record, include_raw=include_raw, max_raw_chars=max_raw_chars)
                 for record in records],
        raw_payload=([record.raw_payload for record in records]
                     if include_raw else None),
            provenance={"adapter": "sieve.reddit_rss", "feed_url": url},
        )

    if kind not in {"posts", "comments", "ids", "thread"}:
        raise ValueError("Arctic Shift kind must be posts, comments, ids, or thread")
    client = arctic_client or ArcticShiftClient(timeout=timeout, retry_count=retries)
    call = arctic_transport or _call_arctic
    records: list[SourceRecord] = []
    raw_payloads: list[Any] = []
    request_url = fetched_at = ""
    next_params: dict[str, Any] | None = None
    page_count = 0

    if kind in {"posts", "comments"}:
        params: dict[str, Any] = {"limit": limit, "sort": sort, "subreddit": subreddit, "after": after, "before": before}
        params = {key: value for key, value in params.items() if value is not None}
        if query is not None:
            params["query"] = query
        for _ in range(max_pages):
            method = client.search_posts if kind == "posts" else client.search_comments
            page_query = params.pop("query", None)
            page: ArcticShiftPage = await call(lambda method=method, params=params, page_query=page_query: method(page_query, **params))
            page_count += 1
            records.extend(_arctic_item(item, include_raw=include_raw, max_raw_chars=max_raw_chars)
                           for item in page.items)
            raw_payloads.append(page.source.raw_payload if include_raw else None)
            request_url, fetched_at = page.source.request_url, page.source.fetched_at
            next_params = dict(page.next_params) if page.next_params else None
            if not next_params:
                break
            params = dict(next_params)
    elif kind == "ids":
        if not ids:
            raise ValueError("ids is required for Arctic Shift ID lookup")
        method = client.get_comments if all(str(value).startswith("t1_") for value in ids) else client.get_posts
        items: tuple[ArcticShiftItem, ...] = await call(lambda: method(ids))
        records.extend(_arctic_item(item, include_raw=include_raw, max_raw_chars=max_raw_chars)
                       for item in items)
        if items:
            request_url, fetched_at = items[0].source.request_url, items[0].source.fetched_at
            raw_payloads.append(items[0].source.raw_payload if include_raw else None)
    else:
        if not post_id:
            raise ValueError("post_id is required for Arctic Shift thread lookup")
        response: ArcticShiftResponse = await call(lambda: client.get_thread(post_id, limit=limit))
        records.extend(_tree_records(response, include_raw=include_raw, max_raw_chars=max_raw_chars))
        request_url, fetched_at = response.request_url, response.fetched_at
        raw_payloads.append(response.raw_payload if include_raw else None)

    return SourceIngestResponse(
        source=source, request_url=request_url, fetched_at=fetched_at,
        records=records, raw_payload=raw_payloads, next_params=next_params,
        provenance={"adapter": "sieve.arctic_shift", "kind": kind,
                    "pages": page_count or 1},
    )


__all__ = ["SourceRecord", "SourceIngestResponse", "ingest_source"]
