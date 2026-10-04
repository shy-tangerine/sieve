import json
import os
import stat
import textwrap

import pytest

from sieve.osint import OSINTError, run_maigret


def fake_maigret(tmp_path, body, *, version="Maigret v0.6.5", exit_code=0):
    path = tmp_path / "maigret"
    path.write_text(textwrap.dedent(f"""\
        #!/bin/sh
        if [ "$1" = "--version" ]; then echo '{version}'; exit 0; fi
        {body}
        exit {exit_code}
    """))
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return str(path)


def test_maigret_normalizes_ndjson_and_redacts(tmp_path):
    body = "printf '%s\\n' '" + json.dumps({"site": "GitHub", "url": "https://github.com/alice", "status": "claimed", "score": 1, "name": "Alice"}) + "'"
    result = run_maigret("alice", allow_osint=True, executable=fake_maigret(tmp_path, body))
    assert result["ok"] is True
    assert result["query"]["value"] == "<redacted>"
    assert result["results"][0]["confidence"] == 1.0
    assert result["provenance"]["upstream_version"] == "0.6.5"


def test_maigret_requires_opt_in_and_missing_binary():
    with pytest.raises(OSINTError, match="explicit"):
        run_maigret("alice")
    with pytest.raises(OSINTError) as exc:
        run_maigret("alice", allow_osint=True, executable="definitely-not-maigret")
    assert exc.value.category == "missing_executable"


def test_maigret_malformed_and_nonzero_fail_closed(tmp_path):
    bad = fake_maigret(tmp_path, "echo nope")
    with pytest.raises(OSINTError) as exc:
        run_maigret("alice", allow_osint=True, executable=bad)
    assert exc.value.category == "malformed_output"
    failed = fake_maigret(tmp_path, "echo '{}'", exit_code=2)
    with pytest.raises(OSINTError) as exc:
        run_maigret("alice", allow_osint=True, executable=failed)
    assert exc.value.category == "upstream_failure"


def test_maigret_output_limit_and_timeout(tmp_path):
    huge = fake_maigret(tmp_path, "head -c 2000 /dev/zero")
    with pytest.raises(OSINTError) as exc:
        run_maigret("alice", allow_osint=True, executable=huge, max_output_bytes=1000)
    assert exc.value.category == "output_limit"
    slow = fake_maigret(tmp_path, "sleep 2")
    with pytest.raises(OSINTError) as exc:
        run_maigret("alice", allow_osint=True, executable=slow, timeout=1)
    assert exc.value.category == "timeout"
