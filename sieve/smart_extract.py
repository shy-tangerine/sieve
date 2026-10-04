"""BYOK-gated LLM extraction (SmartScraper spike).

SPDX-License-Identifier: MIT

Fetch stays keyless (existing pipeline); only this step needs an operator
key. Key comes from the existing ``keys`` system (provider ``llm``:
``sieve keys add llm`` or ``SIEVE_LLM_KEYS`` env). Endpoint and model
are operator env (``SIEVE_LLM_BASE_URL``, ``SIEVE_LLM_MODEL`` default
``auto``). No bundled credentials, no hardcoded gateways. No key = error
pointing at ``sieve keys``.

Public API: ``smart_extract(content, prompt, ...) -> dict``.
"""

from __future__ import annotations

import json
import os
import urllib.request
from urllib.parse import urlparse

LLM_BASE_URL_ENV = "SIEVE_LLM_BASE_URL"
LLM_MODEL_ENV = "SIEVE_LLM_MODEL"
LLM_PROVIDER = "llm"

# ponytail: 8k char cap, callers needing full-page coverage pass focus=/chunk first
MAX_CONTENT_CHARS = 8000
# Bounded response read for the LLM endpoint (issue #33): an operator endpoint
# is still an agent-facing network capability; its response must be capped.
_MAX_LLM_RESPONSE_BYTES = 2 * 1024 * 1024


class SmartExtractError(ValueError):
    """Raised when smart extraction cannot run (no key, bad endpoint, bad LLM JSON)."""


def _load_key(explicit: str | None = None) -> str | None:
    # Canonical resolution via byok_config (issue #228): explicit arg ->
    # SIEVE_LLM_KEYS/config file -> legacy aliases (SIEVE_LLM_API_KEY,
    # FREELMAPI_API_KEY).
    from sieve.byok_config import get_llm_key

    return get_llm_key(explicit)


def _strip_fences(text: str) -> str:
    s = text.strip()
    if s.startswith("```"):
        s = s.split("\n", 1)[1] if "\n" in s else ""
        if s.rsplit("```", 1)[0].strip():
            s = s.rsplit("```", 1)[0]
    return s.strip()


def _default_post(base_url: str, api_key: str, model: str, prompt: str,
                  content: str, timeout: int) -> str:
    endpoint = base_url.rstrip("/") + "/chat/completions"
    # Scheme gate (issue #33): only http(s) endpoints; no file:, ftp:, or
    # custom schemes reaching urlopen.
    parsed = urlparse(endpoint)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise SmartExtractError(f"{LLM_BASE_URL_ENV} must be an http(s) endpoint")
    body = json.dumps({
        "model": model,
        "messages": [
            {"role": "system", "content": (
                "Extract structured data from the page content per the user "
                "instruction. Reply with a single JSON object only, no prose.")},
            {"role": "user", "content": f"Instruction: {prompt}\n\nPage content:\n{content}"},
        ],
        "temperature": 0,
    }).encode()
    req = urllib.request.Request(
        endpoint, data=body,
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {api_key}"}, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - scheme gated above
        payload = json.loads(resp.read(_MAX_LLM_RESPONSE_BYTES + 1).decode("utf-8", "replace"))
    try:
        return payload["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as e:
        raise SmartExtractError(f"unexpected LLM response shape: {str(payload)[:200]}") from e


def smart_extract(content: str, prompt: str, *, base_url: str | None = None,
                  model: str | None = None, api_key: str | None = None,
                  timeout: int = 60, _post=None) -> dict:
    """Extract ``prompt``-shaped JSON from page ``content`` via operator BYOK LLM."""
    if not prompt or not prompt.strip():
        raise SmartExtractError("`prompt` must be provided (--smart PROMPT)")
    key = _load_key(api_key)
    if not key:
        raise SmartExtractError(
            "smart extract needs an operator LLM key: "
            "`sieve keys add llm` (or set SIEVE_LLM_KEYS)")
    url = (base_url or os.environ.get(LLM_BASE_URL_ENV, "")).strip()
    if not url:
        raise SmartExtractError(
            f"smart extract needs an endpoint: set {LLM_BASE_URL_ENV} "
            "(e.g. your OpenAI-compatible gateway base URL)")
    mdl = (model or os.environ.get(LLM_MODEL_ENV, "") or "auto").strip()
    post = _post or _default_post
    try:
        raw = post(url, key, mdl, prompt.strip(), content[:MAX_CONTENT_CHARS], timeout)
    except SmartExtractError:
        raise
    except Exception as e:
        raise SmartExtractError(f"LLM call failed: {e}") from e
    try:
        data = json.loads(_strip_fences(raw or ""))
    except (json.JSONDecodeError, TypeError) as e:
        raise SmartExtractError(f"LLM did not return JSON: {(raw or '')[:200]}") from e
    if not isinstance(data, dict):
        raise SmartExtractError("LLM must return a JSON object")
    return data
