"""URL pattern / scope filtering for Sieve's crawl engine.

Reconstructed clean-room against Sieve's own docs and call sites; no
upstream-derived code.

PURE functions over URL strings plus optional caller-supplied metadata
(``ctx``) — no network I/O, no file writes, stdlib only. Filters decide
keep/drop from the URL alone, or from metadata the caller has already
fetched (title / text / content_type, passed as keyword context), so the
module slots into the fetch loop without side-effects.

API::

    chain = FilterChain([
        URLPatternFilter(exclude=["/admin", "/tag/"]),
        DomainFilter(allowed=["example.com"]),
        ContentRelevanceFilter(query="mlops llm guide", threshold=0.8),
    ])
    chain.accepts("https://example.com/guide/x")  # True

Every filter exposes ``accepts(url, ctx=None) -> bool``; a
:class:`FilterChain` exposes ``accepts(url, **ctx)`` and passes only when
ALL of its filters pass. Caller-supplied patterns are bounded and validated
at construction (#59: bad regex fails immediately, never at filter time).
"""

from __future__ import annotations

import fnmatch
import math
import re
from collections import defaultdict
from typing import Any, Dict, Iterable, List, Optional, Union
from urllib.parse import urlparse

__all__ = [
    "URLFilter",
    "FilterChain",
    "URLPatternFilter",
    "DomainFilter",
    "ContentTypeFilter",
    "ContentRelevanceFilter",
]

# Pattern bounds (#59/#97): caller-supplied include/exclude patterns are
# bounded so a large config cannot inflate matching work.
_MAX_PATTERNS = 64
_MAX_PATTERN_CHARS = 512


class URLFilter:
    """Base class for URL-scope filters. Subclasses implement ``accepts``."""

    def __init__(self, name: Optional[str] = None):
        self.name = name or self.__class__.__name__

    def accepts(self, url: str, ctx: Optional[Dict[str, Any]] = None) -> bool:
        """Return True when ``url`` should be kept. ``ctx`` carries
        pre-fetched metadata (title, text, content_type, ...) when available."""
        raise NotImplementedError


class FilterChain(URLFilter):
    """Run filters in sequence; the URL passes only if ALL pass.

    ``accepts(url, **ctx)`` fans kwargs out to each filter as a shared
    context dict.
    """

    def __init__(self, filters: Optional[Iterable[URLFilter]] = None):
        super().__init__("FilterChain")
        self.filters = tuple(filters or [])

    def add_filter(self, filter_: URLFilter) -> "FilterChain":
        """Append a filter and return self (method chaining)."""
        self.filters = self.filters + (filter_,)
        return self

    def accepts(self, url: str, **ctx: Any) -> bool:
        ctx_dict: Dict[str, Any] = dict(ctx)
        for filter_ in self.filters:
            if not filter_.accepts(url, ctx_dict):
                return False
        return True


def _iter_patterns(patterns: Optional[Union[str, List[str]]]) -> List[str]:
    if patterns is None:
        return []
    if isinstance(patterns, str):
        return [patterns]
    return list(patterns)


def _make_matcher(pattern: Union[str, "re.Pattern"]):
    """Compile one include/exclude pattern into a ``(url, path) -> bool``.

    Accepted forms:
      * compiled ``re.Pattern``            -> regex search over the URL
      * ``re:<regex>`` string              -> regex search over the URL
      * glob string (``*``/``?``/``[..]``) -> fnmatch against URL, path, and
        bare path (so ``"docs/*"`` matches ``"/docs/a"``)
      * plain substring                    -> case-insensitive path/URL contains
    """
    if isinstance(pattern, re.Pattern):
        return lambda url, path: bool(pattern.search(url))
    text = str(pattern)
    if text.startswith("re:"):
        # (#59) Compile at construction: invalid or pathological patterns
        # fail with a clear error, never at filter time.
        try:
            rx = re.compile(text[3:])
        except re.error as exc:
            raise ValueError(f"invalid regex pattern {text!r}: {exc}") from exc
        return lambda url, path: bool(rx.search(url))
    lowered = text.lower()
    if set("*?[") & set(text):
        rx = re.compile(fnmatch.translate(text), re.IGNORECASE)

        def _glob_match(url: str, path: str, rx=rx) -> bool:
            candidates = {url, path, path.lstrip("/")}
            return any(rx.match(candidate) for candidate in candidates)

        return _glob_match
    return lambda url, path: lowered in path.lower() or lowered in url.lower()


