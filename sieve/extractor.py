"""Sieve's own content extraction.

Replaces scrapling's Convertor._extract_content with direct trafilatura +
markdownify + lxml usage. The extraction chain:

1. Trafilatura (primary, for markdown/text/article/structured)
2. markdownify (fallback for markdown/html types)
3. Raw text (last resort: regex tag stripping)

CSS selector narrowing uses lxml directly.
"""

from __future__ import annotations

import logging
import re
from typing import List, Optional

logger = logging.getLogger("sieve.extractor")

# Budgets for CSS-selector narrowed extraction (issue #247): without caps a
# selector matching hundreds of nodes serializes and joins all of them, which
# can produce multi-megabyte strings before any downstream limit applies.
_MAX_SELECTED_NODES = 100
_MAX_NODE_SERIALIZED_CHARS = 100_000
_MAX_AGGREGATE_SERIALIZED_CHARS = 500_000


def _maybe_prune(html: str) -> str:
    try:
        from sieve.pruning import prune_html
        import os
        if os.getenv('SIEVE_PRUNE','0')=='1':
            return prune_html(html)
    except Exception as exc:
        # SIEVE_PRUNE=1 is an explicit operator choice; silently degrading to
        # unpruned content would hide real failures (issue #231).
        logger.warning("HTML pruning failed; using unpruned content: %s", type(exc).__name__)
    return html

def extract_content(
    page,
    extraction_type: str = "markdown",
    css_selector: Optional[str] = None,
    main_content_only: bool = False,
) -> List[str]:
    """Extract content from a Response object.

    Args:
        page: A Response object (sieve.fetcher.Response) or compatible
              object with .body, .encoding, .url, .css()
        extraction_type: 'markdown', 'html', 'text', 'article', 'structured'
        css_selector: CSS selector to narrow extraction scope
        main_content_only: Strip nav/ads/footers

    Returns:
        List of extracted content strings (usually one element).
    """
    from sieve.trafilatura_extractor import extract_with_trafilatura, _fallback_extract

    # Trafilatura is the primary extractor for all text-like types
    if extraction_type in ("markdown", "text", "article", "structured"):
        return extract_with_trafilatura(page, extraction_type=extraction_type, css_selector=css_selector)

    # Fallback: use markdownify for markdown/html, or raw text
    return _fallback_extract(page, extraction_type, css_selector)


def extract_html_content(
    page,
    css_selector: Optional[str] = None,
    main_content_only: bool = False,
) -> str:
    """Extract raw HTML from a page, optionally narrowed by CSS selector.

    Used for extraction_type='html'.
    """
    raw_body = getattr(page, 'body', None)
    encoding = getattr(page, 'encoding', 'utf-8') or 'utf-8'

    if raw_body:
        html = raw_body.decode(encoding, errors="replace")
    elif hasattr(page, 'content'):
        html = page.content
    elif hasattr(page, 'html_content'):
        html = page.html_content
    else:
        return ""

    if css_selector:
        try:
            from lxml import html as lxml_html
            from lxml.etree import tostring
            tree = lxml_html.fromstring(html)
            from lxml.cssselect import CSSSelector
            sel = CSSSelector(css_selector)
            matches = sel(tree)[:_MAX_SELECTED_NODES]
            parts: List[str] = []
            total = 0
            truncated_nodes = 0
            for m in matches:
                if total >= _MAX_AGGREGATE_SERIALIZED_CHARS:
                    truncated_nodes += 1
                    continue
                part = tostring(m, encoding="unicode")
                if len(part) > _MAX_NODE_SERIALIZED_CHARS:
                    part = part[:_MAX_NODE_SERIALIZED_CHARS]
                if total + len(part) > _MAX_AGGREGATE_SERIALIZED_CHARS:
                    part = part[:_MAX_AGGREGATE_SERIALIZED_CHARS - total]
                    truncated_nodes += 1
                total += len(part)
                parts.append(part)
            if truncated_nodes:
                logger.info(
                    "CSS selector output truncated: %d node(s) dropped at budget "
                    "(%d nodes, %d chars)",
                    truncated_nodes, len(parts), total,
                )
            return "\n".join(parts)
        except Exception as e:
            logger.debug(f"CSS selector '{css_selector}' failed: {e}")

    if main_content_only:
        html = _strip_noise_tags(html)

    return html


def _strip_noise_tags(html: str) -> str:
    """Remove script, style, noscript, svg tags from HTML."""
    try:
        from lxml import html as lxml_html
        from lxml.etree import tostring
        tree = lxml_html.fromstring(html)
        for tag in ("script", "style", "noscript", "svg"):
            for el in tree.xpath(f"//{tag}"):
                el.getparent().remove(el)
        return tostring(tree, encoding="unicode")
    except Exception:
        # Regex fallback
        for pattern in (
            r'<script[^>]*>.*?</script>',
            r'<style[^>]*>.*?</style>',
            r'<noscript[^>]*>.*?</noscript>',
        ):
            html = re.sub(pattern, '', html, flags=re.DOTALL | re.IGNORECASE)
        return html


def extract_text_content(
    page,
    css_selector: Optional[str] = None,
    main_content_only: bool = False,
) -> str:
    """Extract plain text from a page (strip all HTML tags)."""
    raw_body = getattr(page, 'body', None)
    encoding = getattr(page, 'encoding', 'utf-8') or 'utf-8'

    if raw_body:
        html = raw_body.decode(encoding, errors="replace")
    elif hasattr(page, 'content'):
        html = page.content
    else:
        return ""

    if css_selector and hasattr(page, 'css'):
        selected = page.css(css_selector)[:_MAX_SELECTED_NODES]
        if selected:
            parts = []
            total = 0
            for el in selected:
                root = getattr(el, '_root', el)
                if hasattr(root, 'text_content'):
                    if total >= 200_000:
                        break
                    piece = root.text_content()
                else:
                    piece = str(root)
                total += len(piece)
                parts.append(piece)
            return " ".join(p for p in parts if p)

    # Strip tags
    text = re.sub(r'<script[^>]*>.*?</script>', '', html, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r'<style[^>]*>.*?</style>', '', text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r'<noscript[^>]*>.*?</noscript>', '', text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r'<[^>]+>', ' ', text)
    # Collapse whitespace
    text = re.sub(r'\s+', ' ', text).strip()
    return text
