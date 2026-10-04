"""URL scorers for best-first crawling.

Reconstructed clean-room against Sieve's own tests (tests/test_scorers.py,
tests/test_security_claims.py) and crawl call sites; no upstream-derived code.

Pure functions over URL strings plus optional caller-supplied metadata
(``ctx``). No I/O, stdlib only. Each scorer exposes
``score(url, ctx=None) -> (float, str)``: the weighted score and a short
human-readable reason. Stats are tracked per scorer instance via ``.stats``.

:func:`score_urls` runs a chain of scorers over a list of URLs and returns
sorted ``ScoreResult`` objects (highest score first) so a crawl can pick the
highest-value page next instead of walking blindly in BFS order.

Caller-supplied configuration is bounded (#97/#196) so a large or adversarial
config cannot inflate matching work.
"""

from __future__ import annotations

import math
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence

__all__ = [
    "ScoringStats",
    "Scorer",
    "URLScorer",
    "CompositeScorer",
    "KeywordRelevanceScorer",
    "PathDepthScorer",
    "ContentTypeScorer",
    "FreshnessScorer",
    "DomainAuthorityScorer",
    "ScoreResult",
    "score_urls",
]

# Pre-computed scores for small path-depth distances (depth - optimal_depth).
_SCORE_LOOKUP = [1.0, 0.5, 1 / 3, 0.25]

# Pre-computed scores for small year differences (freshness).
_FRESHNESS_SCORES = [1.0, 0.9, 0.8, 0.7, 0.6, 0.5]

# Configuration bounds (#97/#196).
_MAX_KEYWORDS = 256
_MAX_KEYWORD_LEN = 128
_MAX_TYPE_PATTERNS = 64
_MAX_TYPE_PATTERN_LEN = 128

# Public per-scorer return type: (score, reason).
Scored = tuple[float, str]


def _keywords(value) -> list[str]:
    values = value.split() if isinstance(value, str) else value
    result = []
    for keyword in values:
        if len(result) >= _MAX_KEYWORDS:
            raise ValueError(f"keywords are limited to {_MAX_KEYWORDS} entries")
        if not isinstance(keyword, str) or not keyword or len(keyword) > _MAX_KEYWORD_LEN:
            raise ValueError(f"keywords must be non-empty strings of at most {_MAX_KEYWORD_LEN} characters")
        result.append(keyword)
    return result


class ScoringStats:
    """Running statistics over scored URLs (count/avg/min/max).

    Min and max are lazy: before any extreme is seen they report the running
    average, so a scorer used only through :meth:`get_average` never invents
    spurious extremes.
    """

    __slots__ = ("_urls_scored", "_total_score", "_min_score", "_max_score")

    def __init__(self) -> None:
        self._urls_scored = 0
        self._total_score = 0.0
        self._min_score: Optional[float] = None
        self._max_score: Optional[float] = None

    def update(self, score: float) -> None:
        self._urls_scored += 1
        self._total_score += score
        if self._min_score is None or score < self._min_score:
            self._min_score = score
        if self._max_score is None or score > self._max_score:
            self._max_score = score

    def get_average(self) -> float:
        return self._total_score / self._urls_scored if self._urls_scored else 0.0

    def get_min(self) -> float:
        if self._min_score is None:
            self._min_score = self.get_average()
        return self._min_score

    def get_max(self) -> float:
        if self._max_score is None:
            self._max_score = self.get_average()
        return self._max_score


class Scorer(ABC):
    """Base contract for a URL scorer.

    ``score(url, ctx)`` returns a ``(score, reason)`` tuple where ``reason``
    briefly explains the score. Stats accumulate across every ``score`` call
    and are exposed via ``.stats``. Subclasses implement the raw
    ``_calculate_score`` (pre-weight) plus a ``score`` that attaches a
    reason.
    """

    __slots__ = ("_weight", "_stats")

    def __init__(self, weight: float = 1.0) -> None:
        if isinstance(weight, bool):
            raise ValueError("weight must be a finite number")
        try:
            numeric_weight = float(weight)
        except (TypeError, ValueError):
            raise ValueError("weight must be a finite number") from None
        if not math.isfinite(numeric_weight):
            raise ValueError("weight must be a finite number")
        self._weight = numeric_weight
        self._stats = ScoringStats()

    @abstractmethod
    def _calculate_score(self, url: str, ctx: Dict) -> float:
        """Raw (unweighted) score for ``url`` given context."""

    @abstractmethod
    def score(self, url: str, ctx: Optional[Dict] = None) -> Scored:
        """Weighted score + reason."""

    @property
    def stats(self) -> ScoringStats:
        return self._stats

    @property
    def weight(self) -> float:
        return self._weight


