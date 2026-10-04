from sieve import capabilities


def test_detect_reports_only_real_executables(monkeypatch, tmp_path):
    executable = tmp_path / "whisper"
    executable.write_text("#!/bin/sh\n")
    executable.chmod(0o755)
    monkeypatch.setattr(capabilities.shutil, "which", lambda name: str(executable) if name == "whisper" else None)
    result = capabilities.detect()
    assert result["transcription"] == [{"name": "whisper", "path": str(executable)}]
    assert result["yt_dlp"] is None


def test_cdp_discovery_accepts_local_endpoint_only(monkeypatch):
    monkeypatch.setenv("SIEVE_CDP_URL", "https://example.com/devtools")
    assert capabilities.detect()["cdp_endpoint"] is None
    monkeypatch.setenv("SIEVE_CDP_URL", "http://127.0.0.1:9222")
    assert capabilities.detect()["cdp_endpoint"] == "http://127.0.0.1:9222"

