"""Declarative structured extraction from raw HTML.

Reconstructed clean-room against Sieve's own tests (tests/test_scrapegraph_ports.py,
tests/test_crawl4ai_ports.py), the MCP ``extract``/``extract_jsonl`` handlers,
and ``sieve extract URL --schema``; no upstream-derived code.

Schema format
-------------
::

    {
        "baseSelector": "div.product",
        "fields": [
            {"name": "title", "selector": "h2.name", "type": "text"},
            {"name": "link", "selector": "a", "type": "attribute", "attribute": "href"},
            {"name": "summary", "selector": ".desc", "type": "html"},
            {"name": "amount", "selector": "span.price", "type": "regex",
             "pattern": "\\\\$([0-9.]+)", "group": 1},
            {"name": "details", "selector": ".details", "type": "nested",
             "fields": [{"name": "id", "selector": ".id", "type": "text"}]}
        ]
    }

``type`` is a single step or a list of steps applied left-to-right over the
selected element: ``text``, ``attribute``, ``html``, ``regex``, ``nested``.
Optional ``transform`` values: ``lowercase``, ``uppercase``, ``strip``.

Schemas arrive from operators (files, LLM output) and may be slightly
malformed: :func:`parse_relaxed_json` tolerates trailing commas and
``//``/``/* */`` comments, and :func:`normalize_schema` accepts common key
aliases (``base_selector``, ``css``/``xpath`` for ``selector``).

JSONL mode returns newline-delimited compact JSON objects where every schema
field is present with ``null`` for missing values (explicit-nulls rule), so
``pandas.read_json(lines=True)`` and ``DuckDB read_json_auto`` see a stable
shape.
"""

from __future__ import annotations

import json
import re
from typing import Any

from lxml import etree, html
from lxml.cssselect import CSSSelector

__all__ = [
    "extract_json",
    "extract_jsonl",
    "validate_schema",
    "normalize_schema",
    "parse_relaxed_json",
]

# Valid field ``type`` steps.
_VALID_FIELD_TYPES = {"text", "attribute", "html", "regex", "nested"}

# Upper bound for a single XPath selector (untrusted operator input).
_MAX_XPATH_CHARS = 2048


# ── tolerant JSON (trailing commas, // and /* */ comments) ──────────────────

def _strip_json_comments(text: str) -> str:
    """Remove ``//`` and ``/* */`` comments, leaving string contents intact."""
    out: list[str] = []
    i = 0
    n = len(text)
    in_string = False
    escaped = False
    while i < n:
        ch = text[i]
        if in_string:
            out.append(ch)
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            i += 1
            continue
        if ch == '"':
            in_string = True
            out.append(ch)
            i += 1
            continue
        if ch == "/" and i + 1 < n and text[i + 1] == "/":
            end = text.find("\n", i)
            if end == -1:
                break
            i = end
            continue
        if ch == "/" and i + 1 < n and text[i + 1] == "*":
            end = text.find("*/", i + 2)
            if end == -1:
                break
            i = end + 2
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def _strip_trailing_commas(text: str) -> str:
    """Remove commas directly before ``}`` or ``]`` (outside strings)."""
    out: list[str] = []
    i = 0
    n = len(text)
    in_string = False
    escaped = False
    while i < n:
        ch = text[i]
        if in_string:
            out.append(ch)
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            i += 1
            continue
        if ch == '"':
            in_string = True
            out.append(ch)
            i += 1
            continue
        if ch == ",":
            j = i + 1
            while j < n and text[j] in " \t\n\r":
                j += 1
            if j < n and text[j] in "}]":
                i += 1
                continue
        out.append(ch)
        i += 1
    return "".join(out)


def parse_relaxed_json(text: str) -> dict:
    """Parse schema JSON tolerating trailing commas and comments.

    Strict JSON is tried first. On failure the tolerant cleanup runs. The
    result must be a JSON object (dict).
    """
    cleaned = text.strip()
    try:
        value = json.loads(cleaned)
        if not isinstance(value, dict):
            raise ValueError("schema JSON must be an object")
        return value
    except json.JSONDecodeError:
        pass
    cleaned = _strip_trailing_commas(_strip_json_comments(cleaned))
    value = json.loads(cleaned)
    if not isinstance(value, dict):
        raise ValueError("schema JSON must be an object")
    return value


