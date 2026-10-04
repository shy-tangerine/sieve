"""Small, bounded adapter around the external ``yt-dlp`` executable.

The adapter deliberately returns the subset of fields consumed by downstream
downstream normalizers.  It does not vendor yt-dlp or attempt to bypass
account, CAPTCHA, or platform access controls.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import threading
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from sieve.security import SecurityError, validate_url

MAX_RESULTS = 25
MAX_OUTPUT_BYTES = 8 * 1024 * 1024
DEFAULT_TIMEOUT = 45
# Cookie jars above this reliably earn HTTP 413 from YouTube (header too
# large). Observed: a 172 KiB jar always 413s; essentials fit in ~2 KiB.
MAX_COOKIE_JAR_BYTES = 16 * 1024

AUTH_BLOCK_HINT = (
    "YouTube is asking for sign-in (bot check). Remedies: export fresh cookies "
    "from a logged-in browser (SIEVE_COOKIES_FROM_BROWSER or "
    "SIEVE_YTDLP_COOKIES_FILE, trimmed — oversized jars are rejected with "
    "HTTP 413), set SIEVE_YTDLP_PLAYER_CLIENT / SIEVE_YTDLP_PO_TOKEN, or fetch "
    "the rendered page instead (sleeper-fetch with a wait_selector)."
)


class YouTubeFetchError(RuntimeError):
    def __init__(self, message: str, *, category: str = "yt_dlp_or_source") -> None:
        super().__init__(message)
        self.category = category


def classify_error(text: str) -> str:
    value = text.lower()
    if "429" in value or "too many requests" in value or "rate limit" in value:
        return "rate_limited"
    if "sign in to confirm" in value or "authentication" in value or "login" in value:
        return "authentication_or_block"
    if any(term in value for term in ("timed out", "timeout", "connection reset", "network")):
        return "network"
    return "yt_dlp_or_source"


def _binary() -> str:
    configured = os.environ.get("SIEVE_YTDLP", "yt-dlp").strip() or "yt-dlp"
    if configured == "yt-dlp":
        return configured
    path = Path(configured).expanduser()
    if path.is_symlink():
        raise YouTubeFetchError("SIEVE_YTDLP must not be a symlink", category="configuration")
    if not path.is_absolute() or not path.is_file() or not os.access(path, os.X_OK):
        raise YouTubeFetchError("SIEVE_YTDLP must be an executable absolute path", category="configuration")
    return str(path)


def _sieve_browser_cookie_source() -> str | None:
    """Return Sieve's persistent Chromium profile for child resolvers.

    The browser must be fully closed before a resolver reads its SQLite cookie
    store.  An explicit active marker lets browser owners fail closed rather
    than racing the profile; no cookie values are ever read by Sieve.
    """
    profile = os.environ.get("SIEVE_BROWSER_PROFILE_DIR", "").strip()
    if not profile:
        return None
    if os.environ.get("SIEVE_BROWSER_PROFILE_ACTIVE", "").strip().lower() in {"1", "true", "yes"}:
        raise YouTubeFetchError(
            "SIEVE_BROWSER_PROFILE_DIR is still in use; close Sieve's browser before media resolution",
            category="configuration",
        )
    path = Path(profile).expanduser()
    if not path.is_dir():
        raise YouTubeFetchError("SIEVE_BROWSER_PROFILE_DIR is not a readable directory", category="configuration")
    return f"chromium:{path}"


def _runtime_args() -> list[str]:
    args: list[str] = []
    if os.environ.get("SIEVE_YTDLP_FORCE_IPV4", "").lower() in {"1", "true", "yes"}:
        args.append("--force-ipv4")
    cookies = os.environ.get("SIEVE_YTDLP_COOKIES_FILE", "").strip()
    if cookies:
        path = Path(cookies).expanduser()
        if not path.is_file():
            raise YouTubeFetchError("SIEVE_YTDLP_COOKIES_FILE is not a readable file", category="configuration")
        try:
            size = path.stat().st_size
        except OSError:
            raise YouTubeFetchError("SIEVE_YTDLP_COOKIES_FILE is not a readable file", category="configuration")
        if size > MAX_COOKIE_JAR_BYTES:
            raise YouTubeFetchError(
                f"SIEVE_YTDLP_COOKIES_FILE is {size} bytes; YouTube rejects oversized "
                f"cookie headers with HTTP 413 — trim to essential cookies "
                f"(under {MAX_COOKIE_JAR_BYTES} bytes) or fetch the rendered page "
                f"instead (sleeper-fetch)",
                category="configuration",
            )
        args.extend(["--cookies", str(path)])
    else:
        from_browser = os.environ.get("SIEVE_COOKIES_FROM_BROWSER", "").strip()
        if from_browser:
            args.extend(["--cookies-from-browser", from_browser])
        else:
            profile_source = _sieve_browser_cookie_source()
            if profile_source:
                args.extend(["--cookies-from-browser", profile_source])
    client = os.environ.get("SIEVE_YTDLP_PLAYER_CLIENT", "").strip()
    if client:
        args.extend(["--extractor-args", f"youtube:player_client={client}"])
    po_token = os.environ.get("SIEVE_YTDLP_PO_TOKEN", "").strip()
    if po_token:
        # Pass the token through a bounded config file, not argv (issue #9):
        # process arguments are observable by other users via procfs and
        # appear in crash/support output. yt-dlp supports --config-location,
        # and the file is created with owner-only permissions and removed
        # immediately after the run. Never include the token in diagnostics.
        config = _write_po_token_config(po_token)
        args.extend(["--config-location", str(config)])
    return args


def _write_po_token_config(po_token: str) -> Path:
    """Write a temporary yt-dlp config carrying the PO token (issue #9)."""
    import tempfile
    fd, name = tempfile.mkstemp(prefix="sieve-ytdlp-", suffix=".conf")
    path = Path(name)
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(f"--extractor-args youtube:po_token={po_token}\n")
        os.chmod(path, 0o600)
    except Exception:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass
        raise
    return path


def resolve_urls(url: str, timeout: int = DEFAULT_TIMEOUT) -> list[str]:
    """Resolve direct media URLs with the configured yt-dlp credentials."""
    url = _validate_media_url(url)
    if shutil.which(_binary()) is None:
        raise YouTubeFetchError("yt-dlp not installed", category="configuration")
    output = _run(["--print", "urls", "--no-playlist", "--", url], timeout)
    urls = [line.strip() for line in output.splitlines() if line.strip().startswith("http")]
    if not urls:
        raise YouTubeFetchError("yt-dlp resolved no media URLs", category="yt_dlp_or_source")
    return urls


def _validate_media_url(url: str) -> str:
    """Reject non-web, credential-bearing, malformed, and private media URLs."""
    value = str(url or "").strip()
    try:
        parsed = urlparse(value)
        parsed.port
    except ValueError as exc:
        raise ValueError("media URL must be a valid HTTP/HTTPS URL") from exc
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
    ):
        raise ValueError("media URL must be a valid HTTP/HTTPS URL")
    try:
        return validate_url(value)
    except SecurityError as exc:
        raise ValueError(str(exc)) from exc


