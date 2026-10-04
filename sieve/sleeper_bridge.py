"""Sleeper integration — real-browser tier for Sieve.

Sleeper is a DOM-level browser control bridge (browser extension + local
daemon on :8790). It drives the REAL browser the user runs — real cookies,
real fingerprint, real session, real auth tokens captured off the page's own
requests. This is Sieve's tier-3 "hard target" fetcher for sites that defeat
patchright (Cloudflare interactive, DataDome, PerimeterX, Akamai).

Merged into Sieve 2026-08-11 from an external Sleeper integration. The daemon
and CLI remain independently deployed by the operator.

Why this tier exists (community intel, Scouts 1/7/10/12):
- CDP-handshake fingerprinting kills headless (nodriver insight, Scout 7)
- Temporal/session coherence is the #1 trust signal (Scout 12)
- Session-cookie reuse is the only viable path on DataDome/PX (Scouts 1/10)
- Sleeper's `api` command carries the page's REAL Authorization token —
  O(1) backend calls instead of DOM scraping (Scout 10's "mint + replay"
  pattern, already built)

Usage:
    from sieve.sleeper_bridge import sleeper_fetch, sleeper_api, is_available
    html = sleeper_fetch("https://www.fiverr.com/search/gigs?query=3d")  # real browser
    data = sleeper_api("/api/endpoint", method="GET")                     # with real auth

Daemon: POST http://127.0.0.1:8790/command  {"cmd": ..., "args": {...}}
CLI:    sleeper <cmd> [args]  (wraps the same endpoint)
"""

from __future__ import annotations

import json
import logging
import os
import urllib.request
import uuid
from pathlib import Path
from urllib.parse import urlparse
from urllib.error import URLError

logger = logging.getLogger("sieve.sleeper")

def _sleeper_base() -> str:
    from sieve.config import get as _cfg
    return (os.environ.get("SLEEPER_BASE", "") or
            str(_cfg("sleeper_base", "http://127.0.0.1:8790") or "http://127.0.0.1:8790"))


SLEEPER_BASE = "http://127.0.0.1:8790"  # default; per-call override via _sleeper_base()
SLEEPER_TIMEOUT = 45.0


def _timeout_value(value: float | None) -> float:
    raw = os.environ.get("SLEEPER_TIMEOUT", "") if value is None else value
    try:
        parsed = float(raw) if raw not in ("", None) else SLEEPER_TIMEOUT
    except (TypeError, ValueError):
        return SLEEPER_TIMEOUT
    return parsed if 0 < parsed <= 300 else SLEEPER_TIMEOUT
_MAX_COMMAND_RESPONSE_BYTES = 10 * 1024 * 1024


def _command(cmd: str, args: dict | None = None, tab: str | None = None,
             timeout: float | None = None) -> dict:
    """POST a command to the Sleeper daemon. Returns the result dict."""
    payload: dict = {"cmd": cmd, "args": args or {}}
    if tab is not None:
        payload["tab"] = tab
    body = json.dumps(payload).encode()
    req = urllib.request.Request(
        _sleeper_base() + "/command",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=_timeout_value(timeout)) as resp:
            body = resp.read(_MAX_COMMAND_RESPONSE_BYTES + 1)
            if len(body) > _MAX_COMMAND_RESPONSE_BYTES:
                raise ValueError("Sleeper response exceeds 10 MiB")
            data = json.loads(body.decode())
    except Exception as e:
        logger.warning("Sleeper %s failed: %s", cmd, type(e).__name__)
        if isinstance(e, (URLError, ConnectionError, TimeoutError)):
            return {"ok": False, "error": "Sleeper daemon not reachable on :8790: " + str(e)}
        return {"ok": False, "error": f"Sleeper command failed: {type(e).__name__}"}
    if not data.get("ok"):
        logger.warning("Sleeper %s returned error: %s", cmd, data.get("error"))
    return data


