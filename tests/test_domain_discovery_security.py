"""Security-boundary tests for multi-source domain discovery (#39).

The discovery seed is validated through the DNS-free subset of the canonical
trust boundary before the domain is derived, and the default transport
revalidates every target (including redirect hops) through the canonical
security layer. All fixtures are offline: private/userinfo/internal hosts are
rejected deterministically, and redirect revalidation is tested with a fake
opener — no network, no DNS.
"""
from __future__ import annotations

import email.message
from urllib.request import Request
from urllib.error import HTTPError

import pytest

from sieve.domain_discovery import (
    DiscoveryResult,
    _bounded_http_fetch,
    _stdlib_http_get,
    _validate_start_url,
    discover_domains_sync,
)
from sieve.security import SecurityError


# ── start-url validation (offline, deterministic) ────────────────────

class TestValidateStartUrl:
    @pytest.mark.parametrize("bad", [
        "http://127.0.0.1/x",
        "https://10.1.2.3/",
        "http://169.254.169.254/latest/meta-data",
        "https://192.168.1.10/",
        "http://0177.0.0.1/",           # octal loopback notation
        "https://localhost/",
        "ftp://example.com/sitemap.xml",  # non-http(s) scheme
        "https://user:pass@example.com/",  # embedded credentials
        "https://user@example.com/",
        "https://example.com\\@127.0.0.1/",  # backslash authority confusion
        "https://[::1]/",                # IPv6 loopback
        "",
        None,
        123,
    ])
    def test_private_userinfo_internal_and_schemes_rejected(self, bad):
        with pytest.raises(SecurityError):
            _validate_start_url(bad)

    def test_bare_host_normalized_to_https(self):
        assert _validate_start_url("example.com") == "https://example.com"

    def test_valid_url_passes_through(self):
        assert _validate_start_url("https://example.com/path") == "https://example.com/path"

    def test_discover_rejects_private_seed_before_any_fetch(self):
        """The error surfaces before any transport is constructed or called."""
        calls = []

        def spy_get(url):
            calls.append(url)
            return (200, b"")

        with pytest.raises(SecurityError):
            discover_domains_sync("http://127.0.0.1:8080/", http_get=spy_get)
        assert calls == []

    def test_discover_rejects_userinfo_seed(self):
        with pytest.raises(SecurityError):
            discover_domains_sync("https://user:pass@example.com/", http_get=lambda u: (200, b""))

    def test_lookalike_host_urls_never_admitted(self):
        """A sitemap entry on a lookalike suffix host is filtered (existing
        contract, verified against the hardened input path)."""

        def fake_get(url):
            if url.endswith("/sitemap.xml"):
                body = (
                    '<?xml version="1.0"?><urlset>'
                    "<url><loc>https://example.com/a</loc></url>"
                    "<url><loc>https://notexample.com/b</loc></url>"
                    "</urlset>"
                ).encode()
                return (200, body)
            return (200, b"")

        res = discover_domains_sync("https://example.com", sources=["sitemap"],
                                    http_get=fake_get)
        assert res.urls == ["https://example.com/a"]
        assert isinstance(res, DiscoveryResult)


# ── bounded transport: per-hop revalidation ──────────────────────────

class _FakeResponse:
    def __init__(self, status, headers=None, body=b""):
        self.status = status
        self.headers = headers or email.message.Message()
        self._body = body

    def read(self, n=-1):
        return self._body[:n] if n > 0 else self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class TestBoundedHttpFetch:
    def test_private_target_never_opened(self):
        opened = []

        def open_fn(req):
            opened.append(req.full_url)
            return _FakeResponse(200, body=b"ok")

        assert _bounded_http_fetch(open_fn, "http://127.0.0.1/x") is None
        assert opened == []

    def test_redirect_to_private_target_is_refused(self):
        """A 301 whose Location points at loopback/RFC1918 is never followed."""
        opened = []

        def open_fn(req):
            opened.append(req.full_url)
            resp = _FakeResponse(301)
            resp.headers = email.message.Message()
            resp.headers["Location"] = "http://169.254.169.254/latest"
            return resp

        assert _bounded_http_fetch(open_fn, "https://example.com/sitemap.xml") is None
        # Only the original (public) target was opened; the private hop was not.
        assert opened == ["https://example.com/sitemap.xml"]

    def test_redirect_chain_follows_validated_hops(self):
        urls = []

        def open_fn(req):
            urls.append(req.full_url)
            if req.full_url == "https://example.com/a":
                resp = _FakeResponse(301)
                resp.headers = email.message.Message()
                resp.headers["Location"] = "https://example.com/b"
                return resp
            return _FakeResponse(200, body=b"final")

        assert _bounded_http_fetch(open_fn, "https://example.com/a") == (200, b"final")
        assert urls == ["https://example.com/a", "https://example.com/b"]

    def test_oversize_body_refused(self):
        def open_fn(req):
            return _FakeResponse(200, body=b"x" * (512 * 1024 + 1))

        assert _bounded_http_fetch(open_fn, "https://example.com/x") is None

    def test_redirect_loop_bounded(self):
        n = {"count": 0}

        def open_fn(req):
            n["count"] += 1
            resp = _FakeResponse(302)
            resp.headers = email.message.Message()
            resp.headers["Location"] = "https://example.com/loop"
            return resp

        assert _bounded_http_fetch(open_fn, "https://example.com/loop") is None
        assert n["count"] <= 3

    def test_stdlib_transport_validates_before_network(self):
        """The default transport refuses private targets with no socket use."""
        assert _stdlib_http_get("http://127.0.0.1:9/x") is None


# ── urllib HTTPError path (no-redirect opener raises on 3xx) ─────────

class TestHttpErrorRedirectPath:
    def test_httperror_redirect_location_is_revalidated(self):
        headers = email.message.Message()
        headers["Location"] = "http://10.0.0.5/secret"

        def open_fn(req):
            raise HTTPError(req.full_url, 301, "moved", headers, None)

        assert _bounded_http_fetch(open_fn, "https://example.com/") is None

    def test_httperror_404_returns_none(self):
        def open_fn(req):
            raise HTTPError(req.full_url, 404, "not found", email.message.Message(), None)

        assert _bounded_http_fetch(open_fn, "https://example.com/") is None
