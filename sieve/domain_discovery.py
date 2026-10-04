"""Multi-source domain URL discovery.

Reconstructed clean-room against Sieve's own tests
(tests/test_ports_domain_similar.py) and the crawl CLI call site
(``sieve crawl --discover-domains`` in sieve/server.py); no upstream-derived
code.

Aggregates URLs for one domain from a set of bounded sources — sitemap
(reusing sieve.sitemap), robots.txt (Sitemap:/Disallow:/Allow: lines), site
feeds, and homepage links — with the transport injected for testability.
The wayback/crt/cc/probe sources are declared but not implemented: they are
recorded in ``sources_failed`` rather than silently skipped, so operators
can see what did not run.

Scope flags mirror the /map-style surface documented in the CLI:

* ``sitemap_only`` — fast path, sitemap source only.
* ``include_subdomains`` — admit ``sub.domain`` (default: exact host only).
* ``allow_external`` — no host filtering at all.
* ``map_search`` — case-insensitive substring filter on URLs.

Host filtering is exact-host or proper-subdomain only: a lookalike host that
merely *ends with* the domain's text (``notexample.com`` vs ``example.com``)
is always rejected (verified by test_domain_filter_rejects_lookalike_suffix_host).
"""

from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass, field
from urllib.parse import urljoin, urlparse

from sieve.security import (
    SecurityError,
    _BLOCKED_HOSTNAMES,
    _DNS_REBINDING_SUFFIXES,
    _is_forbidden_ip,
    _normalize_ip_notation,
    validate_url,
)


def _redirect_handler_base():
    """Late-bound so importing this module never requires network machinery."""
    import urllib.request as _ur

    return _ur.HTTPRedirectHandler


_urllib_redirect_handler = _redirect_handler_base()

__all__ = ["DiscoveryResult", "discover_domains_sync"]

VALID_SOURCES = {"sitemap", "robots", "feed", "homepage", "wayback", "crt", "cc", "probe"}
DEFAULT_SOURCES = ["sitemap", "robots", "feed", "homepage"]

_MAX_FEED_URLS = 50
_MAX_HOMEPAGE_URLS = 100
_HTTP_GET_TIMEOUT_S = 10
_HTTP_GET_MAX_BYTES = 512 * 1024


@dataclass
class DiscoveryResult:
    """Aggregated discovery outcome for one domain."""

    domain: str
    urls: list[str] = field(default_factory=list)
    sources_used: list[str] = field(default_factory=list)
    sources_failed: list[str] = field(default_factory=list)
    via: str = ""


def _host_of(url: str) -> str:
    from sieve.url_policy import hostname
    return hostname(url)


def _host_allowed(url: str, domain: str, include_subdomains: bool, allow_external: bool) -> bool:
    """Exact-host or proper-subdomain match; lookalike suffixes never pass."""
    if allow_external:
        return True
    host = _host_of(url)
    if not host:
        return False
    expected = domain.lower().rstrip(".")
    if include_subdomains:
        return host == expected or host.endswith("." + expected)
    return host == expected


def _dedupe(urls: list[str]) -> list[str]:
    """Order-preserving deduplication."""
    seen: set[str] = set()
    out: list[str] = []
    for url in urls:
        if url not in seen:
            seen.add(url)
            out.append(url)
    return out


def _validate_start_url(start_url: str) -> str:
    """Validate the discovery seed before the domain is derived (issue #39).

    Enforces the DNS-free subset of the canonical trust boundary so offline
    tests and callers can rely on deterministic rejection: http(s)-only
    schemes, no backslash authority confusion, no embedded credentials
    (userinfo), no internal hostnames, and no literal/alternate-notation
    private IPs. DNS-based SSRF is caught per-fetch: :func:`_stdlib_http_get`
    revalidates every target (including redirect hops) through the canonical
    security layer before the socket is opened.
    """
    if not isinstance(start_url, str) or not start_url.strip():
        raise SecurityError("start_url must be a non-empty string")
    candidate = start_url.strip()
    if "\\" in candidate:
        raise SecurityError("URL contains backslash character (potential SSRF bypass)")
    if "://" not in candidate:
        candidate = "https://" + candidate.lstrip("/")
    parsed = urlparse(candidate)
    scheme = (parsed.scheme or "").lower()
    if scheme not in ("http", "https"):
        raise SecurityError(f"Only http and https URLs are supported, got: {scheme}")
    if parsed.username or parsed.password:
        raise SecurityError("URL must not embed credentials (userinfo)")
    hostname = (parsed.hostname or "").lower().rstrip(".")
    if not hostname:
        raise SecurityError("URL has no valid hostname")
    if hostname in _BLOCKED_HOSTNAMES or hostname.endswith(_DNS_REBINDING_SUFFIXES):
        raise SecurityError(f"URL targets internal service: {hostname}")
    # Literal and alternate-notation IPs reuse the canonical SSRF deny-set.
    addr = None
    try:
        addr = ipaddress.ip_address(hostname)
    except ValueError:
        normalized = _normalize_ip_notation(hostname)
        if normalized is not None:
            try:
                addr = ipaddress.ip_address(normalized)
            except ValueError:
                addr = None
    if addr is not None and _is_forbidden_ip(addr):
        raise SecurityError(f"URL targets internal/private IP ({hostname})")
    return candidate


