"""Declarative, user-owned API integrations.

Integration files describe a small set of named HTTP workflows.  They are
data only: no Python, shell, expressions, or dynamic template evaluation is
accepted.  Credentials are referenced by ``env:NAME`` (or optionally
``keyring:SERVICE/USER``) and are resolved only while invoking a workflow.
"""
from __future__ import annotations

import inspect
import json
import math
import os
import tempfile
import re
from pathlib import Path
from typing import Any, Callable, Mapping, NoReturn
from urllib.parse import urljoin, urlsplit

import httpx

from sieve.security import SecurityError, resolve_and_check, validate_url

INTEGRATIONS_DIR = os.path.join(os.path.expanduser("~"), ".sieve", "integrations")
MAX_FILE_BYTES = 256 * 1024
MAX_BODY_BYTES = 2 * 1024 * 1024
MAX_WORKFLOWS = 64
MAX_NESTING = 8
MAX_TEMPLATE_LEN = 512
MAX_PAGES = 20
MAX_ITEMS = 10_000
_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,63}$")
_TEMPLATE = re.compile(r"^\{([A-Za-z][A-Za-z0-9_.-]{0,63})\}$")
_FIELD_PATH = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,63}(?:\.[A-Za-z][A-Za-z0-9_-]{0,63})*$")


class IntegrationError(ValueError):
    """A user-facing integration validation or invocation error."""


def _fail(message: str) -> NoReturn:
    raise IntegrationError(message)


def _walk(value: Any, *, depth: int = 0) -> None:
    if depth > MAX_NESTING:
        _fail("integration templates are nested too deeply")
    if isinstance(value, str):
        if len(value) > MAX_TEMPLATE_LEN:
            _fail("integration template value is too long")
        if "{" in value or "}" in value:
            if not _TEMPLATE.fullmatch(value):
                _fail("templates must be one whole {name} placeholder")
    elif isinstance(value, (int, float, bool)) or value is None:
        return
    elif isinstance(value, list):
        if len(value) > 32:
            _fail("integration lists are limited to 32 items")
        for item in value:
            _walk(item, depth=depth + 1)
    elif isinstance(value, dict):
        if len(value) > 64:
            _fail("integration objects are limited to 64 fields")
        for key, item in value.items():
            if not isinstance(key, str) or len(key) > 128:
                _fail("integration field names must be short strings")
            _walk(item, depth=depth + 1)
    else:
        _fail("integration values must be JSON primitives, lists, or objects")


def _host_allowed(url: str, allowed: list[str]) -> bool:
    host = (urlsplit(url).hostname or "").lower().rstrip(".")
    return any(host == item or host.endswith("." + item) for item in allowed)


