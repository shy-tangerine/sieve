import json
import subprocess
import sys
import tomllib
import shutil
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
CODEX = "plugins/codex/sieve-web-research/.codex-plugin/plugin.json"
CLAUDE = "plugins/claude/sieve-web/.claude-plugin/plugin.json"
MARKETPLACE = "plugins/claude/.claude-plugin/marketplace.json"


@pytest.fixture
def metadata_checkout(tmp_path, monkeypatch):
    from scripts import validate_distribution

    for name in ["plugins", "server.json", "pyproject.toml", "Dockerfile"]:
        source = ROOT / name
        if source.is_dir():
            shutil.copytree(source, tmp_path / name)
        else:
            shutil.copy2(source, tmp_path / name)
    monkeypatch.setattr(validate_distribution, "ROOT", tmp_path)
    return tmp_path, validate_distribution


@pytest.mark.parametrize("urls", [None, [], {}, {"Repository": 42}])
def test_distribution_rejects_missing_project_repository(metadata_checkout, urls):
    root, validator = metadata_checkout
    with (root / "pyproject.toml").open("rb") as handle:
        project = tomllib.load(handle)["project"]
    project["urls"] = urls
    assert validator._validate_metadata(project) == ["pyproject Repository URL must be a nonempty string"]


@pytest.mark.parametrize("relative", ["server.json", CODEX, CLAUDE, MARKETPLACE])
@pytest.mark.parametrize("value", [None, [], 42, "text"])
def test_distribution_rejects_nonobject_metadata(metadata_checkout, capsys, relative, value):
    root, validator = metadata_checkout
    (root / relative).write_text(json.dumps(value))
    assert validator.main() == 1
    assert f"{relative} is invalid: top-level JSON must be an object" in capsys.readouterr().err


@pytest.mark.parametrize("relative", ["server.json", CODEX, CLAUDE, MARKETPLACE])
def test_distribution_rejects_malformed_json(metadata_checkout, capsys, relative):
    root, validator = metadata_checkout
    (root / relative).write_text('{"unfinished":')
    assert validator.main() == 1
    assert f"{relative} is invalid:" in capsys.readouterr().err


@pytest.mark.parametrize("relative,field,value", [
    (CODEX, "name", None), (CLAUDE, "description", 42),
    (CODEX, "author", []), (CLAUDE, "author", {"name": 3}),
    (CODEX, "keywords", "research"), (CLAUDE, "keywords", [3]),
    (CODEX, "homepage", "https://example.test/wrong"),
    (CLAUDE, "repository", "https://example.test/wrong"),
    (CODEX, "version", 1), (CLAUDE, "version", "invalid"),
    (CODEX, "skills", "./missing"), (CODEX, "skills", "./../../outside"),
    (CODEX, "skills", "/absolute"), (CLAUDE, "hooks", {}),
    (CLAUDE, "hooks", "./skills"), (CODEX, "interface", []),
    (MARKETPLACE, "owner", []), (MARKETPLACE, "plugins", {}),
    (MARKETPLACE, "plugins", [42]), (MARKETPLACE, "plugins", []),
    ("server.json", "name", []), ("server.json", "packages", {}),
    ("server.json", "$schema", "https://example.test/wrong"),
    ("server.json", "packages", [42]), ("server.json", "repository", []),
])
def test_distribution_rejects_invalid_metadata_fields(metadata_checkout, capsys, relative, field, value):
    root, validator = metadata_checkout
    path = root / relative
    data = json.loads(path.read_text())
    data[field] = value
    path.write_text(json.dumps(data))
    assert validator.main() == 1
    assert f"{relative} is invalid:" in capsys.readouterr().err


@pytest.mark.parametrize("field,value", [
    ("identifier", "wrong-package"), ("version", "0.0.0"),
    ("registryType", "npm"), ("runtimeHint", []),
    ("transport", {"type": "http"}),
    ("packageArguments", [{"type": "positional", "value": "--help"}]),
])
def test_distribution_rejects_wrong_registry_command(metadata_checkout, capsys, field, value):
    root, validator = metadata_checkout
    path = root / "server.json"
    data = json.loads(path.read_text())
    data["packages"][0][field] = value
    path.write_text(json.dumps(data))
    assert validator.main() == 1
    assert field in capsys.readouterr().err


