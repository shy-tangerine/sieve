"""XHR/fetch response capture for AJAX-shell pages.

Some pages return a complete-looking HTML document whose data panels are
empty: the numbers arrive later over XHR and are injected into placeholder
containers. Text extraction on such a page yields boilerplate prose and no
data, while `content_ok` still reads True — the agent gets a confident empty
answer (meteologix.com is the reference case: 267KB of HTML, 5.6KB of help
text, zero temperatures; the forecast arrives via /ajax_pub/weather*).

The browser tier already loads those XHRs. This module decides which of them
are worth keeping, turns each body into text, and ranks them so the caller can
point at the one fragment that actually carries the page's data.

Capture is bounded on every axis (count, per-fragment bytes, total bytes,
text chars) so a chatty page can never blow up a response.
"""

from __future__ import annotations

import json
import math
import re
from typing import Any, Dict, List, Optional
from html.parser import HTMLParser
from urllib.parse import urlparse

# ─── Tracker/ad blocklist — public-domain suffix matcher ───────────────
# Minimal blocklist: high-signal tracker/ad domains (public knowledge, no
# third-party list copied). Used at request layer to drop analytics noise.
_BLOCKED_SUFFIXES = frozenset({
    "doubleclick.net", "googletagmanager.com", "google-analytics.com",
    "googlesyndication.com", "facebook.net", "facebook.com",
    "hotjar.com", "mixpanel.com", "segment.io", "segment.com",
    "amplitude.com", "sentry.io", "bugsnag.com", "newrelic.com",
    "clarity.ms", "scorecardresearch.com", "quantserve.com",
    "criteo.net", "criteo.com", "taboola.com", "outbrain.com",
    "adsrvr.org", "adnxs.com", "pubmatic.com", "rubiconproject.com",
})


def is_tracker_url(url: str) -> bool:
    """True if URL's host matches the tracker/ad blocklist (suffix match)."""
    try:
        host = (urlparse(url).hostname or "").lower().strip(".")
    except Exception:
        return False
    if not host:
        return False
    return host in _BLOCKED_SUFFIXES or any(host.endswith("." + s) for s in _BLOCKED_SUFFIXES)

# ─── Bounds ──────────────────────────────────────────────────────────────────

MAX_FRAGMENTS = 12          # fragments kept per page (ranked, best first)
MAX_FRAGMENT_BYTES = 512_000  # skip bodies larger than this
MAX_TOTAL_BYTES = 2_000_000   # stop capturing once this much is buffered
MAX_TEXT_CHARS = 20_000     # per-fragment extracted text cap
MAX_SECONDARY_TEXT_CHARS = 2_000  # non-primary fragments are summarized harder

# ─── What is worth capturing ─────────────────────────────────────────────────

CAPTURED_RESOURCE_TYPES = frozenset({"xhr", "fetch"})

# Content types whose bodies can carry data. Anything else (js, css, images,
# fonts, video) is noise for our purpose even when requested via XHR.
_CAPTURED_CONTENT_TYPES = (
    "application/json",
    "text/html",
    "text/plain",
    "text/xml",
    "application/xml",
    "application/ld+json",
    "text/csv",
)

# Telemetry / ads / consent / error-reporting endpoints. These are XHR-heavy
# and never carry page data.
_NOISE_HOST_MARKERS = (
    "google-analytics", "googletagmanager", "googlesyndication", "doubleclick",
    "googleadservices", "adservice", "adsystem", "adnxs", "criteo", "taboola",
    "outbrain", "pubmatic", "rubiconproject", "openx", "smartadserver",
    "facebook.com", "facebook.net", "connect.facebook", "hotjar", "mixpanel",
    "amplitude", "segment.io", "segment.com", "sentry.io", "bugsnag",
    "newrelic", "nr-data.net", "clarity.ms", "quantserve", "scorecardresearch",
    "chartbeat", "parsely", "cloudflareinsights", "onetrust", "cookielaw",
    "sourcepoint", "usercentrics", "trustarc", "optimizely", "branch.io",
    "snowplow", "matomo", "piwik", "statcounter", "adsrvr", "casalemedia",
    "moatads", "krxd.net", "demdex", "omtrdc.net", "adobedtm", "tiqcdn",
    # Bot-detection / challenge infrastructure: chatty over XHR, never data.
    "challenges.cloudflare.com", "html-load.cc", "hcaptcha.com",
    "recaptcha.net", "google.com/recaptcha", "perimeterx", "px-cloud",
    "datadome.co", "captcha-delivery", "arkoselabs", "funcaptcha",
    "imperva", "incapsula", "akamaihd", "akstat.io",
)

