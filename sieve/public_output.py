"""Redact credentials and serialize public results within a UTF-8 byte budget.

Traversal stops when the budget is spent. Strings are sliced before encoding,
containers are consumed incrementally, and inputs are never mutated.
"""

from __future__ import annotations

import json
from inspect import getattr_static
import re
import math
from datetime import date, datetime, time
from enum import Enum
from typing import Any
from itertools import chain, islice
from uuid import UUID
from urllib.parse import unquote, urlsplit, urlunsplit

__all__ = ["redact_secrets", "bounded_json_dumps", "safe_public_json", "safe_error", "MAX_PUBLIC_OUTPUT_BYTES"]

MAX_PUBLIC_OUTPUT_BYTES = 2 * 1024 * 1024
_REDACTED = "[REDACTED]"
_MARKER = '{"_truncated":true}'
_SECRET_KEY = re.compile(
    r"(?:^|[_\-\s])(?:password|passwd|pwd|secret|token|api[_\-]?key|auth|authorization|"
    r"cookies?|set[_\-]?cookie|session[_\-]?id|credentials?|private[_\-]?key|"
    r"access[_\-]?key|client[_\-]?secret|bearer)$", re.IGNORECASE,
)
_CREDENTIAL = re.compile(r"^(?:bearer\s+[A-Za-z0-9._~+/=-]{8,}|basic\s+[A-Za-z0-9+/=]{8,})$", re.IGNORECASE)
_URL_IN_TEXT = re.compile(r"(?<![A-Za-z0-9+.-])[A-Za-z][A-Za-z0-9+.-]*://[^\s<>\"']+")

_ERROR_MESSAGES = {
    "input": "Invalid input.",
    "timeout": "The operation timed out.",
    "blocked": "Access was blocked.",
    "network": "The network request failed.",
    "configuration": "Configuration could not be read.",
    "dependency": "A required optional dependency is unavailable.",
    "permission": "The operation was not permitted.",
    "extract": "Content extraction failed.",
    "ocr": "OCR failed.",
    "budget": "The resource budget was exceeded.",
    "execution": "The configured executable failed.",
    "ok": "The operation completed.",
    "internal": "The operation failed.",
}


def safe_error(exc: Exception, *, fallback_category: str = "internal") -> dict[str, str]:
    """Categorize failure without reading or exposing backend exception values."""
    category = getattr_static(exc, "category", None)
    if not isinstance(category, str) or category not in _ERROR_MESSAGES or category == "ok":
        if isinstance(exc, TimeoutError) or type(exc).__name__ in {
            "TimeoutException", "TimeoutExpired", "ReadTimeout", "ConnectTimeout", "WriteTimeout", "PoolTimeout",
        }:
            category = "timeout"
        elif isinstance(exc, (ValueError, TypeError)):
            category = "input"
        elif isinstance(exc, ImportError):
            category = "dependency"
        elif isinstance(exc, PermissionError):
            category = "permission"
        elif isinstance(exc, OSError):
            category = "network"
        else:
            category = fallback_category if fallback_category in _ERROR_MESSAGES else "internal"
    return {"category": category, "error": _ERROR_MESSAGES[category]}


_ROUTES = {"cli", "mcp", "sdk", "batch", "fetch", "extract", "crawl", "config", "stt", "youtube", "action"}
_FETCHERS = {"http", "dynamic", "stealthy", "cache", "none", "sleeper"}
_DIMENSIONS = {"input_bytes", "output_chars", "output_bytes", "items", "pages", "nodes", "retries", "deadline"}


def safe_diagnostic(exc: Exception | None = None, *, category: str = "internal",
                    route: str = "", fetcher: str = "", truncated=()) -> dict:
    """One bounded vocabulary; backend values cannot supply messages or policy."""
    if exc is not None:
        category = safe_error(exc, fallback_category=category)["category"]
    if not isinstance(category, str) or category not in _ERROR_MESSAGES:
        category = "internal"
    if not isinstance(truncated, (tuple, list, set, frozenset)):
        truncated = ()
    dimensions = sorted({item for item in islice(truncated, 32)
                         if isinstance(item, str) and item in _DIMENSIONS})
    return {"category": category, "safe_message": _ERROR_MESSAGES[category],
            "retryable": category in {"timeout", "network"},
            "route": route if isinstance(route, str) and route in _ROUTES else "",
            "fetcher": fetcher if isinstance(fetcher, str) and fetcher in _FETCHERS else "",
            "truncated": dimensions}


