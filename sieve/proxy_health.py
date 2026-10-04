"""Proxy health-score, quarantine, and weighted random rotation.

Scout 5 community algorithm (r/PrivatePackets) ported to pure stdlib.

Every proxy starts at score 100.  Observations adjust the score:

    200 OK        →  +1  (cap 150)
    429 / 403     →  -20
    5xx / timeout →  -10

When a proxy's score drops below 40 it is moved to a quarantine pool for
``quarantine_minutes`` minutes.  Quarantined proxies are excluded from
selection; once the quarantine window expires they become re-eligible and
their score is restored to 40 (the quarantine threshold) so they get a
chance to climb back up.

Healthy proxies are selected via *weighted random* — weight == score —
so higher-scoring proxies are chosen more often without starving the
rest.  This is the egress-layer complement to AutoThrottle (pacing):
AutoThrottle decides *how fast* to send, this module decides *which route*.

Thread-safe via a single ``threading.Lock`` guarding all mutable state.
No file I/O, no network, no third-party dependencies.
"""

from __future__ import annotations

import random
import threading
import time
from dataclasses import dataclass

__all__ = ["ProxyHealth", "ProxyPool"]

#: Initial / default score for every proxy.
DEFAULT_SCORE = 100
#: Maximum score a proxy can reach.
MAX_SCORE = 150
#: Minimum score to remain in the healthy pool.
HEALTHY_FLOOR = 40
#: Penalty for rate-limit / captcha responses (429, 403).
PENALTY_RATE_LIMITED = -20
#: Penalty for server errors (5xx) and timeouts.
PENALTY_SERVER_ERROR = -10
#: Reward for a successful response (200 OK).
REWARD_OK = +1

# Status codes that signal rate-limiting / anti-bot captcha walls.
_RATE_LIMITED_CODES = {429, 403}


@dataclass
class _Quarantine:
    """Metadata for a quarantined proxy."""

    proxy: str
    score: int
    released_at: float  # epoch seconds after which the proxy may re-enter


