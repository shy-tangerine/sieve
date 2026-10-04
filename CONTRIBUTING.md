# Contributing

For agent-assisted development, start with
[docs/agents.md](docs/agents.md). It describes the public CLI-first
integration contract and safe operating boundaries.

## Workflow

Outside contributors use the standard **fork + pull request** flow.
Direct pushes to the primary branch are maintainer-only. Review and merge
stay with the maintainers; a maintainer may ask an agent-assisted
contribution to be regenerated with tighter scope or tests.

## Research first (mandatory)

Before opening an issue or pull request, **search the existing issues and
pull requests** — both open and closed. This repository is agent-facing and a
large share of contributions arrive as agent-created reports; researching
first keeps the tracker free of duplicates and stale repeats.

- Bug report: link the search you ran and any similar issue you found.
- Feature request: link related prior requests and explain what is different.
- Pull request: link the issue it resolves (or the prior discussion).

Duplicates opened without a research note may be closed as such.

## What is welcome

- **Bug reports** with a reproducible command and the actual vs expected JSON.
- **Small focused fixes** directly, with a regression test.
- **Features only after an issue** — no drive-by feature pull requests.
  Open the feature request first, get a maintainer's go-ahead, then implement.

No AI-disclosure is required from contributors.

## Change checklist

Small, focused changes are easiest to review. Add or update a regression test
for behavior changes, run the relevant test file and then the full suite, and
record upstream provenance when adapting code or algorithms.

Before opening a pull request, check that it contains no credentials, cookies,
local filesystem paths, generated artifacts, or personal metadata. Changes to
an upstream-derived module must include the source URL, observed version, and
the license and notice decision in `PORTS.md`.

## Local checks

```bash
uv run --locked --extra dev pytest -q          # full suite
uv run --locked --extra dev ruff check sieve/  # lint
python scripts/check_public_tree.py            # public-tree guard
```

Pull requests that fail the pre-commit public-release audit will not merge.

## Checkout hygiene

The runtime and normal offline tests do not require local upstream snapshots,
agent metadata, or private notes. Keep these areas distinct:

| Root directories | Role and retention |
|---|---|
| `sieve`, `docs`, `tests`, `examples`, `demos`, `plugins`, `licenses`, `.github` | Durable product source, assets, tests, and release configuration. Preserve. |
| `scripts` | Durable developer/install tooling. The export allowlist selects public scripts; private audit and maintenance helpers stay in the private checkout. |
| `.git` | Repository history and checkout metadata. Preserve. |
| `upstream`, `wiki` | Private compatibility/reference snapshots and maintainer evidence. Preserve unique material. They are excluded from the release artifact and are not required for normal installation or offline tests. Relocation requires updating the operator's compatibility tooling. |
| `raw`, if present | Unclassified operator/reference data. It is absent from the reviewed checkout. Establish ownership and retention before moving or deleting it. |
| `.venv`, `.mypy_cache`, `.pytest_cache`, `.ruff_cache`, `__pycache__`, `*.egg-info`, `build`, `dist`, `benchmarks/results` | Rebuildable environments, caches, and build/benchmark output. Eligible for explicitly scoped cleanup. |
| `.claude`, `.codebase-memory`, `.config`, `.freebuff`, `.opencodereview`, `.repowise`, `.vscode`, `skills` | Workstation/operator state, local intelligence indexes, or installed skill copies. Preserve or move through the owning tool; do not treat these as disposable source files. |

Review rebuildable output before removing it:

```bash
git clean -ndX -- .venv .mypy_cache .pytest_cache .ruff_cache build dist '*.egg-info' benchmarks/results
```

After reviewing that exact path list, use `-fdX` in place of `-ndX` to remove
only those ignored targets. This command deliberately excludes reference
snapshots, private notes, browser data, and operator state. Recreate the
development environment with `uv sync --locked --extra dev`, then run
`uv run --locked --extra dev pytest -q`. Never use an unscoped `git clean`
as a release-preparation step. A fresh source checkout and sanitized export
must pass their normal tests without copying local reference or operator data.

## License

By contributing, you agree that your contributions are licensed under
[MIT](LICENSE) (see the notice decision recorded in
`THIRD-PARTY-NOTICES.md`).
