"""Fetch-tier methods extracted from server.py â get/fetch/stealthy/smart_fetch + escalation helpers."""
from __future__ import annotations

import os
from typing import Annotated, Mapping, Sequence, Optional, Literal, Dict, List, Any
from pydantic import Field
from time import time as now
from asyncio import gather, to_thread

from sieve.cache import DEFAULT_TTL
from sieve.retrieval import HTTPRetrievalAdapter, BrowserRetrievalAdapter

# Keep the fetch tier importable without importing the legacy transport module.
# The server supplies these implementation helpers only when a fetch operation
# is invoked, after its module has finished initialization.
MAX_CONTENT_CHARS = 40000
MAX_BULK_URLS = 100
ExtendedExtractionType = Literal["markdown", "html", "text", "article", "structured"]
SetCookieParam: Any = None
SelectorWaitStates: Any = None
FollowRedirects: Any = None
ImpersonateType: Any = None
_server_symbols_loaded = False
_server_context_decorator = None

def _smart_fetch_request_context(func):
    """Resolve the transport's request context only when a request runs."""
    async_impl = None
    async def wrapped(*args, **kwargs):
        nonlocal async_impl
        _ensure_server_symbols()
        if async_impl is None:
            async_impl = globals()["_server_context_decorator"](func)
        return await async_impl(*args, **kwargs)
    return wrapped

def _ensure_server_symbols() -> None:
    global _server_symbols_loaded
    if _server_symbols_loaded:
        return
    from sieve import server as _server
    names = (
        "BulkResponseModel", "ResponseModel", "_AJAX_SHELL", "_AJAX_SHELL_ERROR",
        "_annotate_quality", "_apply_capture_verdict", "_apply_chunking",
        "_browser_deps_available", "_is_deterministic_net_error", "_is_deterministic_net_msg",
        "_is_js_shell", "_normalize_credentials", "_safe_cookie_dict",
        "_timed", "_translate_response", "_with_agent_hints", "_PDF_PASSWORD",
        "logger", "_browser_import_error", "SetCookieParam", "SelectorWaitStates",
        "FollowRedirects", "ImpersonateType",
    )
    globals().update({name: getattr(_server, name) for name in names})
    globals()["_server_context_decorator"] = _server._smart_fetch_request_context
    _server_symbols_loaded = True
from sieve.cache import get_cached, set_cached  # noqa: F401 — set_cached is a monkeypatch surface for tests
from sieve.robots import is_allowed_strict
from sieve.reddit import is_reddit_url, rewrite_to_old_reddit
from sieve.security import (
    validate_url, validate_css_selector, validate_headers, validate_proxy,
    redact_url_for_logs, SecurityError,
)
from sieve.public_output import safe_error

# Extraction flags for the cache key (issue #4). These live in this lower
# layer so the cache lookup can read them without importing sieve.server
# (architecture gate: server_fetch must not import the transport module).
# server.py re-exports them and sets them from request options.
import contextvars
_MAIN_CONTENT_ONLY: contextvars.ContextVar[bool] = contextvars.ContextVar("_main_content_only", default=True)
_USE_TRAFILATURA: contextvars.ContextVar[bool] = contextvars.ContextVar("_use_trafilatura", default=True)


def _safe_fetch_error(exc: BaseException | None = None, *, fallback_category: str = "internal") -> str:
    """Return a fixed public diagnostic without exposing backend exception text."""
    safe_exc = exc if isinstance(exc, Exception) else RuntimeError()
    return safe_error(safe_exc, fallback_category=fallback_category)["error"]



async def get(
    url: str,
    impersonate: ImpersonateType = "chrome",
    extraction_type: ExtendedExtractionType = "markdown",
    css_selector: Optional[str] = None,
    main_content_only: bool = True,
    use_trafilatura: bool = True,
    params: Optional[Dict] = None,
    headers: Optional[Mapping[str, Optional[str]]] = None,
    cookies: Optional[Dict[str, str]] = None,
    timeout: Optional[int | float] = 30,
    follow_redirects: FollowRedirects = "safe",
    max_redirects: int = 30,
    retries: Optional[int] = 3,
    retry_delay: Optional[int] = 1,
    proxy: Optional[str] = None,
    proxy_auth: Optional[Dict[str, str]] = None,
    auth: Optional[Dict[str, str]] = None,
    verify: Optional[bool] = True,
    http3: Optional[bool] = False,
    stealthy_headers: Optional[bool] = True,
) -> ResponseModel:
    """Make GET HTTP request with browser fingerprint impersonation.
    Fast, but only works for low-protection sites. For protected sites, use
    smart_fetch or stealthy_fetch.
    :param url: The URL to request.
    :param impersonate: Browser to impersonate (default 'chrome').
    :param extraction_type: Content format: 'markdown', 'html', 'text', 'article', 'structured'.
    :param css_selector: CSS selector to narrow content before extraction.
    :param main_content_only: Strip nav/ads/footers (default True).
    :param use_trafilatura: Use Trafilatura for article extraction (default True).
    :param max_redirects: Max redirects (default 30).
    :param retries: Retry attempts (default 3).
    :param retry_delay: Seconds between retries (default 1).
    :param proxy: Proxy URL.
    :param proxy_auth: Proxy auth dict with 'username' and 'password'.
    :param auth: HTTP basic auth dict with 'username' and 'password'.
    :param verify: Verify HTTPS certificates (default True).
    :param http3: Use HTTP/3 (default False).
    :param stealthy_headers: Generate real browser headers (default True).
    """
    _ensure_server_symbols()
    url = validate_url(url)
    validate_css_selector(css_selector)
    validate_proxy(proxy)
    t0 = now()
    bulk = await bulk_get(
        urls=[url], impersonate=impersonate, extraction_type=extraction_type,
        css_selector=css_selector, main_content_only=main_content_only,
        use_trafilatura=use_trafilatura, params=params, headers=headers,
        cookies=cookies, timeout=timeout, follow_redirects=follow_redirects,
        max_redirects=max_redirects, retries=retries, retry_delay=retry_delay,
        proxy=proxy, proxy_auth=proxy_auth, auth=auth, verify=verify,
        http3=http3, stealthy_headers=stealthy_headers,
    )
    result = bulk.results[0]
    result.duration_ms = (now() - t0) * 1000
    return result


