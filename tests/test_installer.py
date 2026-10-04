"""Run the POSIX installer against local fake tools, without installation."""

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest


INSTALLER = Path(__file__).resolve().parents[1] / "scripts/install.sh"
FAKE_TOOL = r'''
import json, os, pathlib, sys
name = pathlib.Path(sys.argv[0]).name
args = sys.argv[1:]
with open(os.environ["FIXTURE_LOG"], "a") as handle:
    handle.write(json.dumps({"tool": name, "args": args, "cwd": os.getcwd(),
                            "venv": os.environ.get("UV_PROJECT_ENVIRONMENT")}) + "\n")
failure = os.environ.get("FIXTURE_FAILURE", "")
if name == "git":
    if args[0] == "clone":
        target = pathlib.Path(args[-1])
        (target / "sieve").mkdir()
        (target / "pyproject.toml").write_text("# fake checkout\n")
    sys.exit(1 if failure == args[0] else 0)
if name == "uv":
    if args[0] == "sync":
        target = pathlib.Path(os.environ["UV_PROJECT_ENVIRONMENT"]) / "bin"
        target.mkdir(parents=True)
        (target / "python").symlink_to(pathlib.Path(sys.argv[0]).with_name("python3.11"))
        sys.exit(1 if failure == "install" else 0)
    if "fetch" in args:
        print('{"content": ["offline fixture"]}')
        sys.exit(1 if failure == "self-check" else 0)
    sys.exit(1 if failure == "version" and "--version" in args else 0)
if name == "timeout":
    os.execvp(args[1], args[1:])
if "--version" in args:
    print("Python 3.11 fixture")
sys.exit(1 if args and "import yt_dlp" in args[-1] else 0)
'''


def run_installer(tmp_path, *, mode="checkout", absolute=False, timeout=True,
                  failure="", extras=None):
    tools = tmp_path / "tools"
    tools.mkdir()
    for name in ["uv", "git", "python3.11"] + (["timeout"] if timeout else []):
        tool = tools / name
        tool.write_text(f"#!{sys.executable}\n" + FAKE_TOOL)
        tool.chmod(0o755)
    for name in ["mktemp", "rm", "cat", "wc"]:
        (tools / name).symlink_to(shutil.which(name))
    temporary = tmp_path / "temporary"
    temporary.mkdir()
    cwd = tmp_path / "working directory"
    cwd.mkdir()
    args = []
    if mode == "checkout":
        (cwd / "sieve").mkdir()
        (cwd / "pyproject.toml").write_text("# fake checkout\n")
    elif mode == "path":
        source = tmp_path / "source checkout"
        source.mkdir()
        (source / "sieve").mkdir()
        args += ["--repo", str(source)]
    else:
        args += ["--repo", "https://example.test/fixture.git", "--revision", "fixture-ref"]
    environment_path = tmp_path / "absolute environment" if absolute else cwd / "relative environment"
    args += ["--venv", str(environment_path) if absolute else "relative environment"]
    if extras is not None:
        args += ["--minimal"] if not extras else ["--extras", extras]
    log = tmp_path / "tools.jsonl"
    environment = {
        **os.environ, "PATH": str(tools), "TMPDIR": str(temporary),
        "FIXTURE_LOG": str(log), "FIXTURE_FAILURE": failure,
    }
    result = subprocess.run(
        ["/bin/sh", str(INSTALLER), *args], cwd=cwd, env=environment,
        capture_output=True, text=True, timeout=10,
    )
    events = [json.loads(line) for line in log.read_text().splitlines()]
    assert list(temporary.iterdir()) == []  # clone and both self-check files
    return result, events, environment_path, cwd


@pytest.mark.parametrize("mode", ["checkout", "clone", "path"])
@pytest.mark.parametrize("absolute", [False, True])
@pytest.mark.parametrize("timeout", [False, True])
def test_installer_paths_and_cleanup(tmp_path, mode, absolute, timeout):
    result, events, environment_path, cwd = run_installer(
        tmp_path, mode=mode, absolute=absolute, timeout=timeout,
    )
    assert result.returncode == 0, result.stderr
    sync = next(event for event in events if event["tool"] == "uv" and event["args"][0] == "sync")
    assert sync["venv"] == str(environment_path)
    assert sync["args"] == ["sync", "--locked", "--all-extras"]
    git_calls = [event["args"][0] for event in events if event["tool"] == "git"]
    assert git_calls == (["clone", "fetch", "checkout"] if mode == "clone" else [])
    assert (Path(sync["cwd"]) == cwd) == (mode == "checkout")
    assert any(event["tool"] == "timeout" for event in events) == timeout
    assert any("fetch" in event["args"] for event in events)


@pytest.mark.parametrize("mode,failure,expected", [
    ("clone", "clone", 1), ("clone", "fetch", 1), ("clone", "checkout", 1),
    ("clone", "install", 1), ("checkout", "install", 1),
    ("clone", "self-check", 0), ("checkout", "self-check", 0),
    ("checkout", "version", 0),
])
def test_installer_failure_cleanup(tmp_path, mode, failure, expected):
    result, _, _, _ = run_installer(tmp_path, mode=mode, failure=failure)
    assert result.returncode == expected, result.stderr
    if failure in {"self-check", "version"}:
        assert "WARN:" in result.stderr


@pytest.mark.parametrize("extras,expected", [
    ("", ["sync", "--locked"]),
    ("browser,media", ["sync", "--locked", "--extra", "browser", "--extra", "media"]),
])
def test_installer_extra_arguments(tmp_path, extras, expected):
    result, events, _, _ = run_installer(tmp_path, extras=extras)
    assert result.returncode == 0, result.stderr
    assert next(event["args"] for event in events if event["tool"] == "uv") == expected
