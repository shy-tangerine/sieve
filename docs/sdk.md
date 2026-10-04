# Python SDK

Sieve's typed client gives Python applications async access to the same search,
fetch, crawl, extraction, and bounded batch operations as the CLI and MCP
surfaces. The default client creates its backend on first use. Tests and
long-lived applications can supply another backend with the same interface.

## Install and use

The SDK ships with the base package:

```python
from sieve.sdk import SieveClient

async with SieveClient(timeout=20, concurrency=4) as sieve:
    hits = await sieve.search("python async programming")
    page = await sieve.fetch(hits["results"][0]["url"])
    data = await sieve.extract(
        "https://example.com",
        {"baseSelector": "article", "fields": [{"name": "title", "selector": "h1", "type": "text"}]},
    )
```

All methods are coroutines. `search()` and `crawl()` forward the backend's
native bounded controls; `fetch()` and `extract()` apply the client's timeout
in milliseconds because that is the server operation contract. Batch records
are ordered by input and contain either `ok: true` with `result`, or
`ok: false` with a short `error` and `category`. Resumed records report their
saved status without repeating the original result body. Version 2 checkpoints
contain a configuration digest and item indices/statuses; they never persist
response bodies, cookies, or keys.

## Batch checkpoints and progress

`batch()` accepts `checkpoint`, `resume`, `retry_failed`, `migrate_v1`,
`checkpoint_key`, and `on_progress`. Checkpoints bind the ordered URLs, schema,
timeout, and adapter identity. Resume skips saved outcomes; `retry_failed=True`
retries only known failures. Changing bound inputs requires a fresh checkpoint.
Version 1 files require explicit migration and retain unknown outcomes rather
than claiming success. See [the CLI migration rules](cli.md).

The default SDK extractor supplies a stable checkpoint key. Injected backends
and batches with extra extraction options require an explicit `checkpoint_key`
when checkpointing. Change it when backend behavior or those options change.

`on_progress(record)` receives a completion summary during work, without the
result body. It may be synchronous or asynchronous; final records remain
ordered by input. Cancelling a batch stops queued and asynchronous work.
Already running synchronous adapter threads cannot be interrupted.

## Supported interface

The public import is `sieve.sdk`. `SieveClient`, `SieveBackend`, `BatchRecord`,
`ClientLimits`, and `SieveError` are the supported names. Backend injection is
structural: a backend must provide async or sync-compatible
`smart_search`, `smart_fetch`, `smart_crawl`, and `extract` methods. A backend's
optional `close()` is awaited by the async context manager.

Errors from backend calls are wrapped in `SieveError`, which preserves the
operation name and original exception as `cause`. Input bound violations remain
`ValueError`. The default backend is created only when the first operation is
called, and `close()` releases it; the client stores no credentials or browser
session data itself.

Return values use the same JSON-compatible models and dictionaries as the
other Sieve interfaces, so
the Python API stays aligned with the CLI JSON seam. Applications that need a
long-term wire contract should serialize those values and validate the fields
they consume.

Search responses include `proxy_routes`, a list of safe route selections made
by the original search: `configured_proxy`, `direct_fallback`, or
`no_proxy_config`. Multiple labels can appear after provider failover. Cached
results retain the original labels; older cache entries and requests that
select no route have an empty list. Direct fallback from a cooling proxy pool
still requires `SIEVE_PROXY_ALLOW_DIRECT=1`. Proxy addresses and credentials
are never included in these labels.

Browser fetches that execute page actions include `action_results`. Each
entry has a zero-based `index`, action `type`, `status` (`ok` or `error`), and
`category` (empty on success, `timeout` or `execution_error` on failure).
An action failure does not prevent later actions from running. Reports contain
at most 20 entries and omit selectors, fill values, pressed keys, and exception
messages. A failed custom callback uses type `callback`. Other callback return
values are ignored. Fetches without actions return an empty report.

## Generate a schema from sample HTML

`sieve.schema_gen.generate_schema(sample_html, fields, api_key=...)` generates
a reusable extraction schema with one LLM call. Its synchronous URL helper,
`generate_schema_from_url(url, fields, *, fetch, **options)`, requires a
caller-supplied `fetch(url) -> str` callable. Pass the callable explicitly as
`fetch=fetch_sample`; omitting it or passing a non-callable raises `TypeError`.
The callable must enforce URL and redirect policy, deadlines, and response
bounds. Fetch exceptions propagate unchanged, and non-text results raise
`ValueError`. Other options, including `api_key`, `base_url`, `model`, and
`timeout`, go to `generate_schema`; `timeout` limits the LLM request, not the
injected fetch.

An explicit `api_key` overrides the shared [LLM key configuration](configuration.md#llm-credentials).
When it is omitted, schema generation uses the canonical LLM provider and its
documented legacy fallbacks.

Schema generation is an explicit BYOK operation. It sends at most the first
12,000 characters of the supplied HTML sample and the selected field names to
the configured OpenAI-compatible endpoint. Remote endpoints must use HTTPS;
plain HTTP is permitted only for loopback endpoints used by local services.
Sieve requires an LLM key before making the request. Ordinary fetch and
extraction calls do not invoke schema generation.
