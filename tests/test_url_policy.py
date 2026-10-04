"""One URL corpus exercises fetch authorization and distinct identity policies."""
import pytest
from hypothesis import given, settings, strategies as st
from sieve import url_policy as policy


@pytest.mark.parametrize("value", ["https://user:secret@example.com", "file:///tmp/x", "https://example.com:99999", "https://example.com/\nprivate"])
def test_invalid_identities_are_rejected(value):
    assert policy.hostname(value) == ""
    assert policy.dedupe_url(value) == ""
    with pytest.raises(ValueError):
        policy.fetch_url(value)


def test_domain_boundaries_and_idna():
    assert policy.hostname("https://BÜCHER.example./") == "xn--bcher-kva.example"
    assert policy.same_domain("https://docs.example.com", "https://example.com", subdomains=True)
    assert not policy.same_domain("https://evil-example.com", "https://example.com", subdomains=True)
    assert not policy.same_domain("https://example.com.evil.test", "https://example.com", subdomains=True)
    assert policy.hostname("https://[2001:4860:4860::8888]:443/") == "2001:4860:4860::8888"


def test_dedupe_keeps_opaque_path_and_query_and_does_not_authorize(monkeypatch):
    original = "https://EXAMPLE.com:443/a%2Fb/?q=a%2Bb&q=c&%75tm_source=test#part"
    assert policy.dedupe_url(original) == "https://example.com/a%2Fb?q=a%2Bb&q=c"
    seen = []
    monkeypatch.setattr("sieve.security.validate_url", lambda url: seen.append(url) or url)
    assert policy.fetch_url(original) == original
    assert seen == [original]


@settings(max_examples=60, deadline=None, derandomize=True)
@given(st.text(alphabet="abcABC012/-_%", max_size=128), st.text(alphabet="abc012%+&=", max_size=128))
def test_dedupe_is_idempotent(path, query):
    value = policy.dedupe_url("https://example.com/" + path + "?" + query)
    assert policy.dedupe_url(value) == value


def test_fetch_uses_private_address_guard():
    with pytest.raises(ValueError):
        policy.fetch_url("http://127.0.0.1/private")