def is_available() -> bool:
    """Cheap liveness probe — daemon up? (Does NOT check extension/tabs.)"""
    try:
        result = _command("tabs", timeout=5)
        return bool(result.get("ok"))
    except Exception:
        return False


def tabs() -> list[dict]:
    """Full tab registry."""
    result = _command("tabs")
    return result.get("result", {}).get("tabs", []) if result.get("ok") else []


def sleeper_fetch(url: str, *, extract: dict | None = None,
                  tab: str | None = None, wait_selector: str | None = None,
                  timeout: float | None = None) -> dict:
    """Navigate the real browser to url and read content.

    Returns {"ok", "url", "title", "text" | "extracted"} where extracted is
    the selector->text map from `extract` (e.g. {"title": "h1",
    "prices": ".price"}).
    """
    if tab is None:
        nav = _command("goto", {"url": url}, timeout=timeout)
        if not nav.get("ok"):
            return nav
        # goto is async (chrome.tabs.update) — wait for the URL to actually
        # change to the target before reading.
        _command("wait_url", {"pattern": url, "timeout_ms": 30000},
                 tab=tab, timeout=timeout)
    if wait_selector:
        # sleeper handler is waitFor(selector) with timeout
        _command("waitFor", {"selector": wait_selector, "timeout": 15000},
                 tab=tab, timeout=timeout)
    # read the page text: body via read_all or exec
    if extract:
        result = _command("extract", {"map": extract}, tab=tab, timeout=timeout)
        if result.get("ok"):
            return {"ok": True, "url": url, "extracted": result.get("result")}
    body = _command("read", {"selector": "body"}, tab=tab, timeout=timeout)
    if body.get("ok"):
        result = body.get("result")
        # read returns {"text": ...} (or {"value":...} for inputs, {"html":...} for html mode)
        if isinstance(result, dict):
            text = result.get("text") or result.get("value") or result.get("html") or ""
        else:
            text = str(result or "")
        if str(text).strip():
            return {"ok": True, "url": url, "text": text}
    # fallback: exec document.body.innerText (handler takes `code`)
    exec_result = _command("exec", {"code": "document.body.innerText"},
                           tab=tab, timeout=timeout)
    text = exec_result.get("result", "") if exec_result.get("ok") else ""
    if str(text).strip():
        return {"ok": True, "url": url, "text": text}
    # An empty render is a shell, CAPTCHA, login wall, or block — never a
    # successful read. Report it so callers record a gap, not evidence.
    return {"ok": False, "url": url, "text": "",
            "error": "empty render — page shell, CAPTCHA, login wall, or block; "
                     "retry with wait_selector for a below-fold marker"}


def sleeper_api(path: str, method: str = "GET", body: dict | None = None,
                tab: str | None = None, timeout: float | None = None) -> dict:
    """Call a page's backend API with the REAL Authorization token captured
    off the page's own requests (the `sleeper api` command). The token never
    leaves the browser; only status + json are returned.

    This is the session-cookie-reuse pattern the anti-bot research
    (Scouts 1/10) identified as the only viable path on DataDome/PX sites —
    already built into Sleeper.
    """
    # Absolute API URLs are checked here as a second, Sieve-side boundary.
    # Relative paths remain same-origin and are validated by the extension.
    allowed = _api_allowed_hosts()
    parsed = urlparse(path)
    if parsed.netloc:
        host = (parsed.hostname or "").lower().rstrip(".")
        if host not in allowed:
            return {"ok": False, "error": f"api: target host not permitted: {host}"}
    args = {"path": path, "method": method, "allowed_hosts": sorted(allowed)}
    if body is not None:
        args["body"] = json.dumps(body)
    result = _command("api", args, tab=tab, timeout=timeout)
    return result.get("result", {}) if result.get("ok") else result


