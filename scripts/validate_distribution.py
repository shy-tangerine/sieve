"""Validate release metadata without contacting or publishing to registries."""

from __future__ import annotations

import json
import re
import sys
import tomllib
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _wheel_public_paths(wheel_path: Path) -> list[str]:
    """Inspect every non-generated wheel path, including top-level scripts."""
    import zipfile

    with zipfile.ZipFile(wheel_path) as zf:
        return [
            name for name in zf.namelist()
            if ".dist-info" not in Path(name).parts[0]
        ]


def _load_policy():
    """Load the export policy from this repository's scripts/export_public.py,
    falling back to the local check_public_tree violations when unavailable."""
    try:
        sys.path.insert(0, str(ROOT))
        from scripts.export_public import is_public_path
        return lambda p: not is_public_path(Path(p))
    except Exception:
        pass
    try:
        sys.path.insert(0, str(ROOT))
        from scripts.check_public_tree import violations
        return lambda p: bool(violations([p]))
    except Exception as exc:
        raise RuntimeError(f"no public-tree policy available: {exc}") from exc


def _validate_wheel_contents() -> list[str]:
    """Run the public-tree policy against real wheel contents (issue #181).

    Path-example checks verify source layout; this closes the gap where a
    packaged wheel could ship a maintainer-only file the source tree guard
    never sees. Skips quietly when no wheel has been built (uv build runs in
    the dedicated build gate).
    """
    failures: list[str] = []
    dist = ROOT / "dist"
    artifacts = sorted([*dist.glob("*.whl"), *dist.glob("*.tar.gz")]) if dist.is_dir() else []
    if not artifacts:
        return failures
    try:
        violates = _load_policy()
    except RuntimeError as exc:
        return [str(exc)]
    for artifact in artifacts:
        try:
            if artifact.suffix == ".whl":
                packaged = _wheel_public_paths(artifact)
            else:
                import tarfile
                with tarfile.open(artifact) as archive:
                    packaged = []
                    for member in archive.getmembers():
                        if member.issym() or member.islnk() or member.isdev():
                            raise ValueError("source distribution contains a link or special file")
                        if not member.isfile():
                            continue
                        path = member.name.partition("/")[2]
                        if path in {"PKG-INFO", "setup.cfg"} or ".egg-info" in Path(path).parts[0]:
                            continue
                        packaged.append(path)
        except Exception as exc:
            failures.append(f"cannot read {artifact.name}: {exc}")
            continue
        bad = sorted(p for p in packaged if Path(p).is_absolute() or ".." in Path(p).parts or violates(p))
        if bad:
            failures.append(
                f"{artifact.name} ships policy-violating paths: {', '.join(bad[:10])}"
            )
    return failures


def _json_object(path: Path) -> dict:
    value = json.loads(path.read_text())
    if not isinstance(value, dict):
        raise ValueError("top-level JSON must be an object")
    return value


def _strings(data: dict, *fields: str) -> None:
    for field in fields:
        if not isinstance(data.get(field), str) or not data[field].strip():
            raise ValueError(f"{field} must be a nonempty string")


def _local_path(base: Path, value: str, *, directory: bool) -> Path:
    if not isinstance(value, str) or not value.startswith("./"):
        raise ValueError("local source paths must start with ./")
    try:
        path = (base / value).resolve()
    except (OSError, RuntimeError) as exc:
        raise ValueError("local source path cannot be resolved") from exc
    if not path.is_relative_to(base.resolve()):
        raise ValueError("local source path escapes its plugin root")
    if not (path.is_dir() if directory else path.is_file()):
        raise ValueError(f"local source path does not exist: {value}")
    return path


