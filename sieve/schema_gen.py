"""LLM-once schema generation for Sieve.

Implements the "LLM-once" pattern (Scout 21): make exactly ONE LLM call against
a sample page that emits a reusable *extraction schema* with CSS selectors, then
hand that schema to the deterministic extractor whose every later run costs zero
LLM tokens.

Selector-gen over schema-gen is the community verdict: pair the JSON schema with
explicit CSS selectors (see ``extraction.extract_json``) so sampling is faithful
and no further model involvement is needed.

Credentials use Sieve's canonical BYOK resolver. The LLM request uses urllib;
JSON parsing and markdown-fence handling use the Python standard library.

Public API
----------
``generate_schema(sample_html, fields, *, model, base_url, api_key) -> dict``
    One LLM call -> ``{"baseSelector": ..., "fields": [...]}``.
``generate_schema_from_url(url, fields, *, fetch, **kw) -> dict``
    Uses a required caller-supplied fetch hook to retrieve the sample HTML.
``schema_to_jsonl_docs(schema) -> dict``
    Human-friendly summary dict for the JSONL doc pipeline.

An unreachable endpoint or any parse/validation failure surfaces as a clean
``ValueError`` so callers can fail fast without guessing.
"""

from __future__ import annotations

import json
import ipaddress
import re
import urllib.request
from urllib.parse import urlsplit
from typing import Any, Callable

from .byok_config import get_llm_key
from sieve.public_output import safe_error

__all__ = [
    "generate_schema",
    "generate_schema_from_url",
    "schema_to_jsonl_docs",
    "parse_schema_json",
    "_build_prompt",
]

# CSS selector + type invented by the model. ``extraction.extract_json`` is the
# deterministic consumer of these. A single step or a list of steps is allowed.
_VALID_TYPES = {
    "text",
    "attribute",
    "html",
    "regex",
    "nested",
}

_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.DOTALL)
_MAX_LLM_RESPONSE_BYTES = 2 * 1024 * 1024
_MAX_FIELDS = 64
_MAX_FIELD_NAME_CHARS = 128


def _build_prompt(sample_html: str, fields: list[str]) -> str:
    """Build the system+user prompt asking for ONLY a JSON extraction schema."""
    field_lines = "\n".join(f"- {f}" for f in fields)
    # Bound the sample so the single token spend stays small. The LLM must
    # actually SEE the page structure — without it, it guesses selectors from
    # training data and emits plausible-but-wrong schemas (verified failure:
    # quotes.toscrape.com emitted ".item" instead of "div.quote").
    sample = (sample_html or "<html></html>")[:12_000]
    return (
        "You are an HTML extraction-schema engineer. Analyze the provided sample "
        "HTML snippet and emit EXACTLY ONE JSON object (no prose, no markdown "
        "outside code fences) describing how to extract structured data with CSS "
        "selectors.\n\n"
        "Schema format (valid for the deterministic extractor):\n"
        '{"baseSelector": "<CSS selector matching each repeatable item>", '
        '"fields": [{"name": "...", "selector": "...", "type": '
        '"text|attribute|html|regex|nested"}]}\n\n'
        "Rules:\n"
        "- Provide exactly one field entry per requested field name, in order.\n"
        "- Each field MUST have a precise, minimal CSS selector targeting that "
        "datum within one item (not across the whole page).\n"
        '- Use type "attribute" for values in href/src/alt etc. (add '
        '"attribute": "href" etc.); use "text" for visible text; "html" for '
        "inner HTML; \"regex\" when combined with a \"pattern\"; \"nested\" for grouped sub-"
        "fields.\n"
        "- \"baseSelector\" must match the repeating container of the items "
        "(e.g. \"div.product\"), so each matched element yields one record.\n"
        "- Base your selectors ONLY on the actual HTML below — never guess "
        "class names the snippet does not contain.\n"
        "Requested fields:\n"
        f"{field_lines}\n\n"
        "Sample HTML to analyze:\n"
        "<html_sample>\n"
        f"{sample}\n"
        "</html_sample>\n"
        "Emit only the JSON object."
    )


