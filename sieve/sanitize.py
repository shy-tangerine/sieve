"""HTML sanitization for AI-targeted extraction.

Reconstructed clean-room against Sieve's own tests (tests/test_scrapling_ports.py)
and extraction call sites; no upstream-derived code or attribution.

Two layers, both operating on parsed lxml trees and returning a new tree
(the input is never mutated):

* :func:`strip_noise_tags` — drop script/style/noscript/svg subtrees.
* :func:`sanitize_for_ai` — remove content an LLM should never see as page
  content: visually hidden nodes (a prompt-injection vector), HTML comments,
  zero-width characters, and C0 control characters.

:func:`sanitize_html` chains both over a raw HTML string and is the usual
entry point.
"""

from __future__ import annotations

import re
from copy import deepcopy

from lxml import html
from lxml.etree import XPath

__all__ = [
    "strip_noise_tags",
    "sanitize_for_ai",
    "sanitize_html",
]

# Element types dropped outright.
_NOISE_TAGS = {"script", "style", "noscript", "svg"}

# Comments never belong in AI-facing content.
_FORBIDDEN_NODE_TYPES = (html.HtmlComment,)

# Selectors matching elements invisible to a human reader. Hidden text is a
# classic indirect-prompt-injection channel, so anything matching is removed.
_HIDDEN_XPATH = XPath(
    # Inline styles that hide the element (with and without the space).
    './/*[contains(@style,"display:none") or contains(@style,"display: none")'
    ' or contains(@style,"visibility:hidden") or contains(@style,"visibility: hidden")'
    ' or contains(@style,"opacity:0") or contains(@style,"opacity: 0")'
    ' or contains(@style,"font-size:0") or contains(@style,"font-size: 0")'
    ' or contains(@style,"height:0") or contains(@style,"height: 0")'
    ' or contains(@style,"width:0") or contains(@style,"width: 0")]'
    # Explicitly marked hidden, plus inert template content.
    " | .//*[@aria-hidden='true']"
    " | .//template"
)

# Zero-width and bidi-isolation characters: invisible but token-relevant.
_ZERO_WIDTH_RE = re.compile(r"[\u200b\u200c\u200d\ufeff\u2060\u180e]")
# C0 control characters except tab (0x09); newline/CR are handled elsewhere.
_CONTROL_CHARS_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")


def _clean_text_value(value: str) -> str:
    return _CONTROL_CHARS_RE.sub("", _ZERO_WIDTH_RE.sub("", value))


def strip_noise_tags(root):
    """Remove script/style/noscript/svg subtrees; return a new root."""
    clean = deepcopy(root)
    for el in list(clean.iter(*_NOISE_TAGS)):
        el.drop_tree()
    return clean


def sanitize_for_ai(root):
    """Strip hidden nodes, comments, and invisible characters; return a new root."""
    clean = deepcopy(root)

    for el in list(_HIDDEN_XPATH(clean)):
        el.drop_tree()

    for el in clean.iter():
        if el.text:
            el.text = _clean_text_value(el.text)
        if el.tail:
            el.tail = _clean_text_value(el.tail)

    # Comments: drop the node but keep any tail text attached to it so
    # surrounding words are not glued together.
    for node in list(clean.iter()):
        if isinstance(node, _FORBIDDEN_NODE_TYPES):
            parent = node.getparent()
            if parent is None:
                continue
            if node.tail:
                previous = node.getprevious()
                if previous is not None:
                    previous.tail = (previous.tail or "") + node.tail
                else:
                    parent.text = (parent.text or "") + node.tail
            parent.remove(node)
    return clean


def sanitize_html(
    html_str: str, *, strip_noise: bool = True, for_ai: bool = True
) -> str:
    """Sanitize a raw HTML string.

    Args:
        html_str: Raw HTML to clean.
        strip_noise: Drop script/style/noscript/svg subtrees.
        for_ai: Additionally strip hidden nodes, comments, and invisible
            characters.

    Returns:
        The cleaned HTML. Input that fails to parse is returned unchanged.
    """
    try:
        root = html.fromstring(html_str)
    except Exception:
        return html_str
    if strip_noise:
        root = strip_noise_tags(root)
    if for_ai:
        root = sanitize_for_ai(root)
    from lxml.etree import tostring

    return tostring(root, encoding="unicode", method="html")
