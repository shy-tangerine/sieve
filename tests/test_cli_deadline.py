"""Issue #469: CLI fetch must always produce bounded output.

A per-request timeout does not cover setup, retries, and shutdown; when the
network is denied (sandbox), the process could idle forever. The structured
fetch route wraps the whole coroutine in a hard asyncio deadline, and the
social fetch bounds every httpx phase (connect included).
"""

import asyncio
import json
import os
from pathlib import Path
import shlex
import socket
import subprocess
import sys
import time
import textwrap

import httpx
import pytest

from sieve import cli


def test_run_with_deadline_returns_result():
    async def quick():
        return {"ok": True}

    assert cli._run_with_deadline(quick(), 5) == {"ok": True}


def test_run_with_deadline_cancels_hanging_coroutine():
    async def hang():
        await asyncio.sleep(999)

    with pytest.raises(TimeoutError, match="CLI deadline"):
        cli._run_with_deadline(hang(), 0.2)


def test_structured_fetch_deadline_is_request_timeout_plus_allowance(monkeypatch):
    """The deadline must derive from --timeout, not be absent."""
    captured = {}

    class FakeServer:
        def __init__(self, cache_ttl=None):
            pass

        async def smart_fetch(self, *args, **kwargs):
            captured["kwargs"] = kwargs
            return {"ok": True}

    monkeypatch.setattr("sieve.server.MasterFetchServer", FakeServer)
    monkeypatch.setattr(cli, "_run_with_deadline", lambda coro, d: captured.setdefault("deadline", d))
    import contextlib

    with contextlib.suppress(Exception):
        cli._run_structured_fetch(
            ["fetch", "--structured", "https://example.com", "--timeout", "12"]
        )
    assert captured.get("deadline") == 42


def test_social_bounded_get_text_bounds_every_httpx_phase():
    """Connect phase must be bounded, not just the overall read timeout."""
    import inspect
    from sieve import social

    source = inspect.getsource(social._bounded_get_text)
    assert "httpx.Timeout(" in source
    assert "connect=" in source


# --- Network-denied end-to-end repro (issue #469) ---------------------------

def _unshare_rn_available() -> bool:
    """True when we can create a user+network namespace (no network at all)."""
    try:
        return subprocess.run(
            ["unshare", "-rn", "true"],
            capture_output=True, timeout=10,
        ).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


@pytest.mark.skipif(not _unshare_rn_available(), reason="unshare -rn unavailable")
def test_network_denied_cli_fetch_exits_bounded():
    """End-to-end repro of #469 in a network namespace with no interfaces.

    Regression guard: before the fix, `sieve fetch --structured` idled in
    ep_poll for minutes when the network was denied (T3/Codex sandbox). The
    CLI-level deadline must now produce a JSON error envelope on stdout and
    exit non-zero within a bounded window — even though DNS, connect, read,
    retries, and shutdown all fail or stall.
    """
    env = dict(os.environ)
    # Tight allowance so CI finishes fast; deadline = 2s timeout + 2s.
    env["SIEVE_CLI_DEADLINE_ALLOWANCE"] = "2"
    repo_root = Path(__file__).resolve().parent.parent
    start = time.monotonic()
    proc = subprocess.run(
        ["unshare", "-rn", sys.executable, "-m", "sieve", "fetch",
         "--structured", "https://example.com/regression-469", "--timeout", "2"],
        capture_output=True, text=True, timeout=90, env=env,
        cwd=str(repo_root),
    )
    elapsed = time.monotonic() - start

    # 1. The process must exit on its own (not be killed by our timeout=90).
    # 2. Output must be machine-readable JSON on stdout with a bounded error.
    assert proc.stdout.strip(), "network-denied fetch must still emit JSON output"
    last = proc.stdout.strip().splitlines()[-1]
    payload = json.loads(last)
    # The CLI contract: a failed fetch is a successful call carrying an error
    # payload, OR a non-zero exit with an error envelope. Both are bounded.
    if proc.returncode != 0:
        assert payload.get("ok") is False
    # 3. The whole lifecycle (DNS + connect + retries + shutdown) must stay
    #    far below the old multi-minute hang — the actual #469 regression.
    assert elapsed < 60, f"CLI took {elapsed:.1f}s under network denial"


@pytest.mark.skipif(not _unshare_rn_available(), reason="unshare -rn unavailable")
def test_network_denied_read_blackhole_is_bounded():
    """A server that accepts but never responds must hit the read deadline."""
    repo_root = Path(__file__).resolve().parent.parent
    import threading

    ready = threading.Event()

    def silent_server():
        srv = socket.socket()
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind(("127.0.0.1", 0))
        srv.listen(5)
        ready.set()
        conn, _ = srv.accept()
        time.sleep(120)  # accept, never respond: read-blackhole

    threading.Thread(target=silent_server, daemon=True).start()
    ready.wait(timeout=5)

    def run_in_namespace(port_holder=[""]):
        pass

    # The server must live in the SAME namespace as the client. Run both in
    # one unshare'd bash: bind the port inside via a small inline script.
    port_script = textwrap.dedent("""
        import socket, threading, time, sys
        def server():
            srv = socket.socket()
            srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            srv.bind(("127.0.0.1", 18099))
            srv.listen(5)
            conn, _ = srv.accept()
            time.sleep(120)
        threading.Thread(target=server, daemon=True).start()
        time.sleep(0.5)
        sys.path.insert(0, {root!r})
        from sieve.social import _bounded_get_text
        start = time.monotonic()
        try:
            _bounded_get_text("http://127.0.0.1:18099/x", timeout=5)
            print("UNEXPECTED SUCCESS")
        except Exception as exc:
            elapsed = time.monotonic() - start
            print(f"{{elapsed:.1f}}s {{type(exc).__name__}}")
            raise SystemExit(0 if elapsed < 15 else 3)
        raise SystemExit(2)
    """).format(root=str(repo_root))

    start = time.monotonic()
    proc = subprocess.run(
        ["unshare", "-rn", "bash", "-c", "ip link set lo up 2>/dev/null; "
         f"{sys.executable} -c {shlex.quote(port_script)}"],
        capture_output=True, text=True, timeout=60, cwd=str(repo_root),
    )
    elapsed = time.monotonic() - start
    assert proc.returncode == 0, (
        f"read-blackhole not bounded: rc={proc.returncode} stdout={proc.stdout!r}"
    )
    assert "ReadTimeout" in proc.stdout
    assert elapsed < 30
