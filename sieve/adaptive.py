"""Adaptive selector relocation after a page redesign.

Reconstructed clean-room against Sieve's own tests (tests/test_adaptive.py
pins the exact ``_visible_text`` semantics) and the extraction call sites;
no upstream-derived code.

When a CSS/XPath selector stops matching (redesign, A/B test, dynamic
classes), :func:`relocate` heuristically re-finds the target: every
candidate element is scored on text similarity to the caller's
``original_text_hint`` (token Jaccard and sequence ratio, best of the two),
plus small structural bonuses (tag match, attribute presence). The
highest-scoring element wins when it clears the minimum confidence.

Purity and bounds:

* persistence-free — Sieve owns caching elsewhere;
* stdlib + lxml only;
* candidate scan capped (``_MAX_CANDIDATE_NODES``, issue #45) and similarity
  text capped, so a huge page cannot turn relocation into unbounded work;
* boilerplate-only elements ("sign in", "menu", "copyright", ...) never
  match, no matter how similar the surrounding structure looks.

Typical usage::

    from lxml import html
    from sieve.adaptive import relocate

    root = html.fromstring(page_source)
    element = relocate(root, '.product-price', 'Price: $10')
"""

from __future__ import annotations

import re
from difflib import SequenceMatcher

from lxml.etree import _Element

__all__ = ["relocate"]

# Tags that plausibly carry target content. Elements outside this set are
# still considered when they sit shallow in the tree.
_MEANINGFUL_TAGS = frozenset({
    "a", "article", "aside", "button", "dd", "div", "dl", "dt",
    "h1", "h2", "h3", "h4", "h5", "h6", "label", "li", "main", "nav",
    "ol", "option", "p", "section", "span", "strong", "table", "td",
    "th", "tr", "ul",
})

# Standalone boilerplate text is never a match signal.
_BOILERPLATE_TEXT_RE = re.compile(
    r"^\s*(?:subscribe|sign in|log in|menu|footer|header|copyright"
    r"|all rights reserved|privacy policy|terms of service"
    r"|skip to content|search|close|click here|learn more"
    r"|read more|view all|back to top|home)\s*$",
    re.IGNORECASE,
)

# Attributes worth a small structural bonus.
_KEY_ATTRIBUTES = ("class", "id", "href", "src", "data-id", "aria-label", "role")

# Budgets (#45): candidate scan and similarity text are capped.
_MAX_CANDIDATE_NODES = 2000
_MAX_SIMILARITY_TEXT = 5000

# Input bounds.
_MAX_SELECTOR_CHARS = 2000
_MAX_HINT_CHARS = 20_000

# Minimum average score for a relocation to be trusted.
_MIN_CONFIDENCE = 0.6

# Bonus weights on top of the primary text score.
_BONUS_CONTAINED_TOKEN = 0.15
_BONUS_TAG_MATCH = 0.1
_BONUS_HAS_ATTRS = 0.05
_BONUS_ATTR_TOKEN = 0.1


def _text_tokens(text: str) -> set[str]:
    """Lowercased word tokens, punctuation stripped."""
    cleaned = re.sub(r"[^\w\s-]", " ", text.lower())
    return {token for token in cleaned.split() if token}


def _token_jaccard(a: set[str], b: set[str]) -> float:
    if not a and not b:
        return 1.0
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def _visible_text(element: _Element) -> str:
    """Whitespace-collapsed visible text, nested nodes counted exactly once.

    ``itertext()`` visits each text node and tail exactly once; recursing
    after that would duplicate descendants and distort relocation scores
    (pinned by test_visible_text_counts_nested_text_once).
    """
    parts: list[str] = []
    total = 0
    for part in element.itertext():
        parts.append(part)
        total += len(part)
        if total >= _MAX_SIMILARITY_TEXT:
            break
    return re.sub(r"\s+", " ", " ".join(parts))[:_MAX_SIMILARITY_TEXT].strip()


