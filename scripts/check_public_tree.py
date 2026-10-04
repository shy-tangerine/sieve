#!/usr/bin/env python3
"""Reject maintainer-only files from the tracked public repository."""

from __future__ import annotations

import subprocess
import sys
import argparse
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
FORBIDDEN_EXACT = frozenset({
    "AGENTS.md", "DESIGN.md", "MIGRATION.md", "SOURCES.md", "UPSTREAM-WATCH.md",
    ".mcp.json", ".repowise-workspace.yaml",
})
FORBIDDEN_PREFIXES = (
    ".claude/", ".codebase-memory/", ".repowise/", ".sieve/", ".hypothesis/",
    ".freebuff/", ".config/systemd/", "scripts/hermes/", "skills/",
    "skills/sieve-development/", "wiki/",
)
FORBIDDEN_PATHS = frozenset({
    "sieve.service",
    "docs/agent-development.md", "docs/benchmarks.md", "docs/benchmarks-chart.svg",
    "docs/media-testing.md", "docs/methodology.md", "docs/release-policy.md",
    "docs/release.md", "docs/takedown-risk.md", "scripts/audit_local_upstreams.py",
    "scripts/bench_chart.py", "scripts/bench_competitors.py", "scripts/check_public_sync.py",
    "scripts/check_upstreams.py", "scripts/export_public.py", "scripts/install_release_hooks.py",
    "scripts/public_release_audit.py", "scripts/public_release_policy.json",
    "scripts/workflow_gate.py", "tests/test_bench.py", "tests/test_check_public_sync.py",
    "tests/test_no_operator_crumbs.py", "tests/test_public_release_audit.py",
    "tests/test_release_policy_adversarial.py", "tests/test_upstream_watch.py",
    "tests/test_workflow_gate.py", ".github/workflows/upstream-watch.yml",
})


def tracked_paths(root: Path = ROOT) -> list[str]:
    output = subprocess.check_output(["git", "ls-files", "-z"], cwd=root)
    return [path for path in output.decode().split("\0") if path]


def violations(paths: list[str]) -> list[str]:
    return sorted(
        path for path in paths
        if path in FORBIDDEN_EXACT or path in FORBIDDEN_PATHS or path.startswith(FORBIDDEN_PREFIXES)
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, help="check every file in a staged public artifact")
    args = parser.parse_args(argv)
    paths = tracked_paths() if args.root is None else [
        path.relative_to(args.root).as_posix()
        for path in args.root.rglob("*") if path.is_file() or path.is_symlink()
    ]
    found = violations(paths)
    if args.root is not None and (args.root / ".git").exists():
        found.append(".git")
    if found:
        print("Maintainer-only paths in public tree:", file=sys.stderr)
        print("\n".join(f"- {path}" for path in found), file=sys.stderr)
        return 1
    print("Public tree check: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
