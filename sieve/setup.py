"""Interactive first-run setup: BYOK keys, browser backend, social opt-in.

Secrets stay on the operator's machine: API keys go to ~/.sieve/search_keys.json
via the existing keys system; other settings go to ~/.sieve/config.toml.
Secret values are never printed back, logged, or transmitted.
"""
from __future__ import annotations

import os
import argparse
import shutil
from pathlib import Path
import sys


def _ask(prompt: str, default: str = "", secret: bool = False) -> str:
    import getpass
    shown = f" [{default}]" if default and not secret else ""
    try:
        raw = getpass.getpass(f"{prompt}{shown}: ") if secret else input(f"{prompt}{shown}: ")
    except (EOFError, KeyboardInterrupt):
        print()
        raise SystemExit(1)
    return raw.strip() or default


def _ask_yn(prompt: str, default: bool = False) -> bool:
    hint = "Y/n" if default else "y/N"
    try:
        raw = input(f"{prompt} [{hint}]: ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        raise SystemExit(1)
    if not raw:
        return default
    return raw in ("y", "yes")


def run_setup(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="sieve setup",
        description="Configure keys, browser backend, and safety gates.",
    )
    parser.parse_args(argv or [])
    if not sys.stdin.isatty():
        print("sieve setup needs an interactive terminal.", file=sys.stderr)
        return 1
    print("Sieve setup â keys stay on this machine, nothing leaves it.\n")

    # Discovery is advisory. Never bind a PATH entry or endpoint without an
    # explicit operator choice, and keep browser profiles isolated.
    from sieve.capabilities import detect
    found = detect()
    transcription = found["transcription"]
    # Only the OpenAI Whisper CLI has a verified adapter contract today.
    # Other local transcribers are useful discovery signals, but must not be
    # offered as configurable paths that the runtime cannot invoke safely.
    supported_transcription = [item for item in transcription if item["name"] == "whisper"]
    browsers = found["browsers"]
    if transcription:
        print("Existing transcription candidates:")
        for item in transcription:
            print(f"  {item['name']}: {item['path']}")
        if supported_transcription:
            print("  The verified Whisper adapter is available; other candidates are informational.")
        else:
            print("  These candidates are informational; no compatible adapter is configured.")
    if browsers:
        print("Existing browser runtimes (Sieve keeps its profile isolated):")
        for item in browsers:
            print(f"  {item['name']}: {item['path']}")
    if found["yt_dlp"]:
        print(f"yt-dlp: {found['yt_dlp']['path']} (reused automatically by media commands)")
    if found["cdp_endpoint"]:
        print(f"Local CDP endpoint detected: {found['cdp_endpoint']} (not saved automatically)")

    # 1. BYOK search keys (optional; keyless works without them).
    from sieve.byok_config import BYOK_PROVIDERS, add_key
    print("Search API keys (Enter to skip; keyless backends work without them):")
    for provider in BYOK_PROVIDERS:
        key = _ask(f"  {provider} key", secret=True)
        if key:
            try:
                add_key(provider, key)
                print(f"  saved {provider} key.")
            except (ValueError, OSError) as e:
                print(f"  skipped {provider}: {e}", file=sys.stderr)

    # 2. Browser backend.
    print("\nBrowser backend: pooled Chromium needs nothing; Sleeper drives your own browser.")
    backend = _ask("  backend (auto/pool/sleeper)", default="auto").lower()
    if backend not in ("auto", "pool", "sleeper"):
        backend = "auto"

    # 3. Sleeper daemon (only relevant for sleeper backend). Privileged local
    # endpoint (issue #147): loopback by default; a remote endpoint requires an
    # explicit override AND SIEVE_AUTH_TOKEN-equivalent protection is the
    # operator's responsibility — say so at confirmation time.
    sleeper_base = ""
    if backend in ("auto", "sleeper"):
        sleeper_base = _ask("  Sleeper daemon URL", default="http://127.0.0.1:8790")
        from urllib.parse import urlparse
        parsed_sleeper = urlparse(sleeper_base)
        loopback = parsed_sleeper.hostname in ("127.0.0.1", "localhost", "::1")
        if (parsed_sleeper.scheme not in {"http", "https"}
                or not parsed_sleeper.hostname
                or parsed_sleeper.username or parsed_sleeper.password):
            print("  Invalid Sleeper daemon URL; using loopback default.", file=sys.stderr)
            sleeper_base = "http://127.0.0.1:8790"
        elif not loopback:
            print("  WARNING: remote Sleeper endpoint — the daemon executes browser commands; "
                  "protect it with its own authentication and network policy.", file=sys.stderr)
            if not _ask_yn("  Persist this remote endpoint anyway?", default=False):
                sleeper_base = "http://127.0.0.1:8790"

    # 4. Social collection opt-in (ToS risk, off by default).
    social = _ask_yn("  Enable browser social collection (Instagram/TikTok ToS risk)?", default=False)

    stt_executable = ""
    if supported_transcription:
        candidate = supported_transcription[0]["path"]
        if _ask_yn(f"  Configure the verified transcription adapter at {candidate}?", default=False):
            # Revalidate at the write boundary (issue #86): discovery output
            # may be stale — the executable could have been replaced between
            # detection and confirmation. Persist only a path that is still a
            # regular, executable, non-symlink absolute file.
            cpath = Path(candidate).expanduser()
            if (cpath.is_absolute() and cpath.is_file() and not cpath.is_symlink()
                    and os.access(cpath, os.X_OK)):
                stt_executable = str(cpath)
            else:
                print(f"  Candidate changed since discovery; not saved ({candidate}).", file=sys.stderr)

    env = {
        "browser_backend": backend,
        "sleeper_base": sleeper_base,
        "enable_social_collect": social,
        "stt_executable": stt_executable,
    }
    from sieve.config import save as _save
    path = _save({k: v for k, v in env.items() if v not in ("", False)})
    print(f"\nWrote {path} (owner-only permissions requested where supported).")
    print("Verify: sieve --version && sieve search \"test\" --max-results 1")
    return 0



def _skill_source_dir() -> Path:
    return Path(__file__).resolve().parent / "_skills"


def _default_skill_dir() -> Path:
    return Path(os.environ.get("SIEVE_AGENT_SKILLS_DIR", "~/.agents/skills")).expanduser()


# Dangerous install roots (issue #229): a destination at or inside these
# locations could overwrite system state. (Note: Path("/") is deliberately
# excluded — it is an ancestor of every path, including legitimate tmp dirs.)
_DANGEROUS_SKILL_ROOTS = (
    Path("/etc"), Path("/usr"), Path("/bin"), Path("/sbin"),
    Path("/var"), Path("/boot"), Path("/lib"), Path("/opt"),
)


def _validate_skill_target(target: Path) -> None:
    """Validate the skill install root before any write (issue #229)."""
    resolved = target.resolve(strict=False)
    for root in _DANGEROUS_SKILL_ROOTS:
        if resolved == root or root in resolved.parents:
            raise ValueError(
                f"refusing to install skills under a system root: {resolved}"
            )
    # Refuse installs into the sieve package itself — but only the LIVE
    # package (this running module), not a test copy extracted elsewhere:
    # comparing paths after resolving symlinks would misfire on isolated
    # installs whose source dir merely lives under a tmp extract.
    live_pkg = Path(__file__).resolve().parent
    if resolved == live_pkg or live_pkg in resolved.parents:
        raise ValueError(
            f"refusing to install skills into the sieve package itself: {resolved}"
        )


def _swap_skill_directory(staging: Path, destination: Path) -> None:
    """Replace ``destination`` with ``staging`` without a data-loss window (issue #41).

    The previous tree is moved aside and only removed after the new tree is
    in place; if the swap fails, the previous tree is restored. Replaces the
    earlier rmtree-then-replace sequence, which left an interruption window
    with no installed skill at all. Symlink destinations are replaced (not
    written through) by moving the link aside first.
    """
    backup = destination.parent / f".{destination.name}.backup-{os.getpid()}"
    had_old = destination.exists() or destination.is_symlink()
    if backup.exists():
        if backup.is_symlink():
            backup.unlink()
        else:
            shutil.rmtree(backup)
    if had_old:
        os.replace(destination, backup)
    restored = False
    swapped = False
    try:
        os.replace(staging, destination)
        swapped = True
    except Exception:
        if had_old and (backup.exists() or backup.is_symlink()):
            if destination.is_symlink() or destination.is_file():
                destination.unlink()
            elif destination.exists():
                shutil.rmtree(destination)
            os.replace(backup, destination)
            restored = True
        raise
    finally:
        # Only discard the backup when it is genuinely stale: either the swap
        # succeeded, or the restore already put it back (issue #41). If the
        # restore itself failed, the backup is the only copy left and must
        # survive for manual recovery.
        if restored or swapped or not had_old:
            if backup.is_symlink():
                backup.unlink()
            elif backup.exists():
                shutil.rmtree(backup, ignore_errors=True)


def install_skills(destination: str | None = None, *, overwrite: bool = False) -> int:
    source = _skill_source_dir()
    if not source.is_dir():
        print("Sieve skills are not present in this installation.", file=sys.stderr)
        return 1
    target = Path(destination).expanduser() if destination else _default_skill_dir()
    try:
        _validate_skill_target(target)
    except ValueError as exc:
        print(f"{exc}", file=sys.stderr)
        return 1
    target.mkdir(parents=True, exist_ok=True)
    skills = sorted(p for p in source.iterdir() if p.is_dir() and (p / "SKILL.md").is_file())
    for skill in skills:
        dest = target / skill.name
        if dest.exists() and not dest.is_dir() and not dest.is_symlink():
            print(f"Refusing to replace a file with a skill: {dest}", file=sys.stderr)
            return 1
        marker = dest / ".sieve-managed"
        managed = not dest.is_symlink() and marker.is_file() and not marker.is_symlink() and marker.read_text() == skill.name + "\n"
        if (dest.exists() or dest.is_symlink()) and not managed and not overwrite:
            print(f"Refusing to replace unmanaged skill: {dest}; use --overwrite to replace it", file=sys.stderr)
            return 1
    installed = []
    for skill in skills:
        dest = target / skill.name
        # Atomic staged install (issues #41/#229): copy to a temp sibling,
        # verify SKILL.md landed, then swap with backup-and-rollback; an
        # interrupted install leaves the previous version intact instead of
        # a mixed tree or a missing skill. Symlink targets are replaced,
        # not written through.
        temp_dest = target / f".{skill.name}.tmp-{os.getpid()}"
        if temp_dest.exists():
            shutil.rmtree(temp_dest)
        try:
            shutil.copytree(skill, temp_dest)
            (temp_dest / ".sieve-managed").write_text(skill.name + "\n")
            if not (temp_dest / "SKILL.md").is_file():
                raise RuntimeError("staged skill missing SKILL.md")
            _swap_skill_directory(temp_dest, dest)
        finally:
            if temp_dest.exists():
                shutil.rmtree(temp_dest, ignore_errors=True)
        installed.append(skill.name)
    print(f"Installed {len(installed)} Sieve skills in {target}")
    return 0


def run_skill_command(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="sieve skill")
    sub = parser.add_subparsers(dest="action")
    install = sub.add_parser("install")
    install.add_argument("--dir", dest="destination")
    install.add_argument("--overwrite", action="store_true", help="replace existing unmanaged skill directories")
    args = parser.parse_args(argv or [])
    if args.action != "install": parser.print_help(); return 0
    return install_skills(args.destination, overwrite=args.overwrite)
