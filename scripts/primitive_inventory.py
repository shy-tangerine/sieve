"""Review direct network, subprocess, and persistent-write callsites.

This inventory is a static review aid, not proof that every side effect is
bounded or that dynamic calls are absent. It recognizes imported aliases and
simple local client/path assignments; reflection, dependency internals, and
runtime-generated callables can be missed.
"""
from __future__ import annotations

import argparse
import ast
import json
from collections import defaultdict
from pathlib import Path

if __package__:
    from .report_output import write_report
else:
    from report_output import write_report

ROOT = Path(__file__).resolve().parents[1]
RUNTIME_SCRIPT_PATHS = (
    "scripts/build_attestation_material.py",
    "scripts/check_dependency_compatibility.py",
    "scripts/check_security_mutations.py",
    "scripts/check_typing_consumer.py",
    "scripts/lint_contract.py",
    "scripts/lint_broad_except.py",
    "scripts/install_codex_plugin.py",
    "scripts/bench_v2.py",
    "scripts/architecture_report.py",
    "scripts/check_public_tree.py",
    "scripts/validate_distribution.py",
    "scripts/generate_adblock_data.py",
    "scripts/report_output.py",
    "scripts/primitive_inventory.py",
    "scripts/generate_docs_capabilities.py",
    "plugins/claude/sieve-web/hooks/session-start.py",
)
NETWORK_CALLS = {
    "urllib.request.urlopen", "urllib.request.urlretrieve", "urllib.urlopen",
    "httpx.get", "httpx.post", "httpx.put", "httpx.patch", "httpx.delete",
    "httpx.head", "httpx.options", "requests.get", "requests.post",
    "requests.put", "requests.patch", "requests.delete", "requests.head",
    "requests.options", "primp.get", "primp.post", "socket.create_connection",
}
NETWORK_CLIENTS = {
    "httpx.Client", "httpx.AsyncClient", "requests.Session", "primp.Client",
    "primp.ImpersonateClient", "aiohttp.ClientSession",
    "httpcore.ConnectionPool", "httpcore.AsyncConnectionPool", "socket.socket",
}
NETWORK_CLIENT_FACTORIES = {
    "urllib.request.build_opener": "urllib.request.OpenerDirector",
}
NETWORK_METHODS = {"get", "post", "put", "patch", "delete", "head", "options", "request", "send", "stream", "fetch", "connect", "connect_ex"}
SUBPROCESS_CALLS = {f"subprocess.{name}" for name in ("run", "Popen", "call", "check_call", "check_output")} | {
    "asyncio.create_subprocess_exec", "asyncio.create_subprocess_shell",
}
PATH_CONSTRUCTORS = {"pathlib.Path", "pathlib.PurePath", "Path", "PurePath"}
PATH_METHODS = {"write_text", "write_bytes", "touch", "mkdir", "unlink", "rmdir", "rename", "replace"}
FILE_OBJECT_METHODS = {"write", "writelines", "truncate"}
FILE_MUTATION_CALLS = {
    "os.open", "os.remove", "os.unlink", "os.rename", "os.replace", "os.mkdir", "os.makedirs",
    "shutil.copy", "shutil.copy2", "shutil.copyfile", "shutil.copytree", "shutil.move", "shutil.rmtree",
    "tempfile.TemporaryFile", "tempfile.NamedTemporaryFile", "tempfile.TemporaryDirectory",
    "tempfile.mkstemp", "tempfile.mkdtemp", "sqlite3.connect",
}
WRITE_MODES = set("wax+")


def _mode(call: ast.Call, position: int = 1) -> str | None:
    value = call.args[position] if len(call.args) > position else next((kw.value for kw in call.keywords if kw.arg == "mode"), None)
    if isinstance(value, ast.Constant) and isinstance(value.value, str):
        return value.value
    return None


def _writes(call: ast.Call, position: int = 1) -> bool:
    mode = _mode(call, position)
    return bool(mode and WRITE_MODES.intersection(mode))


