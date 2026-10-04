"""Search / crawl mixins extracted from server.py — thin wrappers around sieve.search / sieve.crawl.

Boundary validation (#128): caller-supplied bounds are checked with shared
primitives from ``sieve.security`` *before* the heavyweight ``sieve.search`` /
``sieve.crawl`` implementation modules are imported or executed. Booleans,
NaN/inf, out-of-range and oversize values are rejected with stable
``category="validation"`` responses, and no backend work is performed.
"""
from __future__ import annotations
from typing import Optional, List, TYPE_CHECKING

if TYPE_CHECKING:
    from sieve.crawl import CrawlResponseModel as CrawlResponseModel

from sieve.security import bounded_number, validate_search_query, validate_url
from sieve.security import SecurityError

# Shared option bounds (issue #128). Numeric ranges mirror the downstream
# semantic validators in sieve.search / sieve.crawl so pre-import rejection
# and post-import validation cannot disagree.
_SEARCH_MAX_RESULTS_MAX = 100
_SEARCH_PAGE_MAX = 10
_CACHE_TTL_MAX = 604800  # 7 days
_ENGINES_MAX = 12
_DOMAIN_LIST_MAX = 20
_PATH_LIST_MAX = 50
_CRAWL_MAX_PAGES_MAX = 200
_CRAWL_MAX_DEPTH_MAX = 10
_CRAWL_CONCURRENCY_MAX = 16
_CRAWL_TIMEOUT_MAX_MS = 120_000
_CRAWL_DEADLINE_MAX_MS = 3_600_000
_CRAWL_CHARS_PER_MAX = 1_000_000
_CRAWL_CHARS_TOTAL_MAX = 10_000_000
_THROTTLE_DELAY_MAX_S = 60.0
_STR_PARAM_MAX_LEN = 253
_URL_PARAM_MAX_LEN = 2048


def _bounded_str(value, *, name: str, max_len: int = _STR_PARAM_MAX_LEN) -> Optional[str]:
    """Type/size check for an optional string option before heavy imports."""
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{name} must be a string, not {type(value).__name__}")
    if len(value) > max_len:
        raise ValueError(f"{name} must be at most {max_len} characters")
    return value


def _bounded_str_list(value, *, name: str, max_items: int,
                      max_len: int = _STR_PARAM_MAX_LEN) -> Optional[List[str]]:
    """Type/size check for an optional list-of-strings option before heavy imports."""
    if value is None:
        return None
    if isinstance(value, str) or not isinstance(value, (list, tuple)):
        raise ValueError(f"{name} must be a list of strings")
    if len(value) > max_items:
        raise ValueError(f"{name} must contain at most {max_items} entries")
    cleaned: List[str] = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise ValueError(f"{name} entries must be non-empty strings")
        if len(item) > max_len:
            raise ValueError(f"{name} entries must be at most {max_len} characters")
        cleaned.append(item)
    return cleaned


def _validation_search_error(query: str, message: str):
    from sieve.search import SearchResponseModel
    return SearchResponseModel(query=query if isinstance(query, str) else "",
                               results=[], total_results=0, duration_ms=0,
                               category="validation", error=message)


def _validation_crawl_error(url: str, message: str):
    from sieve.crawl import CrawlResponseModel as _CRM
    return _CRM(start_url=url if isinstance(url, str) else "", pages=[], error=message)


async def smart_search(self, query: str, max_results: int = 6, cache_ttl: int = 300, mode: str = "auto", engines: Optional[List[str]] = None, url: Optional[str] = None, site: Optional[str] = None, exclude_sites: Optional[List[str]] = None, location: Optional[str] = None, language: Optional[str] = None, region: Optional[str] = None, page: int = 0, freshness: Optional[str] = None, cached: bool = False, refresh: bool = False, stale_fallback: bool = False, stale_max_age: int = 3600):
    try:
        query = validate_search_query(query)
    except SecurityError:
        # Stable categorized message (#129): validation failures must not leak
        # implementation detail (URL fragments, validator internals) into
        # agent-visible errors.
        return _validation_search_error(query, "search query failed validation")

    # Shared bound validation before any heavyweight import (#128).
    try:
        max_results = bounded_number(max_results, name="max_results",
                                     minimum=1, maximum=_SEARCH_MAX_RESULTS_MAX, integer=True)
        page = bounded_number(page, name="page",
                              minimum=0, maximum=_SEARCH_PAGE_MAX, integer=True)
        cache_ttl = bounded_number(cache_ttl, name="cache_ttl",
                                   minimum=0, maximum=_CACHE_TTL_MAX, integer=True)
        mode = _bounded_str(mode, name="mode", max_len=32)
        engines = _bounded_str_list(engines, name="engines", max_items=_ENGINES_MAX, max_len=64)
        url = _bounded_str(url, name="url", max_len=_URL_PARAM_MAX_LEN)
        site = _bounded_str(site, name="site")
        exclude_sites = _bounded_str_list(exclude_sites, name="exclude_sites",
                                          max_items=_DOMAIN_LIST_MAX)
        location = _bounded_str(location, name="location", max_len=32)
        language = _bounded_str(language, name="language", max_len=32)
        region = _bounded_str(region, name="region", max_len=64)
        freshness = _bounded_str(freshness, name="freshness", max_len=32)
        stale_max_age = bounded_number(stale_max_age, name="stale_max_age", minimum=1, maximum=86400, integer=True)
        if not isinstance(stale_fallback, bool):
            raise ValueError("stale_fallback must be a boolean")
    except ValueError as e:
        return _validation_search_error(query, str(e))

    try:
        from sieve.search import smart_search as _smart_search
        return await _smart_search(self, query, max_results, cache_ttl, mode=mode, engines=engines, url=url, site=site, exclude_sites=exclude_sites, location=location, language=language, region=region, page=page, freshness=freshness, cached=cached, refresh=refresh, stale_fallback=stale_fallback, stale_max_age=stale_max_age)
    except Exception as e:
        # Rationale: the public search boundary redacts external provider failures.
        from sieve.public_output import safe_error
        failure = safe_error(e, fallback_category="network")
        from sieve.search import SearchResponseModel
        return SearchResponseModel(query=query, results=[], total_results=0, duration_ms=0,
                                   category=failure["category"], error=failure["error"])

