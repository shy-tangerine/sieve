# Sieve Web Research for Claude Code

This Claude Code plugin routes web search, fetching, crawling, and structured
extraction through Sieve’s local CLI. Screenshot tasks use Sieve when its
optional browser extra is installed. Search is keyless by default; capabilities
that need optional dependencies remain explicit. It adds a Sieve-first skill and
a supported `SessionStart` hook that makes the routing preference visible at
the beginning of each session.

Install `sieve-cli[browser]` or run `uv sync --extra browser` in a checkout to
enable screenshots. When the extra is absent, explain the missing capability
and use a fallback only when the user has not explicitly requested Sieve alone.

## Install

From a checkout of Sieve, add the local marketplace directory to Claude Code:

```text
/plugin marketplace add ./plugins/claude
/plugin install sieve-web@sieve
```

Then run `/reload-plugins` if Claude Code asks. The plugin needs the `sieve` executable on `PATH`, or an installed Sieve checkout invoked with `uv run --directory ... sieve`.

The skill is a routing preference with an explicit user override. It does not block other tools or silently enable authenticated browser collection. Hosts that require MCP can configure `sieve mcp serve --transport stdio` separately after installing the optional MCP dependencies.
