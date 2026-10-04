from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from types import SimpleNamespace

import pytest
from httpcore._sync.http2 import HTTP2Connection

from sieve import search_metasearch as search


def test_concurrent_metasearch_requests_leave_unrelated_http2_hook_unchanged(monkeypatch):
    original = HTTP2Connection._send_connection_init
    barrier = Barrier(3, timeout=5)

    class Client:
        def request(self, *args, **kwargs):
            barrier.wait()
            assert HTTP2Connection._send_connection_init is original
            barrier.wait()
            return SimpleNamespace(status_code=200, content=b"fixture", text="fixture")

    monkeypatch.setattr(search.httpx, "Client", lambda **kwargs: Client())
    clients = [search._HttpxClient(verify=False) for _ in range(2)]
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(client.request, "GET", "https://example.com") for client in clients]
        barrier.wait()
        assert HTTP2Connection._send_connection_init is original
        barrier.wait()
        assert [future.result(timeout=5).text for future in futures] == ["fixture", "fixture"]
    assert HTTP2Connection._send_connection_init is original


@pytest.mark.parametrize("message,error", [("fixture failure", search.MetaSearchException), ("timed out", search.MetaTimeoutException)])
def test_metasearch_exception_never_changes_http2_hook(monkeypatch, message, error):
    original = HTTP2Connection._send_connection_init

    class Client:
        def request(self, *args, **kwargs):
            assert HTTP2Connection._send_connection_init is original
            raise RuntimeError(message)

    monkeypatch.setattr(search.httpx, "Client", lambda **kwargs: Client())
    with pytest.raises(error):
        search._HttpxClient(verify=False).request("GET", "https://example.com")
    assert HTTP2Connection._send_connection_init is original


def test_metasearch_does_not_require_httpcore_private_init_method(monkeypatch):
    monkeypatch.delattr(HTTP2Connection, "_send_connection_init")
    response = SimpleNamespace(status_code=200, content=b"fixture", text="fixture")

    def client(**kwargs):
        assert kwargs["http2"] is True
        assert kwargs["follow_redirects"] is False
        return SimpleNamespace(request=lambda *args, **kwargs: response)

    monkeypatch.setattr(search.httpx, "Client", client)
    assert search._HttpxClient(verify=False).request("GET", "https://example.com").status_code == 200