# Path fragments that mark a request as telemetry regardless of host (a
# first-party /api/analytics or /log/event is still noise).
_NOISE_PATH_MARKERS = (
    "/analytics", "/telemetry", "/beacon", "/collect", "/pixel", "/track",
    "/tracking", "/metrics", "/log/", "/logs", "/event", "/events",
    "/heartbeat", "/ping", "/consent", "/gtm", "/gdpr", "/ccpa",
    "/error-report", "/csp-report", "/rum", "/stats", "/cdn-cgi/",
    # Sourcepoint-style CMP endpoints. These are served from a first-party
    # CNAME (data-xxxx.example.com), so host matching alone misses them.
    "/wrapper/v2/", "/meta-data", "/pv-data", "/privacy-manager",
)

# Consent-management payloads are large, first-party-looking JSON blobs that
# would otherwise outrank a compact data table on sheer size. Detected from the
# body, since their URLs are deliberately generic.
_CONSENT_BODY_MARKERS = (
    "gdpr", "ccpa", "consent", "vendorlist", "privacymanager", "propertyid",
    "campaigns", "iabvendor", "usnat", "cookiepolicy", "legitimateinterest",
)
_CONSENT_MIN_MARKERS = 2


def is_consent_payload(text: str) -> bool:
    """True when a fragment is a cookie/consent-manager blob rather than data."""
    if not text:
        return False
    sample = text[:4000].lower()
    hits = sum(1 for marker in _CONSENT_BODY_MARKERS if marker in sample)
    return hits >= _CONSENT_MIN_MARKERS


def is_animation_payload(text: str) -> bool:
    """True for Lottie/Bodymovin animation JSON masquerading as data."""
    if not text:
        return False
    sample = text[:2000]
    hits = sum(1 for key in _ANIMATION_KEYS if key in sample)
    return hits >= _ANIMATION_MIN_KEYS

# Asset extensions that occasionally come through as XHR.
# Asset CDNs. A page's own data is served by its application host, never from
# the bucket that holds its icons and animations (CoinGecko pulls Lottie
# animation JSON from static.coingecko.com, which is JSON but not data).
_STATIC_HOST_PREFIXES = (
    "static.", "assets.", "asset.", "cdn.", "cdn-", "img.", "images.",
    "media.", "fonts.", "icons.",
)

# Lottie/Bodymovin animation payloads: JSON, first-party-ish, pure noise.
_ANIMATION_KEYS = ('"layers"', '"assets"', '"markers"', '"ddd"', '"ip"', '"op"')
_ANIMATION_MIN_KEYS = 3

_ASSET_SUFFIXES = (
    ".js", ".mjs", ".css", ".map", ".png", ".jpg", ".jpeg", ".gif", ".webp",
    ".svg", ".ico", ".woff", ".woff2", ".ttf", ".eot", ".mp4", ".webm",
    ".mp3", ".wasm", ".avif",
)


def _host_and_path(url: str) -> tuple[str, str]:
    try:
        p = urlparse(url)
        return (p.hostname or "").lower(), (p.path or "").lower()
    except Exception:
        return "", ""


def should_capture(
    url: str,
    resource_type: str,
    content_type: str = "",
    pattern: Optional[re.Pattern] = None,
) -> bool:
    """True when an XHR response is plausibly a data fragment.

    `pattern`, when given, overrides the noise heuristics entirely: an explicit
    capture_pattern from the caller means "I know what I'm looking for".
    """
    if resource_type not in CAPTURED_RESOURCE_TYPES:
        return False

    if pattern is not None:
        return bool(pattern.search(url))

    host, path = _host_and_path(url)
    if any(marker in host for marker in _NOISE_HOST_MARKERS):
        return False
    if host.startswith(_STATIC_HOST_PREFIXES):
        return False
    if any(marker in path for marker in _NOISE_PATH_MARKERS):
        return False
    if path.endswith(_ASSET_SUFFIXES):
        return False

    # Empty content-type: keep it (some endpoints omit the header) and let the
    # body-level checks decide. Otherwise require a data-bearing type.
    if content_type:
        from sieve.content_type import media_type_of
        ct = media_type_of(content_type)
        if not any(ct.startswith(allowed) for allowed in _CAPTURED_CONTENT_TYPES):
            return False

    return True


# ─── Body -> text ────────────────────────────────────────────────────────────

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"[ \t\x0b\f\r]+")
_BLANK_RE = re.compile(r"\n{3,}")


