from sieve.trafilatura_extractor import _extract_type, _refs_from_html, _with_citations

HTML = ('<html><body><p>Docs and <a href="https://example.com/more\">Learn more</a>.</p>'
        '<script>var x = {"a": 1};</script></body></html>')


def test_refs_reattached_when_trafilatura_drops_links():
    refs = _refs_from_html("Docs and Learn more.", HTML, "https://example.com")
    assert "https://example.com/more" in refs


def test_extract_type_markdown_preserves_links():
    out = _extract_type(HTML, "https://example.com", "markdown")
    assert out is not None
    assert "https://example.com/more" in out


def test_with_citations_no_links_no_refs():
    assert _with_citations("Just text.", "https://example.com", "<html></html>") == "Just text."
