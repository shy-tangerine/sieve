"""Sitemap.xml discovery + parsing for smart_crawl (v8).

The genius move for crawling big sites: instead of blind best-first BFS (one
fetch per page to discover the next layer), fetch the site's sitemap.xml in ONE
call and get the complete URL list + <lastmod> dates. Most real sites ship a
sitemap (and declare it in robots.txt). This collapses a 500-page discovery
crawl into a single fetch.

Public surface:
  discover_sitemap(start_url, *, http_get, max_urls) -> SitemapResult
    Find the site's sitemap(s): check robots.txt `Sitemap:` directives first,
    then the conventional /sitemap.xml + /sitemap_index.xml paths. Fetch + parse
    them (recursing into <sitemapindex> children, capped), and return a flat,
    deduped list of {url, lastmod} plus provenance for the response.

  parse_sitemap(xml_bytes) -> (urls, sub_sitemaps)
    Low-level parser: a sitemap.xml is either a <urlset> (leaf: <url><loc>) or a
    <sitemapindex> (nested: <sitemap><loc>). Returns (leaf_urls, child_indexes).

Robustness: XML parsing is namespace-agnostic (lxml local-name), tolerates gzip
(servers sometimes gzip sitemaps), caps total URLs and recursion depth, and
never raises - a failed fetch/parse yields an empty result so crawl falls back
to BFS.

Transport is injected (`http_get`) so this module has no hard dep on a specific
HTTP client and is unit-testable with a fake. `http_get(url) -> (status, bytes)`
or None on failure.
"""

from __future__ import annotations

import gzip
import io
import logging
from dataclasses import dataclass, field
from typing import Callable, Optional
from urllib.parse import urljoin, urlparse

from lxml import etree

logger = logging.getLogger("master-fetch.sitemap")

# A transport callable: url -> (status:int, body:bytes) | None
HttpGet = Callable[[str], Optional[tuple[int, bytes]]]

_MAX_URLS_DEFAULT = 5000      # cap a single discover_sitemap call
_MAX_INDEX_DEPTH = 3          # <sitemapindex> -> child <sitemapindex> -> ...
_MAX_SITEMAPS = 25            # don't recurse into an unbounded index forest
_MAX_SITEMAP_BYTES = 50 * 1024 * 1024  # XML sitemap protocol maximum
_MAX_GZIP_BYTES = 10 * 1024 * 1024     # bound compressed input before inflate


@dataclass
class SitemapURL:
    url: str
    lastmod: str = ""
    # Provenance (#94): True when this URL came from a document that lxml
    # could only salvage with recover=True (malformed/truncated XML).
    # Recovered URLs are flagged, never silently promoted to authoritative.
    recovered: bool = False


@dataclass
class SitemapParseResult:
    """Detailed parse outcome for one sitemap document (#94)."""

    urls: list[SitemapURL] = field(default_factory=list)
    sub_sitemaps: list[str] = field(default_factory=list)
    recovered: bool = False


@dataclass
class SitemapResult:
    urls: list[SitemapURL] = field(default_factory=list)
    sitemaps_used: list[str] = field(default_factory=list)   # the sitemap URLs that actually parsed
    via: str = ""                                             # "robots" | "conventional" | ""
    robots_checked: bool = False
    # Provenance (#94): sitemap documents that only parsed via lxml recovery.
    recovered_sitemaps: list[str] = field(default_factory=list)


def _maybe_gunzip(body: bytes) -> bytes:
    """Sitemaps are sometimes gzip even when not requested (Sitemap .xml.gz paths
    or servers that ignore Accept-Encoding). Detect by magic bytes, not extension."""
    if body[:2] == b"\x1f\x8b":
        if len(body) > _MAX_GZIP_BYTES:
            raise ValueError("compressed sitemap exceeds size limit")
        # Read one byte beyond the output limit so decompression cannot
        # inflate an attacker-controlled gzip bomb without a bound.
        with gzip.GzipFile(fileobj=io.BytesIO(body)) as stream:
            from sieve.resource_budget import current_budget
            account = current_budget()
            limit = min(_MAX_SITEMAP_BYTES, account.remaining("input_bytes")) if account else _MAX_SITEMAP_BYTES
            expanded = stream.read(limit + 1)
        if account is not None and not account.charge("input_bytes", len(expanded)):
            raise ValueError("sitemap input budget exhausted")
        if len(expanded) > limit:
            raise ValueError("decompressed sitemap exceeds size limit")
        return expanded
    if len(body) > _MAX_SITEMAP_BYTES:
        raise ValueError("sitemap exceeds size limit")
    return body