async def bulk_get(
    urls: List[str],
    impersonate: ImpersonateType = "chrome",
    extraction_type: ExtendedExtractionType = "markdown",
    css_selector: Optional[str] = None,
    main_content_only: bool = True,
    use_trafilatura: bool = True,
    params: Optional[Dict] = None,
    headers: Optional[Mapping[str, Optional[str]]] = None,
    cookies: Optional[Dict[str, str]] = None,
    timeout: Optional[int | float] = 30,
    follow_redirects: FollowRedirects = "safe",
    max_redirects: int = 30,
    retries: Optional[int] = 3,
    retry_delay: Optional[int] = 1,
    proxy: Optional[str] = None,
    proxy_auth: Optional[Dict[str, str]] = None,
    auth: Optional[Dict[str, str]] = None,
    verify: Optional[bool] = True,
    http3: Optional[bool] = False,
    stealthy_headers: Optional[bool] = True,
) -> BulkResponseModel:
    """Async parallel GET requests with browser fingerprint impersonation.
    Fast, but only works for low-protection sites.
    :param urls: List of URLs to request.
    :param impersonate: Browser to impersonate (default 'chrome').
    :param extraction_type: Content format: 'markdown', 'html', 'text', 'article', 'structured'.
    :param css_selector: CSS selector to narrow content.
    :param main_content_only: Strip nav/ads/footers (default True).
    :param use_trafilatura: Use Trafilatura for article extraction (default True).
    :param params: Query parameters.
    :param headers: Request headers.
    :param cookies: Request cookies.
    :param timeout: Timeout in seconds (default 30).
    :param follow_redirects: Redirect policy.
    :param max_redirects: Max redirects (default 30).
    :param retries: Retry attempts (default 3).
    :param retry_delay: Seconds between retries (default 1).
    :param proxy: Proxy URL.
    :param proxy_auth: Proxy auth dict.
    :param auth: HTTP basic auth dict.
    :param verify: Verify HTTPS certificates (default True).
    :param http3: Use HTTP/3 (default False).
    :param stealthy_headers: Generate real browser headers (default True).
    """
    _ensure_server_symbols()
    # Validate all URLs
    urls = [validate_url(u) for u in urls]
    if len(urls) > MAX_BULK_URLS:
        raise ValueError(f"Too many URLs ({len(urls)}). Maximum is {MAX_BULK_URLS} per call.")
    validate_css_selector(css_selector)
    validate_proxy(proxy)
    normalized_proxy_auth = _normalize_credentials(proxy_auth)
    normalized_auth = _normalize_credentials(auth)
    use_tf = use_trafilatura and extraction_type in ("markdown", "text", "article", "structured")
    from sieve.fetcher import HTTPSession
    http_proxy = proxy if isinstance(proxy, str) else None
    async with HTTPSession(
        impersonate=impersonate or "chrome",
        proxy=http_proxy,
        stealthy_headers=stealthy_headers,
        retries=retries,
        retry_delay=retry_delay,
        timeout=max(1, min(int(timeout), 30)),
    ) as session:
        adapter = HTTPRetrievalAdapter(session)
        timed_tasks = [
            _timed(adapter.fetch(
                url, headers=headers, cookies=cookies if isinstance(cookies, dict) else None,
                timeout=max(1, min(int(timeout), 30)), retries=retries,
                proxy=http_proxy, follow_redirects=follow_redirects,
                max_redirects=max_redirects, params=params,
            ))
            for url in urls
        ]
        timed_responses = await gather(*timed_tasks, return_exceptions=True)
        results = []
        for i, resp in enumerate(timed_responses):
            if isinstance(resp, BaseException):
                error = _safe_fetch_error(resp, fallback_category="network")
                results.append(ResponseModel(
                    url=urls[i], status=0,
                    content=[f"[Fetch error: {error}]"],
                    fetcher_used="http", error=error,
                ))
            else:
                page, elapsed = resp
                results.append(_annotate_quality(
                        _translate_response(
                            page, extraction_type, css_selector, main_content_only, use_tf, "http", elapsed,
                        )
                    ))
    successful = sum(1 for r in results if r.status < 400 and not r.error)
    return BulkResponseModel(results=results, total=len(results), successful=successful)
# âââ Dynamic Fetcher (Playwright) ââââââââââââââââââââââââââââââ


async def fetch(
    self,
    url: str,
    extraction_type: ExtendedExtractionType = "markdown",
    css_selector: Optional[str] = None,
    main_content_only: bool = True,
    use_trafilatura: bool = True,
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
    cookies: Sequence[SetCookieParam] | None = None,
    network_idle: bool = False,
    wait_selector_state: SelectorWaitStates = "attached",
    session_id: Optional[str] = None,
) -> ResponseModel:
    """Dynamic content via Playwright browser. Handles JS-rendered pages, low-mid protection.
    For high protection / Cloudflare, use stealthy_fetch or smart_fetch instead.
    :param url: The URL to fetch.
    :param extraction_type: Content format: 'markdown', 'html', 'text', 'article', 'structured'.
    :param css_selector: CSS selector to narrow content.
    :param main_content_only: Strip nav/ads/footers (default True).
    :param use_trafilatura: Use Trafilatura for article extraction (default True).
    :param headless: Run browser in headless mode (default True).
    :param google_search: Set Google referer header (default True).
    :param real_chrome: Use installed Chrome instead of Chromium.
    :param wait: Milliseconds to wait after page load.
    :param proxy: Proxy to use.
    :param timezone_id: Browser timezone.
    :param locale: Browser locale, e.g., 'en-GB'.
    :param extra_headers: Extra request headers.
    :param useragent: Custom user agent.
    :param cdp_url: Connect via CDP URL.
    :param timeout: Timeout in milliseconds (default 30000).
    :param disable_resources: Drop font/image/media/stylesheet requests.
    :param wait_selector: CSS selector to wait for.
    :param cookies: Cookies to set.
    :param network_idle: Wait for no network connections for 500ms.
    :param wait_selector_state: Selector wait state.
    :param session_id: Reuse existing browser session.
    """
    _ensure_server_symbols()
    url = validate_url(url)
    validate_css_selector(css_selector)
    validate_headers(extra_headers)
    validate_proxy(proxy)
    t0 = now()
    bulk = await bulk_fetch(
        self,
        urls=[url], extraction_type=extraction_type, css_selector=css_selector,
        main_content_only=main_content_only, use_trafilatura=use_trafilatura,
        headless=headless, google_search=google_search, real_chrome=real_chrome,
        wait=wait, proxy=proxy, timezone_id=timezone_id, locale=locale,
        extra_headers=extra_headers, useragent=useragent, cdp_url=cdp_url,
        timeout=timeout, disable_resources=disable_resources,
        wait_selector=wait_selector, cookies=cookies, network_idle=network_idle,
        wait_selector_state=wait_selector_state, session_id=session_id,
    )
    result = bulk.results[0]
    result.duration_ms = (now() - t0) * 1000
    return result


async def bulk_fetch(
    self,
    urls: List[str],
    extraction_type: ExtendedExtractionType = "markdown",
    css_selector: Optional[str] = None,
    main_content_only: bool = True,
    use_trafilatura: bool = True,
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
    cookies: Sequence[SetCookieParam] | None = None,
    network_idle: bool = False,
    wait_selector_state: SelectorWaitStates = "attached",
    session_id: Optional[str] = None,
) -> BulkResponseModel:
    """Async parallel dynamic fetch via Playwright. Handles JS-rendered pages.
    :param urls: List of URLs to fetch.
    :param extraction_type: Content format: 'markdown', 'html', 'text', 'article', 'structured'.
    :param css_selector: CSS selector to narrow content.
    :param main_content_only: Strip nav/ads/footers (default True).
    :param use_trafilatura: Use Trafilatura for article extraction (default True).
    :param headless: Run browser in headless mode (default True).
    :param google_search: Set Google referer header (default True).
    :param real_chrome: Use installed Chrome instead of Chromium.
    :param wait: Milliseconds to wait after page load.
    :param proxy: Proxy to use.
    :param timezone_id: Browser timezone.
    :param locale: Browser locale.
    :param extra_headers: Extra request headers.
    :param useragent: Custom user agent.
    :param cdp_url: Connect via CDP URL.
    :param timeout: Timeout in milliseconds (default 30000).
    :param disable_resources: Drop unnecessary resource requests.
    :param wait_selector: CSS selector to wait for.
    :param cookies: Cookies to set.
    :param network_idle: Wait for no network connections for 500ms.
    :param wait_selector_state: Selector wait state.
    :param session_id: Reuse existing browser session.
    """
    _ensure_server_symbols()
    urls = [validate_url(u) for u in urls]
    if len(urls) > MAX_BULK_URLS:
        raise ValueError(f"Too many URLs ({len(urls)}). Maximum is {MAX_BULK_URLS} per call.")
    validate_css_selector(css_selector)
    validate_headers(extra_headers)
    validate_proxy(proxy)
    validate_css_selector(wait_selector)
    if not _browser_deps_available():
        raise RuntimeError(
            f"Dynamic fetch requires browser deps which are unavailable: "
            f"{_browser_import_error or 'patchright not importable'}. "
            "Install with: uv sync"
        )
    use_tf = use_trafilatura and extraction_type in ("markdown", "text", "article", "structured")
    if session_id:
        entry = await self._get_session(session_id, "dynamic")
        timed_tasks = [
            _timed(BrowserRetrievalAdapter(entry.session).fetch(
                url, wait=wait, timeout=timeout, google_search=google_search,
                extra_headers=extra_headers, disable_resources=disable_resources,
                wait_selector=wait_selector, wait_selector_state=wait_selector_state,
                network_idle=network_idle, proxy=proxy,
            ))
            for url in urls
        ]
        timed_responses = await gather(*timed_tasks, return_exceptions=True)
    else:
        from sieve.browser import DynamicBrowser
        async with DynamicBrowser(
            wait=wait, proxy=proxy, locale=locale, timeout=timeout,
            cookies=cookies, cdp_url=cdp_url, headless=headless,
            block_ads=True, max_pages=len(urls), useragent=useragent,
            timezone_id=timezone_id, real_chrome=real_chrome,
            network_idle=network_idle, wait_selector=wait_selector,
            google_search=google_search, extra_headers=extra_headers,
            disable_resources=disable_resources,
            wait_selector_state=wait_selector_state,
        ) as session:
            adapter = BrowserRetrievalAdapter(session)
            timed_tasks = [_timed(adapter.fetch(url)) for url in urls]
            timed_responses = await gather(*timed_tasks, return_exceptions=True)
    results = []
    for i, resp in enumerate(timed_responses):
        if isinstance(resp, BaseException):
            error = _safe_fetch_error(resp, fallback_category="network")
            results.append(ResponseModel(
                url=urls[i], status=0,
                content=[f"[Fetch error: {error}]"],
                fetcher_used="dynamic", error=error,
            ))
        else:
            page, elapsed = resp
            results.append(_annotate_quality(
                    _translate_response(
                        page, extraction_type, css_selector, main_content_only, use_tf, "dynamic", elapsed,
                    )
                ))
    successful = sum(1 for r in results if r.status < 400 and not r.error)
    return BulkResponseModel(results=results, total=len(results), successful=successful)
