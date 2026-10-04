"""Sieve's own HTTP fetcher and Response class.

Replaces scrapling's FetcherSession (which wraps curl_cffi) with a direct
primp-based implementation. primp provides the same TLS impersonation
(JA3/JA4 fingerprinting, HTTP/2 settings randomization) as curl_cffi but
with a cleaner API and no C dependency beyond what's already installed.

The Response class mimics scrapling's Response interface: .status, .url,
.headers, .body (bytes), .encoding, .content (decoded HTML), .css() (CSS
selector via lxml), .reason, .cookies.
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Any, Dict, List, Optional, Union
from urllib.parse import urljoin, urlparse

import primp

from sieve import content_type as sieve_content_type
from sieve.security import redact_url_for_logs, resolve_and_check, validate_url
from sieve.resource_budget import BudgetExceeded, current_budget

logger = logging.getLogger("sieve.fetcher")


def _close_response(response) -> None:
    close = getattr(response, "close", None)
    if close is not None:
        close()


def _read_budgeted_body(response, account) -> bytes:
    """Debit bounded stream chunks before retaining them; never parse a partial body.

    Native transport overhead is at most one 64 KiB chunk beyond the account.
    Small accounts request only their remaining allowance plus one sentinel.
    """
    body = bytearray()
    size = min(65_536, account.remaining("input_bytes") + 1)
    for chunk in response.iter_bytes(size):
        if not account.charge("input_bytes", len(chunk)):
            raise BudgetExceeded("Request input budget exceeded.")
        body.extend(chunk)
    return bytes(body)


# ─── Response ────────────────────────────────────────────────────────────────

class ElementWrapper:
    """Wraps an lxml element to mimic scrapling's Selector element interface.

    Exposes ._root (the lxml node) so trafilatura_extractor's
    CSS-selector narrowing path (tostring(el._root)) keeps working.
    """

    __slots__ = ("_root", "_url")

    def __init__(self, root, url: str = ""):
        self._root = root
        self._url = url

    @property
    def url(self) -> str:
        return self._url

    def css(self, selector: str) -> List["ElementWrapper"]:
        """CSS selector query on this element's subtree."""
        from lxml.cssselect import CSSSelector
        try:
            sel = CSSSelector(selector)
            matches = sel(self._root)
            return [ElementWrapper(m, self._url) for m in matches]
        except Exception:
            return []

    def text_content(self) -> str:
        """Get all text content from this element (full subtree,
        matching lxml's text_content() semantics)."""
        return self._root.text_content() if hasattr(self._root, "text_content") else (self._root.text or "")