class URLPatternFilter(URLFilter):
    """Keep/drop URLs by include/exclude patterns (glob, regex, or substring).

    Semantics:
      * ``exclude`` — ANY match rejects the URL. Explicit reject wins over
        everything, including include matches.
      * ``include`` — when provided, at least ONE pattern must match; an
        empty include list means "no restriction".
    Glob ``*`` crosses slashes; magic-free patterns act as case-insensitive
    path substrings (so ``"/admin"`` also excludes ``"/admin/panel"``).
    Prefix a string with ``re:`` to force regex interpretation, or pass a
    compiled ``re.Pattern`` directly.
    """

    def __init__(
        self,
        include: Optional[Union[str, List[str]]] = None,
        exclude: Optional[Union[str, List[str]]] = None,
    ):
        super().__init__("URLPatternFilter")
        self.include = _iter_patterns(include)
        self.exclude = _iter_patterns(exclude)
        patterns = [*self.include, *self.exclude]
        if len(patterns) > _MAX_PATTERNS or any(
            len(str(p)) > _MAX_PATTERN_CHARS for p in patterns
        ):
            raise ValueError(
                f"URL patterns are limited to {_MAX_PATTERNS} entries of at most "
                f"{_MAX_PATTERN_CHARS} characters"
            )
        self._include_matchers = [_make_matcher(p) for p in self.include]
        self._exclude_matchers = [_make_matcher(p) for p in self.exclude]

    @classmethod
    def from_patterns(
        cls, patterns, use_glob: bool = True, reverse: bool = False
    ) -> "URLPatternFilter":
        """Legacy single-list constructor: the list becomes ``include`` (or
        ``exclude`` when ``reverse`` is True)."""
        pats = (
            [patterns] if isinstance(patterns, (str, re.Pattern)) else list(patterns)
        )
        return cls(
            include=None if reverse else pats,
            exclude=pats if reverse else None,
        )

    @staticmethod
    def _matches_any(matchers, url: str, path: str) -> bool:
        return any(matcher(url, path) for matcher in matchers)

    def accepts(self, url: str, ctx: Optional[Dict[str, Any]] = None) -> bool:
        path = urlparse(url).path or "/"

        if self._matches_any(self._exclude_matchers, url, path):
            return False
        if self._include_matchers:
            return self._matches_any(self._include_matchers, url, path)
        return True


class DomainFilter(URLFilter):
    """Restrict URLs to allowed/blocked domains.

    * ``allowed`` — when set, the host must equal one of these domains (or be
      a subdomain of one with ``allow_subdomains=True``, the default: e.g.
      ``example.com`` admits ``docs.example.com``).
    * ``blocked`` — the host or any of its parents being blocked drops the URL.
    * ``same_domain`` — ``scheme://host`` shorthand: restricting to that exact
      host (plus subdomains).
    Blocked wins over allowed.
    """

    def __init__(
        self,
        allowed: Optional[Union[str, List[str]]] = None,
        blocked: Optional[Union[str, List[str]]] = None,
        same_domain: Optional[str] = None,
        allow_subdomains: bool = True,
    ):
        super().__init__("DomainFilter")
        self._allow_subdomains = allow_subdomains

        if allowed is None and same_domain is not None:
            allowed = [urlparse(same_domain).netloc or same_domain]
        self._allowed: Optional[tuple] = None
        if allowed is not None:
            self._allowed = self._sorted(
                {self._norm(domain) for domain in _iter_patterns(allowed)}
            )
        self._blocked = self._sorted(
            {self._norm(domain) for domain in _iter_patterns(blocked)}
        )

    @staticmethod
    def _norm(domain: str) -> str:
        """Lowercase hostname without trailing dot; credentials/ports/IPv6
        handled by urlparse (bare hosts parse with a ``//`` prefix)."""
        normalized = domain.lower().strip()
        if not normalized:
            return ""
        parsed = urlparse(
            normalized if "://" in normalized else "//" + normalized
        )
        return (parsed.hostname or "").rstrip(".")

    @staticmethod
    def _sorted(domains: set) -> tuple:
        # Longest first so suffix matching prefers the most specific entry.
        return tuple(sorted(domains, key=len, reverse=True))

    def _matches(self, domain: str, domain_list: tuple) -> bool:
        if self._allow_subdomains:
            for entry in domain_list:
                if domain == entry or domain.endswith("." + entry):
                    return True
            return False
        return domain in domain_list

    def accepts(self, url: str, ctx: Optional[Dict[str, Any]] = None) -> bool:
        try:
            domain = (urlparse(url).hostname or "").lower().rstrip(".")
        except ValueError:
            return False
        if not domain:
            return False

        if self._blocked and self._matches(domain, self._blocked):
            return False
        if self._allowed is None:
            return True
        return self._matches(domain, self._allowed)