class URLScorer(Scorer):
    """Alias of :class:`Scorer` kept for call-site naming."""


def _weighted(base: "Scorer", url: str, ctx: Optional[Dict], reason: str) -> Scored:
    """Shared helper: raw score x weight, stats update, reason attached."""
    score = base._calculate_score(url, ctx or {}) * base.weight
    base._stats.update(score)
    return (score, reason)


class CompositeScorer(URLScorer):
    """Combine several scorers into one.

    The final score is the sum of sub-scorer (weighted) scores, optionally
    normalized by their count (mean). Each sub-scorer is evaluated exactly
    once per ``score`` call and its individual reason is aggregated into the
    composite reason. The composite's own ``weight`` applies on top.
    """

    __slots__ = ("_scorers", "_normalize")

    def __init__(self, scorers: Sequence[Scorer], normalize: bool = True,
                 weight: float = 1.0) -> None:
        super().__init__(weight=weight)
        self._scorers = list(scorers)
        self._normalize = normalize

    def _calculate_score(self, url: str, ctx: Dict) -> float:
        total = 0.0
        for scorer in self._scorers:
            total += scorer.score(url, ctx)[0]
        if self._normalize and self._scorers:
            return total / len(self._scorers)
        return total

    def score(self, url: str, ctx: Optional[Dict] = None) -> Scored:
        ctx = ctx or {}
        components = [scorer.score(url, ctx) for scorer in self._scorers]
        total = sum(sub_score for sub_score, _ in components)
        if self._normalize and self._scorers:
            total /= len(self._scorers)
        score = total * self._weight
        self._stats.update(score)
        reasons = []
        for scorer, (sub_score, reason) in zip(self._scorers, components):
            bits = f"{type(scorer).__name__}={sub_score:.3f}"
            if reason:
                bits += f" ({reason})"
            reasons.append(bits)
        return (score, "; ".join(reasons))


class KeywordRelevanceScorer(URLScorer):
    """Score URLs by substring occurrence of keywords.

    ``keywords`` is a string (split on whitespace) or a list of strings.
    Score = matched / total, 1.0 on a full match. Case-insensitive by
    default. Extra terms may arrive per-call via ``ctx['keywords']``.
    """

    __slots__ = ("_keywords", "_case_sensitive")

    def __init__(self, keywords: List[str] | str, weight: float = 1.0,
                 case_sensitive: bool = False) -> None:
        super().__init__(weight=weight)
        self._case_sensitive = case_sensitive
        keywords = _keywords(keywords)
        self._keywords = [k if case_sensitive else k.lower() for k in keywords]

    def _calculate_score(self, url: str, ctx: Dict) -> float:
        url_key = url if self._case_sensitive else url.lower()
        keywords = self._keywords
        extra = ctx.get("keywords")
        if extra:
            extra = _keywords(extra)
            if len(keywords) + len(extra) > _MAX_KEYWORDS:
                raise ValueError(f"combined keywords are limited to {_MAX_KEYWORDS} entries")
            keywords = list(keywords) + [
                k if self._case_sensitive else k.lower() for k in extra
            ]
        if not keywords:
            return 0.0
        matches = sum(1 for k in keywords if k in url_key)
        if not matches:
            return 0.0
        if matches == len(keywords):
            return 1.0
        return matches / len(keywords)

    def score(self, url: str, ctx: Optional[Dict] = None) -> Scored:
        return _weighted(self, url, ctx, f"keywords={self._keywords}")


