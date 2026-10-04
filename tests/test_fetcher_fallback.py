"""Regression tests for the chrome→firefox fallback condition in HTTPSession.get.

Revision-audit findings T4 / pass-1 bug #1: the fallback keep-condition was
`retry_status < 400 or retry_status not in (403, 429)` — a tautology for any
status ≥ 400 outside (403, 429), so a 500/404 fallback response replaced a
valid original. The fallback must win only when it actually succeeded.
"""

import pytest

import sieve.fetcher as fetcher_module
from sieve.fetcher import HTTPSession
from sieve.resource_budget import ResourceBudget, BudgetExceeded, budget_scope


class _StreamResponse:
    def __init__(self, body=b"ok", status=200, location=None):
        self.status_code = status
        self.headers = {"location": location} if location else {"content-type": "application/json"}
        self.url = "https://example.test/final"
        self.data = body
        self.read_sizes = []
        self.read_bytes = 0
        self.closed = 0

    @property
    def content(self):
        raise AssertionError("budgeted responses must not buffer content")

    def iter_bytes(self, size):
        self.read_sizes.append(size)
        for index in range(0, len(self.data), size):
            chunk = self.data[index:index + size]
            self.read_bytes += len(chunk)
            yield chunk

    def close(self):
        self.closed += 1


@pytest.fixture
def stream_primp(monkeypatch):
    queue, calls, clients = [], [], []

    class Client:
        def get(self, url, **kwargs):
            assert kwargs["stream"] is True and kwargs["follow_redirects"] is False
            calls.append((url, kwargs))
            return queue.pop(0)

    def factory(**kwargs):
        clients.append(kwargs)
        return Client()

    monkeypatch.setattr(fetcher_module.primp, "Client", factory)
    monkeypatch.setattr(fetcher_module, "validate_url", lambda url: url)
    monkeypatch.setattr(fetcher_module, "resolve_and_check", lambda *args: None)
    return queue, calls, clients


@pytest.mark.asyncio
async def test_budgeted_primp_streams_without_buffering_and_translation_debits_once(stream_primp):
    from sieve.response_translation import translate_response
    from sieve.server import ResponseModel

    queue, calls, _ = stream_primp
    native = _StreamResponse(b'{"ok":true}')
    queue.append(native)
    account = ResourceBudget(limits={"input_bytes": len(native.data)})
    with budget_scope(account):
        async with HTTPSession(stealthy_headers=False) as session:
            response = await session.get("https://example.test/")
        result, _ = translate_response(response, "text", None, False, False, "http", 1,
                                       ResponseModel, maximum_bytes=1024)
    assert result.content == ['{"ok":true}']
    assert account.consumed["input_bytes"] == len(native.data)
    assert native.closed == 1 and len(calls) == 1


@pytest.mark.asyncio
async def test_budgeted_primp_stops_at_remaining_bytes_plus_sentinel(stream_primp):
    queue, calls, _ = stream_primp
    native = _StreamResponse(b"x" * 1000)
    queue.append(native)
    account = ResourceBudget(limits={"input_bytes": 7})
    with budget_scope(account):
        async with HTTPSession(stealthy_headers=False, retries=3) as session:
            with pytest.raises(BudgetExceeded):
                await session.get("https://example.test/")
    assert native.read_sizes == [8] and native.read_bytes == 8
    assert native.closed == 1 and len(calls) == 1
    assert account.consumed["input_bytes"] == 7
    assert account.report()["truncated"] == ["input_bytes"]


@pytest.mark.asyncio
async def test_budgeted_primp_redirect_closes_stream_before_rejecting_target(stream_primp, monkeypatch):
    queue, calls, _ = stream_primp
    native = _StreamResponse(status=302, location="http://127.0.0.1/")
    queue.append(native)

    def validate(url):
        if "127.0.0.1" in url:
            raise ValueError("private target")
        return url

    monkeypatch.setattr(fetcher_module, "validate_url", validate)
    with budget_scope():
        async with HTTPSession(stealthy_headers=False, retries=0) as session:
            with pytest.raises(ValueError, match="private target"):
                await session.get("https://example.test/")
    assert native.closed == 1 and native.read_bytes == 0 and len(calls) == 1


@pytest.mark.asyncio
async def test_budgeted_primp_fallback_keeps_per_request_proxy_and_closes_discarded_response(stream_primp):
    queue, _, clients = stream_primp
    blocked, success = _StreamResponse(status=403), _StreamResponse(b"success")
    queue.extend([blocked, success])
    with budget_scope():
        async with HTTPSession(stealthy_headers=False) as session:
            result = await session.get("https://example.test/", proxy="http://proxy.example:8080")
    assert result.body == b"success"
    assert blocked.closed == success.closed == 1 and blocked.read_bytes == 0
    assert clients[-1]["impersonate"] == "firefox"
    assert clients[-1]["proxy"] == "http://proxy.example:8080"


@pytest.mark.asyncio
async def test_budgeted_primp_native_stream_against_local_fixture(monkeypatch):
    from http.server import BaseHTTPRequestHandler, HTTPServer
    from threading import Thread

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Length", "10")
            self.end_headers()
            self.wfile.write(b"0123456789")
        def log_message(self, *_):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=lambda: server.serve_forever(poll_interval=0.01), daemon=True)
    thread.start()
    # Only this transport-integration fixture permits loopback. Shared adapter
    # refusal tests separately exercise the actual URL and DNS guards.
    monkeypatch.setattr(fetcher_module, "validate_url", lambda url: url)
    monkeypatch.setattr(fetcher_module, "resolve_and_check", lambda *args: None)
    try:
        async with HTTPSession(stealthy_headers=False, retries=0) as session:
            with budget_scope(ResourceBudget(limits={"input_bytes": 10})) as account:
                result = await session.get(f"http://127.0.0.1:{server.server_port}/")
                assert result.body == b"0123456789" and account.consumed["input_bytes"] == 10
            with budget_scope(ResourceBudget(limits={"input_bytes": 3})) as account:
                with pytest.raises(BudgetExceeded):
                    await session.get(f"http://127.0.0.1:{server.server_port}/")
                assert account.consumed["input_bytes"] == 3
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=1)


