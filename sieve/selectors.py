"""Selector auto-generation: minimal CSS and XPath paths for an element.

Reconstructed clean-room against Sieve's own tests
(tests/test_scrapling_ports.py) and the extraction/adaptive call sites; no
upstream-derived code.

Given a parsed lxml element, walk up the ancestor chain and build the
shortest selector that re-matches it:

* an ``id`` attribute terminates the path immediately (``#id`` in CSS,
  ``//*[@id='id']`` in XPath) — ids are unique by contract;
* otherwise the tag name is used, suffixed with ``:nth-of-type(n)`` (CSS) or
  ``[n]`` (XPath) only when the element is not the unique tag among its
  siblings;
* the walk stops at ``<html>``, whose root is implied in both selector
  languages.

``full_path=True`` variants skip the id short-circuit and emit the complete
ancestor chain. :func:`selector_hint` ties it together for operators: match
a CSS selector against an HTML sample and return the generated CSS/XPath
forms of the first match.
"""

from __future__ import annotations

from lxml import html as lhtml
from lxml.etree import _Element

__all__ = [
    "generate_css_selector",
    "generate_full_css_selector",
    "generate_xpath_selector",
    "generate_full_xpath_selector",
    "selector_hint",
]


def _is_text_node(node) -> bool:
    """Comments/PIs have non-string tags; they never take part in paths."""
    return not isinstance(getattr(node, "tag", None), str)


def _sibling_position(element: _Element, parent: _Element) -> tuple[int, int]:
    """(1-based index among same-tag siblings, count of same-tag siblings)."""
    position = 0
    total = 0
    for child in parent:
        if _is_text_node(child):
            continue
        if child.tag == element.tag:
            total += 1
            if child is element:
                position = total
    return position, total


def _css_identifier(value: str) -> str:
    return "".join(
        char if (char.isalnum() or char in "_-") and not (
            char.isdigit() and (index == 0 or (index == 1 and value[0] == "-"))
        ) and not (char == "-" and len(value) == 1)
        else f"\\{ord(char):x} "
        for index, char in enumerate(value)
    )


def _xpath_literal(value: str) -> str:
    if "'" not in value:
        return f"'{value}'"
    if '"' not in value:
        return f'"{value}"'
    return "concat(" + ', "\'", '.join(f"'{part}'" for part in value.split("'")) + ")"


def _build_selector(
    element: _Element, selection: str = "css", full_path: bool = False
) -> str:
    """Walk ancestors building the selector; CSS joins with `` > ``, XPath
    with ``/`` under a ``//`` root."""
    if _is_text_node(element):
        return ""

    css = selection.lower() == "css"
    path_parts: list[str] = []
    target: _Element | None = element

    while target is not None:
        parent = target.getparent()
        attributes = target.attrib
        if attributes.get("id") and not full_path:
            # Unique id: terminate immediately.
            identifier = attributes["id"]
            part = f"#{_css_identifier(identifier)}" if css else f"*[@id={_xpath_literal(identifier)}]"
            path_parts.append(part)
            break

        if parent is None:
            break

        part = str(target.tag)
        position, same_tag_count = _sibling_position(target, parent)
        if same_tag_count > 1 and position > 0:
            part += (
                f":nth-of-type({position})" if css else f"[{position}]"
            )
        path_parts.append(part)

        target = parent
        if target is not None and target.tag == "html":
            # <html> is the implicit root in both selector languages.
            break

    if css:
        return " > ".join(reversed(path_parts))
    return "//" + "/".join(reversed(path_parts))


def generate_css_selector(element: _Element) -> str:
    """Shortest CSS selector for ``element`` (id short-circuit)."""
    return _build_selector(element, "css", full_path=False)


def generate_full_css_selector(element: _Element) -> str:
    """Complete ancestor-chain CSS selector (no id short-circuit)."""
    return _build_selector(element, "css", full_path=True)


def generate_xpath_selector(element: _Element) -> str:
    """Shortest XPath selector for ``element`` (id short-circuit)."""
    return _build_selector(element, "xpath", full_path=False)


def generate_full_xpath_selector(element: _Element) -> str:
    """Complete ancestor-chain XPath selector (no id short-circuit)."""
    return _build_selector(element, "xpath", full_path=True)


def selector_hint(html_str: str, css_selector: str) -> dict:
    """Match ``css_selector`` against ``html_str`` and return generated forms.

    Returns ``{"ok": True, "css", "xpath", "full_css", "full_xpath"}`` for
    the first match, or ``{"ok": False, "error": "no match"}``. Raises
    ``ValueError`` for a malformed selector argument.
    """
    if not isinstance(css_selector, str) or not css_selector or len(css_selector) > 2000:
        raise ValueError("css_selector must be 1-2000 characters")
    tree = lhtml.fromstring(html_str)
    from lxml.cssselect import CSSSelector
    from cssselect import SelectorError

    try:
        matches = CSSSelector(css_selector)(tree)
    except SelectorError as exc:
        raise ValueError("css_selector is malformed") from exc
    if not matches:
        return {"ok": False, "error": "no match"}
    element = matches[0]
    return {
        "ok": True,
        "css": generate_css_selector(element),
        "xpath": generate_xpath_selector(element),
        "full_css": generate_full_css_selector(element),
        "full_xpath": generate_full_xpath_selector(element),
    }