def validate_integration(spec: Mapping[str, Any]) -> dict[str, Any]:
    """Validate and return a JSON-safe integration spec."""
    if not isinstance(spec, Mapping):
        _fail("integration must be a JSON object")
    _walk(dict(spec))
    name = spec.get("name")
    if not isinstance(name, str) or not _NAME.fullmatch(name):
        _fail("name must contain 1-64 letters, numbers, '_' or '-'")
    base = spec.get("base_url")
    if not isinstance(base, str) or not base.startswith("https://"):
        _fail("base_url must use HTTPS")
    try:
        base = validate_url(base).rstrip("/")
    except SecurityError as exc:
        raise IntegrationError(str(exc)) from exc
    allowed = spec.get("allowed_domains", [urlsplit(base).hostname])
    if not isinstance(allowed, list) or not allowed or len(allowed) > 16:
        _fail("allowed_domains must be a non-empty list")
    allowed = [str(x).lower().strip().rstrip(".") for x in allowed]
    if any(not x or ":" in x or "/" in x for x in allowed):
        _fail("allowed_domains must contain hostnames")
    if not _host_allowed(base, allowed):
        _fail("base_url host is not in allowed_domains")
    workflows = spec.get("workflows")
    if not isinstance(workflows, Mapping) or not workflows or len(workflows) > MAX_WORKFLOWS:
        _fail("workflows must be a non-empty object (maximum 64)")
    clean: dict[str, Any] = {"name": name, "base_url": base, "allowed_domains": allowed,
                             "workflows": {}}
    routing = spec.get("routing", "prefer")
    if not isinstance(routing, str) or routing not in {"prefer", "fallback", "off"}:
        _fail("routing must be prefer, fallback, or off")
    clean["routing"] = routing
    if isinstance(spec.get("description"), str):
        clean["description"] = spec["description"][:1000]
    provenance = spec.get("provenance")
    if provenance is not None:
        if not isinstance(provenance, Mapping):
            _fail("provenance must be an object")
        allowed_provenance = {"schema_url", "schema_version", "retrieved_at", "checksum", "license"}
        if any(key not in allowed_provenance for key in provenance):
            _fail("provenance contains an unsupported field")
        if any(not isinstance(value, str) or len(value) > 512 for value in provenance.values()):
            _fail("provenance values must be bounded strings")
        clean["provenance"] = dict(provenance)
    auth = spec.get("auth")
    if auth is not None:
        if not isinstance(auth, Mapping) or auth.get("type") not in {"bearer", "header", "query"}:
            _fail("auth.type must be bearer, header, or query")
        ref = auth.get("secret_ref")
        if not isinstance(ref, str) or not (ref.startswith("env:") or ref.startswith("keyring:")):
            _fail("auth.secret_ref must use env:NAME or keyring:SERVICE/USER")
        if ref.startswith("env:") and not _NAME.fullmatch(ref[4:]):
            _fail("environment secret names must be simple names")
        if auth["type"] in {"header", "query"}:
            name = auth.get("name")
            if not isinstance(name, str) or not _NAME.fullmatch(name) or any(ch in name for ch in "\r\n:"):
                _fail("header/query auth requires a protocol-safe name")
        prefix = auth.get("prefix")
        if prefix is not None and (not isinstance(prefix, str) or len(prefix) > 128 or "\r" in prefix or "\n" in prefix):
            _fail("auth.prefix must be a single bounded header-safe string")
        clean["auth"] = {k: str(v) for k, v in auth.items() if k in {"type", "secret_ref", "name", "prefix"}}
    for wf_name, wf in workflows.items():
        if not isinstance(wf_name, str) or not _NAME.fullmatch(wf_name) or not isinstance(wf, Mapping):
            _fail("workflow names and values are invalid")
        method = str(wf.get("method", "GET")).upper()
        if method not in {"GET", "POST", "PUT", "PATCH", "DELETE"}:
            _fail("workflow method is not allowed")
        path = wf.get("path", "/")
        if not isinstance(path, str) or not path.startswith("/") or path.startswith("//") or len(path) > 1024:
            _fail("workflow path must be a bounded relative path")
        endpoint = urljoin(base + "/", path.lstrip("/"))
        try:
            endpoint = validate_url(endpoint)
        except SecurityError as exc:
            raise IntegrationError(str(exc)) from exc
        if not _host_allowed(endpoint, allowed):
            _fail("workflow path leaves allowed_domains")
        for field in ("params", "body"):
            if field in wf and not isinstance(wf[field], Mapping):
                _fail(f"workflow {field} must be an object")
            if field in wf:
                _walk(wf[field])
        response = wf.get("response", {})
        if not isinstance(response, Mapping):
            _fail("workflow response must be an object")
        select = response.get("select", "")
        if not isinstance(select, str) or (select and not _FIELD_PATH.fullmatch(select)) or len(select) > 256:
            _fail("response.select must be a dotted field path")
        mapping = response.get("mapping", {})
        if (not isinstance(mapping, Mapping) or len(mapping) > 64
                or any(not isinstance(k, str) or not _NAME.fullmatch(k)
                       or not isinstance(v, str) or not _FIELD_PATH.fullmatch(v)
                       for k, v in mapping.items())):
            _fail("response.mapping must map output names to dotted paths")
        pagination = wf.get("pagination")
        clean_pagination = None
        if pagination is not None:
            if not isinstance(pagination, Mapping):
                _fail("workflow pagination must be an object")
            page_mode = pagination.get("mode", "cursor")
            if page_mode not in {"cursor", "page", "offset"}:
                _fail("pagination.mode must be cursor, page, or offset")
            page_param = pagination.get("param", "cursor" if page_mode == "cursor" else "page")
            if not isinstance(page_param, str) or not _NAME.fullmatch(page_param):
                _fail("pagination.param must be a simple field name")
            next_path = pagination.get("next", pagination.get("next_path", ""))
            items_path = pagination.get("items", pagination.get("items_path", ""))
            for label, value in (("next", next_path), ("items", items_path)):
                if value and (not isinstance(value, str) or not _FIELD_PATH.fullmatch(value)):
                    _fail(f"pagination.{label} must be a dotted field path")
            max_pages = pagination.get("max_pages", 5)
            if isinstance(max_pages, bool) or not isinstance(max_pages, int) or not 1 <= max_pages <= MAX_PAGES:
                _fail(f"pagination.max_pages must be between 1 and {MAX_PAGES}")
            clean_pagination = {"mode": page_mode, "param": page_param,
                                "next": next_path, "items": items_path,
                                "max_pages": max_pages}
        clean["workflows"][wf_name] = {"method": method, "path": path,
            "params": dict(wf.get("params", {})), "body": dict(wf.get("body", {})),
            "response": {"select": select, "mapping": dict(mapping)}}
        if clean_pagination:
            clean["workflows"][wf_name]["pagination"] = clean_pagination
    return clean


