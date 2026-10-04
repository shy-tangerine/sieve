import json
import sys

import pytest

from sieve import updater


def _payload(**overrides):
    value = {
        "installed": "1.0.0", "latest": "1.1.0", "update_available": True,
        "source": "package", "release_url": "https://example.test/release",
        "published": None, "release_notes": ["Breaking changes"],
        "available": True, "error": None,
    }
    value.update(overrides)
    return value


def test_check_release_rejects_malformed_metadata(monkeypatch):
    class Response:
        def read(self, _limit):
            return b"[]"
    monkeypatch.setattr(updater.urllib.request, "urlopen", lambda *a, **k: Response())
    result = updater.check_release()
    assert result["update_available"] is False
    assert result["error"] == "ValueError"


def test_update_check_json_is_stable(monkeypatch, capsys):
    monkeypatch.setattr(updater, "check_release", lambda: _payload())
    assert updater.update_command(check_only=True, json_output=True) == 0
    output = json.loads(capsys.readouterr().out)
    assert list(output) == ["available", "error", "installed", "latest", "published", "release_notes", "release_url", "source", "update_available"]
    assert len(output["release_notes"]) <= updater.MAX_RELEASE_NOTES


def test_update_requires_confirmation(monkeypatch, capsys):
    monkeypatch.setattr(updater, "check_release", lambda: _payload())
    monkeypatch.setattr(updater, "_source_checkout", lambda: False)
    assert updater.update_command(confirm=lambda: False) == 2
    assert "cancelled" in capsys.readouterr().out.lower()


def test_source_update_is_refused_before_prompt(monkeypatch, capsys):
    monkeypatch.setattr(updater, "check_release", lambda: _payload())
    monkeypatch.setattr(updater, "_source_checkout", lambda: True)
    assert updater.update_command(yes=True) == 2
    assert "source checkout" in capsys.readouterr().out.lower()


def test_update_rolls_back_when_verification_fails(monkeypatch, capsys):
    monkeypatch.setattr(updater, "check_release", lambda: _payload())
    monkeypatch.setattr(updater, "_source_checkout", lambda: False)
    commands = []
    monkeypatch.setattr(updater, "_run_pip", lambda cmd: (commands.append(cmd) or (0, "")))
    monkeypatch.setattr(updater, "_advanced", lambda *args: False)
    assert updater.update_command(yes=True) == 1
    assert len(commands) == 2
    assert "restored" in capsys.readouterr().out.lower()


def test_cli_update_check_json_is_read_only(monkeypatch, capsys):
    from sieve import cli

    monkeypatch.setattr(sys, "argv", ["sieve", "update", "check", "--json"])
    monkeypatch.setattr(updater, "check_release", lambda: _payload())

    def unexpected_mutation(*args):
        pytest.fail("read-only update check attempted installation")

    monkeypatch.setattr(updater, "_run_pip", unexpected_mutation)
    monkeypatch.setattr(updater, "_write_last_version", unexpected_mutation)
    assert cli.main() == 0
    assert json.loads(capsys.readouterr().out)["latest"] == "1.1.0"


@pytest.mark.parametrize("confirmation", [[], ["--yes"]])
def test_cli_update_apply_forwards_confirmation(monkeypatch, confirmation):
    from sieve import cli

    calls = []

    def update_command(**kwargs):
        calls.append(kwargs)
        return 2

    monkeypatch.setattr(sys, "argv", ["sieve", "update", "apply", *confirmation])
    monkeypatch.setattr(updater, "update_command", update_command)
    assert cli.main() == 2
    assert calls == [{"yes": bool(confirmation)}]


def test_cli_update_apply_refuses_source_checkout(monkeypatch, capsys):
    from sieve import cli

    monkeypatch.setattr(sys, "argv", ["sieve", "update", "apply", "--yes"])
    monkeypatch.setattr(updater, "check_release", lambda: _payload())
    monkeypatch.setattr(updater, "_source_checkout", lambda: True)

    def unexpected_pip(*args):
        pytest.fail("source checkout attempted package update")

    monkeypatch.setattr(updater, "_run_pip", unexpected_pip)
    assert cli.main() == 2
    assert "source checkout" in capsys.readouterr().out.lower()
