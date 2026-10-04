# Sieve

<!-- mcp-name: io.github.shy-tangerine/sieve -->

<p align="center">
  <a href="docs/i18n/README.de.md">Deutsch</a> ·
  <a href="docs/i18n/README.es.md">Español</a> ·
  <a href="docs/i18n/README.zh-CN.md">简体中文</a> ·
  <a href="docs/i18n/README.ja.md">日本語</a> ·
  <a href="docs/i18n/README.pt-BR.md">Português</a>
</p>

<p align="center">
  <img src="docs/assets/sieve-banner.webp" alt="Sieve — local web research engine" width="100%">
</p>

<p align="center"><strong>Search the web, retrieve difficult pages, crawl sites, and return cited data through one local tool.</strong></p>

<p align="center">
  <a href="https://github.com/shy-tangerine/Sieve/actions/workflows/test.yml"><img alt="Tests" src="https://github.com/shy-tangerine/Sieve/actions/workflows/test.yml/badge.svg"></a>
  <a href="https://www.python.org/"><img alt="Python 3.11+" src="https://img.shields.io/badge/Python-3.11%2B-3776AB?style=flat-square&labelColor=111111"></a>
  <a href="LICENSE"><img alt="MIT" src="https://img.shields.io/badge/license-MIT-8B5CF6?style=flat-square&labelColor=111111"></a>
  <a href="docs/mcp.md"><img alt="MCP server" src="https://img.shields.io/badge/MCP-server-5A45FF?style=flat-square&labelColor=111111"></a>
  <a href="docs/docker.md"><img alt="Docker" src="https://img.shields.io/badge/Docker-lite%20%7C%20browser-2496ED?style=flat-square&labelColor=111111&logo=docker&logoColor=white"></a>
  <a href="sieve/_skills/sieve/SKILL.md"><img alt="Agent Skill" src="https://img.shields.io/badge/Agent-Skill-111827?style=flat-square&labelColor=111111"></a>
</p>

<p align="center">
  <a href="https://github.com/shy-tangerine/Sieve"><img alt="GitHub stars: prelaunch" src="https://img.shields.io/badge/Stars-prelaunch-6B7280?style=flat-square&labelColor=111111"></a>
  <a href="docs/distribution.md"><img alt="PyPI: prelaunch" src="https://img.shields.io/badge/PyPI-prelaunch-6B7280?style=flat-square&labelColor=111111&logo=pypi&logoColor=white"></a>
  <a href="docs/docker.md"><img alt="GHCR: prelaunch" src="https://img.shields.io/badge/GHCR-prelaunch-6B7280?style=flat-square&labelColor=111111&logo=docker&logoColor=white"></a>
  <a href="https://github.com/sponsors/shy-tangerine"><img alt="Sponsor" src="https://img.shields.io/badge/Sponsor-%E2%99%A5-EC4899?style=flat-square&labelColor=111111&logo=githubsponsors&logoColor=white"></a>
</p>

