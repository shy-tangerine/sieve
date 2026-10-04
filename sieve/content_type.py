"""Canonical Content-Type parsing and media-type routing (issue #244).

Content-Type handling was repeated across fetcher (charset extraction),
response_translation (extraction routing), and network_capture (fragment
analysis) with subtly different semantics: some split on ``;`` and lower,
some don't, some treat empty as HTML, some don't. This module is the single
parser every routing decision must use.

``parse_content_type`` returns a normalized ``(media_type, charset)`` pair:
    * media_type is always lowercase ``type/subtype`` (no parameters),
      ``""`` when the header is absent or unparseable;
    * charset is the lowercased charset parameter, or ``None``;
    * the header value is bounded (oversized headers are treated as absent,
      matching the fetcher's historical 1024-byte guard).

Routing helpers give one shared answer to the two questions every consumer
asks: is this JSON? is this binary-vs-text? ``classify_media`` buckets into
``json|pdf|image|html|text|binary|unknown``.

Standard library only.
"""

from __future__ import annotations

from dataclasses import dataclass

__all__ = [
    "ParsedContentType",
    "parse_content_type",
    "media_type_of",
    "charset_of",
    "is_json_media",
    "is_pdf_media",
    "is_image_media",
    "classify_media",
]

# Header guard: a Content-Type header larger than this is hostile/broken.
_MAX_HEADER_CHARS = 1024


@dataclass(frozen=True)
class ParsedContentType:
    media_type: str  # lowercase "type/subtype", "" when absent/unparseable
    charset: str | None  # lowercased charset param, None when unspecified

    @property
    def is_text(self) -> bool:
        return classify_media(self.media_type) in {"text", "html", "json"}


_MEDIA_TOKEN = frozenset("abcdefghijklmnopqrstuvwxyz0123456789!#$%&'*+-.^_`|~")


def parse_content_type(value: str | None) -> ParsedContentType:
    """Parse a Content-Type header into normalized media type + charset.

    Tolerates parameters, quoting, whitespace, and case. Returns
    ``ParsedContentType("", None)`` for absent/oversized/unparseable values.
    """
    if not value or not isinstance(value, str) or len(value) > _MAX_HEADER_CHARS:
        return ParsedContentType("", None)
    parts = value.split(";")
    if not parts[0].isascii():
        return ParsedContentType("", None)
    media = parts[0].strip().lower()
    tokens = media.split("/")
    if len(tokens) != 2 or not all(token and all(char in _MEDIA_TOKEN for char in token) for token in tokens):
        return ParsedContentType("", None)
    charset: str | None = None
    for param in parts[1:]:
        name, _, raw = param.partition("=")
        if name.strip().lower() == "charset":
            candidate = raw.strip().strip('"').strip("'").strip().lower()
            if candidate:
                charset = candidate
    return ParsedContentType(media, charset)


def media_type_of(value: str | None) -> str:
    """Normalized ``type/subtype`` or ``""`` (drop-in for `split(";")[0]`)."""
    return parse_content_type(value).media_type


def charset_of(value: str | None) -> str | None:
    """Normalized charset parameter, or None (drop-in for charset extraction)."""
    return parse_content_type(value).charset


def is_json_media(value: str | None) -> bool:
    """True for application/json, +json structured syntax, text/json."""
    media = media_type_of(value)
    if not media:
        return False
    return media in ("application/json", "text/json") or media.endswith("+json")


def is_pdf_media(value: str | None) -> bool:
    return media_type_of(value) == "application/pdf"


def is_image_media(value: str | None) -> bool:
    return media_type_of(value).startswith("image/")


def classify_media(value: str | None) -> str:
    """Bucket a Content-Type into json|pdf|image|html|text|binary|unknown."""
    media = media_type_of(value)
    if not media:
        return "unknown"
    if is_json_media(media):
        return "json"
    if is_pdf_media(media):
        return "pdf"
    if is_image_media(media):
        return "image"
    if media == "text/html" or media.endswith("+html") or media == "application/xhtml+xml":
        return "html"
    if media.startswith("text/"):
        return "text"
    if media in ("application/xml",) or media.endswith("+xml"):
        return "text"
    return "binary"
