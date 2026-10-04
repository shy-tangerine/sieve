"""AutoThrottle — adaptive per-domain crawl delay.

Reconstructed clean-room against Sieve's own tests (tests/test_autothrottle.py)
and the crawl call site (sieve/crawl.py); no upstream-derived code.

Pure stdlib, thread-safe. One :class:`AutoThrottle` instance learns a delay
per domain from observed responses:

* **Healthy responses** pull the delay toward the server's observed latency
  (halfway between the current delay and the latency, never below the
  latency) so fast sites are crawled faster and slow sites more politely.
* **Blocking responses** (429/403) back the delay off: the server's
  ``Retry-After`` wins when provided, otherwise the current delay doubles.
  The penalty only decays back once the site behaves again.

:class:`parse_retry_after` reads a ``Retry-After`` header value in either
seconds or HTTP-date form.
"""

from __future__ import annotations

import logging
import math
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from threading import Lock
from typing import Dict, Mapping

__all__ = ["AutoThrottle", "parse_retry_after"]

logger = logging.getLogger(__name__)

# Status codes that mean "back off this domain".
_BLOCK_STATUS_CODES = frozenset({429, 403})


def parse_retry_after(headers: Mapping[str, str]) -> float | None:
    """Return seconds to wait from a ``Retry-After`` header, or ``None``.

    Accepts the two RFC forms: delay-seconds (``"120"``) and HTTP-date.
    Header lookup is case-insensitive. Unreadable values return ``None``
    (best-effort: a garbled header never raises into the crawl loop).
    """
    value = ""
    for key in headers:
        if key.lower() == "retry-after":
            value = str(headers[key]).strip()
            break
    if not value:
        return None

    try:
        return max(float(value), 0.0)
    except ValueError:
        pass

    try:
        target = parsedate_to_datetime(value)
        if target.tzinfo is None:
            target = target.replace(tzinfo=timezone.utc)
        seconds = (target - datetime.now(timezone.utc)).total_seconds()
        return max(seconds, 0.0)
    except (TypeError, ValueError, OverflowError):
        logger.debug("Ignoring unreadable Retry-After header")
        return None


class AutoThrottle:
    """Learns a per-domain download delay from observed responses.

    Args:
        min_delay: Floor for any learned delay (and the seed for unseen
            domains).
        max_delay: Ceiling; a back-off never exceeds this.
        backoff_factor: Multiplier applied to the current delay on a block
            when no ``Retry-After`` was provided.
        block_backoff: When False, blocking responses are treated like
            healthy ones (delay tracks latency only).

    Raises:
        ValueError: on non-finite numbers, negative delays,
            ``max_delay < min_delay``, or a non-positive backoff factor.
    """

    def __init__(
        self,
        min_delay: float = 2.0,
        max_delay: float = 30.0,
        backoff_factor: float = 2.0,
        block_backoff: bool = True,
    ) -> None:
        values = (min_delay, max_delay, backoff_factor)
        if any(
            isinstance(v, bool) or not isinstance(v, (int, float))
            or not math.isfinite(float(v))
            for v in values
        ):
            raise ValueError("delay/backoff values must be finite numbers")
        if min_delay < 0 or max_delay < 0:
            raise ValueError("min_delay and max_delay must be non-negative")
        if max_delay < min_delay:
            raise ValueError("max_delay cannot be lower than min_delay")
        if backoff_factor <= 0:
            raise ValueError("backoff_factor must be greater than 0")

        self.min_delay = float(min_delay)
        self.max_delay = float(max_delay)
        self.backoff_factor = float(backoff_factor)
        self.block_backoff = bool(block_backoff)
        self._delays: Dict[str, float] = {}
        self._lock = Lock()

    def get_delay(self, domain: str) -> float:
        """Return the current delay for ``domain``, seeding unseen domains
        at ``min_delay``."""
        with self._lock:
            if domain not in self._delays:
                self._delays[domain] = self.min_delay
            return self._delays[domain]

    def observe(
        self,
        domain: str,
        latency: float,
        status: int,
        retry_after: float | None = None,
    ) -> None:
        """Feed a finished request back into the throttle.

        Args:
            domain: The domain the request belongs to.
            latency: Request duration in seconds (negative values clamp to 0).
            status: HTTP status code.
            retry_after: Seconds the server asked us to wait, or ``None``.
        """
        blocked = status in _BLOCK_STATUS_CODES
        with self._lock:
            current = self._delays.get(domain, self.min_delay)
            target = max(float(latency), 0.0)
            # Converge halfway toward observed latency, never below it.
            new_delay = max((current + target) / 2.0, target)

            if blocked and self.block_backoff:
                penalty = (
                    retry_after
                    if retry_after is not None
                    else current * self.backoff_factor
                )
                new_delay = max(new_delay, float(penalty), current)

            new_delay = max(self.min_delay, min(new_delay, self.max_delay))
            self._delays[domain] = new_delay
            logger.debug(
                "AutoThrottle (%s): latency=%.2fs status=%d delay %.2fs -> %.2fs",
                domain, latency, status, current, new_delay,
            )

    def reset(self) -> None:
        """Drop every learned delay."""
        with self._lock:
            self._delays.clear()
