import gzip
import os

from sieve.sitemap import parse_sitemap


def test_sitemap_parser_stops_before_materializing_extra_records():
    from sieve.sitemap import parse_sitemap_detailed
    from sieve.resource_budget import ResourceBudget, budget_scope
    body = b'<urlset>' + b'<url><loc>https://example.com/a</loc></url>' * 100 + b'</urlset>'
    account = ResourceBudget(limits={"items": 2})
    with budget_scope(account):
        result = parse_sitemap_detailed(body)
    assert len(result.urls) == account.consumed["items"] == 2
    assert "items" in account.truncated


def test_sitemap_bodies_share_input_budget_and_stop_further_fetches():
    from sieve.sitemap import discover_sitemap
    from sieve.resource_budget import ResourceBudget, budget_scope
    calls = []
    def get(url):
        calls.append(url)
        return 200, b"x" * 11
    account = ResourceBudget(limits={"input_bytes": 10})
    with budget_scope(account):
        result = discover_sitemap("https://example.com", http_get=get)
    assert len(calls) == 1
    assert result.urls == []
    assert account.consumed["input_bytes"] == 10
    assert "input_bytes" in account.truncated


def test_sitemap_does_not_expand_external_entities(tmp_path):
    secret = tmp_path / "secret.txt"
    secret.write_text("must not be exposed", encoding="utf-8")
    xml = f'''<!DOCTYPE urlset [<!ENTITY secret SYSTEM "{secret}">]>
    <urlset><url><loc>&secret;</loc></url></urlset>'''.encode()

    urls, children = parse_sitemap(xml)

    assert urls == []
    assert children == []


def test_gzip_sitemap_is_supported():
    xml = b'<urlset><url><loc>https://example.test/a</loc></url></urlset>'

    urls, children = parse_sitemap(gzip.compress(xml))

    assert [item.url for item in urls] == ["https://example.test/a"]
    assert children == []


def test_gzip_bomb_is_bounded():
    """#223: compressed and expanded byte limits both hold before materialization.

    parse_sitemap is fail-soft (malformed/oversized input yields an empty
    result), so the enforcement point is _maybe_gunzip.
    """
    import pytest
    from sieve import sitemap

    bomb = gzip.compress(b"\0" * (sitemap._MAX_SITEMAP_BYTES + 1024))
    with pytest.raises(ValueError, match="decompressed sitemap exceeds"):
        sitemap._maybe_gunzip(bomb)
    # Fail-soft contract: oversized input yields no URLs, not a crash.
    assert sitemap.parse_sitemap(bomb) == ([], [])

    # Compressed-input cap: constructing a container whose *compressed* size
    # is over the cap must be rejected before any inflation happens.
    incompressible = os.urandom(sitemap._MAX_GZIP_BYTES + 1024)
    oversized = gzip.compress(incompressible, compresslevel=1)
    assert len(oversized) > sitemap._MAX_GZIP_BYTES
    with pytest.raises(ValueError, match="compressed sitemap exceeds"):
        sitemap._maybe_gunzip(oversized)


# ── partial-recovery provenance (#94) ────────────────────────────────

