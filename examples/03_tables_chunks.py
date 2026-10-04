#!/usr/bin/env python3
"""Tables + RAG chunking. Contract: tables[] and chunks[] present."""
import json
import subprocess
import sys

PY = sys.executable
URL = "https://en.wikipedia.org/wiki/Web_scraping"


def run(*args):
    print("$ sieve", " ".join(args))
    r = subprocess.run([PY, "-m", "sieve", *args], capture_output=True, text=True, timeout=120)
    return json.loads(r.stdout)


tables = run("extract", URL, "--tables")
assert isinstance(tables.get("tables"), list), tables
print("tables:", len(tables["tables"]))

chunked = run("extract", URL, "--chunk", "regex")
assert isinstance(chunked.get("chunks"), list) and chunked["chunks"], chunked
print("chunks:", len(chunked["chunks"]))
print("OK")
