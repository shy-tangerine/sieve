"""Trafilatura-backed content extraction with citations and fallback chain.

Reconstructed clean-room against Sieve's own tests
(tests/test_link_preservation.py, tests/test_scrapegraph_ports.py,
tests/test_extraction_budgets.py, tests/test_crawl4ai_ports.py), docs, and
the crawl/response-translation call sites; no upstream-derived code.

Trafilatura extracts main-article content from news/blog pages, stripping
navigation, footers, sidebars, and cookie banners. This module wraps it with:

* **Citation preservation** — trafilatura can drop link targets; Markdown
  links are converted to ``⟨n⟩`` markers with a trailing ``## References``
  block, and anchors that only exist in the source HTML are re-attached from
  the raw markup.
* **A robust fallback chain** — requested format → alternate trafilatura
  entry points → html2text/markdownify → raw text.
* **CSS-selector narrowing with budgets (#247)** — at most 100 selected
  nodes, 100k serialized chars per node, 500k aggregate; a selector matching
  nothing extractable falls back to full-page extraction (#440), but a
  selector that matches nothing at all is an error (#122), never a silent
  scope change.
* **Scheme neutralization** — ``javascript:``/``data:``/etc. link
  destinations are defused before Markdown leaves the trust boundary.
"""

from __future__ import annotations

import json
import logging
from sieve.public_output import safe_error
import re
from typing import Optional
from urllib.parse import urljoin, urlsplit

import trafilatura
from lxml.etree import tostring
from lxml.html import fromstring as html_fromstring

__all__ = [
    "extract_with_trafilatura",
    "extract_content_from_html",
    "extract_html_title",
    "convert_links_to_citations",
    "to_fit_markdown",
]

logger = logging.getLogger("sieve.trafilatura_extractor")

# Budgets for CSS-selector narrowed extraction (issue #247).
_MAX_SELECTED_NODES = 100
_MAX_NODE_SERIALIZED_CHARS = 100_000
_MAX_AGGREGATE_SERIALIZED_CHARS = 500_000

# Markdown link syntax: [text](dest "title"), image variant included.
_LINK_PATTERN = re.compile(
    r"!?\[((?:[^\[\]]|\[(?:[^\[\]]|\[[^\]]*\])*\])*)\]"
    r"\(((?:[^()\s]|\([^()]*\))*)(?:\s+\"([^\"]*)\")?\)"
)


def _safe_markdown_links(markdown: str) -> str:
    """Neutralize active URL schemes before Markdown leaves the trust boundary."""

    dangerous = {"javascript", "vbscript", "file", "data"}

    def replace(match: re.Match[str]) -> str:
        destination = match.group("destination").strip()
        if urlsplit(destination).scheme.lower() in dangerous:
            return f"{match.group('prefix')}#)"
        return match.group(0)

    return re.sub(
        r"(?P<prefix>!?\[[^\n\]]*\]\()(?P<destination>[^)\n]+)\)",
        replace,
        markdown,
    )


def _is_probably_binary(data: bytes) -> bool:
    """True when the sample is mostly non-printable (PDF/image/...)."""
    if not data:
        return False
    printable = sum(1 for b in data if 32 <= b <= 126 or b in (9, 10, 13))
    return (printable / len(data)) < 0.1


def _get_html_from_page(page) -> str | None:
    """Best-effort raw HTML from a Response-like object."""
    body = getattr(page, "body", None)
    if body:
        return body.decode(getattr(page, "encoding", None) or "utf-8", errors="replace")
    content = getattr(page, "html_content", None)
    if content:
        return content
    root = getattr(page, "_root", None) or getattr(page, "root", None)
    if root is not None:
        return tostring(root, encoding="unicode")
    return None


def _extract_html_title(html: str) -> str:
    """HTML ``<title>`` content (parsed first, regex fallback)."""
    try:
        tree = html_fromstring(html)
        title_el = tree.find(".//title")
        if title_el is not None and title_el.text:
            return title_el.text.strip()
    except Exception:
        pass
    match = re.search(r"<title[^>]*>(.*?)</title>", html, re.IGNORECASE | re.DOTALL)
    return match.group(1).strip() if match else ""


# ── trafilatura entry points ─────────────────────────────────────────────────

def _trafilatura_markdown(html: str, url: str = "") -> str | None:
    """Markdown via trafilatura.extract() (different heuristics than bare_extraction)."""
    return trafilatura.extract(
        html, url=url,
        include_comments=False, include_tables=True,
        output_format="markdown",
    )


