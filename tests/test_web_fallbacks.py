"""Deterministic tests for HTTP-200 shell and captured-XHR fallbacks."""

from sieve.server import ResponseModel, _agent_hints, _apply_capture_verdict


def _shell_result() -> ResponseModel:
    return ResponseModel(
        url="https://docs.example.test/registry",
        status=200,
        content=["Loading documentation panels…"],
        extracted_type="markdown",
        fetcher_used="stealthy",
        network={
            "primary_url": "https://docs.example.test/api/registry",
            "captured_count": 1,
            "fragments": [
                {
                    "url": "https://docs.example.test/api/registry",
                    "status": 200,
                    "content_type": "application/json",
                    "text": '{"servers":[{"name":"sieve"}]}',
                    "is_primary": True,
                }
            ],
        },
    )


def test_http_200_challenge_is_not_treated_as_usable_content():
    result = ResponseModel(
        url="https://pypi.example.test/project/scrapling",
        status=200,
        content=["Checking your browser before accessing this site"],
        extracted_type="markdown",
        fetcher_used="http",
        error="bot_challenge_detected: client challenge page",
    )

    summary, next_action, content_ok = _agent_hints(result)

    assert result.status == 200
    assert summary.startswith("200 OK")
    assert not content_ok
    assert "bot challenge" in next_action


def test_ajax_shell_preserves_success_status_but_requires_fragment_follow_up():
    result = _shell_result()
    _apply_capture_verdict(result, fold_captured=False, shell_detected=True)

    summary, next_action, content_ok = _agent_hints(result)

    assert result.status == 200
    assert summary.startswith("200 OK")
    assert not content_ok
    assert result.error.startswith("ajax_shell_detected:")
    assert "https://docs.example.test/api/registry" not in next_action
    assert "network.fragments" in next_action


def test_js_shell_is_explicitly_classified_as_blocked():
    from sieve.server import _annotate_quality
    result = _shell_result()
    result.fetcher_used = "stealthy"
    result.total_size_bytes = 5000
    result.content = ["app shell"]
    _annotate_quality(result)
    assert result.content_ok is False
    assert result.blocked == {"blocked": True, "kind": "js_shell_or_anti_bot"}


def test_fold_captured_promotes_primary_fragment_without_a_second_request():
    result = _shell_result()
    original_url = result.url
    _apply_capture_verdict(result, fold_captured=True, shell_detected=True)

    summary, next_action, content_ok = _agent_hints(result)

    assert result.url == original_url
    assert content_ok
    assert '"servers"' in "\n".join(result.content)
    assert result.error == ""
    assert summary.startswith("200 OK")
    assert next_action == ""