def load_integrations(directory: str | None = None) -> dict[str, dict[str, Any]]:
    root = Path(directory or INTEGRATIONS_DIR)
    result: dict[str, dict[str, Any]] = {}
    if not root.exists():
        return result
    for path in sorted(root.glob("*.json")):
        if path.stat().st_size > MAX_FILE_BYTES:
            raise IntegrationError(f"integration file is too large: {path.name}")
        try:
            spec = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise IntegrationError(f"invalid integration file {path.name}: {exc}") from exc
        clean = validate_integration(spec)
        if clean["name"] in result:
            _fail(f"duplicate integration name: {clean['name']}")
        result[clean["name"]] = clean
    return result


def scaffold_integration(name: str, base_url: str, *, directory: str | os.PathLike[str] | None = None,
                         output: str | os.PathLike[str] | None = None,
                         workflow: str = "lookup", method: str = "GET", path: str = "/",
                         secret_ref: str | None = None, auth_type: str = "bearer",
                         routing: str = "prefer", overwrite: bool = False,
                         description: str | None = None, schema_url: str | None = None,
                         schema_version: str | None = None, license_name: str | None = None) -> Path:
    """Write a reviewable integration template and return its path.

    The scaffold only accepts a secret *reference*.  It never reads or writes
    the referenced environment variable/keyring entry, which keeps generated
    files safe to inspect and review before installation.
    """
    # Run the same validation as a loaded file before touching the filesystem.
    if not isinstance(name, str) or not _NAME.fullmatch(name):
        _fail("name must contain 1-64 letters, numbers, '_' or '-'")
    if not isinstance(workflow, str) or not _NAME.fullmatch(workflow):
        _fail("workflow names and values are invalid")
    if secret_ref is not None:
        auth: dict[str, Any] = {"type": auth_type, "secret_ref": secret_ref}
    else:
        auth = {}
    generated: dict[str, Any] = {
        "name": name,
        "base_url": base_url,
        "allowed_domains": [urlsplit(base_url).hostname or ""],
        "routing": routing,
        "workflows": {
            workflow: {"method": method, "path": path, "params": {"query": "{query}"},
                       "response": {"select": "", "mapping": {}}}
        },
    }
    if description:
        generated["description"] = description
    if auth:
        generated["auth"] = auth
    provenance = {"schema_url": schema_url, "schema_version": schema_version,
                  "license": license_name}
    if any(value is not None for value in provenance.values()):
        generated["provenance"] = {key: value for key, value in provenance.items() if value is not None}
    clean = validate_integration(generated)
    target = Path(output) if output is not None else Path(directory or INTEGRATIONS_DIR) / f"{name}.json"
    # Resolve symlinks/canonical paths before checking the destination
    # (issue #15): a symlinked target must not redirect the write outside
    # the intended location.
    if target.is_symlink():
        _fail(f"integration target is a symlink: {target}")
    target = target.resolve(strict=False)
    if target.exists() and not overwrite:
        _fail(f"integration file already exists: {target}")
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(clean, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    fd, temporary_name = tempfile.mkstemp(prefix=f".{target.name}.", dir=target.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        # Owner-only permissions on the scaffold (issue #216): integration
        # files may name secret references and endpoint policy.
        os.chmod(temporary, 0o600)
        os.replace(temporary, target)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
    return target


def _value(data: Any, path: str) -> Any:
    for part in path.split(".") if path else []:
        if not isinstance(data, Mapping) or part not in data:
            return None
        data = data[part]
    return data


def _render(value: Any, args: Mapping[str, Any]) -> Any:
    if isinstance(value, str):
        match = _TEMPLATE.fullmatch(value)
        if match:
            return _value(args, match.group(1))
        return value
    if isinstance(value, list):
        return [_render(v, args) for v in value]
    if isinstance(value, dict):
        return {k: _render(v, args) for k, v in value.items()}
    return value


def _secret(ref: str) -> str:
    if ref.startswith("env:"):
        value = os.environ.get(ref[4:], "")
    else:
        try:
            import keyring
            service, user = ref[8:].split("/", 1)
            value = keyring.get_password(service, user) or ""
        except Exception:
            value = ""
    if not value:
        _fail(f"secret is not configured for {ref.split(':', 1)[0]} reference")
    return value


def _redact_secret(value: Any, secret: str) -> Any:
    """Replace a credential wherever an upstream response echoed it."""
    if isinstance(value, dict):
        for key, item in list(value.items()):
            value[key] = _redact_secret(item, secret)
        return value
    if isinstance(value, list):
        for index, item in enumerate(value):
            value[index] = _redact_secret(item, secret)
        return value
    if isinstance(value, str):
        return value.replace(secret, "[redacted]")
    return value


def _response_data(payload: Any, response_spec: Mapping[str, Any]) -> Any:
    """Apply the declarative response selector/mapping to one payload."""
    selected = _value(payload, response_spec["select"])
    mapping = response_spec["mapping"]
    return ({name: _value(selected, path) for name, path in mapping.items()}
            if mapping else selected)


def _pagination_value(payload: Any, path: str) -> Any:
    """Read a pagination value, accepting a missing optional path."""
    return _value(payload, path) if path else None


def _api_provenance(clean: Mapping[str, Any], workflow: str, source: str) -> dict[str, Any]:
    """Build stable provenance fields without exposing request credentials."""
    provenance = dict(clean.get("provenance") or {})
    provenance.update({"adapter": f"sieve.integration.{clean['name']}",
                       "provider": clean["name"], "integration": clean["name"],
                       "operation": workflow, "workflow": workflow,
                       "source": source, "source_url": source})
    return provenance


def _invoke_api(clean: Mapping[str, Any], workflow: str, args: Mapping[str, Any], *,
                client: httpx.Client | None, timeout: float, max_body_bytes: int) -> dict[str, Any]:
    """Invoke a validated workflow, including bounded pagination."""
    wf = clean["workflows"][workflow]
    endpoint = urljoin(clean["base_url"] + "/", wf["path"].lstrip("/"))
    validate_url(endpoint)
    if not _host_allowed(endpoint, clean["allowed_domains"]):
        _fail("endpoint host is not allowlisted")
    resolve_and_check(urlsplit(endpoint).hostname or "", urlsplit(endpoint).port or 443)
    headers = {"Accept": "application/json"}
    params = _render(wf["params"], args)
    body = _render(wf["body"], args)
    auth = clean.get("auth")
    secret = ""
    if auth:
        secret = _secret(auth["secret_ref"])
        if auth["type"] == "bearer":
            headers["Authorization"] = f"{auth.get('prefix', 'Bearer ')}{secret}"
        elif auth["type"] == "header":
            headers[auth["name"]] = secret
        else:
            params[auth["name"]] = secret
    own = client is None
    http = client or httpx.Client(follow_redirects=False, timeout=timeout)
    page_spec = wf.get("pagination")
    pages: list[Any] = []
    sources: list[str] = []
    current_endpoint = endpoint
    current_params = dict(params)
    more_available = False
    try:
        max_pages = page_spec["max_pages"] if page_spec else 1
        for page_number in range(max_pages):
            for hop in range(4):
                # Streamed read bounded by max_body_bytes (issue #16): the body
                # is never fully buffered before the cap applies; reading stops
                # at max_body_bytes + 1 and anything larger fails. The stream
                # context is consumed fully inside this block, so the JSON
                # validation below sees a normal (capped) response object.
                req = http.build_request(
                    wf["method"], current_endpoint, params=current_params,
                    json=body if wf["method"] != "GET" else None, headers=headers,
                )
                with http.stream(req.method, str(req.url),
                                 content=req.content if req.method != "GET" else None,
                                 headers=req.headers) as streamed:
                    chunks: list[bytes] = []
                    total = 0
                    oversized = False
                    for chunk in streamed.iter_bytes(64 * 1024):
                        total += len(chunk)
                        if total > max_body_bytes:
                            oversized = True
                            break
                        chunks.append(chunk)
                    status = streamed.status_code
                    # Header memory bound (#242): cap count and per-value size
                    # before materializing an httpx.Response from a stream.
                    resp_headers = {
                        str(k)[:256]: str(v)[:4096]
                        for k, v in list(streamed.headers.items())[:64]
                    }
                    is_redirect = streamed.is_redirect
                if oversized:
                    _fail("response body exceeds configured limit")
                response = httpx.Response(
                    status, headers=resp_headers,
                    content=b"".join(chunks), request=req,
                )
                if response.is_redirect:
                    if hop >= 3 or not response.headers.get("location"):
                        _fail("redirect limit exceeded")
                    current_endpoint = urljoin(current_endpoint, response.headers["location"])
                    validate_url(current_endpoint)
                    if not _host_allowed(current_endpoint, clean["allowed_domains"]):
                        _fail("redirect leaves allowed_domains")
                    resolve_and_check(urlsplit(current_endpoint).hostname or "", urlsplit(current_endpoint).port or 443)
                    continue
                break
            try:
                payload = response.json()
            except ValueError as exc:
                _fail(f"response was not valid JSON: {exc}")
            sources.append(current_endpoint)
            if page_spec and page_spec["items"]:
                items = _pagination_value(payload, page_spec["items"])
                if items is None:
                    items = []
                if not isinstance(items, list):
                    _fail("pagination.items must select a JSON array")
                for item in items:
                    pages.append(_response_data(item, wf["response"]))
                    if len(pages) > MAX_ITEMS:
                        _fail(f"pagination exceeds the {MAX_ITEMS} item limit")
            else:
                pages.append(_response_data(payload, wf["response"]))
            if not page_spec:
                break
            next_value = _pagination_value(payload, page_spec["next"])
            more_available = next_value not in (None, "", False)
            if next_value in (None, "", False):
                # Page/offset APIs can omit a next marker; an empty page ends
                # the sequence while a non-empty page advances one position.
                if page_spec["mode"] not in {"page", "offset"} or not pages or not page_spec["items"]:
                    break
                next_value = page_number + 2
                more_available = True
            if isinstance(next_value, bool) or not isinstance(next_value, (str, int, float)):
                _fail("pagination.next must be a string or number")
            if isinstance(next_value, str) and (next_value.startswith("https://") or next_value.startswith("http://")):
                current_endpoint = urljoin(current_endpoint, next_value)
                validate_url(current_endpoint)
                if not _host_allowed(current_endpoint, clean["allowed_domains"]):
                    _fail("pagination URL leaves allowed_domains")
                resolve_and_check(urlsplit(current_endpoint).hostname or "", urlsplit(current_endpoint).port or 443)
                current_params = {}
            else:
                current_params = dict(params)
                current_params[page_spec["param"]] = next_value
        result_data: Any = pages if page_spec else pages[0]
        result = {"ok": response.is_success, "status": response.status_code,
                  "integration": clean["name"], "workflow": workflow,
                  "data": result_data, "source": sources[-1],
                  "provenance": _api_provenance(clean, workflow, sources[-1])}
        if page_spec:
            result["provenance"]["sources"] = sources
            result["provenance"]["pagination"] = {
                "mode": page_spec["mode"], "pages_fetched": page_number + 1,
                "max_pages": page_spec["max_pages"],
                "truncated": page_number + 1 >= page_spec["max_pages"] and more_available,
            }
        if auth:
            _redact_secret(result, secret)
        return result
    except httpx.TimeoutException:
        return {"ok": False, "integration": clean["name"], "workflow": workflow,
                "error": "request timed out", "source": current_endpoint,
                "provenance": _api_provenance(clean, workflow, current_endpoint)}
    finally:
        if own:
            http.close()


def invoke_integration(spec: Mapping[str, Any], workflow: str, arguments: Mapping[str, Any] | None = None,
                       *, client: httpx.Client | None = None, timeout: float = 30.0,
                       max_body_bytes: int = MAX_BODY_BYTES) -> dict[str, Any]:
    """Explicitly invoke one named workflow and return a normalized envelope."""
    clean = validate_integration(spec)
    if workflow not in clean["workflows"]:
        _fail(f"unknown workflow: {workflow}")
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(float(timeout)) or not 0 < timeout <= 120:
        _fail("timeout must be a finite number between 0 and 120 seconds")
    if arguments is None:
        args: Mapping[str, Any] = {}
    elif not isinstance(arguments, Mapping):
        _fail("workflow arguments must be an object")
    else:
        args = arguments
    if not 0 < max_body_bytes <= MAX_BODY_BYTES:
        _fail("max_body_bytes must be between 1 and 2097152")
    return _invoke_api(clean, workflow, args, client=client, timeout=timeout, max_body_bytes=max_body_bytes)


def _call_fallback(fallback: Callable[..., Any], arguments: Mapping[str, Any]) -> Any:
    """Call a scraper adapter with arguments when it declares that parameter."""
    try:
        signature = inspect.signature(fallback)
        try:
            signature.bind(arguments)
        except TypeError:
            return fallback()
    except (TypeError, ValueError):
        # Some extension callables do not expose an inspectable signature.
        return fallback(arguments)
    return fallback(arguments)


def _fallback_result(value: Any, *, integration: str, workflow: str,
                     routing: str, api_attempted: bool) -> dict[str, Any]:
    """Normalize an adapter result while retaining its source metadata."""
    if isinstance(value, Mapping) and isinstance(value.get("ok"), bool):
        result = dict(value)
        result.setdefault("data", result.get("result"))
        result.setdefault("source", "scraper")
    else:
        result = {"ok": True, "data": value, "source": "scraper"}
    provenance = dict(result.get("provenance") or {})
    provenance.update({"adapter": "sieve.integration.fallback", "integration": integration,
                       "workflow": workflow, "routing": routing,
                       "api_attempted": api_attempted})
    result["provenance"] = provenance
    return result


def route_integration(spec: Mapping[str, Any], workflow: str,
                      arguments: Mapping[str, Any] | None = None, *,
                      fallback: Callable[..., Any] | None = None,
                      routing: str | None = None,
                      client: httpx.Client | None = None, timeout: float = 30.0,
                      max_body_bytes: int = MAX_BODY_BYTES) -> dict[str, Any]:
    """Route a provider API and an optional scraper adapter explicitly.

    ``prefer`` tries the API first and falls back to the scraper on any
    provider failure. ``fallback`` tries the scraper first and uses the API
    only when it fails. ``off`` disables the API entirely. The fallback is an
    injected callable so integration files remain declarative and cannot run
    arbitrary code.
    """
    clean = validate_integration(spec)
    if workflow not in clean["workflows"]:
        _fail(f"unknown workflow: {workflow}")
    mode = routing if routing is not None else clean.get("routing", "prefer")
    if mode not in {"prefer", "fallback", "off"}:
        _fail("routing must be prefer, fallback, or off")
    if arguments is None:
        args: Mapping[str, Any] = {}
    elif not isinstance(arguments, Mapping):
        _fail("workflow arguments must be an object")
    else:
        args = arguments

    def scrape() -> dict[str, Any]:
        if fallback is None:
            return {"ok": False, "error": "scraper fallback is not configured", "source": "scraper"}
        try:
            return _fallback_result(_call_fallback(fallback, args), integration=clean["name"],
                                   workflow=workflow, routing=mode, api_attempted=False)
        except Exception as exc:
            return {"ok": False, "error": f"scraper fallback failed: {type(exc).__name__}", "source": "scraper",
                    "provenance": {"adapter": "sieve.integration.fallback",
                                   "integration": clean["name"], "workflow": workflow,
                                   "routing": mode, "api_attempted": False}}

    if mode in {"fallback", "off"}:
        first = scrape()
        if first.get("ok") or mode == "off":
            return first
    try:
        api_result = invoke_integration(clean, workflow, args, client=client,
                                        timeout=timeout, max_body_bytes=max_body_bytes)
    except (IntegrationError, httpx.HTTPError) as exc:
        workflow_spec = clean["workflows"][workflow]
        api_source = urljoin(clean["base_url"] + "/", workflow_spec["path"].lstrip("/"))
        api_result = {"ok": False, "integration": clean["name"], "workflow": workflow,
                      "error": f"integration request failed: {type(exc).__name__}", "source": api_source,
                      "provenance": _api_provenance(clean, workflow, api_source)}
    if api_result.get("ok") or mode == "fallback":
        api_result.setdefault("provenance", {})["routing"] = mode
        api_result["provenance"]["api_attempted"] = True
        return api_result
    if fallback is None:
        api_result.setdefault("provenance", {})["routing"] = mode
        api_result["provenance"]["api_attempted"] = True
        return api_result
    result = scrape()
    result.setdefault("provenance", {})["api_attempted"] = True
    return result