def _run(args: list[str], timeout: int) -> str:
    runtime_args = _runtime_args()
    # Remove the temporary PO-token config file after the child exits (issue #9):
    # it carries the secret and must not linger on disk. It lives only for the
    # subprocess lifetime, matching the argv-exposure window it replaces.
    config_files = [Path(a) for i, a in enumerate(runtime_args) if i > 0 and runtime_args[i - 1] == "--config-location"]
    command = [_binary(), *runtime_args, *args]
    if shutil.which(command[0]) is None and not Path(command[0]).is_file():
        for cf in config_files:
            cf.unlink(missing_ok=True)
        raise YouTubeFetchError("yt-dlp is not installed or SIEVE_YTDLP is invalid", category="configuration")
    try:
        proc = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=False, shell=False)
        stdout, stderr, overflow = _communicate_bounded(proc, max(1, min(int(timeout), 300)))
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
        for cf in config_files:
            cf.unlink(missing_ok=True)
        raise YouTubeFetchError("yt-dlp timed out", category="network")
    except OSError as exc:
        raise YouTubeFetchError("Configured yt-dlp executable failed.", category="permission" if isinstance(exc, PermissionError) else "execution") from exc
    if overflow:
        raise YouTubeFetchError("yt-dlp output exceeded the safety limit", category="yt_dlp_or_source")
    stdout = stdout.decode("utf-8", "replace")
    stderr = stderr.decode("utf-8", "replace")
    try:
        if proc.returncode:
            detail = _redact_error((stderr or stdout).strip()[-2000:])
            category = classify_error(detail)
            if category == "authentication_or_block":
                detail = f"{detail} [{AUTH_BLOCK_HINT}]" if detail else AUTH_BLOCK_HINT
            raise YouTubeFetchError(detail or f"yt-dlp exited with status {proc.returncode}", category=category)
        return stdout
    finally:
        for cf in config_files:
            cf.unlink(missing_ok=True)


