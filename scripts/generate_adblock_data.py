#!/usr/bin/env python3
"""Reproduce Sieve's ad/tracker list from checked-in source snapshots."""

from __future__ import annotations

import argparse
from datetime import UTC, datetime
import hashlib
import json
import re
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE_DIR = Path(__file__).resolve().parent / "data" / "blocklistproject"
MANIFEST = ROOT / "docs" / "adblock-data.json"
OUTPUT = ROOT / "sieve" / "data" / "adblock_domains.txt"
REPOSITORY = "https://github.com/blocklistproject/Lists"
RAW = "https://raw.githubusercontent.com/blocklistproject/Lists/{commit}/{name}"
PINNED_COMMIT = "ee3bdbaddf49c756cf3340bb2e6a9dee5282d1c6"
LISTS = ("ads.txt", "tracking.txt")

# Reviewed destination sites excluded because matching blocks by suffix.
EXCLUDE = frozenset({
    "ahrefs.com", "amap.com", "daum.net", "databricks.com", "eepurl.com",
    "feedburner.com", "gallup.com", "hubspot.net", "mailchi.mp", "markmonitor.com",
    "matomo.org", "mybluehost.me", "ozone.ru", "posthog.com", "powerbi.com",
    "salesforce.com", "samsungelectronics.com", "semrush.com", "sinaimg.cn",
    "stripe.network", "techtarget.com", "webmd.com", "websitewelcome.com",
})
SKIP_EXACT = frozenset({"analytics.kanban-service.de"})
HOST_SINKS = {"0.0.0.0", "127.0.0.1", "::1", "::"}
LABEL = re.compile(r"^[a-z0-9_]([a-z0-9_-]{0,61}[a-z0-9_])?$")


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def parse_hosts(text: str) -> set[str]:
    """Return valid lowercase domains from hosts-format text."""
    domains: set[str] = set()
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        parts = line.split()
        if len(parts) != 2 or parts[0] not in HOST_SINKS:
            continue
        host = parts[1].strip(".").lower()
        labels = host.split(".")
        if len(labels) < 2 or len(host) > 253 or not all(LABEL.match(x) for x in labels):
            continue
        if host not in {"localhost", "localhost.localdomain", "broadcasthost"}:
            domains.add(host)
    return domains


def is_excluded(host: str) -> bool:
    parts = host.split(".")
    return host in SKIP_EXACT or any(".".join(parts[i:]) in EXCLUDE for i in range(len(parts)))


def _fetch_snapshot(commit: str) -> dict[str, bytes]:
    if not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise ValueError("source pin must be a full 40-character Git commit SHA")
    result = {}
    for name in (*LISTS, "LICENSE"):
        url = RAW.format(commit=commit, name=name)
        request = urllib.request.Request(url, headers={"User-Agent": "Sieve source snapshot updater"})
        with urllib.request.urlopen(request, timeout=60) as response:  # noqa: S310 - fixed HTTPS host
            result[name] = response.read()
    license_text = result["LICENSE"].lower()
    if b"unlicense" not in license_text or b"public domain" not in license_text:
        raise ValueError("pinned source commit does not contain the approved Unlicense")
    for name, data in result.items():
        (SOURCE_DIR / name).write_bytes(data)
    return result


def _source_record(commit: str, retrieved_on: str) -> tuple[dict, dict[str, bytes]]:
    files = {name: (SOURCE_DIR / name).read_bytes() for name in (*LISTS, "LICENSE")}
    license_text = files["LICENSE"].lower()
    if b"unlicense" not in license_text or b"public domain" not in license_text:
        raise ValueError("pinned source license snapshot is not the approved Unlicense")
    source = {
        "repository": REPOSITORY,
        "commit": commit,
        "retrieved_on": retrieved_on,
        "license": {
            "name": "Unlicense",
            "url": RAW.format(commit=commit, name="LICENSE"),
            "size_bytes": len(files["LICENSE"]),
            "sha256": sha256(files["LICENSE"]),
        },
        "inputs": {
            name: {
                "url": RAW.format(commit=commit, name=name),
                "size_bytes": len(files[name]),
                "sha256": sha256(files[name]),
            }
            for name in LISTS
        },
    }
    return source, files


