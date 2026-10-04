from sieve import social


def test_explicit_backend_wins(monkeypatch):
    monkeypatch.setenv("SIEVE_BROWSER_BACKEND", "sleeper")
    assert social.resolve_browser_backend("pool") == "pool"
    monkeypatch.delenv("SIEVE_BROWSER_BACKEND")
    assert social.resolve_browser_backend("sleeper") == "sleeper"


def test_env_backend(monkeypatch):
    monkeypatch.setenv("SIEVE_BROWSER_BACKEND", "pool")
    assert social.resolve_browser_backend(None) == "pool"


def test_auto_falls_back_to_pool_without_daemon(monkeypatch):
    monkeypatch.delenv("SIEVE_BROWSER_BACKEND", raising=False)
    monkeypatch.setattr("sieve.sleeper_bridge.is_available", lambda: False)
    assert social.resolve_browser_backend(None) == "pool"


def test_auto_picks_sleeper_when_daemon_up(monkeypatch):
    monkeypatch.delenv("SIEVE_BROWSER_BACKEND", raising=False)
    monkeypatch.setattr("sieve.sleeper_bridge.is_available", lambda: True)
    assert social.resolve_browser_backend(None) == "sleeper"


def test_fetch_browser_sleeper_path(monkeypatch):
    monkeypatch.setattr("sieve.social._sleeper_html", lambda *a, **k: "<html><body>hi</body></html>")
    record = social.fetch_browser("https://www.tiktok.com/@h/video/123", backend="sleeper")
    assert record["provenance"]["fetcher_used"] == "sleeper"
