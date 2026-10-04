from types import SimpleNamespace

import pytest

import sieve.ocr as ocr
import sieve.response_translation as translation
from sieve.pdf_extractor import PdfResult
from sieve.response_translation import translate_response
from sieve.server import ResponseModel
from sieve.server import _with_agent_hints


def _page(body: bytes, content_type: str = "text/html"):
    return SimpleNamespace(
        status=200,
        url="https://example.test/page",
        body=body,
        content=body.decode(),
        encoding="utf-8",
        headers={"content-type": content_type},
    )


def _pdf_page(body: bytes = b"%PDF-1.7 fake", content_type: str = "application/pdf"):
    page = _page(body, content_type)
    page.url = "https://example.test/report.pdf"
    return page


def test_translation_debits_aggregate_bytes_before_parsing():
    from sieve.resource_budget import ResourceBudget, BudgetExceeded, budget_scope
    body = b'{"fixture":true}'
    account = ResourceBudget(limits={"input_bytes": len(body)})
    with budget_scope(account):
        result, _ = translate_response(_page(body, "application/json"), "text", None, False,
                                      False, "http", 1, ResponseModel, maximum_bytes=1024)
        assert result.content == [body.decode()]
        assert account.consumed["input_bytes"] == len(body)
        with pytest.raises(BudgetExceeded):
            translate_response(_page(body, "application/json"), "text", None, False,
                               False, "http", 1, ResponseModel, maximum_bytes=1024)


@pytest.mark.parametrize("use_trafilatura", [False, True])
def test_translation_refuses_primary_parser_after_node_exhaustion(monkeypatch, use_trafilatura):
    from sieve.resource_budget import ResourceBudget, budget_scope

    def forbidden(*args, **kwargs):
        pytest.fail("primary parser ran after request budget exhaustion")

    monkeypatch.setattr("sieve.extractor.extract_content", forbidden)
    monkeypatch.setattr("sieve.trafilatura_extractor.extract_with_trafilatura", forbidden)
    with budget_scope(ResourceBudget(limits={"nodes": 0})):
        result, _ = translate_response(_page(b"<article>fixture</article>"), "text", None,
                                      False, use_trafilatura, "http", 1, ResponseModel, maximum_bytes=1024)
    assert result.is_truncated and not result.content_ok
    assert result.resource_budget["truncated"] == ["nodes"]


@pytest.mark.parametrize("use_trafilatura", [False, True])
@pytest.mark.parametrize("url", ["https://example.test/page", "https://old.reddit.com/r/example/"])
def test_empty_selector_preserves_scope_and_retry_guidance(monkeypatch, use_trafilatura, url):
    from lxml import html
    import sieve.trafilatura_extractor as extraction

    body = b'<main>Outside selected scope</main><div id="selected"></div>'
    page = _page(body)
    page.url = url
    monkeypatch.setattr(translation, "parse_old_reddit_listing", lambda *a: "Outside selected scope")
    page.css = lambda selector: html.fromstring(body).cssselect(selector)
    monkeypatch.setattr(extraction, "_extract_type",
                        lambda fragment, *args: "Outside selected scope" if "<main>" in fragment else "")
    result, ajax_shell = translate_response(
        page, "text", "#selected", False, use_trafilatura, "http", 1,
        ResponseModel, maximum_bytes=1024,
    )
    result = _with_agent_hints(result)

    assert result.content == []
    assert result.error == "selector_empty"
    assert result.next_action == "retry_without_css_selector"
    assert not result.content_ok
    assert not ajax_shell