def _parse_sitemap_body(xml_bytes: bytes, *, allow_recovery: bool,
                        max_urls: int = _MAX_URLS_DEFAULT) -> SitemapParseResult:
    """Shared parser behind parse_sitemap / parse_sitemap_detailed (#94).

    Strict lxml parsing is attempted first: a well-formed document parses
    authoritatively. When strict parsing fails and ``allow_recovery`` is
    set, lxml salvages what it can and the result is flagged ``recovered``
    so callers can treat salvaged URLs as partial provenance rather than
    authoritative.
    """
    if not xml_bytes:
        return SitemapParseResult()
    try:
        body = _maybe_gunzip(xml_bytes)
    except Exception:  # issue #94: fail-soft parser boundary — oversized/bad gzip yields empty
        return SitemapParseResult()
    from sieve.resource_budget import current_budget
    account = current_budget()
    if account is not None and not account.charge("nodes", len(body) + 1):
        return SitemapParseResult()
    try:
        root = etree.fromstring(
            body,
            parser=etree.XMLParser(resolve_entities="internal", no_network=True),
        )
        recovered = False
    except Exception:  # issue #94: strict miss falls through to bounded recovery below
        if not allow_recovery:
            return SitemapParseResult()
        try:
            root = etree.fromstring(
                body,
                parser=etree.XMLParser(
                    recover=True,
                    resolve_entities="internal",
                    no_network=True,
                ),
            )
        except Exception:  # issue #94: fail-soft parser boundary — unsalvageable XML yields empty
            return SitemapParseResult()
        recovered = True
    if root is None:
        return SitemapParseResult()

    def lname(el) -> str:
        t = el.tag
        return t.split("}", 1)[1] if isinstance(t, str) and "}" in t else (t or "")

    root_name = lname(root)
    urls: list[SitemapURL] = []
    children: list[str] = []

    if root_name == "urlset":
        for url_el in root:
            if len(urls) >= max_urls:
                break
            if lname(url_el) != "url":
                continue
            if account is not None and not account.charge("items", 1):
                break
            loc = lastmod = ""
            for child in url_el:
                n = lname(child)
                if n == "loc" and child.text:
                    loc = child.text.strip()
                elif n == "lastmod" and child.text:
                    lastmod = child.text.strip()
            if loc:
                urls.append(SitemapURL(url=loc, lastmod=lastmod, recovered=recovered))
    elif root_name == "sitemapindex":
        for sm_el in root:
            if len(children) >= _MAX_SITEMAPS:
                break
            if account is not None and not account.charge("items", 1):
                break
            if lname(sm_el) != "sitemap":
                continue
            for child in sm_el:
                if lname(child) == "loc" and child.text:
                    loc = child.text.strip()
                    if loc:
                        children.append(loc)
                    break
    return SitemapParseResult(urls=urls, sub_sitemaps=children, recovered=recovered)


def parse_sitemap(xml_bytes: bytes) -> tuple[list[SitemapURL], list[str]]:
    """Parse one sitemap.xml document.

    Returns (leaf_urls, child_sitemap_urls). A <urlset> yields leaf_urls only;
    a <sitemapindex> yields child_sitemap_urls only. Namespace-agnostic: matches
    on local-name so the common xmlns="http://www.sitemaps.org/schemas/.../sitemap"
    (and any namespace variant / no namespace) all parse.

    Malformed/truncated documents are salvaged with lxml recovery (issue #94):
    use :func:`parse_sitemap_detailed` when the caller needs to know whether a
    result is authoritative or recovered.
    """
    detailed = _parse_sitemap_body(xml_bytes, allow_recovery=True)
    return detailed.urls, detailed.sub_sitemaps


def parse_sitemap_detailed(xml_bytes: bytes, *, max_urls: int = _MAX_URLS_DEFAULT) -> SitemapParseResult:
    """Parse one sitemap.xml document with partial-recovery provenance (#94).

    Same parsing behavior as :func:`parse_sitemap`, but the returned
    :class:`SitemapParseResult` reports ``recovered`` for documents that were
    only salvageable via lxml recovery, and every leaf URL parsed from such a
    document carries ``SitemapURL.recovered=True``. Callers must not silently
    promote recovered URLs to authoritative results.
    """
    return _parse_sitemap_body(xml_bytes, allow_recovery=True, max_urls=max_urls)


