"""Explicit, bounded adapters for separately installed OSINT tools.

Maigret is deliberately invoked as a subprocess: it is optional, changing
quickly, and must never become a Sieve dependency or inherit Sieve secrets.
"""
from __future__ import annotations

import json
import math
import os
import re
import selectors
import signal
import shutil
import subprocess
import time
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse

MAIGRET_SOURCE_URL = "https://github.com/soxoj/maigret"
_VERSION_RE = re.compile(r"(?:maigret\s+)?v?(\d+\.\d+(?:\.\d+)*)", re.I)
_SAFE_STATUS = {"claimed", "available", "unknown", "error", "declared"}


class OSINTError(RuntimeError):
    """A safe, categorised adapter failure (never includes the username)."""

    def __init__(self, message: str, category: str = "osint") -> None:
        super().__init__(message)
        self.category = category


def _clean_text(value: Any, limit: int = 300) -> str:
    return str(value).replace("\x00", "")[:limit]


def _record(item: dict[str, Any], *, username: str, upstream_version: str,
            observed_at: str, include_username: bool) -> dict[str, Any] | None:
    site = _clean_text(item.get("site") or item.get("site_name") or item.get("name"), 120).strip()
    url = _clean_text(item.get("url") or item.get("site_url") or item.get("link"), 2048).strip()
    parsed = urlparse(url)
    if not site or parsed.scheme != "https" or not parsed.netloc:
        return None
    raw_status = _clean_text(item.get("status") or item.get("status_code") or "unknown", 40).lower()
    status = raw_status if raw_status in _SAFE_STATUS else "unknown"
    score = item.get("score")
    try:
        confidence = max(0.0, min(1.0, float(score))) if score is not None else (1.0 if status == "claimed" else 0.0)
    except (TypeError, ValueError):
        confidence = 0.0
    fields: dict[str, str] = {}
    for key in ("name", "country", "gender", "language", "bio"):
        if item.get(key) is not None:
            fields[key] = _clean_text(item[key])
    return {
        "kind": "identity_account_match",
        "query": {"type": "username", "value": username if include_username else "<redacted>"},
        "site": site,
        "url": url,
        "status": status,
        "confidence": confidence,
        "observed_fields": fields,
        "provenance": {
            "adapter": "maigret",
            "adapter_version": "0.1",
            "upstream_version": upstream_version,
            "source_url": MAIGRET_SOURCE_URL,
            "observed_at": observed_at,
        },
    }


def _version(executable: str, timeout: float) -> str:
    try:
        completed = subprocess.run([executable, "--version"], capture_output=True,
                                   timeout=min(5.0, timeout), check=False,
                                   env={"PATH": os.environ.get("PATH", "")})
        text = (completed.stdout + completed.stderr).decode("utf-8", "replace")[:1000]
        match = _VERSION_RE.search(text)
        return match.group(1) if match else "unknown"
    except (OSError, subprocess.TimeoutExpired):
        return "unknown"


def _terminate(process: subprocess.Popen[bytes]) -> None:
    """Stop the process and descendants without exposing a shell surface."""
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except (AttributeError, OSError):
        process.kill()


