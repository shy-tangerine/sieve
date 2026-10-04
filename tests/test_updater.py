import inspect
import shutil

from sieve import updater


def test_update_commands_target_sieve_distribution():
    assert "sieve-cli==13.1.1" in updater._pip_cmd("13.1.1")
    assert "sieve-cli==13.1.1" in updater._heal_cmd("13.1.1")
    assert "sieve-cli[all]==13.1.1" in updater._pip_cmd_full("13.1.1")
    assert all("hound-mcp" not in part for part in updater._pip_cmd("13.1.1"))


def test_doctor_core_dependency_set_excludes_optional_mcp():
    source = inspect.getsource(updater.doctor)
    assert '"mcp"' not in source


def test_launcher_prefers_the_environment_that_runs_sieve(monkeypatch, tmp_path):
    executable = tmp_path / "bin" / "python"
    launcher = executable.with_name("sieve")
    launcher.parent.mkdir()
    launcher.touch()
    monkeypatch.setattr(updater.sys, "executable", str(executable))
    monkeypatch.setattr(shutil, "which", lambda _: "/global/bin/sieve")
    assert updater._sieve_launcher_path() == str(launcher)
