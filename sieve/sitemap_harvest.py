"""Sitemap-first URL harvesting for hard-walled sites (Scout 13).

Why: sites behind aggressive anti-bot walls (Fiverr/PerimeterX, DataDome sites)
still serve their sitemap index files publicly with NO challenge. Verified live
on fiverr.com: sitemap.xml, sitemap_gigs[1-7].xml.gz, sitemap_users.xml.gz and
sitemap_categories.xml.gz all return 200 with full URL lists. So instead of
fighting the bot wall to *discover* targets, we harvest the target URL list from
the sitemaps and spend our (expensive) rendered fetches only on the pages we
actually want.

Pure stdlib (urllib.request, gzip, re), no file I/O beyond network reads, and
never raises: every entry point returns [] / None on any failure. XML parsing is
delegated to sieve.sitemap.parse_sitemap; malformed sitemap XML is treated as a gap, not repaired silently.

Public surface:
  SITEMAP_INDEX_HINTS                -> conventional index paths to probe
  fetch_url(url)                     -> bytes | None (gzip auto-decompressed)
  harvest_urls(base_url)             -> list[str] leaf URLs from the sitemap tree
  harvest_gig_urls(domain)           -> list[str] Fiverr gig URLs ('/p/' pattern)
"""

from __future__ import annotations

import gzip
import io
import re
import urllib.request
from urllib.parse import urljoin, urlparse


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Surface redirects instead of following them (#95): every hop is
    validated by the caller before the next fetch, matching the crawl path."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None

    http_error_301 = redirect_request
    http_error_302 = redirect_request
    http_error_303 = redirect_request
    http_error_307 = redirect_request
    http_error_308 = redirect_request


def _build_opener():
    return urllib.request.build_opener(_NoRedirect)

from sieve.sitemap import parse_sitemap_detailed

SITEMAP_INDEX_HINTS = (
    "sitemap.xml",
    "sitemap_index.xml",
    "sitemap/sitemap.xml",
    "robots.txt",
)

DEFAULT_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "Chrome/149.0 Safari/537.36"
)

_MAX_SITEMAPS = 60          # bound the index forest walk
_MAX_DEPTH = 4              # sitemapindex -> child -> child ...
_MAX_SITEMAP_BYTES = 50 * 1024 * 1024
_MAX_GZIP_BYTES = 10 * 1024 * 1024
_ROBOTS_SITEMAP_RE = re.compile(r"(?im)^\s*sitemap\s*:\s*(\S+)\s*$")


def _maybe_gunzip(body: bytes) -> bytes:
    if body[:2] == b"\x1f\x8b":
        if len(body) > _MAX_GZIP_BYTES:
            raise ValueError("compressed sitemap exceeds size limit")
        try:
            with gzip.GzipFile(fileobj=io.BytesIO(body)) as stream:
                expanded = stream.read(_MAX_SITEMAP_BYTES + 1)
            if len(expanded) > _MAX_SITEMAP_BYTES:
                raise ValueError("decompressed sitemap exceeds size limit")
            return expanded
        except Exception:
            raise
    if len(body) > _MAX_SITEMAP_BYTES:
        raise ValueError("sitemap exceeds size limit")
    return body


def fetch_url(
    url: str,
    *,
    timeout: float = 20.0,
    ua: str = DEFAULT_UA,
) -> bytes | None:
    """GET `url` and return the body bytes, or None on any failure.

    gzip is auto-decompressed, detected via Content-Encoding *or* magic bytes
    (`.xml.gz` sitemaps are gzip payloads regardless of transfer encoding).
    """
    if not url:
        return None
    try:
        # (#95) Canonical destination validation before the fetch, and a
        # no-redirect opener so each hop is re-validated.
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            return None
        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": ua,
                "Accept": "application/xml,text/xml,text/plain,*/*",
                "Accept-Language": "en-US,en;q=0.9",
            },
        )
        with _build_opener().open(req, timeout=timeout) as resp:  # noqa: S310
            body = resp.read(_MAX_SITEMAP_BYTES + 1)
            enc = (resp.headers.get("Content-Encoding") or "").lower()
        if not body:
            return None
        if "gzip" in enc:
            # Content-Encoding is attacker-controlled. Decompress through the
            # same bounded reader used for .gz sitemap payloads rather than
            # gzip.decompress(), which materializes the entire expanded body.
            try:
                with gzip.GzipFile(fileobj=io.BytesIO(body)) as stream:
                    expanded = stream.read(_MAX_SITEMAP_BYTES + 1)
                if len(expanded) > _MAX_SITEMAP_BYTES:
                    raise ValueError("decompressed sitemap exceeds size limit")
                body = expanded
            except Exception:
                return None
        return _maybe_gunzip(body)
    except Exception:
        return None


# Internal alias so the recursive walkers bind the real transport at import
# time (keeps harvesting deterministic even if a caller swaps fetch_url).
_fetch = fetch_url


