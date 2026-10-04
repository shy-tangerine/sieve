"""Sieve MCP Server.

Forks Scrapling's built-in MCP server and adds:
- Trafilatura article extraction (cleaner than markdownify)
- Smart fetch routing (auto-escalate HTTP -> Stealthy). 2 tiers: HTTP first, then Patchright stealth browser.
- SQLite content cache with TTL
- smart_fetch umbrella tool (single entry point that routes automatically)
- extract_article and extract_structured modes
- Input validation with SSRF protection

Note: the dynamic (Playwright) browser tier was removed in v3.5.0. open_session
still accepts session_type="dynamic" for backward compatibility with manual
session creation, but smart_fetch auto-routing only uses http -> stealthy.
"""

from __future__ import annotations

from sieve.command_router import CAPABILITY_REGISTRY
from sieve.resource_budget import budgeted

import json
import hmac
import logging
import os
import sys
import inspect
from functools import wraps
import asyncio
import contextvars
from asyncio import to_thread as asyncio_to_thread
from datetime import datetime, timezone
from time import time as now
from typing import Annotated, Mapping, Sequence, Optional, Literal, Dict, List, Any, TYPE_CHECKING
import warnings as _warnings
import sys as _sys
import traceback as _traceback

# Production cleanliness: on Windows the ProactorEventLoop's stdio/subprocess
# pipe transports can emit 'unclosed transport' ResourceWarnings from __del__
# during interpreter teardown (after the loop is closed). These print a
# traceback to stderr that an MCP client can mistake for a crash. The real
# fix is closing the transports while the loop is alive (see
# _shutdown_close_sessions); these two guards suppress any residual noise so
# stderr stays clean. (Real __del__ exceptions still surface.)
_warnings.filterwarnings("ignore", message="unclosed .*transport", category=ResourceWarning)


def _valid_bearer_authorization(
    headers: Sequence[tuple[bytes, bytes]], token: str,
) -> bool:
    """Validate the optional service bearer token without header ambiguity."""
    if not token:
        return True
    values = [value for key, value in headers if key.lower() == b"authorization"]
    if len(values) != 1:
        return False
    expected = b"Bearer " + token.encode("utf-8")
    return hmac.compare_digest(values[0], expected)

# CPython prints 'Exception ignored in __del__' via sys.unraisablehook. The
# asyncio transport teardown on a closed ProactorEventLoop raises RuntimeError
# ('Event loop is closed') and ValueError ('I/O operation on closed pipe') from
# __del__ during GC â after the loop is gone, so they can't be caught. Override
# the hook to swallow ONLY that benign asyncio-transport teardown noise; every
# other unraisable exception still goes to the original hook (real bugs stay
# visible). This is what keeps `python -m sieve` stderr clean on exit.
_ORIG_UNRAISABLEHOOK = getattr(_sys, "unraisablehook", None)

def _quiet_asyncio_del_hook(args):
    etype = getattr(args, "exc_type", None)
    try:
        tb = getattr(args, "exc_traceback", None)
        # extract_tb yields FrameSummary objects (.filename, not .f_code).
        filenames = " ".join(
            (getattr(fr, "filename", "") or "") for fr in _traceback.extract_tb(tb)
        ) if tb is not None else ""
    except Exception:
        filenames = ""
    is_asyncio_teardown = (
        "asyncio" in filenames
        and etype in (RuntimeError, ValueError, ResourceWarning)
    )
    if is_asyncio_teardown:
        return  # benign transport teardown on a closed loop â swallow
    if _ORIG_UNRAISABLEHOOK is not None:
        try:
            _ORIG_UNRAISABLEHOOK(args)
        except Exception:
            pass

try:
    _sys.unraisablehook = _quiet_asyncio_del_hook
except Exception:
    pass

logger = logging.getLogger("master-fetch.server")



from sieve import __version__
from sieve.public_output import failure_payload as safe_error, safe_public_json, MAX_PUBLIC_OUTPUT_BYTES
from sieve.actions import ActionResult, MAX_ACTIONS
from pydantic import BaseModel, Field

# Lazy imports: browser deps (patchright) pull in playwright (~5s load). Defer
# until first use so the MCP server responds to initialize immediately.
# Set when browser import fails (e.g. patchright not installable on Termux).
# When set, sieve runs in HTTP-only mode: fetch + search + crawl work via primp
# + httpx + trafilatura, but stealthy browser escalation and screenshot are disabled.
_browser_import_error: Optional[str] = None

# Module-level type placeholders â needed because FastMCP evaluates string
# annotations at tool registration time. Set to actual types on first fetch.
SetCookieParam: Any = None  # type: ignore[valid-type]
SelectorWaitStates: Any = None
FollowRedirects: Any = None
ImpersonateType: Any = None


def _browser_deps_available() -> bool:
    """True if browser deps (patchright) are importable.

    Non-blocking: reads the cache populated by the prewarm thread.
    Never triggers a synchronous import on the event loop.

    If the cache is not yet populated (prewarm thread hasn't finished),
    returns True (optimistic). The actual browser operation will fail
    gracefully if patchright isn't installed, and the error is caught
    by the tool handler.
    """
    from sieve.browser import is_browser_available_cached, browser_import_error
    global _browser_import_error
    cached = is_browser_available_cached()
    if cached is True:
        return True
    if cached is False:
        _browser_import_error = browser_import_error()
        return False
    # Cache not yet populated (prewarm thread still running or hasn't started).
    # Optimistic: assume available. If wrong, the browser operation raises
    # ImportError which the tool handler catches and reports cleanly.
    return True


async def _fallback_http_get(
    url: str,
    *, proxy: Optional[str] = None,
    headers: Optional[Dict[str, str]] = None,
    cookies: Optional[Dict[str, str]] = None,
    timeout: int = 30,
    verify: bool = True,
):
    """HTTP fetch via primp (TLS impersonation). Used as the HTTP tier.

    Returns a Response object from sieve.fetcher.
    """
    from sieve.fetcher import http_get
    return await http_get(
        url, proxy=proxy, headers=headers, cookies=cookies, timeout=timeout,
    )

if TYPE_CHECKING:
    from sieve.server_search import CrawlResponseModel
    from sieve.fetcher import Response as _ScraplingResponse
    from sieve.search import SearchResponseModel
    from mcp.types import ImageContent, TextContent

from sieve.cache import set_cached, get_cached, clear_cache, clear_all_cache, DEFAULT_TTL  # noqa: F401 — get_cached/set_cached are monkeypatch surfaces for tests
from sieve.robots import clear_robots_cache
from sieve.envelope import (
    classify_source, compute_freshness, page_type_from_error,
)
from sieve.security import (
    validate_url,
    validate_css_selector,
    validate_headers,
    validate_proxy,
    validate_timeout,
    SecurityError,
)
from sieve.browser_sessions import BrowserSessionRegistry, SessionEntry

# Extended extraction types (beyond Scrapling's markdown/html/text)
ExtendedExtractionType = Literal["markdown", "html", "text", "article", "structured"]
SessionType = Literal["dynamic", "stealthy"]
ScreenshotType = Literal["png", "jpeg"]

MAX_CONTENT_CHARS = 40000
MIN_CHUNK_CHARS = 500  # if remaining < this, merge into current chunk (avoids wasteful round-trips)
MAX_RESPONSE_BYTES = 50 * 1024 * 1024  # 50MB hard cap for response bodies
MAX_BULK_URLS = 100  # hard cap to prevent DoS via unbounded parallel requests
def _env_int(name: str, default: int) -> int:
    """Read an integer env var, falling back to default on missing/invalid."""
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(float(raw))
    except (TypeError, ValueError):
        return default

# Idle browser close: after this many seconds with no smart_fetch/screenshot in
# flight, the warm Patchright Chrome is closed entirely (process exits, OS reclaims
# all its RAM). The next fetch relaunches it (~2s cold start). Tuned via the
# SIEVE_BROWSER_IDLE_TIMEOUT env var. Default 300s (5 min) so an agent actively
# working (30-90s think-pauses between fetches) keeps Chrome warm, while a sieve
# left running in the background actually frees its RAM. Set to 0 to keep the
# browser alive forever (the old behavior, useful when RAM is not a concern).
AUTO_SESSION_IDLE_TIMEOUT = _env_int("SIEVE_BROWSER_IDLE_TIMEOUT", 300)
IDLE_CHECK_INTERVAL = 60  # How often to check for idle sessions (seconds)

# MCP initialize `instructions` â injected into the agent's context ONCE on
# connect, then lost on the first context compaction. Deliberately near-
# empty: durable guidance lives in the per-tool definitions, which are
# re-injected every turn. Only the two signals an agent must never miss.
SIEVE_INSTRUCTIONS = (
    "Sieve tools: check content_ok before trusting content; "
    "follow next_action for the next call."
)

class ResponseModel(BaseModel):
    """Request's response information structure."""
    status: int = Field(description="HTTP status (0=network error)")
    content: list[str] = Field(description="Extracted text (truncated if is_truncated)")
    url: str = Field(description="Final URL")
    cached: bool = Field(default=False, description="From cache")
    fetcher_used: str = Field(default="", description="http/dynamic/stealthy/cache/none")
    extracted_type: str = Field(default="markdown", description="markdown|html|text|article|structured")
    session_id: str = Field(default="", description="Browser session ID")
    action_results: list[ActionResult] = Field(default_factory=list, max_length=MAX_ACTIONS, description="Bounded per-action status: index, type, status, safe category. No selectors or action values.")
    duration_ms: float = Field(default=0, description="Duration ms")
    error: str = Field(default="", description="Error + recovery hints")
    content_type: str = Field(default="", description="e.g. text/html, application/json")
    total_size_bytes: int = Field(default=0, description="Raw body bytes")
    total_extracted_chars: int = Field(default=0, description="Total chars of extracted text (before chunking). Use to gauge how much remains: total_extracted_chars - offset")
    is_truncated: bool = Field(default=False, description="True=more extracted content. Use next_offset. Check total_extracted_chars to see how much remains.")
    next_offset: int = Field(default=0, description="Next offset when is_truncated. 0=no more")
    escalation_path: str = Field(default="", description="e.g. httpâstealthy. Pre-v3.5 logs may contain httpâdynamicâstealthy entries from the old 3-tier path.")
    retry_count: int = Field(default=0, description="Retries")
    # Agent-facing signals (set by _with_agent_hints on every finalized response).
    summary: str = Field(default="", description="One-line status for quick reasoning, e.g. '200 OK Â· 12.4KB markdown Â· http Â· truncated'")
    content_ok: bool = Field(default=False, description="True = real content retrieved (status<400, no error, not a JS shell/login wall). Check this before trusting content.")
    next_action: str = Field(default="", description="Suggested next call when one is obvious (paginate/retry/switch source). Empty = nothing to do.")
    blocked: Dict[str, Any] = Field(default_factory=lambda: {"blocked": False}, description="Structured access verdict; JS shells use kind=js_shell_or_anti_bot.")
    fetched_at: str = Field(default="", description="ISO-8601 UTC timestamp this response was generated. For cached responses, content age is bounded by cache_ttl.")
    metadata: Dict[str, Any] = Field(default_factory=dict, description="Page metadata for citation/relevance: title, description, site_name, type, image, canonical, lang, published_time, author (OpenGraph + JSON-LD + canonical). For PDFs: title, author, subject, keywords, creator, producer, creation_date, mod_date. Empty for non-HTML/non-PDF.")
    media: List[str] = Field(default_factory=list, description="Image URLs on the page (only populated when include_media=true). Multimodal agents can fetch/screenshot these. For PDFs: per-page embedded-image metadata (count + dimensions).")
    links: Dict[str, Any] = Field(default_factory=dict, description="Outgoing links classified by context (only populated when include_links=true): {citations:[{url,text}], navigation:[{url,text}], external:[{url,text}], primary_source:url}. citations = links inside the main-content area (the page's referenced sources - the highest-value links to follow); navigation = site chrome; external = off-domain links; primary_source = best-effort hint at the actual primary source (canonical/JSON-LD or a citation on arxiv/doi/github/etc). Use to follow a page's source chain in one step.")
    quality_score: float = Field(default=0.0, description="PDF extraction quality 0.0-1.0 (readable-char ratio; 1.0 = clean, low = garbled/CID corruption). 0.0 for non-PDF. Trust PDF content more the closer this is to 1.0.")
    table_of_contents: list = Field(default_factory=list, description="PDF section-map: outline/bookmarks as [{level, title, page, end_page}] when the PDF has a ToC; for PDFs without bookmarks, a heading-based map is built from font-size detection. page+end_page give a range per section so you can pass pages='X-Y' to grab one section. Empty for non-PDF or PDFs with no detectable headings.")
    # âââ v10 research-grade envelope (additive; all default-valued) âââ
    # page_type: structural class of the page, computed from raw HTML. Drives
    # next_action (list pages point to their links, auth walls suggest switching source).
    page_type: str = Field(default="unknown", description="Structural class: article|docs|list|forum|qa|pdf|js_shell|auth_wall|paywall|redirect|image|json|unknown. Drives next_action. 'list' = a page whose main content is links to other pages (fetch those or smart_crawl). 'auth_wall'/'paywall' = content behind login/payment.")
    # source_type + is_official: domain-based authority signal so the agent can
    # weigh trust without a separate lookup. Conservative: is_official is True
    # only on a strong signal (vendor's own docs domain, gov, edu, github).
    source_type: str = Field(default="unknown", description="Domain authority class: vendor-docs|official-docs|news|blog|forum|qa|gov|edu|github|docs-site|ecommerce|unknown. Helps weigh source trust.")
    is_official: bool = Field(default=False, description="True only on a strong signal that this is the canonical/official source for its subject (vendor docs, gov, edu, github, the org's own domain). Conservative default False.")
    # Freshness: content_age_days from the page's own published/modified date
    # (OpenGraph/JSON-LD/PDF). -1 = no date recoverable. is_stale = age > 365d.
    content_age_days: int = Field(default=-1, description="Age in days from the page's published/modified date (OpenGraph/JSON-LD/PDF creation_date). -1 = no date recoverable. Pair with is_stale to judge currency.")
    is_stale: bool = Field(default=False, description="True when content_age_days > 365 (info may be outdated). For news/current-state questions, seek a newer source.")
    # source + archived_at: set ONLY when this content came from the Internet
    # Archive (auto-fallback after a live hard-block). 'live' (default) = the
    # real page. Honest marking so the agent knows it may be a dated snapshot.
    source: str = Field(default="live", description="'live' (default) = fetched from the real URL. 'archive.org' = the live site hard-blocked and this content was recovered from the Internet Archive's closest snapshot (see archived_at for the snapshot date).")
    archived_at: str = Field(default="", description="ISO date of the archive.org snapshot when source='archive.org'. Empty when source='live'. The content reflects the page as it was on this date.")
    # network: XHR bodies captured during a browser fetch. Populated when
    # capture_xhr=true, or automatically when an AJAX shell is detected (a page
    # whose data panels load over XHR after render). Kept OUT of `content` so
    # extraction stays predictable; fold_captured=true merges it in instead.
    network: Dict[str, Any] = Field(default_factory=dict, description="XHR/fetch fragments captured while rendering (only when capture_xhr=true or an AJAX shell was auto-detected): {fragments:[{url,status,content_type,size_bytes,text,is_primary,text_truncated}], primary_url, captured_count}. The fragment with is_primary=true is the page's data payload â for a page whose numbers arrive over XHR, that fragment holds them while `content` holds only boilerplate. Empty when nothing was captured.")


    resource_budget: dict = Field(default_factory=dict, description="Aggregate consumed and truncated resource dimensions.")