class ProxyHealth:
    """Thread-safe proxy health tracker with quarantine and weighted selection.

    Parameters
    ----------
    proxies
        Optional iterable of ``"host:port"`` strings (may include a scheme
        prefix such as ``"http://host:port"``).  Each starts at score 100.
    quarantine_minutes
        How long a proxy stays in quarantine before becoming re-eligible.
    """

    def __init__(
        self,
        proxies: list[str] | None = None,
        *,
        quarantine_minutes: float = 5,
    ) -> None:
        self._lock = threading.Lock()
        self._scores: dict[str, int] = {}
        self._quarantine: dict[str, _Quarantine] = {}
        self._quarantine_minutes = quarantine_minutes

        if proxies:
            for p in proxies:
                self.register(p)

    # ------------------------------------------------------------------ #
    # registration
    # ------------------------------------------------------------------ #

    def register(self, proxy: str) -> None:
        """Add *proxy* to the pool if not already present."""
        with self._lock:
            if proxy not in self._scores and proxy not in self._quarantine:
                self._scores[proxy] = DEFAULT_SCORE

    # ------------------------------------------------------------------ #
    # observation
    # ------------------------------------------------------------------ #

    def report(
        self,
        proxy: str,
        status_code: int | None,
        *,
        ok: bool = True,
    ) -> None:
        """Feed an observation back into the tracker.

        Parameters
        ----------
        proxy
            The ``"host:port"`` string.
        status_code
            HTTP status code from the response, or ``None`` if the request
            timed out / errored before a response was received.
        ok
            ``True`` for a successful request body.  When ``True`` the status
            is expected to be 200.  When ``False`` *and* ``status_code`` is
            ``None`` the request is treated as a timeout / connection error.
        """
        with self._lock:
            # If the proxy is currently quarantined, pull it back into the
            # score dict at its quarantined score so we can adjust it.
            if proxy in self._quarantine:
                q = self._quarantine.pop(proxy)
                self._scores[proxy] = q.score

            # Ensure the proxy is tracked (auto-register on first report).
            if proxy not in self._scores:
                self._scores[proxy] = DEFAULT_SCORE

            score = self._scores[proxy]

            if ok and status_code is not None and 200 <= status_code < 300:
                score = min(score + REWARD_OK, MAX_SCORE)
            elif status_code in _RATE_LIMITED_CODES:
                score += PENALTY_RATE_LIMITED
            elif status_code is not None and 500 <= status_code < 600:
                score += PENALTY_SERVER_ERROR
            elif not ok and status_code is None:
                # Timeout / connection error.
                score += PENALTY_SERVER_ERROR
            else:
                # Other 2xx/3xx/4xx codes: treat as neutral-ok, small reward.
                if ok:
                    score = min(score + REWARD_OK, MAX_SCORE)

            self._scores[proxy] = score

            # Quarantine check.
            if score < HEALTHY_FLOOR:
                self._quarantine[proxy] = _Quarantine(
                    proxy=proxy,
                    score=score,
                    released_at=time.monotonic() + self._quarantine_minutes * 60,
                )
                self._scores.pop(proxy, None)

    # ------------------------------------------------------------------ #
    # queries
    # ------------------------------------------------------------------ #

    def healthy(self) -> list[str]:
        """Return proxies eligible for selection right now.

        Proxies whose quarantine window has expired are restored to score
        ``40`` (the healthy floor) so they get a fair chance to recover.
        """
        with self._lock:
            now = time.monotonic()
            # Expire any quarantines whose timer has elapsed.
            for proxy in list(self._quarantine):
                q = self._quarantine[proxy]
                if q.released_at <= now:
                    self._quarantine.pop(proxy)
                    self._scores[proxy] = HEALTHY_FLOOR
            # Everything still in _scores is healthy by construction.
            return list(self._scores)

    def pick(self) -> str | None:
        """Weighted-random selection among healthy proxies.

        Weight = current score.  Returns ``None`` if no healthy proxies.
        """
        with self._lock:
            # Defer to healthy() for expiry logic — but it also acquires the
            # lock, so inline the expiry check to avoid deadlock.
            now = time.monotonic()
            for proxy in list(self._quarantine):
                q = self._quarantine[proxy]
                if q.released_at <= now:
                    self._quarantine.pop(proxy)
                    self._scores[proxy] = HEALTHY_FLOOR

            if not self._scores:
                return None

            proxies = list(self._scores)
            weights = [self._scores[p] for p in proxies]
            return random.choices(proxies, weights=weights, k=1)[0]

    def scores(self) -> dict[str, int]:
        """Snapshot of current scores (healthy proxies only)."""
        with self._lock:
            return dict(self._scores)

    def quarantined(self) -> list[str]:
        """Return currently-quarantined proxy strings."""
        with self._lock:
            return list(self._quarantine)

    # ------------------------------------------------------------------ #
    # maintenance
    # ------------------------------------------------------------------ #

    def reset(self) -> None:
        """Wipe all state — scores, quarantines, everything."""
        with self._lock:
            self._scores.clear()
            self._quarantine.clear()


class ProxyPool:
    """Convenience wrapper around ProxyHealth that fails closed when all routes are quarantined.

    ``next()`` uses weighted-random selection via :meth:`ProxyHealth.pick` and
    returns ``None`` when no healthy proxy is currently eligible.

    Parameters mirror :class:`ProxyHealth`.
    """

    def __init__(
        self,
        proxies: list[str] | None = None,
        *,
        quarantine_minutes: float = 5,
    ) -> None:
        self._health = ProxyHealth(proxies, quarantine_minutes=quarantine_minutes)
        self._rr_lock = threading.Lock()
        self._rr_index = 0
        self._all = list(proxies) if proxies else []

    def register(self, proxy: str) -> None:
        """Add *proxy* to both the health tracker and the round-robin list."""
        self._health.register(proxy)
        with self._rr_lock:
            if proxy not in self._all:
                self._all.append(proxy)

    def next(self) -> str | None:
        """Return a healthy proxy, or ``None`` when none is eligible."""
        return self._health.pick()

    def report(
        self,
        proxy: str,
        status_code: int | None,
        *,
        ok: bool = True,
    ) -> None:
        """Delegate an observation to the underlying :class:`ProxyHealth`."""
        self._health.report(proxy, status_code, ok=ok)

    # Pass-through introspection helpers for convenience.
    def healthy(self) -> list[str]:
        return self._health.healthy()

    def scores(self) -> dict[str, int]:
        return self._health.scores()

    def quarantined(self) -> list[str]:
        return self._health.quarantined()

    def reset(self) -> None:
        self._health.reset()
        with self._rr_lock:
            self._rr_index = 0
            self._all.clear()
