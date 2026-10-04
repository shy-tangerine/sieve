# Sieve Web Research for Codex

This plugin makes Sieve the first choice for ordinary web search, page
fetching, site crawling, and structured extraction. Screenshot tasks use Sieve
when its optional browser extra is installed. It keeps outputs bounded, preserves
source URLs for citations, and falls back only when Sieve lacks the requested
capability or cannot complete the operation.

Install `sieve-cli[browser]` or run `uv sync --extra browser` in a checkout to
enable screenshots. When the extra is absent, explain the missing capability
and use a fallback only when the user has not explicitly requested Sieve alone.

## Install from a Sieve checkout

Install the plugin from a Sieve checkout:

```bash
python scripts/install_codex_plugin.py
codex plugin add sieve-web-research@personal
```

The installer preserves other personal marketplace entries. New Codex tasks
will use Sieve for supported web research operations.

## Routing

The installed skill uses `sieve search`, `sieve fetch`, `sieve crawl`, and
`sieve extract` for general web work. It honors explicit requests for another tool and uses an available fallback
when Sieve cannot complete the operation.