class _FakeResponse:
    def __init__(self, status_code: int):
        self.status_code = status_code
        self.headers = {}


class _FakeClient:
    """Pops one queued status per get() call from a queue shared by all clients."""

    def __init__(self, statuses: list[int], identities: list[str | None]):
        self._statuses = statuses
        self._identities = identities
        self.impersonate_os = None

    def get(self, url, **kwargs):
        return _FakeResponse(self._statuses.pop(0))


@pytest.fixture
def fake_primp(monkeypatch):
    """Patch primp.Client with a factory sharing one status queue across all
    constructions (primary + fallback). Usage:

        queue = fake_primp([403, 500])
        ...
        assert fake_primp.identities == ["chrome", "firefox"]
    """
    holder = {"statuses": [], "identities": []}

    def factory(**kwargs):
        holder["identities"].append(kwargs.get("impersonate"))
        return _FakeClient(holder["statuses"], holder["identities"])

    monkeypatch.setattr(fetcher_module.primp, "Client", factory)
    # SSRF validation is monkeypatched out: these tests pin fallback logic,
    # not URL validation (covered by the security/redirect tests).
    monkeypatch.setattr(fetcher_module, "validate_url", lambda url, **kw: url)
    monkeypatch.setattr(fetcher_module, "resolve_and_check", lambda host, port=None: None)

    def setup(statuses: list[int]) -> None:
        holder["statuses"][:] = statuses

    factory.setup = setup
    factory.identities = holder["identities"]
    return factory


@pytest.mark.asyncio
async def test_fallback_worse_status_keeps_original(fake_primp):
    """A 500 from the firefox fallback must NOT replace the original 403."""
    fake_primp.setup([403, 500])

    async with HTTPSession(impersonate="chrome", stealthy_headers=False) as session:
        resp = await session.get("https://example.com/page", follow_redirects=False)

    assert resp.status == 403, (
        "firefox fallback returned 500; original 403 response must be kept"
    )
    assert fake_primp.identities == ["chrome", "firefox"], (
        "fallback branch did not run — test exercised nothing"
    )


@pytest.mark.asyncio
async def test_fallback_better_status_wins(fake_primp):
    """A 200 from the firefox fallback replaces the blocked original."""
    fake_primp.setup([403, 200])

    async with HTTPSession(impersonate="chrome", stealthy_headers=False) as session:
        resp = await session.get("https://example.com/page", follow_redirects=False)

    assert resp.status == 200, (
        "firefox fallback returned 200; it must replace the 403 original"
    )


@pytest.mark.asyncio
async def test_fallback_404_keeps_original(fake_primp):
    """The tautology case from the audit: 404 fallback must not replace 403."""
    fake_primp.setup([403, 404])

    async with HTTPSession(impersonate="chrome", stealthy_headers=False) as session:
        resp = await session.get("https://example.com/page", follow_redirects=False)

    assert resp.status == 403


@pytest.mark.asyncio
async def test_no_fallback_on_success(fake_primp):
    """A successful first response must never trigger the firefox fallback."""
    fake_primp.setup([200])

    async with HTTPSession(impersonate="chrome", stealthy_headers=False) as session:
        resp = await session.get("https://example.com/page", follow_redirects=False)

    assert resp.status == 200
    assert fake_primp.identities == ["chrome"], (
        f"unexpected client constructions: {fake_primp.identities}"
    )


@pytest.mark.asyncio
async def test_redirect_hops_go_through_ssrf_validator(monkeypatch):
    """T5: every redirect Location target must pass through validate_url.

    Mutation-verified contract (revision audit pass 2): replacing the manual
    redirect loop with follow_redirects=True leaves the suite green while
    silently reopening loopback SSRF. This test fails if any hop skips the
    validator.
    """
    calls: list[str] = []

    def recording_validate_url(url, **kwargs):
        calls.append(url)
        if "127.0.0.1" in url or "localhost" in url:
            raise ValueError(f"forbidden redirect target: {url}")
        return url

    monkeypatch.setattr(fetcher_module, "validate_url", recording_validate_url)
    monkeypatch.setattr(fetcher_module, "resolve_and_check", lambda host, port=None: None)

    class _Resp:
        def __init__(self, status, location=None):
            self.status_code = status
            self.headers = {"location": location} if location else {}

    class _Client:
        def __init__(self):
            self.impersonate_os = None
            self.n = 0

        def get(self, url, **kwargs):
            self.n += 1
            return _Resp(302, "http://127.0.0.1/steal")

    monkeypatch.setattr(fetcher_module.primp, "Client", lambda **kw: _Client())

    async with HTTPSession(impersonate="chrome", stealthy_headers=False) as session:
        with pytest.raises(ValueError, match="forbidden redirect target"):
            await session.get("https://evil.example.com/redir", follow_redirects=True)

    # The redirect hop itself was validated (not just the initial URL)
    assert any("127.0.0.1" in c for c in calls), (
        f"redirect hop bypassed SSRF validation; validate_url calls: {calls}"
    )
