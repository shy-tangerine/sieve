"""Data-file and generator checks for sieve.adblock."""

from __future__ import annotations

import importlib.util
import json
import hashlib
from pathlib import Path

import pytest

from sieve import adblock

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "sieve" / "data" / "adblock_domains.txt"
SOURCE_DIR = ROOT / "scripts" / "data" / "blocklistproject"
MANIFEST = ROOT / "docs" / "adblock-data.json"


def _generator():
    spec = importlib.util.spec_from_file_location("generate_adblock_data", ROOT / "scripts" / "generate_adblock_data.py")
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_data_file_header_and_format():
    lines = DATA.read_text(encoding="utf-8").splitlines()
    metadata = json.loads(MANIFEST.read_text(encoding="utf-8"))
    source = metadata["source"]
    header = [x for x in lines if x.startswith("#")]
    body = [x for x in lines if x and not x.startswith("#")]
    assert any("Unlicense" in x for x in header)
    assert any("blocklistproject/Lists" in x for x in header)
    assert source["commit"] in lines[2]
    assert source["retrieved_on"] in lines[5]
    assert "tracking.txt header says MIT" in lines[3]
    assert metadata["output"]["sha256"] == hashlib.sha256(DATA.read_bytes()).hexdigest()
    assert source["license"]["sha256"] == hashlib.sha256((SOURCE_DIR / "LICENSE").read_bytes()).hexdigest()
    for name, record in source["inputs"].items():
        raw = (SOURCE_DIR / name).read_bytes()
        assert record["size_bytes"] == len(raw)
        assert record["sha256"] == hashlib.sha256(raw).hexdigest()
        assert record["url"].endswith(f"/{source['commit']}/{name}")
    assert body == sorted(set(body))
    assert all(x == x.lower() and "." in x and " " not in x for x in body)
    assert len(body) > 100_000
    assert adblock.AD_DOMAINS == frozenset(body)


def test_data_file_excludes_reviewed_destinations():
    gen = _generator()
    assert not any(gen.is_excluded(d) for d in adblock.AD_DOMAINS)
    assert gen.is_excluded("www.semrush.com") and not gen.is_excluded("notsemrush.com")


def test_parse_hosts_accepts_only_valid_sinks_and_hosts():
    gen = _generator()
    text = "\n".join(
        [
            "# comment",
            "0.0.0.0 Ads.Example.com",
            "127.0.0.1 tracker.example.net # trailing",
            "0.0.0.0 localhost",
            "0.0.0.0 nodots",
            "0.0.0.0 bad_ host.com",
            "192.168.1.1 router.example.org",
            "0.0.0.0 a.com b.com",
            "",
        ]
    )
    assert gen.parse_hosts(text) == {"ads.example.com", "tracker.example.net"}


def test_render_is_sorted_with_entry_count():
    gen = _generator()
    source = {
        "commit": "abc123",
        "retrieved_on": "2026-10-02",
        "license": {"sha256": "license-hash"},
        "inputs": {"ads.txt": {"sha256": "ads-hash"}, "tracking.txt": {"sha256": "tracking-hash"}},
    }
    out = gen.render({"b.example.com", "a.example.com"}, source)
    assert "Entries: 2" in out and "abc123" in out
    assert out.splitlines()[-2:] == ["a.example.com", "b.example.com"]


def test_pinned_sources_reproduce_committed_data_offline(monkeypatch):
    gen = _generator()

    def no_network(*args, **kwargs):
        pytest.fail("offline regeneration attempted a network request")

    monkeypatch.setattr(gen.urllib.request, "urlopen", no_network)
    assert gen.main(["--check"]) == 0


def test_check_rejects_output_drift_without_rewriting(tmp_path, monkeypatch):
    gen = _generator()
    output = tmp_path / "adblock_domains.txt"
    output.write_bytes(DATA.read_bytes() + b"tampered.example\n")
    monkeypatch.setattr(gen, "OUTPUT", output)
    assert gen.main(["--check"]) == 1
    assert output.read_bytes().endswith(b"tampered.example\n")


def test_import_does_not_load_the_list():
    adblock._load_domains.cache_clear()
    assert adblock._load_domains.cache_info().currsize == 0
