"""Heuristic DOM pruning: drop low-content subtrees without an LLM.

Reconstructed clean-room against Sieve's own tests (tests/test_crawl4ai_ports.py,
tests/test_extraction_budgets.py) and the extraction call sites; no
upstream-derived code.

Approach
--------
Each element gets a content score built from:

* **text density** — serialized-HTML share that is real text,
* **link density** — the share of text inside ``<a>`` elements (nav/footers
  are mostly links),
* **tag weight** — semantic tags (``article``, ``main``, ``p``, headings)
  score higher than generic containers,
* **class/id penalty** — names matching boilerplate keywords
  (nav/footer/sidebar/comment/advert/cookie/popup/banner) reduce the score.

Subtree metrics (serialized length, text length, words, link counts) are
aggregated bottom-up once per node and memoized, so scoring the whole
document is linear in DOM size rather than quadratic (issue #246).

The prune walk is bounded by a node budget (issue #96): when the budget is
exhausted the walk stops and the partially-pruned tree is returned — a
documented partial result, never unbounded CPU work.
"""

from __future__ import annotations

import math
import re

from lxml import html as lh
from lxml.etree import _Element

__all__ = ["prune_html", "filter_content"]

# Names on class/id that mark boilerplate containers.
_BOILERPLATE_NAME_RE = re.compile(
    r"(nav|footer|sidebar|comment|advert|cookie|popup|banner)", re.I
)

# Higher weight = likelier to carry main content.
_TAG_WEIGHTS = {
    "article": 1.5, "main": 1.4, "section": 1.3,
    "h1": 1.2, "h2": 1.1, "h3": 1.0, "p": 1.0,
    "div": 0.5, "li": 0.5, "span": 0.3,
}

# Removed wholesale before scoring: never main content.
_EXCLUDED_TAGS = (
    "script", "style", "noscript", "svg", "iframe",
    "form", "nav", "footer", "header", "aside",
)

# Node budget (issue #96): see module docstring.
_MAX_PRUNE_NODES = 20_000

# Composite-score weights.
_W_DENSITY = 0.4
_W_LINK = 0.2
_W_TAG = 0.2
_W_NAME = 0.1
_W_TEXTLEN = 0.1

# A subtree scoring this far below the threshold is dropped even when it has
# children; only mildly-low leaves are dropped.
_DROP_MARGIN = 0.15
_LEAF_MIN_TEXT = 20


class _SubtreeMetrics:
    """Aggregated subtree measures, computed once per node bottom-up."""

    __slots__ = ("html_len", "text_len", "words", "link_count", "link_text_len")

    def __init__(self, html_len: int, text_len: int, words: int,
                 link_count: int, link_text_len: int) -> None:
        self.html_len = html_len
        self.text_len = text_len
        self.words = words
        self.link_count = link_count
        self.link_text_len = link_text_len


_STATS_CACHE_ATTR = "_sieve_stats_cache"


class _SubtreeStats:
    """Aggregated subtree measures, computed once per node bottom-up."""

    __slots__ = ("html_len", "text_len", "words", "links", "link_text_len")

    def __init__(self, html_len: int, text_len: int, words: int,
                 links: int, link_text_len: int) -> None:
        self.html_len = html_len
        self.text_len = text_len
        self.words = words
        self.links = links
        self.link_text_len = link_text_len


def _own_text(el: _Element) -> str:
    """Text belonging to this element only (text + child tails, no descendants)."""
    return (el.text or "") + "".join((child.tail or "") for child in el)


def _subtree_stats(el: _Element) -> _SubtreeStats:
    """Aggregate child metrics bottom-up, add this element's own contribution.

    Memoized on the element so repeated scoring of the same node (parents are
    scored after children in the prune walk) never re-serializes.
    """
    cached = getattr(el, _STATS_CACHE_ATTR, None)
    if cached is not None:
        return cached

    own = _own_text(el)
    tag = el.tag if isinstance(el.tag, str) else ""
    # Serialized-length estimate: open tag + attributes + close tag.
    attr_chars = sum(1 + len(k) + 1 + len(v or "") for k, v in el.attrib.items())
    open_len = 2 + len(tag) + attr_chars + 1
    close_len = 3 + len(tag) if tag else 0

    html_len = open_len + close_len + len(own)
    text_len = len(own)
    words = len(own.split())
    link_count = 0
    link_text_len = 0

    for child in el:
        if not isinstance(child.tag, str):
            # Comments/PIs contribute tail text but no markup of their own.
            tail = child.tail or ""
            html_len += len(tail)
            text_len += len(tail)
            words += len(tail.split())
            continue
        metrics = _subtree_stats(child)
        html_len += metrics.html_len
        text_len += metrics.text_len
        words += metrics.words
        link_count += metrics.links
        link_text_len += metrics.link_text_len
        if child.tag == "a":
            link_count += 1
            link_text_len += len((child.text_content() or "").strip())

    stats = _SubtreeStats(html_len, text_len, words, link_count, link_text_len)
    try:
        setattr(el, _STATS_CACHE_ATTR, stats)
    except AttributeError:
        pass  # some lxml node types disallow attributes; recompute then
    return stats