# âââ Stealthy Fetcher (Patchright) âââââââââââââââââââââââââââââ


async def stealthy_fetch(
    self,
    url: str,
    extraction_type: ExtendedExtractionType = "markdown",
    css_selector: Optional[str] = None,
    main_content_only: bool = True,
    use_trafilatura: bool = True,
    headless: bool = True,
    google_search: bool = True,
    real_chrome: bool = False,
    wait: int | float = 0,
    proxy: Optional[str | Dict[str, str]] = None,
    timezone_id: str | None = None,
    locale: str | None = None,
    extra_headers: Optional[Dict[str, str]] = None,
    useragent: Optional[str] = None,
    hide_canvas: bool = False,
    cdp_url: Optional[str] = None,
    timeout: int | float = 30000,
    disable_resources: bool = False,
    wait_selector: Optional[str] = None,
    cookies: Sequence[SetCookieParam] | None = None,
    network_idle: bool = False,
    wait_selector_state: SelectorWaitStates = "attached",
    block_webrtc: bool = False,
    allow_webgl: bool = True,
    solve_cloudflare: bool = False,
    additional_args: Optional[Dict] = None,
    session_id: Optional[str] = None,
    page_action=None,
    capture_xhr: bool = False,
    capture_pattern: Optional[str] = None,
) -> ResponseModel:
    """Stealthy fetcher with anti-bot bypass via Patchright (rebrowser-playwright fork).
    Uses browser fingerprint randomization to evade detection by:
    - Cloudflare embedded challenge pages (not Turnstile CAPTCHA)
    - Basic bot-detection scripts that check navigator/webdriver properties
    Does NOT bypass:
    - Cloudflare Turnstile (interactive CAPTCHA widget â requires human)
    - DataDome (behavioral analysis â detects headless browsers via timing)
    - Akamai Bot Manager (advanced fingerprinting beyond Patchright's scope)
    For the 3-tier auto-escalation that tries HTTPâdynamicâstealthy, use smart_fetch instead.
    :param url: The URL to fetch.
    :param extraction_type: Content format: 'markdown', 'html', 'text', 'article', 'structured'.
    :param css_selector: CSS selector to narrow content.
    :param main_content_only: Strip nav/ads/footers (default True).
    :param use_trafilatura: Use Trafilatura for article extraction (default True).
    :param headless: Run browser in headless mode (default True).
    :param solve_cloudflare: Auto-solve Cloudflare embedded challenges.
    :param block_webrtc: Prevent IP leak via WebRTC.
    :param hide_canvas: Random canvas noise.
    :param allow_webgl: Keep WebGL enabled (default True; WAFs check for it).
    :param real_chrome: Use installed Chrome.
    :param wait: Milliseconds to wait after page load.
    :param proxy: Proxy to use.
    :param timezone_id: Browser timezone.
    :param locale: Browser locale.
    :param extra_headers: Extra request headers.
    :param useragent: Custom user agent.
    :param cdp_url: Connect via CDP URL.
    :param timeout: Timeout in milliseconds (default 30000).
    :param disable_resources: Drop unnecessary resource requests.
    :param wait_selector: CSS selector to wait for.
    :param cookies: Cookies to set.
    :param network_idle: Wait for no network connections for 500ms.
    :param wait_selector_state: Selector wait state.
    :param additional_args: Extra Playwright context args.
    :param session_id: Reuse existing browser session.
    """
    _ensure_server_symbols()
    url = validate_url(url)
    validate_css_selector(css_selector)
    validate_headers(extra_headers)
    validate_proxy(proxy)
    if not _browser_deps_available():
        raise RuntimeError(
            f"Stealthy fetch requires browser deps which are unavailable: "
            f"{_browser_import_error or 'patchright not importable'}. "
            "Install with: uv sync"
        )
    t0 = now()
    bulk = await bulk_stealthy_fetch(
        self,
        urls=[url], extraction_type=extraction_type, css_selector=css_selector,
        main_content_only=main_content_only, use_trafilatura=use_trafilatura,
        headless=headless, google_search=google_search, real_chrome=real_chrome,
        wait=wait, proxy=proxy, timezone_id=timezone_id, locale=locale,
        extra_headers=extra_headers, useragent=useragent, hide_canvas=hide_canvas,
        cdp_url=cdp_url, timeout=timeout, disable_resources=disable_resources,
        wait_selector=wait_selector, cookies=cookies, network_idle=network_idle,
        wait_selector_state=wait_selector_state, block_webrtc=block_webrtc,
        allow_webgl=allow_webgl, solve_cloudflare=solve_cloudflare,
        additional_args=additional_args, session_id=session_id,
        page_action=page_action,
        capture_xhr=capture_xhr, capture_pattern=capture_pattern,
    )
    result = bulk.results[0]
    result.duration_ms = (now() - t0) * 1000
    return result


