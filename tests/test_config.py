import os

from sieve import config


def test_precedence_env_over_file(tmp_path, monkeypatch):
    cfg = tmp_path / "c.toml"
    cfg.write_text('browser_backend = "pool"\n')
    assert config.load(str(cfg))["browser_backend"] == "pool"
    monkeypatch.setattr(config, "_CACHE", {"browser_backend": "pool"}, raising=False)
    try:
        monkeypatch.setenv("SIEVE_BROWSER_BACKEND", "sleeper")
        assert config.get("browser_backend", "auto", env="SIEVE_BROWSER_BACKEND") == "sleeper"
        monkeypatch.delenv("SIEVE_BROWSER_BACKEND")
        assert config.get("browser_backend", "auto", env="SIEVE_BROWSER_BACKEND") == "pool"
    finally:
        monkeypatch.setattr(config, "_CACHE", None, raising=False)


def test_save_roundtrip_toml_types(tmp_path):
    cfg = tmp_path / "c.toml"
    config.save({"a": "x", "n": 3, "flag": True}, path=str(cfg))
    back = config.load(str(cfg))
    assert back == {"a": "x", "n": 3, "flag": True}


def test_save_escapes_paths_and_replaces_atomically(tmp_path):
    cfg = tmp_path / "c.toml"
    value = r"C:\Users\agent\Sieve\new\profile"
    config.save({"profile": value}, path=str(cfg))
    assert config.load(str(cfg))["profile"] == value
    assert not list(tmp_path.glob(".config.*.tmp"))


def test_missing_file_is_empty(tmp_path):
    assert config.load(str(tmp_path / "nope.toml")) == {}


def test_social_gate_reads_config(tmp_path, monkeypatch):
    from sieve import social
    monkeypatch.delenv("SIEVE_ENABLE_SOCIAL_COLLECT", raising=False)
    monkeypatch.setattr(config, "_CACHE", {"enable_social_collect": True}, raising=False)
    social._require_social_collect()  # must not raise
    monkeypatch.setattr(config, "_CACHE", {}, raising=False)
    try:
        social._require_social_collect()
    except RuntimeError:
        pass
    else:
        raise AssertionError("gate should block")
    finally:
        monkeypatch.setattr(config, "_CACHE", None, raising=False)


def test_backend_from_config(tmp_path, monkeypatch):
    from sieve import social
    monkeypatch.delenv("SIEVE_BROWSER_BACKEND", raising=False)
    monkeypatch.setattr("sieve.sleeper_bridge.is_available", lambda: False)
    monkeypatch.setattr(config, "_CACHE", {"browser_backend": "pool"}, raising=False)
    try:
        assert social.resolve_browser_backend(None) == "pool"
    finally:
        monkeypatch.setattr(config, "_CACHE", None, raising=False)
    _ = tmp_path
    _ = os.sep


# ---- issue #233: typed diagnostics for config discovery ----


def _reset_config_state():
    config._CACHE = None
    config.take_diagnostics()


def test_missing_config_records_no_diagnostic(tmp_path):
    _reset_config_state()
    try:
        assert config.load(str(tmp_path / "absent.toml")) == {}
        assert config.take_diagnostics() == []
    finally:
        _reset_config_state()


def test_unreadable_config_diagnostic_never_exposes_path_or_exception(monkeypatch):
    _reset_config_state()
    def fail(*args, **kwargs):
        raise PermissionError("token=sk-live-secret-1234567890 private=/private/config.toml")
    monkeypatch.setattr("builtins.open", fail)
    try:
        assert config.load("/private/config.toml") == {}
        diagnostics = config.take_diagnostics()
        assert diagnostics[0]["type"] == "config_unreadable"
        assert "alice" not in repr(diagnostics)
        assert "sk-live-secret" not in repr(diagnostics)
        assert config.take_diagnostics() == []
    finally:
        _reset_config_state()


def test_malformed_config_records_diagnostic(tmp_path):
    _reset_config_state()
    bad = tmp_path / "bad.toml"
    bad.write_text("not [valid toml ===")
    try:
        assert config.load(str(bad)) == {}
        diags = config.take_diagnostics()
        assert len(diags) == 1
        assert diags[0]["type"] == "config_malformed"
        assert diags[0]["category"] == "configuration"
        assert "path" not in diags[0]
        assert diags[0]["detail"] == "Configuration could not be read."
    finally:
        _reset_config_state()


def test_unreadable_config_records_diagnostic(tmp_path):
    _reset_config_state()
    locked = tmp_path / "locked.toml"
    locked.write_text("a = 1\n")
    locked.chmod(0o000)
    try:
        config.load(str(locked))
        diags = config.take_diagnostics()
        assert diags and diags[0]["type"] == "config_unreadable"
    finally:
        locked.chmod(0o644)
        _reset_config_state()


def test_take_diagnostics_drains(tmp_path):
    _reset_config_state()
    bad = tmp_path / "bad.toml"
    bad.write_text("===")
    try:
        config.load(str(bad))
        assert config.take_diagnostics()
        assert config.take_diagnostics() == []
    finally:
        _reset_config_state()
