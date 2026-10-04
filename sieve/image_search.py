"""Bounded image search through the documented Brave Search API.

Image search is deliberately separate from :mod:`sieve.search`: text search
continues to use its keyless engine pool, while this adapter only runs when a
user has supplied a Brave Search API key through Sieve's existing BYOK store.
No consumer-endpoint scraping or keyless fallback is provided.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import json
from typing import Any

import httpx

from sieve.byok_config import load_byok_keys
from sieve.security import validate_search_query

BRAVE_IMAGES_URL = "https://api.search.brave.com/res/v1/images/search"
DEFAULT_MAX_RESULTS = 6
MAX_RESULTS = 50
DEFAULT_TIMEOUT = 15
MAX_TIMEOUT = 60
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
SAFE_SEARCH_VALUES = frozenset({"strict", "moderate", "off"})


@dataclass(frozen=True)
class ImageResult:
    """One image result and its source/provenance URLs."""

    title: str
    thumbnail_url: str
    image_url: str
    source_url: str
    provenance_url: str

    # Short aliases make the contract convenient for callers while the JSON
    # representation keeps explicit URL names.
    @property
    def thumbnail(self) -> str:
        return self.thumbnail_url

    @property
    def image(self) -> str:
        return self.image_url

    @property
    def source(self) -> str:
        return self.source_url

    @property
    def provenance(self) -> str:
        return self.provenance_url

    def to_dict(self) -> dict[str, str]:
        return {
            "title": self.title,
            "thumbnail_url": self.thumbnail_url,
            "image_url": self.image_url,
            "source_url": self.source_url,
            "provenance_url": self.provenance_url,
        }


@dataclass
class ImageSearchResponse:
    """Stable JSON-shaped result for both successful and failed searches."""

    ok: bool
    query: str
    results: list[ImageResult] = field(default_factory=list)
    provider: str | None = None
    safe_search: str = "strict"
    error: str = ""
    category: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "query": self.query,
            "results": [result.to_dict() for result in self.results],
            "provider": self.provider,
            "safe_search": self.safe_search,
            "error": self.error,
            "category": self.category,
        }


def _failure(query: str, safe_search: str, category: str, error: str) -> ImageSearchResponse:
    return ImageSearchResponse(
        ok=False,
        query=query,
        safe_search=safe_search,
        error=error,
        category=category,
    )


def _clean_url(value: Any) -> str:
    """Return only absolute HTTP(S) URLs from untrusted provider data."""
    value = str(value or "").strip()
    try:
        from sieve.url_policy import fetch_url
        return fetch_url(value)
    except (TypeError, ValueError):
        return ""


def _parse_results(data: Any, limit: int) -> list[ImageResult]:
    if not isinstance(data, dict):
        return []
    raw_results = data.get("results")
    if not isinstance(raw_results, list):
        return []
    results: list[ImageResult] = []
    for raw in raw_results:
        if not isinstance(raw, dict):
            continue
        # Brave's documented response uses properties.url for the full image
        # and thumbnail.src for the preview. Keep conservative fallbacks for
        # compatible response fixtures.
        properties = raw.get("properties") if isinstance(raw.get("properties"), dict) else {}
        thumbnail = raw.get("thumbnail") if isinstance(raw.get("thumbnail"), dict) else {}
        image_url = _clean_url(properties.get("url") or raw.get("image_url") or raw.get("url"))
        thumbnail_url = _clean_url(thumbnail.get("src") or raw.get("thumbnail_url"))
        source_url = _clean_url(raw.get("source") or raw.get("page_url") or raw.get("source_url"))
        if not image_url or not source_url:
            continue
        # An image result without a usable thumbnail is still useful; expose
        # the full image as a bounded fallback rather than dropping it.
        thumbnail_url = thumbnail_url or image_url
        provenance_url = source_url
        title = str(raw.get("title") or "").strip()[:500]
        results.append(ImageResult(title, thumbnail_url, image_url, source_url, provenance_url))
        if len(results) >= limit:
            break
    return results


def search_images(
    query: str,
    *,
    max_results: int = DEFAULT_MAX_RESULTS,
    safe_search: str = "strict",
    timeout: int | float = DEFAULT_TIMEOUT,
) -> ImageSearchResponse:
    """Search images with Brave when a ``brave`` BYOK key is configured.

    This function always returns an :class:`ImageSearchResponse`, including
    structured ``no_provider``, ``rate_limited``, and ``timeout`` failures.
    Counts and timeout are clamped to keep one-shot CLI calls bounded.
    """
    query = str(query or "").strip()
    if not query or len(query) > 300:
        return _failure(query, safe_search, "input", "query must be 1-300 characters")
    safe_search = str(safe_search or "strict").lower().strip()
    if safe_search not in SAFE_SEARCH_VALUES:
        return _failure(query, safe_search, "input", "safe_search must be strict, moderate, or off")
    try:
        # The security validator catches control characters and search
        # operators that could otherwise alter the provider request.
        query = validate_search_query(query)
    except Exception as exc:
        return _failure(query, safe_search, "input", str(exc))
    try:
        from sieve.security import bounded_number
        # Reject bool/NaN/inf/non-numbers; clamp over-range values as before.
        count = min(max(bounded_number(max_results, name="max_results",
                                       minimum=1, integer=True), 1), MAX_RESULTS)
        request_timeout = min(max(bounded_number(timeout, name="timeout",
                                                 minimum=1.0), 1.0), MAX_TIMEOUT)
    except (TypeError, ValueError):
        return _failure(query, safe_search, "input",
                        "max_results and timeout must be finite numbers")

    keys = load_byok_keys().get("brave", [])
    if not keys:
        return _failure(query, safe_search, "no_provider", "no Brave image-search key is configured")

    last_rate_limited = False
    for key in keys:
        # Stream the provider response with a hard byte cap (issue #91): the
        # body must never be buffered in full before the size limit applies.
        body: bytes | None = None
        try:
            with httpx.stream(
                "GET", BRAVE_IMAGES_URL,
                params={"q": query, "count": count, "safesearch": safe_search},
                headers={"Accept": "application/json", "X-Subscription-Token": key},
                timeout=request_timeout,
            ) as response:
                if response.status_code == 429:
                    last_rate_limited = True
                    continue
                if response.status_code in {401, 403}:
                    # A second configured key may be valid; try it before reporting.
                    continue
                if response.status_code != 200:
                    return _failure(query, safe_search, "provider_error", f"Brave image search returned HTTP {response.status_code}")
                chunks: list[bytes] = []
                total = 0
                oversized = False
                for chunk in response.iter_bytes(64 * 1024):
                    total += len(chunk)
                    if total > MAX_RESPONSE_BYTES:
                        oversized = True
                        break
                    chunks.append(chunk)
                if oversized:
                    return _failure(query, safe_search, "provider_error",
                                    f"Brave image search response exceeds {MAX_RESPONSE_BYTES // (1024 * 1024)} MiB")
                body = b"".join(chunks)
        except httpx.TimeoutException:
            return _failure(query, safe_search, "timeout", "Brave image search timed out")
        except httpx.HTTPError as exc:
            return _failure(query, safe_search, "network", f"Brave image search failed: {type(exc).__name__}")

        try:
            data = json.loads(body.decode("utf-8", errors="replace"))
        except (ValueError, TypeError):
            return _failure(query, safe_search, "provider_error", "Brave image search returned invalid JSON")
        return ImageSearchResponse(
            ok=True,
            query=query,
            results=_parse_results(data, count),
            provider="brave",
            safe_search=safe_search,
        )

    if last_rate_limited:
        return _failure(query, safe_search, "rate_limited", "Brave image search rate limit exceeded")
    return _failure(query, safe_search, "authentication", "all configured Brave image-search keys were rejected")


__all__ = ["ImageResult", "ImageSearchResponse", "search_images"]
