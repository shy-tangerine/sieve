"""Issue #251 + #252: public-output secret redaction and bounded serialization."""

import json
import tracemalloc

import pytest


from sieve.public_output import (
    MAX_PUBLIC_OUTPUT_BYTES,
    bounded_json_dumps,
    redact_secrets,
    safe_public_json,
    safe_error,
    safe_diagnostic,
    failure_payload,
)


def test_url_fragment_secrets_are_redacted_without_removing_sections():
    value = safe_public_json({"url": "https://example.com/callback#access_token=fragment-sentinel&section=intro"})
    assert "fragment-sentinel" not in value
    assert "section=intro" in value
    assert "#intro" in safe_public_json({"url": "https://example.com/page#intro"})


def test_safe_error_ignores_backend_values_and_unrecognized_categories():
    class Failure(RuntimeError):
        category = "private=/private/session.json"
        safe_message = "token=sk-live-secret-1234567890"
        def __str__(self):
            raise AssertionError("exception values must not be inspected")

    assert safe_error(Failure()) == {"category": "internal", "error": "The operation failed."}
    assert safe_error(Failure(), fallback_category="extract") == {
        "category": "extract", "error": "Content extraction failed.",
    }


# ---- #251: recursive secret scan ----

def test_safe_error_survives_broken_backend_category():
    class Failure(RuntimeError):
        @property
        def category(self):
            raise RuntimeError("private diagnostic")

    assert safe_error(Failure()) == {"category": "internal", "error": "The operation failed."}


def test_diagnostics_use_only_fixed_vocabulary_and_do_not_read_backend_values():
    class Failure(RuntimeError):
        category = ["private"]

        def __str__(self):
            pytest.fail("diagnostics must not inspect exception strings")

    diagnostic = safe_diagnostic(Failure(), route="/private/config", fetcher="https://user:secret@host/?token=secret", truncated=["nodes", "secret", "nodes"])
    assert diagnostic == {"category": "internal", "safe_message": "The operation failed.", "retryable": False,
                          "route": "", "fetcher": "", "truncated": ["nodes"]}
    assert safe_diagnostic(TimeoutError("sentinel"))["retryable"] is True
    assert safe_diagnostic(ValueError("sentinel"))["retryable"] is False
    assert "sentinel" not in safe_public_json(failure_payload(TimeoutError("sentinel")))


def test_failure_and_truncation_diagnostics_are_shared_at_serialization_boundary():
    data = json.loads(safe_public_json({"error": "The operation timed out.", "category": "timeout", "is_truncated": True}))
    assert data["diagnostic"]["category"] == "timeout"
    assert data["diagnostic"]["retryable"]
    assert data["diagnostic"]["truncated"] == ["output_chars"]

def test_secret_keys_redacted_at_any_depth():
    payload = {
        "config": {
            "password": "hunter2",
            "api_key": "sk-123",
            "nested": {"Authorization": "Bearer abc", "cookie": "a=b"},
        },
        "safe": "value",
    }
    out = redact_secrets(payload)
    assert out["config"]["password"] == "[REDACTED]"
    assert out["config"]["api_key"] == "[REDACTED]"
    assert out["config"]["nested"]["Authorization"] == "[REDACTED]"
    assert out["config"]["nested"]["cookie"] == "[REDACTED]"
    assert out["safe"] == "value"


def test_standalone_credential_values_redacted():
    out = redact_secrets({"header": "Bearer abcdefghijklmno", "note": "bearer token discussion"})
    assert out["header"] == "[REDACTED]"
    # Prose mentioning 'bearer' is not itself a credential.
    assert out["note"] == "bearer token discussion"


def test_proxy_url_with_credential_component():
    # Proxy URLs are credential-bearing;userinfo must not leak verbatim.
    out = redact_secrets({"url": "https://alice:sentinel@proxy.example.org:8443/a?x=1", "ok_field": "http://example.com"})
    assert out["url"] == "https://proxy.example.org:8443/a?x=1"
    assert "sentinel" not in safe_public_json(out)
    assert out["ok_field"] == "http://example.com"


def test_redact_does_not_mutate_input():
    payload = {"token": "abc"}
    redact_secrets(payload)
    assert payload["token"] == "abc"


# ---- #252: bounded serialization ----

def test_small_payload_passes_through():
    text = bounded_json_dumps({"a": 1})
    assert json.loads(text) == {"a": 1}


def test_oversized_string_truncated_structurally():
    text = bounded_json_dumps({"blob": "x" * (MAX_PUBLIC_OUTPUT_BYTES + 10)},
                              max_bytes=1000)
    data = json.loads(text)
    assert data["blob"].startswith("x")
    assert "_truncated" in data["blob"]
    assert len(text.encode("utf-8")) <= 1000


def test_oversized_list_truncated_with_marker():
    text = bounded_json_dumps({"items": list(range(100000))}, max_bytes=2000)
    data = json.loads(text)
    assert len(text.encode("utf-8")) <= 2000
    tail = data["items"][-1]
    assert isinstance(tail, dict) and tail.get("_truncated") is True


