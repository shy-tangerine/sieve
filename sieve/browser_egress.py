"""Private, authenticated browser egress with validated, pinned destinations.

Chromium sends all HTTP requests and HTTPS CONNECT tunnels through this local
proxy, so redirects, popups and cross-origin frames share the same boundary.
An optional operator proxy is chained without a direct fallback. No request
bodies, URLs or credentials are logged or persisted.
"""
from __future__ import annotations

import asyncio
import base64
import fnmatch
import hmac
import ipaddress
import secrets
import socket
import ssl
from urllib.parse import urlparse, urlunparse

from sieve.security import SecurityError, is_forbidden_ip, resolve_and_check, validate_url

MAX_HEADER_BYTES = 64 * 1024
MAX_TUNNEL_BYTES = 64 * 1024 * 1024


async def _public_destination(host: str, port: int) -> str:
    await asyncio.to_thread(resolve_and_check, host, port)
    addresses = await asyncio.to_thread(socket.getaddrinfo, host, port, type=socket.SOCK_STREAM)
    if not addresses or any(is_forbidden_ip(ipaddress.ip_address(info[4][0])) for info in addresses):
        raise SecurityError("browser destination is not public")
    # Connect using this checked literal address; never resolve the hostname a
    # second time at the transport or the upstream proxy.
    addresses.sort(key=lambda info: info[0] != socket.AF_INET)
    return str(addresses[0][4][0])