def _score_from_stats(el: _Element, stats: _SubtreeStats) -> float:
    """Composite content score for one element from cached subtree metrics."""
    if stats.html_len == 0:
        return 0.0

    text_density = stats.text_len / stats.html_len
    # "link density" here is the NON-link share of text: high is good.
    non_link_share = (
        1.0 - (stats.link_text_len / stats.text_len) if stats.text_len else 1.0
    )
    tag_weight = _TAG_WEIGHTS.get(el.tag if isinstance(el.tag, str) else "", 0.5)

    classes = " ".join((el.get("class") or "").split())
    element_id = el.get("id") or ""
    name_penalty = 0.0
    if _BOILERPLATE_NAME_RE.search(classes):
        name_penalty -= 0.5
    if _BOILERPLATE_NAME_RE.search(element_id):
        name_penalty -= 0.5
    name_component = max(0.0, 0.5 + name_penalty)

    return (
        _W_DENSITY * text_density
        + _W_LINK * max(0.0, non_link_share)
        + _W_TAG * tag_weight
        + _W_NAME * name_component
        + _W_TEXTLEN * (math.log(stats.text_len + 1) / 5)
    )


def _validate_inputs(threshold, min_word_threshold) -> None:
    if (
        isinstance(threshold, bool)
        or not isinstance(threshold, (int, float))
        or not math.isfinite(float(threshold))
        or not 0.0 <= float(threshold) <= 1.0
    ):
        raise ValueError("threshold must be a finite number between 0 and 1")
    if min_word_threshold is not None and (
        isinstance(min_word_threshold, bool)
        or not isinstance(min_word_threshold, int)
        or not 0 <= min_word_threshold <= 100_000
    ):
        raise ValueError("min_word_threshold must be an integer between 0 and 100000")


def _parse(html: str) -> _Element | None:
    try:
        tree = lh.fromstring(html)
    except Exception:
        return None
    if tree.tag == "body":
        return tree
    bodies = tree.xpath("//body")
    if isinstance(bodies, list) and bodies:
        return bodies[0]
    return tree


def prune_html(
    html: str,
    threshold: float = 0.48,
    min_word_threshold: int | None = None,
    *, work_budget=None,
) -> str:
    """Prune low-value subtrees from ``html`` and return the pruned markup.

    Args:
        html: Raw HTML string. Unparseable input is returned unchanged.
        threshold: Elements scoring below this are pruned (0-1).
        min_word_threshold: When set, subtrees with fewer words are dropped.

    Returns:
        The pruned HTML string. The result may be a partial prune when the
        node budget was exhausted mid-walk.
    """
    _validate_inputs(threshold, min_word_threshold)
    if not html or not isinstance(html, str):
        return ""
    from sieve.dom_budget import allow_html_pass, dom_budget
    work_budget = dom_budget(work_budget)
    if not allow_html_pass(html, work_budget):
        return html

    root = _parse(html)
    if root is None:
        return html

    for tag in _EXCLUDED_TAGS:
        for el in root.xpath(f".//{tag}"):
            parent = el.getparent()
            if parent is not None:
                parent.remove(el)

    budget = [_MAX_PRUNE_NODES]

    def _prune(el: _Element) -> None:
        if not work_budget.charge(1):
            return
        budget[0] -= 1
        if budget[0] <= 0:
            return
        for child in list(el):
            if not isinstance(child.tag, str):
                continue
            _prune(child)
        if el is root or budget[0] <= 0:
            return

        metrics = _subtree_stats(el)
        if min_word_threshold and metrics.words < min_word_threshold:
            parent = el.getparent()
            if parent is not None:
                parent.remove(el)
            return

        score = _score_from_stats(el, metrics)
        if score >= threshold:
            return
        parent = el.getparent()
        if parent is None:
            return
        if len(el) == 0 and metrics.text_len < _LEAF_MIN_TEXT:
            parent.remove(el)
        elif score < threshold - _DROP_MARGIN:
            parent.remove(el)

    _prune(root)
    return lh.tostring(root, encoding="unicode")


def filter_content(html: str, threshold: float = 0.48) -> list[str]:
    """Prune ``html`` and return the surviving top-level blocks as strings.

    Companion to :func:`prune_html` for callers that want discrete content
    blocks rather than a re-serialized document.
    """
    pruned = prune_html(html, threshold)
    if not pruned:
        return []
    try:
        tree = lh.fromstring(pruned)
        bodies = tree.xpath("//body")
        root = bodies[0] if bodies else tree
    except Exception:
        return []
    return [
        lh.tostring(child, encoding="unicode")
        for child in root
        if isinstance(child.tag, str) and (child.text_content() or "").strip()
    ]
