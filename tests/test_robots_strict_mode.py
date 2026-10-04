"""Strict robots enforcement tests (issue #3): fail closed on unavailable policy.

The fail-open ``is_allowed`` contract is pinned in test_robots_enforcement.py.
This module pins the strict-mode contract added for issue #3: when the policy
outcome is indeterminate (transport/DNS/timeout failures, 401/403, 5xx, parse
errors), strict mode refuses the fetch instead of converting the failure into
implicit permission.
"""

import pytest

from sieve import robots


@pytest.fixture(autouse=True)
def _clean_robots_cache():
    robots._robots_cache.clear()
    robots._robots_inflight.clear()
    robots._last_fetch_status.clear()
    yield
    robots._robots_cache.clear()
    robots._robots_inflight.clear()
    robots._last_fetch_status.clear()


def _parser_with_rules(rules: list[str]):
    from urllib.robotparser import RobotFileParser

    parser = RobotFileParser()
    parser.parse(rules)
    return parser


@pytest.mark.asyncio
async def test_policy_outcomes(monkeypatch):
    """robots_policy returns the three-way outcome, not a collapsed bool."""
    parser = _parser_with_rules(["User-agent: *", "Disallow: /private/"])

    async def fake_parser(domain, user_agent="*"):
        return parser, None

    monkeypatch.setattr(robots, "_get_robots_parser_with_status", fake_parser)
    assert await robots.robots_policy("https://example.com/private/x") == "disallowed"
    assert await robots.robots_policy("https://example.com/public") == "allowed"


@pytest.mark.asyncio
async def test_unavailable_policy_is_classified(monkeypatch):
    """No parser and no status = unavailable (transport/DNS/timeout failure)."""
    async def fake_parser(domain, user_agent="*"):
        return None, None

    monkeypatch.setattr(robots, "_get_robots_parser_with_status", fake_parser)
    assert await robots.robots_policy("https://down.example.com/x") == "unavailable"


@pytest.mark.asyncio
async def test_http_404_is_no_policy_not_unavailable(monkeypatch):
    """404/410 = the site publishes no policy: conventionally allowed."""
    async def fake_parser(domain, user_agent="*"):
        return None, 404

    monkeypatch.setattr(robots, "_get_robots_parser_with_status", fake_parser)
    assert await robots.robots_policy("https://nopolicy.example.com/x") == "no_policy"
    assert await robots.is_allowed_strict("https://nopolicy.example.com/x") is True


@pytest.mark.parametrize("status", [401, 403, 500, 502, 503])
@pytest.mark.asyncio
async def test_auth_walled_and_5xx_fail_closed(monkeypatch, status):
    """401/403 (policy exists but is walled) and 5xx deny in strict mode."""
    async def fake_parser(domain, user_agent="*"):
        return None, status

    monkeypatch.setattr(robots, "_get_robots_parser_with_status", fake_parser)
    assert await robots.robots_policy("https://walled.example.com/x") == "unavailable"
    assert await robots.is_allowed_strict("https://walled.example.com/x") is False


@pytest.mark.asyncio
async def test_malformed_robots_content_fails_closed(monkeypatch):
    """A parser that raises on can_fetch yields unavailable, not allow."""
    class BoomParser:
        def can_fetch(self, ua, url):
            raise RuntimeError("corrupt rules")

    async def fake_parser(domain, user_agent="*"):
        return BoomParser(), None

    monkeypatch.setattr(robots, "_get_robots_parser_with_status", fake_parser)
    assert await robots.robots_policy("https://corrupt.example.com/x") == "unavailable"
    assert await robots.is_allowed_strict("https://corrupt.example.com/x") is False


@pytest.mark.asyncio
async def test_disallowed_fails_closed_trivially(monkeypatch):
    parser = _parser_with_rules(["User-agent: *", "Disallow: /"])

    async def fake_parser(domain, user_agent="*"):
        return parser, None

    monkeypatch.setattr(robots, "_get_robots_parser_with_status", fake_parser)
    assert await robots.is_allowed_strict("https://example.com/anything") is False


@pytest.mark.asyncio
async def test_allowed_policy_passes_strict(monkeypatch):
    parser = _parser_with_rules(["User-agent: *", "Disallow: /private/"])

    async def fake_parser(domain, user_agent="*"):
        return parser, None

    monkeypatch.setattr(robots, "_get_robots_parser_with_status", fake_parser)
    assert await robots.is_allowed_strict("https://example.com/public") is True


@pytest.mark.asyncio
async def test_fetch_status_classifier(monkeypatch):
    """_fetch_robots_txt records the status for 404/401/403/5xx responses."""
    class FakeResp:
        status = 403
        body = b"User-agent: *\nDisallow: /\n"
        encoding = "utf-8"

    class FakeSession:
        def __init__(self, *a, **kw): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def get(self, *a, **kw): return FakeResp()

    import sieve.fetcher as fetcher_mod
    monkeypatch.setattr(fetcher_mod, "HTTPSession", FakeSession)

    result = await robots._fetch_robots_txt("walled.example.org")
    assert result is None
    assert robots._last_fetch_status["walled.example.org"] == 403


@pytest.mark.asyncio
async def test_security_rejection_never_retries_through_urllib(monkeypatch):
    from sieve import fetcher
    from sieve.security import SecurityError
    import urllib.request

    class RejectedSession:
        def __init__(self, **kwargs): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *args): return False
        async def get(self, url, **kwargs):
            raise SecurityError("redirect to a private network rejected")

    def unsafe_fallback(*args, **kwargs):
        pytest.fail("security rejection retried through unchecked urllib")

    monkeypatch.setattr(fetcher, "HTTPSession", RejectedSession)
    monkeypatch.setattr(urllib.request, "urlopen", unsafe_fallback)
    assert await robots._fetch_robots_txt("public.example.org") is None


@pytest.mark.asyncio
async def test_strict_mode_wired_into_smart_fetch(monkeypatch):
    """respect_robots=True routes through the strict check, not the loose one."""
    from sieve.server import MasterFetchServer
    import sieve.server_fetch as sf

    calls = {"strict": 0, "loose": 0}

    async def fake_strict(url):
        calls["strict"] += 1
        return False

    async def fake_loose(url):
        calls["loose"] += 1
        return False

    monkeypatch.setattr(sf, "is_allowed_strict", fake_strict)
    monkeypatch.setattr(sf, "is_allowed", fake_loose, raising=False)

    server = MasterFetchServer(cache_ttl=0)
    resp = await server.smart_fetch(
        "https://example.com/page", extraction_type="text",
        respect_robots=True,
    )
    assert calls["strict"] == 1
    assert calls["loose"] == 0
    assert resp.error in ("robots_txt_disallowed", "robots_unavailable")
