"""Separate URL identities for retrieval, display, deduplication and domains.

Deduplication never authorizes a fetch. Path/query escaping remains opaque;
only explicitly declared tracking parameters may be discarded from a key.
"""
from __future__ import annotations

import ipaddress
from urllib.parse import unquote, urlsplit, urlunsplit


def _parts(url: str):
    if not isinstance(url, str) or any(ord(char) < 32 or ord(char) == 127 for char in url):
        raise ValueError("URL contains control characters")
    value = url.strip()
    if value.startswith("//"):
        value = "https:" + value
    parts = urlsplit(value)
    if parts.scheme not in {"http", "https"} or not parts.hostname or parts.username is not None or parts.password is not None:
        raise ValueError("URL must be credential-free HTTP(S)")
    if parts.port is not None and not 1 <= parts.port <= 65535:
        raise ValueError("URL port is invalid")
    return parts


def hostname(url: str, *, strip_www: bool = False) -> str:
    try:
        host = _parts(url).hostname.lower().rstrip(".")
        try:
            host = ipaddress.ip_address(host).compressed
        except ValueError:
            host = host.encode("idna").decode("ascii")
        return host[4:] if strip_www and host.startswith("www.") else host
    except (ValueError, UnicodeError, AttributeError):
        return ""


def same_domain(left: str, right: str, *, subdomains: bool = False) -> bool:
    first, second = hostname(left), hostname(right)
    return bool(first and second and (first == second or (subdomains and first.endswith("." + second))))


def dedupe_url(url: str, *, tracking_params=(), trailing_slash: bool = True, root_path: str = "") -> str:
    try:
        parts = _parts(url)
        host = hostname(url)
        if not host:
            return ""
        authority = f"[{host}]" if ":" in host else host
        if parts.port is not None and (parts.scheme, parts.port) not in {("http", 80), ("https", 443)}:
            authority += f":{parts.port}"
        path = parts.path or root_path
        if trailing_slash and len(path) > 1:
            path = path.rstrip("/")
        query = "&".join(part for part in parts.query.split("&") if part and
                         unquote(part.split("=", 1)[0]).lower() not in tracking_params
                         and not unquote(part.split("=", 1)[0]).lower().startswith("utm_"))
        return urlunsplit((parts.scheme, authority, path, query, ""))
    except (ValueError, UnicodeError):
        return ""


def fetch_url(url: str) -> str:
    """Authorize the original retrieval URL with Sieve's existing DNS policy."""
    _parts(url)
    from sieve.security import validate_url
    return validate_url(url)


def display_url(url: str) -> str:
    from sieve.public_output import redact_secrets
    value = redact_secrets({"url": url})["url"]
    try:
        _parts(value)
        return value
    except ValueError:
        return ""