def parse_schema_json(raw: str) -> dict[str, Any]:
    """Parse an LLM reply into a validated extraction schema dict.

    Handles OpenAI ``json_object`` responses (bare JSON) as well as fenced
    markdown (strip ```json ... ``` if present), then performs minimal
    validation: top-level keys present and every field carrying ``name`` +
    ``selector``.

    Raises:
        ValueError: if the JSON cannot be parsed or fails validation.
    """
    if not raw or not isinstance(raw, str):
        raise ValueError("empty LLM reply")

    text = raw.strip()
    # If the whole reply is one code fence, unwrap it; otherwise try bare JSON.
    # Also strip a bare fence that may sit around an otherwise JSON-looking body.
    fenced = _FENCE_RE.findall(text)
    if fenced and any(candidate.strip() for candidate in fenced):
        candidate = max(fenced, key=lambda s: len(s)).strip()
    else:
        candidate = text

    try:
        schema: Any = json.loads(candidate)
    except json.JSONDecodeError:
        # Fall back to extracting the first fenced JSON block when the reply
        # mixes prose with a fence that our regex didn't isolate cleanly.
        first_fence = _FENCE_RE.search(text)
        if not first_fence:
            raise ValueError(f"LLM reply is not valid JSON: {candidate[:120]!r}")
        try:
            schema = json.loads(first_fence.group(1).strip())
        except json.JSONDecodeError as exc:
            raise ValueError(f"LLM reply contains no parseable JSON: {exc}") from exc

    if not isinstance(schema, dict):
        raise ValueError(f"LLM schema must be a JSON object, got {type(schema).__name__}")

    base = schema.get("baseSelector")
    if not isinstance(base, str) or not base.strip():
        raise ValueError("schema missing a non-empty 'baseSelector' string")

    fields: Any = schema.get("fields")
    if not isinstance(fields, list) or not fields:
        raise ValueError("schema 'fields' must be a non-empty list")

    normalized: list[dict[str, Any]] = []
    for i, field in enumerate(fields):
        if not isinstance(field, dict):
            raise ValueError(f"field {i} must be an object, got {type(field).__name__}")
        name = field.get("name")
        selector = field.get("selector")
        if not isinstance(name, str) or not name.strip():
            raise ValueError(f"field {i} missing non-empty 'name'")
        if not isinstance(selector, str) or not selector.strip():
            raise ValueError(f"field {i} ({name!r}) missing non-empty 'selector'")
        ftype = field.get("type", "text")
        if isinstance(ftype, str):
            ftype = ftype
        elif isinstance(ftype, list):
            for step in ftype:
                if step not in _VALID_TYPES:
                    raise ValueError(f"field {i} ({name!r}) has unknown type step {step!r}")
        elif ftype not in _VALID_TYPES:
            raise ValueError(f"field {i} ({name!r}) has unknown type {ftype!r}")
        normalized.append(field)

    return {"baseSelector": base, "fields": normalized}


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # An endpoint redirect must not move the sample or bearer token to a
        # different destination, even when the destination also uses HTTPS.
        return None


def _validate_endpoint(base_url: str) -> None:
    if not isinstance(base_url, str) or len(base_url) > 2000 or any(
        ord(char) <= 32 or ord(char) == 127 for char in base_url
    ):
        raise ValueError("base_url must be a valid API endpoint")
    try:
        parts = urlsplit(base_url)
        valid_port = parts.port is None or 1 <= parts.port <= 65535
        host = parts.hostname or ""
        try:
            loopback = ipaddress.ip_address(host).is_loopback
        except ValueError:
            loopback = host.rstrip(".").lower() == "localhost"
        valid = (
            bool(host) and valid_port and parts.username is None
            and not parts.query and not parts.fragment
            and (parts.scheme == "https" or (parts.scheme == "http" and loopback))
        )
    except ValueError:
        valid = False
    if not valid:
        raise ValueError("base_url requires remote HTTPS or loopback HTTP, without embedded credentials")


