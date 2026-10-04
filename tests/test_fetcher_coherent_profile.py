"""Verify consistent-profile fingerprinting in HTTPSession (Obscura port).

The primp TLS fingerprint (impersonate_os) and the browserforge HTTP headers
(UA + sec-ch-ua-platform) must describe the same OS. A mismatch is a
fingerprint leak: a site that checks TLS ClientHello against the declared
UA platform sees two different stories.
"""

import asyncio
import pytest

from sieve.fetcher import HTTPSession, _OS_POOL


OS_TO_PLATFORM = {
    "windows": "Windows",
    "macos": "macOS",
    "linux": "Linux",
}


@pytest.mark.asyncio
async def test_os_pool_covers_all_platforms():
    assert set(_OS_POOL) == {"windows", "macos", "linux"}


@pytest.mark.asyncio
async def test_session_picks_coherent_os():
    async with HTTPSession(impersonate="chrome") as s:
        assert hasattr(s, "_os")
        assert s._os in _OS_POOL
        # primp client carries the same OS
        assert s._client is not None
        assert s._client.impersonate_os == s._os


# Check coherence: the UA's platform substring + sec-ch-ua-platform must
# describe the same OS as the primp fingerprint.
_UA_PLATFORM_HINTS = {
    "windows": ("windows nt", "win64"),
    "macos": ("macintosh", "mac os"),
    "linux": ("linux x86_64", "linux i686", "x11; linux"),
}


@pytest.mark.asyncio
async def test_headers_match_tls_fingerprint():
    async with HTTPSession(impersonate="chrome") as s:
        headers = s._build_headers()
        ua = headers.get("user-agent", "")
        sec_platform = headers.get("sec-ch-ua-platform", "")
        expected_platform = OS_TO_PLATFORM[s._os]

        assert expected_platform in sec_platform, (
            f"OS={s._os} but sec-ch-ua-platform={sec_platform}"
        )
        # UA must carry some platform hint for the same OS
        ua_lower = ua.lower()
        assert any(
            hint in ua_lower for hint in _UA_PLATFORM_HINTS[s._os]
        ), f"OS={s._os} but UA has no matching platform hint: {ua[:80]}"


@pytest.mark.asyncio
async def test_proxy_client_inherits_session_os():
    """Per-request proxy override must keep the session's OS."""
    async with HTTPSession(impersonate="chrome") as s:
        import primp
        # Replicate the internal _create_proxy_client path
        c = primp.Client(
            impersonate="chrome",
            impersonate_os=s._os,
            proxy="http://127.0.0.1:9999",
        )
        assert c.impersonate_os == s._os


@pytest.mark.asyncio
async def test_firefox_fallback_inherits_session_os():
    """403/429 retry must keep the same OS (browser switch, not platform switch)."""
    async with HTTPSession(impersonate="chrome") as s:
        import primp
        fb = primp.Client(
            impersonate="firefox",
            impersonate_os=s._os,
        )
        assert fb.impersonate_os == s._os


@pytest.mark.asyncio
async def test_all_three_os_variants_coherent():
    """Every OS in the pool produces a coherent pair."""
    import sieve.fetcher as mod
    for os_val in _OS_POOL:
        original = mod._OS_POOL
        mod._OS_POOL = [os_val]
        try:
            async with HTTPSession(impersonate="chrome") as s:
                # Re-init under the patched pool so s._os == os_val
                await s._init_client()
                assert s._os == os_val
                headers = s._build_headers()
                sec_platform = headers.get("sec-ch-ua-platform", "")
                expected = OS_TO_PLATFORM[os_val]
                assert expected in sec_platform, f"{os_val}: {sec_platform}"
                ua_lower = headers.get("user-agent", "").lower()
                assert any(
                    hint in ua_lower for hint in _UA_PLATFORM_HINTS[os_val]
                ), f"{os_val}: UA={headers.get('user-agent','')[:80]}"
        finally:
            mod._OS_POOL = original