def _candidate_score(hint_text: str, hint_tokens: set[str], candidate: _Element) -> float:
    """Text similarity with positive structural bonuses, on one fixed scale."""
    visible = _visible_text(candidate)
    if not visible:
        return 0.0
    if _BOILERPLATE_TEXT_RE.match(visible):
        return 0.0

    score = 0.0

    # Primary signal: text similarity.
    if hint_tokens:
        token_score = _token_jaccard(hint_tokens, _text_tokens(visible))
        sequence_score = SequenceMatcher(None, hint_text.lower(), visible.lower()).ratio()
        text_score = max(token_score, sequence_score)
    else:
        text_score = SequenceMatcher(None, hint_text.lower(), visible.lower()).ratio()
    score += text_score

    # Bonus: hint tokens contained in the candidate text.
    if hint_tokens:
        candidate_tokens = _text_tokens(visible)
        if any(token in candidate_tokens for token in hint_tokens):
            score += _BONUS_CONTAINED_TOKEN

        # Bonus: the hint names the candidate's tag (e.g. hint "button Buy").
        tag_candidate = re.split(r"[:\s]", hint_text, maxsplit=1)[0].lower()
        if (
            tag_candidate
            and tag_candidate.isalpha()
            and len(tag_candidate) <= 20
            and candidate.tag == tag_candidate
        ):
            score += _BONUS_TAG_MATCH

    # Bonus: key attributes present (and containing hint tokens).
    attribute_values = [
        candidate.attrib[key].lower() for key in _KEY_ATTRIBUTES if key in candidate.attrib
    ]
    if attribute_values:
        score += _BONUS_HAS_ATTRS
        if hint_tokens:
            joined = " ".join(attribute_values)
            if any(token in joined for token in hint_tokens):
                score += _BONUS_ATTR_TOKEN

    return score


def relocate(
    root: _Element,
    selector: str,
    original_text_hint: str = "",
    *, work_budget=None,
) -> _Element | None:
    """Heuristically relocate an element after its selector stopped matching.

    1. Re-try the original selector (CSS first, then XPath interpretation).
    2. If it still fails, scan meaningful elements for the best text/
       structural match against ``original_text_hint``.

    Args:
        root: Parsed lxml root to search within.
        selector: The CSS/XPath selector that used to match.
        original_text_hint: Known visible text of the target, e.g.
            ``"Price: $10"``. Empty hints fall back to structural scoring.

    Returns:
        The best-matching element, or ``None`` when nothing clears the
        minimum confidence.

    Raises:
        ValueError: on selector/hint length violations.
    """
    if not isinstance(selector, str) or len(selector) > _MAX_SELECTOR_CHARS:
        raise ValueError("selector must be a string of at most 2000 characters")
    if not isinstance(original_text_hint, str) or len(original_text_hint) > _MAX_HINT_CHARS:
        raise ValueError("original_text_hint must be at most 20000 characters")

    hint_text = original_text_hint.strip()
    from sieve.dom_budget import dom_budget, measure_tree
    budget = dom_budget(work_budget)
    measure_tree(root, budget)
    if budget.exhausted:
        return None
    hint_tokens = _text_tokens(hint_text) if hint_text else set()

    # Step 1: the original selector may still work.
    try:
        from lxml.cssselect import CSSSelector

        existing = CSSSelector(selector)(root)
        if existing:
            return existing[0]
    except Exception:
        # fallback boundary: the caller passes an arbitrary historical
        # selector (CSS or XPath, possibly stale); any compile/match failure
        # here just means "still broken", and the heuristic scan below runs.
        pass
    try:
        existing = root.xpath(selector)
        if existing:
            return existing[0]
    except Exception:
        # fallback boundary: same contract as above for XPath-form selectors.
        pass

    # Step 2: heuristic scan.
    best_score = 0.0
    best_element: _Element | None = None
    scanned = 0

    for candidate in root.iter():
        if not budget.charge(1):
            break
        if scanned >= _MAX_CANDIDATE_NODES:
            # Relocation budget exceeded (#45): return the best of what was
            # scanned rather than consuming unbounded CPU.
            break
        scanned += 1

        if candidate.tag not in _MEANINGFUL_TAGS:
            depth = len(list(candidate.iterancestors()))
            if depth > 2 and not _visible_text(candidate):
                continue

        score = _candidate_score(hint_text, hint_tokens, candidate)
        if score > best_score:
            best_score = score
            best_element = candidate

    if best_element is not None and best_score >= _MIN_CONFIDENCE:
        return best_element
    return None
