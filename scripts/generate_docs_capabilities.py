#!/usr/bin/env python3
"""Generate and check README capability metadata and public-definition hashes."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if __package__:
    from .report_output import write_report
else:
    sys.path.insert(0, str(ROOT))
    from report_output import write_report

# Standalone invocation establishes the package path before this import.
from sieve.command_router import CAPABILITY_REGISTRY  # noqa: E402

README = ROOT / "README.md"
MANIFEST = ROOT / "docs" / "capability-manifest.json"
START = "<!-- generated-public-capabilities:start -->"
END = "<!-- generated-public-capabilities:end -->"


def _extras() -> set[str]:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    return set(project["project"]["optional-dependencies"])


def render_manifest() -> str:
    available = _extras()
    tools = {}
    for name, capability in sorted(CAPABILITY_REGISTRY.items()):
        definition = capability.definition
        if definition is None:
            continue
        missing = set(capability.extras) - available
        if missing:
            raise ValueError(f"{name} refers to undefined extras: {sorted(missing)}")
        if definition.get("name") != name:
            raise ValueError(f"{name} has a noncanonical public definition")
        normalized = json.dumps(
            definition, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode("utf-8")
        tools[name] = {
            "definition_sha256": hashlib.sha256(normalized).hexdigest(),
            "required_extras": list(capability.extras),
        }
    return json.dumps({"schema_version": 1, "tools": tools}, indent=2, sort_keys=True) + "\n"


def render_block() -> str:
    available = _extras()
    rows = [START, "| Public MCP tool | Required extra | Input fields |", "|---|---|---|"]
    for name, capability in sorted(CAPABILITY_REGISTRY.items()):
        definition = capability.definition
        if definition is None:
            continue
        missing = set(capability.extras) - available
        if missing:
            raise ValueError(f"{name} refers to undefined extras: {sorted(missing)}")
        fields = definition.get("inputSchema", {}).get("properties", {})
        if definition.get("name") != name:
            raise ValueError(f"{name} has an invalid public tool definition")
        required = ", ".join(f"`{extra}`" for extra in capability.extras) or "core"
        inputs = ", ".join(f"`{field}`" for field in sorted(fields)) or "none"
        rows.append(f"| `{name}` | {required} | {inputs} |")
    rows.append(END)
    return "\n".join(rows)


def replace_block(readme: str, generated: str) -> str:
    start = readme.index(START)
    end = readme.index(END, start) + len(END)
    return readme[:start] + generated + readme[end:]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--write", action="store_true", help="update README and manifest")
    args = parser.parse_args()
    readme = README.read_text(encoding="utf-8")
    manifest = render_manifest()
    generated_readme = replace_block(readme, render_block())
    if args.write:
        write_report(README, generated_readme)
        write_report(MANIFEST, manifest)
        return 0
    if generated_readme != readme or not MANIFEST.exists() or MANIFEST.read_text(encoding="utf-8") != manifest:
        parser.error("capability docs are stale; run scripts/generate_docs_capabilities.py --write")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