class BulkResponseModel(BaseModel):
    """Response from bulk fetch operations, one result per URL."""
    results: list[ResponseModel] = Field(description="Per-URL results")
    total: int = Field(description="Total URLs")
    successful: int = Field(description="Fetches with status<400 + no error")


class ArticleModel(BaseModel):
    """Structured article data extracted by Trafilatura."""
    title: str = Field(description="Article title")
    author: str = Field(description="Article author")
    date: str = Field(description="Publication date")
    body: str = Field(description="Main article text")
    description: str = Field(description="Article summary")
    url: str = Field(description="Source URL")
    categories: list[str] = Field(default=[], description="Categories")
    tags: list[str] = Field(default=[], description="Tags")


class SessionInfo(BaseModel):
    """Information about an open browser session."""
    session_id: str = Field(description="Session ID")
    session_type: SessionType = Field(description="dynamic|stealthy")
    created_at: str = Field(description="ISO timestamp")
    is_alive: bool = Field(description="Session alive?")


class SessionCreatedModel(SessionInfo):
    """Response returned when a new session is created."""
    message: str = Field(description="Confirmation message")


class SessionClosedModel(BaseModel):
    """Response returned when a session is closed."""
    session_id: str = Field(description="Closed session ID")
    message: str = Field(description="Confirmation message")


class CacheInfoModel(BaseModel):
    """Response from cache management operations."""
    message: str = Field(description="Result message")
    purged: int = Field(default=0, description="Entries purged")


class VersionInfoModel(BaseModel):
    """Sieve version and update status."""
    version: str = Field(description="Installed version")
    latest: str = Field(default="", description="Reserved for a future Sieve release channel")
    up_to_date: bool = Field(default=True, description="Local source build has no automatic update comparison")
    update_command: str = Field(default="", description="Empty: update the local checkout manually")


# Kept as a module alias for callers that imported the old private record while
# the lifecycle implementation lived in this transport facade.
_SessionEntry = SessionEntry


# âââ Content quality detection (module-level, used by the class) âââââ

# Heuristic thresholds for catching JS-rendered SPAs whose HTTP shell text
# doesn't match the known signal phrases (e.g. quotes.toscrape.com/js returns
# a nav-only shell). Scoped to the HTTP tier so stealthy-rendered low-text
# pages (image galleries, canvas apps) don't false-positive.
_JS_SHELL_MIN_BODY_BYTES = 3000   # raw HTML body must be at least this large
_JS_SHELL_MIN_TEXT_CHARS = 200    # ...while extracted text is below this

_JS_SHELL_SIGNALS = [
    "enable javascript", "you need to enable javascript",
    "javascript is required", "javascript is disabled",
    "javascript to run this app", "javascript must be enabled",
    "please enable javascript", "requires javascript",
    "we've detected that javascript is disabled",
    "javascript is disabled in this browser",
    "enable javascript to run this app",
]

# Cloudflare challenge page markers. These appear in the raw HTML of CF
# interstitial / Turnstile challenge pages (which can return 200). Used by
# _is_js_shell to detect CF pages that bypass the generic JS-shell signals
# (large HTML body with CF-specific scripts). Without this, extraction_type=html
# (used by smart_crawl) gets the challenge page as "content" and never escalates.
_CF_CHALLENGE_SIGNALS = [
    "challenges.cloudflare.com/turnstile",
    "cf-turnstile",
    "cf_chl_opt",
    "__cf_chl",
    "cf-browser-verification",
    "challenge-platform",
    "cf-mitigated",
]

_GEO_REDIRECT_SIGNALS = [
    "choose a country", "select your country", "select your region",
    "shopping in the u.s.", "choose your country",
    "country selector", "region selector",
]


def _is_cloudflare_from_response(result: ResponseModel) -> bool:
    """Check if a ResponseModel indicates a bot challenge page.

    Detects common bot challenge signatures in page content including embedded
    Cloudflare challenges, generic CAPTCHA pages, and verification prompts.
    Does NOT distinguish DataDome/Turnstile from ordinary bot checks.

    IMPORTANT: Only meaningful on error status codes (403, 503). A status-200
    page about web security that mentions "cloudflare" is not a bot challenge.
    """
    # Guard: only check on error status codes where bot challenges make sense
    if result.status not in (403, 503):
        return False
    content_str = " ".join(result.content).lower()
    cf_signals = ["cloudflare", "cf-browser", "challenge-platform", "cf_chl_opt", "ray id"]
    dd_signals = ["captcha-delivery.com", "datadome", "dd="]
    generic_signals = ["please verify you are a human", "are you a robot", "checking your browser"]
    all_signals = cf_signals + dd_signals + generic_signals
    return any(signal in content_str for signal in all_signals)


_DETERMINISTIC_NET_SIGNALS = (
    "name or service not known",
    "nodename nor servname",
    "temporary failure in name resolution",
    "getaddrinfo",
    "name resolution",
    "connection refused",
    "econnrefused",
)


def _is_deterministic_net_msg(msg: str) -> bool:
    """True if an error message is a transport failure no retry and no
    browser can fix (DNS resolution, refused connection)."""
    return any(s in msg.lower() for s in _DETERMINISTIC_NET_SIGNALS)


def _is_deterministic_net_error(result: ResponseModel) -> bool:
    """True for transport failures a browser retry cannot fix either:
    DNS resolution (the host doesn't exist) and refused connections
    (nothing listening - from this same IP a browser gets refused too).
    Everything else at status 0 (TLS fingerprint blocks, resets, timeouts)
    is worth a stealthy attempt."""
    if result.status != 0:
        return False
    err = (result.error or "").lower()
    if not err and result.content:
        err = result.content[0].lower()
    return any(s in err for s in _DETERMINISTIC_NET_SIGNALS)


def _is_js_shell(result: ResponseModel) -> bool:
    """Check if a response contains only a JS-only placeholder, not real content.

    Used by smart_fetch to decide whether to escalate from HTTP to stealthy.
    Pre-v3.5 callers may have passed through dynamic as an intermediate step.
    """
    # JS-shell detection is an HTML concept. Non-HTML documents (PDFs, images,
    # JSON, feeds, plain text) cannot be JS shells, and the text-ratio
    # heuristic below misfires on them: a scanned PDF has a large binary body
    # but little extractable text, which is indistinguishable from an SPA shell
    # by ratio alone. Only apply when content_type is known to be HTML; an
    # empty/unknown content_type falls through to the existing logic.
    from sieve.content_type import classify_media
    if result.content_type and classify_media(result.content_type) != "html":
        return False
    content_str = " ".join(result.content).lower().strip()
    if not content_str:
        return True  # Empty content after extraction = JS shell or blank page
    if any(signal in content_str for signal in _JS_SHELL_SIGNALS):
        return True
    # Cloudflare challenge pages can return 200 with large HTML (Turnstile
    # scripts, challenge-platform divs). With extraction_type=html (used by
    # smart_crawl), the raw HTML is large so the text-length heuristic below
    # doesn't trigger. Check for CF-specific markers to catch these.
    if result.fetcher_used == "http" and result.status == 200:
        if any(signal in content_str for signal in _CF_CHALLENGE_SIGNALS):
            return True
    # Heuristic: the HTTP tier returned a 200 with a large HTML body but almost
    # no extractable text -> the page is JS-rendered and HTTP got the empty shell
    # (e.g. a SPA whose nav-only shell doesn't match the known signal phrases).
    # Scoped to the HTTP tier: a stealthy result with little text from a large
    # page is a genuinely low-text page (image gallery / canvas), not a shell.
    if result.fetcher_used in ("http", "stealthy") and result.status == 200 \
            and result.total_size_bytes > _JS_SHELL_MIN_BODY_BYTES:
        text_len = sum(len(c) for c in result.content)
        if text_len < _JS_SHELL_MIN_TEXT_CHARS:
            return True
    return False


def _detect_content_issue(result: ResponseModel) -> str:
    """Detect content quality issues in a response. Returns error string or ''.

    Called on the final result to give the caller a signal that content may be unusable,
    even when HTTP status is 200. Sets the error field so AI agents can detect failures
    without having to parse content strings themselves.

    Note: Bot challenge detection is only applied to 403/503 responses. Legitimate
    articles about web security may contain "cloudflare" in body text with status 200.
    """
    content_str = " ".join(result.content).lower().strip()

    if _is_js_shell(result):
        return "js_shell_detected: page requires JavaScript rendering but fetcher returned placeholder"

    if any(signal in content_str for signal in _GEO_REDIRECT_SIGNALS):
        return "geo_redirect_detected: page returned region/country selector instead of content"

    # Only check for bot challenge on error status codes. Legitimate pages
    # (status 200) that mention "cloudflare" in body text are not bot challenges.
    if result.status in (403, 503) and _is_cloudflare_from_response(result):
        return "bot_challenge_detected: page returned bot challenge/verification page"

    # Universal: any 4xx/5xx is an error, even if the server returned an HTML
    # error page as content. Without this, a 404 page gets treated as real
    # content (error="" -> agent trusts it). Set the error field so agents,
    # cache, and archive fallback all see it as a failure.
    if result.status >= 400:
        return f"http_error_{result.status}: server returned error status"
    if result.status == 0:
        return "network_error: request failed (DNS/timeout/connection refused)"

    return ""


def _annotate_quality(result: ResponseModel) -> ResponseModel:
    """Check content quality and set error field if issues detected. Returns same result."""
    if not result.error:
        issue = _detect_content_issue(result)
        if issue:
            result.error = issue
    if result.error.startswith("js_shell_detected"):
        result.blocked = {"blocked": True, "kind": "js_shell_or_anti_bot"}
    return result


def _is_cacheable(result: ResponseModel) -> bool:
    """True only for clean, usable content worth caching.

    Excludes: error statuses (4xx/5xx), JS shells, bot-challenge pages, geo
    redirects, all_tiers_failed, and blank/empty extractions. Caching any of
    those would serve broken pages from cache for the whole TTL.
    """
    if not (0 < result.status < 400):
        return False
    if result.error:
        return False
    return bool(result.content) and any(c.strip() for c in result.content)


    err = (result.error or "").lower()
    if result.status == 404:      # page gone/deleted â archive goldmine
        return True
    if result.status == 451:      # legal block
        return True
    if result.status == 0:        # network/DNS/timeout â archive may have it
        return True
    if result.status >= 500:      # server error
        return True
    if result.status in (403, 503) and "bot_challenge" in err:
        return True
    if err.startswith("all_tiers_failed"):
        return True
    if err.startswith("auth_required"):
        return True
    return False


_AJAX_SHELL_ERROR = (
    "ajax_shell_detected: the page fills its data panels over XHR after render, "
    "so content holds only the surrounding boilerplate"
)


def _apply_capture_verdict(
    result: ResponseModel,
    fold_captured: bool,
    shell_detected: bool = False,
) -> ResponseModel:
    """Decide what a capture pass means for content/error on a result.

    By default captured data stays in `network` and, when the page was an AJAX
    shell, the result is marked so `content_ok` goes False â the agent is told
    plainly that `content` is boilerplate and where the real data is. With
    fold_captured the primary fragment is merged into content instead.
    """
    frags = (result.network or {}).get("fragments") or []
    primary = next((f for f in frags if f.get("is_primary")), None)

    if fold_captured and primary and primary.get("text"):
        result.content = list(result.content) + [
            f"\n\n--- captured from {primary.get('url', '')} ---\n{primary['text']}"
        ]
        return result

    if shell_detected and not result.error:
        if primary:
            result.error = _AJAX_SHELL_ERROR + "; the data is in network.fragments"
        else:
            result.error = _AJAX_SHELL_ERROR + "; no data-bearing XHR was captured"
    return result


def _format_size(n: int) -> str:
    """Human-readable byte size for the summary line."""
    if not n:
        return "0B"
    if n >= 1024 * 1024:
        return f"{n / 1024 / 1024:.1f}MB"
    if n >= 1024:
        return f"{n / 1024:.1f}KB"
    return f"{n}B"


def _agent_hints(result: ResponseModel) -> tuple[str, str, bool]:
    """Build (summary, next_action, content_ok) for a finalized fetch result.

    summary       â one-line status agents can pattern-match on at a glance.
    next_action   â the obvious next call, if any (paginate / bypass robots /
                    switch sources). Empty when there is nothing to do.
    content_ok    â True only when real content was retrieved (status<400, no
                    error, not a JS shell / login wall / empty page). Agents should
                    check this before trusting content.
    """
    has_content = bool(result.content) and any(c.strip() for c in result.content)
    # PDFs carry a quality-based content_ok verdict from the extractor (CID
    # garbage / corruption -> False even on HTTP 200). Respect it instead of
    # letting status-200 + has-content mask corruption (the P3 bug).
    if result.quality_score > 0:
        content_ok = result.content_ok and result.status > 0 and not result.error and has_content
    else:
        content_ok = (
            result.status > 0 and result.status < 400
            and not result.error
            and has_content
        )

    size = result.total_size_bytes or sum(len(c) for c in result.content)
    parts: list[str] = []
    if result.status == 0:
        parts.append("network error")
    else:
        parts.append(f"{int(result.status)} {'OK' if result.status < 400 else 'ERR'}")
    parts.append(f"{_format_size(size)} {result.extracted_type or 'markdown'}")
    if result.fetcher_used:
        parts.append(result.fetcher_used)
    if result.cached:
        parts.append("cached")
    if result.is_truncated:
        parts.append("truncated")
    summary = " Â· ".join(parts)

    next_action = ""
    err = result.error or ""
    if result.is_truncated and result.next_offset:
        next_action = f"page truncated. Use focus='query' to extract only relevant blocks, or offset={result.next_offset} to continue paginating"
    elif err == "selector_empty":
        next_action = "retry_without_css_selector"
    elif err == "robots_txt_disallowed":
        next_action = "blocked by robots.txt: use another permitted source or ask the operator whether an explicit override is appropriate"
    elif err == "robots_unavailable":
        next_action = "robots.txt policy could not be read (strict mode fails closed): retry when the site is reachable, or use another permitted source"
    elif err.startswith("ajax_shell_detected"):
        primary_url = (result.network or {}).get("primary_url", "")
        if primary_url:
            next_action = (
                "content is boilerplate; the page's data is in network.fragments. "
                "Read that bounded fragment directly; do not replay captured URLs automatically."
            )
        else:
            next_action = (
                "page loads its data over XHR but none was captured; retry with "
                "capture_pattern to widen the filter, or find the data endpoint"
            )
    elif err.startswith("js_shell_detected"):
        next_action = "page is a JS shell; retry with --reader browser --browser-backend sleeper (or mcp_sleeper_fetch)"
    elif err.startswith("bot_challenge_detected"):
        next_action = "bot challenge page; re-fetch auto-escalates to the stealthy browser"
    elif err.startswith("geo_redirect_detected"):
        next_action = "geo redirect: try a different regional URL or a proxy"
    elif err.startswith("scanned_pdf"):
        next_action = "scanned/image-only PDF - install OCR extras (uv sync --all-extras) to auto-OCR, or use a vision-capable tool / another source"
    elif (not result.content_ok) and result.quality_score > 0 and result.quality_score < 0.7 and not err:
        next_action = "low-quality PDF extraction (CID font corruption / garbled text) - install OCR extras (uv sync --all-extras) for auto-OCR, or use a vision tool / screenshot on the flagged pages"
    elif err.startswith("encrypted_pdf"):
        next_action = "encrypted PDF - pass a password via the 'password' option"
    elif err.startswith("pdf_deps_missing"):
        next_action = "PDF support not installed - run: uv sync"
    elif err.startswith("not_a_pdf") or err.startswith("pdf_open_failed") or err.startswith("pdf_extract_failed"):
        next_action = "PDF could not be parsed - see error field"
    elif "all_tiers_failed" in err:
        next_action = "all fetchers failed; site may use unbypassable protection (DataDome/Akamai/Turnstile) - switch sources"
    elif result.status == 0 or result.status >= 400:
        next_action = "fetch failed - see error field"

    # v10 envelope-driven next actions: fire ONLY when the fetch succeeded with
    # real content and no error-driven next_action already fired. Turn the
    # envelope (page_type/freshness) into a concrete next step so the
    # agent doesn't have to re-derive it. Precedence: page structure
    # (list/auth/paywall/redirect) > freshness.
    if not next_action and content_ok:
        if result.page_type == "pdf" and result.total_extracted_chars > 20000:
            next_action = (
                f"large PDF ({result.total_extracted_chars} chars extracted). "
                "Use focus='query' to extract only relevant paragraphs, or "
                "pages='X-Y' to fetch specific sections from the table_of_contents"
            )
        elif result.page_type == "list":
            cits = (result.links or {}).get("citations") or []
            top = [c.get("url", "") for c in cits[:3] if isinstance(c, dict) and c.get("url")]
            if top:
                next_action = (
                    "this is a list page; the content you want is likely behind its "
                    f"links. Top targets: {', '.join(top)}. Or call smart_crawl on this URL."
                )
            else:
                next_action = (
                    "this is a list page (links to other pages); call smart_crawl on "
                    "this URL, or fetch the linked pages directly"
                )
        elif result.page_type == "auth_wall":
            next_action = (
                "content behind login/authentication; the Internet Archive may have a "
                "snapshot, or switch sources"
            )
        elif result.page_type == "paywall":
            next_action = "paywalled content; try the Internet Archive or a different source"
        elif result.page_type == "redirect":
            canon = (result.metadata or {}).get("canonical") or ""
            next_action = (
                f"page redirected; the real URL is {canon}" if canon
                else "page redirected; check the final URL field"
            )
        elif result.is_stale and result.page_type in ("article", "docs", "unknown"):
            next_action = (
                f"content is {result.content_age_days} days old (may be outdated); for "
                "current info, smart_search a recent query (e.g. add the current year)"
            )
    return summary, next_action, content_ok