def _api_allowed_hosts() -> set[str]:
    """Return hosts supported by both Sieve and the current Sleeper extension.

    The extension currently hard-codes ChatGPT hosts. Operator configuration
    may narrow that set, but cannot expand it until Sleeper implements the
    companion config-driven allowlist.
    """
    supported = {"chatgpt.com", "www.chatgpt.com"}
    from sieve.config import get as _cfg
    value = _cfg("sleeper_api_hosts", None, env="SIEVE_SLEEPER_API_HOSTS")
    if value is None:
        return supported
    if isinstance(value, str):
        values = value.split(",")
    elif isinstance(value, (list, tuple, set)):
        values = value
    else:
        values = []
    configured = {str(v).strip().lower().rstrip(".") for v in values if str(v).strip()}
    return configured & supported


def sleeper_shot(url: str | None = None, *, tab: str | None = None,
                 out_path: str | None = None,
                 timeout: float | None = None) -> dict:
    """Screenshot the tab's window via the Sleeper extension (viewport PNG).

    Navigates first when url is given. Returns {"ok", "url", "path"} — image
    bytes go to out_path (default ./sieve-shot-<epoch>.png), never stdout.
    Full-page stitching is pool-only; this captures the visible viewport.
    """
    import base64

    if url:
        nav = _command("goto", {"url": url}, tab=tab, timeout=timeout)
        if not nav.get("ok"):
            return nav
        _command("wait_url", {"pattern": url, "timeout_ms": 30000}, tab=tab, timeout=timeout)
    result = _command("shot", {}, tab=tab, timeout=timeout)
    if not result.get("ok"):
        return result
    data_url = (result.get("result") or {}).get("dataUrl", "")
    if not data_url.startswith("data:image/png;base64,"):
        return {"ok": False, "error": "sleeper shot returned no PNG data"}
    encoded = data_url.split(",", 1)[1]
    if len(encoded) > 20 * 1024 * 1024:
        return {"ok": False, "error": "sleeper shot payload exceeds safety limit"}
    try:
        raw = base64.b64decode(encoded, validate=True)
    except (ValueError, base64.binascii.Error):
        return {"ok": False, "error": "sleeper shot returned invalid PNG base64 data"}
    if len(raw) > 15 * 1024 * 1024 or not raw.startswith(b"\x89PNG\r\n\x1a\n"):
        return {"ok": False, "error": "sleeper shot PNG is invalid or too large"}
    # Path policy (#193): agent-facing filesystem output gets the same safe
    # boundary as media downloads — confined to ~/.sieve/downloads, with
    # traversal/symlink escapes rejected, and an atomic temp-file replace.
    root = (Path.home() / ".sieve" / "downloads").resolve()
    root.mkdir(parents=True, exist_ok=True)
    requested = Path(out_path) if out_path else root / f"sieve-shot-{uuid.uuid4().hex}.png"
    if not requested.is_absolute():
        requested = root / requested
    try:
        target = requested.resolve(strict=False)
    except OSError:
        return {"ok": False, "error": "sleeper shot path is invalid"}
    if target != root and root not in target.parents:
        return {"ok": False, "error":
                f"out_path must stay inside {root} (screenshot writes are confined "
                "to an explicit safe boundary)"}
    if target.exists() and target.is_symlink():
        return {"ok": False, "error": "out_path must not be a symlink"}
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
    try:
        with open(tmp, "wb") as f:
            f.write(raw)
        os.replace(tmp, target)
    except OSError:
        tmp.unlink(missing_ok=True)
        raise
    return {"ok": True, "url": url, "path": str(target), "bytes": len(raw)}


if __name__ == "__main__":
    import sys
    if len(sys.argv) < 2:
        print("Usage: python3 -m sieve.sleeper_bridge <url>")
        sys.exit(1)
    print("available:", is_available())
    print("tabs:", len(tabs()))
    r = sleeper_fetch(sys.argv[1])
    print("ok:", r.get("ok"), "| text chars:", len(r.get("text", "")))
    if r.get("text"):
        print(r["text"][:500])
