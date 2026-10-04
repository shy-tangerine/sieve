"""Reliable, brick-proof self-update for the sieve CLI. Cross-platform.

This module owns the entire update lifecycle. The previous updater could brick
the install: installing `sieve-cli[all]` pulled the heavy `[all]`
extra (onnxruntime, tokenizers, rapidocr) which is slow and fails mid-install
(leaving sieve deleted and sieve.exe orphaned -> every `sieve` command
crashes with ModuleNotFoundError, including `sieve -u` itself, so the tool
cannot self-heal). The recovery messages told users to run a bare
`pip install --force-reinstall` while a sieve server held the launcher, which
is the exact command that bricks it.

This rewrite fixes all of that:

- **Core deps installed, no extras.** The self-update installs sieve-cli
  WITH its core deps (so new core deps introduced in major versions are
  installed), but WITHOUT the `[all]` extra (so the heavy onnxruntime /
  tokenizers / rapidocr are NOT pulled). Fast, deterministic, cannot fail on
  a heavy dep. Existing deps already satisfied are left alone by pip.
- **Windows: a detached helper runs pip after the launcher exits.** The running
  `sieve -u` command IS sieve.exe, which Windows locks against overwrite. The
  helper is a standalone `python -c` (no sieve dependency) that waits
  for the parent launcher to exit, stages the launcher aside via the rename
  trick (Windows permits renaming a running .exe, just not overwriting it), then
  runs pip with the launcher free. A still-running sieve server is handled by
  the rename trick (it keeps the old code in memory until restarted); a stale
  locked `.old` is cleared by stopping that server. Never refuses, never bricks.
- **Self-heal.** If pip's first pass leaves the version unchanged or broken, a
  `--force-reinstall --no-deps` pass runs (the launcher is free by then) and
  re-verifies. Catches a half-failed install automatically.
- **Surviving repair.** `~/.sieve/repair.py` (pure stdlib, outside site-packages)
  is written on every update. If sieve is ever bricked (e.g. a manual pip while
  a server held the launcher), `python ~/.sieve/repair.py` stops sieve and
  force-reinstalls. It survives because it is not part of the sieve-cli package.
- **Safe messages.** Every failure prints ONE clean error plus the safe
  recovery (`python ~/.sieve/repair.py`), never a bare destructive pip command.
- **sieve doctor / sieve --rollback.** Proactive health check, and undo a bad
  update by reinstalling the previously-recorded version.
"""

from __future__ import annotations

import os
import sys
import json
import re
import urllib.request
import urllib.error
from pathlib import Path

__all__ = [
    "check_version", "pad_version",
    "do_update", "reinstall", "print_version", "doctor", "rollback",
    "cleanup_old_launcher", "repair_script_path",
    "check_release", "update_command", "release_summary",
]

RELEASE_API_URL = "https://api.github.com/repos/shy-tangerine/Sieve/releases/latest"
RELEASE_URL = "https://github.com/shy-tangerine/Sieve/releases/latest"
MAX_RELEASE_NOTES = 6
MAX_NOTE_CHARS = 180


def _source_checkout() -> bool:
    """Whether this code is running from a checkout rather than site-packages."""
    here = Path(__file__).resolve()
    return (here.parent.parent / ".git").exists()


def _version_key(value: str) -> tuple[int, int, int, int, str]:
    match = re.match(r"^[vV]?(\d+)(?:\.(\d+))?(?:\.(\d+))?(?:[-+](.*))?$", value.strip())
    if not match:
        raise ValueError(f"malformed version: {value!r}")
    return (int(match.group(1)), int(match.group(2) or 0), int(match.group(3) or 0),
            0 if not match.group(4) else -1, match.group(4) or "")


def _major_notes(body: str) -> list[str]:
    """Extract a small, deterministic list of release-note headings."""
    notes = []
    for line in (body or "").splitlines():
        line = re.sub(r"^\s*#+\s*", "", line).strip()
        if line and (line != (body or "").strip()) and len(line) <= MAX_NOTE_CHARS:
            notes.append(line)
        if len(notes) >= MAX_RELEASE_NOTES:
            break
    return notes


