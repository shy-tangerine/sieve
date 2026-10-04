"""Single behavior config: ~/.sieve/config.toml (stdlib tomllib, no new dep).

Precedence: explicit arg > SIEVE_* env > config.toml > builtin default.
Secrets stay OUT: API keys live in ~/.sieve/search_keys.json (managed by
`sieve keys`), proxies in ~/.sieve/search_proxies.json. This file holds
behavior only — backends, timeouts, flags. (YAML was considered for Hermes
parity; TOML parses with stdlib, so it wins.)
"""
from __future__ import annotations

import os
import json
import tomllib
from sieve.public_output import safe_diagnostic

CONFIG_PATH = os.path.join(os.path.expanduser("~"), ".sieve", "config.toml")

_CACHE: dict | None = None

# Typed diagnostics for configuration discovery (issue #233): a config file
# that exists but cannot be read/parsed must not silently behave like an
# absent one. Entries contain safe type/category/detail only, never paths or
# backend exception values.
#
# Types:
#   config_malformed   — file exists but is not valid TOML / not a table
#   config_unreadable  — file exists but could not be opened (permissions,
#                        IO error); distinct from absent so operators see
#                        why their settings are being ignored
_DIAGNOSTICS: list[dict] = []


def take_diagnostics() -> list[dict]:
    """Return and clear accumulated config diagnostics (drain semantics)."""
    global _DIAGNOSTICS
    out = _DIAGNOSTICS
    _DIAGNOSTICS = []
    return out


def peek_diagnostics() -> list[dict]:
    """Return accumulated config diagnostics without clearing them."""
    return list(_DIAGNOSTICS)


def load(path: str | None = None) -> dict:
    """Read the TOML config once per process.

    A missing file is normal (returns {}). A file that exists but cannot be
    parsed or opened records a typed diagnostic (issue #233) and still
    returns {} so callers never crash — but the diagnostic makes the
    silent-fallback observable via :func:`take_diagnostics`.
    """
    global _CACHE, _DIAGNOSTICS
    if _CACHE is not None and path is None:
        return _CACHE
    target = path or CONFIG_PATH
    try:
        with open(target, "rb") as f:
            data = tomllib.load(f)
    except FileNotFoundError:
        data = {}
    except OSError as exc:
        data = {}
        _DIAGNOSTICS.append({"type": "config_unreadable",
                             "detail": "Configuration could not be read.", "category": "configuration",
                             "diagnostic": safe_diagnostic(category="permission" if isinstance(exc, PermissionError) else "configuration", route="config")})
    except tomllib.TOMLDecodeError:
        data = {}
        _DIAGNOSTICS.append({"type": "config_malformed",
                             "detail": "Configuration could not be read.", "category": "configuration",
                             "diagnostic": safe_diagnostic(category="configuration", route="config")})
    if not isinstance(data, dict):
        _DIAGNOSTICS.append({"type": "config_malformed",
                             "detail": "Configuration could not be read.",
                             "category": "configuration",
                             "diagnostic": safe_diagnostic(category="configuration", route="config")})
        data = {}
    if path is None:
        _CACHE = data
        return _CACHE
    return data


def get(name: str, default=None, env: str | None = None):
    """Precedence: env var > config.toml > default. Empty env = unset."""
    if env:
        val = os.environ.get(env, "")
        if val != "":
            return val
    return load().get(name, default)


def _toml_value(v) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return str(v)
    # JSON string quoting is compatible with TOML basic strings and correctly
    # handles backslashes/newlines in paths on Windows and POSIX.
    return json.dumps(str(v), ensure_ascii=False)


def save(updates: dict, path: str | None = None) -> str:
    """Merge updates into config.toml; the replacement file is chmod 0600 where supported."""
    target = path or CONFIG_PATH
    directory = os.path.dirname(target) or "."
    os.makedirs(directory, mode=0o700, exist_ok=True)
    try:
        os.chmod(directory, 0o700)
    except OSError:
        pass
    current = load(target)
    current.update({k: v for k, v in updates.items() if v != ""})
    lines = [f"{k} = {_toml_value(v)}" for k, v in current.items()]
    # Canonical atomic writer (#137): 0700 dir / 0600 file, fsync + replace.
    from sieve.security import atomic_write_bytes
    atomic_write_bytes(target, ("\n".join(lines) + "\n").encode("utf-8"), secret=True)
    global _CACHE
    _CACHE = None
    return target