async def bulk_stealthy_fetch(
    self,
    urls: List[str],
    extraction_type: ExtendedExtractionType = "markdown",
    css_selector: Optional[str] = None,
    main_content_only: bool = True,
    use_trafilatura: bool = True,
    headless: bool = True,
    google_search: bool = True,
    real_chrome: bool = False,
    wait: int | float = 0,
    proxy: Optional[str | Dict[str, str]] = None,
    timezone_id: str | None = None,
    locale: str | None = None,
    extra_headers: Optional[Dict[str, str]] = None,
    useragent: Optional[str] = None,
    hide_canvas: bool = False,
    cdp_url: Optional[str] = None,
    timeout: int | float = 30000,
    disable_resources: bool = False,
    wait_selector: Optional[str] = None,
    cookies: Sequence[SetCookieParam] | None = None,
    network_idle: bool = False,
    wait_selector_state: SelectorWaitStates = "attached",
    block_webrtc: bool = False,
    allow_webgl: bool = True,
    solve_cloudflare: bool = False,
    additional_args: Optional[Dict] = None,
    session_id: Optional[str] = None,
    page_action=None,
    capture_xhr: bool = False,
    capture_pattern: Optional[str] = None,
) -> BulkResponseModel:
    """Async parallel stealthy fetch with browser fingerprint randomization.
    :param urls: List of URLs to fetch.
    :param extraction_type: Content format: 'markdown', 'html', 'text', 'article', 'structured'.
    :param css_selector: CSS selector to narrow content.
    :param main_content_only: Strip nav/ads/footers (default True).
    :param use_trafilatura: Use Trafilatura for article extraction (default True).
    :param headless: Run browser in headless mode (default True).
    :param solve_cloudflare: Auto-solve Cloudflare challenges.
    :param block_webrtc: Prevent IP leak via WebRTC.
    :param hide_canvas: Random canvas noise.
    :param allow_webgl: Keep WebGL enabled (default True).
    :param real_chrome: Use installed Chrome.
    :param wait: Milliseconds to wait after page load.
    :param proxy: Proxy to use.
    :param timezone_id: Browser timezone.
    :param locale: Browser locale.
    :param extra_headers: Extra request headers.
    :param useragent: Custom user agent.
    :param cdp_url: Connect via CDP URL.
    :param timeout: Timeout in milliseconds (default 30000).
    :param disable_resources: Drop unnecessary resource requests.
    :param wait_selector: CSS selector to wait for.
    :param cookies: Cookies to set.
    :param network_idle: Wait for no network connections for 500ms.
    :param wait_selector_state: Selector wait state.
    :param additional_args: Extra Playwright context args.
    :param session_id: Reuse existing browser session.
    :param capture_xhr: Capture XHR/fetch bodies into each result's network field.
    :param capture_pattern: Regex selecting which request URLs to capture.
    """
    _ensure_server_symbols()
    urls = [validate_url(u) for u in urls]
    if len(urls) > MAX_BULK_URLS:
        raise ValueError(f"Too many URLs ({len(urls)}). Maximum is {MAX_BULK_URLS} per call.")
    validate_css_selector(css_selector)
    validate_headers(extra_headers)
    validate_proxy(proxy)
    validate_css_selector(wait_selector)
    if not _browser_deps_available():
        raise RuntimeError(
            f"Stealthy fetch requires browser deps which are unavailable: "
            f"{_browser_import_error or 'patchright not importable'}. "
            "Install with: uv sync"
        )
    use_tf = use_trafilatura and extraction_type in ("markdown", "text", "article", "structured")
    if session_id:
        entry = await self._get_session(session_id, "stealthy")
        timed_tasks = [
            _timed(BrowserRetrievalAdapter(entry.session).fetch(
                url, wait=wait, timeout=timeout, google_search=google_search,
                extra_headers=extra_headers, disable_resources=disable_resources,
                wait_selector=wait_selector, wait_selector_state=wait_selector_state,
                network_idle=network_idle, proxy=proxy, solve_cloudflare=solve_cloudflare,
                page_action=page_action,
                capture_xhr=capture_xhr, capture_pattern=capture_pattern,
            ))
            for url in urls
        ]
        timed_responses = await gather(*timed_tasks, return_exceptions=True)
    else:
        from sieve.browser import StealthyBrowser
        async with StealthyBrowser(
            wait=wait, proxy=proxy, locale=locale, cdp_url=cdp_url,
            timeout=timeout, cookies=cookies, headless=headless,
            block_ads=True, useragent=useragent, timezone_id=timezone_id,
            real_chrome=real_chrome, hide_canvas=hide_canvas,
            allow_webgl=allow_webgl, network_idle=network_idle,
            block_webrtc=block_webrtc, wait_selector=wait_selector,
            google_search=google_search, extra_headers=extra_headers,
            additional_args=additional_args, solve_cloudflare=solve_cloudflare,
            disable_resources=disable_resources,
            wait_selector_state=wait_selector_state,
        ) as session:
            timed_tasks = [
                _timed(BrowserRetrievalAdapter(session).fetch(
                    url, page_action=page_action,
                    capture_xhr=capture_xhr, capture_pattern=capture_pattern,
                ))
                for url in urls
            ]
            timed_responses = await gather(*timed_tasks, return_exceptions=True)
    results = []
    for i, resp in enumerate(timed_responses):
        if isinstance(resp, BaseException):
            error = _safe_fetch_error(resp, fallback_category="network")
            results.append(ResponseModel(
                url=urls[i], status=0,
                content=[f"[Fetch error: {error}]"],
                fetcher_used="stealthy", error=error,
            ))
        else:
            page, elapsed = resp
            results.append(_annotate_quality(
                    _translate_response(
                        page, extraction_type, css_selector, main_content_only, use_tf, "stealthy", elapsed,
                    )
                ))
    successful = sum(1 for r in results if 0 < r.status < 400 and not r.error)
    return BulkResponseModel(results=results, total=len(results), successful=successful)
# âââ SMART FETCH (The One Tool To Rule Them All) ââââââââââââââââ


async def _http_with_retry(self, url: str, **kwargs) -> ResponseModel:
    """HTTP fetch with retry logic for transient network failures.
    Does NOT retry on validation errors (SecurityError/ValueError) â those
    are deterministic (bad URL, oversized response, blocked scheme) and
    retrying just re-downloads the same failure. Only network/transport
    errors are retried with exponential backoff.
    """
    _ensure_server_symbols()
    max_retries = 3
    base_delay = 1.0
    last_error = None
    attempts = 0
    # self.get retries internally (default 3x); this outer loop IS the
    # retry policy (backoff + deterministic-error bail-out), so disable
    # the inner one. Nested, they made 4 x 4 = 16 identical requests
    # against a blocking site before escalation ever ran.
    kwargs.setdefault("retries", 0)
    for attempt in range(max_retries + 1):
        from sieve.resource_budget import BudgetExceeded, current_budget
        account = current_budget()
        if account is not None and (account.expired or (attempt and not account.charge("retries", 1))):
            raise BudgetExceeded("Request retry or deadline budget exceeded.")
        attempts = attempt + 1
        try:
            return await self.get(url, **kwargs)
        except (SecurityError, ValueError):
            # Deterministic failure â surface immediately, no retry.
            raise
        except Exception as e:
            last_error = e
            # DNS failure / connection refused: identical retries
            # cannot help. Bail to the escalation path immediately.
            if _is_deterministic_net_msg(str(e)):
                break
            if attempt < max_retries:
                delay = base_delay * (2 ** attempt)
                logger.warning(
                    f"HTTP fetch attempt {attempt + 1} failed for "
                    f"{redact_url_for_logs(url)}: "
                    f"{safe_error(e)['error']}. Retrying in {delay:.0f}s..."
                )
                await asyncio_sleep(delay)
            else:
                logger.error(
                    f"HTTP fetch failed after {max_retries + 1} attempts for "
                    f"{redact_url_for_logs(url)}: {safe_error(e)['error']}"
                )
    return ResponseModel(
        url=url,
        content=[
            f"[Network error] Failed to fetch after {max_retries + 1} "
            f"attempts.\n"
            f"Error: {safe_error(last_error, fallback_category='network')['error']}\n"
            f"\n"
            f"Tips:\n"
            f"- Check that the URL is publicly accessible.\n"
            f"- If the site requires JavaScript, smart_fetch will auto-escalate to a browser.\n"
            f"- If behind Cloudflare, smart_fetch will try stealthy mode with the Cloudflare solver."
        ],
        status=0, fetcher_used="none", cached=False,
        extracted_type=kwargs.get("extraction_type", "markdown"),
        session_id="", duration_ms=0,
        error=safe_error(last_error, fallback_category="network")["error"],
        retry_count=attempts,
    )


