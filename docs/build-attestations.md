# Build attestations

`scripts/build_attestation_material.py` binds the full source revision,
`uv.lock` SHA-256, wheel and sdist SHA-256 values, and an SPDX inventory of the
locked build graph. The inventory includes development and optional packages;
it is not a claim that every listed dependency ships inside the wheel.
License values are `NOASSERTION` rather than inferred. The fixed creation time
makes this unsigned local material reproducible; it is not the release time.
Generate it outside `dist/` and rerun with `--verify` before release.
The command also writes a standalone `sieve-sbom.spdx.json` and `SHA256SUMS`
beside the material file; verification checks all three files.

The release workflow uploads this material separately, preserving the PyPA
trusted-publishing jobs and their default PEP 740 attestations. Container builds
emit BuildKit SBOM and maximum provenance bound to the image digest.
This workflow creates no GitHub Release object, so GitHub Release attachments
are not applicable. The three local verification files are retained together
as a workflow artifact, separately from the files uploaded to PyPI.

GitHub signed artifact attestations for private repositories require Enterprise
Cloud. Set the repository variable `ENABLE_GITHUB_ATTESTATIONS=true` only after
confirming that service is available. The separate distribution signing job and
container signing step use pinned `actions/attest`, with job-scoped
`attestations: write` and `id-token: write`. A signing failure then fails the job.
No release was dispatched to validate these changes locally.

After an authorized release, consumers can verify a downloaded wheel with
`gh attestation verify sieve_cli-VERSION-py3-none-any.whl -R OWNER/REPO`, or an
image with `gh attestation verify oci://ghcr.io/OWNER/sieve:TAG-lite -R OWNER/REPO`.
Check the expected repository, source revision, artifact digest, and material
lock digest. Local checksum validation proves correspondence, not signer
identity. Signed verification remains a release-only check.
