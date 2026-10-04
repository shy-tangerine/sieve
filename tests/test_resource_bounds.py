import gzip
import json
import pytest

from sieve import arctic_shift
from sieve import sitemap_harvest


def test_sitemap_harvest_rejects_oversized_compressed_input(monkeypatch):
    monkeypatch.setattr(sitemap_harvest, "_MAX_GZIP_BYTES", 1)

    with pytest.raises(ValueError, match="compressed sitemap"):
        sitemap_harvest._maybe_gunzip(gzip.compress(b"<urlset />"))


class _Response:
    status = 200
    headers = {}

    def __init__(self):
        self.body = json.dumps({"data": []}).encode()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self, amount=-1):
        return self.body if amount < 0 else self.body[:amount]


def test_arctic_shift_rejects_oversized_response(monkeypatch):
    monkeypatch.setattr(arctic_shift, "_MAX_RESPONSE_BYTES", 1)
    monkeypatch.setattr(arctic_shift, "urlopen", lambda request, timeout: _Response())

    with pytest.raises(arctic_shift.ArcticShiftTransportError, match="exceeds"):
        arctic_shift.ArcticShiftClient(retry_count=0).search_posts()