@_smart_fetch_request_context
async def smart_fetch(
    self,
    url: Annotated[str, Field(description="Single URL to fetch.")],
    urls: Annotated[Optional[List[str]], Field(description="Multiple URLs to fetch in parallel. Returns bulk results. Use instead of calling smart_fetch multiple times.")] = None,
    extraction_type: Annotated[ExtendedExtractionType, Field(description="Content format: 'markdown' (default), 'html', 'text', 'article', 'structured'.")] = "markdown",
    css_selector: Annotated[Optional[str], Field(description="CSS selector to narrow extracted content (e.g. 'article', '.main-content').")] = None,
    main_content_only: Annotated[bool, Field(description="Strip nav, ads, footers (default True).")] = True,
    use_trafilatura: Annotated[bool, Field(description="Use Trafilatura for cleaner article extraction (default True).")] = True,
    cache_ttl: Annotated[int, Field(description="Cache duration in seconds. Default 3600 (1 hour). Set 0 to skip cache and force a fresh fetch.")] = DEFAULT_TTL,
    force_fetcher: Annotated[Optional[Literal["http", "dynamic", "stealthy"]], Field(description="Lock to one fetcher tier. 'http' = fast HTTP-only, 'dynamic' = Playwright JS rendering, 'stealthy' = Cloudflare bypass. Skips auto-escalation.")] = None,
    respect_robots: Annotated[bool, Field(description="Check robots.txt before fetching (default False).")] = False,
    headless: Annotated[bool, Field(description="Run browser without visible window (default True).")] = True,
    real_chrome: Annotated[bool, Field(description="Use installed Chrome instead of bundled browser.")] = False,
    wait: Annotated[int | float, Field(description="Extra milliseconds to wait after page load for JS rendering.")] = 0,
    proxy: Annotated[Optional[str | Dict[str, str]], Field(description="Proxy URL or dict with server/username/password.")] = None,
    timeout: Annotated[int | float, Field(description="Max request time in milliseconds (default 30000).")] = 30000,
    network_idle: Annotated[bool, Field(description="Wait until network is idle for 500ms before capturing (good for SPAs).")] = False,
    solve_cloudflare: Annotated[bool, Field(description="Attempt Cloudflare bypass in stealthy mode (default True).")] = True,
    block_webrtc: Annotated[bool, Field(description="Prevent WebRTC IP leak in stealthy mode (default True).")] = True,
    hide_canvas: Annotated[bool, Field(description="Randomize canvas fingerprint in stealthy mode (default True).")] = True,
    extra_headers: Annotated[Optional[Dict[str, str]], Field(description="Additional HTTP headers as {name: value} dict.")] = None,
    useragent: Annotated[Optional[str], Field(description="Override browser user agent string.")] = None,
    cookies: Annotated[Sequence[SetCookieParam] | None, Field(description="Cookies as list of {name, value, domain} dicts.")] = None,
    offset: Annotated[int, Field(description="Resume from this character offset when content was truncated. The response tells you the next offset to use.")] = 0,
    max_content_chars: Annotated[Optional[int], Field(description="Max chars of extracted content to return (default 40000). Lower this to save context tokens on big pages; the rest is paginated via offset/next_offset.")] = None,
    pages: Annotated[Optional[str], Field(description="PDF only: page spec like '1-5' or '1,3,5-7' to extract a subset of pages (saves tokens/time on big PDFs). None = all pages.")] = None,
    password: Annotated[Optional[str], Field(description="PDF only: password for an encrypted PDF.")] = None,
    focus: Annotated[Optional[str], Field(description="Query-focused extraction: pass a query and only the BM25-relevant blocks (paragraphs/headings/tables) are returned, saving context on long pages. Works post-cache, so it never triggers a re-fetch. Re-pass the same focus when paginating with offset. Empty = full page.")] = None,
    actions: Annotated[Optional[List[Dict[str, Any]]], Field(description="Page interactions run on the stealthy browser AFTER load, BEFORE extraction: [{click:'button.load-more'}, {fill:{selector:'#q', text:'x'}}, {press:'Enter'}, {wait:500}, {scroll:3}, {wait_selector:'.item'}]. Forces the stealthy tier; bypasses cache. Reaches content behind a click/form/infinite scroll. Combine with capture_xhr=true to grab the XHR responses those interactions fire (data that only loads on click/scroll/tab-switch) â the capture waits for post-interaction requests to settle.")] = None,
    include_media: Annotated[bool, Field(description="If true, populate the response .media field with up to 20 image URLs found on the page (for multimodal agents). Default false (keeps responses lean).")] = False,
    include_links: Annotated[bool, Field(description="If true, populate the response .links field with the page's outgoing links classified as citations/navigation/external + a primary_source hint. Default false. Use when you want to follow a page's referenced sources in one step.")] = False,
    capture_xhr: Annotated[bool, Field(description="Capture the XHR/fetch responses the page makes while rendering, into the .network field. Forces the browser tier. Use for pages whose data (prices, forecasts, tables) arrives over XHR after load and so never appears in the HTML. Auto-enabled when an AJAX shell is detected, so you rarely need to set it. Pass with actions=[...] to also capture XHRs fired by clicks/scroll/tab-switches (data behind an interaction).")] = False,
    capture_pattern: Annotated[Optional[str], Field(description="Regex to select which request URLs to capture (e.g. 'ajax_pub|/api/'). Only used with capture_xhr. Omit to let sieve filter out analytics/ads and keep plausible data endpoints.")] = None,
    fold_captured: Annotated[bool, Field(description="Merge the primary captured XHR fragment into content instead of leaving it in .network only. Default false (content stays exactly what the page rendered).")] = False,
) -> ResponseModel:
    """Fetch a URL (or multiple URLs) with automatic anti-bot escalation.
    Use this for ALL web page fetching. It auto-selects the best method:
    HTTP (fast, curl_cffi) â Dynamic (Playwright, JS rendering) â Stealthy (Cloudflare bypass).
    When to use:
    - Fetching any web page for content extraction
    - Sites that might have anti-bot protection (Cloudflare embedded challenges, JS-required pages)
    - Fetching multiple URLs at once (use urls parameter)
    - When you don't know which fetcher to use. This tool decides for you.
    When NOT to use:
    - Taking screenshots: use the screenshot tool instead
    - Web search: use smart_search instead
    - You specifically need HTTP-only without escalation: set force_fetcher="http"
    Response: url, status, content (extracted text), content_type, total_size_bytes,
    is_truncated (+ next_offset to paginate), escalation_path, duration_ms, error.
    Signals to branch on: content_ok (real content, not a login/bot wall?), next_action
    (suggested next call), summary, page_type (article/docs/list/forum/auth_wall/paywall/...),
    content_age_days + is_stale, source_type + is_official, source + archived_at.
    AJAX shells: some pages render complete-looking HTML whose data panels are
    filled in later over XHR. Those return boilerplate in content and the real
    data in .network (see capture_xhr) â check .network when content reads like
    help text with no numbers.
    """
    # Bulk mode: fetch multiple URLs in parallel
    if urls is not None:
        if actions:
            raise ValueError("actions are not supported in bulk mode; call smart_fetch once per URL")
        return await self._smart_fetch_bulk(
            urls, extraction_type, css_selector, main_content_only,
            use_trafilatura, cache_ttl, force_fetcher, respect_robots,
            headless, real_chrome, wait, proxy, timeout, network_idle,
            solve_cloudflare, block_webrtc, hide_canvas, extra_headers,
            useragent, cookies, max_content_chars, include_media, include_links,
            focus,
        )
    # Validate all inputs
    url, css_selector, extra_headers, timeout, proxy, useragent = \
        self._validate_smart_fetch_params(
            url, extraction_type, css_selector, extra_headers, timeout, proxy, useragent,
        )
    mc = _normalize_max_content_chars(max_content_chars)
    # Request options flow to lower-level fetchers through ContextVars. The
    # decorator scopes them to this call so sequential requests cannot leak
    # state into one another while concurrent bulk tasks remain isolated.
    cache_ttl = _effective_cache_ttl(
        cache_ttl, actions=actions, capture_xhr=capture_xhr,
        cookies=cookies, extra_headers=extra_headers, proxy=proxy,
        useragent=useragent, force_fetcher=force_fetcher,
        persistent_browser_context=_has_persistent_auto_browser_context(self),
    )
    # 1. Check robots.txt compliance. Strict mode fails closed (issue #3): an
    # unavailable policy (transport/DNS/timeout, 401/403, 5xx, parse errors)
    # refuses the fetch instead of silently allowing it.
    if respect_robots and not await is_allowed_strict(url):
        return _apply_chunking(_robots_blocked_response(url, extraction_type), max_chars=mc)
    # 2. Check cache
    if cache_ttl > 0:
        cached = await get_cached(
            url, extraction_type, css_selector, ttl=cache_ttl,
            pages=pages if isinstance(pages, str) else None,
            main_content_only=_MAIN_CONTENT_ONLY.get(),
            use_trafilatura=_USE_TRAFILATURA.get(),
            password=_PDF_PASSWORD.get(),
        )
        if cached is not None:
            return _apply_chunking(
                _response_from_cache(cached, extraction_type), max_chars=mc, offset=offset,
            )
    # 3. Reddit optimization: rewrite listings to old.reddit.com (7x smaller,
    #    2x faster). Done BEFORE force_fetcher so even an explicit
    #    force_fetcher="http" benefits from the old.reddit.com rewrite.
    #    Post pages (/comments/...) stay on www.reddit.com (old.reddit.com
    #    shows the sidebar instead of full comments) â handled inside
    #    rewrite_to_old_reddit.
    is_reddit = is_reddit_url(url)
    if is_reddit:
        url = rewrite_to_old_reddit(url)
    # 3.5. actions: page interactions (click/fill/press/wait/scroll) require
    # the stealthy browser tier. Force it, bypass cache (post-action content
    # is unique to the action sequence), and pass a page_action callable.
    # capture_xhr composes here: the response hook is live for the whole page
    # lifetime, so XHRs fired BY the interactions are captured too. This is
    # the path for data that only loads on click / scroll / tab-switch.
    if actions:
        if force_fetcher == "http":
            raise ValueError("actions require the browser tier; use force_fetcher='stealthy' or omit it")
        from sieve.actions import build_page_action
        page_action = build_page_action(actions)  # validates; raises on bad input
        if page_action is None:
            raise ValueError("actions must be a non-empty list of action dicts")
        return await self._force_fetch(
            url, "stealthy", extraction_type, css_selector, main_content_only,
            use_trafilatura, cache_ttl, offset, headless, real_chrome, wait,
            proxy, timeout, network_idle, solve_cloudflare, block_webrtc,
            hide_canvas, extra_headers, useragent, cookies, mc,
            page_action=page_action,
            capture_xhr=capture_xhr, capture_pattern=capture_pattern,
            fold_captured=fold_captured,
        )
    # 3.6. capture_xhr needs a page that actually runs JS and issues the
    # requests, so it pins the browser tier the same way actions do.
    if capture_xhr and force_fetcher == "http":
        raise ValueError("capture_xhr requires the browser tier; use force_fetcher='stealthy' or omit it")
    # 4. Force specific fetcher (explicit pin wins; uses rewritten url)
    if force_fetcher or capture_xhr:
        return await self._force_fetch(
            url, force_fetcher or "stealthy", extraction_type, css_selector, main_content_only,
            use_trafilatura, cache_ttl, offset, headless, real_chrome, wait,
            proxy, timeout, network_idle, solve_cloudflare, block_webrtc,
            hide_canvas, extra_headers, useragent, cookies, mc,
            capture_xhr=capture_xhr, capture_pattern=capture_pattern,
            fold_captured=fold_captured,
        )
    # 5. Reddit default: skip HTTP, go straight to stealthy. www.reddit.com
    #    JS-walls/blocks plain HTTP ~100% of the time, so the HTTP tier is
    #    ~1s of wasted time before it escalates anyway. old.reddit.com
    #    listings render fine in the stealthy browser. Saves ~1s per fetch.
    #    (An explicit force_fetcher above already returned, so this only
    #    applies to the unpinned/default case.)
    if is_reddit:
        return await self._force_fetch(
            url, "stealthy", extraction_type, css_selector, main_content_only,
            use_trafilatura, cache_ttl, offset, headless, real_chrome, wait,
            proxy, timeout, network_idle, solve_cloudflare, block_webrtc,
            hide_canvas, extra_headers, useragent, cookies, mc,
        )
    # 6. Auto-escalation (HTTP -> stealthy) for everything else
    return await self._auto_escalate(
        url, extraction_type, css_selector, main_content_only,
        use_trafilatura, cache_ttl, offset, headless, real_chrome, wait,
        proxy, timeout, network_idle, solve_cloudflare, block_webrtc,
        hide_canvas, extra_headers, useragent, cookies, mc,
        fold_captured=fold_captured,
    )


