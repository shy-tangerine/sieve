"""DataDome cookie-harvest + HTTP-tier replay orchestrator for Sieve.

This module implements the Scout 1/8/15 recipe for beating DataDome-class
anti-bots (DataDome, PerimeterX, Cloudflare turnstile on hard targets):

  1. Let a REAL browser (Sleeper) visit the page and SOLVE the
     challenge. The browser's real fingerprint + the user's real session are
     what gets the `datadome` cookie (and friends) set as a Set-Cookie on the
     domain. (Scout 1: session-cookie reuse is the only viable path; Scout 8:
     temporal/session coherence is the #1 trust signal.)

  2. Capture the FULL cookie set off the real browser (context.cookies()),
     plus the browser's navigator.userAgent and the proxy IP it used.

  3. REPLAY those cookies through a fast HTTP client (primp / httpx +
     impersonation) at the HTTP tier (Scout 15's MediaCrawler pattern).

CRITICAL: the DataDome token is bound to the client's fingerprint. The replay
MUST use the SAME impersonation profile (same TLS/JA3 + HTTP/2 fingerprint and
same Chrome major version) as the harvesting browser, or the token is rejected.

This module is an ORCHESTRATOR, not a fetcher. It defines the recipe and
provides pure-stdlib helpers (cookie-header builder, impersonation matcher,
replay wrapper). The actual browser solve and the HTTP replay call are
INJECTED callables — the parent wires primp/sleeper at integration time. No
network I/O, no file I/O, stdlib only.

Usage:
    from sieve.datadome_bridge import (
        harvest_plan, cookie_header, match_impersonation, replay_fetch,
    )
    plan = harvest_plan("https://www.etsy.com/market/game_art")
    imp  = match_impersonation(browser_ua, primp.available_profiles)
    res  = replay_fetch(url, cookies=cookies, impersonation=imp,
                        fetch=primp.get)
"""

from __future__ import annotations

import logging
import re

logger = logging.getLogger("master-fetch.datadome_bridge")

# Cookie names that anti-bot / bot-management vendors drop on the client.
# We capture the FULL cookie set at harvest time, but these are the ones that
# matter for replaying past a challenge on a hard target.
DD_COOKIE_NAMES = ("datadome", "_px3", "_pxhd", "_pxvid", "cf_clearance")


def harvest_plan(url: str, *, warmup: bool = True) -> dict:
    """Return the DataDome cookie-harvest + replay recipe plan.

    Pure planning — no side effects, no network. The parent consumes the
    returned dict to drive the real browser (tier) and then the HTTP replay
    (HTTP tier). The recipe's steps are:

      1. navigate — browser visits the homepage first if warmup is on, so the
         request stream looks human and the challenge is issued in-context.
      2. solve     — browser passes the challenge; `datadome` (and friends)
         get set as Set-Cookie on .etsy.com / the target domain.
      3. capture   — grab context.cookies() (ALL cookies) + navigator.userAgent
         + the proxy IP the browser used.
      4. replay    — fast HTTP client with the SAME chrome impersonation +
         the full cookie header harvested above.

    Args:
        url: The target landing URL to eventually fetch at the HTTP tier.
        warmup: Whether to require a homepage warmup navigation before the
            challenge-bearing request (recommended; improves coherence signal).

    Returns:
        dict: {"url", "warmup", "steps": [{"step", "detail"}, ...]}.
    """
    return {
        "url": url,
        "warmup": warmup,
        "steps": [
            {
                "step": "navigate",
                "detail": "browser visits homepage first if warmup",
            },
            {
                "step": "solve",
                "detail": (
                    "browser passes challenge; datadome set as Set-Cookie "
                    "on .etsy.com/.domain"
                ),
            },
            {
                "step": "capture",
                "detail": (
                    "grab context.cookies() ALL cookies + "
                    "navigator.userAgent + proxy IP"
                ),
            },
            {
                "step": "replay",
                "detail": (
                    "HTTP client with SAME chrome impersonation + "
                    "full cookie header"
                ),
            },
        ],
    }


def cookie_header(cookies: list[dict], *, domain: str | None = None) -> str:
    """Build a ``Cookie`` request-header value from a cookie list.

    Args:
        cookies: List of cookie dicts, each with at least ``name`` and
            ``value`` keys, and optionally a ``domain`` key (e.g. the output
            of a browser's ``context.cookies()``).
        domain: If given, only include cookies whose ``domain`` contains this
            substring (case-sensitive). Use to scope a global cookie jar to a
            specific host (e.g. domain="etsy.com" matches ".etsy.com" and
            "www.etsy.com"). If None, include every cookie.

    Returns:
        str: ``"name=value; name2=value2"`` for the selected cookies, or ``""``
        if no cookies are selected.
    """
    parts: list[str] = []
    for c in cookies:
        if domain is not None:
            cdomain = c.get("domain")
            if not cdomain or domain not in cdomain:
                continue
        name = c.get("name")
        if name is None:
            continue
        parts.append(f"{name}={c.get('value', '')}")
    return "; ".join(parts)