[Why Sieve](#why-sieve) · [Features](#features) · [Examples](#examples) · [Install](#install) · [Agents and MCP](#agents-and-mcp) · [Evidence](#evidence) · [Docs](#documentation)

![Real example.com fetch, structured extraction, and crawl](docs/assets/sieve-terminal.gif)

[Replay the terminal recording and run the same commands](docs/terminal-demo.md).

## Why Sieve

Most web-research stacks grow one workaround at a time: a search package, an
HTTP fetcher, a browser for JavaScript, a crawler, an extraction library, a
transcription tool, and another server for agents. Each one introduces its own
output shape, configuration, retry behavior, and security assumptions.

Sieve puts those jobs behind one bounded contract:

| When you need to… | Sieve provides… |
|---|---|
| Find current sources without buying an API plan | Keyless multi-engine search, with optional Serper, Tavily, Exa, Firecrawl, TinyFish, and other BYOK providers |
| Read a page that blocks a plain request | HTTP-first retrieval with block detection, coherent browser identity, pooled browser escalation, remote CDP, proxy health, and captured network evidence |
| Turn a site into a small research corpus | Focused crawling, sitemap and feed discovery, robots support, per-domain throttling, deduplication, hard budgets, and partial results |
| Extract records for a database or RAG pipeline | CSS/XPath schemas, tables, JSON-LD, Schema.org microdata, chunks, similar-item expansion, PDFs, OCR, and optional LLM extraction |
| Run the same workflow over many pages | Resumable JSONL batch extraction and bounded DAG pipelines with checkpoints |
| Give an agent web access | Stable JSON, citations, machine-readable failures, an MCP server, Codex and Claude skills, and prompt-injection-aware content handling |
| Work with public video and social sources | Video search and metadata, public post records, comments, profile collection, downloads, and local transcription |
| Add a provider that Sieve does not ship | Declarative API workflows with schema validation, domain restrictions, secret references, response mapping, and no executable plugin code |

Sieve runs locally and works without keys for its core research path. Provider
keys add coverage; they do not replace the local path. Configuration, browser
profiles, integrations, and secrets stay on the operator's machine.

## Features

### 🔎 Search and source discovery

- Keyless metasearch across independent engines, normalized into one ranked result schema.
- Optional BYOK engines for workflows that need a specific index, answer API, or hosted extraction provider.
- Query validation, provider deadlines, bounded retries, proxy rotation, and per-engine reports instead of silent failures.
- URL normalization, duplicate removal, relevance scoring, optional neural reranking, and fetch hints.
- Explicit cache behavior: `--cached` requires an existing result; `--refresh` replaces it.
- Opt-in, age-bounded stale cache fallback after a total transient keyless search failure, with cache age and original provenance reported.
- Sitemap, robots, RSS/feed, homepage, archive, certificate, and bounded probe discovery.

### 🌐 Retrieval and browser escalation

- Fast HTTP retrieval first, followed by a stealthy browser when a response needs rendering or shows a supported block pattern.
- Coherent TLS, header, navigator, and platform identity across a session.
- Pooled browser tabs with state reset, failed-tab disposal, resource blocking, ad filtering, wait selectors, and network-idle controls.
- Operator-supplied CDP endpoints and dedicated persistent Sieve profiles without copying cookies through CLI arguments.
- Detection of HTTP-200 application shells and bounded XHR/fetch evidence when useful data arrives in the background.
- Screenshots, PDFs, response-size limits, redirect checks, private-network protection, and structured next actions.
- Validated, pinned destinations for managed Chromium redirects, frames, popups, and Service Workers, with configured proxy chaining and one fetch deadline across retries.

### 🕸️ Crawling and repeatable jobs

- Best-first same-domain crawling with focus queries, depth/page/concurrency limits, URL filtering, and deduplication. Shallower routes improve page depth and link discovery without fetching or charging the page twice. Robots.txt compliance is on by default for crawling; bypassing it requires an explicit `respect_robots=false`, which is flagged in the response.
- Optional robots.txt checks and per-domain adaptive throttling with `Retry-After` support.
- Discovery-only mode for building URL inventories before downloading pages.
- Resumable batch extraction with input-bound atomic checkpoints, explicit failure retries, incremental progress, and ordered final JSONL.
- Declarative DAG pipelines for `search_fetch`, `fetch`, `extract`, and bounded batch fan-out.
- Partial results preserve completed work when a deadline or page budget ends.

### 🧱 Structured extraction and RAG

- Declarative CSS and XPath schemas with typed text, attribute, HTML, and repeated-record fields.
- Tolerant inline or `@file` schemas and deterministic JSONL output.
- JSON-LD and nested Schema.org microdata extraction without an LLM.
- HTML tables, metadata, links, script payloads, and similar-element expansion.
- Table spans and expanded grids are bounded before allocation, including the shared cell budget across tables.
- Clean Markdown/text with source links retained as citations.
- Identity, regex, sentence, and semantic chunking for RAG corpora.
- PDF text, optional OCR, and optional BYOK smart extraction.

### 🎬 Media and public-source adapters

- YouTube search, metadata, bounded downloads, captions, and local transcription through a compatible existing Whisper tool.
- Normalized public Instagram and TikTok records through direct, reader, or explicitly enabled browser paths.
- Paced profile collection and comments with item, scroll, and time bounds plus explicit opt-in.
- Authenticated media resolution through external `yt-dlp` using the dedicated Sieve Chromium profile.
- Reddit HTML/RSS, Arctic Shift, OpenLibrary, and research-source normalization.
- Optional Maigret adapter for bounded public username checks; each run requires `--allow-osint`.

### 🔌 Interfaces and integrations

- CLI with one JSON document per operation and nonzero exits for failures.
- Typed async `SieveClient` for Python applications.
- MCP over local stdio or authenticated streamable HTTP, with canonical tool names and output schemas. <!-- contract-test: tests/test_surface_contracts.py::TestCapabilityRouterInvariants::test_canonical_set_matches_public_tools --> <!-- contract-test: tests/test_mcp_http_wire.py::TestBearerWire::test_missing_authorization_is_401 -->
- Codex and Claude skills that route web work through Sieve and preserve citations, limits, retries, and failures.
- Declarative API integrations with HTTPS/domain checks, secret references, bounded templates, and response mapping.
- Docker targets for a small MCP image or a browser-equipped image.
- Explicit update checks, release-note summaries, rollback, and a Topgrade wrapper; research commands never update Sieve. <!-- contract-test: tests/test_update_command.py::test_update_rolls_back_when_verification_fails -->

Sieve replaces the usual chain of search wrappers, HTTP clients, browser
scrapers, crawlers, extraction scripts, media tools, and agent-specific web
plugins. Start with a keyless search, fetch the useful results, escalate to a
browser only when the page needs it, and keep the source URL and retrieval
details attached to every result.

<!-- offline-cli-smoke:start -->
```bash
sieve search "Python free-threading status" --max-results 5
sieve fetch https://docs.python.org/3/howto/free-threading-python.html
sieve crawl https://docs.python.org/3/ --focus "free threading" --max-pages 5
```
<!-- offline-cli-smoke:end -->

Research operations return JSON or JSONL with command-specific limits for
time, pages, concurrency, response size and result count. Each interface
advertises its supported operations and options. The public MCP catalog below
lists its exact inputs and required installation extras.
<!-- contract-test: tests/test_mcp_http_wire.py::test_tool_result_redacts_both_copies_and_bounds_wire -->

## Verified source-checkout quickstart

Before package, container, registry, or hosted artifacts are published, use the
source checkout directly:

```bash
git clone https://github.com/shy-tangerine/Sieve.git
cd Sieve
uv sync --locked --extra dev
uv run sieve --version
uv run sieve search "Python free-threading status" --max-results 1
uv run sieve fetch https://docs.python.org/3/howto/free-threading-python.html --timeout 20
```

The last two commands are the keyless smoke path. Each emits JSON on stdout;
check `content_ok`, `source`, and `next_action` before using the content.

### Capability prerequisites and claim gates

| Capability | Keyless/source-checkout prerequisite | Claim status before publication |
|---|---|---|
| Search, HTTP fetch, crawl, extract, batch, pipelines, SDK | Python 3.11+ and the core install | Verified locally; no provider key required |
| Headless browser retrieval and ordinary JS rendering | `.[browser]` or `.[all]` | Available when the browser runtime is installed |
| Real-browser Sleeper tier for hard-target JS/anti-bot pages | Operator Sleeper daemon and authorized browser session; use `--browser-backend sleeper` | Operator-scoped; do not claim universal availability |
| BYOK search/LLM/provider integrations | Provider key via `sieve keys` or `SIEVE_*` environment | Optional; no bundled credentials |
| PDF, OCR, reranking, NLP, transcription | Corresponding optional extra and local tool where applicable | Optional; verify the tool locally |
| MCP | `.[legacy-server]` or `.[all]` | Local stdio/HTTP only; no hosted MCP artifact is published |
| Authenticated Instagram/social collection | Dedicated Sieve profile or operator CDP, explicit opt-in | Live profile evidence is operator-gated; blocked public probes are not corpus evidence |
| PyPI, container, MCP Registry, Smithery, Brave, standalone binary | Published artifact and maintainer-controlled external gate | Not yet published; do not present as installable channels |

For JavaScript-heavy pages, an HTTP 200 with roughly 1–2 KB of shell text and
zero useful anchors is a failed hydration signal, not an empty result. Follow
the tier ladder in [the agent skill](sieve/_skills/sieve/SKILL.md): HTTP → headless
browser → real-browser Sleeper. The generic Sleeper CLI surface is available
only where shown by `sieve fetch --help`, `sieve crawl --help`, or
`sieve extract --help`; otherwise use the advertised local MCP Sleeper tool.

## Examples

### Search and fetch

```bash
sieve search "site:docs.python.org free threading" --max-results 5 --refresh
sieve fetch https://docs.python.org/3/howto/free-threading-python.html --timeout 20
```

Search includes engine reports, source URLs, scores, and fetch hints. Fetch
reports the retrieval path, content status, truncation, captured-network
evidence, and a machine-readable next action.

To allow a cached result up to one hour old after a total transient keyless
search failure:

```bash
sieve search "site:docs.python.org free threading" --max-results 5 \
  --stale-fallback --stale-max-age 3600
```

This option is disabled by default. Fallback results report `stale`,
`cache_age_seconds`, and `stale_reason`. Authenticated requests, partial or
successful empty responses, and `--cached` or `--refresh` never use this fallback.

### Extract repeated records without an LLM

```bash
sieve extract https://books.toscrape.com/ --schema '{
  "baseSelector": "article.product_pod",
  "fields": [
    {"name":"title", "selector":"h3 a", "type":"attribute", "attribute":"title"},
    {"name":"price", "selector":".price_color", "type":"text"}
  ]
}'
```

```json
{"content_ok":true,"items":[{"title":"A Light in the Attic","price":"£51.77"}]}
```

Use `--tables` for HTML tables, `--xpath` for XPath schemas, `--jsonl` for a
record stream, or `--chunk sentence` to prepare text for retrieval.

### Crawl with a hard budget

```bash
sieve crawl https://docs.python.org/3/ \
  --focus "free threading" \
  --max-pages 20 \
  --max-depth 3 \
  --timeout 90
```

The crawl returns partial pages and truncation flags when a budget ends the run.

### Resume a batch extraction job

```bash
sieve batch-extract --input urls.txt --schema @product-schema.json \
  --checkpoint .sieve-products.json --concurrency 4 --progress

sieve batch-extract --input urls.txt --schema @product-schema.json \
  --checkpoint .sieve-products.json --concurrency 4 --resume --retry-failed --progress
```

The CLI accepts up to 100 input URLs. Version 2 checkpoints bind the ordered
URLs, schema, timeout, and extractor identity, and retain success or failure
status without storing page bodies or credentials. `--resume` skips recorded
items; `--retry-failed` reruns known failures. Changing bound inputs requires a
fresh checkpoint. Older version 1 files need explicit migration.

`--progress` sends completion summaries to stderr during work. Final JSONL
stays in input order on stdout. See [batch checkpoint and migration details](docs/cli.md)
and [SDK batch options](docs/sdk.md#batch-checkpoints-and-progress).

### Compose a bounded pipeline

```bash
sieve pipeline research.json --max-nodes 16 --max-items 200 --timeout 180
```

Pipeline specifications form an acyclic graph. `search_fetch` validates each
selected URL and carries provenance into later nodes. See [pipelines](docs/pipeline.md).

### Add an API workflow

```bash
sieve integration validate ./weather.json
sieve integration invoke weather forecast --args '{"city":"Lisbon"}'
```

The integration file cannot execute Python, shell commands, or template
expressions. See [API integrations](docs/integrations.md).

### Use the async SDK

```python
from sieve.sdk import SieveClient

async with SieveClient(timeout=20, concurrency=4) as sieve:
    hits = await sieve.search("Python free threading")
    page = await sieve.fetch(hits["results"][0]["url"])
    records = await sieve.extract(
        "https://example.com",
        {"baseSelector": "article", "fields": [
            {"name": "title", "selector": "h1", "type": "text"}
        ]},
    )
```

## Install

Sieve requires Python 3.11 or newer. The recommended installer provides the
complete feature set, then detects compatible tools already on the machine so
it does not download a second copy without need.

```bash
git clone https://github.com/shy-tangerine/Sieve.git
cd Sieve
sh scripts/install.sh
sieve setup
```

Setup reports browser runtimes, CDP endpoints, `yt-dlp`, and compatible Whisper
executables. It asks before saving a detected endpoint or external tool.

Manual complete installation:

```bash
uv tool install '.[all]'
sieve setup
```

Individual extras remain in `pyproject.toml` for constrained packaging; they
are not separate Sieve editions. Contributors can use `uv sync --all-extras`.

### Core vs optional capabilities

The core install (`sieve-cli` with no extras) covers search, fetch, crawl,
extract, batch extraction, pipelines, integrations, and the SDK without any
keys or external services. Optional extras add specific capabilities:

| Extra | Adds | Install |
|---|---|---|
| `browser` | Rendered retrieval, block detection, persistent profiles, social browser reader | `pip install 'sieve-cli[browser]'` |
| `pdf` | PDF text extraction | `pip install 'sieve-cli[pdf]'` |
| `ocr` | Scanned-page OCR | `pip install 'sieve-cli[ocr]'` |
| `rerank` | Neural reranking | `pip install 'sieve-cli[rerank]'` |
| `nlp` | Chunking and tokenization helpers | `pip install 'sieve-cli[nlp]'` |
| `stt` | Local speech-to-text transcription | `pip install 'sieve-cli[stt]'` |
| `legacy-server` | MCP server transport (stdio and HTTP) | `pip install 'sieve-cli[legacy-server]'` |
| `all` | Everything above in one install | `pip install 'sieve-cli[all]'` |
| `dev` | Test suite and development tooling | `pip install -e '.[dev]'` |

### Public MCP tool requirements

Direct requirements for public MCP tools are generated from the public tool
registry and the optional dependencies in `pyproject.toml`. Required extras
describe mandatory dependencies; browser escalation can remain optional for
other tools. Regenerate the table and its definition hashes with
`python scripts/generate_docs_capabilities.py --write`.

<!-- generated-public-capabilities:start -->
| Public MCP tool | Required extra | Input fields |
|---|---|---|
| `detect_block` | core | `headers`, `html`, `status` |
| `extract` | core | `extraction_type`, `options`, `schema`, `url`, `xpath` |
| `extract_jsonl` | core | `force_fetcher`, `schema`, `url`, `validate` |
| `research_ingest` | core | `after`, `before`, `ids`, `kind`, `limit`, `max_pages`, `post_id`, `query`, `retries`, `sort`, `source`, `subreddit`, `target`, `timeout` |
| `schema_gen` | core | `fields`, `model`, `url` |
| `screenshot` | `browser` | `options`, `session_id`, `url` |
| `sitemap_harvest` | core | `action`, `max_urls`, `url` |
| `smart_crawl` | core | `crawl_urls`, `discover_only`, `focus`, `options`, `url` |
| `smart_fetch` | core | `cache_ttl`, `css_selector`, `extraction_type`, `focus`, `force_fetcher`, `max_content_chars`, `offset`, `options`, `pages`, `password`, `timeout`, `url`, `urls` |
| `smart_search` | core | `options`, `query` |
<!-- generated-public-capabilities:end -->

`schema_gen` runs only when explicitly called and requires a configured LLM
key. It sends at most 12,000 characters of the fetched HTML sample plus the
selected field names to the configured OpenAI-compatible endpoint. Remote
endpoints require HTTPS; HTTP is limited to loopback for local model services.
See the [MCP guide](docs/mcp.md) and [SDK guide](docs/sdk.md).

`yt-dlp` and `ffmpeg` are external tools discovered at runtime, not Python
extras. The recommended installer (`scripts/install.sh`) enables `all` by
default; the core install is the smaller path when only CLI research is
needed.

### Authenticated Instagram workflow

Create or refresh a dedicated Instagram session with Sieve's bundled Chromium:

```bash
pip install 'sieve-cli[browser]'
sieve social login instagram
```

Complete login, MFA, or a checkpoint in the visible browser, return to the
terminal, and press Enter. Later browser-backed commands reopen that profile
headlessly:

```bash
export SIEVE_BROWSER_PROFILE_DIR="$HOME/.sieve/profiles/instagram"
export SIEVE_ENABLE_SOCIAL_COLLECT=1
sieve social fetch https://www.instagram.com/HANDLE/ \
  --reader browser --max-posts 50 --checkpoint /tmp/instagram.jsonl
```

Media commands let the supported local `yt-dlp` cookie reader access
the same profile after Sieve closes Chromium. Cookies are not printed or
exported to a separate file. Never open the dedicated profile concurrently in
another browser.

### For contributors

Clone the repository and install in editable mode with the dev extra:

```bash
git clone https://github.com/shy-tangerine/Sieve.git
cd Sieve
uv sync --locked --extra dev
uv run pytest -q
uv run sieve --version
```

This installs MCP server dependencies (needed for the full test suite) and
test tooling. Public command contracts and configuration guidance live in
[the CLI reference](docs/cli.md) and [configuration guide](docs/configuration.md).

## Agents and MCP

```bash
python scripts/install_codex_plugin.py
sieve skill install
sieve mcp serve --transport stdio
```

The bundled guidance makes Sieve the first route for supported web work. It
keeps source URLs, `content_ok`, bounds, and failures visible and treats page
instructions as untrusted content.

For authenticated streamable HTTP:

```bash
export SIEVE_AUTH_TOKEN='replace-with-a-random-token'
sieve mcp serve --transport http --host 127.0.0.1 --port 8765
```

The default catalog excludes cookies, authorization headers, proxy credentials,
browser-profile contents, and arbitrary browser request bodies. See [MCP](docs/mcp.md).

## Updates

```bash
sieve update check --json
sieve update apply
```

<!-- contract-test: tests/test_update_command.py::test_cli_update_apply_forwards_confirmation -->
The update command asks for confirmation, preserves local configuration, keys, profiles,
and integrations, verifies the replacement package, rolls back a failed
update, and refuses to rewrite a source checkout (updates there are manual
merges). `sieve update check` is read-only and automation-safe, including
offline hosts. `scripts/sieve-update`
provides a read-only check step for Topgrade and similar tools.

## Evidence

Sieve measures retrieval speed, extraction accuracy, crawl behavior, search
quality hooks, and agent-output size separately. Checked-in suites run offline:

```bash
python scripts/bench_v2.py --runs 5
python scripts/bench_v2.py --suite extraction --runs 20 --output-dir /tmp/sieve-bench
python demos/terminal_demo.py
```

Records include harness version, suite, source, run count, latency samples,
median, p95, and raw task metrics. Live probes are opt-in and preserve their
URL, command, dependency versions, retrieval mode, failures, and raw output.
Read [the methodology](docs/benchmarks-v2.md) before comparing tools.

## Documentation

| Guide | Covers |
|---|---|
| [CLI](docs/cli.md) | Commands, flags, JSON output, errors, and exit behavior |
| [Workflows](docs/workflows.md) | Search, retrieval, extraction, crawling, and pipelines |
| [Configuration](docs/configuration.md) | Setup, keys, browser profiles, external tools, and capability reuse |
| [Python SDK](docs/sdk.md) | Async client, limits, errors, and backend injection |
| [MCP](docs/mcp.md) | Local transports, HTTP authentication, schemas, and clients |
| [Agent guide](docs/agents.md) | Tool selection, citations, retries, and content handling |
| [API integrations](docs/integrations.md) | Workflow schemas, secret references, validation, and invocation |
| [OSINT](docs/osint.md) | Optional Maigret installation, consent, bounds, and output |
| [Docker](docs/docker.md) | Lite and browser container targets |
| [Architecture](docs/architecture.md) | Runtime interfaces and dependency direction |
| [Security](SECURITY.md) | Vulnerability reports and trust boundaries |

## Star History

<p align="center">
  <a href="https://star-history.com/#shy-tangerine/Sieve&Date">
    <img src="https://api.star-history.com/svg?repos=shy-tangerine/Sieve&type=Date" alt="Star History chart">
  </a>
</p>

## Support the project

Sieve accepts individual support and disclosed infrastructure support. Financial support cannot change search ranking,
source selection, benchmark results, security decisions, or access to user
data. Infrastructure used in a benchmark is disclosed next to that benchmark.

## Contributing

Read [CONTRIBUTING.md](CONTRIBUTING.md) before opening a change. Adapted work
must record its source, version, and license in [PORTS.md](PORTS.md) or the
relevant notices. Behavior changes require focused tests and must preserve the
CLI JSON contract.

## License

Sieve is licensed under [MIT](LICENSE). Adapted components retain their
licenses and attribution in [THIRD-PARTY-NOTICES.md](THIRD-PARTY-NOTICES.md).

## Security contracts

URL validation rejects embedded credentials, unsafe schemes and
private or special-purpose network destinations.
Sitemap discovery validates nested sitemap URLs and every redirect hop;
redirect following is opt-in per module and statically enforced.
The content cache lives under ~/.sieve/cache with owner-only permissions and
rejects secret-bearing envelope keys. The cache keys include request-affecting
options with a hashed PDF password. The HTTP server
refuses non-loopback binds without SIEVE_AUTH_TOKEN, and
robots.txt compliance is on by default for crawling.
Strict robots mode fails closed when the policy is unreadable.
The named regression map is in `tests/test_security_claims.py`.

## Python distribution tooling

The PEP 621 metadata in `pyproject.toml` and `uv.lock` work with `uv build` (sdist + wheel). `uv publish` can upload those artifacts to PyPI after release approval and token or trusted-publisher setup. The existing self-hosted release workflow uses the PyPA trusted-publishing action and its manual/tag gates; do not replace or run that workflow merely to switch the upload client.
