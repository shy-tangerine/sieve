#!/usr/bin/env python3
"""Crawl a bounded site, then fetch a discovered page."""
import json
import subprocess
import sys

PY = sys.executable


def run(*args):
    print("$ sieve", " ".join(args))
    r = subprocess.run([PY, "-m", "sieve", *args], capture_output=True, text=True, timeout=180)
    return json.loads(r.stdout)


found = run("crawl", "https://example.com", "--max-pages", "5")
assert isinstance(found.get("pages"), list) and found["pages"], found
print("crawled:", found.get("pages_crawled"))

if found["pages"]:
    page = run("fetch", found["pages"][0]["url"])
    assert isinstance(page.get("content"), list), page
print("OK")
