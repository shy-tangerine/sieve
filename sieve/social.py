"""Public social-page metadata normalization.

This module uses Sieve's ordinary fetch path or an explicitly requested public
reader. It does not log in, harvest cookies, solve CAPTCHAs, or bypass access
controls.
"""

from __future__ import annotations

import hashlib
import html as html_lib
import json
import os
import re
import shutil
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse

import httpx

from .metadata import extract_metadata
from .youtube import _binary as _ytdlp_binary
from .youtube import _run as _ytdlp_run
from .youtube import _runtime_args as _ytdlp_runtime_args

HOSTS = {
    "instagram": {"instagram.com", "www.instagram.com", "m.instagram.com"},
    "tiktok": {"tiktok.com", "www.tiktok.com", "m.tiktok.com", "vm.tiktok.com"},
}
MAX_RESPONSE_BYTES = 8 * 1024 * 1024
MAX_CHECKPOINT_READ_BYTES = 8 * 1024 * 1024
MAX_CHECKPOINT_LINE_BYTES = 1 * 1024 * 1024
CHECKPOINT_SCAN_CHUNK_BYTES = 64 * 1024

SOCIAL_COLLECT_ENV = "SIEVE_ENABLE_SOCIAL_COLLECT"
BROWSER_BACKEND_ENV = "SIEVE_BROWSER_BACKEND"
IG_TRANSCRIPTS_ENV = "IG_TRANSCRIPTS_DIR"


def _transcript_exists(code: Any) -> bool:
    """Return whether retained transcription output already exists for a code."""
    if not code:
        return False
    root = os.environ.get(IG_TRANSCRIPTS_ENV)
    if not root:
        return False
    path = Path(root)
    if not path.is_dir():
        return False
    token = str(code)
    return (path / token).is_file() or any(
        candidate.is_file() and candidate.name.startswith(token + ".")
        for candidate in path.iterdir()
    )


def _checkpoint_aliases(record: dict[str, Any]) -> set[str]:
    """Return every stable identity alias exposed by a social record."""
    platform = str(record.get("platform") or "").strip().lower()
    if not platform:
        url = record.get("url")
        if isinstance(url, str):
            try:
                platform = platform_for_url(url)
            except (TypeError, ValueError):
                platform = ""
    scope = f"{platform}:" if platform else ""
    aliases: set[str] = set()
    for field in ("code", "shortcode"):
        value = record.get(field)
        if value is not None and str(value).strip():
            token = str(value).strip()
            aliases.add(f"{scope}id:{token}")
            # Pre-platform checkpoints were emitted by Instagram collection.
            # Retain their shortcode compatibility without making TikTok IDs
            # share a global namespace.
            if platform == "instagram":
                aliases.add(f"id:{token}")
    url = record.get("url")
    if isinstance(url, str) and url.strip():
        parsed = urlparse(url.strip())
        if parsed.scheme and parsed.netloc:
            normalized = parsed._replace(query="", fragment="").geturl().rstrip("/")
            aliases.add(f"{scope}url:{normalized}")
            if platform == "instagram":
                aliases.add(f"url:{normalized}")
            match = re.search(r"/(?:reel|p|tv|video)/([^/?#]+)", parsed.path)
            if match:
                token = match.group(1)
                aliases.add(f"{scope}id:{token}")
                if platform == "instagram":
                    aliases.add(f"id:{token}")
    return aliases


