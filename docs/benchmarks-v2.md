# Benchmark v2

Sieve's evidence is split by job so a fast static fetch cannot be mistaken for
a rendered crawl or an extraction-quality result. The default suites use a
small checked-in fixture model and do not need a network connection:

```bash
python scripts/bench_v2.py --runs 5
python scripts/bench_v2.py --suite extraction --runs 20 --output-dir /tmp/sieve-bench
```

Each run writes `bench-v2.jsonl` with the harness version, Python/platform,
suite, source, run count, latency samples, median, p95, and raw suite metrics.
The suites cover retrieval latency/output footprint, structured field
accuracy, crawl recall/duplicates/bounds, search URL validity/relevance hooks,
and agent output/token estimates. Fixture accuracy is deterministic; replace
the fixture adapter with an annotated dataset before publishing a quality
claim.

Live probes are opt-in (`--live`) and are recorded as a separate provenance
field. They must report URL, command, package versions, HTTP/rendering mode,
failures, and raw output. Static and rendered paths are never combined in one
latency distribution. Network results are directional and belong in private
research until repeated runs and an independently inspectable fixture or
annotation set exist.

The terminal walkthrough is similarly local and recordable:

```bash
python demos/terminal_demo.py
```
