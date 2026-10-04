import socket
import ipaddress

import pytest

from sieve.security import (
    SecurityError,
    _is_forbidden_ip,
    validate_cdp_url,
    validate_url,
)


@pytest.mark.parametrize("url", [
    "ws://127.0.0.1:9222/devtools/browser/x",
    "http://localhost:9222/json/version",
    "https://[::1]:9222/json/version",
])
def test_validate_cdp_url_allows_loopback(url):
    assert validate_cdp_url(url) == url


@pytest.mark.parametrize("url", [
    "ws://192.168.1.20:9222/devtools/browser/x",
    "ws://remote.example:9222/devtools/browser/x",
    "file:///tmp/browser",
    "ws://user:pass@127.0.0.1:9222/devtools/browser/x",
])
def test_validate_cdp_url_rejects_remote_or_unsafe_targets(url):
    with pytest.raises(SecurityError):
        validate_cdp_url(url)


def test_validate_url_rejects_hostname_resolving_to_private_ip(monkeypatch):
    def fake_getaddrinfo(*args, **kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 80))]

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)

    with pytest.raises(SecurityError, match="resolves to internal/private IP"):
        validate_url("https://public-looking.example/path")


def test_validate_url_allows_public_hostname(monkeypatch):
    def fake_getaddrinfo(*args, **kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))]

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)

    assert validate_url("https://example.com/path") == "https://example.com/path"


@pytest.mark.parametrize("address", [
    "100.64.0.1",       # CGNAT / RFC 6598
    "198.18.0.1",       # benchmarking / RFC 2544
    "192.88.99.1",      # 6to4 relay anycast
    "192.0.0.8",        # special-purpose IPv4
    "2002:7f00:1::",    # 6to4 loopback
    "64:ff9b::7f00:1",  # NAT64 loopback
    "64:ff9b:1::1",     # local-use translation prefix
    "2001:db8::1",      # IPv6 documentation
    "3fff::1",          # IPv6 documentation
])
def test_forbidden_ip_special_ranges(address):
    assert _is_forbidden_ip(ipaddress.ip_address(address))


@pytest.mark.parametrize("address", [
    "100.63.255.255",
    "100.128.0.0",
    "198.20.0.1",
    "192.88.100.1",
    "192.0.0.9",        # globally reachable PCP anycast
    "192.0.0.10",       # globally reachable PCP anycast
    "2002:808:808::",   # 6to4 public address
    "64:ff9b::808:808", # NAT64 public address
    "3fff:1000::1",    # outside 3fff::/20 documentation range
])
def test_forbidden_ip_special_range_boundaries_remain_allowed(address):
    assert not _is_forbidden_ip(ipaddress.ip_address(address))


@pytest.mark.parametrize("url", [
    "https://[2002:7f00:1::]/",
    "https://[64:ff9b::a9fe:a9fe]/",
    "https://[::ffff:100.100.100.200]/",
])
def test_validate_url_blocks_embedded_private_ipv6(url):
    with pytest.raises(SecurityError, match="internal/private IP"):
        validate_url(url)
def test_reranker_rejects_corrupted_tokenizer_without_loading_model(tmp_path, monkeypatch):
    import hashlib
    from sieve import reranker

    model = b"fixture-model"
    tokenizer = b"fixture-tokenizer"
    (tmp_path / "model.onnx").write_bytes(model)
    (tmp_path / "tokenizer.json").write_bytes(tokenizer)
    monkeypatch.setattr(reranker, "MODEL_DIR", tmp_path)
    monkeypatch.setattr(reranker, "MIN_MODEL_BYTES", 1)
    monkeypatch.setattr(reranker, "EXPECTED_SHA256", {
        "model.onnx": hashlib.sha256(model).hexdigest(),
        "tokenizer.json": hashlib.sha256(tokenizer).hexdigest(),
    })
    assert reranker._ensure_model() == (tmp_path / "model.onnx", tmp_path / "tokenizer.json")
    (tmp_path / "tokenizer.json").write_bytes(b"corrupted-fixture")
    assert reranker._ensure_model() is None
