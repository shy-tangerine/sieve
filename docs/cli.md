# CLI reference

Single contract for every verb: arguments in, one JSON document on stdout,
diagnostics on stderr. Exit `0` on success, `1` on failure (failure still
emits `{"ok": false, ...}` on stdout). Timeouts, retries, and result counts
are bounded on every command.

## Core verbs

| Command | Purpose | Key flags |
|---|---|---|
| `sieve search QUERY` | keyless multi-engine web search, ranked JSON | `--max-results N` (6), `--timeout SEC` (45) |
| `sieve images QUERY` | bounded Brave image search (BYOK only), JSON | `--max-results N` (6), `--safe-search strict\|moderate\|off`, `--timeout SEC` (15) |
| `sieve fetch [--structured] URL [...]` | fetch with automatic HTTP → stealthy browser escalation; structured mode includes parsed JSON-LD and nested Schema.org microdata | `--timeout SEC` (30), `--structured`, `--cache-ttl SEC` |
| `sieve crawl URL` | BestFirst deep crawl, same-domain | `--max-pages N` (10), `--max-depth N` (2), `--focus QUERY`, `--discover-only` |
| `sieve crawl URL --discover-domains` | multi-source domain discovery (sitemap, robots, feed, homepage; + wayback/crt/cc/probe as opt-in) | `--discovery-sources LIST` (sitemap,robots,feed,homepage), `--max-urls N` (500), `--sitemap-only`, `--include-subdomains`, `--allow-external`, `--map-search STR` |
| `sieve extract URL` | fetch + declarative schema → JSON, no LLM | `--schema JSON-or-@file`, `--tables`, `--chunk identity\|regex\|sentence\|semantic`, `--xpath`, `--timeout SEC` (30) |
| `sieve extract URL --schema` | tolerant JSON: trailing commas, `//`/`/* */` comments, `base_selector`/`css` aliases | — |
| `sieve fetch --structured URL [...]` | fetch and include parsed JSON-LD entities plus nested Schema.org microdata at `metadata.structured_data`; multiple URLs use bulk response contract | `--structured`, `--timeout SEC` (30), `--cache-ttl SEC` |
| `sieve fetch` markdown | script-tag harvest (`__NEXT_DATA__`/`__INITIAL_STATE__`/JSON-LD) + `html2text` with `base_url` for relative links, wired pre-trafilatura | — |
| `sieve extract URL --similar SEL` | fetch + find-similar list expansion (Scrapling-inspired) | `--similar CSS` |
| `sieve batch-extract URL ...` | bounded schema extraction with ordered final JSONL and resumable checkpoint | `--input FILE`, `--checkpoint FILE`, `--resume`, `--retry-failed`, `--migrate-v1`, `--progress`, `--concurrency N`, `--max-pages N`, `--timeout SEC` |
| `sieve pipeline SPEC.json` | compose bounded search-to-fetch, fetch, extract, and batch-extract nodes as a deterministic DAG | `--max-nodes N` (32), `--max-items N` (1000), `--timeout SEC` (300) |
| `sieve extract URL --smart PROMPT` | fetch + BYOK LLM → JSON (key via `sieve keys add llm`, endpoint `SIEVE_LLM_BASE_URL`, model `SIEVE_LLM_MODEL`=auto) | `--smart PROMPT` |
| `sieve screenshot URL` | page capture (pool full-page; `--browser-backend sleeper` = viewport PNG via your browser, `--output PATH`) | `--full-page`, `--timeout SEC` (30) |
| `sieve search QUERY --cached` | return a cached result or a machine-readable cache miss | `--max-results N` |
| `sieve search QUERY --refresh` | bypass the read cache and replace the stored result | `--max-results N`, `--timeout SEC` |
| `sieve search QUERY --stale-fallback` | use a marked keyless cache entry after a total transient live failure | `--stale-max-age SEC` (3600; 1-86400) |
| `sieve cache clear` / `clear-all` | clear expired / wipe all cache | — |

Image search uses Brave's documented API. Follow [Brave Search API terms](https://api.search.brave.com/app/terms),
provide any required attribution, and verify each image's license and reuse rights with its source before downloading or publishing it.

Batch checkpoints use version 2 and bind the ordered URLs, schema and timeout.
Resume preserves successful and failed outcomes; `--retry-failed` retries only
known failures. Changing the inputs requires a fresh checkpoint. Old version 1
files require explicit `--resume --migrate-v1`: their outcomes are imported as
unknown, never reported as successful, because the old format did not record
failure status. Use a fresh checkpoint to rerun those unknown outcomes.

