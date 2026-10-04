"""Generate and validate Sieve's import and public-surface contract.

The report is derived from Python syntax, so it works in a clean checkout and
does not import optional browser or MCP dependencies.  CI uses ``--check``;
developers can write the generated JSON with ``--output`` for review.
Output replacement is atomic; existing symlinks are replaced, not followed.
"""
from __future__ import annotations

import argparse
import ast
import json
import tomllib
from dataclasses import dataclass
from pathlib import Path

if __package__:
    from .report_output import write_report
else:
    from report_output import write_report

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "sieve"
FORBIDDEN = {("sieve.server_fetch", "sieve.server"), ("sieve.server_search", "sieve.server")}

@dataclass(frozen=True)
class AllowedCycle:
    """Documented exception owned by its narrowest existing interface."""

    modules: frozenset[str]
    rationale: str
    owner: str
    revisit_condition: str
    issue: int = 57
    reviewed_version: str = "13.2.1"
    expires_version: str = "13.3.0"


# These are temporary, optional-feature seams, not a general cycle waiver.
# Every entry names an owner and an observable condition for removing it.
ALLOWED_CYCLES = (
    AllowedCycle(
        frozenset({"sieve.api_backends", "sieve.search_metasearch"}),
        "Provider registry lazily loads metasearch adapters.",
        "search provider seam",
        "Revisit when provider registration moves behind search application boundary.",
    ),
    AllowedCycle(
        frozenset({"sieve.api_backends", "sieve.search_metasearch", "sieve.search_api_keys"}),
        "Optional provider configuration and metasearch share lazy key lookup.",
        "search provider seam",
        "Revisit when provider configuration has injected key access.",
    ),
    AllowedCycle(
        frozenset({"sieve.crawl", "sieve.server", "sieve.server_search"}),
        "Legacy search facade and crawl path retain optional server dispatch.",
        "retrieval application seam",
        "Revisit when legacy server search dispatch routes through retrieval interface.",
    ),
    AllowedCycle(
        frozenset({"sieve.ocr", "sieve.pdf_extractor"}),
        "PDF extraction lazily shares optional OCR implementation.",
        "document extraction seam",
        "Revisit when OCR is injected into PDF extraction.",
    ),
    AllowedCycle(
        frozenset({"sieve.search_metasearch", "sieve.search_api_keys"}),
        "Metasearch providers lazily read optional API keys.",
        "search provider seam",
        "Revisit when provider key access is supplied by search application boundary.",
    ),
)


def _module(path: Path, package: Path = PACKAGE) -> str:
    return "sieve." + path.relative_to(package).with_suffix("").as_posix().replace("/", ".").removesuffix(".__init__")


def build_report(root: Path = ROOT) -> dict:
    imports: dict[str, list[str]] = {}
    public: dict[str, list[str]] = {}
    for path in sorted((root / "sieve").rglob("*.py")):
        module = _module(path, root / "sieve")
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        deps: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                deps.update(alias.name for alias in node.names if alias.name.startswith("sieve"))
            elif isinstance(node, ast.ImportFrom):
                if node.module and node.module.startswith("sieve"):
                    deps.add(node.module)
        imports[module] = sorted(deps)
        names = tree.body
        explicit = next((n for n in names if isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "__all__" for t in n.targets)), None)
        if explicit and isinstance(explicit.value, (ast.List, ast.Tuple)):
            exported = [x.value for x in explicit.value.elts if isinstance(x, ast.Constant) and isinstance(x.value, str)]
        else:
            exported = sorted(n.name for n in names if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and not n.name.startswith("_"))
        public[module] = exported
    return {"schema_version": 1, "imports": imports, "public": public}


def compare_snapshot(report: dict, baseline: dict) -> list[str]:
    """Compare exact sets, including replacements that preserve total counts."""
    def entries(data: dict) -> dict[str, set]:
        return {
            "modules": set(data["imports"]),
            "imports": {(module, target) for module, targets in data["imports"].items() for target in targets},
            "exports": {(module, name) for module, names in data["public"].items() for name in names},
        }

    errors = []
    if report["schema_version"] != baseline["schema_version"]:
        errors.append("architecture snapshot schema changed")
    current, expected = entries(report), entries(baseline)
    for kind in current:
        added, removed = current[kind] - expected[kind], expected[kind] - current[kind]
        if added or removed:
            errors.append(f"{kind} drift: added={sorted(added)}; removed={sorted(removed)}")
    return errors


def _cycles(graph: dict[str, list[str]]) -> list[list[str]]:
    found: list[list[str]] = []
    seen: set[tuple[str, ...]] = set()

    def canonical(cycle: list[str]) -> tuple[str, ...]:
        rotations = [tuple(cycle[i:] + cycle[:i]) for i in range(len(cycle))]
        reverse = list(reversed(cycle))
        rotations.extend(tuple(reverse[i:] + reverse[:i]) for i in range(len(cycle)))
        return min(rotations)

    for start in graph:
        stack: list[tuple[str, list[str]]] = [(start, [start])]
        while stack:
            node, path = stack.pop()
            for nxt in graph.get(node, []):
                if nxt == start:
                    cycle = path[:]
                    key = canonical(cycle)
                    if key not in seen:
                        found.append(list(key))
                        seen.add(key)
                elif nxt in graph and nxt not in path and len(path) < len(graph):
                    stack.append((nxt, path + [nxt]))
    return sorted(found)


def validate(report: dict, *, version: str | None = None, allowed_cycles=ALLOWED_CYCLES) -> list[str]:
    errors = []
    for source, targets in report["imports"].items():
        for target in targets:
            if (source, target) in FORBIDDEN:
                errors.append(f"forbidden transport import: {source} -> {target}")
    allowed = {entry.modules for entry in allowed_cycles}
    seen_cycles: set[frozenset[str]] = set()
    for cycle in _cycles(report["imports"]):
        modules = frozenset(cycle)
        if modules not in allowed and modules not in seen_cycles:
            errors.append(f"import cycle: {' -> '.join(cycle + [cycle[0]])}")
        seen_cycles.add(modules)
    if version is None:
        version = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"]
    release = tuple(int(part) for part in version.split(".")[:3])
    for entry in allowed_cycles:
        if not entry.modules <= report["imports"].keys():
            continue
        if entry.modules not in seen_cycles:
            errors.append(f"stale cycle waiver #{entry.issue}: {sorted(entry.modules)}")
        expiry = tuple(int(part) for part in entry.expires_version.split("."))
        if release >= expiry:
            errors.append(f"expired cycle waiver #{entry.issue}: {sorted(entry.modules)}; review before {entry.expires_version}")
        if not all((entry.rationale, entry.owner, entry.revisit_condition, entry.reviewed_version)):
            errors.append(f"incomplete cycle waiver #{entry.issue}: {sorted(entry.modules)}")
    return errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args(argv)
    report = build_report()
    if args.output:
        write_report(args.output, json.dumps(report, indent=2, sort_keys=True) + "\n")
    errors = validate(report)
    if args.check:
        snapshot = ROOT / "scripts" / "architecture_snapshot.json"
        errors.extend(compare_snapshot(report, json.loads(snapshot.read_text(encoding="utf-8"))))
    if errors:
        for error in errors:
            print(error)
        return 1
    if not args.output:
        print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
