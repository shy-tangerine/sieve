"""The same arbitrary-URL corpus reaches actual HTTP adapter entry points."""
import email.message
from types import SimpleNamespace

import pytest
import sieve.fetcher as fetcher
from sieve.domain_discovery import _bounded_http_fetch
from sieve.security import SecurityError

PRIVATE = ["http://127.0.0.1/x", "http://10.0.0.7/x", "http://169.254.169.254/x",
           "http://[::1]/x", "http://dns-private.example/x"]


@pytest.mark.asyncio
@pytest.mark.parametrize("adapter", ["httpsession", "bounded"])
@pytest.mark.parametrize("target", PRIVATE)
@pytest.mark.parametrize("as_redirect", [False, True])
async def test_http_adapters_never_open_private_targets(monkeypatch, adapter, target, as_redirect):
    def dns(host, port, **kwargs):
        address = "10.0.0.7" if host == "dns-private.example" else "93.184.216.34"
        return [(2, 1, 6, "", (address, port))]

    monkeypatch.setattr("sieve.security.socket.getaddrinfo", dns)
    start = "https://public.example/start" if as_redirect else target
    requested = []
    location = target if as_redirect else None

    class Client:
        def get(self, url, **kwargs):
            requested.append(url)
            return SimpleNamespace(status_code=302 if location else 200,
                                   headers={"location": location} if location else {})

        def close(self):
            pass

    class Response:
        status = 301 if location else 200
        headers = email.message.Message()
        if location:
            headers["Location"] = location

        def read(self, size):
            return b"ok"[:size]

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    if adapter == "httpsession":
        monkeypatch.setattr(fetcher.primp, "Client", lambda **kwargs: Client())
        async with fetcher.HTTPSession(impersonate="chrome", stealthy_headers=False) as session:
            with pytest.raises(SecurityError):
                await session.get(start, retries=0, follow_redirects=True)
    else:
        def open_fn(request):
            requested.append(request.full_url)
            return Response()
        assert _bounded_http_fetch(open_fn, start) is None
    assert requested == (["https://public.example/start"] if as_redirect else [])
