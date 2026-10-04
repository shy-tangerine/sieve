# Workflows

Sieve commands return JSON on stdout and keep requests bounded. From a
checkout, use `uv run sieve`; after `uv tool install .`, use `sieve` directly.

## Discover and read

```bash
uv run sieve search "python web scraping" --max-results 3 --timeout 20
uv run sieve fetch https://example.com --timeout 10
```

Search returns ranked `results`; fetch returns a normalized record with
`status`, `content`, and `next_action`.

To consume page-declared product, recipe, or other Schema.org entities, opt in
to the structured extraction mode:

```bash
uv run sieve fetch --structured https://example.com/product --timeout 10
```

The normalized response keeps the usual `status` and `content` fields and adds
JSON-LD and nested Schema.org microdata under `metadata.structured_data`.
RDFa is not included in this contract.

## Extract structured records

```bash
uv run sieve extract https://example.com \
  --schema '{"baseSelector":"a","fields":[{"name":"href","selector":"a","type":"attribute","attribute":"href"}]}' \
  --timeout 10
```

The response contains an `items` array. Use `--schema @schema.json` for a
larger schema and `--jsonl` for line-oriented processing.

## Crawl with a budget

```bash
uv run sieve crawl https://example.com --max-pages 5 --max-depth 2 --timeout 20
```

The response contains `pages`, `pages_crawled`, and truncation flags. Keep
budgets explicit when running inside an agent.

## Agent routing

Use `search` for discovery, `fetch` for a known URL, `crawl` for bounded
exploration, and `extract` for a known schema. Browser options are for pages
that need JavaScript or an operator-authorized session.
