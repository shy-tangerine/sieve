from sieve.trafilatura_extractor import _safe_markdown_links
from types import SimpleNamespace

import pytest
from lxml import html

import sieve.trafilatura_extractor as extraction


def test_markdown_links_cannot_keep_active_schemes():
    result = _safe_markdown_links(
        "[run](javascript:alert(1)) ![pixel](data:text/html,pwned) "
        "[safe](https://example.test)"
    )

    assert "javascript:" not in result
    assert "data:" not in result
    assert "[run](#)" in result
    assert "![pixel](#)" in result
    assert "[safe](https://example.test)" in result


@pytest.mark.parametrize("kind", ["markdown", "text", "article", "structured"])
def test_empty_selector_never_extracts_full_page(monkeypatch, kind):
    body = '<main>Outside selected scope</main><div id="selected"></div>'
    page = SimpleNamespace(content=body, body=body.encode(), url="https://example.test",
                           css=lambda selector: html.fromstring(body).cssselect(selector))
    monkeypatch.setattr(extraction, "_extract_type",
                        lambda fragment, *args: "Outside selected scope" if "<main>" in fragment else "")

    assert extraction.extract_with_trafilatura(page, kind, "#selected") == []
