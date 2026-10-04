import json
import builtins
import subprocess
import sys
from pathlib import Path
import pytest

from sieve import __version__, cli


def test_media_command_routes_without_server_import(monkeypatch, capsys):
    from sieve import youtube
    monkeypatch.setattr(youtube, "search", lambda *args: {"ok": True, "entries": []})
    assert cli._run_media_command(["youtube", "search", "demo"]) == 0
    assert json.loads(capsys.readouterr().out)["ok"] is True


def test_cli_help_is_default_and_does_not_load_legacy_server(monkeypatch, capsys):
    monkeypatch.setattr(cli.sys, "argv", ["sieve"])
    assert cli.main() == 0
    output = capsys.readouterr().out
    assert output.startswith("Sieve - bounded media")
    assert "sieve youtube search" in output


def test_cli_version_does_not_load_legacy_server(monkeypatch, capsys):
    real_import = builtins.__import__

    def guarded_import(name, *args, **kwargs):
        if name == "sieve.server":
            raise AssertionError("--version imported the legacy server")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", guarded_import)
    monkeypatch.setattr(cli.sys, "argv", ["sieve", "--version"])
    assert cli.main() == 0
    assert capsys.readouterr().out == f"Sieve {__version__}\n"


def test_repair_script_embeds_project_root():
    script = cli._repair_script()
    assert repr(cli.PROJECT_ROOT) in script
    assert "os.environ.get(\"SIEVE_PROJECT_DIR\", PROJECT_ROOT)" not in script


def test_media_failures_are_json_on_stdout(capsys):
    assert cli._run_media_command(["youtube", "metadata", "https://example.com/video"]) == 1
    output = capsys.readouterr()
    assert json.loads(output.out)["ok"] is False
    assert output.err == ""

def test_prefixed_media_name_is_rejected(monkeypatch, capsys):
    assert cli._run_media_command(["mcp-media", "download", "https://example.com/v"]) is None


def test_python_module_propagates_stt_input_error_exit_status():
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "sieve",
            "media",
            "transcribe",
            "https://example.com/video",
            "--timeout",
            "-1",
        ],
        cwd=Path(__file__).parents[1],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 1
    assert result.stderr == ""
    payload = json.loads(result.stdout)
    assert payload["ok"] is False
    assert payload["category"] == "input"
    assert payload["error"] == "Invalid input."


@pytest.mark.parametrize("source", ["probe", "config"])
def test_stt_diagnostics_round_trip_without_exception_values(monkeypatch, capsys, tmp_path, source):
    from sieve import config, stt, youtube

    config.take_diagnostics()
    monkeypatch.setattr(config, "_CACHE", None)
    monkeypatch.delenv("SIEVE_STT_EXECUTABLE", raising=False)
    bad = tmp_path / "private-config.toml"
    bad.write_text('token = "sk-live-secret-1234567890"\ninvalid ===')
    monkeypatch.setattr(config, "CONFIG_PATH", str(bad))
    monkeypatch.setattr(stt, "_validate_url", lambda value: value)
    monkeypatch.setattr(stt, "_model_name", lambda value: "base")
    def probe(*args, **kwargs):
        if source == "probe":
            raise OSError("private=/private/session.json token=sk-live-secret-1234567890")
        return "1"
    monkeypatch.setattr(youtube, "_run", probe)
    monkeypatch.setattr(stt, "_fetch_captions", lambda *a: None)
    monkeypatch.setattr(stt, "_download_audio", lambda *a: tmp_path / "audio.wav")
    monkeypatch.setattr(stt, "_transcribe_with_whisper", lambda *a: {"text": "Good transcription"})
    assert cli._run_media_command(["media", "transcribe", "https://example.test/audio"]) == 0
    output = capsys.readouterr()
    result = json.loads(output.out)
    assert result["text"] == "Good transcription"
    assert result["diagnostics"]
    assert "alice" not in output.out + output.err
    assert "sk-live-secret" not in output.out + output.err
    assert "private-config" not in output.out + output.err
    assert config.take_diagnostics() == []
