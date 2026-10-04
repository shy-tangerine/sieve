# Sieve Claude Code marketplace

This local marketplace distributes the `sieve-web` plugin. It gives Claude Code a Sieve-first routing skill for web research and two explicit slash commands.

Install from the Sieve checkout with:

```text
/plugin marketplace add ./plugins/claude
/plugin install sieve-web@sieve
```

For a published checkout, use the repository path containing this directory as the marketplace source. The plugin expects `sieve` on `PATH` and keeps the user in control: explicit requests for another tool take precedence, and browser authenticated collection is never enabled by the plugin.
