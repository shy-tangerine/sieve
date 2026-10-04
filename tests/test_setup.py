"""Subprocess contracts for the interactive setup command."""
from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import zipfile

import pytest


ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def permissive_umask():
    previous = os.umask(0)
    try:
        yield
    finally:
        os.umask(previous)


def _assert_owner_only(path):
    import stat

    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700


@pytest.mark.skipif(os.name != "posix", reason="POSIX permission contract")
def test_setup_persists_private_config_under_permissive_umask(monkeypatch, tmp_path, permissive_umask):
    from sieve import capabilities, config, setup

    path = tmp_path / "home" / ".sieve" / "config.toml"
    monkeypatch.setattr(config, "CONFIG_PATH", path)
    monkeypatch.setattr(setup.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(capabilities, "detect", lambda: {
        "transcription": [], "browsers": [], "yt_dlp": None, "cdp_endpoint": None,
    })
    monkeypatch.setattr("getpass.getpass", lambda *args: "")
    answers = iter(["pool", ""])
    monkeypatch.setattr("builtins.input", lambda *args: next(answers))
    assert setup.run_setup([]) == 0
    _assert_owner_only(path)


@pytest.mark.skipif(os.name != "posix", reason="POSIX permission contract")
@pytest.mark.parametrize("store", ["keys", "proxies"])
def test_private_store_permissions_under_permissive_umask(monkeypatch, tmp_path, permissive_umask, store):
    from sieve import byok_config, search_proxy

    module = byok_config if store == "keys" else search_proxy
    path = tmp_path / store / ".sieve" / "store.json"
    monkeypatch.setattr(module, "_config_path", lambda: path)
    if store == "keys":
        module.save_byok_keys({"serper": ["offline-fixture"]})
    else:
        module.save_proxies(["http://127.0.0.1:8080"])
    _assert_owner_only(path)


def _run_setup(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "sieve", "setup", *args],
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )


def test_setup_help_works_without_tty() -> None:
    result = _run_setup("--help")

    assert result.returncode == 0
    assert "usage: sieve setup" in result.stdout.lower()
    assert "interactive terminal" not in result.stderr.lower()


def test_setup_still_requires_tty() -> None:
    result = _run_setup()

    assert result.returncode == 1
    assert "sieve setup needs an interactive terminal." in result.stderr


def test_built_distributions_include_installable_agent_skill(tmp_path: Path) -> None:
    uv = shutil.which("uv")
    assert uv is not None, "the development workflow requires uv"

    dist_dir = tmp_path / "dist"
    build = subprocess.run(
        [uv, "build", "--out-dir", str(dist_dir)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )
    assert build.returncode == 0, build.stderr

    wheel = next(dist_dir.glob("*.whl"))
    with zipfile.ZipFile(wheel) as archive:
        assert "sieve/_skills/sieve/SKILL.md" in archive.namelist()
        assert "sieve/_skills/sieve-setup/SKILL.md" in archive.namelist()
        assert not any("sieve-development" in name for name in archive.namelist())
        installed_package = tmp_path / "installed-package"
        archive.extractall(installed_package)

    sdist = next(dist_dir.glob("*.tar.gz"))
    with tarfile.open(sdist, "r:gz") as archive:
        assert any(
            name.endswith("/sieve/_skills/sieve/SKILL.md")
            for name in archive.getnames()
        )
        assert any(
            name.endswith("/sieve/_skills/sieve-setup/SKILL.md")
            for name in archive.getnames()
        )
        assert not any("sieve-development" in name for name in archive.getnames())

    agent_skills = tmp_path / "agent-skills"
    env = os.environ.copy()
    env["PYTHONPATH"] = str(installed_package)
    install = subprocess.run(
        [
            sys.executable,
            "-m",
            "sieve",
            "skill",
            "install",
            "--dir",
            str(agent_skills),
        ],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )
    assert install.returncode == 0, install.stderr
    assert (agent_skills / "sieve" / "SKILL.md").is_file()
    assert (agent_skills / "sieve-setup" / "SKILL.md").is_file()
