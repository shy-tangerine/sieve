## Research

<!-- Link the issue this resolves, or the issue/PR search you ran before opening this PR (mandatory per CONTRIBUTING.md). -->

## Summary

<!-- Explain the user problem and the resulting behavior in a few sentences. -->

## What changed

<!-- List the main implementation or documentation changes and link related issues. -->

## Scope

- [ ] This change is focused and keeps the public CLI or MCP contract clear.
- [ ] User-facing documentation and examples are updated when behavior changes.

## Verification

- [ ] Focused tests pass (or this change is documentation-only).
- [ ] `uv run --locked --extra dev pytest -q` passes when code changes.
- [ ] `git diff --check` passes.
- [ ] I tested the relevant CLI or MCP flow and described the result above.

## Compatibility and provenance

- [ ] Any adapted code or algorithm has its source, revision, license, and notice decision recorded in `PORTS.md`.
- [ ] Output and error behavior remain machine-readable and bounded.

## Privacy and access

- [ ] No credentials, cookies, private paths, generated artifacts, or personal metadata are included.
- [ ] Browser or session behavior remains operator-authorized and does not bypass access controls.
