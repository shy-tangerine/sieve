"""Anti-bot block-page detection for Sieve's tier-up retry loop.

Reconstructed clean-room against Sieve's own call sites (sieve/server.py
``extract``, the ``mcp_detect_block`` MCP tool) and the retry-loop contract;
no upstream-derived code.

:func:`detect_block` accepts a fetch result's status, headers, and decoded
body, and returns ``{"blocked": bool, "reason": str|None, "kind": str|None}``.
The caller escalates to the next fetch tier when ``blocked`` is True.

Detection is layered, from cheapest to most expensive:

1. **HTTP 429** is always rate limiting, whatever the body says.
2. **High-confidence markers** — vendor-specific artifacts (challenge forms,
   CDN paths, incident IDs) that only appear on real block pages — trigger
   on any response, any size.
3. **Medium-confidence markers** — generic "Access Denied"-style text and
   captcha embeds — only fire alongside an error status, so a legitimate
   article that mentions them is not misflagged. 403/503 with HTML content
   is treated as blocked: the retry tiers rescue false positives, so the
   detector errs toward detection.
4. **Structural integrity** — silent blocks ship pages that pass every
   pattern but carry almost no visible content: near-empty 200s, bodies with
   no ``<body>`` tag, script-heavy shells, pages without real content
   elements.

Data-looking bodies (JSON, a lone ``<pre>``-wrapped blob) are exempt from
structural and medium-tier checks: short JSON is a valid response, not a
block page. Vendor kinds recognized: cloudflare, akamai, datadome,
perimeterx, imperva, sucuri, kasada; anything else reports ``other``.
"""

from __future__ import annotations

import re
from typing import Any, Dict, Optional

__all__ = ["detect_block"]

Kind = str
Pattern = re.Pattern[str]

# ── high-confidence vendor markers (single hit is sufficient) ───────────────

_TIER1_PATTERNS: list[tuple[Pattern, Kind, str]] = [
    # Akamai: reference IDs and interruption notice.
    (re.compile(r"Reference\s*#\s*[\d]+\.[0-9a-f]+\.\d+\.[0-9a-f]+", re.IGNORECASE),
     "akamai", "Akamai block (Reference #)"),
    (re.compile(r"Pardon\s+Our\s+Interruption", re.IGNORECASE),
     "akamai", "Akamai challenge (Pardon Our Interruption)"),
    # Cloudflare: challenge form token, error-code span, orchestrate path,
    # attention banner.
    (re.compile(r"challenge-form.*?__cf_chl_f_tk=", re.IGNORECASE | re.DOTALL),
     "cloudflare", "Cloudflare challenge form"),
    (re.compile(r'<span\s+class="cf-error-code">\d{4}</span>', re.IGNORECASE),
     "cloudflare", "Cloudflare firewall block"),
    (re.compile(r"/cdn-cgi/challenge-platform/\S+orchestrate", re.IGNORECASE),
     "cloudflare", "Cloudflare JS challenge"),
    (re.compile(r"Attention\s+Required!?\s*[|<]?\s*cloudflare", re.IGNORECASE | re.DOTALL),
     "cloudflare", "Cloudflare block page (Attention Required)"),
    # PerimeterX: app-id bootstrap and captcha host.
    (re.compile(r"window\._pxAppId\s*=", re.IGNORECASE),
     "perimeterx", "PerimeterX block"),
    (re.compile(r"captcha\.px-cdn\.net", re.IGNORECASE),
     "perimeterx", "PerimeterX captcha"),
    # DataDome: dedicated captcha host.
    (re.compile(r"captcha-delivery\.com", re.IGNORECASE),
     "datadome", "DataDome captcha"),
    # Imperva/Incapsula: resource marker and incident IDs.
    (re.compile(r"_Incapsula_Resource", re.IGNORECASE),
     "imperva", "Imperva/Incapsula block"),
    (re.compile(r"Incapsula\s+incident\s+ID", re.IGNORECASE),
     "imperva", "Imperva/Incapsula incident"),
    # Sucuri: firewall block page.
    (re.compile(r"Sucuri\s+WebSite\s+Firewall", re.IGNORECASE),
     "sucuri", "Sucuri firewall block"),
    # Kasada: script-start timestamp idiom.
    (re.compile(r"KPSDK\.scriptStart\s*=\s*KPSDK\.now\(\)", re.IGNORECASE),
     "kasada", "Kasada challenge"),
    # Generic network-security interstitials.
    (re.compile(r"blocked\s+by\s+network\s+security", re.IGNORECASE),
     "other", "Network security block"),
]

