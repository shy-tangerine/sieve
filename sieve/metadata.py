"""Page metadata extraction for Sieve.

Enriches every HTML fetch response with structured metadata an agent can use to
judge relevance and cite sources: title, description, site name, type, image,
canonical URL, language, published time, and author. Pulled from OpenGraph meta
tags, JSON-LD blocks, the canonical link, and the <title> tag.

Kept dependency-free (regex + json) so it runs cheaply on every fetch. Only
populated for HTML pages; JSON / PDF / image responses get an empty dict.
"""

from __future__ import annotations

import json
import logging
import re
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urljoin

logger = logging.getLogger("master-fetch.metadata")

# Match <meta property/name="KEY" content="VAL"> in either attribute order.
_META_RE = re.compile(
    r'<meta\b[^>]*?(?:property|name)=["\']([^"\']+)["\'][^>]*?content=["\']([^"\']*)["\']',
    re.IGNORECASE,
)
_META_RE_REV = re.compile(
    r'<meta\b[^>]*?content=["\']([^"\']*)["\'][^>]*?(?:property|name)=["\']([^"\']+)["\']',
    re.IGNORECASE,
)
_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)
_CANONICAL_RE = re.compile(
    r'<link\b[^>]*?rel=["\']canonical["\'][^>]*?href=["\']([^"\']+)["\']',
    re.IGNORECASE,
)
_LANG_RE = re.compile(r'<html\b[^>]*?\blang=["\']([^"\']+)["\']', re.IGNORECASE)
_LD_RE = re.compile(
    r'<script\b[^>]*?type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
    re.IGNORECASE | re.DOTALL,
)
_IMG_RE = re.compile(
    r'<img\b[^>]*?\bsrc=["\']([^"\']+)["\']',
    re.IGNORECASE,
)
_ASSET_SKIP = ("data:image",)
_STRUCTURED_MAX_HTML = 2_000_000
_STRUCTURED_MAX_ENTITIES = 100
# (#85) Budget for the broad metadata regex passes: scan a bounded prefix of
# the document and cap tag iterations so adversarial HTML cannot make the
# DOTALL scans consume unbounded time/memory.
_MAX_SCAN_CHARS = 1_000_000
_MAX_META_TAGS = 500
_MAX_IMAGES = 1000
# (#185) Shape budgets for nested structured_data: a bounded document must not
# expand into disproportionately deep/wide nested output.
_MAX_ENTITY_DEPTH = 8
_MAX_KEYS_PER_OBJECT = 64
_MAX_ARRAY_LEN = 128
_MAX_ENTITY_STRING = 2000

# Map meta keys to our flat field names. First match wins per field (OpenGraph
# takes priority over Twitter/Dublin Core, etc.).
_KEY_MAP = {
    "og:title": "title", "twitter:title": "title",
    "og:description": "description", "description": "description", "twitter:description": "description",
    "og:site_name": "site_name",
    "og:type": "type",
    "og:image": "image", "twitter:image": "image",
    "og:url": "og_url",
    "article:published_time": "published_time",
    "article:modified_time": "modified_time",
    "article:author": "author", "author": "author",
}