def match_impersonation(browser_ua: str, available: list[str]) -> str | None:
    """Pick the best impersonation target to match a harvested browser UA.

    The DataDome token is fingerprint-bound, so the HTTP-tier replay must mimic
    the harvesting browser's TLS/JA3 + HTTP/2 + Chrome-major fingerprint. Given
    the browser's user-agent string and a list of available impersonation
    targets (e.g. ``['chrome_148','chrome_147','safari_18','firefox_128']``),
    return the closest matching target name, or ``None`` if nothing fits.

    Heuristic:
      - Chrome UA: extract ``Chrome/<major>`` (regex ``r'Chrome/(\\d+)'``) and
        return the available ``chrome_<n>`` with the smallest numeric distance
        from that major version.
      - Non-Chrome UA: look for a ``Firefox`` or ``Safari`` token and return
        the first available target whose name starts with that engine prefix.

    Args:
        browser_ua: The user-agent string captured from the real browser.
        available: List of impersonation target names the HTTP client supports.

    Returns:
        str | None: Best matching target name, or None if no match.
    """
    # Chrome / Chromium family (also present in Edge, but chrome_XXX is the
    # impersonation target; the major version is what matters for the token).
    chrome_m = re.search(r"Chrome/(\d+)", browser_ua)
    if chrome_m:
        browser_major = int(chrome_m.group(1))
        best: str | None = None
        best_dist = None
        for target in available:
            tm = re.match(r"chrome_(\d+)$", target)
            if not tm:
                continue
            dist = abs(int(tm.group(1)) - browser_major)
            if best_dist is None or dist < best_dist:
                best_dist = dist
                best = target
        return best

    # Firefox family.
    if re.search(r"Firefox/\d+", browser_ua):
        for target in available:
            if target.startswith("firefox"):
                return target
        return None

    # Safari family (Safari UAs do not carry a Chrome/ token).
    if "Safari" in browser_ua:
        for target in available:
            if target.startswith("safari"):
                return target
        return None

    return None


def replay_fetch(
    url: str,
    *,
    cookies: list[dict],
    impersonation: str,
    fetch: callable,
) -> dict:
    """Replay a harvested cookie set through the injected HTTP client.

    Calls ``fetch(url, cookies=<header>, impersonation=<profile>)`` with the
    exact signature the parent wires up (primp/httpx-with-impersonation). The
    replay MUST run with the SAME impersonation profile as the harvesting
    browser or the DataDome token is rejected.

    Args:
        url: The URL to fetch at the HTTP tier.
        cookies: Full cookie list (as captured by ``harvest_plan``'s capture
            step) — passed through :func:`cookie_header`.
        impersonation: The impersonation target name to replay with (from
            :func:`match_impersonation`).
        fetch: Injected callable with signature
            ``fetch(url, *, cookies: str, impersonation: str) -> dict`` where
            the returned dict has keys ``ok``, ``status``, ``text``.

    Returns:
        dict: ``{"ok": bool, "status": int|None, "text": str}`` derived from
        the fetch result, or ``{"ok": False, "error": str}`` if fetch raised.
    """
    try:
        result = fetch(
            url,
            cookies=cookie_header(cookies),
            impersonation=impersonation,
        )
        return {
            "ok": bool(result.get("ok")),
            "status": result.get("status"),
            "text": result.get("text", ""),
        }
    except Exception as e:  # noqa: BLE001 — surface any fetch failure cleanly
        # Categorized, sanitized diagnostic (issue #135): raw exception strings
        # can carry URLs, local endpoints, or resolver internals. The detail
        # goes to debug logging only; the caller gets a stable category.
        logger.debug("DataDome bridge fetch failed: %s", e, exc_info=True)
        return {"ok": False, "error": "datadome_bridge_fetch_failed", "error_type": type(e).__name__}


if __name__ == "__main__":
    import json

    plan = harvest_plan("https://www.etsy.com/market/game_art")
    print(json.dumps(plan, indent=2))
    ua = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36"
    )
    print("match:", match_impersonation(ua, ["chrome_148", "chrome_149"]))