def _communicate_bounded(proc: subprocess.Popen[bytes], timeout: int) -> tuple[bytes, bytes, bool]:
    """Drain both child pipes concurrently while retaining only bounded output.

    Reading both streams concurrently prevents a verbose child from deadlocking
    on a full stderr pipe. Once the cap is reached, the reader continues draining
    but discards additional bytes so the parent never accumulates unbounded
    output in memory.
    """
    streams = (getattr(proc, "stdout", None), getattr(proc, "stderr", None))
    if not all(hasattr(stream, "read") for stream in streams):
        # Small compatibility path for test doubles and unusual Popen wrappers.
        stdout, stderr = proc.communicate(timeout=timeout)
        if isinstance(stdout, str):
            stdout = stdout.encode()
        if isinstance(stderr, str):
            stderr = stderr.encode()
        return stdout or b"", stderr or b"", len(stdout or b"") > MAX_OUTPUT_BYTES or len(stderr or b"") > MAX_OUTPUT_BYTES

    results: list[bytes] = [b"", b""]
    overflow = threading.Event()

    def drain(index: int, stream) -> None:
        retained = bytearray()
        while True:
            chunk = stream.read(64 * 1024)
            if not chunk:
                break
            if isinstance(chunk, str):
                chunk = chunk.encode()
            remaining = MAX_OUTPUT_BYTES - len(retained)
            if remaining > 0:
                retained.extend(chunk[:remaining])
            if len(chunk) > max(remaining, 0):
                overflow.set()
        results[index] = bytes(retained)

    readers = [threading.Thread(target=drain, args=(index, stream), daemon=True)
               for index, stream in enumerate(streams)]
    for reader in readers:
        reader.start()
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
        for reader in readers:
            reader.join()
        raise
    for reader in readers:
        reader.join()
    return results[0], results[1], overflow.is_set()


def _redact_error(value: str) -> str:
    """Keep operator secrets out of CLI diagnostics returned to callers.

    Covers every configured secret/sensitive path that can reach yt-dlp
    runtime argv or its environment (issue #109): PO tokens, cookie files,
    cookies-from-browser profile names, proxy credentials, and the configured
    executable path.
    """
    env_names = (
        "SIEVE_YTDLP_PO_TOKEN",
        "SIEVE_YTDLP_COOKIES_FILE",
        "SIEVE_YTDLP",
        "SIEVE_YTDLP_FORCE_IPV4",
        "SIEVE_YTDLP_PLAYER_CLIENT",
        "SIEVE_YTDLP_FORMAT",
        "SIEVE_COOKIES_FROM_BROWSER",
        "SIEVE_BROWSER_PROFILE_DIR",
        "SIEVE_SEARCH_PROXY",
    )
    for name in env_names:
        secret = os.environ.get(name, "").strip()
        if secret and len(secret) >= 4:
            value = value.replace(secret, "[redacted]")
    # Proxy URLs with embedded credentials: scheme://user:pass@host
    value = re.sub(
        r"(https?|socks[45]?h?)://[^/\s:@]+:[^/\s@]+@",
        r"\1://[redacted]@",
        value,
    )
    return value


def _entry(value: dict[str, Any]) -> dict[str, Any]:
    video_id = str(value.get("id") or "").strip()
    url = value.get("webpage_url") or value.get("original_url")
    if video_id and not url:
        url = f"https://www.youtube.com/watch?v={video_id}"
    return {key: value[key] for key in (
        "id", "title", "description", "uploader", "channel", "channel_id",
        "channel_follower_count", "timestamp", "upload_date", "duration",
        "view_count", "like_count", "comment_count", "thumbnail", "extractor",
    ) if key in value} | ({"webpage_url": str(url)} if url else {})