def failure_payload(exc: Exception, *, fallback_category: str = "internal", route: str = "") -> dict:
    failure = safe_error(exc, fallback_category=fallback_category)
    return {**failure, "diagnostic": safe_diagnostic(category=failure["category"], route=route)}


def result_diagnostic(result, *, route: str = "") -> dict:
    """Describe known envelope fields without copying backend diagnostic data."""
    def value(name, default=None):
        return result.get(name, default) if isinstance(result, dict) else getattr(result, name, default)
    category = value("category")
    if not isinstance(category, str) or category not in _ERROR_MESSAGES:
        category = "internal" if value("error") else "ok"
    budget = value("resource_budget", {})
    raw_dimensions = budget.get("truncated", ()) if isinstance(budget, dict) else ()
    dimensions = list(islice(raw_dimensions, 32)) if isinstance(raw_dimensions, (list, tuple, set)) else []
    if value("is_truncated") or value("truncated_by_budget"):
        dimensions.append("output_chars")
    if value("truncated_by_time"):
        dimensions.append("deadline")
    return safe_diagnostic(category=category, route=route, fetcher=value("fetcher_used", ""), truncated=dimensions)


def _is_secret_key(key: str) -> bool:
    normalized = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", key.strip())
    return bool(_SECRET_KEY.search(normalized))


def _redact_url(match: re.Match[str]) -> str:
    try:
        parts = urlsplit(match.group())
        def redact_parameters(value):
            parameters = []
            for parameter in value.split("&"):
                key, separator, _ = parameter.partition("=")
                if separator and _is_secret_key(unquote(key)):
                    parameter = key + "=%5BREDACTED%5D"
                parameters.append(parameter)
            return "&".join(parameters)
        return urlunsplit(parts._replace(netloc=parts.netloc.rsplit("@", 1)[-1],
                                        query=redact_parameters(parts.query),
                                        fragment=redact_parameters(parts.fragment)))
    except ValueError:
        return _REDACTED


def _redact_string(value: str) -> str:
    if _CREDENTIAL.fullmatch(value):
        return _REDACTED
    return _URL_IN_TEXT.sub(_redact_url, value)


def redact_secrets(obj: Any, _depth: int = 0) -> Any:
    """Return a redacted copy. Public serialization uses the bounded walk below."""
    if _depth > 64:
        return {"_truncated": True}
    if isinstance(obj, dict):
        return {
            _redact_string(str(key)): _REDACTED if _is_secret_key(str(key))
            else redact_secrets(value, _depth + 1)
            for key, value in obj.items()
        }
    if isinstance(obj, (list, tuple)):
        return [redact_secrets(value, _depth + 1) for value in obj]
    return _redact_string(obj) if isinstance(obj, str) else obj


def _string(value: str, budget: int, ensure_ascii: bool, redact: bool) -> str | None:
    prefix = value[:budget]
    if redact:
        if len(value) > budget:
            prefix = _URL_IN_TEXT.sub(
                lambda match: _REDACTED if match.end() == len(prefix) and "/" not in match.group().split("://", 1)[1] else match.group(),
                prefix,
            )
        prefix = _redact_string(prefix)
    truncated = len(value) > budget and prefix != _REDACTED
    suffix = "...[_truncated]"

    def encode(length: int) -> str:
        return json.dumps(prefix[:length] + (suffix if truncated or length < len(prefix) else ""), ensure_ascii=ensure_ascii)

    result = encode(len(prefix))
    if len(result.encode("utf-8")) <= budget:
        return result
    low, high = 0, len(prefix)
    result = encode(0)
    if len(result.encode("utf-8")) > budget:
        return None
    while low < high:
        middle = (low + high + 1) // 2
        candidate = encode(middle)
        if len(candidate.encode("utf-8")) <= budget:
            low, result = middle, candidate
        else:
            high = middle - 1
    return result