def extract_metadata(html: str, url: str, *, work_budget=None) -> dict[str, Any]:
    """Extract a flat metadata dict from an HTML string. Empty if no HTML."""
    meta: dict[str, Any] = {}
    if not html:
        return meta
    from sieve.dom_budget import allow_html_pass, dom_budget
    if not allow_html_pass(html[:_STRUCTURED_MAX_HTML], dom_budget(work_budget)):
        return meta
    scan_html = html[:_MAX_SCAN_CHARS]

    # OpenGraph / meta tags (check both attribute orders).
    seen_tags = 0
    for rx in (_META_RE, _META_RE_REV):
        for m in rx.finditer(scan_html):
            seen_tags += 1
            if seen_tags > _MAX_META_TAGS:
                break
            if rx is _META_RE:
                key, val = m.group(1), m.group(2)
            else:  # reversed: group(1)=content, group(2)=key
                key, val = m.group(2), m.group(1)
            key = key.lower().strip()
            val = val.strip()
            if not val:
                continue
            field = _KEY_MAP.get(key)
            if field and field not in meta:
                meta[field] = val[:500]

    # <title> fallback.
    if "title" not in meta:
        t = _TITLE_RE.search(scan_html)
        if t:
            title = re.sub(r"\s+", " ", t.group(1)).strip()
            if title:
                meta["title"] = title[:500]

    # Canonical URL.
    c = _CANONICAL_RE.search(scan_html)
    if c:
        try:
            meta["canonical"] = urljoin(url, c.group(1).strip())
        except Exception:
            pass

    # html lang.
    if "lang" not in meta:
        l = _LANG_RE.search(scan_html)
        if l:
            meta["lang"] = l.group(1).strip()[:100]

    # JSON-LD: retain the source entities as well as extracting common citation
    # fields below.  This is deliberately a tolerant pass: one malformed block
    # must not discard metadata from the rest of the page.
    structured_data = extract_structured_data(html)
    if structured_data:
        meta["structured_data"] = structured_data

    # JSON-LD: datePublished / author / description / headline.
    ld_blocks = 0
    for m in _LD_RE.finditer(scan_html):
        ld_blocks += 1
        if ld_blocks > _MAX_META_TAGS:
            break
        raw = m.group(1).strip()
        if not raw:
            continue
        try:
            data = json.loads(raw)
        except Exception:
            continue
        objs = data if isinstance(data, list) else [data]
        for obj in objs:
            if not isinstance(obj, dict):
                continue
            if "published_time" not in meta:
                d = obj.get("datePublished") or obj.get("dateCreated")
                if d:
                    meta["published_time"] = str(d)[:20]
            if "title" not in meta:
                h = obj.get("headline") or obj.get("name")
                if h:
                    meta["title"] = str(h)[:500]
            if "description" not in meta:
                d = obj.get("description")
                if d:
                    meta["description"] = str(d)[:300]
            if "author" not in meta:
                a = obj.get("author")
                if isinstance(a, dict):
                    a = a.get("name")
                elif isinstance(a, list) and a:
                    a0 = a[0]
                    a = a0.get("name") if isinstance(a0, dict) else a0
                if a:
                    meta["author"] = str(a)[:200]

    return meta


def extract_structured_data(html: str) -> list[dict[str, Any]]:
    """Return JSON-LD entity objects found in *html*.

    JSON-LD permits a top-level object, an array of objects, and an object with
    an ``@graph`` array.  Preserve each entity's fields so callers can consume
    Product, Recipe, Event, or other Schema.org types without a type-specific
    scraper.  Malformed blocks are skipped and logged at debug level.
    """
    if not html:
        return []
    # Structured markup is untrusted page input. Keep parsing bounded even if a
    # caller bypasses the normal fetch size limits.
    html = html[:_STRUCTURED_MAX_HTML]
    entities: list[dict[str, Any]] = []

    def add(value: Any) -> None:
        if isinstance(value, list):
            for item in value:
                add(item)
        elif isinstance(value, dict):
            if len(entities) >= _STRUCTURED_MAX_ENTITIES:
                return
            graph = value.get("@graph")
            if isinstance(graph, list):
                for item in graph:
                    add(item)
            else:
                entities.append(value)

    def cap_shape(value: Any, depth: int = 0) -> Any:
        """Recursively bound nesting depth, collection sizes and strings (#185)."""
        if depth > _MAX_ENTITY_DEPTH:
            return None
        if isinstance(value, dict):
            out = {}
            for i, (k, v) in enumerate(value.items()):
                if i >= _MAX_KEYS_PER_OBJECT:
                    break
                child = cap_shape(v, depth + 1)
                if child is not None or v is None:
                    out[k] = child
            return out
        if isinstance(value, list):
            return [cap_shape(v, depth + 1) for v in value[:_MAX_ARRAY_LEN]]
        if isinstance(value, str) and len(value) > _MAX_ENTITY_STRING:
            return value[:_MAX_ENTITY_STRING] + "…[truncated]"
        return value

    for match in _LD_RE.finditer(html):
        raw = match.group(1).strip()
        if not raw:
            continue
        if len(raw) > 500_000:
            logger.debug("oversized JSON-LD block skipped")
            continue
        try:
            capped = cap_shape(json.loads(raw))
            if capped is not None:
                add(capped)
        except (json.JSONDecodeError, TypeError) as exc:
            logger.debug("malformed JSON-LD skipped: %s", exc)
    if len(entities) < _STRUCTURED_MAX_ENTITIES:
        entities.extend(_extract_microdata(html, _STRUCTURED_MAX_ENTITIES - len(entities)))
    return entities[:_STRUCTURED_MAX_ENTITIES]


