#!/usr/bin/env python3
"""Public social snapshot (no login). Contract: normalized record shape."""
import json
import subprocess
import sys

PY = sys.executable
URL = "https://www.tiktok.com/@tiktok/video/7106591211972459782"

print("$ sieve social fetch", URL)
r = subprocess.run([PY, "-m", "sieve", "social", "fetch", URL],
                   capture_output=True, text=True, timeout=120)
doc = json.loads(r.stdout)
assert doc.get("platform") in ("tiktok", "instagram"), doc
assert "url" in doc and "provenance" in doc, doc
print("platform:", doc["platform"], "| ok:", doc.get("ok"))
print("OK")
