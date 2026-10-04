#!/usr/bin/env python3
"""A deterministic, recordable Sieve walkthrough.

The demo uses example.com as a stable public fixture and only the installed
CLI. It needs network access, but no credentials or provider keys.
"""
from __future__ import annotations

import json
import argparse
import subprocess
import sys
import time
TARGET = "https://example.com"


def run(*args: str) -> dict:
    proc = subprocess.run([sys.executable, "-m", "sieve", *args], text=True, capture_output=True)
    try:
        value = json.loads(proc.stdout)
    except json.JSONDecodeError:
        raise RuntimeError("CLI did not produce JSON") from None
    if proc.returncode or value.get("error"):
        raise RuntimeError("CLI operation failed")
    return value


def compact(value: dict) -> str:
    if value.get("error"):
        return f"error: {value['error']}"
    if "content" in value:
        content = value["content"]
        text = " ".join(content) if isinstance(content, list) else str(content)
        return f"{value.get('url', '')}  {len(text)} chars  {text[:100]}"
    if "pages" in value:
        pages = value["pages"]
        return f"{len(pages)} pages  " + ", ".join(p.get("url", "") for p in pages)
    return json.dumps(value, ensure_ascii=False)[:180]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pause", type=float, default=0, help="seconds between recording steps (0–10)")
    pause = parser.parse_args().pause
    if not 0 <= pause <= 10:
        parser.error("--pause must be between 0 and 10 seconds")
    print("✦ SIEVE  /  one bounded research tool")
    print(f"fixture  {TARGET}  (stable public fixture)\n")
    for label, args in [
        ("① fetch", ("fetch", TARGET)),
        ("② extract", ("extract", TARGET, "--schema", '{"baseSelector":"body","fields":[{"name":"text","type":"text"}]}')),
        ("③ crawl", ("crawl", TARGET, "--max-pages", "3", "--max-depth", "1")),
    ]:
        print(label)
        result = run(*args)
        if label == "② extract" and not any(item.get("text") for item in result.get("items", [])):
            raise RuntimeError("Extraction returned no fixture records")
        print("  " + compact(result) + "\n")
        time.sleep(pause)
    print("ready  fetch → extract → crawl  |  all output remains JSON")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