def run_maigret(username: str, *, allow_osint: bool = False,
                executable: str = "maigret", timeout: float = 60.0,
                max_output_bytes: int = 1_000_000, max_sites: int = 500,
                tags: str | None = None, include_username: bool = False) -> dict[str, Any]:
    """Run Maigret's username-only NDJSON mode under explicit bounded policy."""
    if not allow_osint:
        raise OSINTError("Maigret requires explicit --allow-osint opt-in", "consent_required")
    if not isinstance(username, str) or not re.fullmatch(r"[A-Za-z0-9._-]{1,80}", username):
        raise OSINTError("username must contain only letters, digits, dot, underscore, or hyphen", "input")
    if isinstance(timeout, bool):
        raise OSINTError("timeout must be a finite number between 1 and 180 seconds", "input")
    try:
        timeout_value = float(timeout)
    except (TypeError, ValueError):
        raise OSINTError("timeout must be a finite number between 1 and 180 seconds", "input") from None
    if not math.isfinite(timeout_value) or not 1 <= timeout_value <= 180:
        raise OSINTError("timeout must be a finite number between 1 and 180 seconds", "input")
    if isinstance(max_output_bytes, bool) or not isinstance(max_output_bytes, int) or not 1_000 <= max_output_bytes <= 10_000_000:
        raise OSINTError("max_output_bytes must be between 1000 and 10000000", "input")
    if isinstance(max_sites, bool) or not isinstance(max_sites, int) or not 1 <= max_sites <= 5000:
        raise OSINTError("max_sites must be between 1 and 5000", "input")
    resolved = shutil.which(executable) if os.path.basename(executable) == executable else executable
    if not resolved or not os.path.isfile(resolved) or not os.access(resolved, os.X_OK):
        raise OSINTError("Maigret executable was not found; install it separately", "missing_executable")
    upstream_version = _version(resolved, float(timeout))
    observed_at = datetime.now(timezone.utc).isoformat()
    command = [resolved, username, "--json", "ndjson", "--timeout", str(min(60, int(timeout))),
               "--max-connections", str(min(max_sites, 100))]
    if tags:
        if not re.fullmatch(r"[A-Za-z0-9_, -]{1,200}", tags):
            raise OSINTError("tags contain unsupported characters", "input")
        command += ["--tags", tags]
    started = time.monotonic()
    try:
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   stdin=subprocess.DEVNULL, env={"PATH": os.path.dirname(resolved) + os.pathsep + os.environ.get("PATH", "")},
                                   start_new_session=True)
        selector = selectors.DefaultSelector()
        for stream in (process.stdout, process.stderr):
            if stream is None:
                process.kill()
                raise RuntimeError("Maigret subprocess did not expose output pipes")
            os.set_blocking(stream.fileno(), False)
            selector.register(stream, selectors.EVENT_READ)
        output = {process.stdout: bytearray(), process.stderr: bytearray()}
        overflow = False
        deadline = time.monotonic() + float(timeout)
        try:
            while selector.get_map():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    _terminate(process)
                    process.wait()
                    raise OSINTError("Maigret timed out", "timeout")
                for key, _ in selector.select(min(remaining, 0.1)):
                    chunk = os.read(key.fd, max_output_bytes + 1)
                    if not chunk:
                        selector.unregister(key.fileobj)
                        continue
                    buffer = output[key.fileobj]
                    if len(buffer) + len(chunk) > max_output_bytes:
                        overflow = True
                        _terminate(process)
                        process.wait()
                        break
                    buffer.extend(chunk)
                if overflow:
                    break
            process.wait(timeout=max(0.1, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            _terminate(process)
            process.wait()
            raise OSINTError("Maigret timed out", "timeout")
        finally:
            selector.close()
        stdout, stderr = bytes(output[process.stdout]), bytes(output[process.stderr])
        if overflow:
            raise OSINTError("Maigret output exceeded the configured byte limit", "output_limit")
    except OSINTError:
        raise
    except OSError:
        raise OSINTError("Maigret could not be started", "execution")
    if len(stdout) > max_output_bytes or len(stderr) > max_output_bytes:
        raise OSINTError("Maigret output exceeded the configured byte limit", "output_limit")
    if process.returncode:
        raise OSINTError("Maigret exited with a nonzero status", "upstream_failure")
    results: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    try:
        for line in stdout.splitlines():
            if not line.strip():
                continue
            item = json.loads(line)
            if not isinstance(item, dict):
                raise ValueError("record is not an object")
            normalized = _record(item, username=username, upstream_version=upstream_version,
                                observed_at=observed_at, include_username=include_username)
            if normalized is None:
                continue
            key = (normalized["site"], normalized["url"])
            if key not in seen:
                seen.add(key)
                results.append(normalized)
            if len(results) >= max_sites:
                break
    except (json.JSONDecodeError, ValueError, UnicodeError):
        raise OSINTError("Maigret returned malformed NDJSON", "malformed_output")
    return {"ok": True, "adapter": "maigret", "query": {"type": "username", "value": username if include_username else "<redacted>"},
            "results": results, "provenance": {"adapter": "maigret", "adapter_version": "0.1",
            "upstream_version": upstream_version, "source_url": MAIGRET_SOURCE_URL,
            "observed_at": observed_at, "duration_ms": round((time.monotonic() - started) * 1000, 1)}}


__all__ = ["OSINTError", "run_maigret"]
