"""Small async-first Python client for Sieve's existing operation contracts.

The default backend is created lazily; applications can inject a compatible
backend for testing or for a long-lived service. The client never stores keys,
cookies, or response bodies outside the returned values.
"""
from __future__ import annotations

import inspect
import math
import random
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Protocol

from sieve.batch_extract import BatchRecord, run_batch
from sieve.public_output import safe_error, safe_diagnostic


class SieveBackend(Protocol):
    async def smart_search(self, query: str, **kwargs: Any) -> Any: ...
    async def smart_fetch(self, url: str, **kwargs: Any) -> Any: ...
    async def smart_crawl(self, url: str, **kwargs: Any) -> Any: ...
    async def extract(self, url: str, **kwargs: Any) -> Any: ...


class _ServerBackend:
    """Adapt the server's explicit operation signatures to the SDK seam."""
    def __init__(self) -> None:
        from sieve.server import MasterFetchServer
        self.server = MasterFetchServer(cache_ttl=0)

    async def smart_search(self, query: str, **kwargs: Any) -> Any:
        return await self.server.smart_search(query, **kwargs)

    async def smart_fetch(self, url: str, **kwargs: Any) -> Any:
        return await self.server.smart_fetch(url, **kwargs)

    async def smart_crawl(self, url: str, **kwargs: Any) -> Any:
        return await self.server.smart_crawl(url, **kwargs)

    async def extract(self, url: str, **kwargs: Any) -> Any:
        return await self.server.extract(url, **kwargs)

    async def close(self) -> None:
        await self.server._shutdown_close_sessions()


class SieveError(RuntimeError):
    """Stable SDK error with an operation and optional original exception."""
    def __init__(self, operation: str, message: str, *, cause: Exception | None = None,
                 category: str = "internal"):
        super().__init__(f"{operation}: {message}")
        self.operation = operation
        self.cause = cause
        self.category = category
        self.diagnostic = safe_diagnostic(category=category, route="sdk")


@dataclass(frozen=True)
class ClientLimits:
    timeout: float = 30.0
    concurrency: int = 3
    max_pages: int = 100


