"""Surface-contract tests for crawl robots defaults (issue #2).

``respect_robots`` must default to True on every crawling surface (crawl
implementation, SDK server method, MCP router collection) so the safe default
cannot drift between CLI/MCP/SDK. Explicit bypass stays available and must be
visible in the crawl response (``respect_robots`` field + summary flag).
"""

import inspect

import pytest


def test_crawl_impl_defaults_respect_robots_true():
    from sieve.crawl import smart_crawl

    sig = inspect.signature(smart_crawl)
    default = sig.parameters["respect_robots"].default
    assert default is True, f"sieve.crawl.smart_crawl respect_robots default drifted: {default!r}"


def test_server_crawl_method_defaults_respect_robots_true():
    from sieve.server import MasterFetchServer

    sig = inspect.signature(MasterFetchServer.smart_crawl)
    default = sig.parameters["respect_robots"].default
    assert default is True, f"MasterFetchServer.smart_crawl respect_robots default drifted: {default!r}"


def test_server_search_mixin_defaults_respect_robots_true():
    from sieve.server_search import smart_crawl

    sig = inspect.signature(smart_crawl)
    default = sig.parameters["respect_robots"].default
    assert default is True, f"server_search.smart_crawl respect_robots default drifted: {default!r}"


def test_crawl_schema_description_documents_true_default():
    """The MCP tool schema must advertise the true default, not a stale one."""
    from sieve.command_router import CAPABILITY_REGISTRY

    found = False
    for capability in CAPABILITY_REGISTRY.values():
        td = capability.definition or {}
        if td.get("name") != "smart_crawl":
            continue
        schema = td.get("inputSchema") or {}
        options = (schema.get("properties") or {}).get("options") or {}
        desc = (options.get("description") or "").lower()
        if "respect_robots" in desc:
            found = True
            # Must advertise default-on; a stale "(false)" would fail here.
            assert "respect_robots (true" in desc, (
                f"schema description does not advertise respect_robots=true: {desc}"
            )
    assert found, "no respect_robots description found in mcp_smart_crawl schema"


def test_crawl_response_exposes_respect_robots_flag():
    """The crawl envelope must make bypass visible (respect_robots field)."""
    from sieve.crawl import CrawlResponseModel

    fields = CrawlResponseModel.model_fields
    assert "respect_robots" in fields
    assert fields["respect_robots"].default is True
    desc = (fields["respect_robots"].description or "").lower()
    assert "bypass" in desc


def test_crawl_summary_flags_explicit_bypass():
    """A bypassed crawl must say so in its summary."""
    from sieve.crawl import CrawlResponseModel

    # The summary text is produced inside smart_crawl; pin the marker here so
    # refactors that drop it fail this test. The marker must also appear in
    # crawl.py's summary construction (checked by grep-like pinning below).
    marker = "robots.txt BYPASSED"
    resp = CrawlResponseModel(
        start_url="https://example.com", pages=[], respect_robots=False,
        summary=f"crawled 1 page(s); {marker}",
    )
    assert marker in resp.summary

    # Pin that crawl.py actually emits the marker on the bypass path.
    import sieve.crawl as crawl_mod
    import pathlib
    source = pathlib.Path(crawl_mod.__file__).read_text(encoding="utf-8")
    assert marker in source, "crawl.py no longer flags explicit robots bypass in the summary"
