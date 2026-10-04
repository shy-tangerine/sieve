"""Local release material must bind exact artifacts, source and lock."""
import json
import pytest
from scripts.build_attestation_material import main, material


def test_material_verifies_and_rejects_changed_artifact(tmp_path):
    artifact = tmp_path / "example.whl"
    artifact.write_bytes(b"synthetic wheel fixture")
    output = tmp_path / "material.json"
    args = ["--artifact", str(artifact), "--source-revision", "a" * 40,
            "--repository", "https://github.com/example/private", "--output", str(output)]
    assert main(args) == 0
    assert main(args + ["--verify"]) == 0
    payload = json.loads(output.read_text())
    assert payload["source"]["revision"] == "a" * 40
    assert payload["sbom"]["packages"]
    assert json.loads((tmp_path / "sieve-sbom.spdx.json").read_text()) == payload["sbom"]
    assert all(package["filesAnalyzed"] is False for package in payload["sbom"]["packages"])
    assert (tmp_path / "SHA256SUMS").read_text().endswith("  example.whl\n")
    artifact.write_bytes(b"changed artifact")
    with pytest.raises(ValueError, match="does not match"):
        main(args + ["--verify"])


def test_material_refuses_symlink_and_invalid_source(tmp_path):
    artifact = tmp_path / "artifact.whl"
    artifact.write_bytes(b"fixture")
    with pytest.raises(ValueError, match="revision"):
        material([artifact], "short", "https://example.com")
    symlink = tmp_path / "alias.whl"
    symlink.symlink_to(artifact)
    with pytest.raises(ValueError, match="regular"):
        material([symlink], "a" * 40, "https://example.com")
