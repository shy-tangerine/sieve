#!/usr/bin/env python3
"""Search, then fetch the top hit. Contract: ranked JSON + content array."""
import json
import subprocess
import sys

PY = sys.executable


def run(*args):
    print("$ sieve", " ".join(args))
    r = subprocess.run([PY, "-m", "sieve", *args], capture_output=True, text=True, timeout=120)
    return json.loads(r.stdout)


hits = run("search", "python web scraping", "--max-results", "3")
assert isinstance(hits.get("results"), list) and hits["results"], hits
url = hits["results"][0]["url"]
print("top hit:", url)

page = run("fetch", url)
assert isinstance(page.get("content"), list) and any(c.strip() for c in page["content"]), page
print("chars:", sum(map(len, page["content"])))
print("OK")
