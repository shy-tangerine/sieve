from sieve import sleeper_bridge


def test_empty_render_is_failure_not_evidence(monkeypatch):
    calls = []

    def fake_command(cmd, args=None, tab=None, timeout=None):
        calls.append(cmd)
        if cmd == "read":
            return {"ok": True, "result": {"text": "   "}}
        return {"ok": True, "result": ""}

    monkeypatch.setattr(sleeper_bridge, "_command", fake_command)
    result = sleeper_bridge.sleeper_fetch("https://example.test/")
    assert result["ok"] is False
    assert result["text"] == ""
    assert "wait_selector" in result["error"]


def test_nonempty_render_still_passes(monkeypatch):
    def fake_command(cmd, args=None, tab=None, timeout=None):
        return {"ok": True, "result": {"text": "hello"}}

    monkeypatch.setattr(sleeper_bridge, "_command", fake_command)
    result = sleeper_bridge.sleeper_fetch("https://example.test/")
    assert result == {"ok": True, "url": "https://example.test/", "text": "hello"}


def test_api_config_cannot_expand_extension_allowlist(monkeypatch):
    from sieve import config
    seen = {}
    monkeypatch.setattr(config, "_CACHE", {"sleeper_api_hosts": ["example.com"]}, raising=False)
    def fake_command(cmd, args=None, tab=None, timeout=None):
        seen.update(args or {})
        return {"ok": True, "result": {"ok": 1}}
    monkeypatch.setattr(sleeper_bridge, "_command", fake_command)
    denied = sleeper_bridge.sleeper_api("https://example.com/api/me")
    assert denied["ok"] is False
    assert "not permitted" in denied["error"]
    assert seen == {}


def test_api_config_can_narrow_extension_allowlist(monkeypatch):
    from sieve import config
    seen = {}
    monkeypatch.setattr(config, "_CACHE", {"sleeper_api_hosts": ["chatgpt.com"]}, raising=False)

    def fake_command(cmd, args=None, tab=None, timeout=None):
        seen.update(args or {})
        return {"ok": True, "result": {"ok": 1}}

    monkeypatch.setattr(sleeper_bridge, "_command", fake_command)
    assert sleeper_bridge.sleeper_api("https://chatgpt.com/backend-api/me") == {"ok": 1}
    assert seen["allowed_hosts"] == ["chatgpt.com"]
