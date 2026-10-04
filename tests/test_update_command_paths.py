"""Isolated package-path tests for the updater (#156, with #19/#144 docs).

``update_command`` is exercised through stubbed release metadata and a
recorded fake ``_run_pip`` — never against this checkout or the operator
installation. Covers: successful package update, cancellation, offline
release check, verification failure with rollback, and successful
verification recording the previous version.
"""
from __future__ import annotations

import json

import pytest

from sieve import updater


def _payload(**overrides):
    value = {
        "installed": "1.0.0", "latest": "1.1.0", "update_available": True,
        "source": "package", "release_url": "https://example.test/release",
        "published": None, "release_notes": ["Fixed things"],
        "available": True, "error": None,
    }
    value.update(overrides)
    return value


@pytest.fixture()
def isolated(monkeypatch):
    """Stub the release check and record every pip invocation."""
    commands = []
    monkeypatch.setattr(updater, "check_release", lambda: _payload())
    monkeypatch.setattr(updater, "_source_checkout", lambda: False)
    monkeypatch.setattr(updater, "_write_last_version", lambda v: commands.append(("record", v)))
    return commands


class TestPackageUpdatePaths:
    def test_successful_update_records_previous_version(self, isolated, monkeypatch, capsys):
        pip_calls = []

        def fake_pip(cmd):
            pip_calls.append(cmd)
            return (0, "")

        monkeypatch.setattr(updater, "_run_pip", fake_pip)
        from importlib.metadata import version as metadata_version
        monkeypatch.setattr("importlib.metadata.version",
                            lambda name: "1.1.0" if name == "sieve-cli" else metadata_version(name))
        monkeypatch.setattr(updater, "_advanced", lambda actual, latest: True)

        assert updater.update_command(yes=True) == 0
        # One install, one last-version record; no rollback.
        assert len(pip_calls) == 1
        assert "sieve-cli==1.1.0" in pip_calls[0]
        assert ("record", "1.0.0") in isolated
        assert "updated to 1.1.0" in capsys.readouterr().out.lower()

    def test_pip_failure_reports_diagnosis_without_recording(self, isolated, monkeypatch, capsys):
        monkeypatch.setattr(updater, "_run_pip", lambda cmd: (1, "resolution error"))
        assert updater.update_command(yes=True) == 1
        assert ("record", "1.0.0") not in isolated
        assert "update failed" in capsys.readouterr().out.lower()

    def test_verification_failure_rolls_back_to_previous(self, isolated, monkeypatch, capsys):
        pip_calls = []

        def fake_pip(cmd):
            pip_calls.append(cmd)
            return (0, "")

        monkeypatch.setattr(updater, "_run_pip", fake_pip)
        from importlib.metadata import version as metadata_version
        monkeypatch.setattr("importlib.metadata.version",
                            lambda name: "1.0.0" if name == "sieve-cli" else metadata_version(name))
        monkeypatch.setattr(updater, "_advanced", lambda actual, latest: False)

        assert updater.update_command(yes=True) == 1
        # Install + rollback pip commands, in that order.
        assert len(pip_calls) == 2
        assert "sieve-cli==1.1.0" in pip_calls[0]
        assert "sieve-cli==1.0.0" in pip_calls[1]
        assert "previous version restored" in capsys.readouterr().out.lower()

    def test_verification_rollback_failure_tells_operator(self, isolated, monkeypatch, capsys):
        codes = iter([(0, ""), (1, "no such version")])
        monkeypatch.setattr(updater, "_run_pip", lambda cmd: next(codes))
        from importlib.metadata import version as metadata_version
        monkeypatch.setattr("importlib.metadata.version",
                            lambda name: "1.0.0" if name == "sieve-cli" else metadata_version(name))
        monkeypatch.setattr(updater, "_advanced", lambda actual, latest: False)

        assert updater.update_command(yes=True) == 1
        out = capsys.readouterr().out
        assert "recovery failed" in out.lower()
        assert "sieve --rollback" in out

    def test_cancellation_never_runs_pip(self, isolated, monkeypatch, capsys):
        def fail_pip(cmd):
            raise AssertionError("pip ran without approval")

        monkeypatch.setattr(updater, "_run_pip", fail_pip)
        assert updater.update_command(confirm=lambda: False) == 2
        assert "cancelled" in capsys.readouterr().out.lower()


class TestOfflineCheck:
    def test_offline_check_is_read_only_and_exit_zero(self, monkeypatch, capsys):
        def offline_fetch(*a, **k):
            raise OSError("no network")

        monkeypatch.setattr(updater.urllib.request, "urlopen", offline_fetch)
        result = updater.check_release()
        assert result["error"] == "URLError" or result["error"]
        assert result["update_available"] is False
        assert result["available"] is False
        # The read-only check command treats an offline host as not-an-error.
        monkeypatch.setattr(updater, "check_release", lambda: result)
        assert updater.update_command(check_only=True) == 0
        assert "unavailable" in capsys.readouterr().out.lower()