class _FileInventory:
    def __init__(self, path: Path, root: Path):
        self.path, self.root = path, root
        self.tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        self.owner_parent: dict[str, str] = {"<module>": ""}
        self.node_owner: dict[ast.AST, str] = {}
        self.aliases: dict[str, dict[str, str]] = defaultdict(dict)
        self.types: dict[str, dict[str, str]] = defaultdict(dict)
        self.file_handles: set[tuple[str, str]] = set()
        self.counts: dict[tuple[str, str, str], int] = defaultdict(int)
        self.calls: list[dict] = []
        self._owners(self.tree, "<module>", "")
        self._imports()
        self._assignments()

    def _owners(self, node: ast.AST, owner: str, parent: str) -> None:
        self.node_owner[node] = owner
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                child_owner = f"{owner}.{child.name}" if owner != "<module>" else child.name
                self.owner_parent[child_owner] = owner
                self._owners(child, child_owner, owner)
            else:
                self._owners(child, owner, parent)

    def _imports(self) -> None:
        for node in ast.walk(self.tree):
            owner = self.node_owner[node]
            if isinstance(node, ast.Import):
                for item in node.names:
                    local = item.asname or item.name.split(".")[0]
                    self.aliases[owner][local] = item.name if item.asname else local
            elif isinstance(node, ast.ImportFrom) and node.module:
                for item in node.names:
                    self.aliases[owner][item.asname or item.name] = f"{node.module}.{item.name}"

    def _resolve(self, node: ast.AST, owner: str) -> str:
        if isinstance(node, ast.Name):
            scope = owner
            while scope:
                if node.id in self.aliases[scope]:
                    return self.aliases[scope][node.id]
                scope = self.owner_parent.get(scope, "")
            return node.id
        if isinstance(node, ast.Attribute):
            base = self._resolve(node.value, owner)
            return f"{base}.{node.attr}"
        return ""

    def _assignments(self) -> None:
        for node in ast.walk(self.tree):
            if isinstance(node, (ast.With, ast.AsyncWith)):
                owner = self.node_owner[node]
                for item in node.items:
                    call = item.context_expr
                    if isinstance(call, ast.Call) and item.optional_vars:
                        constructor = self._resolve(call.func, owner)
                        if constructor in NETWORK_CLIENTS | PATH_CONSTRUCTORS:
                            self.types[owner][ast.unparse(item.optional_vars)] = constructor
                    if isinstance(call, ast.Call) and self._is_writable_open(call, owner) and item.optional_vars:
                        self.file_handles.add((owner, ast.unparse(item.optional_vars)))
            if not isinstance(node, (ast.Assign, ast.AnnAssign, ast.NamedExpr)):
                continue
            value = node.value
            if not isinstance(value, ast.Call):
                continue
            owner = self.node_owner[node]
            constructor = self._resolve(value.func, owner)
            targets = getattr(node, "targets", [getattr(node, "target", None)])
            client_type = NETWORK_CLIENT_FACTORIES.get(constructor, constructor)
            if constructor in NETWORK_CLIENTS or constructor in NETWORK_CLIENT_FACTORIES:
                for target in targets:
                    if target is None:
                        continue
                    self.types[owner][ast.unparse(target)] = client_type
            if constructor in PATH_CONSTRUCTORS:
                for target in targets:
                    if target is None:
                        continue
                    self.types[owner][ast.unparse(target)] = constructor
            if self._is_writable_open(value, owner):
                for target in targets:
                    if target is None:
                        continue
                    self.file_handles.add((owner, ast.unparse(target)))

    def _is_writable_open(self, call: ast.Call, owner: str) -> bool:
        constructor = self._resolve(call.func, owner)
        if constructor in {"open", "builtins.open"}:
            return _writes(call)
        if isinstance(call.func, ast.Attribute):
            return call.func.attr == "open" and self._receiver_type(call.func.value, owner) in PATH_CONSTRUCTORS and _writes(call, 0)
        return False

    def _type(self, owner: str, variable: str) -> str:
        scope = owner
        while scope:
            if variable in self.types[scope]:
                return self.types[scope][variable]
            scope = self.owner_parent.get(scope, "")
        return ""

    def _receiver_type(self, receiver: ast.AST, owner: str) -> str:
        if isinstance(receiver, ast.Call):
            constructor = self._resolve(receiver.func, owner)
            return NETWORK_CLIENT_FACTORIES.get(constructor, constructor)
        return self._type(owner, ast.unparse(receiver))

    def _category(self, node: ast.Call, owner: str) -> tuple[str, str] | None:
        name = self._resolve(node.func, owner)
        if name in NETWORK_CALLS:
            return "network", name
        if name in SUBPROCESS_CALLS:
            return "subprocess", name
        if name in {"open", "builtins.open"} and _writes(node):
            return "persistent_write", name
        if name in FILE_MUTATION_CALLS:
            return "persistent_write", name
        if isinstance(node.func, ast.Attribute):
            method = node.func.attr
            receiver = ast.unparse(node.func.value)
            client = self._receiver_type(node.func.value, owner)
            if client in NETWORK_CLIENTS and method in NETWORK_METHODS:
                return "network", f"{client}.{method}"
            if client == "urllib.request.OpenerDirector" and method == "open":
                return "network", f"{client}.open"
            if client in PATH_CONSTRUCTORS and method in PATH_METHODS:
                return "persistent_write", f"pathlib.Path.{method}"
            if client in PATH_CONSTRUCTORS and method == "open" and _writes(node, 0):
                return "persistent_write", "pathlib.Path.open"
            if (owner, receiver) in self.file_handles and method in FILE_OBJECT_METHODS:
                return "persistent_write", f"file.{method}"
            if method in {"open", "write_text", "write_bytes"} and name.startswith("pathlib.Path."):
                if method != "open" or _writes(node, 0):
                    return "persistent_write", name
        return None

    def scan(self) -> list[dict]:
        relpath = self.path.relative_to(self.root).as_posix()
        calls = sorted((node for node in ast.walk(self.tree) if isinstance(node, ast.Call)), key=lambda node: (node.lineno, node.col_offset))
        for node in calls:
            owner = self.node_owner[node]
            result = self._category(node, owner)
            if not result:
                continue
            category, callee = result
            key = (owner, category, callee)
            self.counts[key] += 1
            ordinal = self.counts[key]
            identity = f"{relpath}::{owner}::{category}::{callee}#{ordinal}"
            self.calls.append({
                "id": identity,
                "file": relpath,
                "line": node.lineno,
                "function": owner,
                "category": category,
                "callee": callee,
                "owner": owner,
                "owner_review": "enclosing function only; accountable owner unverified",
                "bounds_review": "unknown; review the owning function",
                "secret_policy_review": "unknown; review credential flow in the owning function",
                "review_status": "unreviewed baseline entry",
                "rationale": "Pre-existing callsite frozen for explicit security review; rationale not yet verified.",
            })
        return self.calls