def _normalize_max_content_chars(max_content_chars):
    """Validate the token-spend ceiling and apply the hard cap.

    max_content_chars controls context spend per fetch; values below 500
    make pagination useless and the hard cap bounds a single response.
    """
    if max_content_chars is not None:
        if isinstance(max_content_chars, bool) or not isinstance(max_content_chars, int) \
                or max_content_chars < 500:
            raise ValueError("max_content_chars must be an int >= 500")
        return min(max_content_chars, 200000)
    return MAX_CONTENT_CHARS


def _has_persistent_auto_browser_context(server) -> bool:
    """Detect an auto-session profile even if its configuring env was unset later."""
    if os.environ.get("SIEVE_BROWSER_PROFILE_DIR", "").strip():
        return True
    registry = getattr(server, "_browser_sessions", None)
    if registry is None:
        return False
    sessions = getattr(registry, "_sessions", {})
    auto_ids = getattr(registry, "_auto_ids", {})
    for session_id in auto_ids.values():
        entry = sessions.get(session_id) if session_id else None
        session = getattr(entry, "session", None)
        if getattr(session, "_profile_dir", None) or getattr(session, "_cdp_url", None):
            return True
    return False


def _effective_cache_ttl(
    cache_ttl, *, actions, capture_xhr, cookies=None, extra_headers=None,
    proxy=None, useragent=None, force_fetcher=None, persistent_browser_context=False,
):
    """Force fresh fetches when the response would include non-cacheable parts.

    Context-specific fetches bypass both reads and writes because the shared
    cache does not partition their transport or browser identity. Never persist
    these values in a cache key, including hashes that could become verification
    oracles.
    """
    if (
        actions or capture_xhr or cookies or extra_headers or proxy or useragent
        or force_fetcher or persistent_browser_context
    ):
        return 0
    return cache_ttl


def _robots_blocked_response(url, extraction_type, reason: str = "robots_txt_disallowed"):
    """Envelope for a URL refused by robots.txt (respect_robots=true)."""
    if reason == "robots_unavailable":
        detail = (
            f"[Blocked by robots.txt] The robots.txt policy for '{url}' could "
            f"not be retrieved (unreachable, auth-walled, or server error). "
            f"Strict mode fails closed on an unreadable policy. Re-run when the "
            f"site is reachable, or set respect_robots=False to bypass."
        )
    else:
        detail = (
            f"[Blocked by robots.txt] The URL '{url}' is disallowed by the "
            f"site's robots.txt policy. Set respect_robots=False to bypass."
        )
    return ResponseModel(
        url=url,
        content=[detail],
        status=403, fetcher_used="none", cached=False,
        extracted_type=extraction_type, session_id="",
        duration_ms=0, error=reason,
    )


def _response_from_cache(cached, extraction_type):
    """Rebuild a ResponseModel from a cached row, restoring envelope metadata.

    Cache rows carry the envelope so hits keep metadata/links/quality_score/
    toc/page_type/source/archived_at (previously lost on cache hits).
    """
    env = cached.get("envelope") or {}
    return ResponseModel(
        url=cached["url"], status=cached["status"], content=cached["content"],
        cached=True, fetcher_used="cache", duration_ms=0,
        extracted_type=extraction_type,
        content_type=cached.get("content_type", ""),
        total_size_bytes=cached.get("total_size_bytes", 0),
        metadata=env.get("metadata", {}) or {},
        media=env.get("media", []) or [],
        links=env.get("links", {}) or {},
        quality_score=env.get("quality_score", 0.0) or 0.0,
        table_of_contents=env.get("table_of_contents", []) or [],
        page_type=env.get("page_type", "unknown") or "unknown",
        source=env.get("source", "live") or "live",
        archived_at=env.get("archived_at", "") or "",
    )


async def _smart_fetch_bulk(
    self, urls, extraction_type, css_selector, main_content_only,
    use_trafilatura, cache_ttl, force_fetcher, respect_robots,
    headless, real_chrome, wait, proxy, timeout, network_idle,
    solve_cloudflare, block_webrtc, hide_canvas, extra_headers,
    useragent, cookies, max_chars: int = MAX_CONTENT_CHARS,
    include_media: bool = False, include_links: bool = False,
    focus: Optional[str] = None,
) -> BulkResponseModel:
    """Fetch multiple URLs in parallel through the smart fetch pipeline."""
    if len(urls) > MAX_BULK_URLS:
        raise ValueError(
            f"Too many URLs ({len(urls)}). Maximum is {MAX_BULK_URLS} per call."
        )
    async def _fetch_one(u: str) -> ResponseModel:
        try:
            return await self.smart_fetch(
                url=u, extraction_type=extraction_type,
                css_selector=css_selector, main_content_only=main_content_only,
                use_trafilatura=use_trafilatura, cache_ttl=cache_ttl,
                force_fetcher=force_fetcher, respect_robots=respect_robots,
                headless=headless, real_chrome=real_chrome, wait=wait,
                proxy=proxy, timeout=timeout, network_idle=network_idle,
                solve_cloudflare=solve_cloudflare, block_webrtc=block_webrtc,
                hide_canvas=hide_canvas, extra_headers=extra_headers,
                useragent=useragent, cookies=cookies,
                max_content_chars=max_chars,
                include_media=include_media, include_links=include_links,
                focus=focus,
            )
        except Exception as e:
            error = _safe_fetch_error(e)
            return _with_agent_hints(ResponseModel(
                url=u, status=0, content=[f"[Error: {error}]"],
                fetcher_used="none", error=error,
            ))
    # Small delay between URL batches to avoid hammering the same server
    results = []
    batch_size = 10
    for i in range(0, len(urls), batch_size):
        batch = urls[i:i + batch_size]
        batch_results = await gather(*[_fetch_one(u) for u in batch])
        results.extend(batch_results)
        if i + batch_size < len(urls):
            await asyncio_sleep(0.5)
    successful = sum(1 for r in results if r.status > 0 and r.status < 400 and not r.error)
    return BulkResponseModel(results=results, total=len(results), successful=successful)