def test_budget_never_exceeded_deep_nesting():
    deep = current = {}
    for _ in range(50):
        current["child"] = {"blob": "y" * 5000}
        current = current["child"]
    text = bounded_json_dumps(deep, max_bytes=500)
    assert len(text.encode("utf-8")) <= 500
    assert json.loads(text)  # remains valid JSON


def test_safe_public_json_combines_both_contracts():
    text = safe_public_json({"password": "hunter2", "data": "z" * 5000},
                            max_bytes=800)
    data = json.loads(text)
    assert data["password"] == "[REDACTED]"
    assert len(text.encode("utf-8")) <= 800


@pytest.mark.parametrize("ensure_ascii", [True, False])
def test_utf8_and_escaped_strings_respect_budget(ensure_ascii):
    text = bounded_json_dumps({"text": '\"\\\n界😀' * 10000}, max_bytes=200, ensure_ascii=ensure_ascii)
    assert len(text.encode("utf-8")) <= 200
    assert "_truncated" in json.loads(text)["text"]


@pytest.mark.parametrize("budget", [19, 20, 32, 64])
def test_small_valid_budgets_keep_json_and_marker(budget):
    text = safe_public_json({"data": ["x" * 1000] * 100}, max_bytes=budget)
    assert len(text.encode("utf-8")) <= budget
    assert "_truncated" in text
    assert json.loads(text)


@pytest.mark.parametrize("budget", [0, -1, 18, True, 3.5])
def test_invalid_budgets_are_rejected(budget):
    with pytest.raises((TypeError, ValueError)):
        safe_public_json({}, max_bytes=budget)


def test_safe_output_redacts_future_fields_without_mutation():
    payload = {"nested": {"future_password": "sentinel", "accessToken": "sentinel"},
               "url": "https://alice:sentinel@example.org/path", "header": "Basic c2VudGluZWw=",
               "article": "An article about cookies and password managers."}
    text = safe_public_json(payload)
    assert "sentinel" not in text
    assert json.loads(text)["article"] == payload["article"]
    assert payload["nested"]["future_password"] == "sentinel"


def test_credentials_in_error_urls_and_query_parameters_are_redacted():
    payload = {"error": "fetch failed for https://user:sentinel@example.org/a?api_key=sentinel&view=a%2Fb",
               "url": "https://example.org?access_token=sentinel&sort=ascending"}
    text = safe_public_json(payload)
    assert "sentinel" not in text
    assert "view=a%2Fb" in text
    assert "sort=ascending" in text


def test_large_input_has_budget_proportional_allocations():
    payload = {"blob": "x" * 20_000_000, "rows": [0] * 100_000}
    tracemalloc.start()
    try:
        text = safe_public_json(payload, max_bytes=1000)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert len(text.encode("utf-8")) <= 1000
    assert peak < 200_000


def test_wide_mapping_stops_visiting_dropped_items():
    visited = []

    class WideDict(dict):
        def items(self):
            for entry in super().items():
                visited.append(entry[0])
                yield entry

    payload = WideDict((str(i), {"data": "x" * 100}) for i in range(10000))
    text = safe_public_json(payload, max_bytes=500)
    assert len(visited) < 20
    assert len(text.encode("utf-8")) <= 500
    assert json.loads(text)["_truncated"] is True


def test_model_expansion_respects_exclusions_without_full_dump():
    from pydantic import BaseModel, Field

    class Result(BaseModel):
        status: int = 200
        private_value: str = Field(default="sentinel", exclude=True)
        blob: str

        def model_dump(self, **kwargs):
            pytest.fail("bounded serialization must not copy the whole model")

    result = Result(blob="x" * 100000)
    text = safe_public_json(result, max_bytes=500)
    assert "sentinel" not in text
    assert "private_value" not in json.loads(text)
    assert json.loads(text)["status"] == 200
    assert len(text.encode("utf-8")) <= 500


def test_partial_url_authority_cannot_leak_credentials():
    text = safe_public_json({"error": "failure at https://alice:sentinel" + "x" * 10000 + "@example.org"}, max_bytes=200)
    assert "sentinel" not in text


@pytest.mark.parametrize("number", [float("nan"), float("inf"), float("-inf")])
def test_non_finite_numbers_produce_standard_json(number):
    def invalid_constant(value):
        pytest.fail("non-standard JSON constant: " + value)

    assert json.loads(safe_public_json({"number": number}), parse_constant=invalid_constant) == {"number": None}
def test_router_bounds_models_without_eager_dump():
    import asyncio
    from types import SimpleNamespace
    from pydantic import BaseModel
    from sieve.command_router import _dispatch_capability

    class Result(BaseModel):
        status: int = 200
        content: str = "x" * (MAX_PUBLIC_OUTPUT_BYTES * 2)
        password: str = "sentinel"

        def model_dump(self, *args, **kwargs):
            raise AssertionError("public routing must not expand the full model")

    async def fetch(**kwargs):
        return Result()

    content, structured = asyncio.run(_dispatch_capability(
        SimpleNamespace(smart_fetch=fetch), "smart_fetch", {"url": "https://example.org"},
    ))
    assert len(content[0].text.encode()) <= MAX_PUBLIC_OUTPUT_BYTES
    assert structured["status"] == 200
    assert "sentinel" not in content[0].text