def _bounded_http_fetch(open_fn, url: str):  # noqa: W0212
    """Bounded fetch loop shared by the default transport (#39).

    Every hop — the original target and each redirect destination — is
    revalidated through the canonical security layer (scheme, literal and
    DNS-resolved private IPs) before the next request. Redirects are
    surfaced, never followed blindly. Response bodies are byte-bounded.
    ``open_fn(request)`` returns a context-manager response; injected for
    deterministic offline tests.
    """
    import urllib.request as urllib_request
    from urllib.error import HTTPError

    current = url
    for _hop in range(3):
        try:
            current = validate_url(current)
        except Exception:
            return None
        req = urllib_request.Request(
            current, headers={"User-Agent": "Sieve-DomainDiscovery/1.0"}
        )
        try:
            with open_fn(req) as resp:
                code = int(getattr(resp, "status", 0))
                if code in (301, 302, 303, 307, 308):
                    loc = resp.headers.get("Location")
                    if not loc:
                        return None
                    current = urljoin(current, loc)
                    continue
                body = resp.read(_HTTP_GET_MAX_BYTES + 1)
                if not body or len(body) > _HTTP_GET_MAX_BYTES:
                    return None
                return (code, body)
        except HTTPError as e:
            # urllib raises instead of yielding 3xx responses when a
            # no-redirect handler is installed: validate and follow.
            if e.code in (301, 302, 303, 307, 308):
                loc = (e.headers or {}).get("Location") if e.headers is not None else None
                if not loc:
                    return None
                current = urljoin(current, loc)
                continue
            return None
        except Exception:
            return None
    return None


def _stdlib_http_get(url: str):
    """Canonical bounded default transport (#39).

    Unlike a bare urllib.urlopen fallback, every target — and every redirect
    destination — is validated through the canonical security layer before
    the fetch, and response bodies are byte-bounded.
    """
    import urllib.request as urllib_request

    opener = urllib_request.build_opener(_DiscoveryNoRedirect())
    return _bounded_http_fetch(
        lambda request: opener.open(request, timeout=_HTTP_GET_TIMEOUT_S), url
    )


class _DiscoveryNoRedirect(_urllib_redirect_handler):
    """urllib opener handler that surfaces redirects instead of following them
    (issue #39): every redirect hop is validated before the next fetch."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None

    def http_error_301(self, req, fp, code, msg, headers):
        return None

    http_error_302 = http_error_301
    http_error_303 = http_error_301
    http_error_307 = http_error_301
    http_error_308 = http_error_301


def _body_text(body) -> str:
    if isinstance(body, bytes):
        return body.decode("utf-8", errors="ignore")
    return str(body)


def _discover_sitemap(base: str, http_get, max_urls: int) -> list[str]:
    """Delegate to sieve.sitemap's discovery (shared parsing/bounds)."""
    try:
        from sieve.sitemap import discover_sitemap

        result = discover_sitemap(base, http_get=http_get, max_urls=max_urls)
        return [entry.url for entry in result.urls[:max_urls]]
    except Exception:
        return []


def _discover_robots(base: str, http_get) -> list[str]:
    """URLs from robots.txt: Sitemap: lines plus non-trivial Disallow:/Allow:
    paths (they are real, crawler-legitimate routes)."""
    try:
        response = http_get(base + "/robots.txt")
        if not response or response[0] != 200:
            return []
        text = _body_text(response[1])
        urls: list[str] = []
        for line in text.splitlines():
            line = line.strip()
            lowered = line.lower()
            if lowered.startswith("sitemap:"):
                sitemap_url = line.split(":", 1)[1].strip().split()[0]
                if sitemap_url:
                    urls.append(sitemap_url)
            elif lowered.startswith(("disallow:", "allow:")):
                path = line.split(":", 1)[1].strip().split()[0] if ":" in line else ""
                if path and path != "/" and not path.startswith("*"):
                    urls.append(urljoin(base + "/", path))
        return urls
    except Exception:
        return []