@pytest.mark.parametrize("backend", ["pdf", "ocr"])
def test_pdf_open_errors_do_not_echo_backend_values(monkeypatch, backend):
    import sys
    import sieve.pdf_extractor as pdf

    def fail(*args, **kwargs):
        raise RuntimeError("private=/private/session.json token=sk-live-secret-1234567890")

    if backend == "pdf":
        monkeypatch.setattr(pdf, "_get_pdfplumber", lambda: SimpleNamespace(open=fail))
        result = pdf.extract_pdf(b"%PDF-1.7 fake")
    else:
        monkeypatch.setattr(ocr, "_get_pdfium", lambda: SimpleNamespace(PdfDocument=fail))
        monkeypatch.setitem(sys.modules, "numpy", SimpleNamespace())
        result = ocr.ocr_pdf(b"%PDF-1.7 fake")
    output = repr(result)
    assert result.error.startswith("pdf_open_failed")
    assert result.content
    assert "alice" not in output and "sk-live-secret" not in output


@pytest.mark.parametrize("backend", ["pdf", "ocr"])
def test_document_page_failure_keeps_partial_content_without_exception_values(monkeypatch, caplog, backend):
    import logging
    import sys
    import sieve.pdf_extractor as pdf

    sentinel = "private=/private/session.json token=sk-live-secret-1234567890"
    def fail(*args, **kwargs):
        raise RuntimeError(sentinel)
    good = SimpleNamespace(chars=[], images=[], get_size=lambda: (10, 10),
                           render=lambda **kw: SimpleNamespace(to_pil=lambda: SimpleNamespace(convert=lambda kind: object())))
    bad = SimpleNamespace(chars=[], images=[], get_size=lambda: (10, 10), render=fail)

    class Document:
        pages = [good, bad]
        metadata = {}
        def __len__(self): return len(self.pages)
        def __getitem__(self, index): return self.pages[index]
        def close(self): pass
        def get_metadata_dict(self): return {}

    caplog.set_level(logging.DEBUG)
    if backend == "pdf":
        monkeypatch.setattr(pdf, "_get_pdfplumber", lambda: SimpleNamespace(open=lambda *a, **kw: Document()))
        monkeypatch.setattr(pdf, "_extract_toc", lambda *a: [])
        monkeypatch.setattr(pdf, "_render_page", lambda page, *a, **kw: fail() if page is bad else "Good page content " * 20)
        result = pdf.extract_pdf(b"%PDF-1.7 fake")
    else:
        monkeypatch.setattr(ocr, "_get_pdfium", lambda: SimpleNamespace(PdfDocument=lambda *a, **kw: Document()))
        monkeypatch.setitem(sys.modules, "numpy", SimpleNamespace(array=lambda value: value))
        monkeypatch.setattr(ocr, "_get_rapidocr", lambda: object())
        monkeypatch.setattr(ocr, "_ocr_ndarray", lambda *a: "Good page content")
        result = ocr.ocr_pdf(b"%PDF-1.7 fake")
    content = "\n".join(result.content)
    assert "Good page content" in content
    assert "Page 2" in content and "failed" in content.lower()
    assert "alice" not in content + caplog.text
    assert "sk-live-secret" not in content + caplog.text


@pytest.mark.parametrize("header", ["Application/Problem+JSON; charset=UTF-8", 'TEXT/JSON; charset="utf-8"'])
def test_parameterized_json_route_parity(header):
    result, ajax_shell = translate_response(_page(b'{"answer": 42}', header), "markdown", None, True,
                                            False, "http", 1.5, ResponseModel, maximum_bytes=1024)
    assert result.content == ['{"answer": 42}']
    assert not ajax_shell


@pytest.mark.parametrize("header", ["", "application/pdf extra", "x" * 2000, "APPLICATION/PDF; x=1"])
def test_pdf_signature_route_parity_with_header_variants(monkeypatch, header):
    monkeypatch.setattr(translation, "extract_pdf", lambda *a, **kw: PdfResult(content=["PDF content"], content_ok=True))
    result, ajax_shell = translate_response(_pdf_page(content_type=header), "text", None, True,
                                            False, "http", 1, ResponseModel, maximum_bytes=1024)
    assert result.content == ["PDF content"]
    assert not ajax_shell