def generate_schema(
    sample_html: str,
    fields: list[str],
    *,
    model: str = "auto",
    base_url: str = "http://localhost:3001/v1",
    api_key: str | None = None,
    timeout: float = 20.0,
) -> dict[str, Any]:
    """Perform ONE LLM call and return a reusable extraction schema.

    POSTs an OpenAI-compatible ``/chat/completions`` request to
    ``{base_url}/chat/completions`` via ``urllib.request`` (stdlib). Emits
    ``{"baseSelector": ..., "fields": [...]}`` with CSS selectors for each of the
    requested ``fields``. ``response_format={"type": "json_object"}`` is always
    sent; FreeLLMAPI-style routers ignore unknown fields so it is safe to include
    even when unsupported.

    Args:
        sample_html: Truncated sample of a representative page (kept small so the
            single token spend stays bounded).
        fields: The canonical field names the extractor must produce.
        model: Model name; defaults to ``"auto"`` (router picks).
        base_url: API base including the version prefix (e.g. ``.../v1``).
        api_key: Explicit bearer token, overriding the canonical LLM key
            configuration. A configured or explicit key is required.
        timeout: HTTP timeout in seconds.

    Returns:
        The validated extraction schema dict.

    Raises:
        ValueError: on unreachable endpoint, non-200 response, or any parse or
            validation failure, with a message naming the failure point.
    """
    api_key = get_llm_key(api_key)
    if not api_key:
        raise ValueError("api_key is required for schema generation")
    _validate_endpoint(base_url)
    if not isinstance(sample_html, str) or len(sample_html) > 1_000_000:
        raise ValueError("sample_html must be at most 1,000,000 characters")
    if not isinstance(fields, list) or not fields or len(fields) > _MAX_FIELDS:
        raise ValueError(f"fields must contain between 1 and {_MAX_FIELDS} entries")
    if not all(isinstance(f, str) and f.strip() and len(f) <= _MAX_FIELD_NAME_CHARS for f in fields):
        raise ValueError(f"field names must be non-empty strings up to {_MAX_FIELD_NAME_CHARS} characters")

    system = (
        "You emit ONLY valid JSON. Never include prose, explanations, or markdown "
        "outside an optional ```json code fence. Obey the user's requested schema "
        "format exactly."
    )

    payload: dict[str, Any] = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": _build_prompt(sample_html, fields)},
        ],
        "temperature": 0,
        "response_format": {"type": "json_object"},
    }

    endpoint = f"{base_url.rstrip('/')}/chat/completions"
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        endpoint,
        data=data,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
    )

    try:
        with urllib.request.build_opener(_NoRedirect()).open(req, timeout=timeout) as resp:
            body_bytes = resp.read(_MAX_LLM_RESPONSE_BYTES + 1)
            if len(body_bytes) > _MAX_LLM_RESPONSE_BYTES:
                raise ValueError("LLM endpoint response exceeds 2 MiB")
            body = body_bytes.decode("utf-8", errors="replace")
    except Exception as exc:  # URLError / HTTPError / timeout — unify as ValueError
        raise ValueError(f"LLM endpoint failed: {safe_error(exc, fallback_category='network')['error']}") from exc

    try:
        parsed: Any = json.loads(body)
    except json.JSONDecodeError as exc:
        raise ValueError("LLM endpoint returned a non-JSON response") from exc

    if not isinstance(parsed, dict):
        raise ValueError(f"LLM endpoint returned unexpected shape: {type(parsed).__name__}")

    # Extract the assistant message content (choices[0].message.content).
    choices = parsed.get("choices")
    if not isinstance(choices, list) or not choices:
        raise ValueError("LLM response missing 'choices'")
    message = choices[0].get("message", {}) if isinstance(choices[0], dict) else {}
    content = message.get("content")
    if not isinstance(content, str) or not content.strip():
        raise ValueError("LLM response carried no assistant message content")

    return parse_schema_json(content)


def generate_schema_from_url(
    url: str,
    fields: list[str],
    *,
    fetch: Callable[[str], str],
    **kw: Any,
) -> dict[str, Any]:
    """Fetch a sample page and generate an extraction schema from its HTML.

    Args:
        url: Page URL whose HTML is treated as the sample.
        fields: Canonical field names to extract.
        fetch: Required synchronous callable ``fetch(url) -> str html``. The
            caller enforces URL/redirect policy, deadlines and response bounds.
            This helper does not retrieve the page through an implicit fetcher.
        **kw: Keyword arguments forwarded to ``generate_schema``.

    Returns:
        Same as ``generate_schema``.

    Raises:
        TypeError: when ``fetch`` is missing or is not callable.
        ValueError: when the fetch result is not HTML text, or schema generation
            fails validation. Exceptions from ``fetch`` propagate unchanged.
    """
    if not callable(fetch):
        raise TypeError("fetch must be a callable (url -> html)")
    html = fetch(url)
    if not isinstance(html, str):
        raise ValueError(f"fetch callable returned {type(html).__name__}, expected str")
    return generate_schema(html, fields, **kw)


def schema_to_jsonl_docs(schema: dict[str, Any]) -> dict[str, Any]:
    """Return a compact, human-readable summary of a schema for docs/JSONL.

    Mirrors the schema's own structure (``baseSelector`` + field names) so it can
    be dropped into a doc line without leaking full selector details.

    Args:
        schema: An extraction schema dict (as returned by ``generate_schema``).

    Returns:
        ``{"baseSelector": ..., "fields": [names]}``.
    """
    return {
        "baseSelector": schema.get("baseSelector"),
        "fields": [f.get("name") for f in schema.get("fields", []) if isinstance(f, dict)],
    }