def search(query: str, max_results: int = 6, timeout: int = DEFAULT_TIMEOUT) -> dict[str, Any]:
    if isinstance(max_results, bool) or not isinstance(max_results, int):
        raise ValueError("max_results must be an integer")
    if isinstance(timeout, bool) or not isinstance(timeout, int) or not 1 <= timeout <= 300:
        raise ValueError("timeout must be an integer between 1 and 300")
    query = str(query or "").strip()
    if not query or len(query) > 300:
        raise ValueError("query must be 1-300 characters")
    count = max(1, min(int(max_results), MAX_RESULTS))
    raw = _run(["--flat-playlist", "--dump-json", "--no-playlist", "--skip-download", "--no-warnings",
                "--socket-timeout", "20", "--retries", "1", "--fragment-retries", "1",
                f"ytsearch{count}:{query}"], timeout)
    entries = []
    for line in raw.splitlines():
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict) and value.get("id"):
            entries.append(_entry(value))
    return {"ok": True, "query": query, "entries": entries}


def metadata(url: str, timeout: int = DEFAULT_TIMEOUT) -> dict[str, Any]:
    _validate_url(url)
    lines = _run(["--dump-single-json", "--no-playlist", "--skip-download", "--no-warnings",
                  "--socket-timeout", "20", "--retries", "1", "--fragment-retries", "1", url], timeout)
    try:
        value = json.loads(lines)
    except json.JSONDecodeError as exc:
        raise YouTubeFetchError("yt-dlp returned invalid JSON") from exc
    if not isinstance(value, dict) or not value.get("id"):
        raise YouTubeFetchError("yt-dlp returned no usable video")
    return {"ok": True, "entry": _entry(value)}


# Explicit safe boundary for media downloads (issue #10).
_MEDIA_DOWNLOAD_ROOT = Path.home() / ".sieve" / "downloads"


def _resolve_download_target(output_dir: str) -> Path:
    """Resolve the download target inside an explicit safe boundary (issue #10).

    Arbitrary paths would make the media command an unrestricted
    filesystem-write primitive on agent-facing surfaces. Downloads are
    confined to ~/.sieve/downloads (or a direct child of it): '..' components,
    symlink escapes and sensitive/system locations are rejected.
    """
    if not output_dir or "\x00" in output_dir:
        raise ValueError("output_dir must be provided")
    root = _MEDIA_DOWNLOAD_ROOT.expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    requested = Path(output_dir).expanduser()
    if not requested.is_absolute():
        requested = root / requested
    target = requested.resolve(strict=False)
    # Traversal check: the resolved path must stay inside the root.
    if target != root and root not in target.parents:
        raise ValueError(
            f"output_dir must stay inside {root} (media downloads are confined "
            f"to an explicit safe boundary; traversal outside it is rejected)"
        )
    if target.exists() and not target.is_dir():
        raise ValueError("output_dir must be a directory")
    target.mkdir(parents=True, exist_ok=True)
    return target


def download(url: str, output_dir: str, timeout: int = 120, format_name: str | None = None) -> dict[str, Any]:
    _validate_url(url)
    target = _resolve_download_target(output_dir)
    args = ["--no-playlist", "--no-progress", "--no-warnings", "--socket-timeout", "20",
            "--retries", "1", "--fragment-retries", "1", "--merge-output-format", "mp4",
            "--print", "after_move:filepath", "-o", str(target / "video-%(id)s.%(ext)s")]
    chosen = format_name or os.environ.get("SIEVE_YTDLP_FORMAT", "").strip()
    if chosen:
        args.extend(["-f", chosen])
    output = _run([*args, url], timeout)
    files = []
    for line in output.splitlines():
        candidate = line.strip()
        if not candidate:
            continue
        path = Path(candidate).expanduser().resolve()
        try:
            path.relative_to(target)
        except ValueError:
            raise YouTubeFetchError("yt-dlp reported a file outside output_dir", category="security")
        if path.is_file():
            files.append(str(path))
    return {"ok": True, "url": url, "files": files, "output_dir": str(target)}


def _validate_url(url: str) -> None:
    parsed = urlparse(str(url or ""))
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("only canonical HTTPS YouTube URLs are supported") from exc
    if (
        parsed.scheme != "https"
        or parsed.username
        or parsed.password
        or port not in (None, 443)
        or parsed.hostname not in {"youtube.com", "www.youtube.com", "m.youtube.com", "youtu.be"}
    ):
        raise ValueError("only canonical HTTPS YouTube URLs are supported")