`--progress` writes completion summaries to stderr while extraction runs;
stdout remains final JSONL in input order. Python callers can use `on_progress`
for these summaries and `emit` for ordered final records. Custom extractors
must provide a stable `checkpoint_key` and change it when adapter behavior
changes. Cancellation stops queued and asynchronous work and closes owned
sessions; it cannot interrupt an already running synchronous adapter thread.
SDK batches with an injected backend or extra extraction options also require
this key when checkpointing. The default SDK extractor supplies its own key.

Stale search fallback is disabled by default. It reports `stale`,
`cache_age_seconds`, and `stale_reason`, preserves original engine/proxy
provenance, and leaves the stored timestamp unchanged. It is unavailable for
authenticated search, `--cached`, `--refresh`, and find-similar mode. Empty
successful responses, partial results, and validation/policy failures never
trigger it. Authenticated search results are not persisted in the shared cache.


## Source verbs

| Command | Purpose | Key flags |
|---|---|---|
| `sieve youtube search QUERY` | bounded YouTube search via external `yt-dlp` | `--max-results N` (6), `--timeout SEC` (45) |
| `sieve youtube metadata URL` | inspect one video | `--timeout SEC` (45) |
| `sieve youtube download URL DIR` | download (needs `yt-dlp` binary) | `--format FMT`, `--timeout SEC` (120) |
| `sieve social login instagram` | open Sieve Chromium for one-time login to a dedicated persistent profile | `--profile-dir PATH` (default `~/.sieve/profiles/instagram`) |
| `sieve social fetch URL` | normalized public Instagram/TikTok record; browser profile corpus supports resumable retention filtering | `--reader sieve\|jina\|browser`, `--max-media N` (20), `--max-posts N`, `--checkpoint FILE`, browser options |
| `sieve social collect PROFILE` | paced, bounded profile-grid collection — **opt-in** (`SIEVE_ENABLE_SOCIAL_COLLECT=1`) | `--creator H`, `--cdp-url URL`, `--scrolls N` (20), `--scroll-delay MS` (1200), `--max-items N` (100), `--checkpoint FILE` |
| `sieve social comments URL` | post comments; the browser reader requires social-browser opt-in and a local session | `--max-comments N` (50), `--reader ytdlp\|browser`, `--timeout SEC` |
| `sieve media download URL` | authenticated local media resolution through supported external tools | `--timeout SEC` |
| `sieve media transcribe URL` | local whisper (needs `stt` extra); omitted timeout derives from best-effort duration metadata, refusing sources over 1800s | `--model NAME`, `--timeout SEC` (explicit, capped at 600; automatic fallback 300) |

Social browser commands accept `--cdp-url URL`, `--real-chrome`,
`--wait-selector CSS`, and `--network-idle`. Generic `fetch`, `crawl`, and
`extract` expose the smaller `--reader browser --browser-backend
auto|pool|sleeper` surface.
Browser `comments` is pool-only because comment parsing needs the pool DOM.
`screenshot --browser-backend sleeper` captures the viewport; `--full-page`
stitching stays pool-only.

`sieve social login instagram` is interactive and CLI-only. Once login is
complete, CLI and MCP browser operations can reuse the profile headlessly via
`SIEVE_BROWSER_PROFILE_DIR`. Media subprocesses read that profile through
their browser-cookie interface only after Chromium is closed; cookie values
are not emitted or copied to a cookie file.

## Generic browser tiers

`sieve fetch`, `sieve crawl`, and `sieve extract` accept `--reader browser
--browser-backend sleeper` for the real-browser daemon. `--browser-backend
auto` selects Sleeper when its daemon is available and the pooled headless
browser otherwise. A failed Sleeper navigation remains a structured JSON
failure and exits 1.

## Management verbs

| Command | Purpose |
|---|---|
| `sieve keys add\|list\|test\|remove\|clear` | BYOK search keys (serper, tavily, exa, firecrawl, tinyfish, brave) |
| `sieve proxy add\|list\|remove\|clear` | search proxy pool (rotation automatic) |
| `sieve mcp serve --transport http` | streamable HTTP MCP server |
| `sieve --version` / `--doctor` | version + install diagnostics |

## Environment

Full variable table and precedence rules are in
[configuration](configuration.md).
The two that gate behavior: `SIEVE_ENABLE_SOCIAL_COLLECT=1` (browser social
collection, off by default) and
`SIEVE_BROWSER_PROFILE_DIR` (persistent browser identity).

## MCP tools

Canonical tools advertised by `sieve mcp serve`: public fetch, crawl, extraction,
search, screenshot, block detection, sitemap, and research ingestion behaviors.
Operator capabilities that handle browser identity, cookies, headers, or
proxies are configured locally and are excluded from the default catalog.
Prefer CLI verbs for one-shot automation; see [mcp](mcp.md).

The public surface has one name for each behavior. Unknown prefixed tool names
and the retired `--http` entrypoint are rejected.