class TestRecoveryProvenance:
    """Recovered (malformed/truncated) documents are flagged, never silently
    promoted to authoritative results. Tuple contract of parse_sitemap is
    unchanged for existing callers."""

    def test_wellformed_document_is_authoritative(self):
        from sieve.sitemap import parse_sitemap_detailed
        xml = b'<?xml version="1.0"?><urlset><url><loc>https://example.test/a</loc></url></urlset>'
        res = parse_sitemap_detailed(xml)
        assert res.recovered is False
        assert [u.recovered for u in res.urls] == [False]

    def test_truncated_document_is_flagged_recovered(self):
        from sieve.sitemap import parse_sitemap_detailed
        xml = (
            b'<?xml version="1.0"?><urlset>'
            b"<url><loc>https://example.test/a</loc></url>"
            b"<url><loc>https://example.test/b</loc>"  # truncated: no closing tags
        )
        res = parse_sitemap_detailed(xml)
        # lxml salvaged at least one complete entry...
        assert len(res.urls) >= 1
        # ...but explicitly flagged as recovered, not authoritative.
        assert res.recovered is True
        assert all(u.recovered for u in res.urls)

    def test_malformed_document_with_garbage_is_flagged_or_empty(self):
        from sieve.sitemap import parse_sitemap_detailed
        res = parse_sitemap_detailed(b"<urlset><url><loc>https://e.test/x")
        assert res.recovered is True or res.urls == []

    def test_tuple_contract_unchanged(self):
        xml = b'<?xml version="1.0"?><urlset><url><loc>https://example.test/a</loc></url></urlset>'
        urls, children = parse_sitemap(xml)
        assert [u.url for u in urls] == ["https://example.test/a"]
        assert children == []

    def test_discover_sitemap_reports_recovered_provenance(self):
        from sieve.sitemap import discover_sitemap

        truncated = (
            b'<?xml version="1.0"?><urlset>'
            b"<url><loc>https://example.test/a</loc></url>"
            b"<url><loc>https://example.test/b</loc>"
        )

        def fake_get(url):
            if url.endswith("/robots.txt"):
                return (200, b"Sitemap: https://example.test/sitemap.xml")
            if url.endswith("/sitemap.xml"):
                return (200, truncated)
            return None

        res = discover_sitemap("https://example.test", http_get=fake_get, max_urls=10)
        assert res.recovered_sitemaps == ["https://example.test/sitemap.xml"]
        assert res.sitemaps_used == ["https://example.test/sitemap.xml"]
        assert res.urls and all(u.recovered for u in res.urls)

    def test_discover_sitemap_no_recovery_flag_for_valid(self):
        from sieve.sitemap import discover_sitemap

        valid = b'<?xml version="1.0"?><urlset><url><loc>https://example.test/a</loc></url></urlset>'

        def fake_get(url):
            if url.endswith("/robots.txt"):
                return (200, b"Sitemap: https://example.test/sitemap.xml")
            if url.endswith("/sitemap.xml"):
                return (200, valid)
            return None

        res = discover_sitemap("https://example.test", http_get=fake_get, max_urls=10)
        assert res.recovered_sitemaps == []
        assert [u.recovered for u in res.urls] == [False]
def test_harvesters_refuse_recovered_leaves_and_indexes(monkeypatch):
    from sieve import sitemap_harvest as harvest

    valid = b'<urlset><url><loc>https://example.org/seller/gig</loc></url></urlset>'
    truncated = valid[:-len(b'</urlset>')]
    broken_index = b'<sitemapindex><sitemap><loc>https://example.org/gig-child.xml</loc></sitemap>'
    for body in (valid, truncated, broken_index):
        fetched = []

        def fetch(url, **kwargs):
            fetched.append(url)
            if url.endswith("gig-child.xml"):
                return valid
            return body if url.endswith("sitemap.xml") else None

        monkeypatch.setattr(harvest, "_fetch", fetch)
        expected = ["https://example.org/seller/gig"] if body == valid else []
        assert harvest.harvest_urls("https://example.org") == expected
        assert harvest.harvest_gig_urls("example.org") == expected
        assert not any(url.endswith("gig-child.xml") for url in fetched)


def test_gig_harvester_refuses_recovered_child(monkeypatch):
    from sieve import sitemap_harvest as harvest

    index = b'<sitemapindex><sitemap><loc>https://example.org/gig-child.xml</loc></sitemap></sitemapindex>'
    child = b'<urlset><url><loc>https://example.org/seller/gig</loc></url>'

    def fetch(url, **kwargs):
        if url.endswith("sitemap.xml"):
            return index
        return child if url.endswith("gig-child.xml") else None

    monkeypatch.setattr(harvest, "_fetch", fetch)
    assert harvest.harvest_gig_urls("example.org") == []
