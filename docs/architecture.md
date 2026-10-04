# Architecture

The capability registry in `sieve/command_router.py` connects each canonical
name to a callable handler, its public MCP definition and required extras.
Public tool catalogs derive from these definitions. Operator-only capabilities
remain callable locally and are absent from the public catalog.

`scripts/architecture_snapshot.json` records exact modules, import edges and
public exports. CI compares their contents, so replacements that preserve counts
still fail. For an intentional change, regenerate with
`python scripts/architecture_report.py --output scripts/architecture_snapshot.json`
and review the added and removed entries before committing.

`scripts/primitive_inventory.py --check` compares known direct network,
subprocess and file-write calls with `scripts/primitive_inventory_snapshot.json`.
The inventory resolves imported aliases and simple client/path constructors.
New or removed calls produce locations and enclosing functions for review.
Regenerate deliberately with `--output scripts/primitive_inventory_snapshot.json`.
The initial snapshot records existing calls, not security approvals. Its owner,
bounds and secret-policy review fields explicitly record unresolved review work.
Dynamic dispatch, flow-sensitive aliasing and dependency internals remain outside
the scan, so a clean inventory does not prove a request is bounded or safe.

Sieve is a local research engine with three entry paths: a short-lived CLI, an
MCP server, and direct internal Python calls used by those interfaces. The CLI
and MCP layers share the same search, retrieval, crawl, and extraction
functions, so transport choice does not change the result contract.

```text
CLI subprocess                 MCP client
      │                            │
      └──────── command/tool dispatch ────────┐
                                               │
        search ──► select sources              │
                     │                         │
        crawl ───────┼──► retrieval policy ◄──┘
                     │       │
                     │       ├─ HTTP
                     │       ├─ rendered browser
                     │       └─ authorized existing session
                     │
                     └──► parse and extract
                              │
                              └──► bounded JSON envelope
                                   content + sources + status + next_action
```

## Package areas

All runtime code lives under `sieve/`. Modules are grouped by responsibility
through their interfaces even though the package remains flat and easy to
inspect.

| Area | Main modules | Responsibility |
|---|---|---|
| Entry points | `cli.py`, `mcp_stdio.py`, `server.py`, `command_router.py` | Parse commands or MCP calls and serialize responses |
| Search | `search.py`, `search_engines.py`, `search_metasearch.py`, `reranker.py` | Query providers, normalize results, and rank sources |
| Retrieval | `fetcher.py`, `browser.py`, `browser_sessions.py`, `server_fetch.py`, `network_capture.py` | Choose HTTP or browser paths, own browser lifecycle, and capture bounded page data |
| Crawling | `crawl.py`, `domain_discovery.py`, `sitemap.py`, `robots.py`, `autothrottle.py` | Traverse within page, depth, domain, and pacing limits |
| Extraction | `extraction.py`, `extractor.py`, `trafilatura_extractor.py`, `tables.py`, `chunking.py`, `script_harvest.py` | Turn pages into Markdown, records, tables, links, and chunks |
| Safety and state | `security.py`, `cache.py`, `config.py`, `antibot_detector.py` | Validate destinations, bound state, configure behavior, and identify unusable content |
| Source adapters | `youtube.py`, `social.py`, `reddit_rss.py`, `pdf_extractor.py`, `ocr.py`, `stt.py` | Normalize non-article sources behind the same CLI boundary |

Public integrations live under `plugins/`; runnable user flows live under
`examples/`; stable user and integration guidance lives under `docs/`; tests
cover the public behavior of the corresponding runtime areas.

## Retrieval flow

For `sieve fetch URL`, command dispatch validates the URL and bounds, then asks
the retrieval layer for content. A direct HTTP request is preferred. If the
result is blocked, empty, or a JavaScript shell, policy may select a rendered
path when the installed capabilities and caller permissions allow it.

The extractor cleans the returned document, preserves useful links and
metadata, and emits one envelope. Every retrieval path reports the same core
fields, including the final URL, content status, truncation, retrieval method,
error context, and next action.

### Browser lifecycle seam

`browser.py` owns one browser's navigation, page pooling, and rendering
behavior. `browser_sessions.py` owns the process-level registry around those
engines: explicit session IDs, race-safe auto-session reuse, idle eviction,
startup prewarm, and shutdown cleanup. The MCP facade keeps the historical
`MasterFetchServer.open_session`/`close_session` methods as thin compatibility
adapters, while retrieval callers use the same registry for warm browser
sessions. The registry has no MCP or Pydantic dependency, so its lifecycle
contract can be tested with an injected browser factory.

## Trust boundaries

- URLs and redirects are untrusted. Validation occurs before connections and
  again when a redirect or browser path changes the destination.
- Page text, scripts, and captured network responses are untrusted content.
  They are data for extraction and never instructions for Sieve itself.
- Provider keys, proxy credentials, browser profiles, and MCP bearer tokens
  stay in process environment or operator-controlled storage.
- Browser cookies and authorization headers are not copied into response
  payloads.
- Crawls, response bodies, decompression, subprocess output, browser tabs, and
  wall time have explicit bounds.
- Optional dependencies extend a capability without changing the core JSON
  contract.

## Extension points

Add a search provider behind the normalized search interface, a retrieval tier
behind the fetch envelope, or a source adapter behind a CLI subcommand. New
paths must preserve URL validation, resource bounds, structured failures, and
provenance. Adapted upstream code is recorded in [PORTS.md](../PORTS.md) with
its source and license.
