from pathlib import Path

from scripts.primitive_inventory import build_inventory, compare_inventory


def _scan(tmp_path: Path, source: str) -> dict:
    package = tmp_path / "sieve"
    package.mkdir()
    (package / "sample.py").write_text(source, encoding="utf-8")
    return build_inventory(tmp_path)


def test_resolves_import_aliases_and_known_clients(tmp_path):
    report = _scan(tmp_path, """
from urllib.request import urlopen as fetch_url
import subprocess as proc
import httpx as hx
from pathlib import Path as P

def load(url, target):
    response = fetch_url(url)
    client = hx.AsyncClient()
    client.get(url)
    proc.run(["tool"])
    path = P(target)
    path.write_text(response.read())
""")
    calls = {(call["category"], call["callee"]) for call in report["calls"]}
    assert calls == {
        ("network", "urllib.request.urlopen"),
        ("network", "httpx.AsyncClient.get"),
        ("subprocess", "subprocess.run"),
        ("persistent_write", "pathlib.Path.write_text"),
    }
    assert all(call["owner"] == "load" for call in report["calls"])
    assert all(call["bounds_review"].startswith("unknown") for call in report["calls"])
    assert all(call["secret_policy_review"].startswith("unknown") for call in report["calls"])


def test_ignores_unresolved_attributes_and_read_only_paths(tmp_path):
    report = _scan(tmp_path, """
from pathlib import Path

def inspect(url):
    path = Path("cache")
    response = path.read_text()
    url.urlopen()
    runner.run(["tool"])
    return response
""")
    assert report["calls"] == []


def test_open_only_counts_persistent_modes(tmp_path):
    report = _scan(tmp_path, """
from builtins import open as file_open

def read(path):
    with file_open(path, "r") as stream:
        return stream.read()

def save(path):
    with file_open(path, mode="w") as stream:
        stream.write("data")
""")
    calls = {(call["function"], call["category"], call["callee"]) for call in report["calls"]}
    assert calls == {
        ("save", "persistent_write", "builtins.open"),
        ("save", "persistent_write", "file.write"),
    }


def test_callsite_identity_ignores_line_changes(tmp_path):
    original = _scan(tmp_path, "from urllib.request import urlopen as get\ndef fetch(url):\n    get(url)\n")
    (tmp_path / "sieve" / "sample.py").write_text(
        "from urllib.request import urlopen as get\n\n\ndef fetch(url):\n    get(url)\n", encoding="utf-8"
    )
    shifted = build_inventory(tmp_path)
    assert original["calls"][0]["line"] != shifted["calls"][0]["line"]
    assert compare_inventory(shifted, original) == []


def test_added_sink_has_readable_diff_and_dynamic_call_limit_is_explicit(tmp_path):
    baseline = _scan(tmp_path, "def fetch(url):\n    return url\n")
    (tmp_path / "sieve" / "sample.py").write_text(
        "import httpx\ndef fetch(url):\n    return httpx.get(url)\n", encoding="utf-8"
    )
    current = build_inventory(tmp_path)
    errors = compare_inventory(current, baseline)
    assert len(errors) == 1
    assert "added sieve/sample.py:3 fetch [network] httpx.get" in errors[0]
    assert any("Dynamic dispatch" in item for item in current["limitations"])


def test_dynamic_calls_are_documented_as_outside_static_coverage(tmp_path):
    report = _scan(tmp_path, "def call(target, name, url):\n    return getattr(target, name)(url)\n")
    assert report["calls"] == []
    assert any("Dynamic dispatch" in item for item in report["limitations"])


def test_context_clients_inline_paths_and_path_open_modes(tmp_path):
    report = _scan(tmp_path, '''
import httpx as hx
from pathlib import Path as P
import asyncio as aio
async def perform(url):
    async with hx.AsyncClient() as client:
        await client.get(url)
    P("out").write_text("data")
    path = P("file")
    with path.open("w") as stream:
        stream.write("data")
    with path.open("r") as stream:
        stream.read()
    await aio.create_subprocess_exec("tool")
''')
    calls = [call["callee"] for call in report["calls"]]
    assert set(calls) == {"httpx.AsyncClient.get", "pathlib.Path.write_text", "pathlib.Path.open",
                          "file.write", "asyncio.create_subprocess_exec"}
    assert calls.count("pathlib.Path.open") == 1


def test_urllib_opener_open_is_a_network_sink(tmp_path):
    report = _scan(tmp_path, '''
import urllib.request as request
from urllib.request import build_opener as make_opener
from pathlib import Path

def via_module(url):
    opener = request.build_opener()
    opener.open(url)

def via_alias(url):
    opener = make_opener()
    opener.open(url)

def inline(url):
    request.build_opener().open(url)

def local_file(path):
    path = Path(path)
    with path.open("w") as stream:
        stream.write("data")
''')
    calls = {(call["function"], call["category"], call["callee"]) for call in report["calls"]}
    assert calls == {
        ("via_module", "network", "urllib.request.OpenerDirector.open"),
        ("via_alias", "network", "urllib.request.OpenerDirector.open"),
        ("inline", "network", "urllib.request.OpenerDirector.open"),
        ("local_file", "persistent_write", "pathlib.Path.open"),
        ("local_file", "persistent_write", "file.write"),
    }


def test_inventory_scope_changes_fail(tmp_path):
    report = _scan(tmp_path, "")
    baseline = {**report, "scope": {"package": "other"}}
    assert compare_inventory(report, baseline) == ["inventory scope changed"]
