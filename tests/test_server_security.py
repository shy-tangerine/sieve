from sieve.server import MasterFetchServer, _valid_bearer_authorization


def test_bearer_auth_is_case_insensitive_and_exact():
    assert _valid_bearer_authorization([(b"Authorization", b"Bearer secret")], "secret")
    assert not _valid_bearer_authorization([(b"authorization", b"Bearer wrong")], "secret")


def test_bearer_auth_rejects_ambiguous_headers():
    headers = [(b"authorization", b"Bearer secret"), (b"authorization", b"Bearer secret")]
    assert not _valid_bearer_authorization(headers, "secret")
    assert _valid_bearer_authorization([], "")


def test_non_loopback_http_without_token_refuses_to_start(monkeypatch):
    """Runtime auth invariant (issues #79/#154): a non-loopback HTTP bind
    without SIEVE_AUTH_TOKEN must refuse to start, not serve unauthenticated."""
    import pytest

    monkeypatch.delenv("SIEVE_AUTH_TOKEN", raising=False)
    with pytest.raises(SystemExit, match="SIEVE_AUTH_TOKEN"):
        MasterFetchServer().serve(http=True, host="0.0.0.0", port=8765)


def test_loopback_http_without_token_is_allowed(monkeypatch):
    """Localhost-only trust (the documented default) passes the invariant.

    serve() proceeds into the HTTP machinery and may exit for unrelated env
    reasons (uvicorn startup); we assert the SystemExit message is NOT the
    auth-invariant refusal.
    """
    monkeypatch.delenv("SIEVE_AUTH_TOKEN", raising=False)
    server = MasterFetchServer()
    monkeypatch.setattr(server, "build_mcp_server", lambda: object())
    try:
        server.serve(http=True, host="127.0.0.1", port=8765)
    except SystemExit as exc:
        assert "SIEVE_AUTH_TOKEN" not in str(exc), (
            f"loopback bind must pass the auth invariant: {exc}"
        )
    except Exception:
        pass  # server machinery failure is fine; invariant passed


def test_non_loopback_http_with_token_starts(monkeypatch):
    """With SIEVE_AUTH_TOKEN set, a non-loopback bind passes the invariant."""
    monkeypatch.setenv("SIEVE_AUTH_TOKEN", "secret")
    server = MasterFetchServer()
    monkeypatch.setattr(server, "build_mcp_server", lambda: object())
    try:
        server.serve(http=True, host="0.0.0.0", port=8765)
    except SystemExit as exc:
        assert "SIEVE_AUTH_TOKEN" not in str(exc), (
            f"token-protected bind must pass the auth invariant: {exc}"
        )
    except Exception:
        pass  # server machinery failure is fine; invariant passed
