"""Issue #244: canonical Content-Type parsing and media routing."""

import pytest

from sieve.content_type import (
    charset_of,
    classify_media,
    is_image_media,
    is_json_media,
    is_pdf_media,
    media_type_of,
    parse_content_type,
)


def test_basic_parsing():
    parsed = parse_content_type("Text/HTML; charset=UTF-8")
    assert parsed.media_type == "text/html"
    assert parsed.charset == "utf-8"


def test_parameters_and_quoting():
    parsed = parse_content_type('application/json; charset="utf-8"; boundary=x')
    assert parsed.media_type == "application/json"
    assert parsed.charset == "utf-8"


def test_absent_and_garbage():
    assert parse_content_type("").media_type == ""
    assert parse_content_type(None).media_type == ""
    assert parse_content_type("garbage").media_type == ""
    assert parse_content_type("a" * 2000).media_type == ""  # oversized header


def test_media_type_of_matches_legacy_split_semantics():
    assert media_type_of("application/json; charset=utf-8") == "application/json"
    assert media_type_of("  TEXT/PLAIN ") == "text/plain"


def test_charset_of_none_when_absent():
    assert charset_of("text/html") is None
    assert charset_of("text/html; charset=iso-8859-1") == "iso-8859-1"


def test_json_detection_includes_structured_syntax():
    assert is_json_media("application/json")
    assert is_json_media("application/ld+json")
    assert is_json_media("text/json")
    assert not is_json_media("text/html")


def test_pdf_and_image_detection():
    assert is_pdf_media("application/pdf")
    assert not is_pdf_media("text/html")
    assert is_image_media("image/png")
    assert not is_image_media("application/pdf")


def test_classify_buckets():
    assert classify_media("text/html") == "html"
    assert classify_media("application/json") == "json"
    assert classify_media("application/pdf") == "pdf"
    assert classify_media("image/jpeg") == "image"
    assert classify_media("text/plain") == "text"
    assert classify_media("application/octet-stream") == "binary"
    assert classify_media("") == "unknown"


def test_extract_encoding_consistency():
    """fetcher._extract_encoding must agree with the canonical parser."""
    from sieve.fetcher import _extract_encoding

    assert _extract_encoding("text/html; charset=ISO-8859-1") == "iso-8859-1"
    assert _extract_encoding("text/html") == "utf-8"
    assert _extract_encoding("") == "utf-8"


@pytest.mark.parametrize("header", ["/html", "text/", "application//pdf", "application/pdf extra",
                                    "application/pdf/other", "im age/png", "image/πng", "image/K"])
def test_malformed_media_tokens_do_not_select_document_routes(header):
    assert parse_content_type(header).media_type == ""
    assert classify_media(header) == "unknown"
    assert not any((is_json_media(header), is_pdf_media(header), is_image_media(header)))


@pytest.mark.parametrize("header, bucket", [(" Application/PDF ; charset=UTF-8", "pdf"),
                                          ('Application/Problem+JSON; charset="utf-8"', "json"),
                                          ("IMAGE/PNG; x=1", "image")])
def test_page_and_response_envelopes_use_canonical_media(header, bucket):
    from sieve.envelope import detect_page_type
    from sieve.server import ResponseModel, _with_agent_hints, _is_js_shell
    assert detect_page_type("", "https://example.test", header) == bucket
    response = ResponseModel(url="https://example.test", status=200, content=["data"], content_type=header)
    assert _with_agent_hints(response).page_type == bucket
    assert not _is_js_shell(response)


@pytest.mark.parametrize("header", ["", "application/pdf extra", "x" * 2000])
def test_invalid_media_preserves_structural_page_type(header):
    from sieve.envelope import detect_page_type
    from sieve.server import ResponseModel, _with_agent_hints
    assert detect_page_type("<article>content</article>", "https://example.test", header) == "article"
    response = ResponseModel(url="https://example.test", status=200, content=["content"],
                             page_type="article", content_type=header)
    assert _with_agent_hints(response).page_type == "article"


def test_error_page_type_overrides_content_type():
    from sieve.server import ResponseModel, _with_agent_hints
    response = ResponseModel(url="https://example.test", status=200, content=[], content_type="application/pdf", error="js_shell_detected")
    assert _with_agent_hints(response).page_type == "js_shell"


def test_url_filter_normalizes_header_without_matching_parameters():
    from sieve.urlfilters import ContentTypeFilter
    images = ContentTypeFilter("image/")
    assert images.accepts("https://example.test/photo", {"content_type": "IMAGE/PNG; x=1"})
    assert not images.accepts("https://example.test/photo.png", {"content_type": "text/html; note=image/png"})
    assert not images.accepts("https://example.test/photo.png", {"content_type": "image/png extra"})
    assert images.accepts("https://example.test/photo.png")
    assert images.accepts("https://example.test/photo")
