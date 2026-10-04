"""Robots.txt compliance for Sieve.

Respects robots.txt Disallow rules with caching per domain.
Uses the canonical HTTP session so URL and redirect security checks apply.
Non-blocking — all I/O is async.
"""

import asyncio
import logging
from urllib.robotparser import RobotFileParser
from time import time

logger = logging.getLogger("master-fetch.robots")

# Cache robots.txt parsers per domain: {domain: (RobotFileParser | None, fetch_time)}
# A None parser is a cached miss (unreachable or unparseable robots.txt), kept so
# a domain without robots.txt is not re-fetched on every single URL check.
_robots_cache: dict[str, tuple[RobotFileParser | None, float]] = {}
# In-flight fetches per domain, so N concurrent checks on one domain issue one
# request instead of N. Populated and cleared under _robots_lock.
_robots_inflight: dict[str, "asyncio.Task[RobotFileParser | None]"] = {}
_robots_lock: asyncio.Lock | None = None
_ROBOTS_CACHE_TTL = 3600  # 1 hour
# Misses expire sooner than hits: an unreachable robots.txt is often transient,
# so retry within the hour without paying _FETCH_TIMEOUT on every URL.
_ROBOTS_MISS_TTL = 300  # 5 minutes
_FETCH_TIMEOUT = 10  # seconds
_MAX_ROBOTS_BYTES = 1_048_576

# Last HTTP status seen for each domain's robots.txt fetch (best-effort, for
# distinguishing no-policy from unavailable outcomes; issue #3). Populated by
# _fetch_robots_txt, read by _get_robots_parser_with_status.
_last_fetch_status: dict[str, int] = {}


def _get_robots_lock() -> asyncio.Lock:
    """Lazy-init the robots cache lock (needs running event loop)."""
    global _robots_lock
    if _robots_lock is None:
        _robots_lock = asyncio.Lock()
    return _robots_lock

DEFAULT_USER_AGENT = (
    "Sieve/13.2.1 (web research for AI agents)"
)


def _extract_netloc(url: str) -> str:
    """Extract netloc from URL. Returns '' for invalid URLs."""
    from urllib.parse import urlparse
    try:
        return urlparse(url).netloc.lower()
    except Exception:
        return ""


async def _fetch_robots_txt(domain: str) -> str | None:
    """Fetch robots.txt for a domain using primp (async, impersonated).

    Returns the raw text content or None if unreachable.
    """
    try:
        from sieve.fetcher import HTTPSession
        async with HTTPSession(stealthy_headers=False, retries=1) as sess:
            response = await sess.get(
                f"https://{domain}/robots.txt", timeout=_FETCH_TIMEOUT,
            )
            status = getattr(response, 'status', 0)
            classified = _classify_http_status(status)
            if classified is not None:
                # 404/410 = no policy; 401/403/5xx = unavailable. Record the
                # status so strict mode can distinguish them from transport
                # failures, and do not fall through to the urllib retry for
                # auth-walled policies (the wall is intentional).
                _last_fetch_status[domain] = status
                logger.debug(f"robots.txt for {domain}: HTTP {status} ({classified})")
                return None
            body = getattr(response, 'body', None)
            if body:
                if len(body) > _MAX_ROBOTS_BYTES:
                    raise ValueError("robots.txt exceeds size limit")
                _last_fetch_status.pop(domain, None)
                return body.decode(
                    getattr(response, 'encoding', None) or 'utf-8', errors='replace',
                )
    except Exception as e:
        logger.debug("Robots.txt fetch failed for %s: %s", domain, type(e).__name__)
    # Transport failures, including security rejections, must never retry
    # through an HTTP client that skips the canonical trust boundary.
    return None


async def _load_robots_parser(domain: str) -> RobotFileParser | None:
    """Fetch + parse robots.txt for one domain and record the outcome.

    Runs as a single shared task per domain. Both outcomes are cached — a None
    parser is a miss, so an unreachable robots.txt is not re-fetched per URL.
    """
    try:
        raw = await _fetch_robots_txt(domain)
        parser: RobotFileParser | None = None
        if raw is None:
            logger.debug(f"robots.txt unreachable for {domain}")
        else:
            try:
                parser = RobotFileParser()
                parser.parse(raw.splitlines())
                logger.debug(f"Fetched and parsed robots.txt for {domain}")
            except Exception as e:
                parser = None
                logger.debug(f"Failed to parse robots.txt for {domain}: {e}")

        # Timestamp after the fetch, so a slow fetch does not shorten its own TTL.
        async with _get_robots_lock():
            _robots_cache[domain] = (parser, time())
        return parser
    finally:
        async with _get_robots_lock():
            _robots_inflight.pop(domain, None)


