"""Script-tag JSON harvest and HTML cleanup/markdown conversion.

Reconstructed clean-room against Sieve's own tests
(tests/test_scrapegraph_ports.py, docs/cli.md) and the extraction call
sites; no upstream-derived code.

Pages that render client-side leave their data in inline ``<script>`` tags.
Harvesting those blobs recovers the payload without a browser:

* ``application/ld+json`` blocks (structured data),
* ``__NEXT_DATA__`` / ``__INITIAL_STATE__`` application-state blobs,
* generic ``var x = {...}`` JSON assignments inside scripts.

Raw ``window.*``/``document.*`` assignments are deliberately NOT emitted —
only parsed JSON payloads reach the output, so arbitrary script text never
passes as page content.

Also provides bounded HTML minification/cleanup and a Markdown converter
that resolves relative links against ``base_url``.
"""

from __future__ import annotations

import json
import re
from lxml import html as html_parser, etree
from urllib.parse import urljoin

__all__ = [
    "extract_from_script_tags",
    "harvest_script_json",
    "minify_html",
    "cleanup_html",
    "convert_to_md",
]

# ── script JSON harvest ──────────────────────────────────────────────────────

# var/let/const name = { ... }  (non-greedy to the last closing brace)
_JSON_ASSIGN_RE = re.compile(r"(?:const|let|var)?\s*\w+\s*=\s*({[\s\S]*?});?\s*(?:\n|$)")
# Markers of script tags already handled by the specific passes above.

# Budgets (#89): these regex passes run over untrusted HTML, so the input is
# bounded and the aggregate output is capped with a truncation note.
_MAX_DOC_CHARS = 2_000_000
_MAX_OUTPUT_CHARS = 200_000
_MAX_LINKS = 2000
_MAX_IMAGES = 2000


def _try_parse_json(text: str):
    """Parse a candidate JSON blob; None when empty or invalid."""
    text = text.strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except (ValueError, RecursionError):
        return None


def _document(html: str):
    return html_parser.document_fromstring(html[:_MAX_DOC_CHARS].strip() or "<html></html>",
                                          parser=html_parser.HTMLParser(remove_comments=True))


def extract_from_script_tags(html: str) -> str:
    """Harvest JSON payloads from ``<script>`` tags as human-readable text.

    Order: JSON-LD first (structured data, highest value), then
    application-state blobs, then generic JSON assignments in other scripts.
    Output is capped at ``_MAX_OUTPUT_CHARS`` with an explicit truncation note.
    """
    document = _document(html)
    parts: list[str] = []
    scripts = list(document.iter("script"))
    for kind, label in (("application/ld+json", "JSON-LD"), ("__NEXT_DATA__", "__NEXT_DATA__"),
                        ("__INITIAL_STATE__", "__INITIAL_STATE__"), (None, "JSON data from script")):
        for script in scripts:
            marker = script.get("type") if script.get("type") == "application/ld+json" else script.get("id")
            recognized = marker in {"application/ld+json", "__NEXT_DATA__", "__INITIAL_STATE__"}
            if (kind is None and recognized) or (kind is not None and marker != kind):
                continue
            content = script.text or ""
            candidates = [content] if kind else (match.group(1) for match in _JSON_ASSIGN_RE.finditer(content))
            for candidate in candidates:
                parsed = _try_parse_json(candidate)
                if parsed is not None:
                    parts.append(f"{label}: {json.dumps(parsed, indent=2)}")

    result = "\n\n".join(parts)
    if len(result) > _MAX_OUTPUT_CHARS:
        result = result[:_MAX_OUTPUT_CHARS] + (
            "\n\n[truncated: script-harvest output budget exceeded]"
        )
    return result


def harvest_script_json(html: str) -> list[str]:
    """Harvested chunks as a list (for the markdown fallback chain)."""
    harvested = extract_from_script_tags(html)
    return [harvested] if harvested else []


# ── HTML minification / cleanup ──────────────────────────────────────────────