async def smart_crawl(self, url: str, max_pages: int = 10, max_depth: int = 2, path_include: Optional[List[str]] = None, path_exclude: Optional[List[str]] = None, discover_only: bool = False, focus: Optional[str] = None, crawl_urls: Optional[List[str]] = None, max_content_chars_per: int = 8000, max_total_chars: Optional[int] = None, concurrency: int = 3, cache_ttl=None, respect_robots: bool = True, force_fetcher: Optional[str] = None, timeout: int = 30000, deadline_ms: int = 120000, sitemap=False, auto_throttle: bool = False, throttle_min_delay: float = 2.0):
    # Shared bound validation before any heavyweight import (#128).
    try:
        url = validate_url(url)
        max_pages = bounded_number(max_pages, name="max_pages",
                                   minimum=1, maximum=_CRAWL_MAX_PAGES_MAX, integer=True)
        max_depth = bounded_number(max_depth, name="max_depth",
                                   minimum=0, maximum=_CRAWL_MAX_DEPTH_MAX, integer=True)
        concurrency = bounded_number(concurrency, name="concurrency",
                                     minimum=1, maximum=_CRAWL_CONCURRENCY_MAX, integer=True)
        timeout = bounded_number(timeout, name="timeout",
                                 minimum=1, maximum=_CRAWL_TIMEOUT_MAX_MS, integer=True)
        deadline_ms = bounded_number(deadline_ms, name="deadline_ms",
                                     minimum=1, maximum=_CRAWL_DEADLINE_MAX_MS, integer=True)
        max_content_chars_per = bounded_number(max_content_chars_per, name="max_content_chars_per",
                                               minimum=100, maximum=_CRAWL_CHARS_PER_MAX, integer=True)
        if max_total_chars is not None:
            max_total_chars = bounded_number(max_total_chars, name="max_total_chars",
                                             minimum=1000, maximum=_CRAWL_CHARS_TOTAL_MAX, integer=True)
        path_include = _bounded_str_list(path_include, name="path_include",
                                         max_items=_PATH_LIST_MAX, max_len=_URL_PARAM_MAX_LEN)
        path_exclude = _bounded_str_list(path_exclude, name="path_exclude",
                                         max_items=_PATH_LIST_MAX, max_len=_URL_PARAM_MAX_LEN)
        crawl_urls = _bounded_str_list(crawl_urls, name="crawl_urls",
                                       max_items=_PATH_LIST_MAX, max_len=_URL_PARAM_MAX_LEN)
        focus = _bounded_str(focus, name="focus", max_len=2048)
        force_fetcher = _bounded_str(force_fetcher, name="force_fetcher", max_len=32)
        if cache_ttl is not None:
            cache_ttl = bounded_number(cache_ttl, name="cache_ttl",
                                       minimum=0, maximum=_CACHE_TTL_MAX, integer=True)
        throttle_min_delay = bounded_number(throttle_min_delay, name="throttle_min_delay",
                                            minimum=0.0, maximum=_THROTTLE_DELAY_MAX_S)
    except (ValueError, SecurityError) as e:
        return _validation_crawl_error(url, str(e)[:200])

    if cache_ttl is None:
        from sieve.cache import DEFAULT_TTL
        cache_ttl = DEFAULT_TTL
    try:
        from sieve.crawl import smart_crawl as _smart_crawl
        return await _smart_crawl(self, url, max_pages=max_pages, max_depth=max_depth, path_include=path_include, path_exclude=path_exclude, discover_only=discover_only, focus=focus, crawl_urls=crawl_urls, max_content_chars_per=max_content_chars_per, max_total_chars=max_total_chars, concurrency=concurrency, cache_ttl=cache_ttl, respect_robots=respect_robots, force_fetcher=force_fetcher, timeout=timeout, deadline_ms=deadline_ms, sitemap=sitemap, auto_throttle=auto_throttle, throttle_min_delay=throttle_min_delay)
    except Exception as e:
        from sieve.crawl import CrawlResponseModel as _CRM
        from sieve.public_output import safe_error
        failure = safe_error(e, fallback_category="network")
        return _CRM(start_url=url, pages=[], error=failure["error"])