def _discover_feed(base: str, http_get) -> list[str]:
    """Probe common feed paths; return absolute <link>/href targets."""
    feed_paths = ("/feed", "/rss", "/atom.xml", "/feed.xml", "/rss.xml", "/index.xml")
    for path in feed_paths:
        try:
            response = http_get(base + path)
            if not response or response[0] != 200:
                continue
            body = _body_text(response[1])
            matches = re.findall(
                r"<link[^>]*>([^<]+)</link>|href=[\"']([^\"']+)[\"']", body
            )
            urls = [
                a or b
                for a, b in matches
                if (a or b).strip().startswith("http")
            ]
            if urls:
                return urls[:_MAX_FEED_URLS]
        except Exception:
            continue
    return []


def _discover_homepage(base: str, http_get) -> list[str]:
    """Same-host links on the homepage, fragments stripped."""
    try:
        response = http_get(base + "/")
        if not response or response[0] != 200:
            return []
        body = _body_text(response[1])
        from sieve.url_policy import same_domain
        urls: list[str] = []
        for href in re.findall(r'href=["\']([^"\']+)["\']', body, re.IGNORECASE):
            if href.startswith(("javascript:", "mailto:", "#")):
                continue
            absolute = urljoin(base + "/", href)
            if same_domain(absolute, base, subdomains=True):
                urls.append(absolute.split("#")[0])
        return urls[:_MAX_HOMEPAGE_URLS]
    except Exception:
        return []


def discover_domains_sync(
    start_url: str,
    *,
    sources: list[str] | None = None,
    max_urls: int = 500,
    http_get=None,
    sitemap_only: bool = False,
    include_subdomains: bool = False,
    allow_external: bool = False,
    map_search: str | None = None,
) -> DiscoveryResult:
    """Discover URLs for ``start_url``'s domain across the requested sources.

    Args:
        start_url: Seed URL; its host defines the discovery domain. Validated
            through the trust boundary before any fetch (issue #39): private
            IPs, credentials in the URL, internal hostnames and non-http(s)
            schemes are rejected with :class:`SecurityError`.
        sources: Source names (subset of :data:`VALID_SOURCES`); defaults to
            :data:`DEFAULT_SOURCES`. Unknown names are ignored.
        max_urls: Cap on returned URLs (after dedup, filtering, and search).
        http_get: ``callable(url) -> (status, body) | None``; injected for
            testability. Defaults to a bounded stdlib fetcher.
        sitemap_only: Restrict to the sitemap source (fast path).
        include_subdomains: Admit subdomains of the start domain.
        allow_external: Disable host filtering entirely.
        map_search: Case-insensitive substring filter on discovered URLs.

    Returns:
        :class:`DiscoveryResult` with ``urls``, per-source ``sources_used`` /
        ``sources_failed``, and a ``via`` summary (``"+"``-joined used
        sources, or ``"none"``).
    """
    start_url = _validate_start_url(start_url)
    parsed = urlparse(start_url)
    domain = (parsed.hostname or "").lower().rstrip(".")
    if not domain:
        raise SecurityError(f"start_url has no valid hostname: {start_url!r}")

    selected = [s for s in (DEFAULT_SOURCES if sources is None else sources) if s in VALID_SOURCES]
    if sitemap_only:
        selected = [s for s in selected if s == "sitemap"] or ["sitemap"]

    if http_get is None:
        http_get = _stdlib_http_get

    if start_url.startswith("http"):
        base = f"{parsed.scheme}://{domain}"
    else:
        base = f"https://{domain}"  # unreachable after _validate_start_url; kept defensively

    collected: list[str] = []
    used: list[str] = []
    failed: list[str] = []

    for source in selected:
        try:
            if source == "sitemap":
                found = _discover_sitemap(base, http_get, max_urls)
            elif source == "robots":
                found = _discover_robots(base, http_get)
            elif source == "feed":
                found = _discover_feed(base, http_get)
            elif source == "homepage":
                found = _discover_homepage(base, http_get)
            else:
                # wayback / crt / cc / probe: declared but not implemented.
                found = []
            if found:
                collected.extend(found)
                used.append(source)
            else:
                failed.append(source)
        except Exception:
            failed.append(source)

    filtered = [
        url for url in collected
        if _host_allowed(url, domain, include_subdomains, allow_external)
    ]
    if map_search:
        needle = map_search.lower()
        filtered = [url for url in filtered if needle in url.lower()]

    deduped = _dedupe(filtered)[:max_urls]
    return DiscoveryResult(
        domain=domain,
        urls=deduped,
        sources_used=used,
        sources_failed=failed,
        via="+".join(used) if used else "none",
    )
