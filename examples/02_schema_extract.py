#!/usr/bin/env python3
"""Schema extraction → JSON. Contract: items list matching the schema."""
import json
import subprocess
import sys

PY = sys.executable
SCHEMA = {"baseSelector": "a", "fields": [{"name": "href", "selector": "a", "type": "attribute", "attribute": "href"}]}

print("$ sieve extract https://example.com --schema {...}")
r = subprocess.run([PY, "-m", "sieve", "extract", "https://example.com",
                    "--schema", json.dumps(SCHEMA)],
                   capture_output=True, text=True, timeout=120)
doc = json.loads(r.stdout)
assert isinstance(doc.get("items"), list) and doc["items"], doc
print("items:", len(doc["items"]))
print("OK")
