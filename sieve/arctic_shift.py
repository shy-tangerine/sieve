"""Best-effort client for the public Arctic Shift Reddit archive API.

Arctic Shift is a useful historical source, not a permanently available
service.  This module therefore keeps transport policy local, exposes the
upstream metadata on every response, and never treats an empty/error response
as proof that the archive has no matching data.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

logger = logging.getLogger("master-fetch.arctic-shift")

ARCTIC_SHIFT_BASE_URL = "https://arctic-shift.photon-reddit.com"
ARCTIC_SHIFT_API_VERSION = "public-api-v1 (endpoint paths documented by Arctic Shift)"
ARCTIC_SHIFT_API_METADATA: Mapping[str, str] = {
    "provider": "Arctic Shift",
    "api_version": ARCTIC_SHIFT_API_VERSION,
    "base_url": ARCTIC_SHIFT_BASE_URL,
    "documentation_url": "https://github.com/ArthurHeitmann/arctic_shift/blob/master/api/README.md",
    "status_url": "https://status.arctic-shift.photon-reddit.com",
    "availability": "best_effort",
    "availability_note": "Public service; no uptime or performance guarantees.",
}

_RETRYABLE_STATUSES = frozenset({408, 425, 429, 500, 502, 503, 504})
_MAX_RESPONSE_BYTES = 10 * 1024 * 1024


class ArcticShiftError(RuntimeError):
    """Base exception for Arctic Shift client failures."""


class ArcticShiftHTTPError(ArcticShiftError):
    """The upstream returned a non-success HTTP status after retries."""

    def __init__(self, status: int, url: str, message: str = "") -> None:
        self.status = status
        self.url = url
        super().__init__(message or f"Arctic Shift returned HTTP {status}: {url}")


class ArcticShiftTransportError(ArcticShiftError):
    """The request could not be completed or its JSON was invalid."""


@dataclass(frozen=True)
class ArcticShiftResponse:
    """One source response, including raw payload and provenance."""

    endpoint: str
    request_url: str
    request_params: Mapping[str, Any]
    raw_payload: Any
    status_code: int
    headers: Mapping[str, str]
    fetched_at: str
    api_metadata: Mapping[str, str] = field(default_factory=lambda: ARCTIC_SHIFT_API_METADATA)


@dataclass(frozen=True)
class ArcticShiftItem:
    """An item with its untouched upstream payload and source provenance."""

    kind: str
    payload: Mapping[str, Any]
    source: ArcticShiftResponse

    @property
    def id(self) -> str | None:
        value = self.payload.get("id")
        return str(value) if value is not None else None


@dataclass(frozen=True)
class ArcticShiftPage:
    """A search page and the date cursor for the next page, if derivable."""

    kind: str
    items: tuple[ArcticShiftItem, ...]
    source: ArcticShiftResponse
    next_params: Mapping[str, Any] | None = None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _clean_id(value: str) -> str:
    value = value.strip()
    if not value or "," in value:
        raise ValueError("IDs must be non-empty and may not contain commas")
    return value


class ArcticShiftClient:
    """Small, injectable, stdlib-only Arctic Shift API client.

    ``retry_count`` is the number of retries after the first attempt.  The
    default is intentionally modest because this is a shared free service.
    ``sleep`` is injectable for deterministic tests.
    """

    def __init__(
        self,
        *,
        base_url: str = ARCTIC_SHIFT_BASE_URL,
        timeout: float = 15.0,
        retry_count: int = 2,
        retry_backoff: float = 0.5,
        user_agent: str = "sieve-cli/arctic-shift-client",
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if timeout <= 0:
            raise ValueError("timeout must be positive")
        if retry_count < 0:
            raise ValueError("retry_count must not be negative")
        if retry_backoff < 0:
            raise ValueError("retry_backoff must not be negative")
        # Validate the endpoint in the constructor (issue #191): reject
        # credential-bearing, non-http(s), or malformed URLs before any
        # request building or provenance output can leak them.
        parsed = urlparse(base_url)
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            raise ValueError("base_url must be an http(s) URL with a hostname")
        if parsed.username or parsed.password:
            raise ValueError("base_url must not embed credentials")
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.retry_count = retry_count
        self.retry_backoff = retry_backoff
        self.user_agent = user_agent
        self._sleep = sleep

    @property
    def api_metadata(self) -> Mapping[str, str]:
        """Metadata callers can use to label this source and its limitations."""
        if self.base_url == ARCTIC_SHIFT_BASE_URL:
            return ARCTIC_SHIFT_API_METADATA
        return {**ARCTIC_SHIFT_API_METADATA, "base_url": self.base_url}

    def _get(self, endpoint: str, params: Mapping[str, Any]) -> ArcticShiftResponse:
        query = {key: value for key, value in params.items() if value is not None}
        url = f"{self.base_url}{endpoint}?{urlencode(query)}" if query else f"{self.base_url}{endpoint}"
        request = Request(url, headers={"Accept": "application/json", "User-Agent": self.user_agent})
        for attempt in range(self.retry_count + 1):
            try:
                with urlopen(request, timeout=self.timeout) as response:
                    status = int(response.status)
                    body = response.read(_MAX_RESPONSE_BYTES + 1)
                    if len(body) > _MAX_RESPONSE_BYTES:
                        raise ArcticShiftTransportError("Arctic Shift response exceeds 10 MiB")
                    headers = {str(k): str(v)[:512] for k, v in response.headers.items() if str(k).lower() in {"retry-after", "x-ratelimit-limit", "x-ratelimit-remaining", "x-ratelimit-reset"}}
                if status in _RETRYABLE_STATUSES and attempt < self.retry_count:
                    self._wait(attempt, headers)
                    continue
                if status < 200 or status >= 300:
                    raise ArcticShiftHTTPError(status, url)
                try:
                    payload = json.loads(body.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
                    raise ArcticShiftTransportError(f"Invalid JSON from Arctic Shift: {url}") from exc
                return ArcticShiftResponse(endpoint, url, query, payload, status, headers, _now(), self.api_metadata)
            except HTTPError as exc:
                headers = {str(k): str(v)[:512] for k, v in exc.headers.items() if str(k).lower() in {"retry-after", "x-ratelimit-limit", "x-ratelimit-remaining", "x-ratelimit-reset"}}
                if exc.code in _RETRYABLE_STATUSES and attempt < self.retry_count:
                    self._wait(attempt, headers)
                    continue
                raise ArcticShiftHTTPError(exc.code, url) from exc
            except (TimeoutError, URLError, OSError) as exc:
                if attempt < self.retry_count:
                    self._wait(attempt, {})
                    continue
                raise ArcticShiftTransportError(f"Arctic Shift request failed: {url}") from exc
        raise ArcticShiftTransportError("Arctic Shift retry loop exhausted unexpectedly")

    def _wait(self, attempt: int, headers: Mapping[str, str]) -> None:
        retry_after = headers.get("Retry-After")
        try:
            delay = min(60.0, max(0.0, float(retry_after))) if retry_after else self.retry_backoff * (2**attempt)
        except ValueError:
            delay = self.retry_backoff * (2**attempt)
        logger.debug("Retrying Arctic Shift request in %.2fs", delay)
        self._sleep(delay)

    def _search(self, kind: str, params: Mapping[str, Any]) -> ArcticShiftPage:
        endpoint = f"/api/{kind}/search"
        response = self._get(endpoint, params)
        data = response.raw_payload.get("data", []) if isinstance(response.raw_payload, dict) else []
        if not isinstance(data, list):
            data = []
        items = tuple(ArcticShiftItem(kind, item, response) for item in data if isinstance(item, dict))
        next_params: dict[str, Any] | None = None
        limit = params.get("limit")
        if isinstance(limit, int) and len(items) >= limit and items and items[-1].payload.get("created_utc") is not None:
            cursor_key = "after" if params.get("sort", "desc") == "asc" else "before"
            next_params = dict(params)
            next_params.pop("after", None)
            next_params.pop("before", None)
            next_params[cursor_key] = items[-1].payload["created_utc"]
        return ArcticShiftPage(kind, items, response, next_params)

    def search_posts(self, query: str | None = None, **filters: Any) -> ArcticShiftPage:
        return self._search("posts", self._search_params(query, filters))

    def search_comments(self, query: str | None = None, **filters: Any) -> ArcticShiftPage:
        return self._search("comments", self._search_params(query, filters))

    @staticmethod
    def _search_params(query: str | None, filters: Mapping[str, Any]) -> dict[str, Any]:
        params = dict(filters)
        response_format = params.get("format")
        if response_format is not None and response_format != "json":
            raise ValueError("Sieve currently supports Arctic Shift format='json' only")
        if params.get("limit") == "auto":
            raise ValueError("Sieve does not yet implement Arctic Shift limit='auto'")
        if query is not None:
            params.setdefault("query", query)
        params.setdefault("limit", 25)
        params.setdefault("sort", "desc")
        if not 1 <= int(params["limit"]) <= 100:
            raise ValueError("search limit must be between 1 and 100")
        return params

    def iter_search(self, kind: str, *, max_pages: int = 20, **filters: Any) -> Iterator[ArcticShiftPage]:
        if kind not in {"posts", "comments"}:
            raise ValueError("kind must be 'posts' or 'comments'")
        if isinstance(max_pages, bool) or not isinstance(max_pages, int) or not 1 <= max_pages <= 100:
            raise ValueError("max_pages must be an integer between 1 and 100")
        page_number = 0
        params = self._search_params(None, filters)
        while page_number < max_pages:
            page = self._search(kind, params)
            yield page
            page_number += 1
            if not page.next_params:
                return
            params = dict(page.next_params)

    def _ids(self, kind: str, ids: list[str] | tuple[str, ...], **options: Any) -> tuple[ArcticShiftItem, ...]:
        clean = [_clean_id(value) for value in ids]
        if not clean or len(clean) > 500:
            raise ValueError("ids must contain between 1 and 500 IDs")
        params = {"ids": ",".join(clean), **options}
        response = self._get(f"/api/{kind}/ids", params)
        data = response.raw_payload.get("data", []) if isinstance(response.raw_payload, dict) else []
        return tuple(ArcticShiftItem(kind, item, response) for item in data if isinstance(item, dict))

    def get_posts(self, ids: list[str] | tuple[str, ...], **options: Any) -> tuple[ArcticShiftItem, ...]:
        return self._ids("posts", ids, **options)

    def get_comments(self, ids: list[str] | tuple[str, ...], **options: Any) -> tuple[ArcticShiftItem, ...]:
        return self._ids("comments", ids, **options)

    def get_post(self, post_id: str, **options: Any) -> ArcticShiftItem | None:
        return next(iter(self.get_posts([post_id], **options)), None)

    def get_comment(self, comment_id: str, **options: Any) -> ArcticShiftItem | None:
        return next(iter(self.get_comments([comment_id], **options)), None)

    def get_thread(self, post_id: str, **options: Any) -> ArcticShiftResponse:
        """Return the raw comment-tree response for a post."""
        params = {"link_id": _clean_id(post_id), **options}
        return self._get("/api/comments/tree", params)


__all__ = [
    "ARCTIC_SHIFT_API_METADATA", "ARCTIC_SHIFT_API_VERSION", "ARCTIC_SHIFT_BASE_URL",
    "ArcticShiftClient", "ArcticShiftError", "ArcticShiftHTTPError",
    "ArcticShiftTransportError", "ArcticShiftResponse", "ArcticShiftItem", "ArcticShiftPage",
]