def _validate_metadata(project: dict) -> list[str]:
    """Validate the supported plugin and stdio registry contracts offline.

    Plugin manifests intentionally retain their independent version numbers;
    deciding release-version alignment belongs to the separate version policy.
    """
    failures = []
    version = project.get("version")
    urls = project.get("urls")
    if not isinstance(urls, dict) or not isinstance(urls.get("Repository"), str) or not urls["Repository"].strip():
        return ["pyproject Repository URL must be a nonempty string"]
    repository = urls["Repository"]
    if urls.get("Homepage") != repository:
        failures.append("pyproject Homepage and Repository must agree")
    registry_path = ROOT / "server.json"
    try:
        registry = _json_object(registry_path)
        _strings(registry, "$schema", "name", "description", "version")
        if registry["$schema"] != "https://static.modelcontextprotocol.io/schemas/2025-12-11/server.schema.json":
            raise ValueError("$schema must name the supported MCP registry schema")
        if not registry["name"].startswith("io.github."):
            raise ValueError("name must use an io.github namespace")
        if registry["version"] != version:
            raise ValueError("version does not match pyproject.toml")
        if registry.get("repository") != {"url": repository, "source": "github"}:
            raise ValueError("repository must match the public project repository")
        packages = registry.get("packages")
        if not isinstance(packages, list) or len(packages) != 1 or not isinstance(packages[0], dict):
            raise ValueError("packages must contain one PyPI package object")
        package = packages[0]
        expected = {
            "registryType": "pypi", "registryBaseUrl": "https://pypi.org",
            "identifier": "sieve-cli", "version": version, "runtimeHint": "uvx",
            "transport": {"type": "stdio"},
            "packageArguments": [
                {"type": "positional", "value": "mcp"},
                {"type": "positional", "value": "serve"},
                {"type": "named", "name": "--transport", "value": "stdio"},
            ],
        }
        for field, value in expected.items():
            if package.get(field) != value:
                raise ValueError(f"PyPI package {field} does not match the supported stdio command")
    except (OSError, ValueError) as exc:
        failures.append(f"server.json is invalid: {exc}")

    for relative, source_field in (
        ("plugins/codex/sieve-web-research/.codex-plugin/plugin.json", "skills"),
        ("plugins/claude/sieve-web/.claude-plugin/plugin.json", "hooks"),
    ):
        path = ROOT / relative
        try:
            plugin = _json_object(path)
            _strings(plugin, "name", "description", "version", "license", "homepage", "repository")
            if not re.fullmatch(r"\d+\.\d+\.\d+", plugin["version"]):
                raise ValueError("version must be a stable semver triplet")
            if plugin["homepage"] != repository or plugin["repository"] != repository:
                raise ValueError("homepage/repository must match the public project repository")
            if not isinstance(plugin.get("author"), dict):
                raise ValueError("author must be an object")
            _strings(plugin["author"], "name")
            if "url" in plugin["author"] and plugin["author"]["url"] != repository:
                raise ValueError("author url must match the public project repository")
            keywords = plugin.get("keywords")
            if not isinstance(keywords, list) or not keywords or any(not isinstance(k, str) or not k.strip() for k in keywords):
                raise ValueError("keywords must be a nonempty list of strings")
            _local_path(path.parent.parent, plugin.get(source_field), directory=source_field == "skills")
            if source_field == "skills":
                interface = plugin.get("interface")
                if not isinstance(interface, dict):
                    raise ValueError("interface must be an object")
                _strings(interface, "displayName", "shortDescription", "longDescription", "developerName", "category")
                if interface.get("websiteURL") != repository:
                    raise ValueError("interface websiteURL must match the public project repository")
                for field in ("capabilities", "defaultPrompt"):
                    values = interface.get(field)
                    if not isinstance(values, list) or not values or any(not isinstance(v, str) or not v.strip() for v in values):
                        raise ValueError(f"interface {field} must be a nonempty list of strings")
        except (OSError, ValueError) as exc:
            failures.append(f"{relative} is invalid: {exc}")

    relative = "plugins/claude/.claude-plugin/marketplace.json"
    path = ROOT / relative
    try:
        marketplace = _json_object(path)
        _strings(marketplace, "name")
        if not isinstance(marketplace.get("owner"), dict):
            raise ValueError("owner must be an object")
        _strings(marketplace["owner"], "name")
        plugins = marketplace.get("plugins")
        if not isinstance(plugins, list) or not plugins:
            raise ValueError("plugins must be a nonempty list")
        for plugin in plugins:
            if not isinstance(plugin, dict):
                raise ValueError("plugin entries must be objects")
            _strings(plugin, "name", "description", "version", "source")
            if plugin["version"] != version:
                raise ValueError("plugin version does not match pyproject.toml")
            source = _local_path(path.parent.parent, plugin["source"], directory=True)
            manifest = _json_object(source / ".claude-plugin/plugin.json")
            if plugin["name"] != manifest.get("name"):
                raise ValueError("plugin name does not match its source manifest")
    except (OSError, ValueError) as exc:
        failures.append(f"{relative} is invalid: {exc}")
    return failures


def main() -> int:
    failures: list[str] = []
    with (ROOT / "pyproject.toml").open("rb") as handle:
        project = tomllib.load(handle)["project"]
    version = project.get("version")
    if not isinstance(version, str) or not re.fullmatch(r"\d+\.\d+\.\d+", version):
        failures.append("pyproject project.version must be a stable semver triplet")
    if project.get("name") != "sieve-cli":
        failures.append("PyPI project name must remain sieve-cli")
    if "sieve" not in project.get("scripts", {}):
        failures.append("the sieve console script is missing")

    failures.extend(_validate_metadata(project))

    dockerfile = (ROOT / "Dockerfile").read_text()
    for target in ("lite", "browser"):
        if f"FROM base AS {target}" not in dockerfile:
            failures.append(f"Dockerfile is missing the {target} target")

    failures.extend(_validate_wheel_contents())

    if failures:
        for failure in failures:
            print(f"FAIL: {failure}", file=sys.stderr)
        return 1
    print(f"distribution metadata: PASS (sieve-cli {version})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