class Response:
    """HTTP response with CSS selector support.

    Mimics scrapling's Response interface used throughout server.py:
        .status (int)          - HTTP status code
        .url (str)             - final URL after redirects
        .headers (dict)        - response headers
        .body (bytes)          - raw response body
        .encoding (str)        - detected encoding
        .content (str)         - decoded body (lazy)
        .css(selector) -> list - CSS selector query
        .reason (str)          - status text
        .cookies (dict)        - response cookies
    """

    __slots__ = (
        "_status", "_url", "_headers", "_body", "_encoding",
        "_reason", "_cookies", "_root", "_content_cached", "_captured", "action_results",
        "_input_account",
    )

    def __init__(
        self,
        url: str,
        body: bytes,
        status: int,
        headers: Optional[Dict[str, str]] = None,
        encoding: str = "utf-8",
        reason: str = "",
        cookies: Optional[Dict[str, str]] = None,
        captured: Optional[List[Dict[str, Any]]] = None,
    ):
        self._url = url
        self._body = body if isinstance(body, bytes) else (body or b"")
        self._status = status
        self._headers = headers or {}
        self._encoding = encoding or "utf-8"
        self._reason = reason or ""
        self._cookies = cookies or {}
        self._root: Any = None  # lazy lxml tree
        self._content_cached: Optional[str] = None
        # XHR/fetch bodies grabbed during a browser fetch (capture_xhr).
        # Empty for the HTTP tier, which sees only the document itself.
        self._captured: List[Dict[str, Any]] = captured or []
        self.action_results = []
        self._input_account = None

    # ── Properties matching scrapling's interface ──────────────────

    @property
    def status(self) -> int:
        return self._status

    @property
    def url(self) -> str:
        return self._url

    @property
    def headers(self) -> Dict[str, str]:
        return self._headers

    @property
    def body(self) -> bytes:
        return self._body

    @property
    def encoding(self) -> str:
        return self._encoding

    @property
    def reason(self) -> str:
        return self._reason

    @property
    def cookies(self) -> Dict[str, str]:
        return self._cookies

    @property
    def captured(self) -> List[Dict[str, Any]]:
        """XHR/fetch fragments captured during a browser fetch (may be empty)."""
        return self._captured

    @property
    def content(self) -> str:
        """Decoded body (lazy, cached)."""
        if self._content_cached is None:
            self._content_cached = self._body.decode(
                self._encoding, errors="replace"
            )
        return self._content_cached

    @property
    def html_content(self) -> str:
        """Alias for .content (scrapling compat)."""
        return self.content

    # ── CSS selector support ───────────────────────────────────────

    def _ensure_parsed(self):
        """Lazily parse the body into an lxml tree."""
        if self._root is not None:
            return
        if not self._body:
            from lxml import etree
            self._root = etree.fromstring(b"<html></html>")
            return
        try:
            from io import BytesIO
            from lxml import html as lxml_html
            # Use lxml.html.parse() from BytesIO for cross-platform
            # consistency. lxml.html.fromstring() returns different root
            # elements on different platforms (e.g. ubuntu CI returns the
            # first child element, not the <html> root), which breaks
            # CSSSelector (it only searches descendants, not the root).
            # parse() always returns a full tree with <html> as root.
            tree = lxml_html.parse(BytesIO(self._body))
            self._root = tree.getroot()
            if self._root is None:
                raise ValueError("parse returned empty tree")
        except Exception:
            try:
                from lxml import html as lxml_html
                self._root = lxml_html.fromstring(self.content)
            except Exception:
                from lxml import etree
                self._root = etree.fromstring(b"<html></html>")

    def css(self, selector: str) -> List[ElementWrapper]:
        """CSS selector query. Returns list of ElementWrapper objects.

        Each ElementWrapper has ._root (lxml element) so callers can do
        tostring(el._root, encoding='unicode') to get the HTML.
        """
        self._ensure_parsed()
        if self._root is None:
            return []
        from lxml.cssselect import CSSSelector
        sel = CSSSelector(selector)
        matches = sel(self._root)
        return [ElementWrapper(m, self._url) for m in matches]

    @property
    def first(self) -> "Response":
        """Scrapling compat: .css('body').first returns self-like or empty."""
        return self

    def get_all_text(self, strip=False, ignore_tags=()) -> str:
        """Get all text content, optionally stripping whitespace and ignoring tags."""
        self._ensure_parsed()
        if self._root is None:
            return ""
        try:
            # Collect text from all elements, skipping ignored tags
            tags_to_skip = set(ignore_tags) if ignore_tags else set()
            texts = []
            for el in self._root.iter():
                tag = el.tag if isinstance(el.tag, str) else ""
                if tag in tags_to_skip:
                    continue
                if el.text:
                    texts.append(el.text)
                if el.tail:
                    texts.append(el.tail)
            result = " ".join(texts)
            if strip:
                result = result.strip()
            return result
        except Exception:
            return self.content


# ─── Browser response builder ─────────────────────────────────────────────────