def _fetch(http_get: HttpGet, url: str) -> Optional[bytes]:
    from sieve.resource_budget import current_budget
    account = current_budget()
    if account is not None and (account.expired or account.remaining("input_bytes") <= 0):
        account.truncated.add("input_bytes")
        return None
    try:
        res = http_get(url)
    except Exception:
        return None
    if not res:
        return None
    status, body = res
    if account is not None and not account.charge("input_bytes", len(body)):
        return None
    if status != 200 or not body:
        return None
    return body


def _robots_sitemaps(start_url: str, http_get: HttpGet) -> tuple[list[str], bool]:
    """Fetch /robots.txt and return its Sitemap: directives (absolute URLs)."""
    p = urlparse(start_url)
    robots_url = f"{p.scheme or 'https'}://{p.netloc}/robots.txt"
    body = _fetch(http_get, robots_url)
    if body is None:
        return [], False
    from sieve.resource_budget import current_budget
    account = current_budget()
    if account is not None and not account.charge("nodes", len(body) + 1):
        return [], True
    try:
        text = body.decode("utf-8", errors="replace")
    except Exception:
        return [], True
    out: list[str] = []
    for line in text.splitlines():
        if len(out) >= _MAX_SITEMAPS:
            break
        line = line.strip()
        if line.lower().startswith("sitemap:"):
            val = line.split(":", 1)[1].strip()
            if val:
                out.append(urljoin(robots_url, val))
    # de-dup, preserve order
    seen: set[str] = set()
    uniq = [u for u in out if not (u in seen or seen.add(u))]
    return uniq, True


def _conventional_sitemaps(start_url: str) -> list[str]:
    p = urlparse(start_url)
    base = f"{p.scheme or 'https'}://{p.netloc}"
    return [base + "/sitemap.xml", base + "/sitemap_index.xml"]


def discover_sitemap(start_url: str, *, http_get: HttpGet,
                     max_urls: int = _MAX_URLS_DEFAULT) -> SitemapResult:
    """Discover + fetch the site's sitemap(s), returning a flat URL list.

    Tries robots.txt `Sitemap:` directives first; if none parse, falls back to
    the conventional /sitemap.xml + /sitemap_index.xml paths. Recurses into
    <sitemapindex> children (capped by _MAX_INDEX_DEPTH + _MAX_SITEMAPS). Caps
    total URLs at max_urls. Invalid max_urls values raise ValueError; network or
    parsing failures return an empty result so the caller can fall back to BFS.
    """
    result = SitemapResult()
    if isinstance(max_urls, bool) or not isinstance(max_urls, int) or not 1 <= max_urls <= 100_000:
        raise ValueError("max_urls must be an integer between 1 and 100000")
    if not start_url:
        return result

    candidates: list[str] = []
    robots_smaps, robots_checked = _robots_sitemaps(start_url, http_get)
    result.robots_checked = robots_checked
    if robots_smaps:
        candidates.extend(robots_smaps)
        result.via = "robots"
    else:
        candidates.extend(_conventional_sitemaps(start_url))
        result.via = "conventional"

    seen_urls: set[str] = set()
    flat: list[SitemapURL] = []
    visited_sitemaps: set[str] = set()

    def _drain(sitemap_url: str, depth: int) -> None:
        if depth > _MAX_INDEX_DEPTH or len(visited_sitemaps) >= _MAX_SITEMAPS:
            return
        if sitemap_url in visited_sitemaps:
            return
        visited_sitemaps.add(sitemap_url)
        body = _fetch(http_get, sitemap_url)
        if body is None:
            return
        result.sitemaps_used.append(sitemap_url)  # provenance: every sitemap fetched + parsed
        parsed = parse_sitemap_detailed(body, max_urls=max_urls - len(flat))
        if parsed.recovered:
            # Provenance (#94): salvaged documents are surfaced to callers
            # instead of being silently promoted to authoritative results.
            result.recovered_sitemaps.append(sitemap_url)
        urls, children = parsed.urls, parsed.sub_sitemaps
        for su in urls:
            u = su.url.split("#")[0]
            if not u or u in seen_urls:
                continue
            seen_urls.add(u)
            flat.append(su)
            if len(flat) >= max_urls:
                return
        for child in children:
            if len(flat) >= max_urls:
                return
            _drain(child, depth + 1)

    for c in candidates:
        if len(flat) >= max_urls:
            break
        _drain(c, 0)

    result.urls = flat
    return result