def _apply_envelope(result: ResponseModel) -> None:
    """Compute the v10 research-grade envelope fields on a result in place.

    page_type: definitive error/content_type signals override the structural
    value set in _translate_response (js_shell/auth_wall/redirect from error;
    pdf/json/image from content_type). source_type/is_official from the URL
    (cheap heuristic). content_age_days/is_stale from metadata dates + fetched_at.
    Recomputed on every return (incl. cache hits) since it is near-free and the
    inputs (url, metadata, fetched_at) are always present.
    """
    # page_type override: definitive signals win over the structural guess.
    err_type = page_type_from_error(result.error)
    if err_type:
        result.page_type = err_type
    else:
        from sieve.content_type import classify_media
        media = classify_media(result.content_type)
        if media in {"pdf", "json", "image"}:
            result.page_type = media
        # else: keep the structural page_type from _translate_response
        # (forum/qa/list/docs/article/paywall/redirect/unknown).
    # Source authority: cheap URL heuristic, recomputed always (not cached).
    st, off = classify_source(result.url)
    result.source_type = st
    result.is_official = off
    # Freshness: from metadata dates vs this response's fetched_at. Recomputed
    # always (metadata may be cache-restored; age is relative to now).
    age, stale = compute_freshness(result.metadata, result.fetched_at)
    result.content_age_days = age
    result.is_stale = stale


def _with_agent_hints(result: ResponseModel) -> ResponseModel:
    """Stamp the v10 envelope + agent-facing hints on a result.

    This is the universal final wrapper (called by _apply_chunking on every
    return: live fetches, cache hits, robots blocks, archive fallback), so the
    envelope appears on every response an agent ever sees.
    """
    result.fetched_at = datetime.now(timezone.utc).isoformat()
    _apply_envelope(result)
    summary, next_action, content_ok = _agent_hints(result)
    result.summary = summary
    result.content_ok = content_ok
    result.next_action = next_action
    return result


def _dedup_repeated_lines(text: str) -> str:
    """oc-inspired: collapse repeated short lines (nav chrome, per-item buttons).
    ponytail: naive exact-match, short-line threshold 30, limit 5 â upgrade if corpus shows misses."""
    if not text or len(text) < 2000:
        return text
    lines = text.splitlines()
    if len(lines) < 20:
        return text
    from collections import Counter
    # count short lines only
    short = [l.strip() for l in lines if l.strip() and len(l.strip()) <= 30]
    if not short:
        return text
    cnt = Counter(short)
    repeated = {k for k, v in cnt.items() if v > 5}
    if not repeated:
        return text
    out, seen = [], {}
    dup = 0
    for l in lines:
        s = l.strip()
        if s in repeated:
            seen[s] = seen.get(s, 0) + 1
            if seen[s] <= 2:  # keep 2 examples
                out.append(l)
            else:
                dup += 1
        else:
            out.append(l)
    if dup:
        out.append(f"\n[{dup} repeated nav controls collapsed]")
    # collapse long runs of link-like short lines (oc collapseRuns)
    # if >8 consecutive short lines, keep 5 + marker
    final, i = [], 0
    while i < len(out):
        j = i
        while j < len(out) and out[j].strip() and len(out[j].strip()) <= 25:
            j += 1
        run = j - i
        if run > 8:
            final.extend(out[i:i+5])
            final.append(f"[{run-5} similar links collapsed]")
            i = j
        else:
            final.append(out[i])
            i += 1
    return "\n".join(final)


def _apply_chunking(result: ResponseModel, max_chars: int = MAX_CONTENT_CHARS, offset: int = 0) -> ResponseModel:
    """Truncate content if it exceeds max_chars, starting from offset.

    Smart merge: if remaining content after a chunk is less than MIN_CHUNK_CHARS,
    include it all in the current chunk. This prevents wasteful round-trips where
    an agent calls again just to get 55 chars.

    Always sets total_extracted_chars so agents can gauge remaining content
    without making a follow-up call. Stamps agent-facing hints (summary,
    content_ok, next_action, fetched_at) on every returned result.
    """
    full_text = "\n".join(result.content)
    # oc-inspired budget dedup: collapse repeated short lines (per-item chrome)
    # ponytail: O(n) scan, naive exact-match dedup; upgrade to block-hash if needed
    try:
        full_text = _dedup_repeated_lines(full_text)
    except Exception:
        pass
    # Query-focused filter (post-cache): if the caller passed `focus`, keep only
    # the BM25-relevant blocks so the agent loads less context on long pages.
    # Only applies to text-like extractions (not raw html). Runs before chunking
    # so offset/next_offset page through the FOCUSED content.
    focus_q = _FOCUS.get()
    if focus_q and result.extracted_type in ("markdown", "text", "article", "structured"):
        try:
            from sieve.focus import focus_content
            full_text = focus_content(full_text, focus_q)
        except Exception as e:
            logger.debug("focus filter failed: %s", safe_error(e, fallback_category="extract")["category"])
    total_len = len(full_text)

    if offset >= total_len:
        # model_copy(update=...) preserves EVERY field by construction
        # (metadata/links/page_type/source_type/quality_score/toc/...). The
        # old hand-written constructor dropped any field not listed, so every
        # new envelope field silently vanished on the no-more-content branch.
        return _with_agent_hints(result.model_copy(update={
            "content": ["[No more content.]"],
            "total_extracted_chars": total_len,
            "is_truncated": False,
            "next_offset": 0,
        }))

    chunk = full_text[offset:offset + max_chars]
    chunk_len = len(chunk)
    remaining = total_len - offset - chunk_len

    # Smart merge: if remaining is small, include it all in this chunk.
    # Avoids wasteful round-trip where agent calls again for 55 chars.
    truncated = False
    next_off = 0
    if remaining > MIN_CHUNK_CHARS:
        truncated = True
        next_off = offset + chunk_len
        remaining_hint = total_len - next_off
        chunk += (
            f"\n\n[Truncated: showing {chunk_len:,} of {total_len:,} extracted chars. "
            f"{remaining_hint:,} chars remaining. Next offset: {next_off}]"
        )
    elif remaining > 0:
        # Remaining is small â include it all, no truncation flag
        chunk = full_text[offset:]

    # model_copy(update=...) preserves every field by construction â no more
    # hand-maintained field list that silently dropped envelope fields on
    # truncation. Add a field to ResponseModel and it survives chunking free.
    return _with_agent_hints(result.model_copy(update={
        "content": [chunk],
        "total_extracted_chars": total_len,
        "is_truncated": truncated,
        "next_offset": next_off,
    }))


# âââ Response translation helpers ââââââââââââââââââââââââââââââââââ

# PDF extraction options flow from smart_fetch down to _translate_response via
# contextvars (task-local, safe under concurrent bulk fetches) instead of
# threading two new params through every fetcher signature.
_PDF_PAGES: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar("_pdf_pages", default=None)
_PDF_PASSWORD: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar("_pdf_password", default=None)
# Query-focused content filter (smart_fetch `focus` param). Applied POST-cache
# inside _apply_chunking: the full extracted text is cached once, and different
# focus queries are just different BM25 views over the same cached content.
_FOCUS: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar("_focus", default=None)
# Extraction flags participate in the cache key (issue #4): toggling them
# changes the returned content, so cached entries must not cross over.
# Extraction flags live in server_fetch (lower layer) so the cache-key work
# there can read them without importing this module (architecture gate).
from sieve.server_fetch import _MAIN_CONTENT_ONLY, _USE_TRAFILATURA  # noqa: F401
# Opt-in: populate ResponseModel.media with the page's image URLs (multimodal).
_INCLUDE_MEDIA: contextvars.ContextVar[bool] = contextvars.ContextVar("_include_media", default=False)
_INCLUDE_LINKS: contextvars.ContextVar[bool] = contextvars.ContextVar("_include_links", default=False)
# Set by _translate_response (the only place the raw body is still around) so
# _auto_escalate can tell an AJAX shell from a genuinely thin page without
# re-parsing. Task-local, like the options above.
_AJAX_SHELL: contextvars.ContextVar[bool] = contextvars.ContextVar("_ajax_shell", default=False)


def _smart_fetch_request_context(func):
    """Scope smart_fetch request options to one invocation and its child tasks."""
    signature = inspect.signature(func)

    @wraps(func)
    async def wrapped(*args, **kwargs):
        from sieve.resource_budget import budget_scope, current_budget
        owns_budget = current_budget() is None
        values = signature.bind(*args, **kwargs)
        values.apply_defaults()
        options = values.arguments
        tokens = [
            (_PDF_PAGES, _PDF_PAGES.set(options["pages"] if isinstance(options["pages"], str) else None)),
            (_PDF_PASSWORD, _PDF_PASSWORD.set(options["password"] if isinstance(options["password"], str) else None)),
            (_FOCUS, _FOCUS.set(options["focus"] if isinstance(options["focus"], str) and options["focus"].strip() else None)),
            (_MAIN_CONTENT_ONLY, _MAIN_CONTENT_ONLY.set(bool(options["main_content_only"]))),
            (_USE_TRAFILATURA, _USE_TRAFILATURA.set(bool(options["use_trafilatura"]))),
            (_INCLUDE_MEDIA, _INCLUDE_MEDIA.set(bool(options["include_media"]))),
            (_INCLUDE_LINKS, _INCLUDE_LINKS.set(bool(options["include_links"]))),
        ]
        try:
            with budget_scope() as account:
                result = await func(*args, **kwargs)
                if owns_budget:
                    records = getattr(result, "results", [result])
                    for record in records:
                        if not hasattr(record, "content"):
                            continue
                        parts = []
                        for part in record.content:
                            allowed = account.take("output_chars", len(part))
                            parts.append(part[:allowed])
                            if allowed < len(part):
                                record.is_truncated = True
                                record.content_ok = bool(allowed)
                        record.content = parts
                        record.resource_budget = account.report()
                return result
        finally:
            for variable, token in reversed(tokens):
                variable.reset(token)

    return wrapped


def _translate_response(
    page: _ScraplingResponse, extraction_type: str, css_selector: Optional[str],
    main_content_only: bool, use_trafilatura: bool = False,
    fetcher_used: str = "", duration_ms: float = 0,
) -> ResponseModel:
    """Translate a native response through the extraction seam."""
    from sieve.response_translation import translate_response
    model, ajax_shell = translate_response(
        page, extraction_type, css_selector, main_content_only, use_trafilatura,
        fetcher_used, duration_ms, ResponseModel, maximum_bytes=MAX_RESPONSE_BYTES,
        pages=_PDF_PAGES.get(), password=_PDF_PASSWORD.get(),
        include_media=_INCLUDE_MEDIA.get(), include_links=_INCLUDE_LINKS.get(),
    )
    _AJAX_SHELL.set(ajax_shell)
    return model


async def _timed(coro):
    """Run a coroutine and return (result, elapsed_ms)."""
    t0 = now()
    result = await coro
    elapsed = (now() - t0) * 1000
    return result, elapsed


async def _safe_prewarm(coro_fn, timeout: float = 20.0) -> None:
    """Run a prewarm callable in the background, fully isolated.

    `coro_fn` is a zero-arg callable returning a coroutine. Catches
    BaseException (a hung/crashing prewarm NEVER takes down the server, not even
    CancelledError) and caps it at `timeout` so a stuck launch can't linger.
    Prewarm is best-effort by design.
    """
    try:
        await asyncio.wait_for(coro_fn(), timeout=timeout)
    except (KeyboardInterrupt, SystemExit):
        raise
    except BaseException:
        pass


async def _safe_imported_prewarm(module_name: str, attr: str, timeout: float = 20.0) -> None:
    """Import and run an optional async prewarm without touching the event loop.

    Startup prewarms are best-effort. Their imports can be surprisingly heavy
    (Scrapling/Playwright/ONNX chains), so resolve the callable in a worker
    thread first; then run the coroutine with the same isolation as
    _safe_prewarm. Import failure, timeout, cancellation, and bad callables are
    all non-fatal.
    """
    def _resolve():
        import importlib
        module = importlib.import_module(module_name)
        return getattr(module, attr)

    try:
        coro_fn = await asyncio.to_thread(_resolve)
    except (KeyboardInterrupt, SystemExit):
        raise
    except BaseException:
        return
    await _safe_prewarm(coro_fn, timeout=timeout)


def _normalize_credentials(credentials: Optional[Dict[str, str]]) -> Optional[tuple]:
    """Convert a credentials dictionary to a tuple accepted by fetchers.

    Returns None if credentials is None or empty.
    Validates types and lengths to prevent injection/DoS.
    """
    if not credentials:
        return None
    username = credentials.get("username")
    password = credentials.get("password")
    if username is None or password is None:
        raise ValueError("Credentials dictionary must contain both 'username' and 'password' keys")
    if not isinstance(username, str) or not isinstance(password, str):
        raise SecurityError("Credential username and password must be strings")
    if len(username) > 512 or len(password) > 512:
        raise SecurityError("Credential values exceed maximum length of 512 characters")
    if "\n" in username or "\r" in username or "\n" in password or "\r" in password:
        raise SecurityError("Credential values must not contain newline characters")
    return username, password


