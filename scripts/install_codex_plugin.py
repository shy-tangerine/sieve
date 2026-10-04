#!/usr/bin/env python3
"""Install Sieve's Codex plugin into the user's personal marketplace.

Atomic install (issue #116): the plugin package and the marketplace
metadata are fully validated *before* any destination mutation, the plugin
tree is staged in a temp sibling, and the destination is swapped with
backup-and-rollback — an interrupted or failed install leaves the previous
version intact instead of a partially merged tree. Unrelated marketplace
entries are preserved.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import tempfile
from pathlib import Path


PLUGIN_NAME = "sieve-web-research"
OWNERSHIP_MARKER = ".sieve-managed"


def _marketplace(agents_dir: Path) -> Path:
    return agents_dir / "plugins" / "marketplace.json"


def _validate_marketplace_data(data) -> dict:
    """Validate personal-marketplace metadata shape; raise ValueError on bad JSON."""
    if not isinstance(data, dict):
        raise ValueError("personal marketplace must be a JSON object")
    if data.get("name") not in (None, "personal"):
        raise ValueError(f"personal marketplace has unexpected name: {data.get('name')!r}")
    if "interface" in data and not isinstance(data["interface"], dict):
        raise ValueError("personal marketplace has invalid interface shape")
    plugins = data.get("plugins", [])
    if not isinstance(plugins, list) or any(not isinstance(item, dict) for item in plugins):
        raise ValueError("personal marketplace has invalid plugins shape")
    return data


def _load_marketplace(path: Path) -> dict:
    if not path.exists():
        return {"name": "personal", "interface": {"displayName": "Personal"}, "plugins": []}
    return _validate_marketplace_data(json.loads(path.read_text()))


def _updated_marketplace(path: Path) -> dict:
    data = _load_marketplace(path)
    data.setdefault("name", "personal")
    data.setdefault("interface", {"displayName": "Personal"})
    data.setdefault("plugins", [])
    entry = {
        "name": PLUGIN_NAME,
        "source": {"source": "local", "path": f"./plugins/{PLUGIN_NAME}"},
        "policy": {"installation": "AVAILABLE", "authentication": "ON_INSTALL"},
        "category": "Research",
    }
    data["plugins"] = [item for item in data["plugins"] if item.get("name") != PLUGIN_NAME]
    data["plugins"].append(entry)
    return data


def _validate_source(source: Path) -> None:
    """Validate the plugin package and its manifest before any destination write."""
    manifest_path = source / ".codex-plugin" / "plugin.json"
    if not manifest_path.is_file():
        raise FileNotFoundError(f"Codex plugin package not found: {source}")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(manifest, dict):
        raise ValueError(f"plugin manifest must be a JSON object: {manifest_path}")
    if manifest.get("name") != PLUGIN_NAME:
        raise ValueError(
            f"plugin manifest name mismatch: expected {PLUGIN_NAME!r}, got {manifest.get('name')!r}"
        )


def _swap_directory(staging: Path, destination: Path, *, post_swap=None) -> None:
    """Replace ``destination`` with ``staging`` atomically (issue #116).

    The previous tree is moved aside first; on any failure — including a
    ``post_swap`` callback such as marketplace promotion — the partial new
    tree is removed and the previous tree is restored, so a failed or
    interrupted replacement never leaves a partial merge or a missing
    plugin.
    """
    backup = destination.parent / f".{destination.name}.backup-{os.getpid()}"
    had_old = destination.exists() or destination.is_symlink()
    if backup.is_symlink():
        backup.unlink()
    elif backup.exists():
        shutil.rmtree(backup)
    if had_old:
        os.replace(destination, backup)
    restored = False
    swapped = False
    try:
        os.replace(staging, destination)
        if post_swap is not None:
            post_swap()
        swapped = True
    except Exception:
        if had_old and backup.exists():
            if destination.is_symlink() or destination.is_file():
                destination.unlink()
            elif destination.exists():
                shutil.rmtree(destination)
            os.replace(backup, destination)
            restored = True
        raise
    finally:
        # Only discard the backup when it is genuinely stale: either the swap
        # succeeded, or the restore already put it back (issue #116). If the
        # restore itself failed, the backup is the only copy left and must
        # survive for manual recovery.
        if backup.is_symlink():
            backup.unlink()
        elif backup.exists() and (restored or swapped or not had_old):
            shutil.rmtree(backup, ignore_errors=True)


def install(source_dir: Path, agents_dir: Path, *, dry_run: bool = False, overwrite: bool = False) -> tuple[Path, Path]:
    source = source_dir / "plugins" / "codex" / PLUGIN_NAME
    _validate_source(source)
    marketplace = _marketplace(agents_dir)
    destination = agents_dir.parent / "plugins" / PLUGIN_NAME
    # Preflight: validate the existing marketplace metadata before any
    # destination mutation, so malformed metadata cannot leave partial files.
    data = _updated_marketplace(marketplace)
    if destination.is_symlink():
        raise ValueError(f"refusing to install over a symlink: {destination}")
    if destination.exists() and not destination.is_dir():
        raise ValueError(f"refusing to replace a file with a plugin: {destination}")
    marker = destination / OWNERSHIP_MARKER
    managed = marker.is_file() and not marker.is_symlink() and marker.read_text() == PLUGIN_NAME + "\n"
    if destination.exists() and not managed and not overwrite:
        raise ValueError(f"refusing to replace unmanaged directory: {destination}; use --overwrite to replace it")
    if dry_run:
        return destination, marketplace

    destination.parent.mkdir(parents=True, exist_ok=True)

    # Write the marketplace temp file first (#116): metadata failures happen
    # before any destination mutation and can leave no partial state.
    marketplace.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=marketplace.parent, delete=False) as handle:
        json.dump(data, handle, indent=2)
        handle.write("\n")
        temporary = Path(handle.name)

    # Stage the full tree, then swap atomically (#116): no dirs_exist_ok
    # merge-copy, so stale files from a previous version cannot survive.
    # Marketplace promotion runs inside the swap's rollback envelope, so a
    # failed promotion restores the previous plugin tree.
    staging = destination.parent / f".{PLUGIN_NAME}.staging-{os.getpid()}"
    if staging.exists():
        shutil.rmtree(staging)
    try:
        shutil.copytree(source, staging)
        (staging / OWNERSHIP_MARKER).write_text(PLUGIN_NAME + "\n")
        if not (staging / ".codex-plugin" / "plugin.json").is_file():
            raise RuntimeError("staged plugin missing .codex-plugin/plugin.json")

        def _promote() -> None:
            temporary.replace(marketplace)

        _swap_directory(staging, destination, post_swap=_promote)
    finally:
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)
        if temporary.exists():
            temporary.unlink(missing_ok=True)
    return destination, marketplace


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-dir", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument(
        "--agents-dir",
        type=Path,
        default=Path.home() / ".agents",
        help="agents directory (default: ~/.agents; useful for isolated tests)",
    )
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--overwrite", action="store_true", help="replace an existing unmanaged plugin directory")
    args = parser.parse_args()
    destination, marketplace = install(args.source_dir.resolve(), args.agents_dir.expanduser().resolve(), dry_run=args.dry_run, overwrite=args.overwrite)
    print(f"plugin directory: {destination}")
    print(f"marketplace: {marketplace}")
    if not args.dry_run:
        print(f"Install with: codex plugin add {PLUGIN_NAME}@personal")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
