# Agent plugins

Sieve ships first party plugins that make its local CLI the default web
research and scraping route for Codex and Claude Code. They route ordinary
search, page reading, bounded crawling, screenshots, and deterministic
extraction through Sieve while honoring an explicit request for another tool.

Install Sieve first so the `sieve` executable is available on `PATH`:

```bash
uv tool install .
sieve --version
```

Built-in search needs no provider key. Add optional extras only for the
capabilities you need; see [configuration](configuration.md).

## Codex plugin

From a Sieve checkout, install the plugin into your personal Codex marketplace:

```bash
python scripts/install_codex_plugin.py
codex plugin add sieve-web-research@personal
```

The installer preserves existing personal marketplace entries and upgrades
only folders marked as Sieve-managed. Unmarked folders, including older
installs, require `python scripts/install_codex_plugin.py --overwrite` to
replace them. This replaces the whole plugin folder; copy any custom files
you want to keep first. Failed upgrades restore the previous folder.

Start a new
Codex task so the routing skill is loaded. It uses `sieve search`,
`sieve fetch`, `sieve crawl`, and `sieve extract` for ordinary web work, with
bounded output and source URLs preserved for citations.

## Claude Code plugin

From the Sieve checkout, add the local marketplace in Claude Code:

```text
/plugin marketplace add ./plugins/claude
/plugin install sieve-web@sieve
/reload-plugins
```

The plugin package is `plugins/claude/sieve-web`. Its skill and `SessionStart`
hook make the Sieve-first preference visible while leaving explicit tool
choices and authenticated browser use under user control.

## Routing examples

```bash
sieve search "query" --max-results 5 --timeout 20
sieve fetch https://example.com --timeout 10
sieve crawl https://example.com --max-pages 5 --max-depth 2 --timeout 20
sieve extract https://example.com \
  --schema '{"baseSelector":"a","fields":[{"name":"href","selector":"a","type":"attribute","attribute":"href"}]}' \
  --timeout 10
```

Use `search` for discovery, `fetch` for known URLs, `crawl` for bounded site
exploration, and `extract` for repeated records or a known schema. Prefer
direct HTTP extraction. Use browser options for JavaScript pages or an
operator-authorized session. Keep API keys, cookies, and browser profiles in
operator configuration rather than plugin or repository files.