class PathDepthScorer(URLScorer):
    """Score URLs by path depth — shallow paths preferred.

    Depth counts non-empty path segments. The score peaks at
    ``optimal_depth`` (default 3) and decays with distance from it;
    ``ctx['optimal_depth']`` overrides per call.
    """

    __slots__ = ("_optimal_depth",)

    def __init__(self, optimal_depth: int = 3, weight: float = 1.0) -> None:
        super().__init__(weight=weight)
        self._optimal_depth = optimal_depth

    @staticmethod
    def _quick_depth(path: str) -> int:
        """Count non-empty path segments (no regex, no splits).

        ``"/"`` or ``""`` -> 0; ``"/a"`` -> 1; ``"/a/b"`` -> 2.
        """
        if not path or path == "/" or "/" not in path:
            return 0
        depth = 0
        last_was_slash = True
        for ch in path:
            if ch == "/":
                if not last_was_slash:
                    depth += 1
                last_was_slash = True
            else:
                last_was_slash = False
        if not last_was_slash:
            depth += 1
        return depth

    def _calculate_score(self, url: str, ctx: Dict) -> float:
        scheme_end = url.find("://")
        pos = url.find("/", scheme_end + 3) if scheme_end != -1 else url.find("/")
        depth = self._quick_depth(url[pos:]) if pos != -1 else 0
        optimal = ctx.get("optimal_depth", self._optimal_depth)
        distance = abs(depth - optimal)
        if distance < len(_SCORE_LOOKUP):
            return _SCORE_LOOKUP[distance]
        return 1.0 / (1.0 + distance)

    def score(self, url: str, ctx: Optional[Dict] = None) -> Scored:
        return _weighted(self, url, ctx, "path-depth")


class ContentTypeScorer(URLScorer):
    """Score URLs by file extension / content-type pattern.

    ``type_weights`` maps patterns to scores. A ``'.ext$'`` pattern goes into
    a fast exact-extension lookup; anything else compiles as a regex matched
    against the whole URL. Regex patterns are sorted by score (descending)
    for early exit.
    """

    __slots__ = ("_exact_types", "_regex_types")

    def __init__(self, type_weights: Dict[str, float], weight: float = 1.0) -> None:
        super().__init__(weight=weight)
        if len(type_weights) > _MAX_TYPE_PATTERNS:
            raise ValueError(
                f"type_weights are limited to {_MAX_TYPE_PATTERNS} patterns"
            )
        self._exact_types: Dict[str, float] = {}
        self._regex_types: List[tuple[re.Pattern, float]] = []
        for pattern, score in type_weights.items():
            if (
                not isinstance(pattern, str)
                or not pattern
                or len(pattern) > _MAX_TYPE_PATTERN_LEN
            ):
                raise ValueError(
                    f"type_weights patterns must be non-empty strings of at most "
                    f"{_MAX_TYPE_PATTERN_LEN} characters"
                )
            if not math.isfinite(float(score)):
                raise ValueError("type_weights scores must be finite")
            if pattern.startswith(".") and pattern.endswith("$"):
                self._exact_types[pattern[1:-1]] = score
            else:
                try:
                    compiled = re.compile(pattern)
                except re.error as exc:
                    raise ValueError(
                        f"invalid type_weights pattern {pattern!r}: {exc}"
                    ) from exc
                self._regex_types.append((compiled, score))
        self._regex_types.sort(key=lambda entry: -entry[1])

    @staticmethod
    def _quick_extension(url: str) -> str:
        """Extension after the last dot, terminated by ``?``/``#``/``;`` or
        any non-alphanumeric character."""
        pos = url.rfind(".")
        if pos == -1:
            return ""
        end = len(url)
        for i in range(pos + 1, len(url)):
            ch = url[i]
            if ch in "?#;" or not ch.isalnum():
                end = i
                break
        return url[pos + 1:end].lower()

    def _calculate_score(self, url: str, ctx: Dict) -> float:
        ext = self._quick_extension(url)
        if ext:
            exact = self._exact_types.get(ext)
            if exact is not None:
                return exact
        for pattern, score in self._regex_types:
            if pattern.search(url):
                return score
        return 0.0

    def score(self, url: str, ctx: Optional[Dict] = None) -> Scored:
        return _weighted(self, url, ctx, "content-type")


