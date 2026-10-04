import base64
from pathlib import Path

from sieve import sleeper_bridge


def _png_data_url():
    return "data:image/png;base64," + base64.b64encode(b"\x89PNG\r\n\x1a\n-fake").decode()


def test_sleeper_shot_saves_png(tmp_path, monkeypatch):
    calls = []

    def fake_command(cmd, args=None, tab=None, timeout=None):
        calls.append(cmd)
        if cmd == "goto":
            return {"ok": True}
        if cmd == "wait_url":
            return {"ok": True}
        if cmd == "shot":
            return {"ok": True, "result": {"dataUrl": _png_data_url()}}
        raise AssertionError(cmd)

    monkeypatch.setattr(sleeper_bridge, "_command", fake_command)
    # Confine HOME to tmp so the safe boundary (#193) points inside tmp_path.
    monkeypatch.setenv("HOME", str(tmp_path))
    out = tmp_path / ".sieve" / "downloads" / "shot.png"
    result = sleeper_bridge.sleeper_shot("https://example.com", out_path=str(out))
    assert result["ok"] is True, result
    written = Path(result["path"])
    assert written.exists()
    assert written.read_bytes() == b"\x89PNG\r\n\x1a\n-fake"
    assert calls[0] == "goto"


def test_sleeper_shot_rejects_outside_boundary(tmp_path, monkeypatch):
    """#193: out_path outside ~/.sieve/downloads is rejected, not written."""

    def fake_command(cmd, args=None, tab=None, timeout=None):
        if cmd == "shot":
            return {"ok": True, "result": {"dataUrl": _png_data_url()}}
        return {"ok": True}

    monkeypatch.setattr(sleeper_bridge, "_command", fake_command)
    monkeypatch.setenv("HOME", str(tmp_path))
    escape = tmp_path / "elsewhere" / "shot.png"
    result = sleeper_bridge.sleeper_shot("https://example.com", out_path=str(escape))
    assert result["ok"] is False
    assert "confined" in result["error"]
    assert not escape.exists()


def test_sleeper_shot_rejects_symlink_escape(tmp_path, monkeypatch):
    """#193: a symlink at the target path is rejected before overwrite."""

    def fake_command(cmd, args=None, tab=None, timeout=None):
        if cmd == "shot":
            return {"ok": True, "result": {"dataUrl": _png_data_url()}}
        return {"ok": True}

    monkeypatch.setattr(sleeper_bridge, "_command", fake_command)
    monkeypatch.setenv("HOME", str(tmp_path))
    downloads = tmp_path / ".sieve" / "downloads"
    downloads.mkdir(parents=True)
    victim = tmp_path / "victim.txt"
    victim.write_text("do not clobber")
    link = downloads / "shot.png"
    link.symlink_to(victim)
    result = sleeper_bridge.sleeper_shot("https://example.com", out_path=str(link))
    assert result["ok"] is False
    assert victim.read_text() == "do not clobber"


def test_sleeper_shot_propagates_nav_failure(monkeypatch):
    monkeypatch.setattr(sleeper_bridge, "_command", lambda *a, **k: {"ok": False, "error": "boom"})
    result = sleeper_bridge.sleeper_shot("https://example.com")
    assert result["ok"] is False
