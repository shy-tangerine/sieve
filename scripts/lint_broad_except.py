#!/usr/bin/env python3
"""Reject undocumented new broad catches; retain exact, visible legacy debt."""
from __future__ import annotations

import ast
import hashlib
import io
import json
import sys
import tokenize
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TARGET = ROOT / "sieve"
BASELINE = Path(__file__).resolve().parent / "broad_except_baseline.txt"
RATIONALE_MARKERS = ("issue #", "rationale:", "fallback boundary")


def _identity(value):
    """Normalize optional AST fields across supported Python versions."""
    if isinstance(value, ast.AST):
        return [type(value).__name__, [[name, _identity(item)] for name, item in ast.iter_fields(value)
                                      if item is not None and item != []]]
    if isinstance(value, list):
        return [_identity(item) for item in value]
    return value


def check_file(path: Path, *, rel: str | None = None) -> list[str]:
    """Real comment rationales apply to one catch, never an entire module."""
    rel = rel or path.relative_to(ROOT).as_posix()
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(path))
    comments = [(token.start[0], token.string.lower())
                for token in tokenize.generate_tokens(io.StringIO(source).readline)
                if token.type == tokenize.COMMENT]
    violations = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Try, ast.TryStar)):
            continue
        for handler in node.handlers:
            types = handler.type.elts if isinstance(handler.type, ast.Tuple) else [handler.type]
            broad = next((item.id for item in types if isinstance(item, ast.Name)
                          and item.id in {"Exception", "BaseException"}), None)
            if handler.type is not None and broad is None:
                continue
            rationale = " ".join(text for line, text in comments
                                 if line == node.lineno or handler.lineno <= line <= handler.end_lineno)
            if any(marker in rationale for marker in RATIONALE_MARKERS):
                continue
            site = hashlib.sha256(json.dumps(_identity(node), separators=(",", ":"), default=repr).encode()).hexdigest()[:20]
            violations.append(f"{rel}:{handler.lineno}: broad 'except {broad or 'bare'}' "
                              f"without inline rationale [site={site}]")
    return violations


def _violation_key(violation: str) -> str:
    """Line movement is harmless; replacing a catch changes its AST identity."""
    return violation.split(":", 1)[0] + ":" + violation.rsplit("[site=", 1)[1].rstrip("]")


def load_baseline() -> Counter[str]:
    counts: Counter[str] = Counter()
    if BASELINE.exists():
        for line in BASELINE.read_text().splitlines():
            if line.strip() and not line.startswith("#"):
                key, count = line.rsplit(":", 1)
                counts[key] = int(count)
    return counts


def save_baseline(counts: Counter[str]) -> None:
    header = "# Reviewed legacy broad catches, issue #234. New sites require a comment rationale.\n# file:AST fingerprint:count\n\n"
    BASELINE.write_text(header + "".join(f"{key}:{count}\n" for key, count in sorted(counts.items())))


def main(argv: list[str]) -> int:
    if "--staged" in argv:
        import subprocess
        result = subprocess.run(["git", "diff", "--cached", "--name-only", "--", "sieve/"],
                                capture_output=True, text=True, check=True)
        files = [ROOT / name for name in result.stdout.splitlines()
                 if name.endswith(".py") and (ROOT / name).is_file()]
    else:
        files = sorted(TARGET.rglob("*.py"))
    violations = [item for path in files for item in check_file(path)]
    current = Counter(_violation_key(item) for item in violations)
    baseline = load_baseline()
    added = current - baseline
    if "--report" in argv:
        print("\n".join(violations))
    if added:
        for key, count in sorted(added.items()):
            print(f"NEW broad catch: {key} ({count}); narrow it or add a comment rationale")
    elif "--update-baseline" in argv:
        if "--staged" in argv:
            raise ValueError("baseline cleanup requires the whole tree")
        save_baseline(current)
    print(f"broad-except policy: {len(violations)} legacy catches, {sum(added.values())} new")
    return bool(added) and "--report" not in argv


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
