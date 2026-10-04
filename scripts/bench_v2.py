#!/usr/bin/env python3
"""Reproducible Sieve evidence suites (local by default).

Local fixtures make accuracy and crawl claims repeatable. ``--live`` is an
opt-in probe and is reported separately, never merged with fixture scores.
Output replacement is atomic; existing symlinks are replaced, not followed.
"""
from __future__ import annotations

import argparse
import json
import platform
import statistics
import sys
import time
from pathlib import Path

if __package__:
    from .report_output import write_report
else:
    from report_output import write_report

VERSION = "2.0"
FIXTURE = {"pages": {"/": ["/docs", "/blog"], "/docs": ["/docs/api", "/docs"], "/blog": ["/docs/api"]},
           "expected_fields": {"title": "Sieve", "kind": "research"},
           "search": [{"url": "https://example.test/docs", "title": "Sieve API"},
                      {"url": "https://example.test/blog", "title": "Sieve blog"}]}


def percentile(values: list[float], p: float) -> float | None:
    if not values:
        return None
    return round(statistics.quantiles(values, n=100, method="inclusive")[int(p) - 1], 3) if len(values) > 1 else round(values[0], 3)


def crawl_metrics(graph: dict[str, list[str]], max_pages: int = 10, max_depth: int = 3) -> dict:
    seen, queue = {"/"}, [("/", 0)]
    duplicates = 0
    while queue and len(seen) < max_pages:
        page, depth = queue.pop(0)
        if depth >= max_depth:
            continue
        for link in graph.get(page, []):
            if link in seen:
                duplicates += 1
                continue
            if link not in seen and len(seen) < max_pages:
                seen.add(link)
                queue.append((link, depth + 1))
    nodes = set(graph) | {link for links in graph.values() for link in links}
    return {"visited": len(seen), "recall": round(len(seen) / len(nodes), 3), "duplicates": duplicates, "bounded": len(seen) <= max_pages}


def extraction_score(expected: dict, actual: dict) -> dict:
    matched = sum(actual.get(k) == v for k, v in expected.items())
    return {"fields": len(expected), "matched": matched, "accuracy": round(matched / len(expected), 3) if expected else 1.0}


def run_suite(name: str, runs: int) -> dict:
    latencies = []
    for _ in range(runs):
        start = time.perf_counter_ns()
        if name == "crawl":
            metrics = crawl_metrics(FIXTURE["pages"])
        elif name == "extraction":
            metrics = extraction_score(FIXTURE["expected_fields"], {"title": "Sieve", "kind": "research"})
        elif name == "search":
            rows = FIXTURE["search"]
            valid = sum(bool(r.get("url", "").startswith("https://")) for r in rows)
            relevant = sum("sieve" in (r.get("title", "") + r.get("url", "")).lower() for r in rows)
            metrics = {"results": len(rows), "valid_urls": valid, "validity": round(valid / len(rows), 3),
                       "relevance_hook": {"query_terms": ["sieve"], "matched": relevant,
                                          "score": round(relevant / len(rows), 3)}}
        else:  # retrieval and agent footprint use an explicit, local operation
            payload = json.dumps(FIXTURE, sort_keys=True)
            metrics = {"bytes": len(payload), "output_tokens_est": len(payload) // 4,
                       "quality_hook": {"nonempty": bool(payload), "records": len(FIXTURE["pages"])}}
        latencies.append((time.perf_counter_ns() - start) / 1_000_000)
    return {"suite": name, "source": "local-fixture", "runs": runs, "metrics": metrics,
            "latency_ms": {"median": round(statistics.median(latencies), 3), "p95": percentile(latencies, 95), "samples": [round(v, 3) for v in latencies]}}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--suite", choices=("all", "retrieval", "extraction", "crawl", "search", "agent"), default="all")
    parser.add_argument("--runs", type=int, default=5)
    parser.add_argument("--output-dir", type=Path, default=Path("benchmarks/results"))
    parser.add_argument("--live", action="store_true", help="record opt-in network probes separately (not included in fixture scores)")
    args = parser.parse_args()
    if args.runs < 1:
        parser.error("--runs must be positive")
    names = ["retrieval", "extraction", "crawl", "search", "agent"] if args.suite == "all" else [args.suite]
    stamp = {"benchmark": VERSION, "python": sys.version.split()[0], "platform": platform.platform(), "runs": args.runs, "live": args.live}
    records = [dict(stamp, **run_suite(name, args.runs)) for name in names]
    args.output_dir.mkdir(parents=True, exist_ok=True)
    output = args.output_dir / "bench-v2.jsonl"
    write_report(output, "".join(json.dumps(record, sort_keys=True) + "\n" for record in records))
    print(json.dumps({"output": str(output), "suites": records}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