async def response_from_browser_page(
    page: Any,
    first_response: Any,
    final_response: Optional[Any],
    captured: Optional[List[Dict[str, Any]]] = None,
) -> Response:
    """Build a Response from a patchright/playwright page + response objects.

    Mirrors scrapling's ResponseFactory.from_async_playwright_response but
    without the Selector/parser overhead. Gets the page content (with the
    Windows page.content() retry workaround), response headers, status, etc.
    """
    # Get page content with Windows retry workaround
    # (Playwright has a known issue with page.content() on Windows:
    #  https://github.com/microsoft/playwright/issues/16108)
    page_content = b""
    for _ in range(20):
        try:
            html_str = await page.content()
            if html_str:
                page_content = html_str.encode("utf-8")
                break
        except Exception:
            await page.wait_for_timeout(500)

    # Determine the final response (fall back to first if no final)
    resp = final_response if final_response else first_response
    if resp is None:
        return Response(
            url=page.url if page else "",
            body=page_content,
            status=0,
            headers={},
            encoding="utf-8",
            captured=captured,
        )

    # Extract headers
    try:
        headers = await resp.all_headers()
    except Exception:
        headers = {}

    # Extract encoding from content-type
    ct = headers.get("content-type", "")
    encoding = _extract_encoding(ct)

    # Extract status
    status = resp.status

    # Extract cookies from context
    cookies: Dict[str, str] = {}
    try:
        cookie_list = await page.context.cookies()
        for c in cookie_list:
            cookies[c.get("name", "")] = c.get("value", "")
    except Exception:
        pass

    return Response(
        url=page.url if page else (resp.url if hasattr(resp, "url") else ""),
        body=page_content,
        status=status,
        headers=headers,
        encoding=encoding,
        cookies=cookies,
        captured=captured,
    )


def _extract_encoding(content_type: str) -> str:
    """Extract charset from a content-type header (canonical parser, #244)."""
    return sieve_content_type.charset_of(content_type) or "utf-8"


# ─── HTTP fetcher (primp-based) ────────────────────────────────────────────────

# Default impersonation targets for the primp client
# primp supports: chrome, safari, firefox, edge, random
# (primp falls back to 'random' for unknown targets)
_IMPERSONATE_POOL = [
    "chrome",
    "safari",
    "firefox",
    "edge",
]