class _MicroNode:
    __slots__ = ("tag", "attrs", "children", "text")

    def __init__(self, tag: str, attrs: dict[str, str]):
        self.tag = tag
        self.attrs = attrs
        self.children: list[_MicroNode] = []
        self.text: list[str] = []


class _MicrodataParser(HTMLParser):
    """Small, tolerant tree builder for the Schema.org microdata subset."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = _MicroNode("__root__", {})
        self._stack = [self.root]

    def handle_starttag(self, tag, attrs):
        node = _MicroNode(tag.lower(), {k.lower(): (v or "") for k, v in attrs})
        self._stack[-1].children.append(node)
        if tag.lower() not in {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}:
            self._stack.append(node)

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if self._stack[-1].tag == tag.lower():
            self._stack.pop()

    def handle_endtag(self, tag):
        tag = tag.lower()
        for i in range(len(self._stack) - 1, 0, -1):
            if self._stack[i].tag == tag:
                del self._stack[i:]
                break

    def handle_data(self, data):
        self._stack[-1].text.append(data)


def _extract_microdata(html: str, limit: int) -> list[dict[str, Any]]:
    parser = _MicrodataParser()
    try:
        parser.feed(html)
        parser.close()
    except Exception as exc:
        logger.debug("malformed microdata skipped: %s", exc)
        return []

    def descendants(node):
        for child in node.children:
            yield child
            # Nested items are consumed as one property value by value(); do
            # not leak their fields into the enclosing item.
            if child.attrs.get("itemscope") is None:
                yield from descendants(child)

    def value(node):
        if node.attrs.get("itemscope") is not None:
            item = build(node)
            return item
        attr = {"meta": "content", "audio": "src", "embed": "src", "iframe": "src", "img": "src", "source": "src", "track": "src", "a": "href", "area": "href", "link": "href", "object": "data", "data": "value", "meter": "value", "time": "datetime"}.get(node.tag)
        if attr and node.attrs.get(attr):
            return node.attrs[attr][:500]
        text = " ".join("".join(node.text).split())
        for child in node.children:
            text = " ".join([text, " ".join(str(value(child)).split())]).strip()
        return text[:500]

    def build(node):
        out: dict[str, Any] = {}
        itemtype = node.attrs.get("itemtype", "").split()
        if itemtype:
            out["@type"] = itemtype[0].rsplit("/", 1)[-1]
        if node.attrs.get("itemid"):
            out["@id"] = node.attrs["itemid"][:500]
        for child in descendants(node):
            props = child.attrs.get("itemprop", "").split()
            if not props:
                continue
            val = value(child)
            for prop in props:
                if not prop or prop in out and not isinstance(out[prop], list):
                    if prop in out:
                        out[prop] = [out[prop], val]
                    continue
                out[prop] = val
        return out

    roots = [n for n in descendants(parser.root) if n.attrs.get("itemscope") is not None and not n.attrs.get("itemprop")]
    return [item for item in (build(n) for n in roots[:limit]) if item.get("@type") or len(item) > 1]


def extract_image_urls(html: str, url: str, max_n: int = 20) -> list[str]:
    """Extract absolute image URLs from <img src=...> tags. Deduped, order-
    preserving, capped at max_n. Skips data: URIs. Used by smart_fetch's opt-in
    include_media flag so a multimodal agent can pull the page's images."""
    if not html:
        return []
    out: list[str] = []
    seen: set[str] = set()
    for m in _IMG_RE.finditer(html[:_MAX_SCAN_CHARS]):
        src = (m.group(1) or "").strip()
        if not src or src.lower().startswith(_ASSET_SKIP):
            continue
        try:
            absu = urljoin(url, src)
        except Exception:
            continue
        if not absu.startswith(("http://", "https://")):
            continue
        if absu in seen:
            continue
        seen.add(absu)
        out.append(absu)
        if len(out) >= max_n:
            break
    return out