async def _force_fetch(
    self, url, force_fetcher, extraction_type, css_selector,
    main_content_only, use_trafilatura, cache_ttl, offset,
    headless, real_chrome, wait, proxy, timeout, network_idle,
    solve_cloudflare, block_webrtc, hide_canvas, extra_headers,
    useragent, cookies, max_chars: int = MAX_CONTENT_CHARS,
    page_action=None, capture_xhr: bool = False,
    capture_pattern: Optional[str] = None, fold_captured: bool = False,
) -> ResponseModel:
    """Execute a forced fetcher tier and finalize the result."""
    # HTTP fetcher takes seconds; browser timeout is ms. Cap at 30s.
    http_timeout = max(1, min(int(timeout / 1000), 30))
    if force_fetcher == "sleeper":
        # Sleeper is a synchronous local-daemon bridge; keep its blocking
        # urllib call off the event loop while preserving the normal response
        # envelope and cache/quality handling.
        from sieve.sleeper_bridge import sleeper_fetch
        raw = await to_thread(sleeper_fetch, url, timeout=http_timeout)
        if raw.get("ok"):
            value = raw.get("text", "")
            if not value and raw.get("extracted") is not None:
                value = str(raw["extracted"])
            result = ResponseModel(
                url=raw.get("url", url), status=200,
                content=[str(value)] if str(value).strip() else [],
                fetcher_used="sleeper", extracted_type=extraction_type,
                content_type="text/plain", total_size_bytes=len(str(value).encode("utf-8")),
            )
        else:
            result = ResponseModel(
                url=raw.get("url", url), status=0, content=[],
                fetcher_used="sleeper", extracted_type=extraction_type,
                error=_safe_fetch_error(fallback_category="network"),
            )
        result.escalation_path = "direct:sleeper"
        return await self._finalize_result(result, url, extraction_type, css_selector, cache_ttl, offset, max_chars)
    if force_fetcher == "http":
        http_cookies = _safe_cookie_dict(cookies)
        result = await self.get(
            url, extraction_type=extraction_type, css_selector=css_selector,
            main_content_only=main_content_only, use_trafilatura=use_trafilatura,
            proxy=proxy if isinstance(proxy, str) else None,
            headers=extra_headers, cookies=http_cookies, timeout=http_timeout,
            stealthy_headers=True,
        )
        result.escalation_path = "direct:http"
        return await self._finalize_result(result, url, extraction_type, css_selector, cache_ttl, offset, max_chars)
    else:  # stealthy ("dynamic" also routes here â Patchright handles everything)
        # Playwright fixes the proxy when the browser context starts.
        # The shared auto-session is direct, so a proxied request must use
        # the one-off path where stealthy_fetch constructs the browser
        # with the requested proxy.
        ssid = None if proxy else await self._ensure_auto_session("stealthy")
        # Interactions (page_action) depend on real layout: scroll thresholds
        # and element visibility break when stylesheets are blocked, so the
        # loaders that fire page 2+ never trigger. Keep resources for the
        # interactive path; block them otherwise (faster, we strip them
        # anyway).
        result = await self.stealthy_fetch(
            url, extraction_type=extraction_type,
            css_selector=css_selector, main_content_only=main_content_only,
            use_trafilatura=use_trafilatura, headless=headless,
            real_chrome=real_chrome, wait=wait, proxy=proxy,
            timeout=timeout, network_idle=network_idle,
            disable_resources=(page_action is None),
            solve_cloudflare=solve_cloudflare, block_webrtc=block_webrtc,
            hide_canvas=hide_canvas, extra_headers=extra_headers,
            useragent=useragent, cookies=cookies,
            session_id=ssid,
            page_action=page_action,
            capture_xhr=capture_xhr, capture_pattern=capture_pattern,
        )
        result.escalation_path = "direct:stealthy"
        if capture_xhr:
            _apply_capture_verdict(result, fold_captured)
        return await self._finalize_result(result, url, extraction_type, css_selector, cache_ttl, offset, max_chars)


async def _capture_pass(
    self, url, extraction_type, css_selector, main_content_only,
    use_trafilatura, cache_ttl, offset, headless, real_chrome, wait,
    proxy, timeout, network_idle, solve_cloudflare, block_webrtc,
    hide_canvas, extra_headers, useragent, cookies, max_chars,
    fold_captured: bool = False,
) -> Optional[ResponseModel]:
    """Re-fetch an AJAX shell through the browser, capturing its XHRs.
    Returns a finalized result when the pass produced usable fragments, or
    None when it did not (browser unavailable, crash, nothing captured) so
    the caller can fall back to the HTTP-tier result it already has.
    """
    if not _browser_deps_available():
        return None
    try:
        ssid = None if proxy else await self._ensure_auto_session("stealthy")
        result = await self.stealthy_fetch(
            url, extraction_type=extraction_type,
            css_selector=css_selector, main_content_only=main_content_only,
            use_trafilatura=use_trafilatura, headless=headless,
            real_chrome=real_chrome, wait=wait, proxy=proxy,
            timeout=timeout, network_idle=True,
            disable_resources=True,
            solve_cloudflare=solve_cloudflare, block_webrtc=block_webrtc,
            hide_canvas=hide_canvas, extra_headers=extra_headers,
            useragent=useragent, cookies=cookies, session_id=ssid,
            capture_xhr=True,
        )
    except Exception as e:
        logger.debug("XHR capture pass failed for %s: %s", redact_url_for_logs(url), e)
        return None
    if not (result.network or {}).get("fragments"):
        return None
    result.escalation_path = "httpâstealthy(capture)"
    _apply_capture_verdict(result, fold_captured, shell_detected=True)
    # Shell results carry an error by design, which keeps them out of the
    # cache â the next call re-runs the capture instead of serving
    # boilerplate for the whole TTL.
    return await self._finalize_result(
        result, url, extraction_type, css_selector, cache_ttl, offset, max_chars,
    )