def _trafilatura_article(html: str, url: str = "") -> dict | None:
    """Article dict via bare_extraction, with an HTML <title> fallback."""
    result = trafilatura.bare_extraction(
        html, url=url,
        include_comments=False, include_tables=True,
    )
    if result is None:
        return None

    title = getattr(result, "title", "") or ""
    if not title:
        title = _extract_html_title(html)

    return {
        "title": title,
        "author": getattr(result, "author", "") or "",
        "date": getattr(result, "date", "") or "",
        "body": getattr(result, "text", "") or "",
        "description": getattr(result, "description", "") or "",
        "url": getattr(result, "url", "") or url,
        "categories": getattr(result, "categories", None) or [],
        "tags": getattr(result, "tags", None) or [],
    }


def _trafilatura_structured(html: str, url: str = "") -> dict | None:
    """Article dict enriched with sitename/categories/tags metadata."""
    article = _trafilatura_article(html, url)
    if article is None:
        return None
    try:
        metadata = trafilatura.metadata(html, url=url) if html else None
        if metadata:
            article["sitename"] = getattr(metadata, "sitename", "")
            if not article["categories"]:
                article["categories"] = getattr(metadata, "categories", []) or []
            if not article["tags"]:
                article["tags"] = getattr(metadata, "tags", []) or []
    except Exception as exc:
        logger.debug("Metadata extraction failed: %s", safe_error(exc, fallback_category="extract")["category"])
    return article


# ── citation handling ────────────────────────────────────────────────────────

def convert_links_to_citations(markdown: str, base_url: str = "") -> tuple[str, str]:
    """Convert Markdown links to ``⟨n⟩`` citation markers + References block.

    Each distinct destination gets one number (first-seen order); relative
    URLs resolve against ``base_url``. Images keep the ``!`` prefix. Returns
    ``(converted_markdown, references_block)``.
    """
    link_map: dict[str, tuple[int, str]] = {}
    parts: list[str] = []
    last = 0
    counter = 1

    for match in _LINK_PATTERN.finditer(markdown):
        parts.append(markdown[last:match.start()])
        text, url, title = match.groups()
        if base_url and not url.startswith(("http://", "https://", "mailto:")):
            url = urljoin(base_url, url)
        if url not in link_map:
            descriptors: list[str] = []
            if title:
                descriptors.append(title)
            if text and text != title:
                descriptors.append(text)
            link_map[url] = (
                counter,
                ": " + " - ".join(descriptors) if descriptors else "",
            )
            counter += 1
        num = link_map[url][0]
        is_image = match.group(0).startswith("!")
        parts.append(f"![{text}⟨{num}⟩]" if is_image else f"{text}⟨{num}⟩")
        last = match.end()
    parts.append(markdown[last:])

    converted = "".join(parts)
    refs = ["\n\n## References\n\n"] if link_map else [""]
    for url, (num, desc) in sorted(link_map.items(), key=lambda kv: kv[1][0]):
        refs.append(f"⟨{num}⟩ {url}{desc}\n")
    return converted, "".join(refs)


def _refs_from_html(md: str, html: str, base_url: str) -> str:
    """Build a References block from raw-HTML anchors whose text survives in md.

    Covers links trafilatura dropped entirely (the anchor text is present but
    the destination was lost during extraction).
    """
    seen: dict[str, int] = {}
    for match in re.finditer(
        r'<a\s[^>]*href="([^"]+)"[^>]*>(.*?)</a>', html, re.DOTALL | re.IGNORECASE
    ):
        href = match.group(1)
        inner = re.sub(r"<[^>]+>", "", match.group(2)).strip()
        if not inner or len(inner) < 2:
            continue
        if href.startswith(("http://", "https://")):
            full = href
        elif base_url and not href.startswith(("mailto:", "#", "javascript:")):
            full = urljoin(base_url, href)
        else:
            continue
        if inner in (md or "") and full not in seen:
            seen[full] = len(seen) + 1
    if not seen:
        return ""
    lines = ["\n\n## References\n\n"]
    lines += [f"⟨{n}⟩ {u}\n" for u, n in sorted(seen.items(), key=lambda kv: kv[1])]
    return "".join(lines)


def _with_citations(md: str | None, url: str, html: str = "") -> str | None:
    """Attach the References block, re-attaching source-HTML hrefs if needed."""
    if not md:
        return md
    try:
        text, refs = convert_links_to_citations(md, url)
        if not refs.strip().removeprefix("## References").strip() and html:
            refs = _refs_from_html(md, html, url)
        return text + refs
    except Exception:
        return md


# ── format dispatch ──────────────────────────────────────────────────────────