class _FragmentTextParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.skip = 0
    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style"}: self.skip += 1
        elif not self.skip and tag == "br": self.parts.append("\n")
    def handle_endtag(self, tag):
        if tag in {"script", "style"} and self.skip: self.skip -= 1
        elif not self.skip and tag in {"div","p","li","tr","h1","h2","h3","h4","h5","h6","section","article"}: self.parts.append("\n")
        elif not self.skip and tag in {"td","th","span"}: self.parts.append(" ")
    def handle_data(self, data):
        if not self.skip: self.parts.append(data)

def _html_to_text(html: str) -> str:
    """Flatten a bounded HTML fragment to readable text."""
    parser = _FragmentTextParser()
    parser.feed(html[:MAX_FRAGMENT_BYTES])
    parser.close()
    text = "".join(parser.parts)
    text = _WS_RE.sub(" ", text)
    text = "\n".join(line.strip() for line in text.split("\n"))
    return _BLANK_RE.sub("\n\n", text).strip()


def _json_to_text(body: str) -> str:
    """Pretty-print JSON so an agent can read it; fall back to the raw body."""
    try:
        parsed = json.loads(body)
    except Exception:
        return body.strip()
    try:
        return json.dumps(parsed, indent=1, ensure_ascii=False, sort_keys=False)
    except Exception:
        return body.strip()


# Minimum readable text below which an HTML fragment is assumed to carry its
# data in inline script literals instead of in markup.
_THIN_TEXT_CHARS = 400
_MAX_SCRIPT_LITERAL = 40_000

_SCRIPT_RE = re.compile(r"<script[^>]*>(.*?)</script>", re.DOTALL | re.IGNORECASE)
# Assignments whose right-hand side is a JSON literal: `var x = {...}`,
# `window.__DATA__ = [...]`, `"series": [...]`.
_ASSIGN_RE = re.compile(r"[\w$.\[\]\"']\s*[:=]\s*(?=[{\[])")


def _balanced_literal(src: str, start: int, limit: int = _MAX_SCRIPT_LITERAL) -> str:
    """Return the JSON-ish literal beginning at `start`, or '' if unbalanced.

    Tracks string state so braces inside quoted values do not end the literal.
    """
    opening = src[start]
    closing = "}" if opening == "{" else "]"
    depth = 0
    in_str: Optional[str] = None
    escaped = False
    for i in range(start, min(len(src), start + limit)):
        ch = src[i]
        if in_str:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == in_str:
                in_str = None
            continue
        if ch in ("'", '"'):
            in_str = ch
        elif ch == opening:
            depth += 1
        elif ch == closing:
            depth -= 1
            if depth == 0:
                return src[start:i + 1]
    return ""


def script_data(html: str) -> str:
    """Pull JSON-ish literals out of inline <script> blocks.

    Pages routinely ship their data as script variables (chart series,
    __NEXT_DATA__, bootstrapped state) rather than as markup. Stripping scripts
    would throw exactly the numbers away, so recover the literals instead.
    """
    chunks: List[str] = []
    seen_total = 0
    for script in _SCRIPT_RE.findall(html):
        if seen_total >= _MAX_SCRIPT_LITERAL:
            break
        for match in _ASSIGN_RE.finditer(script):
            start = match.end()
            literal = _balanced_literal(script, start)
            # Skip trivial literals ({}, [], small option bags).
            if len(literal) < 120:
                continue
            chunks.append(literal)
            seen_total += len(literal)
            if seen_total >= _MAX_SCRIPT_LITERAL:
                break
    return "\n".join(chunks)


def analyze_fragment(body: str, content_type: str = "") -> Dict[str, Any]:
    """Turn a captured body into text plus the provenance of that text.

    `from_script` marks text recovered from inline script literals rather than
    from rendered markup. Both are data, but a rendered panel reads far better
    than a chart config, so ranking prefers it.
    """
    if not body:
        return {"text": "", "from_script": False}
    from sieve.content_type import media_type_of
    ct = media_type_of(content_type)
    stripped = body.lstrip()
    if ct.startswith(("application/json", "application/ld+json")) or (
        not ct and stripped[:1] in ("{", "[")
    ):
        return {"text": _json_to_text(body), "from_script": False}
    if "<" in body:
        text = _html_to_text(body)
        # Thin markup + inline scripts: the payload is in the script literals.
        if len(text) < _THIN_TEXT_CHARS and "<script" in body.lower():
            data = script_data(body)
            if data:
                return {
                    "text": (text + "\n\n" + data).strip() if text else data,
                    "from_script": True,
                }
        return {"text": text, "from_script": False}
    return {"text": body.strip(), "from_script": False}


