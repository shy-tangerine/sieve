"""Find-similar list expansion.

Reconstructed clean-room against Sieve's own tests
(tests/test_ports_domain_similar.py, tests/test_dom_budget.py) and the
``sieve extract URL --similar SELECTOR`` call site; no upstream-derived code.

Given a selector, take its FIRST match as the prototype and return the other
elements that share its structural position:

* same depth (equal ancestor count),
* same 3-level tag path (grandparent/parent/self tags),
* attribute and text similarity above ``threshold``.

Each result carries ``{"html", "text", "attributes"}``. ``href``/``src`` are
excluded from similarity by default (list items legitimately differ there).
Text comparison uses ``difflib.SequenceMatcher`` (stdlib only).

Work is bounded (#45/#249): element scans are input-size capped, and the
per-candidate serialize/similarity passes charge a shared
:class:`~sieve.dom_budget.DomWorkBudget` so a pathological page with
thousands of candidates cannot turn a bounded fetch into unbounded work —
matching stops when the budget is spent.
"""

from __future__ import annotations

from difflib import SequenceMatcher

__all__ = ["find_similar"]

# Input bounds (#45): a bounded request must do bounded work.
_MAX_HTML_CHARS = 10_000_000
_MAX_SELECTOR_CHARS = 2000

# Per-candidate text capped for similarity and output.
_MAX_TEXT_CHARS = 500


def _text(element) -> str:
    parts = []
    remaining = _MAX_TEXT_CHARS
    for piece in element.itertext():
        parts.append(piece[:remaining])
        remaining -= len(parts[-1])
        if remaining <= 0:
            break
    return "".join(parts).strip()


def _attributes(element, ignore: set[str]) -> dict:
    """Attribute dict minus ignored keys (href/src by default)."""
    return {k: v for k, v in element.attrib.items() if k not in ignore}


def _alike(
    prototype,
    prototype_attrs: dict,
    candidate,
    ignore: set[str],
    threshold: float,
    match_text: bool,
) -> bool:
    """Structural + attribute (+ optional text) similarity vote.

    Shared checks: same depth, same 3-level tag path, attribute-set overlap.
    Optional checks (when evidence exists): attribute values, text similarity.
    The average of performed checks must meet ``threshold``.
    """
    checks = 0
    score = 0.0

    # Depth equality.
    if len(list(candidate.iterancestors())) == len(list(prototype.iterancestors())):
        score += 1
    checks += 1

    # Tag path: grandparent/parent/self tags.
    def _tag_path(el):
        path = [el.tag]
        parent = el.getparent()
        if parent is not None:
            path.insert(0, parent.tag)
            grandparent = parent.getparent()
            if grandparent is not None:
                path.insert(0, grandparent.tag)
        return tuple(str(t) for t in path)

    if _tag_path(candidate) == _tag_path(prototype):
        score += 1
    checks += 1

    # Attribute names overlap.
    candidate_attrs = _attributes(candidate, ignore)
    if prototype_attrs:
        shared = set(prototype_attrs) & set(candidate_attrs)
        score += len(shared) / len(set(prototype_attrs) | set(candidate_attrs))
    checks += 1

    # Attribute values (exact matches on the retained keys).
    if prototype_attrs:
        matching_values = sum(
            1
            for key, value in prototype_attrs.items()
            if candidate_attrs.get(key) == value
        )
        score += matching_values / len(prototype_attrs)
        checks += 1

    # Optional text similarity.
    if match_text:
        prototype_text = _text(prototype)
        candidate_text = _text(candidate)
        if prototype_text and candidate_text:
            ratio = SequenceMatcher(None, prototype_text, candidate_text).ratio()
        elif not prototype_text and not candidate_text:
            ratio = 1.0
        else:
            ratio = 0.0
        score += ratio
        checks += 1

    if not checks:
        return False
    return round(score / checks, 2) >= threshold


def find_similar(
    html: str,
    selector: str,
    *,
    threshold: float = 0.2,
    ignore_attrs=None,
    match_text: bool = False,
    work_budget=None,
) -> list[dict]:
    """Find elements similar to the first ``selector`` match.

    Args:
        html: Raw HTML string.
        selector: CSS selector whose first match is the prototype.
        threshold: Average similarity (0-1) required to accept a candidate.
        ignore_attrs: Attributes excluded from similarity (default
            ``{"href", "src"}``).
        match_text: Include text similarity in the vote.

    Returns:
        ``[{"html", "text", "attributes"}, ...]`` for each similar element,
        excluding the prototype itself. Unparseable HTML or an invalid
        selector yields ``[]``.
    """
    if not isinstance(html, str) or len(html) > _MAX_HTML_CHARS:
        raise ValueError("html exceeds the find_similar safety limit")
    if not isinstance(selector, str) or not selector or len(selector) > _MAX_SELECTOR_CHARS:
        raise ValueError("selector must be 1-2000 characters")
    if isinstance(threshold, bool) or not isinstance(threshold, (int, float)) or not 0 <= threshold <= 1:
        raise ValueError("threshold must be between 0 and 1")

    ignore = set(ignore_attrs) if ignore_attrs is not None else {"href", "src"}
    from sieve.dom_budget import allow_html_pass, dom_budget, measure_tree
    budget = dom_budget(work_budget)
    if not allow_html_pass(html, budget):
        return []

    from lxml import html as lxml_html
    from lxml.cssselect import CSSSelector

    try:
        tree = lxml_html.fromstring(html)
    except Exception:
        return []
    try:
        matches = CSSSelector(selector)(tree)
    except Exception:
        return []
    if not matches:
        return []

    prototype = matches[0]
    prototype_attrs = _attributes(prototype, ignore)
    depth = len(list(prototype.iterancestors()))

    # Candidates: same depth, same 2-level tag path (parent + self).
    path_parts = [prototype.tag]
    parent = prototype.getparent()
    if parent is not None:
        path_parts.insert(0, parent.tag)
        grandparent = parent.getparent()
        if grandparent is not None:
            path_parts.insert(0, grandparent.tag)
    xpath = "//" + "/".join(path_parts) + f"[count(ancestor::*) = {depth}]"
    try:
        candidates = prototype.xpath(xpath)
    except Exception:
        candidates = []

    # Shared work budget (#249): the tree measurement plus every candidate's
    # serialize/similarity pass charge one budget; stop when it is spent.
    out: list[dict] = []
    from lxml import etree

    for candidate in candidates:
        if candidate is prototype:
            continue
        measure_tree(candidate, budget)
        if budget.exhausted:
            break
        if _alike(prototype, prototype_attrs, candidate, ignore, threshold, match_text):
            try:
                element_html = etree.tostring(candidate, encoding="unicode", method="html")
            except Exception:
                element_html = ""
            out.append({
                "html": element_html,
                "text": _text(candidate),
                "attributes": dict(candidate.attrib or {}),
            })
    return out