def check_release(*, fetcher=None) -> dict:
    """Return the stable, side-effect-free release check document."""
    from sieve import __version__
    installed = str(__version__)
    result = {
        "installed": installed, "latest": None, "update_available": False,
        "source": "source" if _source_checkout() else "package",
        "release_url": RELEASE_URL, "published": None, "release_notes": [],
        "available": False, "error": None,
    }
    try:
        request = urllib.request.Request(RELEASE_API_URL, headers={"Accept": "application/vnd.github+json", "User-Agent": "sieve-update"})
        opener = fetcher or urllib.request.urlopen
        response = opener(request, timeout=5)
        raw = response.read(256 * 1024 + 1)
        if len(raw) > 256 * 1024:
            raise ValueError("release metadata exceeds size limit")
        payload = json.loads(raw.decode("utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("release metadata is not an object")
        tag = payload.get("tag_name")
        if not isinstance(tag, str):
            raise ValueError("release metadata has no tag_name")
        latest = tag.lstrip("v")
        _version_key(latest)
        result.update({"latest": latest, "release_url": payload.get("html_url") if isinstance(payload.get("html_url"), str) else RELEASE_URL,
                       "published": payload.get("published_at") if isinstance(payload.get("published_at"), str) else None,
                       "release_notes": _major_notes(payload.get("body", "") if isinstance(payload.get("body", ""), str) else ""),
                       "available": True})
        result["update_available"] = _version_key(installed) < _version_key(latest)
    except Exception as exc:
        result["error"] = type(exc).__name__
    return result


def release_summary(result: dict) -> str:
    """Render bounded human-facing release notes."""
    if result.get("error"):
        return "Release check unavailable; try again later."
    if not result.get("update_available"):
        return "Sieve is up to date."
    lines = [f"Sieve {result['installed']} → {result['latest']}", f"Release notes: {result['release_url']}"]
    lines.extend(f"  • {note}" for note in result.get("release_notes", [])[:MAX_RELEASE_NOTES])
    return "\n".join(lines)


def update_command(*, yes: bool = False, check_only: bool = False, json_output: bool = False,
                   confirm=None) -> int:
    """Implement ``sieve update [check]`` with explicit user control."""
    result = check_release()
    if check_only:
        print(json.dumps(result, sort_keys=True) if json_output else release_summary(result))
        # A read-only check is safe to place in Topgrade: an offline host or a
        # temporarily unavailable release endpoint is not an update failure.
        return 0
    if result.get("error"):
        print(release_summary(result))
        return 1
    if not result.get("update_available"):
        print(release_summary(result))
        return 0
    print(release_summary(result))
    if _source_checkout():
        print("Source checkout detected; refusing to overwrite local changes. Update with a separate worktree and review the merge.")
        return 2
    approved = yes
    if not approved:
        ask = confirm or (lambda: input("Update Sieve now? [y/N] ").strip().lower() in {"y", "yes"})
        try:
            approved = bool(ask())
        except (EOFError, KeyboardInterrupt):
            approved = False
    if not approved:
        print("Update cancelled.")
        return 2
    previous = result["installed"]
    code, stderr = _run_pip(_pip_cmd(result["latest"]))
    if code:
        print(f"Update failed: {_diagnose(stderr)}")
        return 1
    try:
        from importlib.metadata import version as metadata_version
        actual = metadata_version("sieve-cli")
        if not _advanced(actual, result["latest"]):
            raise RuntimeError(f"installed version is {actual}")
    except Exception as exc:
        rollback_code, _ = _run_pip(_pip_cmd(previous))
        if rollback_code:
            print(f"Update verification failed ({exc}); recovery failed. Run: sieve --rollback")
        else:
            print(f"Update verification failed ({exc}); previous version restored.")
        return 1
    _write_last_version(previous)
    print(f"Sieve updated to {result['latest']}.")
    return 0


# âââ version probing âââââââââââââââââââââââââââââââââââââââââââââââââââââââ

def check_version() -> tuple[str, str | None, bool | None]:
    """Return (installed, latest, is_current).

    installed: the importlib.metadata version, or "unknown" if the package
    metadata is missing (a half-failed install / brick). latest: the current
    latest is always None for the standalone Sieve build. Sieve is a local
    source checkout, not an upstream release; comparing its
    version with PyPI would falsely present an upstream release as a Sieve
    update.
    """
    from importlib.metadata import version as _get_version
    try:
        installed = _get_version("sieve-cli")
    except Exception:
        try:
            installed = _get_version("hound-mcp")  # legacy pre-rename install
        except Exception:
            installed = "unknown"

    return installed, None, None


def pad_version(v: str) -> tuple[int, ...]:
    """Parse a dotted version into a comparable int tuple (first 3 parts)."""
    return tuple(int(p) for p in v.split(".")[:3])


def _at_or_ahead(installed: str, target: str) -> bool:
    """True if installed is parseable and >= target (so no update needed)."""
    if not installed or installed == "unknown":
        return False
    try:
        return pad_version(installed) >= pad_version(target)
    except (ValueError, IndexError):
        return installed == target


def _advanced(new_ver: str, target: str) -> bool:
    """True if a pinned pip run installed the requested target exactly."""
    if not new_ver or new_ver == "unknown":
        return False
    try:
        return pad_version(new_ver) == pad_version(target)
    except (ValueError, IndexError):
        return new_ver == target


# âââ launcher + process helpers (Windows file-lock handling) âââââââââââââââ

def _sieve_launcher_path() -> str | None:
    """Locate the sieve launcher (sieve.exe on Windows, `sieve` on POSIX)."""
    import shutil
    scripts_dir = os.path.join(os.path.dirname(sys.executable), "Scripts")
    for name in ("sieve.exe", "sieve"):
        fb = os.path.join(scripts_dir, name)
        if os.path.exists(fb):
            return fb
    posix_bin = os.path.dirname(sys.executable)
    posix_fallback = os.path.join(posix_bin, "sieve")
    if os.path.exists(posix_fallback):
        return posix_fallback
    candidate = shutil.which("sieve")
    if candidate and os.path.exists(candidate):
        return candidate
    return None


def _stop_sieve_cmd() -> str:
    """Platform command to stop all running sieve launcher processes."""
    if sys.platform == "win32":
        return "taskkill /IM sieve.exe /F"
    return "pkill -x sieve"


def _looks_like_file_lock_error(stderr: str) -> bool:
    if not stderr:
        return False
    s = stderr.lower()
    return ("winerror 32" in s or "being used by another process" in s
            or ("permission denied" in s and "sieve" in s))


def _other_sieve_pids() -> list[int]:
    """PIDs of OTHER running sieve launcher processes (excludes this one)."""
    import subprocess
    my_pid = os.getpid()
    pids: list[int] = []
    try:
        if sys.platform == "win32":
            out = subprocess.check_output(
                ["tasklist", "/FI", "IMAGENAME eq sieve.exe", "/FO", "CSV", "/NH"],
                text=True, timeout=10, creationflags=0x08000000,  # CREATE_NO_WINDOW
            )
            for line in out.splitlines():
                parts = [p.strip().strip('"') for p in line.split('","')]
                if len(parts) >= 2 and parts[0].lower() == "sieve.exe":
                    try:
                        pid = int(parts[1])
                    except ValueError:
                        continue
                    if pid != my_pid:
                        pids.append(pid)
        else:
            out = subprocess.check_output(["ps", "-eo", "pid=,comm="], text=True, timeout=10)
            for line in out.splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    pid_s, comm = line.split(None, 1)
                    pid = int(pid_s)
                except ValueError:
                    continue
                if os.path.basename(comm.strip()) == "sieve" and pid != my_pid:
                    pids.append(pid)
    except Exception:
        return []
    return pids


def _stop_all_sieve() -> None:
    """Kill all running sieve launcher processes (except this one)."""
    import subprocess
    pids = _other_sieve_pids()
    if not pids:
        return
    try:
        if sys.platform == "win32":
            subprocess.run(["taskkill", "/IM", "sieve.exe", "/F"],
                         capture_output=True, timeout=10,
                         creationflags=0x08000000)
        else:
            for pid in pids:
                try:
                    os.kill(pid, 15)  # SIGTERM
                except Exception:
                    pass
    except Exception:
        pass


def cleanup_old_launcher() -> None:
    """Sweep a stale sieve.exe.old left by a previous self-update (Windows).

    Windows locks the running .exe against deletion, so the updater renames the
    live launcher to sieve.exe.old before pip writes a fresh one. The .old can
    only be deleted once the process running from it exits, so we sweep it on
    the next launch instead. No-op on non-Windows.
    """
    if sys.platform != "win32":
        return
    exe = _sieve_launcher_path()
    if not exe:
        return
    old = exe + ".old"
    try:
        if os.path.exists(old):
            os.remove(old)
    except OSError:
        pass  # still locked (a sieve server still runs from it) - leave it


# âââ sieve home: state + the surviving repair script âââââââââââââââââââââââ

def _sieve_home() -> str:
    p = os.path.join(os.path.expanduser("~"), ".sieve")
    os.makedirs(p, exist_ok=True)
    return p


def repair_script_path() -> str:
    return os.path.join(_sieve_home(), "repair.py")


def _state_path(name: str) -> str:
    return os.path.join(_sieve_home(), name)


_REPAIR_SCRIPT = '''#!/usr/bin/env python3
r"""Sieve repair - recover from a broken install (failed update, brick).

Run with:  python __REPAIR__
Stops any running sieve process, reinstalls the local editable project, verifies.
Pure standard library - works even when the package metadata is gone, because
this file lives in ~/.sieve (outside site-packages), so a failed pip uninstall
never touches it.
"""
import subprocess, sys

SIEVE_DIR = os.environ.get("SIEVE_PROJECT_DIR") or os.getcwd()

def _stop():
    if sys.platform == "win32":
        subprocess.run(["taskkill", "/IM", "sieve.exe", "/F"], capture_output=True)
    else:
        # -x matches the process name exactly ("sieve"), not this script ("python").
        subprocess.run(["pkill", "-x", "sieve"], capture_output=True)

def _pip(*extra):
    return subprocess.run(
        [sys.executable, "-m", "pip", "install", *extra, "--quiet",
         "--disable-pip-version-check"])

def main():
    print("Sieve repair: stopping any running sieve...")
    _stop()
    print("Sieve repair: reinstalling the local project (editable)...")
    r = _pip("-e", SIEVE_DIR)
    if r.returncode != 0:
        print("Sieve repair: reinstall failed (installer exit %d)." % r.returncode)
        print("  Try manually:  uv pip install --editable %s" % SIEVE_DIR)
        return r.returncode
    try:
        from importlib.metadata import version as _v
        print("Sieve " + _v("sieve-cli") + "  repaired")
        return 0
    except Exception as e:
        print("Sieve repair: still broken after reinstall: " + str(e))
        print("  Reinstall all deps:  uv pip install --editable %s" % SIEVE_DIR)
        return 1

if __name__ == "__main__":
    sys.exit(main())
'''


def _write_repair_script() -> None:
    """(Re)write ~/.sieve/repair.py so the brick-recovery safety net exists."""
    try:
        with open(repair_script_path(), "w", encoding="utf-8") as f:
            f.write(_REPAIR_SCRIPT.replace("__REPAIR__", repair_script_path()))
    except OSError:
        pass  # home dir not writable; not fatal - the update can still proceed


def _write_last_version(v: str) -> None:
    if not v or v == "unknown":
        return
    try:
        with open(_state_path("last_version"), "w", encoding="utf-8") as f:
            f.write(v.strip())
    except OSError:
        pass


def _read_last_version() -> str | None:
    try:
        with open(_state_path("last_version"), "r", encoding="utf-8") as f:
            v = f.read().strip()
            return v or None
    except OSError:
        return None


# âââ pip commands + runner âââââââââââââââââââââââââââââââââââââââââââââââââ

def _pip_cmd(target: str) -> list[str]:
    """Install `target` with core deps (no extras). Fast, reliable.

    Uses NO --no-deps (unlike v10.x) so new core deps introduced in major
    versions are installed. Does NOT include [all] so the heavy extras
    (onnxruntime, tokenizers, rapidocr) are NOT pulled. Existing deps that
    are already satisfied are left alone by pip.
    """
    return [sys.executable, "-m", "pip", "install",
            f"sieve-cli=={target}", "--quiet", "--disable-pip-version-check",
            "--no-python-version-warning"]


def _heal_cmd(target: str) -> list[str]:
    """Force-reinstall `target` (with core deps) - the self-heal / brick-recovery pass.

    Uses NO --no-deps so missing core deps are installed. Does NOT include [all]
    so heavy extras are not pulled.
    """
    return [sys.executable, "-m", "pip", "install", "--force-reinstall",
            f"sieve-cli=={target}", "--quiet", "--disable-pip-version-check",
            "--no-python-version-warning"]


def _pip_cmd_full(target: str) -> list[str]:
    """Full reinstall: force-reinstall sieve-cli[all] at the pinned version
    and its dependencies. This intentionally lets pip repair missing or broken
    core dependencies and [all] extras (rapidocr, onnxruntime, tokenizers)."""
    return [sys.executable, "-m", "pip", "install", "--force-reinstall",
            f"sieve-cli[all]=={target}", "--quiet", "--disable-pip-version-check",
            "--no-python-version-warning"]


def _run_pip(cmd: list[str]) -> tuple[int, str]:
    """Run pip, capturing stderr for diagnosis. Returns (returncode, stderr)."""
    import subprocess
    try:
        r = subprocess.run(cmd, timeout=300, capture_output=True, text=True)
        return r.returncode, (r.stderr or "")
    except subprocess.TimeoutExpired:
        return 124, "timed out"
    except Exception as e:
        return 1, str(e)


def _diagnose(stderr: str) -> str:
    if _looks_like_file_lock_error(stderr):
        return "a running sieve server holds the launcher"
    s = (stderr or "").lower()
    if "no matching distribution" in s or "could not find a version" in s:
        return "version not found on PyPI"
    if "timed out" in s or "timeout" in s:
        return "network timed out"
    return "pip failed"


# âââ the detached Windows helper (standalone python -c, survives brick) ââââ

def _build_helper_source(target: str, repair_path: str, parent_pid: int, full: bool = False) -> str:
    """Build the standalone helper source. Pure stdlib, no sieve import,
    so it runs even if the package is mid-replacement or bricked.

    The helper: waits for the parent launcher to exit, stages the launcher aside
    (rename trick; stops a server only if it holds a stale .old), runs pip,
    self-heals on verify-fail, prints a clean result. Plain ASCII
    output (no ANSI) since it runs detached after the parent's color setup is
    gone and may run on a legacy console.
    """
    return '''import os, sys, time, subprocess
PARENT = __PARENT_PID__
TARGET = __TARGET__
REPAIR = __REPAIR__
EXE = __EXE__
WIN = (sys.platform == "win32")
FULL = __FULL__

def _wait_parent_exit(timeout=15):
    if not WIN or not PARENT:
        return
    end = time.time() + timeout
    while time.time() < end:
        try:
            os.waitpid(PARENT, os.WNOHANG)
            return
        except (ChildProcessError, OSError):
            return  # not our child (the launcher was) - assume gone after sleep
        except Exception:
            break
    time.sleep(2)  # fallback: give the launcher time to release the file

def _sieve_pids():
    out = []
    if not WIN:
        return out
    my = os.getpid()
    try:
        o = subprocess.check_output(
            ["tasklist", "/FI", "IMAGENAME eq sieve.exe", "/FO", "CSV", "/NH"],
            text=True, timeout=10, creationflags=0x08000000)
    except Exception:
        return out
    for ln in o.splitlines():
        ps = [x.strip().strip(chr(34)) for x in ln.split(chr(34) + "," + chr(34))]
        if len(ps) >= 2 and ps[0].lower() == "sieve.exe":
            try:
                pid = int(ps[1])
            except ValueError:
                continue
            if pid != my:
                out.append(pid)
    return out

def _stop_all_sieve():
    if WIN:
        subprocess.run(["taskkill", "/IM", "sieve.exe", "/F"], capture_output=True)
    else:
        subprocess.run(["pkill", "-x", "sieve"], capture_output=True)

def _stage():
    # Rename the live sieve.exe -> sieve.exe.old so pip can write a fresh one
    # to the now-free path. Windows permits RENAMING a running .exe (it only
    # forbids overwrite/delete), so a server keeps running from the .old until
    # it restarts - no need to stop it. The only stop is for a stale .old left
    # by a previous update that a server still runs from.
    if not EXE or not WIN:
        return True
    old = EXE + ".old"
    if os.path.exists(old):
        for _ in range(2):
            try:
                os.remove(old)
                break
            except OSError:
                print("  a stale sieve.exe.old is locked - stopping the old sieve server...")
                _stop_all_sieve()
                time.sleep(2)
    try:
        os.rename(EXE, old)
        return True
    except OSError:
        # Rename failed (e.g. read-only system install). pip will likely fail
        # too; the self-heal pass and the repair.py fallback handle the rest.
        return False

def _pip(*extra):
    r = subprocess.run(
        [sys.executable, "-m", "pip", "install", *extra, "--quiet",
         "--disable-pip-version-check"],
        capture_output=True, text=True, timeout=300)
    return r.returncode, (r.stderr or "")

def _ver():
    try:
        from importlib.metadata import version as _v
        return _v("sieve-cli")
    except Exception:
        return "unknown"

def _pad(v):
    try:
        return tuple(int(x) for x in v.split(".")[:3])
    except Exception:
        return None

def _advanced(new):
    if not new or new == "unknown":
        return False
    np, tp = _pad(new), _pad(TARGET)
    if np and tp:
        return np == tp
    return new == TARGET

_wait_parent_exit()
# Move below any shell prompt that printed when the parent exited.
try:
    sys.stdout.write(chr(10)); sys.stdout.flush()
except Exception:
    pass

servers_before = _sieve_pids()
if servers_before:
    print("  stopping " + str(len(servers_before)) + " running sieve server(s)...")
    _stop_all_sieve()
    time.sleep(1)
    servers_before = []
_stage()

if FULL:
    rc, stderr = _pip("--force-reinstall", "sieve-cli[all]==" + TARGET)
else:
    rc, stderr = _pip("sieve-cli==" + TARGET)
if not _advanced(_ver()):
    print("  first pass did not complete - recovering...")
    if FULL:
        rc2, stderr2 = _pip("--force-reinstall", "sieve-cli[all]==" + TARGET)
    else:
        rc2, stderr2 = _pip("--force-reinstall", "sieve-cli==" + TARGET)
    if not _advanced(_ver()):
        print("  Sieve  " + ("reinstall" if FULL else "update") + " failed - " + (stderr2 or stderr or "pip failed").strip().splitlines()[-1:][0] if (stderr2 or stderr) else "pip failed")
        print("  recover with:  python \\"" + REPAIR + "\\"")
        sys.exit(1)

# Best-effort: sweep the staged .old (fails if a server still maps it - fine).
# Safety: if pip didn't recreate the .exe (already satisfied, no --force-reinstall),
# restore it from the .old backup.
try:
    if WIN and EXE:
        if os.path.exists(EXE + ".old"):
            if not os.path.exists(EXE):
                os.rename(EXE + ".old", EXE)
            else:
                os.remove(EXE + ".old")
except OSError:
    pass

new = _ver()
print("  Sieve  v" + new + "  " + ("reinstalled" if FULL else "updated"))
if servers_before:
    print("  restart your running sieve server (PID " + ", ".join(str(p) for p in servers_before) + ") to use it")
'''.replace("__PARENT_PID__", str(parent_pid)).replace("__TARGET__", repr(target)).replace("__REPAIR__", repr(repair_path)).replace("__EXE__", repr(_sieve_launcher_path())).replace("__FULL__", str(full))


def _spawn_helper(target: str, repair_path: str, parent_pid: int, full: bool = False) -> bool:
    """Spawn the detached Windows helper (inherits this console). Returns True
    if spawned. `full=True` triggers a complete reinstall with deps + [all]
    extras instead of the usual core-only update."""
    import subprocess
    src = _build_helper_source(target, repair_path, parent_pid, full)
    try:
        subprocess.Popen([sys.executable, "-c", src])
        return True
    except Exception:
        return False


# âââ public commands âââââââââââââââââââââââââââââââââââââââââââââââââââââââ

def do_update(target: str | None = None) -> None:
    """Explain how to update the local source checkout."""
    from sieve import cli_ui as ui
    installed, _, _ = check_version()
    print(ui.branded(ui.ver(installed if installed != "unknown" else "?"),
                     ui.dim("local source build")))
    print("  " + ui.warn("Sieve has no upstream self-update path."))
    print("  " + ui.dim("Review upstream changes, then update this checkout manually."))
    return


def reinstall() -> None:
    """Explain how to repair the local editable checkout."""
    from sieve import cli_ui as ui
    installed, _, _ = check_version()
    print(ui.branded(ui.ver(installed if installed != "unknown" else "?"),
                     ui.dim("local source build")))
    print("  " + ui.warn("Sieve is editable; reinstall dependencies from pyproject.toml if needed."))
    print("  " + ui.dim("No upstream package will be installed automatically."))


def rollback() -> None:
    """Reinstall the version recorded before the last update (undo a bad update)."""
    from sieve import cli_ui as ui
    last = _read_last_version()
    if not last:
        print(ui.branded(ui.dim("nothing to roll back to"),
                         ui.dim("no previous version recorded")))
        return
    installed = check_version()[0]
    if _at_or_ahead(installed, last) and installed != "unknown":
        try:
            same = pad_version(installed) == pad_version(last)
        except (ValueError, IndexError):
            same = installed == last
        if same:
            print(ui.branded(ui.ver(installed), ui.dim("already at the previous version")))
            return
    print(ui.branded(ui.dim("rolling back"), ui.ver_transition(installed, last)))
    do_update(target=last)


def print_version() -> None:
    """Render `sieve -v`: a compact bordered version panel (or a clean error
    panel when the install is corrupted, pointing at the safe repair path)."""
    from sieve import cli_ui as ui
    W = 50
    inner = W - 4
    installed, latest, is_current = check_version()
    if installed == "unknown":
        repair = repair_script_path()
        body = [
            ui.dim("package metadata is missing - a previous update was"),
            ui.dim("interrupted. The launcher works, but pip lost the version."),
            "",
            ui.dim("recover with:"),
            "  " + ui.cmd(f'python "{repair}"'),
            ui.dim("repair the editable checkout with pip if needed"),
        ]
        print(ui.panel([ui.err("install corrupted")] + body, 62))
        return
    print(ui.panel([
        ui.lr(ui.wordmark(), "", inner),
        ui.lr(ui.ver(installed), ui.dim("local source build"), inner),
    ], W))


def _doctor_short(p: str | None, w: int = 34) -> str:
    if not p:
        return ""
    home = os.path.expanduser("~")
    if p.startswith(home):
        p = "~" + p[len(home):]
    if len(p) > w:
        p = "..." + p[-(w - 3):]
    return p


def _doctor_check_package(checks: list) -> str | None:
    try:
        import sieve as _mf
        mf_ver = getattr(_mf, "__version__", "?")
        checks.append(("package imports", True, mf_ver))
        return mf_ver
    except Exception as e:
        checks.append(("package imports", False, str(e)))
        return None


def _doctor_check_metadata(checks: list, mf_ver: str | None) -> None:
    from importlib.metadata import version as _meta_version
    try:
        meta_ver = _meta_version("sieve-cli")
        ok = (mf_ver is not None and meta_ver == mf_ver)
        checks.append(("metadata consistent", ok,
                       f"meta {meta_ver} vs module {mf_ver}" if not ok else meta_ver))
    except Exception:
        checks.append(("metadata consistent", False, "sieve-cli metadata missing"))


def _doctor_check_launcher_clean(exe: str | None) -> str:
    if exe and sys.platform == "win32":
        old = exe + ".old"
        if os.path.exists(old):
            try:
                os.remove(old)
            except OSError:
                return "sieve.exe.old locked by a running server (cleaned when it stops)"
    return ""


def _doctor_missing(mods: tuple[str, ...]) -> list[str]:
    missing: list[str] = []
    for mod in mods:
        try:
            __import__(mod)
        except Exception:
            missing.append(mod)
    return missing


def doctor() -> None:
    """Proactive health check. Diagnoses a half-broken install before it bricks,
    and offers the right fix. Prints a clean report."""
    from sieve import cli_ui as ui

    _short = _doctor_short
    checks: list[tuple[str, bool, str]] = []

    exe = _sieve_launcher_path()
    checks.append(("launcher resolves", bool(exe), _short(exe) or "sieve not on PATH"))

    mf_ver = _doctor_check_package(checks)
    _doctor_check_metadata(checks, mf_ver)

    stale = _doctor_check_launcher_clean(exe)
    checks.append(("launcher clean", not stale, stale or "ok"))

    _write_repair_script()
    rp = repair_script_path()
    checks.append(("repair script ready", os.path.exists(rp), _short(rp or "")))

    stale_pids = _other_sieve_pids()
    stale_detail = f"{len(stale_pids)} running: PID {', '.join(str(p) for p in stale_pids[:5])}" if stale_pids else "none running"
    checks.append(("no stale servers", not stale_pids, stale_detail))

    missing = _doctor_missing(("httpx", "aiosqlite", "pydantic"))
    checks.append(("core dependencies", not missing,
                   ", ".join(missing) + " missing" if missing else "ok"))

    optional_missing = _doctor_missing(("onnxruntime", "tokenizers", "rapidocr"))
    optional_ok = not optional_missing
    optional_detail = ", ".join(optional_missing) + " missing" if optional_missing else "ok"

    browser_missing = _doctor_missing(("playwright", "patchright"))
    browser_ok = not browser_missing
    browser_detail = ", ".join(browser_missing) + " missing (HTTP-only mode)" if browser_missing else "ok"

    byok_detail = "none configured"
    try:
        from sieve.byok_config import load_byok_keys
        byok_keys = load_byok_keys()
        if byok_keys:
            byok_detail = ", ".join(f"{p}({len(v)})" for p, v in byok_keys.items() if v)
        else:
            byok_detail = "none (local keyless search only)"
    except Exception:
        byok_detail = "check failed"

    installed, latest, _ = check_version()
    if latest is None:
        checks.append(("PyPI update check", True, "not applicable to local source build"))
    else:
        try:
            ahead = pad_version(installed) >= pad_version(latest)
        except (ValueError, IndexError):
            ahead = True
        checks.append(("PyPI update check", True,
                       "up to date" if ahead else f"v{latest} available"))

    # Render
    all_ok = all(ok for _, ok, _ in checks)
    status = ui.ok("all healthy") if all_ok else ui.err("issues found")
    rows = [ui.wordmark() + "  " + status, ""]
    for label, ok_flag, detail in checks:
        mark = (ui._sty(ui._glyph("\u2713", "+"), ui._GREEN) if ok_flag
                else ui._sty(ui._glyph("\u2717", "x"), ui._RED))
        rows.append(f"{mark} {label:<22} {ui.dim(_short(detail, 30))}")
    # Optional [all] extras (non-blocking, shown with a different marker)
    opt_mark = (ui._sty(ui._glyph("\u2713", "+"), ui._GREEN) if optional_ok
                else ui._sty(ui._glyph("!", "!"), ui._MAGENTA))
    rows.append(f"{opt_mark} {'[all] extras':<22} {ui.dim(_short(optional_detail, 30))}")
    # Browser deps (non-blocking, same marker style as [all] extras)
    bw_mark = (ui._sty(ui._glyph("\u2713", "+"), ui._GREEN) if browser_ok
               else ui._sty(ui._glyph("!", "!"), ui._MAGENTA))
    rows.append(f"{bw_mark} {'browser deps':<22} {ui.dim(_short(browser_detail, 30))}")
    # BYOK search API keys (non-blocking, info-only)
    rows.append(f"  {'byok search keys':<22} {ui.dim(_short(byok_detail, 30))}")
    # Search proxy rotation (non-blocking, info-only)
    proxy_detail = "none (direct connection)"
    try:
        from sieve.search_proxy import load_proxies
        proxies = load_proxies()
        if proxies:
            proxy_detail = f"{len(proxies)} proxy(s) configured (rotating)"
    except Exception:
        proxy_detail = "check failed"
    rows.append(f"  {'search proxies':<22} {ui.dim(_short(proxy_detail, 30))}")
    print(ui.panel(rows, 64))
    # Verdict + fixes (outside the panel)
    if missing:
        print("  " + ui.warn("fix deps") + "  " + ui.cmd("pip install --force-reinstall sieve-cli"))
    if any(not ok for _, ok, _ in checks) and not missing:
        print("  " + ui.warn("repair") + "  " + ui.cmd(f'python "{_short(rp, 46)}"'))
    if stale_pids:
        print("  " + ui.warn("stop stale servers") + "  " + ui.cmd("taskkill /IM sieve.exe /F") if sys.platform == "win32" else ui.cmd("pkill -x sieve"))
    if not optional_ok:
        print("  " + ui.warn("install extras") + "  " + ui.cmd("pip install sieve-cli[all]"))
    if not browser_ok:
        print("  " + ui.warn("browser mode") + "  HTTP-only (stealthy/screenshot disabled). "
              + ui.cmd("pip install sieve-cli[all]") + " if your platform supports playwright)")
    if latest and installed != "unknown":
        try:
            if pad_version(installed) < pad_version(latest):
                print("  " + ui.warn("update") + "  " + ui.cmd("sieve -u"))
        except (ValueError, IndexError):
            pass