def fragment_text(body: str, content_type: str = "") -> str:
    """Text of a captured body (see analyze_fragment for provenance)."""
    return analyze_fragment(body, content_type)["text"]


# ─── Ranking ─────────────────────────────────────────────────────────────────

_NUM_RE = re.compile(r"\d")
# Markup that signals a rendered data panel rather than a config blob.
_STRUCTURE_MARKERS = ("<tr", "<td", "<li", "<table", '"data"', '"results"',
                      '"items"', '"list"', '"series"', '"values"')


def score_fragment(frag: Dict[str, Any]) -> float:
    """Heuristic value of a fragment as *the* data payload of the page.

    Size is damped (sqrt) on purpose: a compact forecast strip carrying 30
    numbers must outrank a 80KB config blob. What actually marks a data payload
    is numeric density and repeated row structure, so those drive the score.
    """
    text = frag.get("text") or ""
    if not text:
        return 0.0

    score = math.sqrt(len(text))

    digits = len(_NUM_RE.findall(text))
    density = digits / len(text)
    # Data panels are number-heavy; prose, nav markup and config are not.
    if density > 0.02:
        score *= 1.0 + min(density * 8.0, 3.0)

    raw = (frag.get("_raw") or "").lower()
    structure_hits = sum(1 for m in _STRUCTURE_MARKERS if m in raw)
    if structure_hits:
        score *= 1.0 + min(structure_hits * 0.15, 0.6)

    # Many short lines = rows of a rendered panel, not a prose blob.
    lines = [ln for ln in text.split("\n") if ln.strip()]
    if len(lines) >= 5:
        score *= 1.0 + min(len(lines) / 100.0, 0.5)

    # Tiny fragments (acks, {"ok":true}) are never the payload.
    if len(text) < 200:
        score *= 0.2

    return score


