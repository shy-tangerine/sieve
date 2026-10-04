"""Browser session lifecycle and auto-session state.

The browser engine in :mod:`sieve.browser` owns navigation and page pooling.
This module owns the other half of the concern: identifying sessions,
serializing creation, reusing warm sessions, expiring idle sessions, and
closing subprocess resources.  Keeping that state here prevents the MCP
facade from becoming the owner of browser lifecycle details.

The registry deliberately returns small records and browser objects rather
than transport-specific response models.  The server facade can therefore
keep its public Pydantic contract while this module remains usable by the
CLI, retrieval adapters, and focused tests.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from time import time as now
from typing import Any, Callable, Dict, Literal, Optional, Sequence
from uuid import uuid4

from sieve.security import validate_css_selector, validate_headers, validate_proxy

SessionType = Literal["dynamic", "stealthy"]
BrowserFactory = Callable[..., Any]

_AUTO_SESSION_TYPES: tuple[SessionType, ...] = ("dynamic", "stealthy")


@dataclass
class SessionEntry:
    """A browser object and the metadata needed by the registry."""

    session: Any
    session_type: SessionType
    created_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    _alive: bool = True


@dataclass(frozen=True)
class OpenedSession:
    """The stable result of registering and starting a browser session."""

    session_id: str
    entry: SessionEntry


class BrowserSessionRegistry:
    """Own persistent browser sessions and their lifecycle state.

    The registry is intentionally independent of MCP/Pydantic models.  Its
    interface is the small set of operations callers need:

    * ``open`` / ``close`` for explicit sessions;
    * ``get`` for a validated live session;
    * ``ensure_auto_session`` for a shared warm browser;
    * ``prewarm`` and ``shutdown`` for process lifecycle.

    ``browser_factory`` is an internal test seam.  Production callers leave it
    unset and the registry lazily constructs Sieve's concrete browser classes.
    """

    def __init__(
        self,
        *,
        browser_available: Callable[[], bool],
        browser_error: Callable[[], Optional[str]],
        idle_timeout: int,
        idle_check_interval: int,
        browser_factory: Optional[BrowserFactory] = None,
        logger_: Optional[logging.Logger] = None,
    ) -> None:
        self._sessions: Dict[str, SessionEntry] = {}
        self._sessions_lock = asyncio.Lock()
        self._auto_session_lock = asyncio.Lock()
        self._auto_ids: dict[SessionType, Optional[str]] = {
            "dynamic": None,
            "stealthy": None,
        }
        self._auto_last_used: dict[SessionType, float] = {
            "dynamic": 0.0,
            "stealthy": 0.0,
        }
        self._idle_monitor_task: Optional[asyncio.Task] = None
        self._browser_available = browser_available
        self._browser_error = browser_error
        self._idle_timeout = idle_timeout
        self._idle_check_interval = idle_check_interval
        self._browser_factory = browser_factory
        self._logger = logger_ or logging.getLogger("sieve.browser_sessions")

    @property
    def sessions(self) -> Dict[str, SessionEntry]:
        """The live-session map, exposed for compatibility diagnostics."""

        return self._sessions

    @property
    def sessions_lock(self) -> asyncio.Lock:
        """Lock guarding :attr:`sessions`."""

        return self._sessions_lock

    def _require_browser(self, message: str) -> None:
        if self._browser_available():
            return
        detail = self._browser_error() or "patchright not importable"
        raise RuntimeError(
            f"{message}: {detail}. Install with: uv sync"
        )

    def _require_auto_browser(self) -> None:
        if self._browser_available():
            return
        detail = self._browser_error() or "patchright not importable"
        raise RuntimeError(
            f"Browser unavailable: {detail}. Install browser deps: uv sync --all-extras "
            "(or pip install playwright patchright)."
        )

    @staticmethod
    def _session_alive(entry: SessionEntry) -> bool:
        return bool(getattr(entry.session, "_is_alive", False))

    async def get(
        self,
        session_id: str,
        expected_type: Optional[SessionType] = None,
    ) -> SessionEntry:
        """Return a live session, optionally enforcing its browser type.

        The lock only protects lookup and validation.  Callers may use the
        returned entry after releasing it, but must not close that session
        concurrently while doing so.
        """

        async with self._sessions_lock:
            entry = self._sessions.get(session_id)
            if entry is None:
                raise ValueError(
                    f"Session '{session_id}' not found. Use list_sessions to see active sessions."
                )
            if not self._session_alive(entry):
                raise ValueError(
                    f"Session '{session_id}' is no longer alive. Open a new session."
                )
            if expected_type is not None and entry.session_type != expected_type:
                raise ValueError(
                    f"Session '{session_id}' is a '{entry.session_type}' session, but this tool "
                    f"requires a '{expected_type}' session. Use the matching fetch tool for your "
                    "session type."
                )
            return entry

    def _make_browser(self, session_type: SessionType, **kwargs: Any) -> Any:
        if self._browser_factory is not None:
            return self._browser_factory(session_type, **kwargs)
        from sieve.browser import DynamicBrowser, StealthyBrowser

        if session_type == "stealthy":
            return StealthyBrowser(**kwargs)
        return DynamicBrowser(**kwargs)

    async def open(
        self,
        session_type: SessionType,
        *,
        session_id: Optional[str] = None,
        headless: bool = True,
        google_search: bool = True,
        real_chrome: bool = False,
        wait: int | float = 0,
        proxy: Optional[str | Dict[str, str]] = None,
        timezone_id: str | None = None,
        locale: str | None = None,
        extra_headers: Optional[Dict[str, str]] = None,
        useragent: Optional[str] = None,
        cdp_url: Optional[str] = None,
        timeout: int | float = 30000,
        disable_resources: bool = False,
        wait_selector: Optional[str] = None,
        cookies: Sequence[Any] | None = None,
        network_idle: bool = False,
        wait_selector_state: Any = "attached",
        max_pages: int = 5,
        hide_canvas: bool = False,
        block_webrtc: bool = False,
        allow_webgl: bool = True,
        solve_cloudflare: bool = False,
        additional_args: Optional[Dict] = None,
    ) -> OpenedSession:
        """Validate, construct, register, and start a browser session."""

        self._require_browser("Browser sessions require browser deps which are unavailable")
        sid = session_id or uuid4().hex[:12]
        async with self._sessions_lock:
            if sid in self._sessions:
                raise ValueError(
                    f"Session '{sid}' already exists. Use a different ID or close "
                    "the existing one."
                )

        # Keep validation at the lifecycle seam so every caller gets the same
        # safety contract, including callers that do not enter through MCP.
        validate_proxy(proxy)
        validate_headers(extra_headers)
        validate_css_selector(wait_selector)

        common_kwargs: Dict[str, Any] = {
            "wait": wait,
            "proxy": proxy,
            "locale": locale,
            "timeout": timeout,
            "cookies": cookies,
            "cdp_url": cdp_url,
            "headless": headless,
            "block_ads": True,
            "max_pages": max_pages,
            "useragent": useragent,
            "timezone_id": timezone_id,
            "real_chrome": real_chrome,
            "network_idle": network_idle,
            "wait_selector": wait_selector,
            "google_search": google_search,
            "extra_headers": extra_headers,
            "disable_resources": disable_resources,
            "wait_selector_state": wait_selector_state,
        }
        if session_type == "stealthy":
            common_kwargs.update(
                hide_canvas=hide_canvas,
                block_webrtc=block_webrtc,
                allow_webgl=allow_webgl,
                solve_cloudflare=solve_cloudflare,
                additional_args=additional_args,
            )
        session = self._make_browser(session_type, **common_kwargs)
        entry = SessionEntry(session=session, session_type=session_type)
        async with self._sessions_lock:
            self._sessions[sid] = entry
        try:
            await session.start()
        except Exception:
            async with self._sessions_lock:
                entry._alive = False
                self._sessions.pop(sid, None)
            raise
        return OpenedSession(session_id=sid, entry=entry)

    async def close(self, session_id: str) -> SessionEntry:
        """Remove and close a session, returning its former registry entry."""

        async with self._sessions_lock:
            entry = self._sessions.pop(session_id, None)
        if entry is None:
            raise ValueError(f"Session '{session_id}' not found.")
        await entry.session.close()
        entry._alive = False
        return entry

    def _auto_is_alive(self, session_type: SessionType) -> Optional[str]:
        session_id = self._auto_ids[session_type]
        if not session_id:
            return None
        entry = self._sessions.get(session_id)
        if entry is not None and self._session_alive(entry):
            return session_id
        return None

    async def ensure_auto_session(self, session_type: SessionType) -> str:
        """Return one shared, warm session for *session_type*.

        Creation is serialized so startup prewarm and a first request cannot
        launch two browser processes.  The final locked check also cleans up an
        orphan if a caller injected an out-of-band creator.
        """

        self._require_auto_browser()
        async with self._sessions_lock:
            existing_id = self._auto_is_alive(session_type)
            if existing_id:
                self._auto_last_used[session_type] = now()
                self.ensure_idle_monitor()
                return existing_id

        async with self._auto_session_lock:
            async with self._sessions_lock:
                existing_id = self._auto_is_alive(session_type)
                if existing_id:
                    self._auto_last_used[session_type] = now()
                    self.ensure_idle_monitor()
                    return existing_id

            opened = await self.open(session_type, headless=True)
            orphan: Optional[SessionEntry] = None
            async with self._sessions_lock:
                existing_id = self._auto_is_alive(session_type)
                if existing_id:
                    orphan = self._sessions.pop(opened.session_id, None)
                    result_id = existing_id
                else:
                    self._auto_ids[session_type] = opened.session_id
                    result_id = opened.session_id
                self._auto_last_used[session_type] = now()

        if orphan is not None:
            try:
                await orphan.session.close()
            except Exception:
                pass
        self.ensure_idle_monitor()
        return result_id

    async def prewarm(self) -> None:
        """Best-effort warm the shared stealthy browser in the background."""

        async def _warm() -> None:
            def _check_and_import() -> bool:
                from sieve.browser import check_browser_available

                return check_browser_available()

            # Import patchright off the event loop; it can take seconds on a
            # cold install and must not delay an MCP initialize handshake.
            if not await asyncio.to_thread(_check_and_import):
                return
            await self.ensure_auto_session("stealthy")

        try:
            await asyncio.wait_for(_warm(), timeout=30.0)
            self._logger.debug("Stealthy browser warmed at startup")
        except BaseException as exc:
            self._logger.debug(
                "Startup warm-up failed/skipped (will launch on first stealthy fetch): %r",
                exc,
            )

    def ensure_idle_monitor(self) -> None:
        """Start the idle monitor once when auto-session eviction is enabled."""

        if self._idle_timeout == 0:
            return
        if self._idle_monitor_task is None or self._idle_monitor_task.done():
            self._idle_monitor_task = asyncio.create_task(self._start_idle_monitor())

    async def _stale_auto_sessions(self, timestamp: float) -> list[tuple[SessionType, str]]:
        async with self._sessions_lock:
            stale: list[tuple[SessionType, str]] = []
            for session_type in _AUTO_SESSION_TYPES:
                session_id = self._auto_ids[session_type]
                if (
                    session_id
                    and timestamp - self._auto_last_used[session_type] > self._idle_timeout
                ):
                    stale.append((session_type, session_id))
                    self._auto_ids[session_type] = None
            return stale

    async def _discard_after_close_failure(
        self,
        session_type: SessionType,
        session_id: str,
        exc: Exception,
    ) -> None:
        self._logger.warning(
            "Idle monitor failed to close %s session %s: %s",
            session_type,
            session_id,
            exc,
        )
        async with self._sessions_lock:
            entry = self._sessions.pop(session_id, None)
            if entry:
                entry._alive = False

    async def _start_idle_monitor(self) -> None:
        """Close auto sessions after the configured period of inactivity."""

        while True:
            await asyncio.sleep(self._idle_check_interval)
            try:
                if self._idle_timeout == 0:
                    continue
                stale = await self._stale_auto_sessions(now())
                for session_type, session_id in stale:
                    try:
                        await self.close(session_id)
                    except Exception as exc:
                        await self._discard_after_close_failure(
                            session_type, session_id, exc
                        )
            except asyncio.CancelledError:
                raise
            except Exception:
                self._logger.exception(
                    "Idle monitor check failed, will retry on next cycle"
                )

    async def shutdown(self) -> None:
        """Close all sessions and drain subprocess transports best-effort."""

        monitor = self._idle_monitor_task
        self._idle_monitor_task = None
        if monitor is not None and not monitor.done():
            monitor.cancel()
            try:
                await monitor
            except BaseException:
                pass

        async with self._sessions_lock:
            entries = list(self._sessions.items())
            self._sessions.clear()
            for session_type in _AUTO_SESSION_TYPES:
                self._auto_ids[session_type] = None
        for _session_id, entry in entries:
            try:
                await entry.session.close()
            except (KeyboardInterrupt, SystemExit):
                raise
            except BaseException:
                pass
            entry._alive = False

        # Patchright's subprocess transport schedules connection_lost callbacks
        # on the current loop. Drain them before the loop is torn down.
        try:
            await asyncio.sleep(0.15)
        except (KeyboardInterrupt, SystemExit):
            raise
        except BaseException:
            pass
        try:
            self.close_all_subprocess_transports()
        except (KeyboardInterrupt, SystemExit):
            raise
        except BaseException:
            pass
        try:
            await asyncio.sleep(0.05)
        except (KeyboardInterrupt, SystemExit):
            raise
        except BaseException:
            pass

    @staticmethod
    def close_all_subprocess_transports() -> None:
        """Close lingering asyncio subprocess transports on the current loop."""

        try:
            loop = asyncio.get_event_loop()
        except RuntimeError:
            return
        try:
            transports = list(getattr(loop, "_subprocess_transports", {}).values())
        except Exception:
            transports = []
        for transport in transports:
            try:
                if not transport.is_closing():
                    transport.close()
            except BaseException:
                pass


__all__ = ["BrowserSessionRegistry", "OpenedSession", "SessionEntry", "SessionType"]
