"""Offline browser request/deadline regressions and a local Chromium fixture."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from sieve import browser


class FakeCDP:
    def __init__(self):
        self.commands = []
        self.listeners = {}

    def on(self, event, callback):
        self.listeners[event] = callback

    def remove_listener(self, event, callback):
        self.listeners.pop(event, None)

    async def send(self, command, params=None):
        self.commands.append((command, params))

    async def detach(self):
        self.commands.append(("detach", None))


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["GET", "CONNECT"])
async def test_egress_blocks_private_targets_before_connection(method, monkeypatch):
    from sieve.browser_egress import BrowserEgress
    proxy = BrowserEgress()
    proxy._connect = AsyncMock(side_effect=AssertionError("private target connected"))
    config = await proxy.start()
    port = int(config["server"].rsplit(":", 1)[1])
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        target = "127.0.0.1:80" if method == "CONNECT" else "http://127.0.0.1/private"
        writer.write(f"{method} {target} HTTP/1.1\r\nProxy-Authorization: {proxy.authorization}\r\n\r\n".encode())
        await writer.drain()
        assert (await reader.read()).startswith(b"HTTP/1.1 403")
        proxy._connect.assert_not_called()
        writer.close()
        await writer.wait_closed()
    finally:
        await proxy.close()


@pytest.mark.asyncio
async def test_egress_requires_authentication_and_closes_idle_clients():
    from sieve.browser_egress import BrowserEgress
    proxy = BrowserEgress()
    config = await proxy.start()
    reader, writer = await asyncio.open_connection("127.0.0.1", int(config["server"].rsplit(":", 1)[1]))
    writer.write(b"GET http://example.test/ HTTP/1.1\r\n\r\n")
    await writer.drain()
    assert (await reader.read()).startswith(b"HTTP/1.1 407")
    writer.close()
    await writer.wait_closed()
    reader, writer = await asyncio.open_connection("127.0.0.1", int(config["server"].rsplit(":", 1)[1]))
    writer.write(b"GET ")
    await writer.drain()
    await asyncio.sleep(0)
    await proxy.close()
    assert await asyncio.wait_for(reader.read(), 1) == b""
    assert not proxy.tasks
    writer.close()
    await writer.wait_closed()


def fetch_fixture(monkeypatch):
    page = Mock()
    page.unroute_all = AsyncMock()
    page.set_extra_http_headers = AsyncMock()
    page.goto = AsyncMock()
    page.context = SimpleNamespace(new_cdp_session=AsyncMock(return_value=FakeCDP()))
    session = browser.DynamicBrowser(retries=1, wait=0)
    session._is_alive = True
    session._init_script = None
    session._acquire_page = AsyncMock(return_value=page)
    session._release_page = AsyncMock()
    session._wait_for_stability = AsyncMock()
    monkeypatch.setattr(browser, "resolve_and_check", lambda *args: None)
    return session, page


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [TimeoutError("navigation timeout"), RuntimeError("Target closed")])
async def test_navigation_errors_do_not_trigger_commit_compatibility_retry(monkeypatch, failure):
    session, page = fetch_fixture(monkeypatch)
    page.goto.side_effect = failure
    with pytest.raises(type(failure)):
        await session.fetch("https://example.test", timeout=1000)
    assert page.goto.await_count == 1
    session._release_page.assert_awaited_once_with(page, discard=True)


@pytest.mark.asyncio
async def test_only_unsupported_commit_uses_compatibility_navigation(monkeypatch):
    from sieve import fetcher
    session, page = fetch_fixture(monkeypatch)
    response = SimpleNamespace()
    page.goto.side_effect = [ValueError("wait_until: unsupported commit"), response, response]
    monkeypatch.setattr(fetcher, "response_from_browser_page", AsyncMock(return_value=response))
    assert await session.fetch("https://example.test", timeout=1000) is response
    assert [call.kwargs.get("wait_until") for call in page.goto.await_args_list] == ["commit", None, "commit"]
    assert page.goto.await_args_list[-1].args == ("about:blank",)


@pytest.mark.asyncio
async def test_shared_deadline_cancels_setup_and_releases_guard_and_tab(monkeypatch):
    session, page = fetch_fixture(monkeypatch)
    entered = asyncio.Event()
    cancelled = asyncio.Event()
    async def setup(page):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()
    with pytest.raises(TimeoutError):
        await session.fetch("https://example.test", timeout=50, page_setup=setup)
    assert entered.is_set() and cancelled.is_set()
    assert page.goto.await_count == 0
    session._release_page.assert_awaited_once_with(page, discard=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("driver_module", ["patchright.async_api", "playwright.async_api"])
async def test_chromium_redirect_subresource_and_popup_policy(monkeypatch, driver_module):
    """Only fixture hostname DNS is allowed; private URLs keep real validation."""
    patchright = pytest.importorskip(driver_module)
    calls = []
    async def serve(reader, writer):
        try:
            request = await reader.readuntil(b"\r\n\r\n")
            path = request.split(b" ")[1].decode()
            calls.append(path)
            if path == "/redirect":
                body = b""
                headers = f"HTTP/1.1 302 Found\r\nLocation: http://127.0.0.1:{port}/private\r\n"
            elif path == "/safe-redirect":
                body = b""
                headers = "HTTP/1.1 302 Found\r\nLocation: /article\r\n"
            elif path == "/safe.js":
                body = b"window.fixtureLoaded = true;"
                headers = "HTTP/1.1 200 OK\r\nContent-Type: application/javascript\r\n"
            elif path == "/worker.js":
                body = (f'self.addEventListener("install", event => event.waitUntil('
                        f'fetch("http://127.0.0.1:{port}/private-worker").catch(() => {{}})));').encode()
                headers = "HTTP/1.1 200 OK\r\nContent-Type: application/javascript\r\n"
            elif path == "/frame":
                body = (f'<html><body><img src="http://127.0.0.1:{port}/private-frame">'
                        f'<script>window.open("http://127.0.0.1:{port}/frame-popup");</script>'
                        '<p>frame fixture</p></body></html>').encode()
                headers = "HTTP/1.1 200 OK\r\nContent-Type: text/html\r\n"
            else:
                body = (f'<html><body><script src="http://127.0.0.1:{port}/private-script"></script>'
                        '<script src="/safe.js"></script>'
                        f'<script>window.open("http://127.0.0.1:{port}/private-popup");</script>'
                        f'<iframe src="http://frame.other.test:{port}/frame"></iframe>'
                        '<p>safe fixture</p></body></html>').encode()
                headers = "HTTP/1.1 200 OK\r\nContent-Type: text/html\r\n"
            writer.write((headers + f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n").encode() + body)
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()
    server = await asyncio.start_server(serve, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    from sieve import browser_egress
    destination = browser_egress._public_destination
    async def fixture_destination(host, port):
        if host in {"localhost", "frame.other.test"}:
            return "127.0.0.1"
        return await destination(host, port)
    monkeypatch.setattr(browser_egress, "_public_destination", fixture_destination)
    original_validate = browser_egress.validate_url
    denied = []
    def fixture_validate(url):
        if url.startswith(f"http://localhost:{port}/"):
            return url
        try:
            return original_validate(url)
        except ValueError:
            denied.append(url)
            raise
    monkeypatch.setattr(browser_egress, "validate_url", fixture_validate)
    proxy = browser_egress.BrowserEgress()
    proxy_config = await proxy.start()
    try:
        async with patchright.async_playwright() as driver:
            try:
                engine = await driver.chromium.launch(headless=True, args=["--disable-popup-blocking"])
            except Exception as exc:
                if "Executable doesn't exist" in str(exc):
                    pytest.skip("install Chromium for local browser policy integration")
                raise
            try:
                context = await engine.new_context(proxy=proxy_config)
                page = await context.new_page()
                response = await page.goto(f"http://localhost:{port}/redirect", timeout=5000)
                assert response.status == 403
                await page.goto(f"http://localhost:{port}/safe-redirect", wait_until="load", timeout=5000)
                assert page.url == f"http://localhost:{port}/article"
                async def evaluate(expression):
                    options = {"isolated_context": False} if driver_module.startswith("patchright") else {}
                    return await page.evaluate(expression, **options)
                assert await evaluate("window.fixtureLoaded")
                assert await evaluate("window.isSecureContext && !!navigator.serviceWorker")
                await page.add_script_tag(content="window.workerReady = navigator.serviceWorker.register('/worker.js').then(() => navigator.serviceWorker.ready)")
                await asyncio.wait_for(evaluate("window.workerReady"), 5)
                assert await page.locator("p").inner_text() == "safe fixture"
                assert calls[:3] == ["/redirect", "/safe-redirect", "/article"]
                assert sorted(calls[3:]) == ["/frame", "/safe.js", "/worker.js"]
                assert any(url.endswith("/private-worker") for url in denied)
                assert any(url.endswith("/private-frame") for url in denied)
                assert len(context.pages) > 1  # popup exists, but its private target never connected
            finally:
                await engine.close()
    finally:
        await proxy.close()
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
@pytest.mark.parametrize("scheme", ["http", "socks4", "socks5"])
async def test_upstream_proxy_uses_pinned_ip_and_preserves_authentication(scheme):
    from sieve.browser_egress import BrowserEgress
    observed = []
    async def serve(reader, writer):
        try:
            if scheme == "http":
                request = await reader.readuntil(b"\r\n\r\n")
                observed.append(request)
                writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            elif scheme == "socks4":
                observed.append(await reader.readexactly(8))
                observed.append(await reader.readuntil(b"\x00"))
                writer.write(b"\x00\x5a" + b"\x00" * 6)
            else:
                assert await reader.readexactly(4) == b"\x05\x02\x00\x02"
                writer.write(b"\x05\x02")
                await writer.drain()
                assert await reader.readexactly(2) == b"\x01\x07"
                observed.append(await reader.readexactly(7))
                size = (await reader.readexactly(1))[0]
                observed.append(await reader.readexactly(size))
                writer.write(b"\x01\x00")
                await writer.drain()
                observed.append(await reader.readexactly(10))
                writer.write(b"\x05\x00\x00\x01" + b"\x00" * 6)
            writer.write(b"opaque tunnel bytes")
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()
    server = await asyncio.start_server(serve, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    proxy = BrowserEgress({"server": f"{scheme}://127.0.0.1:{port}",
                           "username": "fixture", "password": "dummy"})
    try:
        reader, writer = await proxy._connect("original.example.test", "93.184.216.34", 443)
        assert await reader.read() == b"opaque tunnel bytes"
        writer.close()
        await writer.wait_closed()
        if scheme == "http":
            assert b"CONNECT 93.184.216.34:443" in observed[0]
            assert b"Proxy-Authorization: Basic Zml4dHVyZTpkdW1teQ==" in observed[0]
        elif scheme == "socks4":
            assert observed[0][-4:] == bytes([93, 184, 216, 34])
            assert observed[1] == b"fixture\x00"
        else:
            assert observed[:2] == [b"fixture", b"dummy"]
            assert observed[2][4:8] == bytes([93, 184, 216, 34])
    finally:
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
async def test_http_upstream_forwarding_pins_target_without_connect(monkeypatch):
    from sieve import browser_egress
    observed = []
    async def serve(reader, writer):
        try:
            request = await reader.readuntil(b"\r\n\r\n")
            observed.append(request)
            status = b"403 Forbidden" if request.startswith(b"CONNECT") else b"200 OK"
            writer.write(b"HTTP/1.1 " + status + b"\r\nContent-Length: 2\r\nConnection: close\r\n\r\nok")
            await writer.drain()
        finally:
            writer.close()
    upstream = await asyncio.start_server(serve, "127.0.0.1", 0)
    async def destination(host, port):
        assert host == "example.test" and port == 80
        return "93.184.216.34"
    monkeypatch.setattr(browser_egress, "_public_destination", destination)
    proxy = browser_egress.BrowserEgress({"server": f"http://127.0.0.1:{upstream.sockets[0].getsockname()[1]}",
                                        "username": "fixture", "password": "dummy"})
    config = await proxy.start()
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", int(config["server"].rsplit(":", 1)[1]))
        writer.write(f"GET http://example.test/path?q=1 HTTP/1.1\r\nHost: example.test\r\nProxy-Authorization: {proxy.authorization}\r\n\r\n".encode())
        await writer.drain()
        assert b"200 OK" in await reader.read()
        assert observed[0].startswith(b"GET http://93.184.216.34:80/path?q=1 HTTP/1.1")
        assert b"Host: example.test" in observed[0]
        assert b"Proxy-Authorization: Basic Zml4dHVyZTpkdW1teQ==" in observed[0]
        assert proxy.authorization.encode() not in observed[0]
        writer.close()
        await writer.wait_closed()
    finally:
        await proxy.close()
        upstream.close()
        await upstream.wait_closed()


@pytest.mark.asyncio
async def test_destination_revalidates_dns_and_returns_literal(monkeypatch):
    import socket
    from sieve import browser_egress
    monkeypatch.setattr(browser_egress, "resolve_and_check", lambda *_: "93.184.216.34")
    addresses = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))]
    monkeypatch.setattr(browser_egress.socket, "getaddrinfo", lambda *a, **k: list(addresses))
    assert await browser_egress._public_destination("example.test", 443) == "93.184.216.34"
    addresses.append((socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443)))
    with pytest.raises(ValueError, match="not public"):
        await browser_egress._public_destination("example.test", 443)