def rank_fragments(fragments: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Sort fragments best-first, flag the primary one, and trim the rest.

    Only the primary keeps a full text budget; secondaries are truncated hard
    so a page with a dozen XHRs stays affordable in context.
    """
    if not fragments:
        return []

    for frag in fragments:
        frag["_score"] = score_fragment(frag)

    ranked = sorted(fragments, key=lambda f: f["_score"], reverse=True)

    # A rendered panel always beats script-recovered data for the primary slot,
    # regardless of score: both may hold the numbers, but "02:00pm 29°C" is
    # usable as-is while a chart series is epoch pairs the caller must decode.
    # Score still decides within each group, and among script fragments when no
    # rendered one carries anything substantial.
    rendered = [
        f for f in ranked
        if not f.get("from_script") and len(f.get("text") or "") >= 200
    ]
    if rendered and ranked and ranked[0] is not rendered[0]:
        ranked.remove(rendered[0])
        ranked.insert(0, rendered[0])

    ranked = ranked[:MAX_FRAGMENTS]

    for i, frag in enumerate(ranked):
        is_primary = i == 0 and frag["_score"] > 0
        frag["is_primary"] = is_primary
        limit = MAX_TEXT_CHARS if is_primary else MAX_SECONDARY_TEXT_CHARS
        text = frag.get("text") or ""
        if len(text) > limit:
            frag["text"] = text[:limit]
            frag["text_truncated"] = True
        else:
            frag["text_truncated"] = False
        frag.pop("_score", None)
        frag.pop("_raw", None)

    return ranked


def build_network_field(fragments: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Assemble the `network` envelope returned to the agent."""
    # Drop consent and animation blobs before ranking: they are large,
    # first-party-looking and would otherwise crowd out the real payload and
    # eat the fragment budget.
    fragments = [
        f for f in fragments
        if f.get("text", "").strip()
        and not is_consent_payload(f.get("text", ""))
        and not is_animation_payload(f.get("text", ""))
    ]
    ranked = rank_fragments(fragments)
    if not ranked:
        return {}
    primary = next((f for f in ranked if f.get("is_primary")), None)
    return {
        "fragments": ranked,
        "primary_url": primary.get("url", "") if primary else "",
        "captured_count": len(ranked),
    }


# ─── AJAX-shell detection ────────────────────────────────────────────────────

# A shell is identified by *empty containers holding a loading placeholder* —
# panels the page intends to fill later. Counting attributes or measuring a
# text/bytes ratio does not work: heavy sites (GitHub) carry data-* hooks all
# over and extract poorly, which made both signals fire on pages whose content
# was in fact fully present.
AJAX_SHELL_MIN_BODY_BYTES = 20_000
AJAX_SHELL_MIN_PLACEHOLDERS = 2

# Elements that can stand in as a deferred panel.
_CONTAINER_TAGS = frozenset({
    "div", "section", "main", "article", "aside", "ul", "ol", "table",
    "tbody", "span", "p", "figure",
})

# A loading placeholder: spinner/skeleton imagery or class naming.
_PLACEHOLDER_RE = re.compile(
    r"(?:loader|loading|spinner|skeleton|placeholder)", re.IGNORECASE
)
# Attributes a script reads to know what to request into this container.
_DEFERRED_ATTRS = (
    "data-load", "data-ajax", "data-url", "data-endpoint", "data-remote",
    "data-feed", "data-widget", "data-src",
)


# A waiting panel is not literally empty: it usually holds a short "Loading…"
# label next to the spinner. Anything longer than this is real content.
_PLACEHOLDER_MAX_TEXT = 80


def _is_placeholder_container(el: Any) -> bool:
    """True when an element is an unfilled panel marked to be filled later.

    Holding (almost) no text is the load-bearing part: a container that already
    carries content is not waiting on anything, whatever attributes it has.
    """
    try:
        if len(el.text_content().strip()) > _PLACEHOLDER_MAX_TEXT:
            return False
    except Exception:
        return False

    attrs = el.attrib
    # Own markers: a spinner class, or a data hook naming what to fetch.
    blob = " ".join(f"{k}={v}" for k, v in attrs.items())
    if _PLACEHOLDER_RE.search(attrs.get("class", "") or ""):
        return True
    if any(a in attrs for a in _DEFERRED_ATTRS):
        return True
    # Or it contains nothing but a spinner image.
    for child in el.iter():
        if child is el:
            continue
        if child.tag == "img":
            src = child.get("src", "") or child.get("data-src", "") or ""
            if _PLACEHOLDER_RE.search(src):
                return True
        cls = child.get("class", "") if hasattr(child, "get") else ""
        if cls and _PLACEHOLDER_RE.search(cls):
            return True
    return bool(_PLACEHOLDER_RE.search(blob))


# Deferred panels alone are not enough: measured across a spread of real pages,
# GitHub shows 79 of them and python docs 7, with their content fully present.
# What sets a page whose data never arrived apart is that the text left behind
# carries no figures at all (meteologix: 0.25% digits, against 1.1-6.5% for
# pages whose content is intact). Both conditions must hold.
AJAX_SHELL_MAX_DIGIT_RATIO = 0.005
# Below this the page is a plain JS shell, already handled upstream.
AJAX_SHELL_MIN_TEXT_CHARS = 200


def count_placeholder_containers(raw_html: str, stop_at: int = 0) -> int:
    """Count unfilled panels in a document (optionally short-circuiting)."""
    try:
        from lxml import html as lxml_html
        tree = lxml_html.fromstring(raw_html)
    except Exception:
        return 0
    found = 0
    counted: List[Any] = []
    for el in tree.iter():
        if not isinstance(el.tag, str) or el.tag not in _CONTAINER_TAGS:
            continue
        if not _is_placeholder_container(el):
            continue
        # A waiting panel is usually a wrapper around a spinner div, and both
        # match. Count the outermost only, so "2 panels" means two of them.
        if any(el in ancestor.iter() for ancestor in counted):
            continue
        counted.append(el)
        found += 1
        if stop_at and found >= stop_at:
            return found
    return found


def detect_ajax_shell(raw_html: str, extracted_text: str) -> bool:
    """True when a page's data panels are waiting on XHRs that carry the data.

    Conservative by construction: a page must both show unfilled panels *and*
    have given up text with essentially no numbers in it. Either signal alone
    fires on ordinary pages that merely defer some widget.
    """
    if not raw_html or len(raw_html) < AJAX_SHELL_MIN_BODY_BYTES:
        return False
    if len(extracted_text) < AJAX_SHELL_MIN_TEXT_CHARS:
        return False
    digits = len(_NUM_RE.findall(extracted_text))
    if digits / len(extracted_text) >= AJAX_SHELL_MAX_DIGIT_RATIO:
        return False
    return count_placeholder_containers(
        raw_html, stop_at=AJAX_SHELL_MIN_PLACEHOLDERS
    ) >= AJAX_SHELL_MIN_PLACEHOLDERS
