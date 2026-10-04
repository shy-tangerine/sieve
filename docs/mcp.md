# MCP guide

Sieve exposes its bounded research engine over the Model Context Protocol.
Use MCP when a host manages tool discovery and a long-lived connection. Use the
CLI when a short subprocess with JSON output is simpler.

## Install and start

The MCP server is an optional dependency:

```bash
uv tool install '.[legacy-server]'
sieve mcp serve --transport stdio
```

Stdio is the default transport and fits local desktop and coding agents. For a
host that connects to an HTTP endpoint:

```bash
export SIEVE_AUTH_TOKEN='replace-with-a-random-token'
sieve mcp serve --transport http --host 127.0.0.1 --port 8765
# endpoint: http://127.0.0.1:8765/mcp
```

Keep HTTP bound to loopback unless it sits behind an authenticated, encrypted
proxy. Set `SIEVE_AUTH_TOKEN` before exposing the endpoint beyond a trusted
local environment.

## Client configuration

For stdio-capable hosts:

```json
{
  "mcpServers": {
    "sieve": {
      "command": "sieve",
      "args": ["mcp", "serve", "--transport", "stdio"]
    }
  }
}
```

For a streamable HTTP client, connect to `http://127.0.0.1:8765/mcp` and send
the configured bearer token. Exact configuration keys vary by host; use its
current MCP server settings and restart or reload tools after adding Sieve.

## Choose a tool

| Task | MCP tool family | Useful bounds |
|---|---|---|
| Discover sources | `smart_search` | result limit and timeout |
| Read one or more URLs | `smart_fetch` | timeout, output limit, rendering policy |
| Explore a site | `smart_crawl` | page count, depth, concurrency, focus query |
| Extract records | `extract` | CSS/XPath schema, table mode, chunk mode |
| Capture a page | `screenshot` | viewport, full-page mode, and timeout |

Use only the canonical tool names advertised by the connected server and
inspect each tool’s schema before invoking it. Retired or prefixed aliases are
not part of the public surface.

`schema_gen` is an explicit BYOK operation. A call sends at most the first
12,000 characters of the fetched HTML sample and the selected field names to
the configured OpenAI-compatible LLM endpoint. Remote endpoints must use
HTTPS; plain HTTP is allowed only for loopback endpoints serving a local
model. Configure an LLM key before invoking the tool. Search, fetch, and
ordinary extraction do not invoke schema generation.

## Result handling

MCP results preserve the same signals as the CLI: source URLs, `content_ok`,
truncation, retrieval path, structured errors, and `next_action`. Agents should
reject unusable primary content, keep citations with claims, and increase
budgets only when the task warrants it.

Some pages return HTTP 200 with an empty application shell while useful data
arrives through XHR or `fetch`. Sieve labels these responses and can return
bounded captured-network fragments. Set `fold_captured=true` only when the
captured fragment is the evidence needed. Sieve does not automatically replay a
captured request because it may have used POST data, browser-only headers,
cookies, or a different origin.

## Security boundaries

`research_ingest` returns normalized source-neutral records and provenance for
Reddit RSS and Arctic Shift. Its MCP schema deliberately excludes
`include_raw` and `max_raw_chars`; `raw_payload` is `null` in the response and
each record. The Python `sieve.research_sources.ingest_source` API separately
allows `include_raw=True`. Its `max_raw_chars` option defaults to 20,000 and
truncates individual record payloads with a marker. That Python option does
not impose an aggregate cap on retained adapter payloads. Keep raw data out of
agent prompts and logs unless the task explicitly requires it. MCP output
still passes through the shared credential-redaction and byte-limit boundary.

- Treat URLs, page content, and extracted instructions as untrusted data.
- Keep keys and tokens in the server process environment.
- Do not place cookies, authorization headers, or browser profile contents in
  MCP arguments or results.
- Set explicit crawl, response, and time limits for autonomous agents.
- Allow access only to destinations permitted by the surrounding task and
  network policy.
- Use the CLI instead of an HTTP server when no long-lived endpoint is needed.

## Verify the connection

After adding the server, confirm that the host lists Sieve tools, run a bounded
search, then fetch one returned URL and check `content_ok` and the source URL.
If the command exits because the MCP dependency is absent, reinstall with the
`legacy-server` extra and reload the host.