@pytest.mark.parametrize("field,value", [("name", "wrong"), ("source", "./missing"), ("version", "0.0.0")])
def test_distribution_rejects_invalid_marketplace_source(metadata_checkout, capsys, field, value):
    root, validator = metadata_checkout
    path = root / MARKETPLACE
    data = json.loads(path.read_text())
    data["plugins"][0][field] = value
    path.write_text(json.dumps(data))
    assert validator.main() == 1
    assert MARKETPLACE in capsys.readouterr().err


def test_distribution_rejects_source_symlink_escape(metadata_checkout, tmp_path, capsys):
    root, validator = metadata_checkout
    outside = tmp_path.parent / (tmp_path.name + "-outside")
    outside.mkdir()
    skills = root / "plugins/codex/sieve-web-research/skills"
    shutil.rmtree(skills)
    skills.symlink_to(outside, target_is_directory=True)
    assert validator.main() == 1
    assert "escapes its plugin root" in capsys.readouterr().err


def test_distribution_metadata_validator_passes():
    if not (ROOT / "scripts/validate_distribution.py").exists():
        pytest.skip("the release validator is private and omitted from public exports")
    result = subprocess.run(
        [sys.executable, "scripts/validate_distribution.py"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("kind", ["wheel", "sdist"])
@pytest.mark.parametrize("private_path", ["AGENTS.md", "scripts/export_public.py"])
def test_distribution_rejects_maintainer_files_in_both_artifacts(tmp_path, monkeypatch, kind, private_path):
    import io
    import tarfile
    import zipfile
    from scripts import validate_distribution

    dist = tmp_path / "dist"
    dist.mkdir()
    if kind == "wheel":
        with zipfile.ZipFile(dist / "sieve_cli-1.0-py3-none-any.whl", "w") as archive:
            archive.writestr(private_path, "private fixture")
    else:
        with tarfile.open(dist / "sieve_cli-1.0.tar.gz", "w:gz") as archive:
            member = tarfile.TarInfo("sieve_cli-1.0/" + private_path)
            data = b"private fixture"
            member.size = len(data)
            archive.addfile(member, io.BytesIO(data))
    monkeypatch.setattr(validate_distribution, "ROOT", tmp_path)
    failures = validate_distribution._validate_wheel_contents()
    assert len(failures) == 1
    assert private_path in failures[0]


def test_mcp_registry_metadata_tracks_package_version():
    with (ROOT / "pyproject.toml").open("rb") as handle:
        version = tomllib.load(handle)["project"]["version"]
    metadata = json.loads((ROOT / "server.json").read_text())
    package = metadata["packages"][0]
    assert metadata["version"] == version
    assert package["registryType"] == "pypi"
    assert package["version"] == version
    assert package["identifier"] == "sieve-cli"
    arguments = package["packageArguments"]
    assert [argument["value"] for argument in arguments[:2]] == ["mcp", "serve"]
    assert arguments[2:] == [{"type": "named", "name": "--transport", "value": "stdio"}]


def test_plugin_versions_follow_package_version():
    with (ROOT / "pyproject.toml").open("rb") as handle:
        version = tomllib.load(handle)["project"]["version"]
    manifests = (CODEX, "plugins/claude/sieve-web/.claude-plugin/plugin.json")
    for relative in manifests:
        data = json.loads((ROOT / relative).read_text())
        assert data["version"] == version, relative
    marketplace = json.loads((ROOT / MARKETPLACE).read_text())
    plugin = next(item for item in marketplace["plugins"] if item["name"] == "sieve-web")
    assert plugin["version"] == version


def test_docker_docs_bind_host_publication_to_loopback_by_default():
    docs = (ROOT / "docs/docker.md").read_text()
    assert "-p 127.0.0.1:8765:8765 sieve-cli:lite" in docs
    assert "--shm-size=1g -p 127.0.0.1:8765:8765 sieve-cli:browser" in docs
    assert "-p 8765:8765" not in docs


def test_docker_public_binding_documents_auth_contract():
    docker_docs = (ROOT / "docs/docker.md").read_text()
    configuration = (ROOT / "docs/configuration.md").read_text()
    assert "-e SIEVE_AUTH_TOKEN -p 0.0.0.0:8765:8765" in docker_docs
    assert "Authorization: Bearer <token>" in docker_docs
    assert "`SIEVE_AUTH_TOKEN`" in configuration


def test_docker_entrypoints_use_canonical_mcp_serve_command():
    dockerfile = (ROOT / "Dockerfile").read_text()
    assert dockerfile.count('"mcp", "serve"') == 2
    assert '"--host", "0.0.0.0"' in dockerfile