# OS targets for primp's impersonate_os. Picked from the same set as
# browserforge's HeaderGenerator os param — keeps TLS fingerprint and
# HTTP headers telling the same story (Obscura PORTS.md: "consistent-profile
# fingerprinting — TLS ClientHello + GREASE + UA + WebGL seeded from one
# profile, applied to navigation AND subresources").
_OS_POOL = [
    "windows",
    "macos",
    "linux",
]
_OS_TO_PLATFORM = {"windows": "Windows", "macos": "macOS", "linux": "Linux"}
# Header validation (issue #126): CR/LF and other control chars enable header
# injection; NUL breaks downstream C stacks.
_HEADER_BAD_CHARS = re.compile(r"[\x00-\x1f\x7f]")
_DEFAULT_USER_AGENTS = {"windows": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/131.0.0.0 Safari/537.36", "macos": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/131.0.0.0 Safari/537.36", "linux": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/131.0.0.0 Safari/537.36"}

# Stable profile default: one OS per process/session (fingerprint coherence).
# Obscura concept — coherence beats rotation. Rotation is opt-in via rotate_os=True.
# Lock-guarded (#197): concurrent first-picks must not race the lazy init;
# tests reset through the same lock so mutation stays coherent.
_STABLE_OS: Optional[str] = None
_STABLE_OS_LOCK = __import__("threading").Lock()


def _pick_os(rotate: bool = False) -> str:
    global _STABLE_OS
    if len(_OS_POOL) == 1:
        return _OS_POOL[0]
    if rotate:
        import random
        return random.choice(_OS_POOL)
    with _STABLE_OS_LOCK:
        if _STABLE_OS is None:
            import random
            _STABLE_OS = random.choice(_OS_POOL)
        return _STABLE_OS


class HTTPSession:
    """Async HTTP fetch session using primp for TLS impersonation.

    Replaces scrapling's FetcherSession. Provides:
    - TLS fingerprint impersonation (Chrome, Firefox, Safari, Edge)
    - Proxy support (http, https, socks5)
    - Retry with backoff
    - Stealthy headers (Google referer, realistic User-Agent)

    Usage:
        async with HTTPSession(impersonate="chrome") as session:
            response = await session.get("https://example.com")
    """

    def __init__(
        self,
        impersonate: Union[str, List[str]] = "chrome",
        proxy: Optional[str] = None,
        stealthy_headers: bool = True,
        retries: int = 1,
        retry_delay: float = 1.0,
        timeout: int = 30,
        rotate_os: bool = False,
        rng=None,
    ):
        # Construction-time bounds (issue #125): negative or absurd retry/
        # timeout values must fail here, not at request time where they turn
        # into unbounded work or instant failures.
        if isinstance(retries, bool) or not isinstance(retries, int) or not 0 <= retries <= 10:
            raise ValueError("retries must be an integer between 0 and 10")
        if isinstance(retry_delay, bool) or not isinstance(retry_delay, (int, float)) or not 0 <= retry_delay <= 60:
            raise ValueError("retry_delay must be a number between 0 and 60")
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not 1 <= timeout <= 300:
            raise ValueError("timeout must be a number between 1 and 300 seconds")
        self._impersonate = impersonate
        self._proxy = proxy
        self._stealthy_headers = stealthy_headers
        self._retries = retries
        self._retry_delay = retry_delay
        self._timeout = timeout
        self._rotate_os = rotate_os
        from sieve.session_coherence import rng_scope
        with rng_scope(rng) as owned_rng:
            self._rng = owned_rng
        self._client: Optional[primp.Client] = None

    async def __aenter__(self) -> "HTTPSession":
        await self._init_client()
        return self

    async def __aexit__(self, *exc) -> None:
        await self.close()

    async def _init_client(self) -> None:
        """Initialize the primp client in a worker thread (import is ~1s)."""
        impersonate = self._impersonate
        if isinstance(impersonate, list):
            # primp doesn't support rotation pools like scrapling's curl_cffi.
            # Pick a random one from the pool.
            impersonate = self._rng.choice(impersonate)

        # Stable OS profile: one coherent OS per process (opt-in rotation)
        self._os = self._rng.choice(_OS_POOL)

        def _create():
            kwargs: Dict[str, Any] = {
                "impersonate": impersonate,
                "impersonate_os": self._os,
            }
            if self._proxy:
                kwargs["proxy"] = self._proxy
            return primp.Client(**kwargs)

        self._client = await asyncio.to_thread(_create)

    async def close(self) -> None:
        """Close the underlying client."""
        # primp.Client doesn't have an explicit close, but we drop the reference
        self._client = None

    def _build_headers(
        self, headers: Optional[Dict[str, str]] = None
    ) -> Dict[str, str]:
        """Build request headers with stealthy defaults."""
        final_headers: Dict[str, str] = {}
        if self._stealthy_headers:
            final_headers["referer"] = "https://www.google.com/"
            try:
                from browserforge.headers import HeaderGenerator
                hg = HeaderGenerator()
                # Coherent OS: browserforge's UA and sec-ch-ua-platform must
                # match primp's TLS fingerprint (Obscura consistent-profile).
                generated = hg.generate(browser="chrome", os=self._os)
                # Merge browserforge headers (lowercase keys)
                for k, v in generated.items():
                    if k.lower() not in ("referer",):
                        final_headers.setdefault(k.lower(), v)
            except Exception:
                pass
        final_headers.setdefault("sec-ch-ua-platform", f'"{_OS_TO_PLATFORM[self._os]}"')
        final_headers.setdefault("user-agent", _DEFAULT_USER_AGENTS[self._os])
        # User-supplied headers override defaults, after validation (issue
        # #126): control characters in names/values enable header injection,
        # and unbounded counts/lengths are a request-smuggling vector.
        if headers:
            if len(headers) > 64:
                raise ValueError("too many custom headers (max 64)")
            for k, v in headers.items():
                if not isinstance(k, str) or not k.strip() or len(k) > 256:
                    raise ValueError(f"invalid header name: {k!r}")
                if not isinstance(v, str) or len(v) > 8192:
                    raise ValueError(f"invalid header value for {k!r}")
                if _HEADER_BAD_CHARS.search(k) or _HEADER_BAD_CHARS.search(v):
                    raise ValueError(f"header {k!r} contains control characters")
                final_headers[k] = v
        return final_headers

    async def get(
        self,
        url: str,
        *,
        headers: Optional[Dict[str, str]] = None,
        cookies: Optional[Dict[str, str]] = None,
        timeout: Optional[int] = None,
        retries: Optional[int] = None,
        proxy: Optional[str] = None,
        follow_redirects: Union[bool, str] = True,
        max_redirects: int = 5,
        params: Optional[Dict[str, str]] = None,
        **kwargs: Any,
    ) -> Response:
        """Fetch a URL via HTTP with TLS impersonation.

        Returns a Response object.
        """
        # Coerce follow_redirects from scrapling-style string to bool.
        # Scrapling accepted: "safe", "always", "never". primp expects bool.
        if isinstance(follow_redirects, str):
            follow_redirects = follow_redirects.lower() != "never"

        if self._client is None:
            await self._init_client()

        client = self._client
        if proxy:
            # Override proxy for this request: create a new client
            def _create_proxy_client():
                impersonate = self._impersonate
                if isinstance(impersonate, list):
                    impersonate = self._rng.choice(impersonate)
                return primp.Client(impersonate=impersonate, impersonate_os=self._os, proxy=proxy)
            client = await asyncio.to_thread(_create_proxy_client)

        final_headers = self._build_headers(headers)
        if cookies:
            # Cookie encoding policy (issue #126): validate names/values for
            # control characters and separators that would smuggle extra
            # cookie pairs, and bound the count.
            if len(cookies) > 64:
                raise ValueError("too many cookies (max 64)")
            parts = []
            for k, v in cookies.items():
                if not isinstance(k, str) or not isinstance(v, str):
                    raise ValueError("cookie names and values must be strings")
                if not k.strip() or len(k) > 256 or len(v) > 4096:
                    raise ValueError(f"invalid cookie name/value: {k!r}")
                if _HEADER_BAD_CHARS.search(k) or _HEADER_BAD_CHARS.search(v) or ";" in k or "=" in k or ";" in v:
                    raise ValueError(f"cookie {k!r} contains invalid characters")
                parts.append(f"{k}={v}")
            final_headers["cookie"] = "; ".join(parts)

        actual_timeout = timeout or self._timeout
        actual_retries = retries if retries is not None else self._retries
        account = current_budget()
        if account is not None:
            if account.expired or account.remaining("input_bytes") == 0:
                account.charge("input_bytes", 1)
                raise BudgetExceeded("Request input or deadline budget exceeded.")
            actual_timeout = min(actual_timeout, account.time_remaining)

        # Unified SSRF deny — primary + fallback branches route every target
        # through resolve_and_check via validate_url + explicit resolve_and_check on the
        # initial host. Browser tier hook: call resolve_and_check(host) before page.goto().
        parsed0 = urlparse(url)
        if parsed0.hostname:
            resolve_and_check(parsed0.hostname, parsed0.port or (443 if parsed0.scheme == "https" else 80))
        url = validate_url(url)

        last_error: Optional[Exception] = None
        for attempt in range(actual_retries + 1):
            resp = None
            try:
                # primp's get() is synchronous, wrap in to_thread. Redirects
                # are followed manually so every Location target is passed
                # through the SSRF validator before the next request.
                def _do_get(active_client):
                    from urllib.parse import urlencode
                    req_url = url
                    if params:
                        separator = "&" if "?" in req_url else "?"
                        req_url = f"{req_url}{separator}{urlencode(params)}"
                    redirects = 0
                    while True:
                        resp = active_client.get(
                            req_url,
                            headers=final_headers,
                            timeout=min(actual_timeout, account.time_remaining) if account is not None else actual_timeout,
                            follow_redirects=False,
                            **({"stream": True} if account is not None else {}),
                        )
                        if not follow_redirects or resp.status_code not in (301, 302, 303, 307, 308):
                            return resp
                        location = (dict(resp.headers).get("location") or "").strip()
                        if not location:
                            return resp
                        if redirects >= max_redirects:
                            _close_response(resp)
                            raise RuntimeError(f"Too many redirects for {url}")
                        try:
                            req_url = validate_url(urljoin(req_url, location))
                        finally:
                            _close_response(resp)
                        redirects += 1

                resp = await asyncio.to_thread(_do_get, client)
                # oc-style chrome→firefox retry: Reddit etc 403 on chrome but accept firefox
                status_code = getattr(resp, "status_code", 0) or 0
                if status_code in (403, 429) and self._impersonate == "chrome" and attempt == 0:
                    try:
                        fallback_proxy = proxy or self._proxy
                        fallback = await asyncio.to_thread(lambda fallback_proxy=fallback_proxy: primp.Client(impersonate="firefox", impersonate_os=self._os, **({"proxy": fallback_proxy} if fallback_proxy else {})))
                        def _retry_get():
                            return _do_get(fallback)
                        retry_resp = await asyncio.to_thread(_retry_get)
                        retry_status = getattr(retry_resp, "status_code", 0) or 0
                        # Keep the fallback only when it actually succeeded:
                        # a worse status (500/404) must never replace the
                        # original response's status/body downstream.
                        if retry_status < 400:
                            _close_response(resp)
                            resp = retry_resp
                        else:
                            _close_response(retry_resp)
                    except Exception as exc:
                        # #139 rationale: optional Firefox retry failures retain
                        # the original response; redirect validation still applies.
                        logger.debug("chrome->firefox fallback failed for %s: %s", redact_url_for_logs(url), exc)

                # Parse encoding from content-type
                # Normalize header names to lowercase: primp/servers vary in
                # casing, and downstream detection (JSON/PDF/image) does
                # exact 'content-type' lookups.
                resp_headers = {str(k).lower(): v for k, v in (dict(resp.headers) if hasattr(resp, "headers") else {}).items()}
                ct = ""
                for k, v in resp_headers.items():
                    if k.lower() == "content-type":
                        ct = v
                        break
                encoding = _extract_encoding(ct)

                # Parse cookies
                resp_cookies: Dict[str, str] = {}
                if hasattr(resp, "cookies"):
                    try:
                        for c in resp.cookies:
                            if isinstance(c, dict):
                                resp_cookies[c.get("name", "")] = c.get("value", "")
                            elif hasattr(c, "name"):
                                resp_cookies[c.name] = c.value
                    except Exception:
                        pass

                body = await asyncio.to_thread(_read_budgeted_body, resp, account) if account is not None else (resp.content if hasattr(resp, "content") else b"")
                result = Response(
                    url=str(resp.url) if hasattr(resp, "url") else url,
                    body=body,
                    status=resp.status_code if hasattr(resp, "status_code") else 0,
                    headers=resp_headers,
                    encoding=encoding,
                    reason=resp.reason if hasattr(resp, "reason") else "",
                    cookies=resp_cookies,
                )
                result._input_account = account
                return result

            except BudgetExceeded:
                raise
            except Exception as e:
                # #139 rationale: transport failures retry within the shared
                # retry/deadline limits; exhausted attempts propagate to the caller.
                last_error = e
                if attempt < actual_retries:
                    if account is not None and not account.charge("retries", 1):
                        raise BudgetExceeded("Request retry budget exceeded.") from e
                    logger.warning(
                        f"HTTP fetch attempt {attempt + 1} failed for {url}: {str(e)[:200]}. "
                        f"Retrying in {self._retry_delay}s..."
                    )
                    await asyncio.sleep(self._retry_delay)
                else:
                    raise
            finally:
                if account is not None and resp is not None:
                    _close_response(resp)

        # Should not reach here, but just in case
        raise last_error or RuntimeError(f"Failed to fetch {url}")


# ─── Convenience function ─────────────────────────────────────────────────────

async def http_get(
    url: str,
    *,
    impersonate: Union[str, List[str]] = "chrome",
    proxy: Optional[str] = None,
    headers: Optional[Dict[str, str]] = None,
    cookies: Optional[Dict[str, str]] = None,
    timeout: int = 30,
    stealthy_headers: bool = True,
    retries: int = 1,
) -> Response:
    """One-off async HTTP fetch with TLS impersonation.

    Usage:
        response = await http_get("https://example.com")
    """
    async with HTTPSession(
        impersonate=impersonate,
        proxy=proxy,
        stealthy_headers=stealthy_headers,
        retries=retries,
        timeout=timeout,
    ) as session:
        return await session.get(
            url,
            headers=headers,
            cookies=cookies,
            timeout=timeout,
        )


def _proxy_for_env() -> Optional[str]:
    """Read proxy from environment, if set."""
    import os
    return os.environ.get("SIEVE_SEARCH_PROXY")


def search_proxy() -> Optional[str]:
    """Get the search proxy, if configured."""
    return _proxy_for_env()
