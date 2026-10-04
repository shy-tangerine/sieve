"""Cache security tests (issues #31, #254, #4).

- #31: cache lives under ~/.sieve/cache with owner-only directory permissions.
- #254: secret-bearing envelope keys never reach disk.
- #4: request-affecting options (extraction flags, PDF password) are part of
  the cache key so different requests never share an entry.
"""

import json
import stat
from pathlib import Path

import pytest

from sieve import cache


@pytest.fixture
def cache_dir(tmp_path):
    return tmp_path / "cache"


@pytest.mark.asyncio
async def test_cache_dir_created_owner_only(cache_dir):
    await cache._ensure_db(cache_dir)
    mode = stat.S_IMODE(cache_dir.stat().st_mode)
    assert mode & 0o077 == 0, f"cache dir is group/world accessible: {oct(mode)}"


@pytest.mark.asyncio
async def test_existing_loose_dir_is_tightened(cache_dir):
    cache_dir.mkdir(parents=True)
    cache_dir.chmod(0o755)
    await cache._ensure_db(cache_dir)
    mode = stat.S_IMODE(cache_dir.stat().st_mode)
    assert mode & 0o077 == 0


def test_sanitize_envelope_drops_secret_keys():
    env = {
        "metadata": {"title": "ok"},
        "password": "hunter2",
        "api_key": "sk-123",
        "nested": {"cookie": "session=abc", "keep": "yes"},
        "links": [{"url": "https://x", "authorization": "Bearer z"}],
    }
    clean = cache._sanitize_envelope(env)
    assert clean["metadata"] == {"title": "ok"}
    assert "password" not in clean
    assert "api_key" not in clean
    assert "cookie" not in clean["nested"]
    assert clean["nested"]["keep"] == "yes"
    assert clean["links"][0] == {"url": "https://x"}


@pytest.mark.asyncio
async def test_secret_envelope_keys_never_reach_disk(cache_dir):
    await cache.set_cached(
        "https://example.com", "markdown", ["content"], 200,
        cache_dir=cache_dir,
        envelope={"metadata": {"password": "hunter2", "title": "t"}},
    )
    raw = (cache_dir / "cache.db").read_bytes()
    assert b"hunter2" not in raw
    # Round-trip keeps the non-secret part.
    row = await cache.get_cached("https://example.com", "markdown", cache_dir=cache_dir)
    assert row is not None
    assert row["envelope"]["metadata"]["title"] == "t"
    assert "password" not in row["envelope"]["metadata"]


def test_extraction_flags_change_cache_key():
    a = cache._cache_key("https://x", "markdown", main_content_only=True)
    b = cache._cache_key("https://x", "markdown", main_content_only=False)
    c = cache._cache_key("https://x", "markdown", use_trafilatura=False)
    assert len({a, b, c}) == 3, "request-affecting flags must change the key"


def test_password_is_hashed_into_key_and_absent_from_it():
    k1 = cache._cache_key("https://x", "markdown", password="secret-pdf-pass")
    k2 = cache._cache_key("https://x", "markdown", password="other-pass")
    k3 = cache._cache_key("https://x", "markdown")
    assert len({k1, k2, k3}) == 3
    assert "secret-pdf-pass" not in k1  # hashed, never stored verbatim


@pytest.mark.asyncio
async def test_different_passwords_do_not_share_entries(cache_dir):
    await cache.set_cached("https://x", "text", ["for A"], 200,
                           cache_dir=cache_dir, password="pwA")
    await cache.set_cached("https://x", "text", ["for B"], 200,
                           cache_dir=cache_dir, password="pwB")
    row_a = await cache.get_cached("https://x", "text", cache_dir=cache_dir, password="pwA")
    row_b = await cache.get_cached("https://x", "text", cache_dir=cache_dir, password="pwB")
    assert row_a["content"] == ["for A"]
    assert row_b["content"] == ["for B"]


@pytest.mark.asyncio
async def test_flag_change_is_a_cache_miss(cache_dir):
    await cache.set_cached("https://x", "markdown", ["stripped"], 200,
                           cache_dir=cache_dir, main_content_only=True)
    hit = await cache.get_cached("https://x", "markdown", cache_dir=cache_dir,
                                 main_content_only=True)
    miss = await cache.get_cached("https://x", "markdown", cache_dir=cache_dir,
                                  main_content_only=False)
    assert hit is not None
    assert miss is None


def test_default_cache_dir_is_under_sieve():
    assert cache._CACHE_DIR == Path.home() / ".sieve" / "cache"