# ── medium-confidence markers (error status or short page only) ─────────────

_TIER2_PATTERNS: list[tuple[Pattern, Kind, str]] = [
    (re.compile(r"Access\s+Denied", re.IGNORECASE),
     "other", "Access Denied on short page"),
    (re.compile(r"Checking\s+your\s+browser", re.IGNORECASE),
     "cloudflare", "Cloudflare browser check"),
    (re.compile(r"<title>\s*Just\s+a\s+moment", re.IGNORECASE),
     "cloudflare", "Cloudflare interstitial"),
    (re.compile(r"class=[\"']g-recaptcha[\"']", re.IGNORECASE),
     "other", "reCAPTCHA on block page"),
    (re.compile(r"class=[\"']h-captcha[\"']", re.IGNORECASE),
     "other", "hCaptcha on block page"),
    (re.compile(r"Access\s+to\s+This\s+Page\s+Has\s+Been\s+Blocked", re.IGNORECASE),
     "perimeterx", "PerimeterX block page"),
    (re.compile(r"blocked\s+by\s+security", re.IGNORECASE),
     "other", "Blocked by security"),
    (re.compile(r"Request\s+unsuccessful", re.IGNORECASE),
     "imperva", "Request unsuccessful (Imperva)"),
]

# Sizes/budgets: medium-tier markers only on short pages; structural checks
# only on modest pages; scan windows bounded regardless of input size.
_TIER2_MAX_SIZE = 10_000
_STRUCTURAL_MAX_SIZE = 50_000
_SMALL_PAGE_SIZE = 5_000
_EMPTY_CONTENT_THRESHOLD = 100
_INSPECTION_WINDOW = 500_000
_SNIPPET_WINDOW = 15_000
_DEEP_WINDOW = 30_000

_CONTENT_ELEMENTS_RE = re.compile(
    r"<(?:p|h[1-6]|article|section|li|td|a|pre)\b", re.IGNORECASE
)
_SCRIPT_TAG_RE = re.compile(r"<script\b", re.IGNORECASE)
_STYLE_TAG_RE = re.compile(r"<style\b[\s\S]*?</style>", re.IGNORECASE)
_SCRIPT_BLOCK_RE = re.compile(r"<script\b[\s\S]*?</script>", re.IGNORECASE)
_TAG_RE = re.compile(r"<[^>]+>")
_BODY_RE = re.compile(r"<body\b", re.IGNORECASE)


def _looks_like_data(html: str) -> bool:
    """True when the body is data (JSON) or an HTML wrapper around data:
    those are legitimate short responses, not block pages."""
    stripped = html.strip()
    if not stripped:
        return False
    if stripped[0] in ("{", "["):
        return True
    if stripped[:10].lower().startswith(("<html", "<!")):
        # <body><pre>{...}</pre> is a common JSON-over-HTML shape.
        return bool(
            re.search(r"<body[^>]*>\s*<pre[^>]*>\s*[{\\[]", stripped[:500], re.IGNORECASE)
        )
    return stripped[0] == "<"


def _strip_invisible(html: str) -> str:
    """Remove script/style blocks so their text never counts as visible."""
    stripped = _SCRIPT_BLOCK_RE.sub("", html)
    return _STYLE_TAG_RE.sub("", stripped)


def _structural_check(html: str) -> tuple[bool, str]:
    """Detect silent blocks: pages that pass every pattern but carry almost
    no visible content. Returns (blocked, reason)."""
    html_len = len(html)
    if html_len > _STRUCTURAL_MAX_SIZE or _looks_like_data(html):
        return False, ""

    signals: list[str] = []

    if not _BODY_RE.search(html):
        return True, f"Structural: no <body> tag ({html_len} bytes)"

    body_match = re.search(r"<body\b[^>]*>([\s\S]*)</body>", html, re.IGNORECASE)
    body_content = body_match.group(1) if body_match else html
    visible_text = _TAG_RE.sub("", _strip_invisible(body_content)).strip()
    visible_len = len(visible_text)
    if visible_len < 50:
        signals.append("minimal_text")

    content_elements = len(_CONTENT_ELEMENTS_RE.findall(html))
    if content_elements == 0:
        signals.append("no_content_elements")

    script_count = len(_SCRIPT_TAG_RE.findall(html))
    if script_count > 0 and content_elements == 0 and visible_len < 100:
        signals.append("script_heavy_shell")

    if len(signals) >= 2:
        return True, (
            f"Structural: {', '.join(signals)} "
            f"({html_len} bytes, {visible_len} chars visible)"
        )
    if len(signals) == 1 and html_len < _SMALL_PAGE_SIZE:
        return True, (
            f"Structural: {signals[0]} on small page "
            f"({html_len} bytes, {visible_len} chars visible)"
        )
    return False, ""