def test_translation_seam_preserves_json_without_html_extraction():
    result, ajax_shell = translate_response(
        _page(b'{"answer": 42}', "application/json"), "markdown", None, True,
        False, "http", 1.5, ResponseModel, maximum_bytes=1024,
    )

    assert result.content == ['{"answer": 42}']
    assert result.content_type == "application/json"
    assert ajax_shell is False


def test_translation_seam_extracts_metadata_and_page_type():
    result, _ = translate_response(
        _page(b"<html><head><title>Example</title></head><body><article>Hello</article></body></html>"),
        "text", None, True, False, "http", 1.5, ResponseModel, maximum_bytes=1024,
    )

    assert result.metadata["title"] == "Example"
    assert result.page_type in {"article", "unknown"}


def test_pdf_route_forwards_extraction_options(monkeypatch):
    seen = {}

    def fake_extract(body, **options):
        seen.update(options)
        return PdfResult(content=["# Report"], content_ok=True,
                          metadata={"title": "Report"})

    monkeypatch.setattr(translation, "extract_pdf", fake_extract)
    result, ajax_shell = translate_response(
        _pdf_page(), "text", None, True, False, "http", 2.0, ResponseModel,
        maximum_bytes=1024, pages="2-4", password="secret", include_media=True,
    )

    assert seen == {
        "extraction_type": "text", "pages": "2-4", "password": "secret",
        "include_media": True,
    }
    assert result.content == ["# Report"]
    assert result.metadata == {"title": "Report"}
    assert result.content_ok is True
    assert ajax_shell is False


def test_scanned_pdf_uses_ocr_and_preserves_pdf_metadata(monkeypatch):
    base = PdfResult(
        content=["[Scanned/image-only PDF]"], scanned=True,
        error="scanned_pdf: image-only", metadata={"title": "Scanned report"},
        table_of_contents=[{"level": 1, "title": "Intro", "page": 1}],
        media=["page 1: 1 embedded image(s); largest 100x100"], quality_score=0.1,
    )
    monkeypatch.setattr(translation, "extract_pdf", lambda *args, **kwargs: base)
    monkeypatch.setattr(ocr, "ocr_available", lambda: True)
    monkeypatch.setattr(ocr, "ocr_pdf", lambda *args, **kwargs: PdfResult(
        content=["--- Page 1 ---\n\nRecovered text"],
    ))

    result, _ = translate_response(
        _pdf_page(), "markdown", None, True, False, "http", 2.0, ResponseModel,
        maximum_bytes=1024,
    )

    assert result.content == ["--- Page 1 ---\n\nRecovered text"]
    assert result.error == ""
    assert result.content_ok is True
    assert result.quality_score == 0.9
    assert result.metadata == {"title": "Scanned report"}
    assert result.table_of_contents == [{"level": 1, "title": "Intro", "page": 1}]
    assert result.media == ["page 1: 1 embedded image(s); largest 100x100"]


def test_captured_network_is_kept_for_json_early_return():
    page = _page(b'{"values": [1, 2, 3]}', "application/json")
    page.captured = [{
        "url": "https://example.test/api/data",
        "status": 200,
        "content_type": "application/json",
        "size_bytes": 22,
        "_body": '{"values": [1, 2, 3]}',
    }]

    result, ajax_shell = translate_response(
        page, "markdown", None, True, False, "http", 1.0, ResponseModel,
        maximum_bytes=1024,
    )

    assert result.network["captured_count"] == 1
    assert result.network["primary_url"] == "https://example.test/api/data"
    assert ajax_shell is False


def test_pdf_url_that_returns_login_html_is_not_extracted():
    page = _pdf_page(b"<html><body>Please sign in to download this PDF.</body></html>",
                     "text/html")
    result, _ = translate_response(
        page, "markdown", None, True, False, "http", 1.0, ResponseModel,
        maximum_bytes=1024,
    )

    assert result.error.startswith("auth_required:")
    assert result.content_ok is False
    assert "login/paywall" in result.error
