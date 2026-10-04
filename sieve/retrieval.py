"""Small retrieval seam shared by HTTP, browser, and test adapters.

The retrieval policy works with one operation: ``fetch``.  Transport details
live in adapters, which keeps request orchestration and result translation
local to the retrieval tier while making the seam directly testable.
"""
from __future__ import annotations

import inspect
from typing import Any, Mapping, Protocol


class RetrievalAdapter(Protocol):
    """The only transport fact retrieval policy needs to know."""

    async def fetch(self, url: str, **options: Any) -> Any:
        """Fetch one URL and return the adapter's native page response."""


class HTTPRetrievalAdapter:
    """Production adapter for the bounded HTTP session."""

    def __init__(self, session: Any) -> None:
        self._session = session

    async def fetch(self, url: str, **options: Any) -> Any:
        return await self._session.get(url, **options)


class BrowserRetrievalAdapter:
    """Production adapter for an already configured browser session."""

    def __init__(self, session: Any) -> None:
        self._session = session

    async def fetch(self, url: str, **options: Any) -> Any:
        return await self._session.fetch(url, **options)


class InMemoryRetrievalAdapter:
    """Deterministic adapter for interface-level retrieval tests."""

    def __init__(self, responses: Mapping[str, Any] | None = None) -> None:
        self.responses = dict(responses or {})
        self.requests: list[tuple[str, dict[str, Any]]] = []

    async def fetch(self, url: str, **options: Any) -> Any:
        self.requests.append((url, dict(options)))
        response = self.responses.get(url)
        if isinstance(response, BaseException):
            raise response
        if callable(response):
            value = response(url, **options)
            return await value if inspect.isawaitable(value) else value
        if response is None:
            raise KeyError(f"No in-memory response configured for {url}")
        return response
