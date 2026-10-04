"""RFC 6265 cookie matching for SessionProfile (#99).

Dedicated tests for host-only vs Domain cookies, path-boundary matching,
Secure over HTTPS only, expiry, duplicate-name replacement order, and
lookalike-host rejection. All fixtures are fake cookies — no real sessions
are read or replayed.
"""
from __future__ import annotations

import time

import pytest

from sieve.session_coherence import SessionProfile


@pytest.fixture()
def profile():
    return SessionProfile(name="test")


class TestHostOnlyVsDomain:
    def test_host_only_cookie_never_sent_to_subdomain(self, profile):
        profile.add_cookie("sid", "abc", "example.com", host_only=True)
        assert profile.cookie_header_for_url("https://example.com/") == "sid=abc"
        assert profile.cookie_header_for_url("https://www.example.com/") == ""

    def test_domain_cookie_is_sent_to_subdomain(self, profile):
        profile.add_cookie("sid", "abc", "example.com")
        assert profile.cookie_header_for_url("https://example.com/") == "sid=abc"
        assert profile.cookie_header_for_url("https://www.example.com/") == "sid=abc"
        assert profile.cookie_header_for_url("https://a.b.example.com/") == "sid=abc"

    def test_leading_dot_domain_is_normalized(self, profile):
        profile.add_cookie("sid", "abc", ".example.com")
        assert profile.cookie_header_for_url("https://www.example.com/") == "sid=abc"

    def test_lookalike_suffix_host_never_matches(self, profile):
        profile.add_cookie("sid", "abc", "example.com")
        assert profile.cookie_header_for_url("https://notexample.com/") == ""
        assert profile.cookie_header_for_url("https://example.com.evil.test/") == ""

    def test_unrelated_host_never_matches(self, profile):
        profile.add_cookie("sid", "abc", "example.com")
        assert profile.cookie_header_for_url("https://other.test/") == ""


class TestPathMatching:
    def test_exact_path_and_prefixes_match(self, profile):
        profile.add_cookie("c", "1", "example.com", path="/docs")
        assert profile.cookie_header_for_url("https://example.com/docs") == "c=1"
        assert profile.cookie_header_for_url("https://example.com/docs/page") == "c=1"
        assert profile.cookie_header_for_url("https://example.com/docs/page/x") == "c=1"

    def test_path_boundary_respected(self, profile):
        profile.add_cookie("c", "1", "example.com", path="/docs")
        # A sibling directory sharing the text prefix is not a match.
        assert profile.cookie_header_for_url("https://example.com/documents") == ""
        assert profile.cookie_header_for_url("https://example.com/docs-old") == ""
        assert profile.cookie_header_for_url("https://example.com/docs2") == ""
        assert profile.cookie_header_for_url("https://example.com/") == ""

    def test_root_path_cookie_sent_everywhere(self, profile):
        profile.add_cookie("c", "1", "example.com", path="/")
        assert profile.cookie_header_for_url("https://example.com/any/where") == "c=1"

    def test_longer_path_cookie_ordered_first(self, profile):
        profile.add_cookie("root", "r", "example.com", path="/")
        profile.add_cookie("deep", "d", "example.com", path="/a/b")
        header = profile.cookie_header_for_url("https://example.com/a/b/page")
        assert header == "deep=d; root=r"


class TestSecureFlag:
    def test_secure_cookie_not_sent_over_http(self, profile):
        profile.add_cookie("sess", "s3cret", "example.com", secure=True)
        assert profile.cookie_header_for_url("https://example.com/") == "sess=s3cret"
        assert profile.cookie_header_for_url("http://example.com/") == ""

    def test_plain_cookie_sent_over_both_schemes(self, profile):
        profile.add_cookie("c", "1", "example.com")
        assert profile.cookie_header_for_url("http://example.com/") == "c=1"
        assert profile.cookie_header_for_url("https://example.com/") == "c=1"


class TestExpiry:
    def test_expired_cookie_not_sent(self, profile):
        profile.add_cookie("c", "1", "example.com", expires=time.time() - 1)
        assert profile.cookie_header_for_url("https://example.com/") == ""

    def test_future_expiry_sent(self, profile):
        profile.add_cookie("c", "1", "example.com", expires=time.time() + 3600)
        assert profile.cookie_header_for_url("https://example.com/") == "c=1"

    def test_session_cookie_without_expiry_sent(self, profile):
        profile.add_cookie("c", "1", "example.com")
        assert profile.cookie_header_for_url("https://example.com/") == "c=1"


