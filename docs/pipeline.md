# Declarative pipelines

`sieve pipeline` composes Sieve's existing search, fetch, and extraction operations in
a small JSON DAG. It is useful when an agent needs one reproducible request
that fans out over several known URLs and returns one machine-readable result.
The runner accepts only the named `fetch`, `extract`, `batch_extract`,
`search_fetch`, and optional `schema_gen` stages; specs cannot execute shell
commands, Python, or imports.

```json
{
  "version": 1,
  "nodes": [
    {"id": "landing", "stage": "fetch", "url": "https://example.com"},
    {"id": "records", "stage": "batch_extract",
     "urls": ["https://example.com/a", "https://example.com/b"],
     "schema": {"baseSelector": "article", "fields": [{"name": "title", "selector": "h1", "type": "text"}]},
     "depends_on": ["landing"]}
  ]
}
```

Run it with `sieve pipeline pipeline.json`. Nodes are validated before any
network work: IDs and dependencies must be known, the graph must be acyclic,
and URL/schema fields must have the expected types. Execution follows the
spec's stable topological order. Each output includes `id`, `status`, and
provenance; a failed node causes dependent nodes to be reported as skipped.

The command bounds node count, output items, and total runtime. Override those
limits only for a deliberately bounded workload with `--max-nodes`,
`--max-items`, and `--timeout`. Batch fan-out keeps its own concurrency and
URL limits. An integration may provide `schema_gen` to make one BYOK LLM call
over a bounded sample and reuse the resulting schema for later deterministic
extraction. Results are one deterministic JSON document on stdout, with no
response bodies or credentials persisted by the pipeline layer.

## Search to fetch

Use a `search_fetch` node with normalized search result objects. It applies
explicit bounds and filters, validates every URL through Sieve's SSRF and
scheme checks, and fetches selected URLs with bounded concurrency. Results
remain in search order; the pipeline does not rank URLs or inspect page
content to decide what to execute.

```json
{"version": 1, "nodes": [{"id": "pages", "stage": "search_fetch",
  "results": [{"url": "https://example.com/docs", "title": "Docs",
               "relevance_score": 0.9, "source_type": "docs"}],
  "max_results": 3, "max_concurrency": 3, "allowed_domains": ["example.com"],
  "allowed_source_types": ["docs", "reference"], "min_relevance": 0.5}]}
```

Keep `max_results` and `max_concurrency` explicit and small for agent-authored `search_fetch` nodes; they bound selected results and simultaneous fetch work independently.

### Concurrency bounds

Fan-out concurrency is bounded at two levels:

- **Per node** — a `search_fetch` node may set `max_concurrency` as an integer
  from 1 to 32 (booleans are rejected). It controls how many fetches that node
  runs in parallel through an internal semaphore.
- **Runner default** — `run_pipeline(..., search_max_concurrency=4)` (CLI:
  `sieve pipeline`) supplies the default when a node does not set its own
  `max_concurrency`. A node override always wins over the global default.

Invalid values — `0`, `33`, `true`/`false`, or any non-integer — are rejected
by spec validation before any network work, both at the node level and for
the runner default. Cancellation and timeouts apply on top of the semaphore:
when the pipeline's total runtime budget is exhausted, in-flight fetches are
cancelled and remaining items are reported as `timeout` per item rather than
silently dropped.

Malformed or disallowed results are omitted. Each fetched item carries its
original search index and selected provenance. Fetch errors and timeouts are
returned per item so one blocked page does not hide successful pages.