def normalize_schema(schema: dict) -> dict:
    """Normalize relaxed schema key aliases into the canonical shape.

    ``base_selector``/``base-selector``/``base`` become ``baseSelector``;
    per-field ``css``/``cssSelector``/``css_selector``/``xpath``/``path``
    become ``selector``.
    """
    if not isinstance(schema, dict):
        return schema
    out = dict(schema)
    if "baseSelector" not in out:
        for key in ("base_selector", "base-selector", "base"):
            if key in out:
                out["baseSelector"] = out.pop(key)
                break
    fields = out.get("fields")
    if isinstance(fields, list):
        normalized: list[Any] = []
        for field in fields:
            if not isinstance(field, dict):
                normalized.append(field)
                continue
            entry = dict(field)
            if "selector" not in entry:
                for key in ("css", "cssSelector", "css_selector", "xpath", "path"):
                    if key in entry:
                        entry["selector"] = entry.pop(key)
                        break
            normalized.append(entry)
        out["fields"] = normalized
    return out


# ── parsing and selection helpers ────────────────────────────────────────────

def _parse_html(html_content: str):
    """Parse HTML into an lxml tree with error recovery; fragments get an
    ``<html>`` wrapper so relative selectors behave consistently."""
    try:
        parser = etree.HTMLParser(recover=True, remove_blank_text=True)
        tree = etree.fromstring(html_content, parser)
    except Exception:
        try:
            tree = html.fromstring(html_content)
        except Exception:
            return etree.Element("html")
    if not isinstance(tree.tag, str) or tree.tag.lower() != "html":
        wrapper = etree.Element("html")
        wrapper.append(tree)
        return wrapper
    return tree


def _get_text(element) -> str:
    """Normalized text: all descendant text nodes, whitespace-collapsed."""
    try:
        return " ".join(t.strip() for t in element.xpath(".//text()") if t.strip())
    except Exception:
        try:
            return element.text_content().strip()
        except Exception:
            return ""


def _get_html(element) -> str:
    """Serialize an element back to HTML."""
    try:
        return etree.tostring(element, encoding="unicode", method="html")
    except Exception:
        try:
            return etree.tostring(element, encoding="unicode")
        except Exception:
            return ""


def _select_css(scope, selector: str) -> list:
    try:
        return list(CSSSelector(selector)(scope))
    except Exception:
        return []


def _select_xpath(scope, selector: str) -> list:
    """Evaluate a bounded XPath selector, returning no matches on invalid input."""
    if not isinstance(selector, str) or len(selector) > _MAX_XPATH_CHARS:
        return []
    try:
        return scope.xpath(selector)
    except Exception:
        return []


def _select(scope, selector: str, xpath_mode: bool) -> list:
    if xpath_mode:
        results = _select_xpath(scope, selector)
        if results:
            return results
        return _select_css(scope, selector)
    return _select_css(scope, selector)


def _apply_transform(value: Any, transform: str) -> Any:
    if transform == "lowercase" and isinstance(value, str):
        return value.lower()
    if transform == "uppercase" and isinstance(value, str):
        return value.upper()
    if transform == "strip" and isinstance(value, str):
        return value.strip()
    return value


# ── field extraction ─────────────────────────────────────────────────────────

def _resolve_source(element, field: dict[str, Any]):
    """Resolve an optional ``source`` reference before selector scoping.

    ``"+tag.class"`` selects the first following sibling with that tag and
    classes. Returns ``None`` when the reference matches nothing.
    """
    source = field.get("source")
    if not source:
        return element
    source = source.strip()
    if not source.startswith("+"):
        return element
    parts = source[1:].strip().split(".")
    tag = parts[0].strip() or "*"
    classes = [p.strip() for p in parts[1:] if p.strip()]
    xpath = f"./following-sibling::{tag}"
    for cls in classes:
        xpath += f"[contains(concat(' ',normalize-space(@class),' '),' {cls} ')]"
    xpath += "[1]"
    results = element.xpath(xpath)
    return results[0] if results else None


def _extract_field(element, field: dict[str, Any], xpath_mode: bool) -> Any:
    """Extract one field value from an element, following the type pipeline."""
    resolved = _resolve_source(element, field)
    if resolved is None:
        return field.get("default")

    type_pipeline = field["type"]
    if not isinstance(type_pipeline, list):
        type_pipeline = [type_pipeline]

    target = resolved
    if "selector" in field:
        selected = _select(resolved, field["selector"], xpath_mode)
        if not selected:
            return field.get("default")
        target = selected[0]

    value: Any = target
    for step in type_pipeline:
        if step == "text":
            value = _get_text(value)
        elif step == "attribute":
            value = value.get(field.get("attribute", "")) if hasattr(value, "get") else None
        elif step == "html":
            value = _get_html(value)
        elif step == "regex":
            pattern = field.get("pattern")
            if pattern and isinstance(value, str):
                match = re.search(pattern, value)
                value = match.group(field.get("group", 1)) if match else None
            else:
                value = None
        elif step == "nested":
            nested_list = _select(resolved, field.get("selector", "."), xpath_mode)
            nested = nested_list[0] if nested_list else None
            if nested is not None:
                item: dict[str, Any] = {}
                for nested_field in field.get("fields", []):
                    nested_value = _extract_field(nested, nested_field, xpath_mode)
                    if nested_value is not None:
                        item[nested_field["name"]] = nested_value
                value = item
            else:
                value = {}
        if value is None:
            break

    if "transform" in field:
        value = _apply_transform(value, field["transform"])

    return value if value is not None else field.get("default")