def _match_patterns(
    text: str, patterns: list[tuple[Pattern, Kind, str]]
) -> Optional[tuple[Kind, str]]:
    """First matching pattern as (kind, reason), or None."""
    for pattern, kind, reason in patterns:
        if pattern.search(text):
            return kind, reason
    return None


def _kind_from_reason(reason: str) -> Kind:
    lowered = reason.lower()
    for vendor in ("cloudflare", "akamai", "datadome", "perimeterx",
                   "imperva", "sucuri", "kasada"):
        if vendor in lowered:
            return vendor
    if "incapsula" in lowered:
        return "imperva"
    return "other"


def detect_block(
    status: Optional[int],
    headers: Optional[Dict[str, Any]],
    html: str = "",
) -> Dict[str, Any]:
    """Decide whether a fetch result is an anti-bot block page.

    Args:
        status: HTTP status code (0/None = network error).
        headers: Response headers (reserved for future signal use; the
            current detection tiers are status- and body-driven).
        html: Decoded response body text.

    Returns:
        ``{"blocked": bool, "reason": str|None, "kind": str|None}``.
    """
    status = status or 0
    html = html or ""
    html_len = len(html)
    inspection = html[:_INSPECTION_WINDOW]
    snippet = inspection[:_SNIPPET_WINDOW]

    # 1. Rate limiting is unambiguous.
    if status == 429:
        return {"blocked": True, "reason": "HTTP 429 Too Many Requests", "kind": "other"}

    # 2. High-confidence markers anywhere in the near window...
    tier1 = _match_patterns(snippet, _TIER1_PATTERNS)
    if tier1:
        return {"blocked": True, "reason": tier1[1], "kind": tier1[0]}

    # ...and in the de-scripted body of large pages (block markers can sit
    # below megabytes of inline scripts).
    if html_len > _SNIPPET_WINDOW:
        deep_snippet = _strip_invisible(inspection)[:_DEEP_WINDOW]
        deep = _match_patterns(deep_snippet, _TIER1_PATTERNS)
        if deep:
            return {"blocked": True, "reason": deep[1], "kind": deep[0]}

    # 3a. 403/503 with HTML content: treat as blocked (retry tiers rescue
    # false positives), with medium-tier markers refining the reason.
    if status in (403, 503) and not _looks_like_data(html):
        if html_len < _EMPTY_CONTENT_THRESHOLD:
            return {
                "blocked": True,
                "reason": f"HTTP {status} with near-empty response ({html_len} bytes)",
                "kind": "other",
            }
        check_snippet = snippet
        if html_len > _TIER2_MAX_SIZE:
            check_snippet = _strip_invisible(html[:_INSPECTION_WINDOW])[:_DEEP_WINDOW]
        tier2 = _match_patterns(check_snippet, _TIER2_PATTERNS)
        if tier2:
            return {
                "blocked": True,
                "reason": f"{tier2[1]} (HTTP {status}, {html_len} bytes)",
                "kind": tier2[0],
            }
        return {
            "blocked": True,
            "reason": f"HTTP {status} with HTML content ({html_len} bytes)",
            "kind": "other",
        }

    # 3b. Other error statuses: medium-tier markers only on short pages.
    if status and status >= 400 and html_len < _TIER2_MAX_SIZE:
        tier2 = _match_patterns(snippet, _TIER2_PATTERNS)
        if tier2:
            return {
                "blocked": True,
                "reason": f"{tier2[1]} (HTTP {status}, {html_len} bytes)",
                "kind": tier2[0],
            }

    # 3c. HTTP 200 with near-empty non-data content: a silent block.
    if status == 200:
        stripped = html.strip()
        if len(stripped) < _EMPTY_CONTENT_THRESHOLD and not _looks_like_data(html):
            return {
                "blocked": True,
                "reason": f"Near-empty content ({len(stripped)} bytes) with HTTP 200",
                "kind": "other",
            }

    # 4. Structural integrity: shells and incomplete renders.
    structural_blocked, structural_reason = _structural_check(html)
    if structural_blocked:
        return {
            "blocked": True,
            "reason": structural_reason,
            "kind": _kind_from_reason(structural_reason),
        }

    return {"blocked": False, "reason": None, "kind": None}
