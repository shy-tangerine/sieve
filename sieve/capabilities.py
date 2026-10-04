"""Safe discovery of capabilities already available on the operator's machine.

Discovery is advisory. Sieve never adopts a discovered executable or endpoint
until the operator explicitly confirms it during ``sieve setup``.
"""
from __future__ import annotations

import os
import shutil
from pathlib import Path
from urllib.parse import urlparse

_TRANSCRIBERS = ("whisper", "whisper-ctranslate2", "faster-whisper")
_BROWSERS = ("chromium", "chromium-browser", "google-chrome", "chrome", "firefox")


def _executable(name: str) -> dict[str, str] | None:
    found = shutil.which(name)
    if not found:
        return None
    path = Path(found).resolve()
    # which() can return a dangling or non-executable entry from a broken PATH.
    if not path.is_file() or not os.access(path, os.X_OK):
        return None
    return {"name": name, "path": str(path)}


def _safe_endpoint(value: str) -> str | None:
    parsed = urlparse(value.strip())
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return None
    if parsed.username or parsed.password:
        return None
    if parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
        return None
    return value.strip()


def detect() -> dict[str, object]:
    """Return local capability candidates without changing configuration."""
    transcription = [_executable(name) for name in _TRANSCRIBERS]
    browsers = [_executable(name) for name in _BROWSERS]
    result: dict[str, object] = {
        "transcription": [item for item in transcription if item],
        "browsers": [item for item in browsers if item],
        "yt_dlp": _executable("yt-dlp"),
        "cdp_endpoint": _safe_endpoint(os.environ.get("SIEVE_CDP_URL", "")),
    }
    return result

