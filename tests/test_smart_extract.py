"""Mocked-provider tests for BYOK-gated smart extraction (no network)."""

import pytest

from sieve.smart_extract import SmartExtractError, smart_extract


def _ok(url, key, model, prompt, content, timeout):
    assert url == "http://llm.local/v1"
    assert key == "k123"
    assert prompt and content
    return '{"title": "Acme Widget", "price": "$9.99"}'


def test_smart_extract_ok(monkeypatch):
    monkeypatch.setenv("SIEVE_LLM_KEYS", "k123")
    out = smart_extract("page text", "give title+price",
                        base_url="http://llm.local/v1", _post=_ok)
    assert out == {"title": "Acme Widget", "price": "$9.99"}


def test_smart_extract_strips_fences(monkeypatch):
    monkeypatch.setenv("SIEVE_LLM_KEYS", "k123")
    out = smart_extract("t", "p", base_url="http://llm.local/v1",
                        _post=lambda *a: '```json\n{"a": 1}\n```')
    assert out == {"a": 1}


def test_smart_extract_no_key_errors(monkeypatch):
    monkeypatch.delenv("SIEVE_LLM_KEYS", raising=False)
    for alias in ("SIEVE_LLM_API_KEY", "FREELMAPI_API_KEY"):
        monkeypatch.delenv(alias, raising=False)
    import sieve.byok_config as bc
    monkeypatch.setattr(bc, "_read_config_file", lambda: {})
    with pytest.raises(SmartExtractError, match="sieve keys add llm"):
        smart_extract("t", "p", base_url="http://llm.local/v1", _post=_ok)


def test_smart_extract_legacy_alias_fallback(monkeypatch):
    """Issue #228: legacy SIEVE_LLM_API_KEY still satisfies key resolution."""
    monkeypatch.delenv("SIEVE_LLM_KEYS", raising=False)
    monkeypatch.setenv("SIEVE_LLM_API_KEY", "legacy-key")
    seen = {}

    def spy(url, key, model, prompt, content, timeout):
        seen["key"] = key
        return '{"a": 1}'

    out = smart_extract("t", "p", base_url="http://llm.local/v1", _post=spy)
    assert out == {"a": 1}
    assert seen["key"] == "legacy-key"


def test_get_llm_key_resolution_order(monkeypatch):
    """Issue #228: explicit arg > canonical keys > legacy aliases."""
    from sieve.byok_config import get_llm_key

    monkeypatch.delenv("SIEVE_LLM_KEYS", raising=False)
    monkeypatch.delenv("SIEVE_LLM_API_KEY", raising=False)
    monkeypatch.delenv("FREELMAPI_API_KEY", raising=False)
    import sieve.byok_config as bc
    monkeypatch.setattr(bc, "_read_config_file", lambda: {})

    # Nothing configured -> None.
    assert get_llm_key() is None

    # Legacy alias satisfies when canonical sources are empty.
    monkeypatch.setenv("FREELMAPI_API_KEY", "freelm-key")
    assert get_llm_key() == "freelm-key"

    # Canonical SIEVE_LLM_KEYS beats the legacy alias.
    monkeypatch.setenv("SIEVE_LLM_KEYS", "canonical-key")
    assert get_llm_key() == "canonical-key"

    # Explicit argument beats everything.
    assert get_llm_key("explicit-key") == "explicit-key"


@pytest.mark.parametrize("explicit,canonical,file_keys,legacy,expected", [
    (" explicit-fixture ", "canonical-fixture", ["file-fixture"], "legacy-fixture", "explicit-fixture"),
    (None, " canonical-first , canonical-second ", ["file-fixture"], "legacy-fixture", "canonical-first"),
    (None, None, ["file-first", "file-second"], "legacy-fixture", "file-first"),
    (None, None, [], "legacy-fixture", "legacy-fixture"),
    ("  ", None, [], None, "gateway-fixture"),
])
def test_llm_key_competing_sources_are_deterministic_and_secret_safe(
    monkeypatch, caplog, explicit, canonical, file_keys, legacy, expected,
):
    import sieve.byok_config as bc

    for name, value in (("SIEVE_LLM_KEYS", canonical), ("SIEVE_LLM_API_KEY", legacy), ("FREELMAPI_API_KEY", "gateway-fixture")):
        if value is None:
            monkeypatch.delenv(name, raising=False)
        else:
            monkeypatch.setenv(name, value)
    monkeypatch.setattr(bc, "_read_config_file", lambda: {"llm": file_keys} if file_keys else {})
    assert bc.get_llm_key(explicit) == expected
    for secret in ("explicit-fixture", "canonical-first", "canonical-second", "file-first", "file-second", "legacy-fixture", "gateway-fixture"):
        assert secret not in caplog.text
    if expected in {"legacy-fixture", "gateway-fixture"}:
        assert "deprecated" in caplog.text
        assert "sieve keys add llm" in caplog.text
    else:
        assert not caplog.text


def test_smart_extract_no_endpoint_errors(monkeypatch):
    monkeypatch.setenv("SIEVE_LLM_KEYS", "k123")
    monkeypatch.delenv("SIEVE_LLM_BASE_URL", raising=False)
    with pytest.raises(SmartExtractError, match="SIEVE_LLM_BASE_URL"):
        smart_extract("t", "p", _post=_ok)


def test_smart_extract_non_json_errors(monkeypatch):
    monkeypatch.setenv("SIEVE_LLM_KEYS", "k123")
    with pytest.raises(SmartExtractError, match="did not return JSON"):
        smart_extract("t", "p", base_url="http://llm.local/v1",
                      _post=lambda *a: "no json here")