def _safe_cookie_dict(cookies: Sequence[SetCookieParam] | None) -> Optional[Dict[str, str]]:
    """Safely convert MCP cookie param list to {name: value} dict.

    Handles missing keys gracefully and logs warnings.
    Returns None for empty/None input.
    """
    if not cookies:
        return None
    result: Dict[str, str] = {}
    for c in cookies:
        if isinstance(c, dict):
            name = c.get("name", "")
            value = c.get("value", "")
            if name:
                result[name] = value
            else:
                # Don't log the dict â it may contain a sensitive cookie value.
                logger.warning("Cookie dict missing 'name' key, skipping")
    return result or None


# âââ Main server class âââââââââââââââââââââââââââââââââââââââââââââ

_PUBLIC_MCP_TOOLS = frozenset(name for name, capability in CAPABILITY_REGISTRY.items() if capability.definition is not None)


class MasterFetchServer:
    """Enhanced MCP server built on Scrapling with smart routing, caching, and Trafilatura."""

    def __init__(self, cache_ttl: int = DEFAULT_TTL, use_trafilatura: bool = True):
        # Browser lifecycle state belongs to the registry, not the transport
        # facade. Keep the map/lock aliases for compatibility diagnostics.
        self._browser_sessions = BrowserSessionRegistry(
            # Resolve the module-level hook at call time so existing tests and
            # embedding callers can still monkeypatch capability detection.
            browser_available=lambda: _browser_deps_available(),
            browser_error=lambda: _browser_import_error,
            idle_timeout=AUTO_SESSION_IDLE_TIMEOUT,
            idle_check_interval=IDLE_CHECK_INTERVAL,
            logger_=logger,
        )
        self._sessions = self._browser_sessions.sessions
        self._sessions_lock = self._browser_sessions.sessions_lock
        self._cache_ttl = cache_ttl
        self._use_trafilatura = use_trafilatura

    # âââ Core helpers âââââââââââââââââââââââââââââââââââââââââââââ

    async def _get_session(self, session_id: str, expected_type: Optional[SessionType]) -> _SessionEntry:
        """Look up a session by ID, optionally validating its type.

        Holds the session lock to prevent races with close_session.
        Returns the entry with validation â the caller MUST NOT close
        the session concurrently while using the returned entry.
        """
        return await self._browser_sessions.get(session_id, expected_type)

    async def _ensure_auto_session(self, session_type: SessionType) -> str:
        """Get or create an auto-persistent browser session. Avoids browser startup on every fetch.

        Race-safe: if two concurrent calls both pass the initial check,
        the second one closes its orphaned session and reuses the first.

        Idle timeout: when AUTO_SESSION_IDLE_TIMEOUT > 0, auto sessions close
        after that many seconds of inactivity. When it is 0 (default), the
        browser is kept alive forever and no idle monitor is started.
        """
        return await self._browser_sessions.ensure_auto_session(session_type)


    async def _prewarm_stealthy(self) -> None:
        """Warm the single stealthy browser at startup (background, best-effort).

        Scheduled when the MCP server starts so the browser is warm by the time
        the agent first needs a stealthy fetch or screenshot, skipping the
        ~3-5s cold start. Closes after SIEVE_BROWSER_IDLE_TIMEOUT of inactivity,
        then relaunches on the next fetch. Idempotent: _ensure_auto_session
        reuses any existing session.

        Robustness: fully isolated â catches BaseException (so a
        CancelledError or any launch failure can NEVER crash the server) and is
        capped at 30s so a hung browser launch can't hold the session-creation
        lock forever (a later real fetch can then take the lock and retry). On
        any failure the browser simply lazy-launches on the first stealthy fetch.

        Event-loop safety: the browser availability check AND the patchright
        import both run inside a worker thread. The old code called
        _browser_deps_available() on the event loop first, which triggered
        import patchright synchronously and blocked the loop for 1-3s,
        starving server.run() so the MCP initialize handshake never got its
        reply out (client reported -32001 REQUEST_TIMEOUT). Now the entire
        check+import is off the event loop.
        """
        await self._browser_sessions.prewarm()

    async def _start_idle_monitor(self) -> None:
        """Background task: close auto browser sessions after AUTO_SESSION_IDLE_TIMEOUT
        of inactivity.

        All reads of _auto_*_id and _auto_*_last_used happen inside the sessions lock
        to prevent races with _ensure_auto_session.
        Session closing happens outside the lock to avoid blocking other operations.
        """
        await self._browser_sessions._start_idle_monitor()

    def _ensure_idle_monitor(self) -> None:
        """Start the idle monitor background task if not already running.

        Idempotent: safe to call from any code path (session creation, reuse, etc.).
        If the monitor task crashed, the next call restarts it.

        No-op when AUTO_SESSION_IDLE_TIMEOUT == 0 (keep-alive-forever mode) â
        avoids a perpetual background task that wakes every IDLE_CHECK_INTERVAL
        only to `continue`.
        """
        self._browser_sessions.ensure_idle_monitor()

    async def _shutdown_close_sessions(self) -> None:
        """Gracefully close all browser sessions when the server is stopping.

        Called from serve()'s finally block so the single warm Chrome instance
        is torn down cleanly when the agent harness closes the MCP server
        (stdin closed / process exit), rather than relying on OS child reaping.
        Best-effort: never raises.

        The critical detail on Windows: the patchright Chrome subprocess is an
        asyncio BaseSubprocessTransport. Closing the session schedules
        connection_lost via loop.call_soon; if the event loop exits before that
        callback runs, the transport's __del__ fires during GC AFTER the loop is
        closed and prints 'Exception ignored in __del__' tracebacks to stderr
        (RuntimeError: Event loop is closed / ValueError: I/O operation on closed
        pipe). An MCP client reading stderr sees a crash-like traceback even
        though the process exited 0. So we close the sessions, then EXPLICITLY
        flush the loop with a short sleep so pending callbacks drain while the
        loop is alive, then close any lingering asyncio subprocess transports
        so their __del__ is a no-op (they're already closing).
        """
        await self._browser_sessions.shutdown()

    @staticmethod
    def _close_all_subprocess_transports() -> None:
        """Close every asyncio subprocess transport still alive on the current
        loop so their __del__ is a no-op (no 'unclosed transport' / 'Event loop
        is closed' noise on Windows teardown). Best-effort; never raises."""
        BrowserSessionRegistry.close_all_subprocess_transports()

    async def _finalize_result(
        self,
        result: ResponseModel,
        url: str,
        extraction_type: str,
        css_selector: Optional[str],
        cache_ttl: int,
        offset: int = 0,
        max_chars: int = MAX_CONTENT_CHARS,
    ) -> ResponseModel:
        """Apply content quality annotation, cache, and chunking to a fetch result.

        Centralizes the repetitive 'annotate -> cache -> chunk' pattern
        that was duplicated 8+ times across smart_fetch.
        """
        result = _annotate_quality(result)
        # Only cache CLEAN content. Caching JS shells / bot challenges / geo
        # redirects / error statuses would serve broken pages from cache for
        # the whole TTL (and the cache-hit path doesn't restore the error field,
        # so content_ok would come back True â the agent would trust garbage).
        if cache_ttl > 0 and _is_cacheable(result):
            await set_cached(
                url, extraction_type, result.content, result.status,
                css_selector, cache_ttl,
                content_type=result.content_type,
                total_size_bytes=result.total_size_bytes,
                pages=_PDF_PAGES.get(),
                source=result.source,
                main_content_only=_MAIN_CONTENT_ONLY.get(),
                use_trafilatura=_USE_TRAFILATURA.get(),
                password=_PDF_PASSWORD.get(),
                envelope={
                    "metadata": result.metadata,
                    "media": result.media,
                    "links": result.links,
                    "quality_score": result.quality_score,
                    "table_of_contents": result.table_of_contents,
                    "page_type": result.page_type,
                    "source": result.source,
                    "archived_at": result.archived_at,
                },
            )
        return _apply_chunking(result, max_chars=max_chars, offset=offset)

    def _validate_smart_fetch_params(
        self,
        url: str,
        extraction_type: str,
        css_selector: Optional[str],
        extra_headers: Optional[Dict[str, str]],
        timeout: int | float,
        proxy: Optional[str | Dict[str, str]],
        useragent: Optional[str],
    ) -> tuple:
        """Validate and sanitize inputs for smart_fetch and related tools.

        Returns (validated_url, validated_css_selector, validated_headers,
                 validated_timeout, validated_proxy, validated_useragent).
        """
        url = validate_url(url)
        css_selector = validate_css_selector(css_selector)
        extra_headers = validate_headers(extra_headers)
        proxy = validate_proxy(proxy)

        # Timeout validation: browser uses ms, HTTP uses seconds.
        # Since smart_fetch can use both, validate as milliseconds (max 120s).
        timeout = validate_timeout(timeout)

        # User agent sanitization
        if useragent is not None:
            if not isinstance(useragent, str):
                raise SecurityError("User agent must be a string")
            useragent = useragent.strip()
            if "\n" in useragent or "\r" in useragent:
                raise SecurityError("User agent contains newline characters")

        return url, css_selector, extra_headers, timeout, proxy, useragent

    # âââ Session Management ââââââââââââââââââââââââââââââââââââââ

    async def open_session(
        self,
        session_type: SessionType,
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
        cookies: Sequence[SetCookieParam] | None = None,
        network_idle: bool = False,
        wait_selector_state: SelectorWaitStates = "attached",
        max_pages: int = 5,
        hide_canvas: bool = False,
        block_webrtc: bool = False,
        allow_webgl: bool = True,
        solve_cloudflare: bool = False,
        additional_args: Optional[Dict] = None,
    ) -> SessionCreatedModel:
        """Open a persistent browser session that can be reused across multiple fetch calls.

        This avoids the overhead of launching a new browser for each request.
        Internal helper â used by _ensure_auto_session to create the single
        warm stealthy session. Not exposed as an MCP tool.

        :param session_type: "dynamic" for standard Playwright, or "stealthy" for anti-bot bypass.
        :param session_id: Optional custom session ID (random 12-char hex if not provided).
        :param headless: Run browser headless (default True).
        :param google_search: Set Google referer header (default True).
        :param real_chrome: Use installed Chrome instead of Chromium.
        :param wait: Milliseconds to wait after everything finishes.
        :param proxy: Proxy string or dict with 'server', 'username', 'password'.
        :param timezone_id: Change browser timezone.
        :param locale: User locale, e.g., 'en-GB'.
        :param extra_headers: Extra headers to add to requests.
        :param useragent: Custom user agent string.
        :param cdp_url: Connect via CDP URL instead of launching a new browser.
        :param timeout: Timeout in milliseconds (default 30000).
        :param disable_resources: Drop font/image/media/stylesheet requests for speed.
        :param wait_selector: CSS selector to wait for before proceeding.
        :param cookies: Cookies for the session.
        :param network_idle: Wait until no network connections for 500ms.
        :param wait_selector_state: 'attached', 'detached', 'visible', or 'hidden'.
        :param max_pages: Max concurrent browser tabs (default 5).
        :param hide_canvas: (Stealthy) Random canvas noise for anti-fingerprinting.
        :param block_webrtc: (Stealthy) Prevent IP leak via WebRTC.
        :param allow_webgl: (Stealthy) Keep WebGL enabled (default True; WAFs check for it).
        :param solve_cloudflare: (Stealthy) Auto-solve Cloudflare challenges.
        :param additional_args: (Stealthy) Extra Playwright context args.
        """
        opened = await self._browser_sessions.open(
            session_type,
            session_id=session_id,
            headless=headless,
            google_search=google_search,
            real_chrome=real_chrome,
            wait=wait,
            proxy=proxy,
            timezone_id=timezone_id,
            locale=locale,
            extra_headers=extra_headers,
            useragent=useragent,
            cdp_url=cdp_url,
            timeout=timeout,
            disable_resources=disable_resources,
            wait_selector=wait_selector,
            cookies=cookies,
            network_idle=network_idle,
            wait_selector_state=wait_selector_state,
            max_pages=max_pages,
            hide_canvas=hide_canvas,
            block_webrtc=block_webrtc,
            allow_webgl=allow_webgl,
            solve_cloudflare=solve_cloudflare,
            additional_args=additional_args,
        )
        return SessionCreatedModel(
            session_id=opened.session_id,
            session_type=opened.entry.session_type,
            created_at=opened.entry.created_at,
            is_alive=True,
            message=f"Session '{opened.session_id}' ({session_type}) created successfully.",
        )

    async def close_session(self, session_id: Annotated[str, Field(description="Session ID to close")]) -> SessionClosedModel:
        """Close a persistent browser session and free its resources.

        :param session_id: The unique identifier of the session to close.
        """
        await self._browser_sessions.close(session_id)
        return SessionClosedModel(
            session_id=session_id,
            message=f"Session '{session_id}' closed successfully.",
        )

    # âââ Screenshot âââââââââââââââââââââââââââââââââââââââââââââââ

    async def screenshot(
        self,
        url: str,
        session_id: Optional[str] = None,
        image_type: ScreenshotType = "png",
        full_page: bool = False,
        quality: Optional[int] = None,
        wait: int | float = 0,
        wait_selector: Optional[str] = None,
        wait_selector_state: SelectorWaitStates = "attached",
        network_idle: bool = False,
        timeout: int | float = 30000,
    ) -> List[ImageContent | TextContent]:
        """Capture a screenshot of a web page.

        If session_id is omitted, a stealthy browser session is auto-managed
        (reused across calls, so no cold-start after the first screenshot).
        Pass session_id only to reuse a specific session from open_session.

        :param url: The URL to navigate to and capture.
        :param session_id: Optional ID of an open browser session. If omitted, a stealthy session is auto-managed.
        :param image_type: Image format: "png" (default) or "jpeg".
        :param full_page: Capture full scrollable page instead of viewport.
        :param quality: JPEG quality (0-100), only for jpeg.
        :param wait: Milliseconds to wait after page load.
        :param wait_selector: CSS selector to wait for.
        :param wait_selector_state: State to wait for.
        :param network_idle: Wait for no network connections for 500ms.
        :param timeout: Timeout in milliseconds (default 30000).
        """
        url = validate_url(url)
        validate_css_selector(wait_selector)

        if not _browser_deps_available():
            raise RuntimeError(
                f"Screenshot requires browser deps which are unavailable: "
                f"{_browser_import_error or 'patchright not importable'}. "
                "Install with: uv sync"
            )

        if quality is not None and image_type != "jpeg":
            raise ValueError("'quality' is only valid when 'image_type' is 'jpeg'.")

        # Auto-manage a stealthy session when none is provided (mirrors smart_fetch).
        if session_id:
            ssid = session_id
        else:
            ssid = await self._ensure_auto_session("stealthy")
        entry = await self._get_session(ssid, expected_type=None)
        screenshot_kwargs: Dict[str, Any] = {"type": image_type, "full_page": full_page}
        if quality is not None:
            screenshot_kwargs["quality"] = quality

        captured: Dict[str, Any] = {}

        async def _capture(page: Any) -> None:
            try:
                captured["bytes"] = await page.screenshot(**screenshot_kwargs)
                captured["url"] = page.url
            except Exception as exc:
                captured["error"] = exc

        await entry.session.fetch(
            url, wait=wait, timeout=timeout, network_idle=network_idle,
            wait_selector=wait_selector, wait_selector_state=wait_selector_state,
            page_action=_capture,
        )

        if "error" in captured:
            raise captured["error"]
        if "bytes" not in captured:
            raise RuntimeError(f"Failed to capture screenshot for {url}")

        from mcp.server.mcpserver import Image  # lazy: mcpserver is ~1s to import, only needed for screenshots
        from mcp.types import TextContent  # lazy: mcp.types is ~1s; only needed for screenshot output
        image = Image(data=captured["bytes"], format=image_type).to_image_content()
        return [image, TextContent(type="text", text=captured["url"])]

    # âââ HTTP Fetcher (curl_cffi) âââââââââââââââââââââââââââââââââ

    @staticmethod
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
        from sieve.server_fetch import get as _impl
        return await _impl(url=url, impersonate=impersonate, extraction_type=extraction_type, css_selector=css_selector, main_content_only=main_content_only, use_trafilatura=use_trafilatura, params=params, headers=headers, cookies=cookies, timeout=timeout, follow_redirects=follow_redirects, max_redirects=max_redirects, retries=retries, retry_delay=retry_delay, proxy=proxy, proxy_auth=proxy_auth, auth=auth, verify=verify, http3=http3, stealthy_headers=stealthy_headers)

    @staticmethod
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
        from sieve.server_fetch import bulk_get as _impl
        return await _impl(urls=urls, impersonate=impersonate, extraction_type=extraction_type, css_selector=css_selector, main_content_only=main_content_only, use_trafilatura=use_trafilatura, params=params, headers=headers, cookies=cookies, timeout=timeout, follow_redirects=follow_redirects, max_redirects=max_redirects, retries=retries, retry_delay=retry_delay, proxy=proxy, proxy_auth=proxy_auth, auth=auth, verify=verify, http3=http3, stealthy_headers=stealthy_headers)

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
        from sieve.server_fetch import fetch as _impl
        return await _impl(self, url=url, extraction_type=extraction_type, css_selector=css_selector, main_content_only=main_content_only, use_trafilatura=use_trafilatura, headless=headless, google_search=google_search, real_chrome=real_chrome, wait=wait, proxy=proxy, timezone_id=timezone_id, locale=locale, extra_headers=extra_headers, useragent=useragent, cdp_url=cdp_url, timeout=timeout, disable_resources=disable_resources, wait_selector=wait_selector, cookies=cookies, network_idle=network_idle, wait_selector_state=wait_selector_state, session_id=session_id)

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
        from sieve.server_fetch import bulk_fetch as _impl
        return await _impl(self, urls=urls, extraction_type=extraction_type, css_selector=css_selector, main_content_only=main_content_only, use_trafilatura=use_trafilatura, headless=headless, google_search=google_search, real_chrome=real_chrome, wait=wait, proxy=proxy, timezone_id=timezone_id, locale=locale, extra_headers=extra_headers, useragent=useragent, cdp_url=cdp_url, timeout=timeout, disable_resources=disable_resources, wait_selector=wait_selector, cookies=cookies, network_idle=network_idle, wait_selector_state=wait_selector_state, session_id=session_id)

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
        from sieve.server_fetch import stealthy_fetch as _impl
        return await _impl(self, url=url, extraction_type=extraction_type, css_selector=css_selector, main_content_only=main_content_only, use_trafilatura=use_trafilatura, headless=headless, google_search=google_search, real_chrome=real_chrome, wait=wait, proxy=proxy, timezone_id=timezone_id, locale=locale, extra_headers=extra_headers, useragent=useragent, hide_canvas=hide_canvas, cdp_url=cdp_url, timeout=timeout, disable_resources=disable_resources, wait_selector=wait_selector, cookies=cookies, network_idle=network_idle, wait_selector_state=wait_selector_state, block_webrtc=block_webrtc, allow_webgl=allow_webgl, solve_cloudflare=solve_cloudflare, additional_args=additional_args, session_id=session_id, page_action=page_action, capture_xhr=capture_xhr, capture_pattern=capture_pattern)

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
        from sieve.server_fetch import bulk_stealthy_fetch as _impl
        return await _impl(self, urls=urls, extraction_type=extraction_type, css_selector=css_selector, main_content_only=main_content_only, use_trafilatura=use_trafilatura, headless=headless, google_search=google_search, real_chrome=real_chrome, wait=wait, proxy=proxy, timezone_id=timezone_id, locale=locale, extra_headers=extra_headers, useragent=useragent, hide_canvas=hide_canvas, cdp_url=cdp_url, timeout=timeout, disable_resources=disable_resources, wait_selector=wait_selector, cookies=cookies, network_idle=network_idle, wait_selector_state=wait_selector_state, block_webrtc=block_webrtc, allow_webgl=allow_webgl, solve_cloudflare=solve_cloudflare, additional_args=additional_args, session_id=session_id, page_action=page_action, capture_xhr=capture_xhr, capture_pattern=capture_pattern)

    async def _http_with_retry(self, url: str, **kwargs) -> ResponseModel:
        from sieve.server_fetch import _http_with_retry as _impl
        return await _impl(self, url=url, **kwargs)

    async def smart_fetch(
        self,
        url: Annotated[str, Field(description="Single URL to fetch.")],
        urls: Annotated[Optional[List[str]], Field(description="Multiple URLs to fetch in parallel. Returns bulk results. Use instead of calling smart_fetch multiple times.")] = None,
        extraction_type: Annotated[ExtendedExtractionType, Field(description="Content format: 'markdown' (default), 'html', 'text', 'article', 'structured'.")] = "markdown",
        css_selector: Annotated[Optional[str], Field(description="CSS selector to narrow extracted content (e.g. 'article', '.main-content').")] = None,
        main_content_only: Annotated[bool, Field(description="Strip nav, ads, footers (default True).")] = True,
        use_trafilatura: Annotated[bool, Field(description="Use Trafilatura for cleaner article extraction (default True).")] = True,
        cache_ttl: Annotated[int, Field(description="Cache duration in seconds. Default 3600 (1 hour). Set 0 to skip cache and force a fresh fetch.")] = DEFAULT_TTL,
        force_fetcher: Annotated[Optional[Literal["http", "dynamic", "stealthy", "sleeper"]], Field(description="Lock to one fetcher tier. 'http' = fast HTTP-only, 'stealthy' = pool browser, or 'sleeper' = real-browser daemon. Skips auto-escalation.")] = None,
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
        from sieve.server_fetch import smart_fetch as _impl
        return await _impl(self, url=url, urls=urls, extraction_type=extraction_type, css_selector=css_selector, main_content_only=main_content_only, use_trafilatura=use_trafilatura, cache_ttl=cache_ttl, force_fetcher=force_fetcher, respect_robots=respect_robots, headless=headless, real_chrome=real_chrome, wait=wait, proxy=proxy, timeout=timeout, network_idle=network_idle, solve_cloudflare=solve_cloudflare, block_webrtc=block_webrtc, hide_canvas=hide_canvas, extra_headers=extra_headers, useragent=useragent, cookies=cookies, offset=offset, max_content_chars=max_content_chars, pages=pages, password=password, focus=focus, actions=actions, include_media=include_media, include_links=include_links, capture_xhr=capture_xhr, capture_pattern=capture_pattern, fold_captured=fold_captured)

    async def _smart_fetch_bulk(
        self, urls, extraction_type, css_selector, main_content_only,
        use_trafilatura, cache_ttl, force_fetcher, respect_robots,
        headless, real_chrome, wait, proxy, timeout, network_idle,
        solve_cloudflare, block_webrtc, hide_canvas, extra_headers,
        useragent, cookies, max_chars: int = MAX_CONTENT_CHARS,
        include_media: bool = False, include_links: bool = False,
        focus: Optional[str] = None,
    ) -> BulkResponseModel:
        from sieve.server_fetch import _smart_fetch_bulk as _impl
        return await _impl(self, urls=urls, extraction_type=extraction_type, css_selector=css_selector, main_content_only=main_content_only, use_trafilatura=use_trafilatura, cache_ttl=cache_ttl, force_fetcher=force_fetcher, respect_robots=respect_robots, headless=headless, real_chrome=real_chrome, wait=wait, proxy=proxy, timeout=timeout, network_idle=network_idle, solve_cloudflare=solve_cloudflare, block_webrtc=block_webrtc, hide_canvas=hide_canvas, extra_headers=extra_headers, useragent=useragent, cookies=cookies, max_chars=max_chars, include_media=include_media, include_links=include_links, focus=focus)

    async def _force_fetch(
        self, url, force_fetcher, extraction_type, css_selector,
        main_content_only, use_trafilatura, cache_ttl, offset,
        headless, real_chrome, wait, proxy, timeout, network_idle,
        solve_cloudflare, block_webrtc, hide_canvas, extra_headers,
        useragent, cookies, max_chars: int = MAX_CONTENT_CHARS,
        page_action=None, capture_xhr: bool = False,
        capture_pattern: Optional[str] = None, fold_captured: bool = False,
    ) -> ResponseModel:
        from sieve.server_fetch import _force_fetch as _impl
        return await _impl(self, url=url, force_fetcher=force_fetcher, extraction_type=extraction_type, css_selector=css_selector, main_content_only=main_content_only, use_trafilatura=use_trafilatura, cache_ttl=cache_ttl, offset=offset, headless=headless, real_chrome=real_chrome, wait=wait, proxy=proxy, timeout=timeout, network_idle=network_idle, solve_cloudflare=solve_cloudflare, block_webrtc=block_webrtc, hide_canvas=hide_canvas, extra_headers=extra_headers, useragent=useragent, cookies=cookies, max_chars=max_chars, page_action=page_action, capture_xhr=capture_xhr, capture_pattern=capture_pattern, fold_captured=fold_captured)

    async def _capture_pass(
        self, url, extraction_type, css_selector, main_content_only,
        use_trafilatura, cache_ttl, offset, headless, real_chrome, wait,
        proxy, timeout, network_idle, solve_cloudflare, block_webrtc,
        hide_canvas, extra_headers, useragent, cookies, max_chars,
        fold_captured: bool = False,
    ) -> Optional[ResponseModel]:
        from sieve.server_fetch import _capture_pass as _impl
        return await _impl(self, url=url, extraction_type=extraction_type, css_selector=css_selector, main_content_only=main_content_only, use_trafilatura=use_trafilatura, cache_ttl=cache_ttl, offset=offset, headless=headless, real_chrome=real_chrome, wait=wait, proxy=proxy, timeout=timeout, network_idle=network_idle, solve_cloudflare=solve_cloudflare, block_webrtc=block_webrtc, hide_canvas=hide_canvas, extra_headers=extra_headers, useragent=useragent, cookies=cookies, max_chars=max_chars, fold_captured=fold_captured)

    async def _auto_escalate(
        self, url, extraction_type, css_selector, main_content_only,
        use_trafilatura, cache_ttl, offset, headless, real_chrome, wait,
        proxy, timeout, network_idle, solve_cloudflare, block_webrtc,
        hide_canvas, extra_headers, useragent, cookies, max_chars: int = MAX_CONTENT_CHARS,
        fold_captured: bool = False,
    ) -> ResponseModel:
        from sieve.server_fetch import _auto_escalate as _impl
        return await _impl(self, url=url, extraction_type=extraction_type, css_selector=css_selector, main_content_only=main_content_only, use_trafilatura=use_trafilatura, cache_ttl=cache_ttl, offset=offset, headless=headless, real_chrome=real_chrome, wait=wait, proxy=proxy, timeout=timeout, network_idle=network_idle, solve_cloudflare=solve_cloudflare, block_webrtc=block_webrtc, hide_canvas=hide_canvas, extra_headers=extra_headers, useragent=useragent, cookies=cookies, max_chars=max_chars, fold_captured=fold_captured)


    # âââ Cache Management ââââââââââââââââââââââââââââââââââââââââââ

    async def cache_clear(self, all: Annotated[bool, Field(description="True=wipe all, False=expired only")] = False) -> CacheInfoModel:
        """Clear expired cache entries, or all entries if 'all' is True.

        :param all: If True, clear ALL cache entries. If False (default), only expired ones.
        """
        if all:
            count = await clear_all_cache()
            return CacheInfoModel(message=f"Cleared all {count} cache entries.", purged=count)
        else:
            count = await clear_cache()
            await clear_robots_cache()
            return CacheInfoModel(
                message=f"Cleared {count} expired cache entries.", purged=count,
            )

    # âââ Version ââââââââââââââââââââââââââââââââââââââââââââââââââ

    async def version(self) -> VersionInfoModel:
        """Return the installed local Sieve build identity.

        Sieve is a standalone source checkout. It deliberately does not
        compare its version with the legacy sieve distribution on PyPI.
        """
        from sieve import updater
        installed, latest, is_current = await asyncio_to_thread(updater.check_version)
        # up_to_date: True if at or ahead of PyPI (no update needed)
        up_to_date = is_current if is_current is not None else True
        if not up_to_date and latest:
            try:
                if updater.pad_version(installed) > updater.pad_version(latest):
                    up_to_date = True
            except (ValueError, IndexError):
                pass
        return VersionInfoModel(
            version=installed,
            latest=latest or "",
            up_to_date=up_to_date,
            update_command="",
        )

    # âââ Extract (schema â JSON) âââââââââââââââââââââââââââââââââ
    # Ported from crawl4ai v0.9.2 extraction_strategy.py (JsonCss/XPath),
    # wired as a Sieve tool: fetch a URL (any tier) then apply a declarative
    # schema over the DOM to return structured JSON â no LLM required.

    @budgeted
    async def extract(self, url: str, schema: dict | None = None, *, xpath: bool = False,
                      extraction_type: str = "html", tables: bool = False,
                      chunk: str | None = None, smart: str | None = None,
                      **fetch_kwargs) -> dict:
        """Fetch a URL and extract structured JSON via a declarative schema.

        :param url: URL to fetch (uses Sieve's smart_fetch pipeline).
        :param schema: ``{"baseSelector": "...", "fields": [{name, selector,
            type, transform}]}`` â same format as crawl4ai JsonCss/XPath
            strategies. ``type``: text|attribute|html|regex|nested.
        :param xpath: True = XPath selectors, False = CSS.
        :param extraction_type: pass through to smart_fetch (default html).
        :return: ``{"url", "status", "content_ok", "blocked", "items",
            "error"}`` where items is the list of extracted dicts.
        """
        from sieve.extraction import extract_json
        from sieve.antibot_detector import detect_block

        if not url:
            raise ValueError("'url' must be provided")
        if schema is not None and not isinstance(schema, dict):
            raise ValueError("'schema' must be an object with baseSelector + fields")
        if schema is None and not tables and chunk is None and not smart:
            raise ValueError("'schema' is required unless --tables, --chunk, or --smart is given")

        kw = {**fetch_kwargs}
        kw.setdefault("extraction_type", extraction_type)
        kw.setdefault("cache_ttl", 0)
        result = await self.smart_fetch(url=url, **kw)

        payload = result.model_dump()
        # Block detection over whatever HTML/text we got back.
        html_src = ""
        if payload.get("content"):
            html_src = "\n".join(payload["content"])
        block = detect_block(
            payload.get("status", 0),
            payload.get("headers") or {},
            html_src,
        )
        payload["blocked"] = block
        if block.get("blocked"):
            return {
                "url": url, "status": payload.get("status"),
                "content_ok": False, "blocked": block,
                "items": [], "error": payload.get("error") or f"blocked: {block.get('kind')}",
            }

        if not payload.get("content_ok"):
            return {
                "url": url, "status": payload.get("status"),
                "content_ok": False, "blocked": block,
                "items": [], "error": payload.get("error") or payload.get("summary", "fetch failed"),
            }

        # Re-fetch raw HTML when the pipeline returned markdown/text.
        if extraction_type != "html" or not payload.get("content"):
            raw = await self.smart_fetch(url=url, extraction_type="html", cache_ttl=0)
            html_src = "\n".join(raw.model_dump().get("content") or [])

        items = []
        from sieve.dom_budget import DomWorkBudget
        from sieve.resource_budget import current_budget
        work_budget = DomWorkBudget()
        extraction_error = ""
        if schema:
            try:
                items = extract_json(html_src, schema, xpath=xpath, work_budget=work_budget)
            except Exception as e:  # extraction failures shouldn't kill the tool
                extraction_error = safe_error(e, fallback_category="extract")["error"]
        result = {
            "url": url, "status": payload.get("status"),
            "content_ok": not bool(extraction_error), "blocked": block,
            "items": items, "error": extraction_error,
        }
        if tables:
            try:
                from sieve.tables import extract_tables
                result["tables"] = extract_tables(html_src, work_budget=work_budget)
            except Exception as e:
                result["tables"] = []
                result["tables_error"] = safe_error(e, fallback_category="extract")["error"]
        if chunk:
            try:
                from sieve.chunking import chunk_text
                text_src = "\n".join(payload.get("content") or []) or html_src
                result["chunks"] = chunk_text(text_src, chunk)
            except Exception as e:
                result["chunks"] = []
                result["chunk_error"] = safe_error(e, fallback_category="extract")["error"]
        if smart:
            try:
                from sieve.smart_extract import smart_extract
                text_src = "\n".join(payload.get("content") or []) or html_src
                result["smart"] = smart_extract(text_src, smart)
            except Exception as e:
                result["smart"] = {}
                result["smart_error"] = safe_error(e, fallback_category="extract")["error"]
        account = current_budget()
        if account is not None:
            from sieve.resource_budget import bound_output
            for field in ("items", "tables", "chunks", "smart"):
                if field in result:
                    result[field] = bound_output(result[field])
            result["resource_budget"] = account.report()
        return result

    # âââ Search ââââââââââââââââââââââââââââââââââââââââââââââââââââ

    async def smart_search(self, query: str, max_results: int = 6, cache_ttl: int = 300, mode: str = "auto", engines: Optional[List[str]] = None, url: Optional[str] = None, site: Optional[str] = None, exclude_sites: Optional[List[str]] = None, location: Optional[str] = None, language: Optional[str] = None, region: Optional[str] = None, page: int = 0, freshness: Optional[str] = None, cached: bool = False, refresh: bool = False, stale_fallback: bool = False, stale_max_age: int = 3600) -> SearchResponseModel:
        from sieve.server_search import smart_search as _impl
        return await _impl(self, query, max_results, cache_ttl, mode=mode, engines=engines, url=url, site=site, exclude_sites=exclude_sites, location=location, language=language, region=region, page=page, freshness=freshness, cached=cached, refresh=refresh, stale_fallback=stale_fallback, stale_max_age=stale_max_age)

    async def smart_crawl(self, url: str, max_pages: int = 10, max_depth: int = 2, path_include: Optional[List[str]] = None, path_exclude: Optional[List[str]] = None, discover_only: bool = False, focus: Optional[str] = None, crawl_urls: Optional[List[str]] = None, max_content_chars_per: int = 8000, max_total_chars: Optional[int] = None, concurrency: int = 3, cache_ttl: int = DEFAULT_TTL, respect_robots: bool = True, force_fetcher: Optional[str] = None, timeout: int = 30000, deadline_ms: int = 120000, sitemap: str | bool = False, auto_throttle: bool = False, throttle_min_delay: float = 2.0) -> "CrawlResponseModel":
        from sieve.server_search import smart_crawl as _impl
        return await _impl(self, url, max_pages=max_pages, max_depth=max_depth, path_include=path_include, path_exclude=path_exclude, discover_only=discover_only, focus=focus, crawl_urls=crawl_urls, max_content_chars_per=max_content_chars_per, max_total_chars=max_total_chars, concurrency=concurrency, cache_ttl=cache_ttl, respect_robots=respect_robots, force_fetcher=force_fetcher, timeout=timeout, deadline_ms=deadline_ms, sitemap=sitemap, auto_throttle=auto_throttle, throttle_min_delay=throttle_min_delay)

    # âââ Serve âââââââââââââââââââââââââââââââââââââââââââââââââââââ

    def build_mcp_server(self):
        """Construct the low-level MCP Server (mcp 2.x API).

        Kept separate from serve() so tests can drive the real MCP layer
        in-process (mcp Client(server)) instead of spawning stdio.
        """
        from mcp.server import Server, ServerRequestContext
        from mcp.types import (
            CallToolRequestParams,
            CallToolResult,
            ListToolsResult,
            PaginatedRequestParams,
            TextContent,
            Tool,
        )

        # 2026-07-28 stateless: allow server/discover without initialize
        try:
            from mcp.server import runner as _runner
            from mcp_types.methods import SPEC_CLIENT_METHODS
            _runner._INIT_EXEMPT = frozenset(SPEC_CLIENT_METHODS)
        except Exception:
            pass
        try:
            from mcp_types.methods import validate_client_request as _ov, LATEST_PROTOCOL_VERSION
            if not getattr(_ov, "_sieve_patched", False):
                from mcp_types.methods import serialize_server_result as _os
                import mcp_types.methods as _mt
                def _pv(m, v, p, **kw):
                    if p is not None and "_meta" not in (p or {}):
                        p = {**(p or {}), "_meta": {"io.modelcontextprotocol/protocolVersion": LATEST_PROTOCOL_VERSION, "io.modelcontextprotocol/clientCapabilities": {}}}
                    try: return _ov(m, v, p, **kw)
                    except KeyError: return _ov(m, LATEST_PROTOCOL_VERSION, p, **kw)
                _pv._sieve_patched = True
                def _ps(m, v, d, **kw):
                    try: return _os(m, v, d, **kw)
                    except KeyError: return _os(m, LATEST_PROTOCOL_VERSION, d, **kw)
                _mt.validate_client_request = _pv; _mt.serialize_server_result = _ps
                _runner._methods = _mt
        except Exception:
            pass

        # ââ list_tools: return hand-crafted minimal definitions ââââââ
        async def handle_list_tools(
            ctx: ServerRequestContext, params: PaginatedRequestParams | None,
        ) -> ListToolsResult:
            definitions = [capability.definition for capability in CAPABILITY_REGISTRY.values()
                           if capability.definition is not None]
            # MCP cursors are opaque. The catalog is static and small enough
            # to fit in one page; reject a cursor rather than silently lying
            # about pagination support.
            if params is not None and params.cursor:
                return ListToolsResult(tools=[])
            return ListToolsResult(tools=[Tool(**td) for td in definitions])

        # ââ call_tool: dispatch to existing methods âââââââââââââââââ
        # v2 contract: handlers return full result types and exceptions do
        # NOT become is_error results (they become protocol errors, which
        # most clients raise instead of showing the LLM). Every tool error
        # the agent should see is caught here and returned as is_error=True.
        async def handle_call_tool(
            ctx: ServerRequestContext, params: CallToolRequestParams,
        ) -> CallToolResult:
            try:
                if params.name not in _PUBLIC_MCP_TOOLS:
                    raise ValueError(f"Unknown MCP tool: {params.name}")
                result = await self._dispatch(params.name, params.arguments or {})
                # _dispatch returns (content_list, structured_dict) or just content_list
                if isinstance(result, tuple):
                    content_list, structured = result
                else:
                    content_list, structured = result, None
                # Both text and structuredContent cross the same boundary.
                # Reserve space for JSON escaping, duplicated content, and the
                # transport envelope instead of budgeting each copy independently.
                component_budget = MAX_PUBLIC_OUTPUT_BYTES // 8
                clean_structured = None if structured is None else json.loads(
                    safe_public_json(structured, max_bytes=component_budget),
                )
                clean_content = []
                remaining = MAX_PUBLIC_OUTPUT_BYTES // 2
                for item in content_list:
                    if remaining < 256:
                        clean_content.append(TextContent(type="text", text='{"_truncated":true}'))
                        break
                    item_budget = min(component_budget, remaining // 8)
                    if item.type == "text":
                        text = item.text
                        if len(text) > item_budget:
                            # The structured copy preserves partial results when
                            # a legacy branch already produced oversized text.
                            text = safe_public_json(clean_structured or {"_truncated": True}, max_bytes=item_budget)
                        else:
                            parsed_json = True
                            try:
                                value = json.loads(text)
                            except (ValueError, RecursionError):
                                value = text
                                parsed_json = False
                            text = safe_public_json(value, max_bytes=item_budget)
                            if not parsed_json:
                                text = json.loads(text)
                        clean_item = TextContent(type="text", text=text)
                    else:
                        if item.type == "image" and len(item.data) > item_budget:
                            clean_item = TextContent(type="text", text='{"_truncated":true}')
                        else:
                            try:
                                data = json.loads(safe_public_json(item, max_bytes=item_budget))
                                if data.get("_truncated") or "_truncated" in data.get("data", ""):
                                    raise ValueError("content withheld by output budget")
                                clean_item = type(item).model_validate(data)
                            except (TypeError, ValueError):
                                clean_item = TextContent(type="text", text='{"_truncated":true}')
                    remaining -= len(safe_public_json(clean_item).encode("utf-8"))
                    clean_content.append(clean_item)
                return CallToolResult(content=clean_content, structured_content=clean_structured)
            except Exception as e:
                error_text = safe_public_json(safe_error(e), max_bytes=max(19, MAX_PUBLIC_OUTPUT_BYTES // 8))
                return CallToolResult(
                    content=[TextContent(type="text", text=error_text)],
                    is_error=True,
                )

        return Server(
            "Sieve",
            version=__version__,
            instructions=SIEVE_INSTRUCTIONS,
            website_url="https://github.com/shy-tangerine/Sieve",
            on_list_tools=handle_list_tools,
            on_call_tool=handle_call_tool,
        )

    def serve(self, http: bool = False, host: str = "127.0.0.1", port: int = 8765):
        """Start the MCP server using low-level Server for minimal token overhead.

        When ``http`` is False (default) the server runs over stdio, which is what
        Claude Code, Cursor, OpenCode, and other local MCP clients expect. When
        True it exposes the **streamable HTTP** transport (MCP 2025-03-26 spec)
        at ``http://host:port/mcp``, which is what Open WebUI (v0.6.31+) and
        other HTTP MCP clients connect to directly, no proxy needed. The legacy
        SSE transport was removed (deprecated in the spec)."""
        # Runtime auth invariant (issues #79/#154): a non-loopback bind without
        # SIEVE_AUTH_TOKEN refuses to start instead of serving an unauthenticated
        # endpoint. This is the enforcement the security docs promise; the
        # container ENTRYPOINT binds 0.0.0.0, so an image without the token set
        # fails fast rather than exposing research/fetch capability to the network.
        if http:
            import os as _os
            _token_set = bool(_os.environ.get("SIEVE_AUTH_TOKEN", "").strip())
            _loopback = host in ("127.0.0.1", "localhost", "::1")
            if not _loopback and not _token_set:
                raise SystemExit(
                    "refusing to start: binding the MCP HTTP transport to a "
                    "non-loopback address requires SIEVE_AUTH_TOKEN to be set "
                    "(clients must send 'Authorization: Bearer <token>'). "
                    "Bind to 127.0.0.1 for localhost-only use."
                )
        server = self.build_mcp_server()

        if not http:
            import anyio
            from mcp.server.stdio import stdio_server

            async def _run():
                # Warm the single stealthy browser at startup so it's ready before
                # the agent's first stealthy fetch/screenshot. It stays alive until
                # the idle monitor closes it after SIEVE_BROWSER_IDLE_TIMEOUT of
                # inactivity (default 300s), then relaunches on the next fetch.
                # Best-effort, runs in the background while the server handles the
                # initialize handshake.
                warm = asyncio.create_task(self._prewarm_stealthy())
                warm_reranker = asyncio.create_task(
                    _safe_imported_prewarm("sieve.reranker", "prewarm_reranker")
                )
                try:
                    async with stdio_server() as (read, write):
                        await server.run(read, write, server.create_initialization_options())
                finally:
                    # Bulletproof teardown: cancel prewarm tasks + close sessions,
                    # swallowing EVERYTHING (including 'Event loop is closed' and
                    # BaseException) so the process always exits cleanly. A noisy
                    # teardown traceback must never look like a server crash to the
                    # MCP client (which reports it as 'failed to load').
                    for _t in (warm, warm_reranker):
                        try:
                            _t.cancel()
                        except BaseException:
                            pass
                    for _t in (warm, warm_reranker):
                        try:
                            await _t
                        except BaseException:
                            pass
                    try:
                        await self._shutdown_close_sessions()
                    except BaseException:
                        pass

            anyio.run(_run)
        else:
            # Streamable HTTP transport (MCP 2025-03-26 spec). This is the
            # transport Open WebUI (v0.6.31+) and other modern HTTP MCP clients
            # connect to directly, no mcpo proxy needed. Endpoint: http://host:port/mcp
            import uvicorn

            app = self.build_http_asgi_app(server)
            uvicorn.run(app, host=host, port=port)

    def build_http_asgi_app(self, server=None):
        """Build the streamable-HTTP MCP ASGI application (issues #56/#159/#173).

        Kept separate from ``serve()`` so the exact wire surface — bearer
        middleware, stateless mode, session manager, lifespan — can be
        driven in-process by the HTTP-wire tests without binding a port or
        running uvicorn. ``server`` defaults to the real MCP server from
        :meth:`build_mcp_server`; tests may inject a stub.
        """
        from contextlib import asynccontextmanager
        from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
        from starlette.applications import Starlette
        from starlette.routing import Route

        if server is None:
            server = self.build_mcp_server()
        manager = StreamableHTTPSessionManager(app=server)

        # Auth gate (Scrapling MCP pattern): when SIEVE_AUTH_TOKEN is set,
        # reject any request without a matching `Authorization: Bearer <token>`.
        # Unset = localhost-only trust (default).
        import os as _os
        _auth_token = _os.environ.get("SIEVE_AUTH_TOKEN", "").strip()

        class _StreamableHTTPASGIApp:
            async def __call__(self, scope, receive, send):
                if _auth_token and not _valid_bearer_authorization(
                        scope.get("headers") or [], _auth_token):
                        from starlette.responses import JSONResponse
                        await JSONResponse(
                            {"error": "unauthorized: missing or invalid Authorization Bearer token"},
                            status_code=401,
                        )(scope, receive, send)
                        return
                # ?stateless=true — MCP 2026-07-28 stateless transport
                qs = scope.get("query_string", b"").decode("latin-1")
                qp = {kv.split("=",1)[0]:(kv.split("=",1)[1] if "=" in kv else "") for kv in qs.split("&") if kv}
                if qp.get("stateless","").lower() == "true":
                    from mcp.server.streamable_http_manager import StreamableHTTPSessionManager as _SHM
                    sm = _SHM(app=server, stateless=True)
                    async with sm.run():
                        await sm.handle_request(scope, receive, send)
                    return
                await manager.handle_request(scope, receive, send)

        @asynccontextmanager
        async def lifespan(app):
            warm = asyncio.create_task(self._prewarm_stealthy())
            warm_reranker = asyncio.create_task(
                _safe_imported_prewarm("sieve.reranker", "prewarm_reranker")
            )
            try:
                async with manager.run():
                    yield
            finally:
                for _t in (warm, warm_reranker):
                    try:
                        _t.cancel()
                    except BaseException:
                        pass
                for _t in (warm, warm_reranker):
                    try:
                        await _t
                    except BaseException:
                        pass
                try:
                    await self._shutdown_close_sessions()
                except BaseException:
                    pass

        return Starlette(routes=[Route("/mcp", endpoint=_StreamableHTTPASGIApp())], lifespan=lifespan)

    async def _dispatch(self, name: str, args: dict) -> list | tuple:
        """Route MCP tool calls to internal methods and format responses.

        Returns either:
        - (content_list, structured_dict) for tools with structured output
        - content_list for tools with mixed content (e.g. screenshot with ImageContent)
        """
        from sieve.command_router import CapabilityRouter
        return await CapabilityRouter(self).dispatch(name, args)



def _help_epilog() -> str:
    """Styled epilog for `sieve --help`: the command cheat-sheet + docs link."""
    from sieve import cli_ui as ui
    return "\n".join([
        ui.dim("commands:"),
        f"  {ui.cyan('sieve youtube ...')}  {ui.dim('CLI Â· search, inspect, and download YouTube media')}",
        f"  {ui.cyan('sieve search ...')}   {ui.dim('keyless multi-engine search')}",
        f"  {ui.cyan('sieve fetch ...')}    {ui.dim('fetch URL(s) with auto anti-bot')}",
        f"  {ui.cyan('sieve crawl ...')}    {ui.dim('deep-crawl a site')}",
        f"  {ui.cyan('sieve extract ...')}  {ui.dim('schema extract â JSON')}",
        f"  {ui.cyan('sieve screenshot ...')} {ui.dim('screenshot a URL')}",
        f"  {ui.cyan('sieve cache ...')}    {ui.dim('cache clear / clear-all')}",
        f"  {ui.cyan('sieve social ...')}   {ui.dim('CLI Â· fetch and browser-collect public social media')}",
        f"  {ui.cyan('sieve -v')}           {ui.dim('version + update check')}",
        f"  {ui.cyan('sieve -u')}           {ui.dim('update to the latest version')}",
        f"  {ui.cyan('sieve --reinstall')}  {ui.dim('full reinstall with all deps + extras')}",
        f"  {ui.cyan('sieve --doctor')}     {ui.dim('health check + fix advice')}",
        f"  {ui.cyan('sieve --rollback')}   {ui.dim('undo the last update')}",
        "",
        ui.dim("docs:") + "  " + ui.cyan("https://github.com/shy-tangerine/Sieve"),
    ])


def _test_byok_keys(provider: str | None = None) -> None:
    """Test BYOK search API keys by making a live search request."""
    from sieve import cli_ui as ui
    from sieve.byok_config import load_byok_keys, redact_key
    import httpx
    import time

    keys = load_byok_keys()
    if not keys:
        print(ui.dim("  No search API keys configured."))
        print(ui.dim("  Add with: sieve keys add <provider> (secret prompted or piped; see --stdin)"))
        return

    # API endpoints + auth headers for live testing.
    _TEST_CONFIG: dict[str, dict] = {
        "serper": {"url": "https://google.serper.dev/search", "method": "POST",
                   "data": {"q": "test", "num": 1},
                   "headers": lambda k: {"X-API-KEY": k, "Content-Type": "application/json"}},
        "tavily": {"url": "https://api.tavily.com/search", "method": "POST",
                   "data": {"query": "test", "max_results": 1, "search_depth": "basic"},
                   "headers": lambda k: {"Authorization": f"Bearer {k}", "Content-Type": "application/json"}},
        "exa": {"url": "https://api.exa.ai/search", "method": "POST",
                "data": {"query": "test", "numResults": 1, "type": "auto"},
                "headers": lambda k: {"x-api-key": k, "Content-Type": "application/json"}},
        "firecrawl": {"url": "https://api.firecrawl.dev/v2/search", "method": "POST",
                      "data": {"query": "test", "limit": 1},
                      "headers": lambda k: {"Authorization": f"Bearer {k}", "Content-Type": "application/json"}},
        "tinyfish": {"url": "https://api.search.tinyfish.ai", "method": "GET",
                     "data": {"query": "test", "limit": 1},
                     "headers": lambda k: {"X-API-Key": k}},
    }

    providers_to_test = [provider] if provider else list(keys.keys())
    for p in providers_to_test:
        if p not in _TEST_CONFIG:
            print(f"  {ui.red('ERROR')} Unknown provider: {p}")
            continue
        pkeys = keys.get(p, [])
        if not pkeys:
            print(f"  {ui.dim(p)}: no keys configured")
            continue
        cfg = _TEST_CONFIG[p]
        for i, key in enumerate(pkeys):
            try:
                t0 = time.time()
                if cfg["method"] == "GET":
                    resp = httpx.get(cfg["url"], params=cfg["data"],
                                   headers=cfg["headers"](key), timeout=10)
                else:
                    resp = httpx.post(cfg["url"], json=cfg["data"],
                                    headers=cfg["headers"](key), timeout=10)
                elapsed = time.time() - t0
                if resp.status_code == 200:
                    print(f"  {ui.ok('PASS')} {p}[{i}] {redact_key(key)} -> 200 OK ({elapsed:.1f}s)")
                elif resp.status_code == 429:
                    print(f"  {ui.warn('RATE')} {p}[{i}] {redact_key(key)} -> 429 rate-limited")
                elif resp.status_code in (401, 403):
                    print(f"  {ui.red('FAIL')} {p}[{i}] {redact_key(key)} -> {resp.status_code} unauthorized")
                else:
                    print(f"  {ui.red('FAIL')} {p}[{i}] {redact_key(key)} -> {resp.status_code}")
            except Exception as e:
                print(f"  {ui.red('FAIL')} {p}[{i}] {redact_key(key)} -> {safe_error(e)['error']}")


def _read_keys_add_secret(args) -> str:
    """Resolve the ``keys add`` secret without ever placing it in argv.

    Priority: ``--key-fd FD``, then ``--stdin``, then a hidden getpass prompt
    when stdin is an attached terminal. Ambiguous input (both explicit
    sources at once), EOF and empty secrets are rejected. The raw secret is
    never echoed, logged, or included in error messages.
    """
    key_fd = getattr(args, "key_fd", None)
    use_stdin = bool(getattr(args, "key_stdin", False))
    provider = getattr(args, "provider", "")

    if key_fd is not None and use_stdin:
        raise ValueError("Ambiguous secret input: pass either --key-fd or --stdin, not both")
    if key_fd is not None:
        try:
            raw = os.read(key_fd, 4096)
        except OSError as e:
            raise ValueError(f"Cannot read secret from fd {key_fd}: {e}") from e
        if not raw:
            raise ValueError(f"EOF reading secret from fd {key_fd}")
    elif use_stdin:
        raw = sys.stdin.readline() if sys.stdin is not None else ""
        if not raw:
            raise ValueError("EOF reading secret from stdin")
    elif sys.stdin is not None and sys.stdin.isatty():
        import getpass
        try:
            raw = getpass.getpass(f"API key for {provider}: ")
        except EOFError:
            raw = ""
        if not raw:
            raise ValueError("No secret entered")
    else:
        raise ValueError(
            "No secret provided. Pipe it with --stdin "
            "(e.g. printf '%s\\n' \"$KEY\" | sieve keys add <provider> --stdin) "
            "or run from an attached terminal to be prompted."
        )
    key = (raw.decode() if isinstance(raw, bytes) else raw).strip()
    if not key:
        raise ValueError("Key cannot be empty")
    return key


def main():
    """Entry point for the sieve CLI."""
    from sieve import cli_ui as ui
    from sieve import updater
    import argparse
    parser = argparse.ArgumentParser(
        prog="sieve",
        description=ui.branded(ui.dim("web research for AI agents Â· $0 Â· no keys"), ""),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=_help_epilog(),
    )
    parser.add_argument("--cache-ttl", type=int, default=3600,
                        help="default cache TTL in seconds (default 3600)")
    parser.add_argument("-v", "--version", action="store_true",
                        help="show version + update status")
    parser.add_argument("-u", "--update", action="store_true",
                        help="update sieve to the latest version")
    parser.add_argument("--doctor", action="store_true",
                        help="diagnose the install and suggest fixes")
    parser.add_argument("--rollback", action="store_true",
                        help="reinstall the version from before the last update")
    parser.add_argument("--reinstall", action="store_true",
                        help="full reinstall with all deps + [all] extras")
    subparsers = parser.add_subparsers(dest="keys_cmd",
                                       help="manage BYOK search API keys")
    keys_parser = subparsers.add_parser("keys", help="manage search API keys (serper, tavily, exa, firecrawl, tinyfish)")
    keys_sub = keys_parser.add_subparsers(dest="keys_action", help="keys sub-command")
    keys_add = keys_sub.add_parser(
        "add",
        help="add an API key for a provider (secret prompted or piped; never in argv)",
        usage="sieve keys add [-h] [--key-fd FD] [--stdin] provider",
    )
    keys_add.add_argument("provider", help="provider name (serper, tavily, exa, firecrawl, tinyfish)")
    keys_add.add_argument("key", nargs="?", default=None, help=argparse.SUPPRESS)
    keys_add.add_argument("--stdin", dest="key_stdin", action="store_true",
                          help="read the secret from stdin (single line)")
    keys_add.add_argument("--key-fd", dest="key_fd", type=int, default=None,
                          help="read the secret from this file descriptor (single line)")
    keys_list = keys_sub.add_parser("list", help="list configured API keys (redacted)")
    keys_test = keys_sub.add_parser("test", help="test API keys")
    keys_test.add_argument("provider", nargs="?", default=None, help="test only this provider (default: all)")
    keys_remove = keys_sub.add_parser("remove", help="remove an API key")
    keys_remove.add_argument("provider", help="provider name")
    keys_remove.add_argument("index", type=int, nargs="?", default=None, help="key index (0-based). Omit to remove all keys for this provider")
    keys_clear = keys_sub.add_parser("clear", help="remove all API keys")

    proxy_parser = subparsers.add_parser("proxy", help="manage search proxies for IP rotation")
    proxy_sub = proxy_parser.add_subparsers(dest="proxy_action", help="proxy sub-command")
    proxy_add = proxy_sub.add_parser("add", help="add a proxy (format: http://ip:port or socks5://user:pass@ip:port)")
    proxy_add.add_argument("proxies", nargs="+", help="one or more proxy URLs to add")
    proxy_list = proxy_sub.add_parser("list", help="list configured proxies (redacted)")
    proxy_remove = proxy_sub.add_parser("remove", help="remove a proxy by index")
    proxy_remove.add_argument("index", type=int, help="proxy index (0-based, see 'sieve proxy list')")
    proxy_clear = proxy_sub.add_parser("clear", help="remove all proxies")

    youtube_parser = subparsers.add_parser("youtube", help="search, inspect, or download YouTube videos via yt-dlp")
    youtube_sub = youtube_parser.add_subparsers(dest="youtube_action")
    yt_search = youtube_sub.add_parser("search", help="search YouTube and emit normalized JSON")
    yt_search.add_argument("query")
    yt_search.add_argument("--max-results", type=int, default=6)
    yt_search.add_argument("--timeout", type=int, default=45)
    yt_meta = youtube_sub.add_parser("metadata", help="inspect one YouTube URL")
    yt_meta.add_argument("url")
    yt_meta.add_argument("--timeout", type=int, default=45)
    yt_download = youtube_sub.add_parser("download", help="download one YouTube video")
    yt_download.add_argument("url")
    yt_download.add_argument("output_dir")
    yt_download.add_argument("--format", dest="format_name")
    yt_download.add_argument("--timeout", type=int, default=120)

    social_parser = subparsers.add_parser("social", help="fetch public Instagram/TikTok metadata")
    social_sub = social_parser.add_subparsers(dest="social_action")
    social_fetch = social_sub.add_parser("fetch", help="emit one normalized JSON record")
    social_fetch.add_argument("url")
    social_fetch.add_argument("--reader", choices=["sieve", "jina"], default="sieve")
    social_fetch.add_argument("--timeout", type=int, default=30)
    social_fetch.add_argument("--max-media", type=int, default=20)

    search_parser = subparsers.add_parser("search", help="keyless web search, JSON on stdout")
    search_parser.add_argument("query")
    search_parser.add_argument("--max-results", type=int, default=6)
    search_parser.add_argument("--timeout", type=int, default=45)
    search_parser.add_argument("--cache-ttl", type=int, default=3600, help=argparse.SUPPRESS)
    search_parser.add_argument("--cached", action="store_true", help="use a fresh cached result only")
    search_parser.add_argument("--refresh", action="store_true", help="bypass cache and refresh the stored result")
    search_parser.add_argument("--stale-fallback", action="store_true", help="allow age-bounded keyless cache fallback after a transient live failure")
    search_parser.add_argument("--stale-max-age", type=int, default=3600, help="maximum cached result age in seconds (1-86400)")

    fetch_parser = subparsers.add_parser("fetch", help="fetch URL(s) with auto anti-bot escalation, JSON on stdout")
    fetch_parser.add_argument("url", nargs="+")
    fetch_parser.add_argument("--timeout", type=int, default=30)
    fetch_parser.add_argument("--cache-ttl", type=int, default=3600, help=argparse.SUPPRESS)
    fetch_parser.add_argument("--extract", choices=["markdown","html","text","article","structured"], default=None, help=argparse.SUPPRESS)
    fetch_parser.add_argument("--reader", choices=["sieve", "browser"], default="sieve", help="reader tier: HTTP/sieve or browser")
    fetch_parser.add_argument("--browser-backend", choices=["auto", "pool", "sleeper"], default=None, help="browser tier: pool headless or sleeper real-browser")

    crawl_parser = subparsers.add_parser("crawl", help="deep-crawl a site, JSON on stdout")
    crawl_parser.add_argument("url")
    crawl_parser.add_argument("--max-pages", type=int, default=10)
    crawl_parser.add_argument("--max-depth", type=int, default=2)
    crawl_parser.add_argument("--focus")
    crawl_parser.add_argument("--discover-only", action="store_true")
    crawl_parser.add_argument("--discover-domains", action="store_true", help="multi-source domain discovery (sitemap+robots+feed+homepage)")
    crawl_parser.add_argument("--discovery-sources", default="sitemap,robots,feed,homepage", help="comma-separated sources for --discover-domains (sitemap,robots,feed,homepage,wayback,crt,cc,probe)")
    crawl_parser.add_argument("--max-urls", type=int, default=500, help="max URLs for --discover-domains")
    crawl_parser.add_argument("--sitemap-only", action="store_true", default=False)
    crawl_parser.add_argument("--include-subdomains", action="store_true", default=False)
    crawl_parser.add_argument("--allow-external", action="store_true", default=False)
    crawl_parser.add_argument("--map-search", type=str, default=None)
    crawl_parser.add_argument("--timeout", type=int, default=30)
    crawl_parser.add_argument("--cache-ttl", type=int, default=3600, help=argparse.SUPPRESS)
    crawl_parser.add_argument("--reader", choices=["sieve", "browser"], default="sieve", help="reader tier: HTTP/sieve or browser")
    crawl_parser.add_argument("--browser-backend", choices=["auto", "pool", "sleeper"], default=None, help="browser tier: pool headless or sleeper real-browser")

    extract_parser = subparsers.add_parser("extract", help="fetch + declarative schema extract, JSON on stdout")
    extract_parser.add_argument("url")
    extract_parser.add_argument("--schema", required=False, default=None, help="JSON schema string or @file (required unless --similar/--tables/--chunk)")
    extract_parser.add_argument("--tables", action="store_true", help="extract data tables as {headers, rows, caption}")
    extract_parser.add_argument("--chunk", choices=["identity", "regex", "sentence", "semantic"], default=None, help="split page text (sentence/semantic need .[nlp] extra)")
    extract_parser.add_argument("--similar", default=None, help="CSS selector for find-similar list expansion (Scrapling-inspired)")
    extract_parser.add_argument("--smart", default=None, help="BYOK LLM prompt: extract page into JSON (needs `sieve keys add llm` + SIEVE_LLM_BASE_URL)")
    extract_parser.add_argument("--xpath", action="store_true")
    extract_parser.add_argument("--timeout", type=int, default=30)
    extract_parser.add_argument("--reader", choices=["sieve", "browser"], default="sieve", help="reader tier: HTTP/sieve or browser")
    extract_parser.add_argument("--browser-backend", choices=["auto", "pool", "sleeper"], default=None, help="browser tier: pool headless or sleeper real-browser")

    screenshot_parser = subparsers.add_parser("screenshot", help="screenshot a URL, JSON on stdout")
    screenshot_parser.add_argument("url")
    screenshot_parser.add_argument("--full-page", action="store_true")
    screenshot_parser.add_argument("--timeout", type=int, default=30)
    screenshot_parser.add_argument("--output", default=None)
    screenshot_parser.add_argument("--browser-backend", choices=["auto", "pool", "sleeper"], default=None)

    cache_parser = subparsers.add_parser("cache", help="cache management")
    cache_sub = cache_parser.add_subparsers(dest="cache_action")
    cache_sub.add_parser("clear", help="clear expired cache")
    cache_clear_all = cache_sub.add_parser("clear-all", help="wipe all cache")
    # hidden alias: sieve cache purge
    cache_parser.add_argument("--clear-all", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()

    # Sweep a stale launcher left by a previous `sieve -u` (Windows only).
    updater.cleanup_old_launcher()

    if args.update:
        updater.do_update()
        return
    if args.reinstall:
        updater.reinstall()
        return
    if args.keys_cmd == "keys":
        from sieve.byok_config import (
            BYOK_PROVIDERS, add_key, remove_key, clear_all_keys,
            list_keys, redact_key,
        )
        action = args.keys_action
        if action == "add":
            if getattr(args, "key", None) is not None:
                print(f"  {ui.red('ERROR')} Positional secret arguments are not allowed: "
                      "the key would appear in shell history and process lists.")
                print(ui.dim("  Run `sieve keys add <provider>` to be prompted, or pipe it with --stdin."))
                return 1
            try:
                key = _read_keys_add_secret(args)
                add_key(args.provider, key)
                print(f"  {ui.ok('OK')} Added key for {args.provider} -> {redact_key(key)}")
                print("  Config: ~/.sieve/search_keys.json")
            except (ValueError, OSError) as e:
                print(f"  {ui.red('ERROR')} {e}")
                return 1
            return
        if action == "list":
            keys = list_keys()
            if not keys:
                print(ui.dim("  No search API keys configured."))
                print(ui.dim("  Add with: sieve keys add <provider> (secret prompted or piped; see --stdin)"))
                print(ui.dim(f"  Providers: {', '.join(BYOK_PROVIDERS)}"))
                return
            print(ui.branded(ui.cyan("BYOK Search Keys"), ui.dim("~/.sieve/search_keys.json")))
            for provider in BYOK_PROVIDERS:
                pkeys = keys.get(provider, [])
                if pkeys:
                    print(f"  {ui.cyan(provider)}: {len(pkeys)} key(s)")
                    for i, k in enumerate(pkeys):
                        print(f"    [{i}] {redact_key(k)}")
                else:
                    print(f"  {ui.dim(provider)}: not configured")
            return
        if action == "test":
            _test_byok_keys(args.provider)
            return
        if action == "remove":
            try:
                removed = remove_key(args.provider, args.index)
                if removed:
                    idx_str = f"[{args.index}]" if args.index is not None else "(all)"
                    print(f"  {ui.ok('OK')} Removed {removed} key(s) {idx_str} from {args.provider}")
                else:
                    print(f"  {ui.warn('INFO')} No keys configured for {args.provider}")
            except (ValueError, IndexError, OSError) as e:
                print(f"  {ui.red('ERROR')} {e}")
                return 1
            return
        if action == "clear":
            removed = clear_all_keys()
            if removed:
                print(f"  {ui.ok('OK')} Removed {removed} key(s) from all providers")
            else:
                print(ui.dim("  No keys to remove."))
            return
        # No sub-action: show help.
        keys_parser.print_help()
        return
    if getattr(args, 'proxy_action', None):
        from sieve.search_proxy import (
            add_proxy, remove_proxy, clear_proxies,
            redact_proxy, load_proxies, MAX_PROXIES,
        )
        action = args.proxy_action
        if action == "add":
            added = 0
            for proxy_url in args.proxies:
                try:
                    count = add_proxy(proxy_url)
                    added += 1
                    print(f"  {ui.ok('OK')} Added {redact_proxy(proxy_url)} ({count}/{MAX_PROXIES})")
                except ValueError as e:
                    print(f"  {ui.red('ERROR')} {e}")
            if added:
                print("  Config: ~/.sieve/search_proxies.json")
                print(f"  Rotation: each search uses the next proxy, cycling through all {count}.")
            return
        if action == "list":
            # Show config file proxies + env var proxies (merged).
            all_proxies = load_proxies()
            if not all_proxies:
                print(ui.dim("  No search proxies configured."))
                print(ui.dim("  Add with: sieve proxy add http://ip:port"))
                print(ui.dim("  Format: http://ip:port, https://ip:port, socks5://ip:port"))
                print(ui.dim("  With auth: http://user:pass@ip:port"))
                return
            print(ui.branded(ui.cyan("Search Proxy Pool"), ui.dim("~/.sieve/search_proxies.json")))
            for i, p in enumerate(all_proxies):
                print(f"  [{i}] {redact_proxy(p)}")
            print("")
            print(ui.dim(f"  Rotation: each search call uses the next proxy, cycling through all {len(all_proxies)}."))
            print(ui.dim(f"  Pool size: {len(all_proxies)}/{MAX_PROXIES}"))
            return
        if action == "remove":
            try:
                removed = remove_proxy(args.index)
                print(f"  {ui.ok('OK')} Removed [{args.index}] {redact_proxy(removed)}")
            except (IndexError, ValueError) as e:
                print(f"  {ui.red('ERROR')} {e}")
                return 1
            return
        if action == "clear":
            removed = clear_proxies()
            if removed:
                print(f"  {ui.ok('OK')} Removed {removed} proxy(s)")
            else:
                print(ui.dim("  No proxies to remove."))
            return
        proxy_parser.print_help()
        return
    if getattr(args, "youtube_action", None):
        from sieve import youtube
        try:
            if args.youtube_action == "search":
                result = youtube.search(args.query, args.max_results, args.timeout)
            elif args.youtube_action == "metadata":
                result = youtube.metadata(args.url, args.timeout)
            elif args.youtube_action == "download":
                result = youtube.download(args.url, args.output_dir, args.timeout, args.format_name)
            else:
                youtube_parser.print_help()
                return
            print(safe_public_json(result, ensure_ascii=False))
            return
        except (ValueError, youtube.YouTubeFetchError, RuntimeError) as exc:
            print(safe_public_json({"ok": False, **safe_error(exc)}), file=sys.stderr)
            return 1
    if getattr(args, "social_action", None):
        from sieve.social import fetch
        try:
            print(safe_public_json(fetch(args.url, reader=args.reader, timeout=args.timeout, max_media=args.max_media), ensure_ascii=False))
            return
        except Exception as exc:
            # Rationale: the search CLI emits a safe diagnostic and exits nonzero.
            print(safe_public_json({"ok": False, **safe_error(exc)}), file=sys.stderr)
            return 1
    if args.keys_cmd == "search":
        import asyncio
        try:
            srv = MasterFetchServer(cache_ttl=args.cache_ttl)
            result = asyncio.run(srv.smart_search(args.query, max_results=args.max_results,
                                                   cache_ttl=args.cache_ttl,
                                                   cached=args.cached, refresh=args.refresh,
                                                   stale_fallback=args.stale_fallback, stale_max_age=args.stale_max_age))
            print(safe_public_json(result, ensure_ascii=False))
            return 1 if result.error else 0
        except Exception as exc:
            # Rationale: the search CLI emits a safe diagnostic and exits nonzero.
            print(safe_public_json({"ok": False, **safe_error(exc)}), file=sys.stderr)
            return 1
    if args.keys_cmd in ("crawl", "mcp-smart-crawl", "mcp-crawl", "smart-crawl"):
        import asyncio as _aio2
        try:
            if getattr(args, "discover_domains", False):
                from sieve.domain_discovery import discover_domains_sync
                srcs = [s.strip() for s in (getattr(args, "discovery_sources", "") or "").split(",") if s.strip()]
                res = discover_domains_sync(args.url, sources=srcs, max_urls=getattr(args, "max_urls", 500),
                                            sitemap_only=getattr(args, "sitemap_only", False),
                                            include_subdomains=getattr(args, "include_subdomains", False),
                                            allow_external=getattr(args, "allow_external", False),
                                            map_search=getattr(args, "map_search", None))
                print(safe_public_json({"ok": True, "domain": res.domain, "urls": res.urls, "count": len(res.urls), "sources_used": res.sources_used, "sources_failed": res.sources_failed, "via": res.via}, ensure_ascii=False))
                return
            srv = MasterFetchServer(cache_ttl=getattr(args, "cache_ttl", 3600) or 3600)
            kw = {}
            if getattr(args, "max_pages", None) is not None: kw["max_pages"] = args.max_pages
            if getattr(args, "max_depth", None) is not None: kw["max_depth"] = args.max_depth
            if getattr(args, "focus", None): kw["focus"] = args.focus
            if getattr(args, "discover_only", False): kw["discover_only"] = True
            backend = getattr(args, "browser_backend", None)
            if backend == "auto":
                from sieve.social import resolve_browser_backend
                backend = resolve_browser_backend("auto")
            if backend == "sleeper":
                kw["force_fetcher"] = "sleeper"
            elif backend == "pool" or getattr(args, "reader", "sieve") == "browser":
                kw["force_fetcher"] = "stealthy"
            result = _aio2.run(srv.smart_crawl(args.url, **kw))
            print(safe_public_json(result, ensure_ascii=False))
            failed = bool(getattr(result, "error", "")) or any(
                bool(getattr(page, "error", "")) and getattr(page, "fetcher_used", "") == "sleeper"
                for page in getattr(result, "pages", [])
            )
            return 1 if failed else 0
        except Exception as exc:
            print(safe_public_json({"ok": False, **safe_error(exc)}))
            return 1
    if args.keys_cmd in ("extract", "mcp-extract", "mcp_extract"):
        import asyncio as _aio3
        import json as _json3
        import pathlib as _pl3
        try:
            if getattr(args, "similar", None):
                srv = MasterFetchServer(cache_ttl=3600)
                # fetch html then find similar
                fetched = _aio3.run(srv.smart_fetch(url=args.url, extraction_type="html", cache_ttl=0))
                html_src = "\n".join(fetched.model_dump().get("content") or [])
                if not html_src:
                    # fallback: raw fetch
                    html_src = ""
                from sieve.similar import find_similar
                items = find_similar(html_src, args.similar)
                print(safe_public_json({"ok": True, "url": args.url, "selector": args.similar, "count": len(items), "items": items}, ensure_ascii=False))
                return
            if not args.schema and not getattr(args, "tables", False) and not getattr(args, "chunk", None) and not getattr(args, "smart", None):
                print(safe_public_json({"ok": False, "error": "--schema required (or use --similar SELECTOR, --tables, --chunk, --smart PROMPT)"}), file=sys.stderr)
                return 1
            schema = None
            if args.schema:
                raw = args.schema
                if raw.startswith("@"):
                    raw = _pl3.Path(raw[1:]).read_text()
                try:
                    from sieve.extraction import parse_relaxed_json as _prj, normalize_schema as _nsc
                    schema = _nsc(_prj(raw))
                except Exception:
                    schema = _json3.loads(raw)
            srv = MasterFetchServer(cache_ttl=3600)
            fetch_kw = {}
            backend = getattr(args, "browser_backend", None)
            if backend == "auto":
                from sieve.social import resolve_browser_backend
                backend = resolve_browser_backend("auto")
            if backend == "sleeper":
                fetch_kw["force_fetcher"] = "sleeper"
            elif backend == "pool" or getattr(args, "reader", "sieve") == "browser":
                fetch_kw["force_fetcher"] = "stealthy"
            result = _aio3.run(srv.extract(url=args.url, schema=schema, xpath=bool(getattr(args, "xpath", False)),
                                           tables=bool(getattr(args, "tables", False)),
                                           chunk=getattr(args, "chunk", None),
                                           smart=getattr(args, "smart", None), **fetch_kw))
            print(safe_public_json(result, ensure_ascii=False))
            failed = isinstance(result, dict) and (
                bool(result.get("error")) or
                (result.get("status") == 0 and result.get("content_ok") is False)
            )
            return 1 if failed else 0
        except Exception as exc:
            print(safe_public_json({"ok": False, **safe_error(exc)}))
            return 1
    if args.keys_cmd in ("screenshot", "mcp-screenshot", "mcp_screenshot"):
        import asyncio as _aio4
        try:
            from sieve.social import resolve_browser_backend
            if resolve_browser_backend(getattr(args, "browser_backend", None)) == "sleeper":
                from sieve.sleeper_bridge import sleeper_shot
                print(safe_public_json(sleeper_shot(args.url, out_path=args.output,
                                                timeout=args.timeout), ensure_ascii=False))
                return
            srv = MasterFetchServer(cache_ttl=3600)
            result = _aio4.run(srv.screenshot(url=args.url))
            print(safe_public_json(result, ensure_ascii=False))
            return
        except Exception as exc:
            # Rationale: the search CLI emits a safe diagnostic and exits nonzero.
            print(safe_public_json({"ok": False, **safe_error(exc)}), file=sys.stderr)
            return 1
    if args.keys_cmd == "cache":
        import asyncio as _aio5
        try:
            srv = MasterFetchServer(cache_ttl=3600)
            all_flag = bool(getattr(args, "cache_action", None) == "clear-all" or getattr(args, "clear_all", False))
            result = _aio5.run(srv.cache_clear(all=all_flag))
            print(safe_public_json(result, ensure_ascii=False))
            return
        except Exception as exc:
            # Rationale: the search CLI emits a safe diagnostic and exits nonzero.
            print(safe_public_json({"ok": False, **safe_error(exc)}), file=sys.stderr)
            return 1
    if args.keys_cmd == "fetch":
        import asyncio
        try:
            srv = MasterFetchServer(cache_ttl=args.cache_ttl)
            urls = args.url
            backend = getattr(args, "browser_backend", None)
            if backend == "auto":
                from sieve.social import resolve_browser_backend
                backend = resolve_browser_backend("auto")
            force = "sleeper" if backend == "sleeper" else ("stealthy" if backend == "pool" or getattr(args, "reader", "sieve") == "browser" else None)
            fetch_kw = {"timeout": args.timeout * 1000}
            if force:
                fetch_kw["force_fetcher"] = force
            if len(urls) == 1:
                result = asyncio.run(srv.smart_fetch(urls[0], **fetch_kw))
            else:
                result = asyncio.run(srv.smart_fetch(urls[0], urls=urls, **fetch_kw))
            dump = safe_public_json(result, ensure_ascii=False)
            print(dump)
            return 1 if getattr(result, "error", "") else 0
        except Exception as exc:
            # Rationale: the search CLI emits a safe diagnostic and exits nonzero.
            print(safe_public_json({"ok": False, **safe_error(exc)}), file=sys.stderr)
            return 1
    if args.rollback:
        updater.rollback()
        return
    if args.doctor:
        updater.doctor()
        return
    if args.version:
        updater.print_version()
        return

    srv = MasterFetchServer(cache_ttl=args.cache_ttl)
    srv.serve(http=False)


if __name__ == "__main__":
    main()