class SieveClient:
    """Async facade over search, fetch, crawl, extract, and batch extract."""
    def __init__(self, backend: SieveBackend | None = None, *, timeout: float = 30.0,
                 concurrency: int = 3, max_pages: int = 100, rng: random.Random | None = None):
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(float(timeout)) or timeout <= 0 or timeout > 3600:
            raise ValueError("timeout must be between 0 and 3600 seconds")
        if isinstance(concurrency, bool) or not isinstance(concurrency, int) or not 1 <= concurrency <= 32:
            raise ValueError("concurrency must be between 1 and 32")
        if isinstance(max_pages, bool) or not isinstance(max_pages, int) or not 1 <= max_pages <= 10000:
            raise ValueError("max_pages must be between 1 and 10000")
        self.limits = ClientLimits(timeout, concurrency, max_pages)
        self._backend = backend
        self._rng = rng if rng is not None else random.Random()

    async def _backend_or_default(self) -> SieveBackend:
        if self._backend is None:
            self._backend = _ServerBackend()
        return self._backend

    async def __aenter__(self) -> "SieveClient":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.close()

    async def close(self) -> None:
        """Close an injected or lazily-created backend when it supports it."""
        backend = self._backend
        self._backend = None
        if backend is not None:
            closer = getattr(backend, "close", None)
            if closer is not None:
                result = closer()
                if inspect.isawaitable(result):
                    await result

    async def _call(self, operation: str, method: str, *args: Any, **kwargs: Any) -> Any:
        try:
            from sieve.session_coherence import rng_scope
            with rng_scope(self._rng):
                result = getattr(await self._backend_or_default(), method)(*args, **kwargs)
                return await result if inspect.isawaitable(result) else result
        except Exception as exc:
            failure = safe_error(exc)
            raise SieveError(operation, failure["error"], cause=exc, category=failure["category"]) from exc

    async def search(self, query: str, *, max_results: int = 6, **kwargs: Any) -> Any:
        if not 1 <= max_results <= 100:
            raise ValueError("max_results must be between 1 and 100")
        # smart_search has no per-call timeout parameter; the backend owns its
        # bounded search-engine timeouts. Do not pass an unsupported kwarg.
        return await self._call("search", "smart_search", query, max_results=max_results,
                                **kwargs)

    async def fetch(self, url: str, **kwargs: Any) -> Any:
        kwargs.pop("timeout", None)
        return await self._call("fetch", "smart_fetch", url=url,
                                timeout=int(self.limits.timeout * 1000), **kwargs)

    async def crawl(self, url: str, *, max_pages: int | None = None, **kwargs: Any) -> Any:
        kwargs.pop("timeout", None)
        bound = self.limits.max_pages if max_pages is None else max_pages
        if not 1 <= bound <= self.limits.max_pages:
            raise ValueError(f"max_pages must be between 1 and {self.limits.max_pages}")
        return await self._call("crawl", "smart_crawl", url, max_pages=bound,
                                timeout=int(self.limits.timeout * 1000), **kwargs)

    async def extract(self, url: str, schema: dict[str, Any], **kwargs: Any) -> Any:
        if not isinstance(schema, dict):
            raise ValueError("schema must be an object")
        kwargs.pop("timeout", None)
        return await self._call("extract", "extract", url, schema=schema,
                                timeout=int(self.limits.timeout * 1000), **kwargs)

    async def batch(self, urls: list[str], schema: dict[str, Any], *,
                    checkpoint: str | None = None, resume: bool = False,
                    max_pages: int | None = None, retry_failed: bool = False,
                    migrate_v1: bool = False, checkpoint_key: str | None = None,
                    on_progress: Callable[[BatchRecord], Awaitable[None] | None] | None = None,
                    **kwargs: Any) -> list[BatchRecord]:
        """Run the shared batch engine while reusing this client's extractor."""
        # ``run_batch.max_pages`` is a slice bound and must not exceed the
        # supplied URL count. The client-wide bound still caps large batches.
        bound = min(self.limits.max_pages, len(urls)) if max_pages is None else max_pages
        if not 1 <= bound <= self.limits.max_pages:
            raise ValueError(f"max_pages must be between 1 and {self.limits.max_pages}")
        async def extractor(url: str, request_schema: dict[str, Any]) -> Any:
            return await self.extract(url, request_schema, **kwargs)
        if checkpoint_key is None and not kwargs and (self._backend is None or isinstance(self._backend, _ServerBackend)):
            checkpoint_key = "sieve-sdk-html-v1"
        try:
            return await run_batch(urls, schema, extractor=extractor,
                                   concurrency=self.limits.concurrency,
                                   timeout=self.limits.timeout, max_pages=bound,
                                   checkpoint=checkpoint, resume=resume, checkpoint_key=checkpoint_key,
                                   retry_failed=retry_failed, migrate_v1=migrate_v1, on_progress=on_progress)
        except Exception as exc:
            # Rationale: the SDK batch boundary exposes a redacted SieveError with its original cause.
            failure = safe_error(exc)
            raise SieveError("batch", failure["error"], cause=exc, category=failure["category"]) from exc

    async def maigret(self, username: str, *, allow_osint: bool = False,
                      executable: str = "maigret", timeout: float = 60.0,
                      max_output_bytes: int = 1_000_000, max_sites: int = 500,
                      tags: str | None = None, include_username: bool = False) -> Any:
        """Run the explicitly enabled, external Maigret username adapter."""
        from sieve.osint import run_maigret
        try:
            return await __import__("asyncio").to_thread(
                run_maigret, username, allow_osint=allow_osint, executable=executable,
                timeout=timeout, max_output_bytes=max_output_bytes, max_sites=max_sites,
                tags=tags, include_username=include_username)
        except Exception as exc:
            failure = safe_error(exc)
            raise SieveError("maigret", failure["error"], cause=exc, category=failure["category"]) from exc


__all__ = ["BatchRecord", "ClientLimits", "SieveBackend", "SieveClient", "SieveError"]
