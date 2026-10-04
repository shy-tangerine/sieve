"""Robots.txt enforcement coverage (revision-audit finding T7).

``respect_robots`` defaults to true and is wired into server_fetch, but the
audit's mutation battery showed the entire feature untested: forcing
``is_allowed`` to always-True left the suite green. These tests pin the
behavioral contract of ``sieve.robots``: allow, disallow, and
unreachable-robots fail-open.
"""

import pytest

from sieve import robots


def _parser_with_rules(rules: list[str]):
    from urllib.robotparser import RobotFileParser

    parser = RobotFileParser()
    parser.parse(rules)
    return parser


@pytest.fixture(autouse=True)
def _clean_robots_cache():
    robots._robots_cache.clear()
    robots._robots_inflight.clear()
    yield
    robots._robots_cache.clear()
    robots._robots_inflight.clear()


@pytest.mark.asyncio
async def test_disallowed_url_is_refused(monkeypatch):
    """A robots.txt Disallow must actually refuse the fetch (T7 core case)."""
    parser = _parser_with_rules([
        "User-agent: *",
        "Disallow: /private/",
    ])
    async def fake_parser(domain, user_agent="*"):
        return parser

    monkeypatch.setattr(robots, "_get_robots_parser", fake_parser)

    assert await robots.is_allowed("https://example.com/private/secret") is False
    assert await robots.is_allowed("https://example.com/public/page") is True


@pytest.mark.asyncio
async def test_unreachable_robots_fails_open(monkeypatch):
    """Unreachable robots.txt = allow (documented default)."""
    async def fake_parser(domain, user_agent="*"):
        return None

    monkeypatch.setattr(robots, "_get_robots_parser", fake_parser)
    assert await robots.is_allowed("https://example.com/anything") is True


@pytest.mark.asyncio
async def test_malformed_url_allows(monkeypatch):
    """Malformed URLs are allowed without consulting the parser."""
    called = False

    async def fake_parser(domain, user_agent="*"):
        nonlocal called
        called = True
        return None

    monkeypatch.setattr(robots, "_get_robots_parser", fake_parser)
    assert await robots.is_allowed("not a url") is True
    assert called is False


@pytest.mark.asyncio
async def test_parser_is_cached_per_domain(monkeypatch):
    """Second check on the same domain must hit the cache, not re-fetch."""
    calls = 0
    parser = _parser_with_rules(["User-agent: *", "Disallow:"])

    async def fake_fetch(domain):
        nonlocal calls
        calls += 1
        return "User-agent: *\nDisallow: /\n"

    monkeypatch.setattr(robots, "_fetch_robots_txt", fake_fetch)

    assert await robots.is_allowed("https://cached.example.com/a") is False
    assert await robots.is_allowed("https://cached.example.com/b") is False
    assert calls == 1, f"robots.txt fetched {calls}x for the same domain"