def _extract_type(html: str, url: str, extraction_type: str) -> str | None:
    """Extract in the requested format with a robust fallback chain.

    ``markdown``/``text`` prefer extract()/bare_extraction respectively;
    ``article``/``structured`` prefer bare_extraction and wrap markdown in the
    expected JSON shape as a fallback. Unknown types return None (trafilatura
    does not do raw HTML).
    """
    if extraction_type == "markdown":
        result = _trafilatura_markdown(html, url)
        if result:
            return _with_citations(result, url, html)
        article = _trafilatura_article(html, url)
        if article and article["body"]:
            title = article.get("title", "")
            return f"# {title}\n\n{article['body']}" if title else article["body"]
        return None

    if extraction_type == "text":
        article = _trafilatura_article(html, url)
        if article and article["body"]:
            return article["body"]
        return _trafilatura_markdown(html, url)

    if extraction_type == "article":
        article = _trafilatura_article(html, url)
        if article and article["body"]:
            return json.dumps(article, indent=2)
        md = _trafilatura_markdown(html, url)
        if md:
            html_title = _extract_html_title(html)
            return json.dumps({
                "title": html_title, "author": "", "date": "",
                "body": md, "description": "", "url": url,
                "categories": [], "tags": [],
            }, indent=2)
        return None

    if extraction_type == "structured":
        data = _trafilatura_structured(html, url)
        if data and data.get("body"):
            return json.dumps(data, indent=2)
        md = _trafilatura_markdown(html, url)
        if md:
            article = _trafilatura_article(html, url)
            title = article.get("title", "") if article else ""
            if not title:
                title = _extract_html_title(html)
            return json.dumps({
                "title": title, "author": "", "date": "",
                "body": md, "description": "", "url": url,
                "sitename": "", "categories": [], "tags": [],
            }, indent=2)
        return None

    return None


def to_fit_markdown(html: str, base_url: str = "") -> tuple[str, str]:
    """Pruned-HTML markdown pair: (converted_markdown, pruned_html)."""
    try:
        from sieve.pruning import prune_html as _prune

        pruned = _prune(html)
    except Exception:
        pruned = html
    try:
        from sieve.script_harvest import convert_to_md as _convert

        raw = _safe_markdown_links(_convert(pruned, base_url))
    except Exception:
        try:
            from markdownify import markdownify as _markdownify

            raw = _safe_markdown_links(_markdownify(pruned))
        except Exception:
            raw = pruned
    return raw, pruned


def _fallback_extract(
    page, extraction_type: str, css_selector: Optional[str]
) -> list[str]:
    """Last-resort extraction: html2text/markdownify or raw tag-stripped text.

    A caller-supplied selector that fails to compile or matches nothing is a
    hard error (#122) — silently widening to full-page extraction would change
    the requested scope. When script JSON was harvested, it is appended as
    supplemental content.
    """
    html = getattr(page, "content", "") or ""
    if not html and getattr(page, "body", None):
        html = page.body.decode(
            getattr(page, "encoding", None) or "utf-8", errors="replace"
        )
    if not html:
        return [""]

    harvested: list[str] = []
    if css_selector:
        from lxml.etree import XPathError

        try:
            from lxml import html as lxml_html
            from lxml.cssselect import CSSSelector

            tree = lxml_html.fromstring(html)
            matches = CSSSelector(css_selector)(tree)
        except XPathError as exc:
            raise ValueError(
                f"invalid css_selector {css_selector!r}: {type(exc).__name__}"
            ) from None
        if not matches:
            raise ValueError(
                f"css_selector {css_selector!r} matched nothing on this page; "
                "refine the selector instead of falling back to full-page extraction"
            )
        html = "\n".join(tostring(m, encoding="unicode") for m in matches)

    try:
        from sieve.script_harvest import harvest_script_json

        harvested = harvest_script_json(html)
    except Exception:
        harvested = []

    base_url = getattr(page, "url", "") or ""

    if extraction_type == "html":
        return [html]

    if extraction_type == "text":
        text = re.sub(r"<script[^>]*>.*?</script>", "", html, flags=re.DOTALL | re.IGNORECASE)
        text = re.sub(r"<style[^>]*>.*?</style>", "", text, flags=re.DOTALL | re.IGNORECASE)
        text = re.sub(r"<noscript[^>]*>.*?</noscript>", "", text, flags=re.DOTALL | re.IGNORECASE)
        text = re.sub(r"<[^>]+>", " ", text)
        text = re.sub(r"\s+", " ", text).strip()
        return [text] if text else ([] if css_selector else [html])

    # markdown / article / structured: html2text-with-baseurl then markdownify.
    try:
        from sieve.script_harvest import convert_to_md as _convert

        result = _safe_markdown_links(_convert(html, base_url))
        if harvested and result:
            result = result + "\n\n" + "\n\n".join(harvested)
        elif harvested:
            result = "\n\n".join(harvested)
        logger.info(
            "Falling back to markdownify/html2text extraction (type=%s)", extraction_type
        )
        if extraction_type == "markdown":
            result = _with_citations(result, base_url, html) or result
        if result:
            return [result]
        return ["\n\n".join(harvested)] if harvested else ([] if css_selector else [html])
    except Exception:
        pass

    try:
        from markdownify import markdownify as _markdownify

        result = _safe_markdown_links(_markdownify(html))
        if harvested and result:
            result = result + "\n\n" + "\n\n".join(harvested)
        elif harvested:
            result = "\n\n".join(harvested)
        logger.info("Falling back to markdownify extraction (type=%s)", extraction_type)
        if extraction_type == "markdown":
            result = _with_citations(result, base_url, html) or result
        if result:
            return [result]
        return ["\n\n".join(harvested)] if harvested else ([] if css_selector else [html])
    except ImportError:
        return [] if css_selector else [html]


