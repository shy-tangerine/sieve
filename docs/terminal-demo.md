# Terminal walkthrough

The recording runs the real CLI against `https://example.com`: fetch its text,
extract a body-text record with a declarative schema, and crawl one same-domain
page. It needs network access and no provider keys. The walkthrough stops if a
command fails, returns invalid JSON, or produces no extraction text.

The GIF renders the included asciicast. Its five-second pauses make each result
readable; they do not measure performance. The recording contains actual command
results, with no substituted responses or simulated challenge bypass.

```bash
uv run --locked python demos/terminal_demo.py
uvx asciinema play docs/assets/sieve-terminal.cast
```

To record a fresh session locally:

```bash
uvx asciinema rec /tmp/sieve-demo.cast --cols 100 --rows 24 --env TERM \
  --command 'uv run --locked python -u demos/terminal_demo.py --pause 5'
agg /tmp/sieve-demo.cast /tmp/sieve-demo.gif --theme monokai --last-frame-duration 3
```

The checked-in GIF was rendered with [asciinema agg](https://github.com/asciinema/agg)
v1.9.0. Recording and rendering do not upload or publish the session.