async def _auto_escalate(
    self, url, extraction_type, css_selector, main_content_only,
    use_trafilatura, cache_ttl, offset, headless, real_chrome, wait,
    proxy, timeout, network_idle, solve_cloudflare, block_webrtc,
    hide_canvas, extra_headers, useragent, cookies, max_chars: int = MAX_CONTENT_CHARS,
    fold_captured: bool = False,
) -> ResponseModel:
    """Auto-escalation: try HTTP first, fall back to stealthy if it fails.
    Two tiers. No domain intel routing. No dynamic tier.
    HTTP is fast (~1s). Stealthy (Patchright) handles everything else.
    If HTTP succeeds, fire background pre-warm so stealthy is ready
    for the next call that needs it.
    """
    start_time = now()
    errors = []
    http_cookies = _safe_cookie_dict(cookies)
    # HTTP fetcher takes seconds; browser timeout is ms. Cap at 30s.
    http_timeout = max(1, min(int(timeout / 1000), 30))
    # Tier 1: HTTP (always try first â it's fast)
    result = await self._http_with_retry(
        url, extraction_type=extraction_type,
        css_selector=css_selector, main_content_only=main_content_only,
        use_trafilatura=use_trafilatura,
        proxy=proxy if isinstance(proxy, str) else None,
        headers=extra_headers, cookies=http_cookies, stealthy_headers=True,
        timeout=http_timeout,
    )
    elapsed = (now() - start_time) * 1000
    result.duration_ms = elapsed
    # PDF-intent URLs (.pdf) are binary; never escalate to a JS browser
    # (a stealthy render of a PDF URL is always wasted, and the body is
    # either %PDF or a login/error redirect handled in _translate_response).
    if url.lower().split('?')[0].endswith('.pdf'):
        result.escalation_path = "direct:http"
        return await self._finalize_result(result, url, extraction_type, css_selector, cache_ttl, offset, max_chars)
    # Accept if status is OK and content is real (not a JS shell).
    # NOTE: status 0 = transport-level failure (DNS/TLS/reset/timeout),
    # NOT success - `0 < 400` must never accept a network error.
    if 200 <= result.status < 400 and not _is_js_shell(result):
        # The HTTP tier "succeeded" but the page is a shell whose panels are
        # filled over XHR: the text we have is boilerplate. Re-run through the
        # browser capturing XHR so the data is recoverable. Only worth it when
        # a browser is available; otherwise fall through with the honest error.
        if _AJAX_SHELL.get():
            captured_result = await self._capture_pass(
                url, extraction_type, css_selector, main_content_only,
                use_trafilatura, cache_ttl, offset, headless, real_chrome,
                wait, proxy, timeout, network_idle, solve_cloudflare,
                block_webrtc, hide_canvas, extra_headers, useragent,
                cookies, max_chars, fold_captured,
            )
            if captured_result is not None:
                return captured_result
            result.error = result.error or (_AJAX_SHELL_ERROR + "; no data-bearing XHR was captured")
        result.escalation_path = "direct:http"
        return await self._finalize_result(result, url, extraction_type, css_selector, cache_ttl, offset, max_chars)
    # Should we escalate? Stealthy browser can genuinely help for:
    # 1. Status 200 with JS shell -> page needs a real browser
    # 2. Status 403 or 503 -> explicit bot block / bot challenge
    # 3. Status 429 -> rate limited; stealthy has a different fingerprint
    # 4. Status 500/502 -> server error; may be intermittent or bot-related
    # 5. Status 0 (transport failure: TLS fingerprint block, reset,
    #    timeout) -> the browser's own network stack may succeed where
    #    raw HTTP was blocked. EXCEPT deterministic failures a browser
    #    cannot fix either (DNS, refused connection).
    # NOT for 401/407 (auth needed, not bot), 404/410 (page gone, stealthy
    # gets the same 404), 451 (legal block), 400 (bad request).
    should_escalate = (
        (result.status < 400 and result.status > 0 and _is_js_shell(result))
        or result.status in (403, 429, 500, 502, 503)
        or (result.status == 0 and not _is_deterministic_net_error(result))
    )
    if not should_escalate:
        result.duration_ms = elapsed
        return await self._finalize_result(result, url, extraction_type, css_selector, cache_ttl, offset, max_chars)
    # Tier 2: Stealthy browser
    # Skip if browser deps are unavailable (HTTP-only mode)
    if not _browser_deps_available():
        result.duration_ms = elapsed
        result.escalation_path = "http(browser_unavailable)"
        if result.error:
            result.error += "; browser_unavailable"
        else:
            result.error = f"browser_unavailable: http status {result.status}, stealthy escalation skipped"
        return await self._finalize_result(result, url, extraction_type, css_selector, cache_ttl, offset, max_chars)
    errors.append(f"HTTP failed (status {result.status})")
    remaining = max(timeout - int((now() - start_time) * 1000), 5000)
    # If the HTTP tier already looked like an AJAX shell, capture during the
    # escalation fetch we are about to make anyway rather than paying for a
    # second browser round-trip afterwards.
    http_flagged_shell = _AJAX_SHELL.get()
    try:
        # Playwright fixes the proxy when the browser context starts. Do not
        # route a proxied request through the shared direct auto-session.
        ssid = None if proxy else await self._ensure_auto_session("stealthy")
        result = await self.stealthy_fetch(
            url, extraction_type=extraction_type,
            css_selector=css_selector, main_content_only=main_content_only,
            use_trafilatura=use_trafilatura, headless=headless,
            real_chrome=real_chrome, wait=wait, proxy=proxy,
            timeout=remaining, network_idle=network_idle,
            disable_resources=True,
            solve_cloudflare=solve_cloudflare, block_webrtc=block_webrtc,
            hide_canvas=hide_canvas, extra_headers=extra_headers,
            useragent=useragent, cookies=cookies,
            session_id=ssid,
            capture_xhr=http_flagged_shell,
        )
    except Exception as e:
        # A browser crash (session launch failure, patchright error)
        # must surface as the structured all-tiers-failed envelope,
        # not as a raw exception that loses the HTTP-tier context.
        result = ResponseModel(
            url=url, status=0, content=[],
            fetcher_used="stealthy",
            error=_safe_fetch_error(e, fallback_category="network"),
        )
    elapsed = (now() - start_time) * 1000
    result.duration_ms = elapsed
    if 200 <= result.status < 400 and not _is_js_shell(result):
        # _AJAX_SHELL now reflects the rendered page: a browser render can
        # still be a shell when the panels are filled by later XHRs.
        rendered_shell = _AJAX_SHELL.get()
        if (result.network or {}).get("fragments"):
            result.escalation_path = "httpâstealthy(capture)"
            _apply_capture_verdict(
                result, fold_captured,
                shell_detected=rendered_shell or http_flagged_shell,
            )
            return await self._finalize_result(result, url, extraction_type, css_selector, cache_ttl, offset, max_chars)
        if rendered_shell:
            captured_result = await self._capture_pass(
                url, extraction_type, css_selector, main_content_only,
                use_trafilatura, cache_ttl, offset, headless, real_chrome,
                wait, proxy, timeout, network_idle, solve_cloudflare,
                block_webrtc, hide_canvas, extra_headers, useragent,
                cookies, max_chars, fold_captured,
            )
            if captured_result is not None:
                return captured_result
            # Optional final tier: an operator may explicitly enable
            # real-browser escalation after a headless JS-shell verdict.
            from sieve.config import get as _cfg
            auto_sleeper = _cfg("sleeper_auto_escalate", False, env="SIEVE_SLEEPER_AUTO_ESCALATE")
            if str(auto_sleeper).lower() in ("1", "true", "yes", "on"):
                from sieve.sleeper_bridge import is_available, sleeper_fetch
                if await to_thread(is_available):
                    sleeper_result = await to_thread(sleeper_fetch, url, timeout=max(5, int(remaining / 1000)))
                    if sleeper_result.get("ok"):
                        text = str(sleeper_result.get("text") or "")
                        result = ResponseModel(
                            url=sleeper_result.get("url", url), status=200,
                            content=[text] if text.strip() else [],
                            fetcher_used="sleeper", extracted_type=extraction_type,
                            content_type="text/plain", total_size_bytes=len(text.encode("utf-8")),
                            escalation_path="http→stealthy→sleeper",
                        )
                        return await self._finalize_result(result, url, extraction_type, css_selector, cache_ttl, offset, max_chars)
        result.escalation_path = "httpâstealthy"
        return await self._finalize_result(result, url, extraction_type, css_selector, cache_ttl, offset, max_chars)
    # All tiers failed
    errors.append(f"Stealthy failed (status {result.status})")
    result.content = [
        f"[All fetch tiers failed for {url}]\n"
        f"Attempted: HTTP â Stealthy\n"
        f"Failures: {'; '.join(errors)}\n"
        f"Final status: {result.status}\n"
        f"\n"
        f"Tips:\n"
        f"- If the site uses Cloudflare Turnstile or DataDome, no free tool can bypass it.\n"
        f"- Try a different URL on the same domain (some paths have lower protection).\n"
        f"- Set solve_cloudflare=True (already tried).\n"
        f"- Try with a proxy via the proxy parameter."
    ]
    result.escalation_path = "httpâstealthy(all_failed)"
    result.retry_count = 2
    result.duration_ms = elapsed
    result.error = f"all_tiers_failed: HTTP status {result.status}"
    return await self._finalize_result(result, url, extraction_type, css_selector, cache_ttl, offset, max_chars)