def minify_html(html: str) -> str:
    """Collapse ordinary text whitespace without changing attributes or raw text."""
    source = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\ufffe\uffff]", "", html[:_MAX_DOC_CHARS]).strip()
    if not source:
        return ""
    try:
        root = html_parser.fromstring(source, parser=html_parser.HTMLParser(remove_comments=True))
    except etree.ParserError:
        return ""
    stack = [(root, False)]
    while stack:
        element, inherited = stack.pop()
        preserved = inherited or element.tag in {"script", "style", "pre", "textarea"}
        if element.text and not preserved:
            element.text = re.sub(r"\s+", " ", element.text) if element.text.strip() else None
        if element.tail and not inherited:
            element.tail = re.sub(r"\s+", " ", element.tail) if element.tail.strip() else None
        stack.extend((child, preserved) for child in reversed(element))
    return html_parser.tostring(root, encoding="unicode").strip()


def cleanup_html(
    html: str, base_url: str = ""
) -> tuple[str, str, list[str], list[str], str]:
    """Split a page into (title, minified body, links, images, harvested JSON).

    Link and image collections are capped (``_MAX_LINKS``/``_MAX_IMAGES``);
    relative URLs resolve against ``base_url`` when provided. Images with no
    scheme and no base_url are kept as-is.
    """
    document = _document(html)
    title_element = next(document.iter("title"), None)
    title = "".join(title_element.itertext()).strip() if title_element is not None else ""

    script_content = extract_from_script_tags(html)

    link_urls: list[str] = []
    for element in document.iter("a"):
        if len(link_urls) >= _MAX_LINKS:
            break
        href = element.get("href")
        if href is None:
            continue
        link_urls.append(urljoin(base_url, href) if base_url else href)

    image_urls: list[str] = []
    for element in document.iter("img"):
        if len(image_urls) >= _MAX_IMAGES:
            break
        src = element.get("src")
        if src is None:
            continue
        if src.startswith("http"):
            image_urls.append(src)
        elif base_url:
            image_urls.append(urljoin(base_url, src))
        else:
            image_urls.append(src)

    body_element = document.find("body")
    body = html_parser.tostring(body_element if body_element is not None else document, encoding="unicode")
    return title, minify_html(body), link_urls, image_urls, script_content


# ── Markdown conversion ──────────────────────────────────────────────────────

# Relative link destinations left in converted Markdown.
_RELATIVE_LINK_RE = re.compile(
    r"(?P<prefix>!? \[[^\n\]]*\]\()(?P<destination>[^)\n]+)\)", re.VERBOSE
)


def _fix_relative_links(markdown: str, base_url: str) -> str:
    """Resolve non-absolute link destinations against ``base_url``."""

    def replace(match: re.Match[str]) -> str:
        dest = match.group("destination").strip()
        if not dest.startswith(("http://", "https://", "mailto:", "#", "data:", "javascript:")):
            dest = urljoin(base_url, dest)
        return f"{match.group('prefix')}{dest})"

    return _RELATIVE_LINK_RE.sub(replace, markdown)


def convert_to_md(html: str, base_url: str = "") -> str:
    """Convert HTML to Markdown, resolving relative links against base_url.

    Prefers ``html2text`` (with ``baseurl`` so it resolves links itself);
    falls back to ``markdownify`` plus manual link fixup. When neither
    optional dependency is installed, returns the raw HTML.
    """
    if not html:
        return ""

    try:
        import html2text  # type: ignore

        converter = html2text.HTML2Text()
        converter.ignore_links = False
        converter.body_width = 0
        if base_url:
            converter.baseurl = base_url
        converted = converter.handle(html)
        if converted and converted.strip():
            return converted
    except ImportError:
        pass
    except Exception:
        pass

    try:
        from markdownify import markdownify as _markdownify  # type: ignore

        converted = _markdownify(html)
        if base_url and converted:
            converted = _fix_relative_links(converted, base_url)
        return converted
    except Exception:
        return html