class ContentTypeFilter(URLFilter):
    """Keep/drop URLs by MIME/content type.

    Decides from caller-supplied ``content_type`` in ctx first (no in-engine
    HEAD request); without it, falls back to the URL's file extension against
    a built-in extension→MIME map. Extension-less URLs pass (the caller can
    supply ``content_type``).
    """

    # Common web extension → MIME map.
    _MIME_MAP = {
        "txt": "text/plain", "html": "text/html", "htm": "text/html",
        "xhtml": "application/xhtml+xml", "css": "text/css", "csv": "text/csv",
        "ics": "text/calendar", "js": "application/javascript",
        "json": "application/json", "xml": "application/xml",
        "pdf": "application/pdf", "md": "text/markdown",
        "gif": "image/gif", "jpeg": "image/jpeg", "jpg": "image/jpeg",
        "png": "image/png", "svg": "image/svg+xml", "webp": "image/webp",
    }

    def __init__(self, allowed_types: Union[str, List[str]]):
        super().__init__("ContentTypeFilter")
        self._allowed = frozenset(t.lower() for t in _iter_patterns(allowed_types))
        self._ext_ok = frozenset(
            ext for ext, mime in self._MIME_MAP.items()
            if any(allowed in mime or mime in allowed for allowed in self._allowed)
        )

    @staticmethod
    def _extension(url: str) -> str:
        name = urlparse(url).path.rsplit("/", 1)[-1]
        if "." not in name:
            return ""
        return name.rpartition(".")[-1].lower()

    def accepts(self, url: str, ctx: Optional[Dict[str, Any]] = None) -> bool:
        ctx = ctx or {}
        raw_content_type = ctx.get("content_type")
        if raw_content_type:
            from sieve.content_type import media_type_of
            content_type = media_type_of(raw_content_type)
            return bool(content_type) and any(allowed in content_type for allowed in self._allowed)
        ext = self._extension(url)
        if not ext:
            return True
        return ext in self._ext_ok


class ContentRelevanceFilter(URLFilter):
    """BM25-ish relevance over URL + optional caller-supplied title/text.

    There is NO network I/O: the candidate document is built from ctx
    ``title``/``text`` when the caller already fetched the page, falling back
    to the URL string itself. The title is weighted three-fold.
    """

    def __init__(
        self,
        query: Union[str, List[str]],
        threshold: float = 0.5,
        k1: float = 1.2,
        b: float = 0.75,
        avgdl: int = 1000,
    ):
        super().__init__("ContentRelevanceFilter")
        self.query = " ".join(query) if isinstance(query, list) else query
        self.query_terms = self._tokenize(self.query)
        self.threshold = threshold
        self.k1 = k1        # TF saturation
        self.b = b          # length normalization
        self.avgdl = avgdl  # average document length

    @staticmethod
    def _tokenize(text: str) -> List[str]:
        return re.findall(r"[a-z0-9]+", text.lower())

    def _build_document(self, url: str, ctx: Dict[str, Any]) -> str:
        title = ctx.get("title") or ""
        text = ctx.get("text") or ""
        parts = [f"{title} {title} {title}", text]
        document = " ".join(part for part in parts if part)
        if not document.strip():
            document = url
        return document

    def _bm25(self, document: str) -> float:
        """Simplified BM25 over one document: query-term scores summed with
        saturation (k1) and length normalization (b, avgdl)."""
        doc_terms = self._tokenize(document)
        doc_len = len(doc_terms)
        term_freq: Dict[str, int] = defaultdict(int)
        for term in doc_terms:
            term_freq[term] += 1

        score = 0.0
        for term in set(self.query_terms):
            freq = term_freq[term]
            idf = math.log((1 + 1) / (freq + 0.5) + 1)
            numerator = freq * (self.k1 + 1)
            denominator = freq + self.k1 * (
                1 - self.b + self.b * (doc_len / self.avgdl)
            )
            score += idf * (numerator / denominator)
        return score

    def accepts(self, url: str, ctx: Optional[Dict[str, Any]] = None) -> bool:
        ctx = ctx or {}
        if not self.query_terms:
            return True
        return self._bm25(self._build_document(url, ctx)) >= self.threshold
