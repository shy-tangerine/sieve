"""Build and verify unsigned release material from the lock and exact artifacts.

Signing is a separate OIDC release-job operation. This local command neither
uploads artifacts nor claims a signature.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


def material(artifacts: list[Path], revision: str, repository: str) -> dict:
    if not re.fullmatch(r"[0-9a-f]{40,64}", revision):
        raise ValueError("source revision must be a full commit digest")
    if not artifacts or any(not path.is_file() or path.is_symlink() for path in artifacts):
        raise ValueError("artifacts must be regular files")
    if len({path.name for path in artifacts}) != len(artifacts):
        raise ValueError("artifact names must be unique")
    lock = tomllib.loads((ROOT / "uv.lock").read_text())
    packages = [{"SPDXID": f"SPDXRef-Package-{index}", "name": package["name"],
                 "versionInfo": package.get("version", "NOASSERTION"),
                 "downloadLocation": "NOASSERTION", "filesAnalyzed": False,
                 "licenseConcluded": "NOASSERTION", "licenseDeclared": "NOASSERTION",
                 "copyrightText": "NOASSERTION"}
                for index, package in enumerate(lock["package"])]
    return {"source": {"repository": repository, "revision": revision},
            "lock_sha256": digest(ROOT / "uv.lock"),
            "artifacts": {path.name: digest(path) for path in artifacts},
            "sbom": {"spdxVersion": "SPDX-2.3", "dataLicense": "CC0-1.0",
                     "SPDXID": "SPDXRef-DOCUMENT", "name": "Sieve locked build graph",
                     "documentNamespace": "https://spdx.org/spdxdocs/sieve-" + digest(ROOT / "uv.lock"),
                     "creationInfo": {"creators": ["Tool: Sieve build_attestation_material"],
                                      "created": "1970-01-01T00:00:00Z"},
                     "packages": packages}}


def main(args=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifact", type=Path, action="append", required=True)
    parser.add_argument("--source-revision", required=True)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--verify", action="store_true")
    options = parser.parse_args(args)
    expected = material(options.artifact, options.source_revision, options.repository)
    if options.verify:
        if json.loads(options.output.read_text()) != expected:
            raise ValueError("attestation material does not match source, lock, or artifacts")
        if json.loads(options.output.with_name("sieve-sbom.spdx.json").read_text()) != expected["sbom"]:
            raise ValueError("SBOM does not match the locked build graph")
        if options.output.with_name("SHA256SUMS").read_text() != "".join(f"{checksum}  {name}\n" for name, checksum in sorted(expected["artifacts"].items())):
            raise ValueError("checksums do not match the artifacts")
    else:
        options.output.parent.mkdir(parents=True, exist_ok=True)
        options.output.write_text(json.dumps(expected, indent=2, sort_keys=True) + "\n")
        options.output.with_name("sieve-sbom.spdx.json").write_text(json.dumps(expected["sbom"], indent=2, sort_keys=True) + "\n")
        options.output.with_name("SHA256SUMS").write_text("".join(f"{checksum}  {name}\n" for name, checksum in sorted(expected["artifacts"].items())))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
