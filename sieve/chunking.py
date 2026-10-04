"""Text chunking strategies: regex splitting, identity, and sentence chunking.

Reconstructed clean-room against Sieve's own tests (tests/test_crawl4ai_ports.py)
and call sites; no upstream-derived code or attribution.

Strategies
----------
``RegexChunking``      split text by caller-supplied regex patterns, applied
                       in sequence (the output of one pattern is the input of
                       the next).
``IdentityChunking``   the whole text as a single chunk.
``NltkSentenceChunking`` sentence boundaries via NLTK punkt (requires the
                       optional ``nlp`` extra).

All strategies implement ``chunk(text) -> list[str]`` and share bounded
output: chunk counts, per-chunk size, and aggregate characters are capped so
a pathological input cannot produce unbounded memory.

``chunk_text(text, strategy)`` dispatches by name; unknown names raise
``ValueError``.
"""

from __future__ import annotations

import re

from ._chunking import ChunkingStrategy as _ChunkingStrategy

__all__ = [
    "IdentityChunking",
    "RegexChunking",
    "NltkSentenceChunking",
    "chunk_text",
]


class IdentityChunking(_ChunkingStrategy):
    """Return the input text unchanged as a single chunk."""

    def chunk(self, text: str) -> list[str]:
        return [text]


# Output bounds. A bounded document split by user-supplied patterns can
# still explode combinatorially (each pattern multiplies the parts), so the
# number of chunks, the size of any single chunk, and the total emitted
# characters are all capped.
_MAX_PATTERNS = 32
_MAX_PATTERN_CHARS = 512
_MAX_CHUNKS = 10_000
_MAX_CHUNK_CHARS = 200_000
_MAX_TOTAL_CHARS = 2_000_000

# Regex constructs that enable catastrophic backtracking on untrusted text.
# Lookarounds are rejected outright; anything else must compile.
_REJECTED_CONSTRUCTS = ("(?<", "(?=")


class RegexChunking(_ChunkingStrategy):
    """Split text by one or more regex patterns, applied in sequence.

    Args:
        patterns: Regex strings to split on. With ``None`` the default is a
            paragraph split on blank lines. Each pattern is validated at
            construction: at most 32 non-empty patterns of at most 512
            characters, no lookarounds, and each must compile.

    Raises:
        ValueError: on pattern-count/size violations, lookaround constructs,
            or patterns that fail to compile.
    """

    def __init__(self, patterns: list[str] | None = None):
        selected = list(patterns) if patterns is not None else [r"\n\n"]
        if len(selected) > _MAX_PATTERNS:
            raise ValueError(
                f"patterns are limited to {_MAX_PATTERNS} non-empty regex strings "
                f"up to {_MAX_PATTERN_CHARS} characters"
            )
        for pattern in selected:
            if (
                not isinstance(pattern, str)
                or not pattern
                or len(pattern) > _MAX_PATTERN_CHARS
            ):
                raise ValueError(
                    f"patterns are limited to {_MAX_PATTERNS} non-empty regex strings "
                    f"up to {_MAX_PATTERN_CHARS} characters"
                )
            if any(tok in pattern for tok in _REJECTED_CONSTRUCTS):
                raise ValueError(
                    f"pattern {pattern!r} contains a lookaround construct outside "
                    "the safe subset"
                )
            try:
                re.compile(pattern)
            except re.error as exc:
                raise ValueError(f"invalid regex pattern {pattern!r}: {exc}") from exc
        self.patterns = selected

    def chunk(self, text: str) -> list[str]:
        """Split ``text`` by each pattern in order, under the output bounds."""
        parts = [text]
        for pattern in self.patterns:
            if len(parts) >= _MAX_CHUNKS:
                break
            nxt: list[str] = []
            for piece in parts:
                if len(nxt) >= _MAX_CHUNKS:
                    break
                nxt.extend(re.split(pattern, piece))
            parts = nxt

        out: list[str] = []
        total = 0
        for piece in parts:
            if piece is None:
                continue
            if len(piece) > _MAX_CHUNK_CHARS:
                piece = piece[:_MAX_CHUNK_CHARS]
            total += len(piece)
            out.append(piece)
            if total > _MAX_TOTAL_CHARS or len(out) >= _MAX_CHUNKS:
                break
        return out


class NltkSentenceChunking(_ChunkingStrategy):
    """Sentence chunking via NLTK's punkt tokenizer (optional ``nlp`` extra)."""

    def chunk(self, text: str) -> list[str]:
        try:
            from nltk.tokenize import sent_tokenize
        except ImportError as exc:
            raise RuntimeError("nltk is not installed (uv sync --extra nlp)") from exc
        try:
            sentences = sent_tokenize(text)
        except LookupError as exc:
            raise RuntimeError(
                'nltk punkt data missing (python -c "import nltk; '
                "nltk.download('punkt_tab')\")"
            ) from exc
        return [s.strip() for s in sentences if s.strip()]


_STRATEGIES = {
    "identity": IdentityChunking,
    "regex": RegexChunking,
    "sentence": NltkSentenceChunking,
}

try:  # optional dependency; registered only when importable
    from sieve.semchunk import SemanticChunking

    _STRATEGIES["semantic"] = SemanticChunking
except ImportError:
    pass


def chunk_text(text: str, strategy: str = "regex") -> list[str]:
    """Split ``text`` with the named strategy.

    Known names: ``identity``, ``regex``, ``sentence``, ``semantic`` (when the
    semchunk dependency is installed). Unknown names raise ``ValueError``.
    """
    try:
        cls = _STRATEGIES[strategy]
    except KeyError:
        known = "|".join(sorted(_STRATEGIES))
        raise ValueError(f"unknown chunk strategy {strategy!r} ({known})") from None
    return cls().chunk(text)