def _checkpoint_items(items: list[dict[str, Any]], checkpoint: str | None) -> None:
    """Append items not already represented by any stable identity alias."""
    if not checkpoint:
        return
    cp = Path(checkpoint)
    cp.parent.mkdir(parents=True, exist_ok=True)
    existing: set[str] = set()
    if cp.exists():
        # Read only bounded tail, line-by-line. A torn write can end with
        # invalid UTF-8; valid earlier JSONL records remain usable for resume.
        size = cp.stat().st_size
        with cp.open("rb") as source:
            discard_first = size > MAX_CHECKPOINT_READ_BYTES
            if size > MAX_CHECKPOINT_READ_BYTES:
                source.seek(size - MAX_CHECKPOINT_READ_BYTES)
            for raw_line in _checkpoint_lines(source, discard_first=discard_first):
                try:
                    line = raw_line.decode("utf-8")
                except UnicodeDecodeError:
                    continue
                if not line.strip():
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(record, dict):
                    existing.update(_checkpoint_aliases(record))
    fresh: list[dict[str, Any]] = []
    for item in items:
        aliases = _checkpoint_aliases(item)
        if not aliases:
            # Profile corpus records normally have a code. Keep code-less
            # records compatible with prior behavior rather than collapsing
            # unrelated malformed payloads into one checkpoint entry.
            fresh.append(item)
            continue
        if aliases & existing:
            continue
        existing.update(aliases)
        fresh.append(item)
    if not cp.exists():
        fd = os.open(cp, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        handle_ctx = os.fdopen(fd, "a", encoding="utf-8")
    else:
        handle_ctx = cp.open("a", encoding="utf-8")
    with handle_ctx as handle:
        for item in fresh:
            handle.write(json.dumps(item, ensure_ascii=False) + "\n")


def _checkpoint_lines(source: Any, *, discard_first: bool = False):
    """Yield checkpoint lines without allowing an oversized line to grow memory."""
    pending = bytearray()
    oversized = False
    first_line = discard_first
    while True:
        chunk = source.read(CHECKPOINT_SCAN_CHUNK_BYTES)
        if not chunk:
            if pending and not oversized:
                yield bytes(pending)
            return
        start = 0
        while True:
            newline = chunk.find(b"\n", start)
            if newline < 0:
                fragment = chunk[start:]
                if first_line:
                    pending.clear()
                    oversized = False
                    break
                if not oversized:
                    if len(pending) + len(fragment) > MAX_CHECKPOINT_LINE_BYTES:
                        oversized = True
                        pending.clear()
                    else:
                        pending.extend(fragment)
                break
            fragment = chunk[start : newline + 1]
            if first_line:
                first_line = False
                pending.clear()
                oversized = False
                start = newline + 1
                continue
            if not oversized:
                if len(pending) + len(fragment) <= MAX_CHECKPOINT_LINE_BYTES:
                    pending.extend(fragment)
                    yield bytes(pending)
                pending.clear()
            oversized = False
            start = newline + 1

def _require_social_collect() -> None:
    """Opt-in gate for browser-automation social paths.

    Scroll-collection and logged-in comment reading are the exact patterns
    platforms can restrict. They stay available but
    default off; single-shot public fetch (social fetch) is unaffected.
    """
    import os

    if os.environ.get(SOCIAL_COLLECT_ENV, "") != "1":
        from sieve.config import get as _cfg
        if str(_cfg("enable_social_collect", "", env=None)).lower() not in ("1", "true", "yes"):
            raise RuntimeError(
                "browser social collection is disabled by default (platform ToS risk). "
                f"Set {SOCIAL_COLLECT_ENV}=1 to enable it."
            )


def resolve_browser_backend(explicit: str | None = None) -> str:
    """Pick the browser backend: pool (bundled Chromium) or sleeper (your browser).

    Explicit flag wins, then SIEVE_BROWSER_BACKEND, then auto: Sleeper daemon
    up means the operator runs their own browser (any brand) — use it.
    Otherwise fall back to the pooled Chromium.
    """
    import os

    from sieve.config import get as _cfg_get
    want = str(explicit or os.environ.get(BROWSER_BACKEND_ENV, "") or
               _cfg_get("browser_backend", "auto") or "auto").lower()
    if want in ("pool", "sleeper"):
        return want
    try:
        from .sleeper_bridge import is_available
        if is_available():
            return "sleeper"
    except Exception:
        pass
    return "pool"


def _sleeper_html(url: str, *, wait_selector: str | None = None,
                  scrolls: int = 0, scroll_delay: int = 0,
                  timeout: float | None = None) -> str:
    """Render url in the operator's own browser via Sleeper; return outerHTML."""
    from .sleeper_bridge import _command

    nav = _command("goto", {"url": url}, timeout=timeout)
    if not nav.get("ok"):
        raise RuntimeError(f"sleeper navigation failed: {nav.get('error', nav)}")
    _command("wait_url", {"pattern": url, "timeout_ms": 30000}, timeout=timeout)
    if wait_selector:
        _command("waitFor", {"selector": wait_selector, "timeout": 15000}, timeout=timeout)
    for _ in range(max(0, min(int(scrolls), 100))):
        _command("exec", {"code": "window.scrollTo(0, document.body.scrollHeight)"}, timeout=timeout)
        if scroll_delay:
            import time
            time.sleep(max(0, min(int(scroll_delay), 10_000)) / 1000)
    out = _command("exec", {"code": "document.documentElement.outerHTML"}, timeout=timeout)
    html = (out.get("result", "") or "") if out.get("ok") else ""
    if not str(html).strip():
        raise RuntimeError("sleeper returned empty render — shell, login wall, or block")
    return str(html)

ITEM_PATTERNS = {
    # Instagram emits both /reel/ID and /username/reel/ID forms in rendered
    # profile grids. Accept either, but keep the item-type and ID boundary
    # strict so navigation/profile URLs are not misclassified as posts.
    "instagram": re.compile(r"https://(?:www\.)?instagram\.com/(?:[^\"'<>\s]+/)?(?:reel|p|tv)/[^\"'<>\s]+"),
    "tiktok": re.compile(r"https://(?:www\.)?tiktok\.com/@[^\"'<>\s]+/video/\d+"),
}

_SHORTCODE_RE = re.compile(r"(?:instagram\.com/[^\s\"'<]*/?(?:reel|p|tv)/|\b)([A-Za-z0-9_-]{5,})")


def _instagram_caption(value: Any) -> str | None:
    """Convert the caption shapes used by Instagram's public payloads."""
    if isinstance(value, str):
        return value.strip() or None
    if isinstance(value, dict):
        return _instagram_caption(value.get("text") or value.get("caption"))
    return None


def _instagram_payloads(value: Any) -> list[dict[str, Any]]:
    """Walk bounded JSON payloads and return post-like dictionaries."""
    found: list[dict[str, Any]] = []
    def walk(node: Any) -> None:
        if len(found) >= 2000:
            return
        if isinstance(node, dict):
            if node.get("shortcode") or node.get("code"):
                found.append(node)
            for child in node.values():
                walk(child)
        elif isinstance(node, list):
            for child in node:
                walk(child)
    walk(value)
    return found


def _instagram_payload_index(html: str) -> dict[str, dict[str, Any]]:
    from bs4 import BeautifulSoup
    index: dict[str, dict[str, Any]] = {}
    soup = BeautifulSoup(html[:8_000_000], "html.parser")
    for script in soup.find_all("script"):
        raw = script.string or script.get_text()
        if not raw or not ("shortcode" in raw or '"code"' in raw):
            continue
        try:
            payload = json.loads(raw)
        except (TypeError, ValueError):
            continue
        for item in _instagram_payloads(payload):
            code = str(item.get("shortcode") or item.get("code") or "").strip()
            if code:
                index.setdefault(code, item)
    return index


def _payload_metric(payload: dict[str, Any], *names: str) -> Any:
    for name in names:
        if payload.get(name) is not None:
            return payload[name]
    # Older GraphQL responses wrap counts as {count: N} under edge_* keys.
    for key, value in payload.items():
        if any(name in str(key) for name in names) and isinstance(value, dict):
            if value.get("count") is not None:
                return value["count"]
    return None


def extract_profile_corpus(platform: str, html: str, *, max_posts: int = 100,
                           base_url: str | None = None) -> list[dict[str, Any]]:
    """Extract stable post records from rendered profile HTML/XHR fragments.

    Only values present in the page payload are reported. Shares and saves are
    deliberately null because Instagram does not expose them publicly.
    """
    items = extract_profile_items(platform, html, max_posts, base_url)
    if platform != "instagram":
        return items
    payloads = _instagram_payload_index(html)
    # XHR payloads can contain the grid without rendering anchors at all.
    # Build canonical post URLs from their stable shortcode in that case.
    known = {re.search(r"/(?:reel|p|tv)/([^/?#]+)", i["url"]).group(1)
             for i in items if re.search(r"/(?:reel|p|tv)/([^/?#]+)", i["url"])}
    host = "https://www.instagram.com"
    for code, payload in payloads.items():
        if code in known or len(items) >= max(1, min(int(max_posts), 500)):
            continue
        kind = "reel" if payload.get("media_type") == 2 or payload.get("is_video") else "p"
        items.append({"url": f"{host}/{kind}/{code}/", "visible_metric": "",
                      "caption": None, "cover_url": payload.get("display_url")})
    output: list[dict[str, Any]] = []
    for item in items[:max(1, min(int(max_posts), 500))]:
        url = item["url"]
        match = re.search(r"/(?:reel|p|tv)/([^/?#]+)", url)
        code = match.group(1) if match else None
        payload = payloads.get(code or "", {})
        caption = _instagram_caption(payload.get("caption")) or item.get("caption")
        views = _payload_metric(payload, "play_count", "video_view_count", "view_count")
        likes = _payload_metric(payload, "like_count", "likes", "like")
        comments = _payload_metric(payload, "comment_count", "comments", "comment")
        visible = item.get("visible_metric") or ""
        if views is None:
            match_views = re.search(r"([\d,.]+[KMB]?)\s*(?:views?|plays?)", visible, re.I)
            views = match_views.group(1) if match_views else None
        if likes is None:
            match_likes = re.search(r"([\d,.]+[KMB]?)\s*likes?", visible, re.I)
            likes = match_likes.group(1) if match_likes else None
        if comments is None:
            match_comments = re.search(r"([\d,.]+[KMB]?)\s*comments?", visible, re.I)
            comments = match_comments.group(1) if match_comments else None
        record = {"code": code, "shortcode": code, "url": url,
                  "caption": caption, "views": views,
                  "metrics": {"views": views, "likes": likes, "comments": comments,
                               "shares": None, "saves": None},
                  "cover_url": item.get("cover_url"),
                  "provenance": {"adapter": "sieve.social.instagram",
                                 "source_url": base_url or url,
                                 "evidence": "rendered_dom_or_captured_xhr"}}
        output.append(record)
    return output


def platform_for_url(url: str) -> str:
    try:
        parsed = urlparse(url)
        port = parsed.port
    except (TypeError, ValueError) as exc:
        raise ValueError("social URL must be a valid HTTPS URL") from exc
    if parsed.scheme != "https" or parsed.username or parsed.password or port not in (None, 443):
        raise ValueError("social URL must use HTTPS without credentials or a custom port")
    host = (parsed.hostname or "").lower()
    for platform, hosts in HOSTS.items():
        if host in hosts:
            return platform
    raise ValueError("supported social hosts are Instagram and TikTok")


def _safe_media_url(value: Any) -> str | None:
    """Allow only ordinary HTTPS media URLs from untrusted page metadata."""
    if not isinstance(value, str) or not value:
        return None
    value = html_lib.unescape(value).strip()
    parsed = urlparse(value)
    try:
        port = parsed.port
    except ValueError:
        return None
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or port not in (None, 443):
        return None
    return value


def normalize(platform: str, url: str, html: str, *, adapter: str = "sieve.social", max_media: int = 20) -> dict[str, Any]:
    platform = platform.lower().strip()
    if platform not in HOSTS or platform_for_url(url) != platform:
        raise ValueError("url does not match the selected social platform")
    meta = extract_metadata(html[:2_000_000], url)
    candidate = meta.get("canonical") or url
    try:
        canonical = candidate if platform_for_url(candidate) == platform else url
    except (TypeError, ValueError):
        canonical = url
    path_id = urlparse(canonical).path.strip("/")
    stable_id = hashlib.sha256(canonical.encode()).hexdigest()[:20]
    title = str(meta.get("title") or "").strip().lower()
    generic_shell_titles = {"instagram", "tiktok", "log in | instagram", "log in | tiktok"}
    has_signal = any(meta.get(key) for key in ("description", "author", "image", "published_time"))
    if not has_signal and title and title not in generic_shell_titles:
        has_signal = True
    media_url = _safe_media_url(meta.get("image"))
    return {
        "ok": has_signal,
        "collection_status": "ok" if has_signal else "empty_or_blocked",
        "id": path_id or stable_id,
        "platform": platform,
        "creator": meta.get("author"),
        "url": canonical,
        "caption": meta.get("description") or meta.get("title"),
        "published_at": meta.get("published_time"),
        "duration_seconds": None,
        "metrics": {},
        "media_urls": [media_url] if media_url and max_media else [],
        "provenance": {"adapter": adapter, "source_url": url, "collected_at": datetime.now(UTC).isoformat()},
        "raw_payload": {key: value for key, value in meta.items() if key in {"title", "description", "image", "author", "canonical", "published_time"}},
    }


def _bounded_get_text(url: str, *, timeout: int, platform: str | None = None,
                      allow_jina_redirect: bool = False) -> str:
    """Read a social response with bounded, same-owner redirect handling.

    The httpx timeout is a 5-tuple so connect/read/write/pool are each bounded
    (issue #469): a default float timeout can leave the connect phase waiting
    on a denied-network sandbox until an outer kill signal arrives.
    """
    current = url
    bounded = max(1, min(int(timeout), 120))
    for _ in range(5):
        with httpx.stream(
            "GET", current, follow_redirects=False,
            timeout=httpx.Timeout(bounded, connect=min(bounded, 10)),
            headers={"Accept": "text/html, text/plain", "User-Agent": "Sieve/1"},
        ) as response:
            if 300 <= response.status_code < 400:
                location = response.headers.get("location")
                if not location:
                    raise RuntimeError("social fetch returned a redirect without a location")
                target = urljoin(current, location)
                try:
                    if platform is not None:
                        if platform_for_url(target) != platform:
                            raise ValueError
                    elif not allow_jina_redirect:
                        raise ValueError
                    else:
                        parsed = urlparse(target)
                        if (parsed.scheme != "https" or parsed.hostname != "r.jina.ai"
                                or parsed.username or parsed.password or parsed.port not in (None, 443)):
                            raise ValueError
                except (TypeError, ValueError):
                    raise RuntimeError("social fetch redirect left the permitted host") from None
                current = target
                continue
            if response.status_code >= 400:
                raise RuntimeError(f"social fetch failed with HTTP {response.status_code}")
            body = bytearray()
            for chunk in response.iter_bytes():
                if len(body) + len(chunk) > MAX_RESPONSE_BYTES:
                    raise RuntimeError("social response exceeded the safety limit")
                body.extend(chunk)
            return bytes(body).decode(response.encoding or "utf-8", "replace")
    raise RuntimeError("social fetch exceeded the redirect limit")


def fetch(url: str, *, reader: str = "sieve", timeout: int = 30, max_media: int = 20) -> dict[str, Any]:
    """Fetch one public social URL and return a normalized content record."""
    platform = platform_for_url(url)
    if reader not in {"sieve", "jina"}:
        raise ValueError("reader must be 'sieve' or 'jina'")
    transport_url = url if reader == "sieve" else "https://r.jina.ai/" + url
    return normalize(platform, url, _bounded_get_text(
        transport_url, timeout=timeout, platform=platform if reader == "sieve" else None,
        allow_jina_redirect=reader == "jina",
    ),
                     adapter="jina_reader" if reader == "jina" else "sieve.social",
                     max_media=max_media)


def fetch_browser(url: str, *, cdp_url: str | None = None, real_chrome: bool = False,
                  wait: int = 1500, wait_selector: str | None = None,
                  network_idle: bool = False, timeout: int = 45,
                  max_media: int = 20, backend: str | None = None,
                  max_posts: int = 0, checkpoint: str | None = None,
                  max_scrolls: int = 20) -> dict[str, Any]:
    """Fetch through a browser seam: pooled Chromium or the operator's own
    browser via Sleeper (any brand). Backend: explicit > SIEVE_BROWSER_BACKEND > auto."""
    platform = platform_for_url(url)
    if max_posts < 0:
        raise ValueError("max_posts must be non-negative")
    if max_posts:
        return collect_browser(url, cdp_url=cdp_url, real_chrome=real_chrome,
                               scrolls=max_scrolls, timeout=timeout,
                               max_items=max_posts, checkpoint=checkpoint,
                               backend=backend)
    if resolve_browser_backend(backend) == "sleeper":
        html = _sleeper_html(url, wait_selector=wait_selector, timeout=timeout)
        record = normalize(platform, url, html, adapter="sieve.sleeper", max_media=max_media)
        record["provenance"]["fetcher_used"] = "sleeper"
        return record
    import asyncio

    from .server import MasterFetchServer

    platform = platform_for_url(url)
    server = MasterFetchServer()

    async def run() -> dict[str, Any]:
        try:
            response = await server.stealthy_fetch(
                url, extraction_type="html", main_content_only=False,
                use_trafilatura=False, real_chrome=real_chrome,
                cdp_url=cdp_url, wait=max(0, min(int(wait), 30_000)),
                wait_selector=wait_selector, network_idle=network_idle,
                timeout=max(1, min(int(timeout), 120)) * 1000,
            )
            html = "\n".join(response.content or [])
            try:
                if platform_for_url(response.url) != platform:
                    raise ValueError
            except (TypeError, ValueError):
                raise RuntimeError("browser navigation left the permitted social host") from None
            record = normalize(platform, url, html, adapter="sieve.browser", max_media=max_media)
            record["provenance"]["browser_status"] = response.status
            record["provenance"]["fetcher_used"] = response.fetcher_used
            if response.error and record["ok"]:
                record["ok"] = False
                record["collection_status"] = "browser_error"
            return record
        finally:
            await server._shutdown_close_sessions()

    return asyncio.run(run())


def extract_profile_items(platform: str, html: str, max_items: int = 100,
                          base_url: str | None = None) -> list[dict[str, Any]]:
    """Extract visible profile-grid links from rendered HTML, without guessing metrics."""
    from bs4 import BeautifulSoup

    pattern = ITEM_PATTERNS.get(platform)
    if pattern is None:
        raise ValueError("profile collection supports Instagram and TikTok")
    source = html_lib.unescape(html[:8_000_000]).replace("\\/", "/")
    soup = BeautifulSoup(source, "html.parser")
    found: list[dict[str, Any]] = []
    seen: set[str] = set()
    for anchor in soup.find_all("a", href=True):
        raw_url = html_lib.unescape(str(anchor.get("href")))
        if base_url:
            from urllib.parse import urljoin
            raw_url = urljoin(base_url, raw_url)
        match = pattern.search(raw_url)
        if not match:
            continue
        item_url = match.group(0).rstrip(".,;:)")
        if item_url in seen:
            continue
        seen.add(item_url)
        image = anchor.find("img")
        found.append({
            "url": item_url,
            "visible_metric": " ".join(anchor.get_text(" ", strip=True).split()),
            "caption": image.get("alt") if image else None,
            "cover_url": (image.get("src") or image.get("data-src")) if image else None,
        })
        if len(found) >= max(1, min(int(max_items), 500)):
            break
    # Some rendered pages put links in scripts rather than anchors.
    if len(found) < max_items:
        for raw_url in pattern.findall(source):
            item_url = html_lib.unescape(raw_url).rstrip(".,;:)")
            if item_url not in seen:
                seen.add(item_url)
                found.append({"url": item_url, "visible_metric": "", "caption": None, "cover_url": None})
            if len(found) >= max(1, min(int(max_items), 500)):
                break
    return found


def collect_browser(url: str, *, creator: str | None = None, cdp_url: str | None = None,
                    real_chrome: bool = False, scrolls: int = 20,
                    scroll_delay: int = 1200, timeout: int = 60,
                    max_items: int = 100, backend: str | None = None,
                    checkpoint: str | None = None) -> dict[str, Any]:
    """Collect a bounded, paced profile grid with optional retention filtering."""
    if int(scrolls) < 0 or int(scrolls) > 100:
        raise ValueError("scrolls must be between 0 and 100")
    if int(scroll_delay) < 0 or int(scroll_delay) > 10_000:
        raise ValueError("scroll_delay must be between 0 and 10000 milliseconds")
    if int(timeout) <= 0 or int(timeout) > 120:
        raise ValueError("timeout must be between 1 and 120 seconds")
    if int(max_items) <= 0 or int(max_items) > 500:
        raise ValueError("max_items must be between 1 and 500")
    _require_social_collect()
    platform = platform_for_url(url)
    if resolve_browser_backend(backend) == "sleeper":
        html = _sleeper_html(url, scrolls=scrolls, scroll_delay=scroll_delay, timeout=timeout)
        items = extract_profile_corpus(platform, html, max_posts=max_items, base_url=url)
        retained = [item for item in items
                    if platform != "instagram" or not _transcript_exists(item.get("code"))]
        skipped_transcripts = len(items) - len(retained)
        items = retained
        _checkpoint_items(items, checkpoint)
        return {"ok": bool(items), "collection_status": "ok" if items else "empty_or_blocked",
                "platform": platform, "creator": creator, "profile_url": url,
                "captured_at": datetime.now(UTC).isoformat(), "items": items,
                "provenance": {"adapter": "sieve.sleeper", "source_url": url,
                               "fetcher_used": "sleeper",
                               "skipped_transcripts": skipped_transcripts,
                               "checkpoint": checkpoint}}
    import asyncio

    from .server import MasterFetchServer

    server = MasterFetchServer()

    async def run() -> dict[str, Any]:
        try:
            actions = [{"scroll": max(0, min(int(scrolls), 100))},
                       {"wait": max(0, min(int(scroll_delay), 10_000))}]
            response = await server.stealthy_fetch(
                url, extraction_type="html", main_content_only=False,
                use_trafilatura=False, real_chrome=real_chrome, cdp_url=cdp_url,
                timeout=max(1, min(int(timeout), 120)) * 1000,
                page_action=__import__("sieve.actions", fromlist=["build_page_action"]).build_page_action(actions),
                capture_xhr=True,
            )
            try:
                if platform_for_url(response.url) != platform:
                    raise ValueError
            except (TypeError, ValueError):
                raise RuntimeError("browser navigation left the permitted social host") from None
            items = extract_profile_items(platform, "\n".join(response.content or []), max_items, url)
            rendered = "\n".join(response.content or [])
            # Captured XHR fragments are first-class input: Instagram often
            # leaves only a shell in the DOM while the grid arrives as JSON.
            fragments = (getattr(response, "network", None) or {}).get("fragments", [])
            for fragment in fragments:
                if isinstance(fragment, dict) and fragment.get("text"):
                    rendered += "\n" + str(fragment["text"])
            items = extract_profile_corpus(platform, rendered, max_posts=max_items, base_url=url)
            retained = [item for item in items
                        if platform != "instagram" or not _transcript_exists(item.get("code"))]
            skipped_transcripts = len(items) - len(retained)
            items = retained
            _checkpoint_items(items, checkpoint)
            status = "rate_limited" if response.status in (401, 429) else ("ok" if items else "empty_or_blocked")
            failure = [] if items else ([f"browser HTTP {response.status}"] if response.status in (401, 429)
                                        else ["no post payloads exposed"])
            return {"ok": bool(items), "collection_status": status,
                    "platform": platform, "creator": creator, "profile_url": response.url,
                    "captured_at": datetime.now(UTC).isoformat(), "items": items,
                "provenance": {"adapter": "sieve.browser", "source_url": url,
                               "browser_status": response.status, "fetcher_used": response.fetcher_used,
                               "partial_failures": failure, "skipped_transcripts": skipped_transcripts,
                               "checkpoint": checkpoint}}
        finally:
            await server._shutdown_close_sessions()

    return asyncio.run(run())


_COMMENT_UI = re.compile(r"^(Reply|View all \d+ replies|[\d,]+ likes|This reel has .*|More posts from .*|See translation)$")


def parse_browser_comments(text: str, max_comments: int) -> list[dict[str, Any]]:
    """Pick comment texts out of rendered post markdown."""
    items: list[dict[str, Any]] = []
    pending_likes: int | None = None
    for line in (text or "").splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        like_match = re.fullmatch(r"([\d,]+) likes", stripped)
        if like_match:
            pending_likes = int(like_match.group(1).replace(",", ""))
            continue
        if _COMMENT_UI.match(stripped):
            continue
        items.append({"author": None, "text": stripped, "likes": pending_likes, "timestamp": None})
        pending_likes = None
        if len(items) >= max(1, min(int(max_comments), 500)):
            break
    return items


def comments_browser(url: str, *, max_comments: int = 50, timeout: int = 120) -> dict[str, Any]:
    """Read comments through the pool browser (needs a logged-in profile)."""
    _require_social_collect()
    import asyncio

    from . import actions
    from .server import MasterFetchServer

    platform = platform_for_url(url)
    server = MasterFetchServer()

    async def run() -> str:
        try:
            action = actions.build_page_action([{"scroll": 10}, {"wait": 3000}])
            response = await server.stealthy_fetch(
                url, extraction_type="markdown", main_content_only=False,
                use_trafilatura=False, timeout=max(1, min(int(timeout), 300)) * 1000,
                page_action=action,
            )
            return "\n".join(response.content or [])
        finally:
            await server._shutdown_close_sessions()

    items = parse_browser_comments(asyncio.run(run()), max_comments)
    return {"ok": bool(items), "platform": platform, "url": url,
            "count": len(items), "comments": items,
            "provenance": {"adapter": "sieve.comments.browser", "source_url": url,
                           "captured_at": datetime.now(UTC).isoformat()}}


def comments(url: str, *, max_comments: int = 50, timeout: int = 90, reader: str = "ytdlp") -> dict[str, Any]:
    """Read post comments. yt-dlp first; reader="browser" uses a logged-in pool session."""
    if reader == "browser":
        return comments_browser(url, max_comments=max_comments, timeout=timeout)
    if reader != "ytdlp":
        raise ValueError("reader must be 'ytdlp' or 'browser'")
    platform = platform_for_url(url)
    if shutil.which(_ytdlp_binary()) is None:
        raise RuntimeError("yt-dlp is not installed for comment extraction")
    with tempfile.TemporaryDirectory(prefix="sieve-comments-") as tmpdir:
        _ytdlp_run([*_ytdlp_runtime_args(), "--skip-download", "--write-comments",
                    "--no-playlist", "-o", str(Path(tmpdir) / "post.%(ext)s"),
                    "--", url], timeout)
        info_files = sorted(Path(tmpdir).glob("*.info.json"))
        if not info_files:
            raise RuntimeError("yt-dlp returned no metadata for comments")
        info_path = info_files[0]
        if info_path.stat().st_size > 10 * 1024 * 1024:
            raise RuntimeError("yt-dlp metadata exceeds the safety limit")
        info = json.loads(info_path.read_text(encoding="utf-8", errors="replace"))
    raw = info.get("comments") or []
    items = []
    for comment in raw[:max(1, min(int(max_comments), 500))]:
        if not isinstance(comment, dict):
            continue
        author = comment.get("author")
        if isinstance(author, dict):
            author = author.get("name") or author.get("id")
        items.append({
            "author": author,
            "text": comment.get("text"),
            "likes": comment.get("like_count"),
            "timestamp": comment.get("timestamp"),
        })
    return {"ok": bool(items), "platform": platform, "url": url,
            "count": len(items), "comments": items,
            "provenance": {"adapter": "sieve.comments", "source_url": url,
                           "captured_at": datetime.now(UTC).isoformat()}}
