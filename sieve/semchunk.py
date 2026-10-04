"""Token-aware semantic chunking — semchunk-style sentence windowing with tiktoken.

Concepts from semchunk (MIT, https://github.com/isaacsw976/semchunk) — sentence-aware
windowing with token counting. Reimplemented from scratch, no semchunk code copied
and no license obligations attached. tiktoken is optional (pip install 'sieve-cli[nlp]').
"""
from __future__ import annotations
import re

from ._chunking import ChunkingStrategy

_SENT_RE = re.compile(r"(?<=[.!?])\s+")

def _sentences(text: str) -> list[str]:
    try:
        from nltk.tokenize import sent_tokenize
        try:
            return [s.strip() for s in sent_tokenize(text) if s.strip()]
        except LookupError:
            pass
    except ImportError:
        pass
    # fallback: regex sentence split
    parts = [p.strip() for p in _SENT_RE.split(text) if p.strip()]
    return parts if parts else ([text] if text.strip() else [])

class SemanticChunking(ChunkingStrategy):
    """Sentence-windowed, token-counted chunking (tiktoken cl100k_base)."""

    def __init__(self, max_tokens: int = 500, overlap_tokens: int = 50, model: str = "cl100k_base"):
        if isinstance(max_tokens, bool) or not isinstance(max_tokens, int) or not 1 <= max_tokens <= 8192:
            raise ValueError("max_tokens must be an integer between 1 and 8192")
        if isinstance(overlap_tokens, bool) or not isinstance(overlap_tokens, int) or not 0 <= overlap_tokens <= 8192:
            raise ValueError("overlap_tokens must be an integer between 0 and 8192")
        self.max_tokens = max_tokens
        if overlap_tokens >= max_tokens:
            raise ValueError("overlap_tokens must be smaller than max_tokens")
        self.overlap_tokens = overlap_tokens
        self.model = model

    def _enc(self):
        try:
            import tiktoken
        except ImportError as e:
            raise RuntimeError("tiktoken is not installed (pip install -e \'.[nlp]\')") from e
        try:
            return tiktoken.get_encoding(self.model)
        except Exception:
            return tiktoken.get_encoding("cl100k_base")

    def _count(self, enc, text: str) -> int:
        return len(enc.encode(text))

    def chunk(self, text: str) -> list[str]:
        if not text or not text.strip():
            return []
        # Aggregate work budget (issue #60): cap the input and the produced
        # chunk count so a very large document cannot drive unbounded
        # per-sentence tokenization + overlap re-tokenization.
        if len(text) > _MAX_CHUNK_INPUT_CHARS:
            text = text[:_MAX_CHUNK_INPUT_CHARS]
        sents = _sentences(text)
        if not sents:
            return [text]
        enc = self._enc()
        chunks: list[str] = []
        cur: list[str] = []
        cur_tokens = 0
        for sent in sents:
            s_tokens = self._count(enc, sent)
            # single sentence too large -> hard split by tokens
            if s_tokens > self.max_tokens:
                if cur:
                    chunks.append(" ".join(cur))
                    cur, cur_tokens = [], 0
                # split long sentence by encoding slices
                toks = enc.encode(sent)
                for i in range(0, len(toks), self.max_tokens):
                    piece = enc.decode(toks[i:i + self.max_tokens])
                    chunks.append(piece)
                continue
            if cur_tokens + s_tokens > self.max_tokens and cur:
                chunks.append(" ".join(cur))
                # overlap: keep tail sentences that fit overlap_tokens
                if self.overlap_tokens > 0:
                    overlap: list[str] = []
                    ov = 0
                    for s in reversed(cur):
                        t = self._count(enc, s)
                        if ov + t > self.overlap_tokens:
                            break
                        overlap.insert(0, s)
                        ov += t
                    cur = overlap[:]
                    cur_tokens = ov
                else:
                    cur, cur_tokens = [], 0
            cur.append(sent)
            cur_tokens += s_tokens
        if cur:
            chunks.append(" ".join(cur))
        return chunks[:_MAX_CHUNKS]


# Input/chunk-count caps for aggregate tokenization work (issue #60).
_MAX_CHUNK_INPUT_CHARS = 1_000_000
_MAX_CHUNKS = 2_000