# ── public entry points ──────────────────────────────────────────────────────

def extract_content_from_html(
    html: str, url: str = "", extraction_type: str = "markdown"
) -> str | None:
    """Extract content from a raw HTML string (smart_crawl path: one body
    feeds both links and markdown)."""
    return _extract_type(html, url, extraction_type)


def extract_html_title(html: str) -> str:
    """Public wrapper for the HTML ``<title>`` fallback extractor."""
    return _extract_html_title(html)


def extract_with_trafilatura(
    page,
    extraction_type: str = "markdown",
    css_selector: Optional[str] = None,
) -> list[str]:
    """Extract content from a Response-like page via trafilatura.

    Chain: binary sniff → CSS narrowing (budgeted, #247) → trafilatura →
    cross-format markdown retry → html2text/markdownify fallback. An explicit
    selector never widens scope: matching elements without extractable text
    produce an empty result for caller-directed retry without the selector.
    """
    if css_selector is not None and (
        not isinstance(css_selector, str) or len(css_selector) > 2000
    ):
        raise ValueError("css_selector must be a string of at most 2000 characters")
    try:
        raw = getattr(page, "body", None) or getattr(page, "content", None)
        if raw:
            raw_bytes = raw if isinstance(raw, bytes) else str(raw).encode("latin-1", errors="replace")
            if b"\x00" in raw_bytes[:1000] or _is_probably_binary(raw_bytes[:4096]):
                return [
                    "[Binary content detected. Cannot extract text from this URL. "
                    f"Content type may be PDF, image, or other non-text format. "
                    f"Size: {len(raw_bytes):,} bytes]"
                ]

        html = _get_html_from_page(page)
        if html is None:
            logger.warning("Cannot extract HTML from page object, falling back to markdownify")
            return _fallback_extract(page, extraction_type, css_selector)

        page_url = getattr(page, "url", "")

        if css_selector:
            # Budgets (#247): cap selected nodes, per-node and aggregate chars.
            if not hasattr(page, "css"):
                return _fallback_extract(page, extraction_type, css_selector)
            selected = page.css(css_selector)
            selected = selected[:_MAX_SELECTED_NODES]
            if not selected:
                return _fallback_extract(page, extraction_type, css_selector)
            parts: list[str] = []
            aggregate = 0
            for el in selected:
                if aggregate >= _MAX_AGGREGATE_SERIALIZED_CHARS:
                    logger.info(
                        "CSS selector output truncated at %d chars (%d node(s) skipped)",
                        aggregate, len(selected) - len(parts),
                    )
                    break
                try:
                    el_html = tostring(
                        el._root if hasattr(el, "_root") else el, encoding="unicode"
                    )
                    if len(el_html) > _MAX_NODE_SERIALIZED_CHARS:
                        el_html = el_html[:_MAX_NODE_SERIALIZED_CHARS]
                    part = _extract_type(el_html, page_url, extraction_type)
                    if part and part.strip():
                        aggregate += len(el_html)
                        parts.append(part)
                except Exception:
                    continue
            if parts:
                return parts
            return []

        result = _extract_type(html, page_url, extraction_type)
        if result:
            return [result]

        if extraction_type != "markdown":
            md = _trafilatura_markdown(html, page_url)
            if md:
                logger.info(
                    "Requested type '%s' failed, returning markdown fallback",
                    extraction_type,
                )
                return [md]

        logger.warning("All Trafilatura methods failed; falling back to markdownify")
        return _fallback_extract(page, extraction_type, css_selector)

    except Exception as exc:
        logger.warning(
            "Trafilatura extraction crashed: %s, falling back to markdownify",
            safe_error(exc, fallback_category="extract")["category"]
        )
        return _fallback_extract(page, extraction_type, css_selector)