async def _get_robots_parser(domain: str, user_agent: str = "*") -> RobotFileParser | None:
    """Fetch and parse robots.txt for a domain. Caches result.

    Returns None if robots.txt is unreachable (allow by default).
    Returns RobotFileParser if successfully fetched.
    """
    lock = _get_robots_lock()

    async with lock:
        # Check cache. Misses are cached too, under a shorter TTL.
        if domain in _robots_cache:
            parser, fetched_at = _robots_cache[domain]
            ttl = _ROBOTS_CACHE_TTL if parser is not None else _ROBOTS_MISS_TTL
            if time() - fetched_at < ttl:
                return parser
            del _robots_cache[domain]

        # Join the in-flight fetch for this domain rather than starting another.
        task = _robots_inflight.get(domain)
        if task is None:
            task = asyncio.ensure_future(_load_robots_parser(domain))
            _robots_inflight[domain] = task

    # shield: one caller being cancelled must not cancel the fetch the others
    # are waiting on.
    try:
        return await asyncio.shield(task)
    except Exception as e:
        logger.debug(f"robots.txt lookup failed for {domain}: {e}")
        return None


async def _get_robots_parser_with_status(
    domain: str, user_agent: str = "*"
) -> tuple[RobotFileParser | None, int | None]:
    """Like _get_robots_parser but also returns the last observed HTTP status.

    The status lets strict mode distinguish "no robots.txt" (404/410) from
    "policy exists but could not be read" (401/403/5xx/transport failure).
    """
    parser = await _get_robots_parser(domain, user_agent)
    return parser, _last_fetch_status.get(domain)


async def is_allowed(url: str, user_agent: str = "*") -> bool:
    """Check if a URL is allowed per robots.txt (fail-open).

    Returns True if:
    - robots.txt is unreachable (allow by default)
    - robots.txt allows this URL
    - URL is invalid (malformed)

    Returns False only if robots.txt explicitly disallows this URL.

    Callers that must fail closed when the policy is unavailable (strict
    ``respect_robots`` mode, issue #3) should use :func:`robots_policy` or
    :func:`is_allowed_strict` instead.
    """
    # Fail-open contract: only an explicit Disallow denies. Unavailable and
    # no-policy outcomes allow, matching the documented default.
    return (await robots_policy(url, user_agent)) != _DISALLOWED


# Policy outcomes for strict robots enforcement (issue #3). "unavailable"
# covers transport/DNS/timeout/parse failures AND auth-walled robots.txt
# (401/403): the site's crawling policy exists but could not be read, so
# strict mode must not treat the fetch as permitted. A 404 means the site
# publishes no policy, which conventionally allows crawling.
_ALLOWED = "allowed"
_DISALLOWED = "disallowed"
_UNAVAILABLE = "unavailable"
_NO_POLICY = "no_policy"


def _classify_http_status(status: int) -> str | None:
    """Map a robots.txt fetch HTTP status to a policy outcome.

    Returns None when the status is a normal success (200) and the body
    should be parsed normally.
    """
    if status == 404 or status == 410:
        # No robots.txt published: no policy to violate.
        return _NO_POLICY
    if status in (401, 403):
        # The policy exists but is auth-walled: unavailable, not "no policy".
        return _UNAVAILABLE
    if 500 <= status <= 599:
        return _UNAVAILABLE
    return None


async def robots_policy(url: str, user_agent: str = "*") -> str:
    """Classify a URL against the site's robots.txt policy (issue #3).

    Returns one of:
    - "allowed":     robots.txt was fetched and permits this URL (or the URL
                     is malformed — nothing to enforce against).
    - "disallowed":  robots.txt explicitly disallows this URL.
    - "no_policy":   the server answered 404/410 for robots.txt: the site
                     publishes no policy, which conventionally allows crawling.
    - "unavailable": the policy could not be determined — transport error,
                     DNS failure, timeout, auth-walled robots (401/403), 5xx,
                     or unparseable content.

    Strict callers (``respect_robots`` mode) should fail closed on
    "unavailable" rather than converting a retrieval failure into implicit
    permission.
    """
    domain = _extract_netloc(url)
    if not domain:
        # Malformed URL: nothing to enforce; the URL validator rejects it.
        return _ALLOWED

    parser, status = await _get_robots_parser_with_status(domain, user_agent)
    if parser is None:
        if status is not None:
            return _classify_http_status(status) or _UNAVAILABLE
        return _UNAVAILABLE
    try:
        return _DISALLOWED if not parser.can_fetch(user_agent, url) else _ALLOWED
    except Exception:
        return _UNAVAILABLE


async def is_allowed_strict(url: str, user_agent: str = "*") -> bool:
    """Fail-closed robots check for strict ``respect_robots`` mode (issue #3).

    True only when the policy is readable and permits the URL. Unavailable
    policies (transport/DNS/timeout failures, 401/403, 5xx, parse errors)
    deny the fetch instead of silently allowing it.
    """
    outcome = await robots_policy(url, user_agent)
    return outcome in (_ALLOWED, _NO_POLICY)



async def clear_robots_cache() -> None:
    """Clear the robots.txt cache (both hits and cached misses)."""
    lock = _get_robots_lock()
    async with lock:
        _robots_cache.clear()
        _last_fetch_status.clear()
    logger.info("Robots.txt cache cleared")