class TestDuplicateNameOrder:
    def test_same_name_domain_path_replaces_in_place(self, profile):
        profile.add_cookie("a", "old", "example.com")
        profile.add_cookie("b", "keep", "example.com")
        profile.add_cookie("a", "new", "example.com")
        header = profile.cookie_header_for_url("https://example.com/")
        # Replacement keeps creation position (RFC 6265 §5.3 step 2).
        assert header == "a=new; b=keep"

    def test_same_name_different_paths_both_kept(self, profile):
        profile.add_cookie("a", "deep", "example.com", path="/x")
        profile.add_cookie("a", "root", "example.com", path="/")
        header = profile.cookie_header_for_url("https://example.com/x")
        assert header == "a=deep; a=root"

    def test_same_name_different_host_only_both_kept(self, profile):
        profile.add_cookie("a", "exact", "www.example.com", host_only=True)
        profile.add_cookie("a", "wild", "example.com")
        header = profile.cookie_header_for_url("https://www.example.com/")
        assert header == "a=exact; a=wild"


class TestLegacyCompatibility:
    def test_domain_only_header_refuses_ambiguous_scope(self, profile):
        profile.add_cookie("sid", "abc", "example.com", host_only=True)
        with pytest.raises(ValueError, match="full HTTP"):
            profile.cookie_header("www.example.com")

    def test_header_alias_obeys_url_scope(self, profile):
        profile.add_cookie("sid", "abc", "example.com", host_only=True, secure=True, path="/private")
        profile.add_cookie("expired", "old", "example.com", expires=time.time() - 1)
        assert profile.cookie_header("https://example.com/private") == "sid=abc"
        for url in ("http://example.com/private", "https://www.example.com/private", "https://example.com/"):
            assert profile.cookie_header(url) == ""

    def test_persisted_minimal_jar_normalized(self, profile):
        """A persisted jar with only name/value/domain still works (#99)."""
        restored = SessionProfile.from_dict(profile.to_dict(include_secrets=True))
        assert restored.cookie_jar == []
        profile.cookie_jar.append({"name": "legacy", "value": "v", "domain": "example.com"})
        jar = SessionProfile(cookie_jar=profile.cookie_jar)
        assert jar.cookie_header_for_url("https://www.example.com/") == "legacy=v"

    def test_cookies_for_url_returns_copies(self, profile):
        profile.add_cookie("c", "1", "example.com")
        matched = profile.cookies_for_url("https://example.com/")
        matched[0]["value"] = "tampered"
        assert profile.cookies()[0]["value"] == "1"

    def test_to_dict_does_not_leak_cookies_by_default(self, profile):
        profile.add_cookie("sess", "s3cret", "example.com", secure=True)
        data = profile.to_dict()
        assert "cookie_jar" not in data
        assert data["cookie_count"] == 1
        assert "s3cret" not in str(data)
class TestSameSiteContext:
    def test_strict_cookie_requires_same_site_context(self, profile):
        profile.add_cookie("sid", "strict", "example.com", same_site="Strict")
        assert profile.cookie_header_for_url("https://example.com/") == ""
        assert profile.cookie_header_for_url("https://example.com/", same_site=True) == "sid=strict"

    def test_lax_cookie_allows_only_safe_top_level_cross_site_navigation(self, profile):
        profile.add_cookie("sid", "lax", "example.com", same_site="Lax")
        assert profile.cookie_header_for_url("https://example.com/") == ""
        assert profile.cookie_header_for_url("https://example.com/", top_level_navigation=True) == "sid=lax"
        assert profile.cookie_header_for_url("https://example.com/", top_level_navigation=True, method="POST") == ""

    def test_none_requires_secure_cookie(self, profile):
        profile.add_cookie("bad", "none", "example.com", same_site="None")
        profile.add_cookie("good", "none", "example.com", same_site="None", secure=True)
        assert profile.cookie_header_for_url("https://example.com/") == "good=none"
        assert profile.cookie_header_for_url("http://example.com/") == ""

    def test_persisted_browser_attribute_is_honored(self, profile):
        jar = SessionProfile(cookie_jar=[{"name": "sid", "value": "strict", "domain": "example.com", "sameSite": "Strict"}])
        assert jar.cookie_header_for_url("https://example.com/") == ""
        assert jar.cookie_header_for_url("https://example.com/", same_site=True) == "sid=strict"
