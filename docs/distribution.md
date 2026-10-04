# Distribution channels

## Explicit updates

Sieve never updates during ordinary research commands. Use `sieve update check
--json` to inspect the installed and latest versions, availability, release
URL, publication date, and bounded notes. The check is read-only and safe to
run from automation (including offline hosts: an unavailable release endpoint
is reported, not treated as an update failure).

Apply an update only after reviewing that information, with `sieve update
apply`; `--yes` is reserved for approved automation. The apply path behaves
differently by install mode:

- **Package install** — asks for confirmation, installs the pinned
  `sieve-cli` release, verifies the resulting installed version, and
  automatically rolls back to the previous version if verification fails. If
  the rollback itself fails, the operator is told to run `sieve --rollback`.
- **Source checkout** — always refused. Update a checkout by reviewing and
  merging upstream changes yourself in a separate worktree; the updater never
  rewrites a checkout.

Sieve has one supported product contract: the `sieve` executable and its
machine-readable JSON output. Distribution channels package that same contract
for different environments.

| Channel | Status | Best for |
| --- | --- | --- |
| PyPI (`sieve-cli`) | Release workflow ready; publication pending | CLI, local MCP stdio, Python subprocess callers |
| GHCR (`ghcr.io/shy-tangerine/sieve`) | Release workflow ready; publication pending | Reproducible MCP HTTP containers |
| Official MCP Registry | `server.json` ready; submission pending ownership checks | Discovering the local stdio package |
| Codex and Claude plugins | Source packages included; marketplace publication pending | Agent routing guidance |
| Standalone binaries | Deferred | Consider after Python installation friction is measured |
| Smithery / hosted MCP | Deferred | Consider only with a public HTTPS deployment and explicit auth policy |

## Python SDK

Python 3.11, 3.12 and 3.13 are covered by minimum-direct and locked-dependency
checks for the base package and `dev`, `mcp`, `browser`, `ocr`, `pdf`, `rerank`
and `all` extras. The legacy `ocr-legacy` extra supports Python 3.11–3.12;
use `ocr` on Python 3.13. ONNX Runtime starts at 1.19 and reranking tokenizers
at 0.20.3 so the declared minimum installations support this Python range.

Each compatibility job installs an isolated environment, checks dependency
consistency, imports the installed package and optional boundary, runs the
installed CLI, and exercises named security/resource-budget tests. Run one
combination with:

```bash
uv run --locked --extra dev python scripts/check_dependency_compatibility.py \
  --python 3.13 --mode minimum --extra rerank
```

The package ships a supported, typed, async-first client at `sieve.sdk`. It
wraps the same bounded search, fetch, crawl, extraction, and batch operations
used by the CLI and MCP surfaces. See the [SDK reference](sdk.md) for its
compatibility policy.

## Hosted MCP considerations

Smithery is a discovery or gateway option, not a prerequisite for local Sieve.
Its URL mode requires a public Streamable HTTP endpoint, explicit forwarding
authentication, resource limits, scan/WAF access, observability, and a
rollback plan. The local stdio package and official MCP Registry remain usable
if Smithery is unavailable.

## Release verification

Before configuring a publisher or submitting registry metadata, run:

```bash
uv build
uv run --locked --extra dev python scripts/validate_distribution.py
uv run --locked --extra dev twine check dist/*
```

The offline validator checks plugin and registry object shapes, required field
types, existing local skill/hook/marketplace paths, public repository URLs,
and the `uvx sieve-cli mcp serve --transport stdio` registry command. It also
inspects wheel and sdist contents against the public-tree policy. Plugin
manifest versions remain independent; this gate does not decide their deferred
release-version policy.

The GitHub Actions release workflow uses trusted publishing and performs
registry writes only from a versioned tag or an explicit TestPyPI dispatch.