def _robots_sitemaps(robots_body: bytes, robots_url: str) -> list[str]:
    try:
        text = robots_body.decode("utf-8", errors="replace")
    except Exception:
        return []
    out: list[str] = []
    seen: set[str] = set()
    for m in _ROBOTS_SITEMAP_RE.finditer(text):
        u = urljoin(robots_url, m.group(1).strip())
        if u and u not in seen:
            seen.add(u)
            out.append(u)
    return out


def _leaf_urls(parsed_urls) -> list[str]:
    """parse_sitemap yields SitemapURL dataclasses; flatten to plain strings."""
    out: list[str] = []
    for item in parsed_urls or []:
        u = getattr(item, "url", item)
        if isinstance(u, str) and u:
            out.append(u.split("#")[0])
    return out


def _parse_sitemap(body: bytes):
    """Harvest only authoritative documents, including sitemap indexes."""
    result = parse_sitemap_detailed(_maybe_gunzip(body))
    return ([], []) if result.recovered else (result.urls, result.sub_sitemaps)


def harvest_urls(
    base_url: str,
    *,
    max_urls: int = 2000,
    timeout: float = 20.0,
) -> list[str]:
    """Harvest leaf URLs from a site's sitemap tree. Never raises.

    Probes SITEMAP_INDEX_HINTS in order against `base_url`; a fetched robots.txt
    contributes its `Sitemap:` directives as extra candidates. Recurses into
    <sitemapindex> children (gzip children handled) until max_urls is reached.
    Returns [] when no sitemap is reachable/parseable - the caller decides the
    fallback (e.g. rendered crawl).
    """
    collected: list[str] = []
    try:
        if not base_url:
            return []
        root = base_url.rstrip("/")
        if not urlparse(root).scheme:
            root = "https://" + root

        seen_urls: set[str] = set()
        visited: set[str] = set()
        queue: list[tuple[str, int]] = []

        def _push(u: str, depth: int) -> None:
            if u and u not in visited and depth <= _MAX_DEPTH:
                queue.append((u, depth))

        for hint in SITEMAP_INDEX_HINTS:
            _push(root + "/" + hint, 0)

        while queue and len(collected) < max_urls and len(visited) < _MAX_SITEMAPS:
            url, depth = queue.pop(0)
            if url in visited:
                continue
            visited.add(url)
            body = _fetch(url, timeout=timeout)
            if not body:
                continue
            if url.endswith("robots.txt"):
                for sm in _robots_sitemaps(body, url):
                    _push(sm, depth)
                continue
            try:
                parsed, children = _parse_sitemap(body)
            except Exception:
                continue
            for u in _leaf_urls(parsed):
                if u in seen_urls:
                    continue
                seen_urls.add(u)
                collected.append(u)
                if len(collected) >= max_urls:
                    break
            for child in children or []:
                _push(urljoin(url, child), depth + 1)
    except Exception:
        pass
    return collected[:max_urls]


def harvest_gig_urls(
    domain: str = "www.fiverr.com",
    *,
    max_urls: int = 5000,
    timeout: float = 30.0,
) -> list[str]:
    """Harvest Fiverr gig URLs straight from the public gig sitemaps (Scout 13).

    Fiverr shards its gig URLs across sitemap_gigs1..7.xml.gz, all served without
    a PerimeterX challenge. Gig pages live at /<seller>/<slug> exposed in the
    sitemap as '/p/' URLs, so we filter on that marker. Falls back to the
    sitemap.xml index. Returns [] on network failure.
    """
    out: list[str] = []
    try:
        dom = (domain or "www.fiverr.com").strip().strip("/")
        if "://" in dom:
            dom = urlparse(dom).netloc or dom
        base = "https://" + dom

        candidates = [f"{base}/sitemap_gigs{i}.xml.gz" for i in range(1, 8)]
        candidates.append(f"{base}/sitemap.xml")

        seen: set[str] = set()
        for url in candidates:
            if len(out) >= max_urls:
                break
            body = _fetch(url, timeout=timeout)
            if not body:
                continue
            try:
                parsed, children = _parse_sitemap(body)
            except Exception:
                continue
            leaves = _leaf_urls(parsed)
            # sitemap.xml is an index: pull gig-looking children one level down
            for child in (children or [])[:_MAX_SITEMAPS]:
                if "gig" not in child.lower():
                    continue
                cbody = _fetch(urljoin(url, child), timeout=timeout)
                if cbody:
                    try:
                        cparsed, _ = _parse_sitemap(cbody)
                    except Exception:
                        continue
                    leaves.extend(_leaf_urls(cparsed))
            for u in leaves:
                # Fiverr gig URLs are /<username>/<slug> — exactly 2 non-empty
                # path segments after the host (e.g. /michael7878/master-your-song).
                # (Older writeups filtered '/p/' — Fiverr no longer uses that.)
                path = urlparse(u).path.strip("/")
                if path.count("/") != 1 or u in seen:
                    continue
                seen.add(u)
                out.append(u)
                if len(out) >= max_urls:
                    break
    except Exception:
        pass
    return out[:max_urls]