def _encode(obj: Any, budget: int, ensure_ascii: bool, redact: bool,
            depth: int, ancestors: set[int], compact: bool) -> str | None:
    if budget < len(_MARKER):
        return None
    if depth > 64 or id(obj) in ancestors:
        return _MARKER
    if isinstance(obj, str):
        return _string(obj, budget, ensure_ascii, redact)
    if isinstance(obj, (date, datetime, time, UUID)):
        value = str(obj) if isinstance(obj, UUID) else obj.isoformat()
        return _string(value, budget, ensure_ascii, redact)
    if isinstance(obj, Enum):
        return _encode(obj.value, budget, ensure_ascii, redact, depth + 1, ancestors, compact)
    model = hasattr(type(obj), "model_fields")
    if isinstance(obj, (dict, list, tuple)) or model:
        mapping = isinstance(obj, dict) or model
        opening, closing = ("{", "}") if mapping else ("[", "]")
        separator = "," if compact else ", "
        marker = ('"_truncated":true' if compact else '"_truncated": true') if mapping else _MARKER
        remaining = budget - 2 - len(marker) - len(separator)
        pieces: list[str] = []
        ancestors.add(id(obj))
        try:
            if model:
                entries = _model_items(obj)
            else:
                entries = obj.items() if mapping else enumerate(obj)
                if mapping and "diagnostic" not in obj and (obj.get("error") or obj.get("is_truncated") or obj.get("resource_budget")):
                    entries = chain(entries, (("diagnostic", result_diagnostic(obj)),))
            for key, value in entries:
                label = ""
                if mapping:
                    if not isinstance(key, (str, int, float, bool, type(None))):
                        raise TypeError("public JSON keys must be scalar values")
                    key = key if isinstance(key, str) else json.dumps(key)
                    if len(key) > remaining:
                        pieces.append(marker)
                        break
                    label = json.dumps(_redact_string(key) if redact else key, ensure_ascii=ensure_ascii) + (":" if compact else ": ")
                    if redact and _is_secret_key(key):
                        value = _REDACTED
                available = remaining - len(label.encode("utf-8")) - len(separator) * bool(pieces)
                encoded = _encode(value, available, ensure_ascii, redact, depth + 1, ancestors, compact)
                if encoded is None:
                    pieces.append(marker)
                    break
                piece = label + encoded
                remaining -= len(piece.encode("utf-8")) + len(separator) * bool(pieces)
                pieces.append(piece)
        finally:
            ancestors.remove(id(obj))
        result = opening + separator.join(pieces) + closing
        return result if len(result.encode("utf-8")) <= budget else _MARKER
    if isinstance(obj, int) and obj.bit_length() > budget * 3:
        return _MARKER
    if isinstance(obj, float) and not math.isfinite(obj):
        return "null"
    if obj is not None and not isinstance(obj, (int, float, bool)):
        raise TypeError(f"unsupported public JSON value: {type(obj).__name__}")
    result = json.dumps(obj, ensure_ascii=ensure_ascii)
    return result if len(result) <= budget else _MARKER


def _model_items(obj: Any):
    """Expand model fields lazily, preserving schema-level serialization exclusions."""
    for name, field in type(obj).model_fields.items():
        if not field.exclude:
            yield name, getattr(obj, name)
    yield from (obj.model_extra or {}).items()
    if "diagnostic" not in type(obj).model_fields and (getattr(obj, "error", "") or getattr(obj, "is_truncated", False) or getattr(obj, "resource_budget", {})):
        yield "diagnostic", result_diagnostic(obj)


def _dumps(obj: Any, max_bytes: int, ensure_ascii: bool, redact: bool) -> str:
    if isinstance(max_bytes, bool) or not isinstance(max_bytes, int):
        raise TypeError("max_bytes must be an integer")
    if max_bytes < len(_MARKER):
        raise ValueError(f"max_bytes must be at least {len(_MARKER)} for a truncation marker")
    compact = hasattr(obj, "model_dump")
    if compact and not hasattr(type(obj), "model_fields"):
        obj = obj.model_dump()
    return _encode(obj, max_bytes, ensure_ascii, redact, 0, set(), compact) or _MARKER


def bounded_json_dumps(obj: Any, *, max_bytes: int = MAX_PUBLIC_OUTPUT_BYTES,
                       ensure_ascii: bool = False) -> str:
    """Serialize with explicit truncation, without first copying the entire input."""
    return _dumps(obj, max_bytes, ensure_ascii, False)


def safe_public_json(obj: Any, *, max_bytes: int = MAX_PUBLIC_OUTPUT_BYTES,
                     ensure_ascii: bool = False) -> str:
    """Redact during the bounded traversal at the final public output boundary."""
    return _dumps(obj, max_bytes, ensure_ascii, True)
