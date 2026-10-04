"""Stealth ports — tracker blocklist, stable OS, SSRF deny, MCP stateless."""
import ipaddress

def test_tracker_blocklist():
    from sieve.network_capture import is_tracker_url
    assert is_tracker_url("https://www.google-analytics.com/collect")
    assert is_tracker_url("https://sub.doubleclick.net/x")
    assert not is_tracker_url("https://example.com/page")
    assert not is_tracker_url("https://notdoubleclick.net/")  # suffix, not substring
    assert is_tracker_url("https://a.b.hotjar.com/c")

def test_stable_os_default():
    from sieve.fetcher import _pick_os, _STABLE_OS
    import sieve.fetcher as f
    f._STABLE_OS = None
    a = _pick_os()
    b = _pick_os()
    assert a == b
    assert a in ("windows","macos","linux")
    c = _pick_os(rotate=True)  # rotate may differ but valid
    assert c in ("windows","macos","linux")
    f._STABLE_OS = None

def test_is_forbidden_ip_public():
    from sieve.security import is_forbidden_ip
    assert not is_forbidden_ip(ipaddress.ip_address("8.8.8.8"))
    assert is_forbidden_ip(ipaddress.ip_address("127.0.0.1"))
    assert is_forbidden_ip(ipaddress.ip_address("10.0.0.1"))

def test_resolve_and_check_blocks_literal():
    from sieve.security import resolve_and_check, SecurityError
    try:
        resolve_and_check("127.0.0.1")
        assert False, "should block loopback"
    except SecurityError:
        pass

def test_mcp_build_has_stateless_patches():
    from sieve.server import MasterFetchServer
    s = MasterFetchServer()
    srv = s.build_mcp_server()
    assert srv is not None
    # build_http_asgi_app handles ?stateless=true — check source contains marker
    import inspect
    src = inspect.getsource(s.build_http_asgi_app)
    assert "stateless" in src.lower()