class FreshnessScorer(URLScorer):
    """Score URLs by the recency of a date embedded in the URL.

    Recognizes ``YYYY/MM/DD``, ``YYYY-MM-DD``, ``YYYY_MM_DD`` and bare
    ``YYYY`` (1900-2099). More recent years score higher; URLs without a
    date get a neutral 0.5. ``ctx['current_year']`` overrides the default.
    """

    __slots__ = ("_current_year",)

    _DATE_RE = re.compile(
        r"(?:/|[-_])((?:19|20)\d{2})"          # year
        r"(?:(?:/|[-_])\d{2}"                  # optional month
        r"(?:(?:/|[-_])\d{2})?)?"              # optional day
    )

    def __init__(self, weight: float = 1.0, current_year: int = 2024) -> None:
        super().__init__(weight=weight)
        self._current_year = current_year

    def _extract_year(self, url: str, current_year: int) -> Optional[int]:
        latest: Optional[int] = None
        for match in self._DATE_RE.finditer(url):
            year = int(match.group(1))
            if year <= current_year and (latest is None or year > latest):
                latest = year
        return latest

    def _calculate_score(self, url: str, ctx: Dict) -> float:
        current_year = ctx.get("current_year", self._current_year)
        year = self._extract_year(url, current_year)
        if year is None:
            return 0.5
        year_diff = current_year - year
        if 0 <= year_diff < len(_FRESHNESS_SCORES):
            return _FRESHNESS_SCORES[year_diff]
        return max(0.1, 1.0 - year_diff * 0.1)

    def score(self, url: str, ctx: Optional[Dict] = None) -> Scored:
        return _weighted(self, url, ctx, "freshness")


class DomainAuthorityScorer(URLScorer):
    """Score URLs by the authority of their host domain.

    ``domain_weights`` maps domains to scores. Unknown domains get
    ``default_weight``. The five highest-weighted domains are kept in a
    fast-path dict; host extraction is case-insensitive with the port
    stripped and accepts bare hostnames.
    """

    __slots__ = ("_domain_weights", "_default_weight", "_top_domains")

    def __init__(self, domain_weights: Dict[str, float],
                 default_weight: float = 0.5, weight: float = 1.0) -> None:
        super().__init__(weight=weight)
        self._domain_weights = {
            domain.lower(): score for domain, score in domain_weights.items()
        }
        self._default_weight = default_weight
        self._top_domains = dict(
            sorted(self._domain_weights.items(), key=lambda item: -item[1])[:5]
        )

    @staticmethod
    def _extract_domain(url: str) -> str:
        start = url.find("://")
        start = start + 3 if start != -1 else 0
        end = url.find("/", start)
        if end == -1:
            end = url.find("?", start)
            if end == -1:
                end = url.find("#", start)
                if end == -1:
                    end = len(url)
        domain = url[start:end]
        port_index = domain.rfind(":")
        if port_index != -1:
            domain = domain[:port_index]
        return domain.lower()

    def _calculate_score(self, url: str, ctx: Dict) -> float:
        domain = self._extract_domain(url)
        top = self._top_domains.get(domain)
        if top is not None:
            return top
        return self._domain_weights.get(domain, self._default_weight)

    def score(self, url: str, ctx: Optional[Dict] = None) -> Scored:
        return _weighted(self, url, ctx, "domain-authority")


@dataclass
class ScoreResult:
    """Outcome of scoring one URL through a chain of scorers."""

    url: str
    score: float
    reasons: List[str] = field(default_factory=list)
    """Per-scorer human-readable reasons (in scorer order)."""

    def __lt__(self, other: "ScoreResult") -> bool:
        # Highest score first when sorting.
        return self.score > other.score


def score_urls(
    urls: Sequence[str],
    scorers: Sequence[Scorer],
    **ctx,
) -> List[ScoreResult]:
    """Score ``urls`` through an ordered scorer chain, highest score first.

    A single scorer contributes its (weighted) score; multiple scorers'
    weighted scores are summed with per-scorer reasons collected. ``ctx``
    kwargs reach every scorer (e.g. ``focus``, ``current_year``,
    ``optimal_depth``, ``keywords``). An empty chain yields zero scores.
    """
    chain = list(scorers)
    if not chain:
        return [ScoreResult(url=url, score=0.0) for url in urls]

    results: List[ScoreResult] = []
    for url in urls:
        score = 0.0
        reasons: List[str] = []
        for scorer in chain:
            sub_score, reason = scorer.score(url, ctx)
            score += sub_score
            if reason:
                reasons.append(f"{type(scorer).__name__}: {reason} (score={sub_score:.3f})")
        results.append(ScoreResult(url=url, score=score, reasons=reasons))
    results.sort()
    return results
