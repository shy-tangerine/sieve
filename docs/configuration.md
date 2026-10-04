# Installation and configuration

## Install Sieve

Sieve requires Python 3.11 or newer. The recommended installer checks existing
local capabilities before it installs the complete tool:

```bash
sh scripts/install.sh
sieve --version
sieve fetch https://example.com
```

It reports compatible transcription tools, browser runtimes, CDP endpoints,
and `yt-dlp`, so Sieve can reuse an existing installation instead of adding a
second copy. Setup asks before saving detected local endpoints or tools.

## Manual and development installs

For a manual complete installation with [uv](https://docs.astral.sh/uv/):

```bash
uv tool install '.[all]'           # every optional capability
```

Individual extras remain available for constrained environments and are listed
in `pyproject.toml`. Contributor setup lives in [CONTRIBUTING.md](../CONTRIBUTING.md).

## Configure Sieve

Run `sieve setup` for guided configuration. Settings live in
`~/.sieve/config.toml`; provider keys live in Sieve's separate key store.
Explicit flags take precedence over environment variables, which take
precedence over configuration and built-in defaults.

Most users can begin without configuration. Add provider keys only when a
workflow needs a particular search or extraction service:

```bash
sieve keys add serper
sieve keys list
sieve keys test serper
```

The command prompts for the value rather than placing it in shell history.

### LLM credentials

Smart extraction, schema generation, routed LLM operations, and CLI pipelines
use the same LLM key resolver. An explicit API key supplied through the Python
API takes precedence. Otherwise Sieve selects the first key from
`SIEVE_LLM_KEYS`, a comma-separated list, or from the `llm` provider in
`~/.sieve/search_keys.json`. The canonical environment setting overrides that
provider's file entry. Use `sieve keys add llm` for hidden interactive entry.

When canonical sources are empty, `SIEVE_LLM_API_KEY` and then
`FREELMAPI_API_KEY` remain supported legacy aliases. Sieve logs a deprecation
notice naming the alias, without its value. These aliases remain supported
until a separately announced migration removes them. Missing credentials fail
before the LLM request; Sieve never supplies a placeholder key.

Schema generation is opt-in and sends a bounded HTML sample (up to 12,000
characters) and selected field names to the configured OpenAI-compatible
endpoint. Remote endpoints require HTTPS; HTTP is limited to loopback for a
local model service. This transfer occurs only when the schema-generation
operation is explicitly called.

Image search uses the documented Brave Search Images API and is strictly
BYOK-gated. Configure a Brave key with `sieve keys add brave` (or set
`SIEVE_SEARCH_BRAVE_KEYS` as a comma-separated list). `sieve images QUERY`
defaults to strict safe search, bounds result count and timeout, and returns a
structured `no_provider`, `rate_limited`, or `timeout` failure when the
provider is unavailable. There is no keyless image scraping fallback. Follow
[Brave's API terms](https://api.search.brave.com/app/terms), include required
attribution, and check source-specific image licenses and reuse rights before
using returned images.


## Browser-backed work

Direct HTTP is the first fetch tier. Install the `browser` extra for pages
that require rendering, screenshots, or an operator-authorized session.
Sieve can own an isolated profile or attach to an endpoint supplied by the
operator. Keep profiles outside the repository and use one owner at a time.

Relevant settings include:

Sieve-managed Chromium contexts send HTTP and HTTPS traffic through an
authenticated loopback proxy. Redirects, frames, popups and Service Workers
must pass public-address checks before connecting to a validated IP. Configured
HTTP/HTTPS and SOCKS proxies remain chained; proxy failures do not trigger a
direct connection. HTTP forward requests retain the original Host header;
HTTPS tunnels retain the original TLS hostname. Each connection has bounded
headers, idle time, lifetime and a shared 64 MiB transfer limit.

The browser fetch timeout covers setup, navigation, callbacks and retries as
one deadline. A timed-out fetch discards its tab; a completed fetch navigates
to a blank page before returning the tab to the pool.

| Setting | Purpose |
|---|---|
| `SIEVE_BROWSER_PROFILE_DIR` | Dedicated persistent profile for Sieve-owned rendering |
| `SIEVE_BROWSER_IDLE_TIMEOUT` | Idle timeout for the pooled runtime |
| `SIEVE_BROWSER_BACKEND` | Select the configured rendering backend |
| `SIEVE_ENABLE_SOCIAL_COLLECT` | Explicitly enable paced browser collection |
| `SIEVE_AUTH_TOKEN` | Bearer token required by HTTP MCP transport when set |
| `IG_TRANSCRIPTS_DIR` | Retained Instagram transcript directory; corpus collection skips codes with existing transcript files |
| `SIEVE_YTDLP_COOKIES_FILE` | Explicit operator-supplied cookie file override for media commands |
| `SIEVE_COOKIES_FROM_BROWSER` | Explicit browser-cookie source override for local media tools |
| `SIEVE_SEARCH_DEADLINE` | Overall search deadline |

Treat profile locations, cookie files, endpoints, and keys as operator
configuration. Keep them out of repository files and diagnostic output.

### Fetch caching

Requests that supply cookies or custom headers bypass both cache lookup and
storage, even when `cache_ttl` is positive. Sieve does not put these values or
their hashes in persistent cache keys. Anonymous requests can still reuse their
cached content. Browser-profile identity and other request-context dimensions
remain part of the broader cache-policy work; use `cache_ttl=0` for authenticated
browser sessions.

### Dedicated Instagram profile

Create the authenticated profile once from an interactive terminal:

```bash
sieve social login instagram
```

The default location is `~/.sieve/profiles/instagram`; override it with
`--profile-dir PATH`. The command opens Sieve Chromium visibly for login, MFA,
or checkpoints and closes it after the operator returns to the terminal and
presses Enter. Set `SIEVE_BROWSER_PROFILE_DIR` to the same path for later
headless CLI and MCP browser work.

Unless an explicit cookie source is configured, supported local media tools
read the dedicated Sieve Chromium profile in place after Chromium closes.
`yt-dlp` is the single-media resolver. Sieve does not export cookie values, and
the profile must not be opened concurrently.

## MCP

Install the `legacy-server` extra, then start the standard input transport:

```bash
sieve mcp serve --transport stdio
```

For a local HTTP endpoint, pass `--transport http` with an explicit host and
port. Set `SIEVE_AUTH_TOKEN` when the compatibility service must require a
bearer token. See [the MCP reference](mcp.md) for host configuration and the
captured-network response contract.

## Diagnose an installation

```bash
sieve --doctor
sieve --help
```

Sieve writes structured command results to stdout and diagnostics to stderr.
A failed command still emits a JSON failure record when that command uses the
machine-readable CLI contract.

## Reuse existing local tools

`sieve setup` reports local candidates before it asks about persistent
configuration. It checks for `whisper` and `whisper-ctranslate2`, browser
runtimes, a local `SIEVE_CDP_URL`, and `yt-dlp`. Detection checks that an
executable resolves to a real executable file; it does not trust an arbitrary
PATH entry, import a daily browser profile, or save a remote endpoint.

The supported external transcription adapter is the OpenAI Whisper CLI. When
the operator approves `stt_executable` (or sets `SIEVE_STT_EXECUTABLE`), Sieve
invokes `<path> INPUT --model MODEL --output_format txt --output_dir DIR` and
reads the resulting `.txt` file. Other tools are reported for awareness and
remain unconfigured until an adapter with an explicit contract is provided.
`yt-dlp` is reused automatically by media commands when it is already on
`PATH`; browser runtimes are used only through Sieve's isolated profile or an
explicit local CDP attachment.

## Sleeper real-browser tier

Generic `fetch`, `crawl`, and `extract` accept `--reader browser
--browser-backend sleeper` for the operator's real browser. With
`--browser-backend auto`, Sieve selects Sleeper when its local daemon is
available and otherwise uses the pooled headless browser.

`SIEVE_SLEEPER_AUTO_ESCALATE=1` opts in to a final Sleeper attempt after a
headless browser returns a detected JavaScript shell. It is disabled by
default.

Sleeper's current extension permits its authenticated `api` bridge only for
`chatgpt.com` and `www.chatgpt.com`. Sieve applies the same preflight boundary.
Custom `SIEVE_SLEEPER_API_HOSTS` values are not supported until the companion
extension implements and verifies a config-driven allowlist; setting the value
in Sieve alone cannot expand the extension's hard-coded boundary.