class BrowserEgress:
    def __init__(self, upstream: dict[str, str] | None = None):
        self.upstream = upstream
        self.password = secrets.token_urlsafe(32)
        self.authorization = "Basic " + base64.b64encode(f"sieve:{self.password}".encode()).decode()
        self.server: asyncio.Server | None = None
        self.tasks: set[asyncio.Task] = set()
        self.closed = False

    async def start(self) -> dict[str, str]:
        self.server = await asyncio.start_server(self._serve, "127.0.0.1", 0, limit=MAX_HEADER_BYTES)
        port = self.server.sockets[0].getsockname()[1]
        return {"server": f"http://127.0.0.1:{port}", "username": "sieve",
                "password": self.password, "bypass": "<-loopback>"}

    async def close(self) -> None:
        self.closed = True
        if self.server is not None:
            self.server.close()
        tasks = list(self.tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if self.server is not None:
            await self.server.wait_closed()
            self.server = None

    def _bypass(self, host: str) -> bool:
        for pattern in (self.upstream or {}).get("bypass", "").split(","):
            pattern = pattern.strip().lower()
            if pattern == "<local>" and "." not in host:
                return True
            if pattern and (fnmatch.fnmatchcase(host.lower(), pattern)
                            or pattern.startswith(".") and host.lower().endswith(pattern)):
                return True
        return False

    async def _connect(self, host: str, address: str, port: int, *, tunnel: bool = True):
        if self.upstream is None or self._bypass(host):
            return await asyncio.open_connection(address, port)
        proxy = urlparse(self.upstream["server"])
        reader, writer = await asyncio.open_connection(
            proxy.hostname, proxy.port,
            ssl=ssl.create_default_context() if proxy.scheme == "https" else None,
            limit=MAX_HEADER_BYTES,
        )
        try:
            username = self.upstream.get("username", "").encode()
            password = self.upstream.get("password", "").encode()
            ip = ipaddress.ip_address(address)
            if proxy.scheme in {"http", "https"}:
                if not tunnel:
                    return reader, writer
                authority = f"[{address}]:{port}" if ip.version == 6 else f"{address}:{port}"
                auth = ("Proxy-Authorization: Basic " + base64.b64encode(username + b":" + password).decode() + "\r\n") if username else ""
                writer.write(f"CONNECT {authority} HTTP/1.1\r\nHost: {authority}\r\n{auth}\r\n".encode())
                await writer.drain()
                response = await reader.readuntil(b"\r\n\r\n")
                if response.split(b" ", 2)[1] != b"200":
                    raise OSError("upstream proxy refused browser tunnel")
            elif proxy.scheme == "socks5":
                writer.write(b"\x05\x02\x00\x02" if username else b"\x05\x01\x00")
                await writer.drain()
                version, method = await reader.readexactly(2)
                if version != 5 or method not in ({0, 2} if username else {0}):
                    raise OSError("upstream SOCKS authentication refused")
                if method == 2:
                    if len(username) > 255 or len(password) > 255:
                        raise ValueError("SOCKS credentials exceed 255 bytes")
                    writer.write(bytes([1, len(username)]) + username + bytes([len(password)]) + password)
                    await writer.drain()
                    if await reader.readexactly(2) != b"\x01\x00":
                        raise OSError("upstream SOCKS authentication refused")
                writer.write(bytes([5, 1, 0, 1 if ip.version == 4 else 4]) + ip.packed + port.to_bytes(2, "big"))
                await writer.drain()
                reply = await reader.readexactly(4)
                if reply[0:2] != b"\x05\x00":
                    raise OSError("upstream SOCKS connection refused")
                size = {1: 4, 4: 16}.get(reply[3])
                if reply[3] == 3:
                    size = (await reader.readexactly(1))[0]
                if size is None:
                    raise OSError("invalid SOCKS response")
                await reader.readexactly(size + 2)
            elif proxy.scheme == "socks4":
                if ip.version != 4 or b"\x00" in username:
                    raise ValueError("SOCKS4 requires an IPv4 target and a valid user ID")
                writer.write(b"\x04\x01" + port.to_bytes(2, "big") + ip.packed + username + b"\x00")
                await writer.drain()
                if (await reader.readexactly(8))[1] != 90:
                    raise OSError("upstream SOCKS4 connection refused")
            else:
                raise ValueError("unsupported upstream proxy")
            return reader, writer
        except BaseException:
            # Rationale: failed/cancelled handshakes must close their socket;
            # the original exception still propagates without direct fallback.
            writer.close()
            raise

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.current_task()
        if task is None:
            raise RuntimeError("browser proxy requires an asyncio task")
        if self.closed or len(self.tasks) >= 128:
            writer.close()
            return
        self.tasks.add(task)
        upstream_writer = None
        connected = False
        try:
            async with asyncio.timeout(30):
                raw = await reader.readuntil(b"\r\n\r\n")
                lines = raw.decode("latin-1").split("\r\n")
                method, target, version = lines[0].split(" ")
                headers = [line for line in lines[1:] if line]
                auth = next((line.split(":", 1)[1].strip() for line in headers
                             if line.lower().startswith("proxy-authorization:")), "")
                if not hmac.compare_digest(auth, self.authorization):
                    writer.write(b'HTTP/1.1 407 Proxy Authentication Required\r\nProxy-Authenticate: Basic realm="Sieve"\r\nContent-Length: 0\r\nConnection: close\r\n\r\n')
                    await writer.drain()
                    return
                parsed = urlparse(validate_url("https://" + target if method == "CONNECT" else target))
                host = parsed.hostname
                if not host:
                    raise ValueError("browser destination requires a hostname")
                port = parsed.port or (443 if parsed.scheme == "https" else 80)
                address = await _public_destination(host, port)
                # The hostname is request-local; concurrent tunnels must never
                # share target-dependent routing state.
                forward = (method != "CONNECT" and self.upstream is not None
                           and not self._bypass(host)
                           and urlparse(self.upstream["server"]).scheme in {"http", "https"})
                upstream_reader, upstream_writer = await self._connect(host, address, port, tunnel=not forward)
                if method == "CONNECT":
                    writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
                    await writer.drain()
                else:
                    path = urlunparse(("", "", parsed.path or "/", parsed.params, parsed.query, ""))
                    headers = [line for line in headers if line.split(":", 1)[0].lower()
                               not in {"proxy-authorization", "proxy-connection", "connection"}]
                    if forward:
                        operator_proxy = self.upstream or {}
                        authority = f"[{address}]:{port}" if ":" in address else f"{address}:{port}"
                        path = f"http://{authority}{path}"
                        username = operator_proxy.get("username", "")
                        if username:
                            credentials = f"{username}:{operator_proxy.get('password', '')}"
                            headers.append("Proxy-Authorization: Basic " + base64.b64encode(credentials.encode()).decode())
                    upstream_writer.write((f"{method} {path} {version}\r\n" + "\r\n".join(headers)
                                           + "\r\nConnection: close\r\n\r\n").encode("latin-1"))
                    await upstream_writer.drain()
                connected = True

            remaining = MAX_TUNNEL_BYTES
            async def relay(source, destination):
                nonlocal remaining
                while remaining:
                    chunk = await asyncio.wait_for(source.read(min(65536, remaining)), timeout=30)
                    if not chunk:
                        return
                    chunk = chunk[:remaining]
                    remaining -= len(chunk)
                    destination.write(chunk)
                    await destination.drain()

            async with asyncio.timeout(300), asyncio.TaskGroup() as group:
                channels = {group.create_task(relay(reader, upstream_writer)),
                            group.create_task(relay(upstream_reader, writer))}
                _, pending = await asyncio.wait(channels, return_when=asyncio.FIRST_COMPLETED)
                for channel in pending:
                    channel.cancel()
        except Exception:
            # Rationale: the local proxy boundary converts invalid targets and
            # network errors to a fixed response, never exposing request secrets.
            if not connected:
                try:
                    writer.write(b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
                except OSError:
                    pass  # peer closed while its request was being rejected
        finally:
            if upstream_writer is not None:
                upstream_writer.close()
            writer.close()
            self.tasks.discard(task)