def render(domains: set[str], source: dict) -> str:
    """Render sorted domains with hashes and terms for the exact source inputs."""
    hashes = source["inputs"]
    header = (
        "# Sieve ad/tracker domain list (generated; do not edit).\n"
        f"# Source: The Block List Project, {REPOSITORY}\n"
        f"# Lists: ads.txt and tracking.txt at commit {source['commit']}\n"
        "# Repository license: Unlicense (public domain); tracking.txt header says MIT\n"
        f"# Terms evidence: LICENSE sha256={source['license']['sha256']}\n"
        f"# Retrieved: {source['retrieved_on']}\n"
        f"# Input sha256: ads.txt={hashes['ads.txt']['sha256']} tracking.txt={hashes['tracking.txt']['sha256']}\n"
        f"# Entries: {len(domains)}\n"
        "# Regenerate offline: python scripts/generate_adblock_data.py\n"
    )
    return header + "\n".join(sorted(domains)) + "\n"


def _build(source: dict, files: dict[str, bytes]) -> tuple[str, dict]:
    domains: set[str] = set()
    for name in LISTS:
        domains |= parse_hosts(files[name].decode("utf-8", "replace"))
    domains = {host for host in domains if not is_excluded(host)}
    if len(domains) < 100_000:
        raise ValueError(f"refusing to render only {len(domains)} hosts")
    text = render(domains, source)
    raw = text.encode("utf-8")
    metadata = {
        "schema_version": 1,
        "source": source,
        "output": {
            "path": "sieve/data/adblock_domains.txt",
            "entries": len(domains),
            "size_bytes": len(raw),
            "sha256": sha256(raw),
        },
    }
    return text, metadata


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="verify byte-for-byte output without writing")
    parser.add_argument("--fetch", metavar="GIT_SHA", help="refresh source snapshots from an explicit full commit SHA")
    args = parser.parse_args(argv)
    try:
        previous = json.loads(MANIFEST.read_text(encoding="utf-8")) if MANIFEST.exists() else None
        commit = args.fetch or (previous or {}).get("source", {}).get("commit", PINNED_COMMIT)
        retrieved_on = (previous or {}).get("source", {}).get(
            "retrieved_on", datetime.now(UTC).date().isoformat()
        )
        if args.fetch:
            if args.check:
                parser.error("--check cannot be combined with --fetch")
            _fetch_snapshot(commit)
            retrieved_on = datetime.now(UTC).date().isoformat()

        source, files = _source_record(commit, retrieved_on)
        if previous and not args.fetch and previous.get("source") != source:
            raise ValueError("pinned source snapshots changed; refresh them explicitly with --fetch <GIT_SHA>")
        for name in LISTS:
            recorded = source["inputs"][name]
            if recorded["url"] != RAW.format(commit=commit, name=name):
                raise ValueError(f"source URL is inconsistent for {name}")
        text, metadata = _build(source, files)
        expected_manifest = json.dumps(metadata, indent=2, sort_keys=True) + "\n"
        if args.check:
            if OUTPUT.read_text(encoding="utf-8") != text or MANIFEST.read_text(encoding="utf-8") != expected_manifest:
                print("adblock snapshots or generated output differ; regenerate and review", file=sys.stderr)
                return 1
            print(f"verified {metadata['output']['entries']} domains offline")
            return 0

        OUTPUT.write_text(text, encoding="utf-8")
        MANIFEST.write_text(expected_manifest, encoding="utf-8")
        print(f"wrote {metadata['output']['entries']} domains offline")
        return 0
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
        print(f"adblock data generation failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