def _extract_item(element, fields: list[dict[str, Any]], xpath_mode: bool) -> dict[str, Any]:
    """Extract all schema fields from one element.

    Every declared field name is present in the result (explicit-nulls rule)
    so JSONL and dict outputs always agree on shape.
    """
    item: dict[str, Any] = {}
    for field in fields:
        if field.get("type") == "computed":
            continue
        name = field.get("name")
        if not name:
            continue
        try:
            value = _extract_field(element, field, xpath_mode)
        except Exception:
            value = None
        item[name] = value
    return item


# ── validation ───────────────────────────────────────────────────────────────

def validate_schema(schema: dict) -> list[str]:
    """Validate a declarative extraction schema.

    Returns a list of human-readable error strings; empty means valid.
    Never raises.
    """
    errors: list[str] = []
    if not isinstance(schema, dict):
        errors.append("Schema must be a dict.")
        return errors

    if "baseSelector" in schema:
        base = schema["baseSelector"]
        if not isinstance(base, str) or not base.strip():
            errors.append("baseSelector must be a non-empty string.")

    fields = schema.get("fields")
    if not isinstance(fields, list) or len(fields) == 0:
        errors.append("fields must be a non-empty list.")
        return errors

    for i, field in enumerate(fields):
        prefix = f"fields[{i}]"
        if not isinstance(field, dict):
            errors.append(f"{prefix}: field must be a dict.")
            continue
        name = field.get("name")
        if not isinstance(name, str) or not name.strip():
            errors.append(f"{prefix}: 'name' must be a non-empty string.")
        if "selector" not in field or not isinstance(field.get("selector"), str):
            errors.append(f"{prefix}: 'selector' must be a string.")
        ftype = field.get("type")
        if ftype is None:
            continue
        if isinstance(ftype, list):
            bad = [t for t in ftype if t not in _VALID_FIELD_TYPES]
            if bad:
                errors.append(
                    f"{prefix}: unknown type value(s): {bad!r}. "
                    f"Valid types: {sorted(_VALID_FIELD_TYPES)}."
                )
        elif isinstance(ftype, str):
            if ftype not in _VALID_FIELD_TYPES:
                errors.append(
                    f"{prefix}: unknown type {ftype!r}. "
                    f"Valid types: {sorted(_VALID_FIELD_TYPES)}."
                )
        else:
            errors.append(f"{prefix}: 'type' must be a string or list of strings.")
    return errors


# ── public extraction API ────────────────────────────────────────────────────

def extract_json(
    html: str,
    schema: dict[str, Any],
    *,
    xpath: bool = False,
    jsonl: bool = False,
    work_budget=None,
) -> list[dict] | str:
    """Extract structured records from HTML using a declarative schema.

    Args:
        html: Raw HTML string.
        schema: Schema with ``baseSelector`` and ``fields``.
        xpath: Interpret field selectors as XPath instead of CSS.
        jsonl: Return a newline-delimited JSON string (explicit nulls).

    Returns:
        List of record dicts, or the JSONL string when ``jsonl=True``.
        Empty input yields ``""`` (jsonl) or ``[]``.
    """
    if not html or not schema:
        return "" if jsonl else []
    from sieve.dom_budget import allow_html_pass, dom_budget, measure_tree
    budget = dom_budget(work_budget)
    if not allow_html_pass(html, budget):
        return "" if jsonl else []

    fields = schema.get("fields", [])
    tree = _parse_html(html)
    base_elements = _select(tree, schema.get("baseSelector", ""), xpath)

    items = []
    from sieve.resource_budget import current_budget
    account = current_budget()
    for element in base_elements:
        measure_tree(element, budget)
        if budget.exhausted or (account is not None and not account.charge("items", 1)):
            break
        items.append(_extract_item(element, fields, xpath))
    if jsonl:
        return "\n".join(json.dumps(item, separators=(",", ":")) for item in items)
    return items


def extract_jsonl(
    html: str, schema: dict[str, Any], *, xpath: bool = False
) -> str:
    """Extract records as JSON-lines (see :func:`extract_json`)."""
    return extract_json(html, schema, xpath=xpath, jsonl=True)