def build_inventory(root: Path = ROOT) -> dict:
    files = sorted((root / "sieve").rglob("*.py"))
    files.extend(root / relative for relative in RUNTIME_SCRIPT_PATHS if (root / relative).is_file())
    calls = [call for path in files for call in _FileInventory(path, root).scan()]
    return {
        "schema_version": 1,
        "scope": {"package": "sieve/", "runtime_scripts": list(RUNTIME_SCRIPT_PATHS)},
        "limitations": [
            "Static AST inventory is a review aid, not a sandbox or a complete side-effect proof.",
            "Dynamic dispatch, reflection, dependencies, dynamic file modes, and unrecognized client/path construction may be missed.",
            "Alias and constructor resolution is flow-insensitive; shadowing or reassignment can produce false positives.",
            "The owner is the enclosing function; accountable ownership is unverified.",
            "Bounds and credential handling remain unreviewed; review each owning function before treating an entry as approved.",
        ],
        "calls": sorted(calls, key=lambda call: call["id"]),
    }


def compare_inventory(current: dict, baseline: dict) -> list[str]:
    def entries(report: dict) -> dict[str, dict]:
        return {call["id"]: call for call in report["calls"]}

    now, before = entries(current), entries(baseline)
    errors = []
    for field in ("schema_version", "scope"):
        if current.get(field) != baseline.get(field):
            errors.append(f"inventory {field} changed")
    for identity in sorted(now.keys() - before.keys()):
        call = now[identity]
        errors.append(f"added {call['file']}:{call['line']} {call['function']} [{call['category']}] {call['callee']} ({identity})")
    for identity in sorted(before.keys() - now.keys()):
        call = before[identity]
        errors.append(f"removed {call['file']}:{call['line']} {call['function']} [{call['category']}] {call['callee']} ({identity})")
    return errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="compare with the reviewed baseline")
    parser.add_argument("--output", type=Path, help="explicitly regenerate the reviewed baseline")
    args = parser.parse_args(argv)
    report = build_inventory()
    snapshot = ROOT / "scripts" / "primitive_inventory_snapshot.json"
    if args.output:
        write_report(args.output, json.dumps(report, indent=2, sort_keys=True) + "\n")
        return 0
    if args.check:
        errors = compare_inventory(report, json.loads(snapshot.read_text(encoding="utf-8")))
        if errors:
            print("\n".join(errors))
            return 1
        return 0
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
