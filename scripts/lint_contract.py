"""Reject new substantive Ruff findings while retaining visible legacy debt."""
from __future__ import annotations

import json
import subprocess
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BASELINE = ROOT / "scripts/lint_baseline.json"


def findings() -> Counter[str]:
    result = subprocess.run(["ruff", "check", "--select", "F,E4,E7,E9,W,B",
                             "--output-format", "json", "sieve/", "scripts/", "tests/"], cwd=ROOT,
                            capture_output=True, text=True, check=False)
    if result.returncode not in (0, 1):
        raise RuntimeError("Ruff could not run: " + result.stderr)
    counts: Counter[str] = Counter()
    for item in json.loads(result.stdout):
        path = Path(item["filename"])
        source = path.read_text().splitlines()[item["location"]["row"] - 1].strip()
        key = json.dumps([path.relative_to(ROOT).as_posix(), item["code"], item["message"], source])
        counts[key] += 1
    return counts


def main() -> int:
    current = findings()
    baseline = Counter(json.loads(BASELINE.read_text()))
    added = current - baseline
    for key, count in sorted(added.items()):
        path, code, message, source = json.loads(key)
        print(f"{path}: {code} {message} ({count}): {source}")
    print(f"Ruff: {sum(current.values())} legacy findings; {sum(added.values())} new")
    return bool(added)


if __name__ == "__main__":
    raise SystemExit(main())
