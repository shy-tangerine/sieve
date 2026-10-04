"""Local neural reranker (v7 Phase 2).

Runs an ONNX cross-encoder (Apache-2.0 `cross-encoder/ms-marco-MiniLM-L-6-v2`,
22.7M params, trained on MS MARCO passage reranking = query/document relevance)
on the `onnxruntime` we ALREADY ship for OCR. No new runtime. The model + tokenizer
are downloaded ONCE on first neural search into
`~/.sieve_cache/models/msmarco-minilm-l6-v2/` (pinned to a specific HF
revision + hash-checked), NOT bundled in the wheel, so the lean install stays small.

Graceful fallback: if onnxruntime/tokenizers are missing (lean install) or the
model download fails / is offline, `get_reranker()` returns None and the caller
falls back to cross-engine consensus + engine-position order (no lexical rerank
- BM25 was removed as redundant; neural matches its speed and ranks better).
Neural rerank is an `[all]` extra; lean installs get consensus-ordered search.
"""

from __future__ import annotations

import hashlib
import logging
import math
import asyncio
import urllib.request
import os
import tempfile
from pathlib import Path
from typing import Optional

logger = logging.getLogger("master-fetch.reranker")

MODEL_ID = "cross-encoder/ms-marco-MiniLM-L-6-v2"
# Pinned revision for reproducibility (downloaded files never shift under us).
MODEL_REV = "c5ee24cb16019beea0893ab7796b1df96625c6b8"
_BASE = f"https://huggingface.co/{MODEL_ID}/resolve/{MODEL_REV}"
MODEL_FILES = {
    "model.onnx": f"{_BASE}/onnx/model.onnx",
    "tokenizer.json": f"{_BASE}/tokenizer.json",
    "vocab.txt": f"{_BASE}/vocab.txt",
}
# (#34) Maintainer-known SHA-256 pins for the files at MODEL_REV. A download
# that does not match is rejected rather than trusted; a first-download hash
# is no longer self-recorded, so a tampered mirror cannot poison the cache.
EXPECTED_SHA256 = {
    "model.onnx": "5d3e70fd0c9ff14b9b5169a51e957b7a9c74897afd0a35ce4bd318150c1d4d4a",
    "tokenizer.json": "d241a60d5e8f04cc1b2b3e9ef7a4921b27bf526d9f6050ab90f9267a1f9e5c66",
}
# Canonical config root (#31).
MODEL_DIR = Path.home() / ".sieve" / "models" / "msmarco-minilm-l6-v2"
MAX_SEQ = 512
# Sanity floor so a truncated/failed download is rejected (real onnx is ~80MB).
MIN_MODEL_BYTES = 50_000_000
MAX_MODEL_FILE_BYTES = 256 * 1024 * 1024

_reranker: Optional[object] = None
_reranker_tried: bool = False
_reranker_unavailable_reason: str = ""
_reranker_lock: Optional["asyncio.Lock"] = None


class _Reranker:
    """Wraps a warm ONNX InferenceSession + tokenizer for query/doc scoring."""

    def __init__(self, model_path: Path, tokenizer):
        import numpy as np
        import onnxruntime as ort
        so = ort.SessionOptions()
        try:
            so.graph_opt_level = ort.GraphOptLevel.ORT_ENABLE_ALL
        except Exception:
            pass
        self.sess = ort.InferenceSession(
            str(model_path), so, providers=["CPUExecutionProvider"],
        )
        self.input_names = [i.name for i in self.sess.get_inputs()]
        self.tok = tokenizer
        try:
            self.tok.enable_truncation(max_length=MAX_SEQ)
        except Exception:
            pass
        self._np = np

    def _feed(self, input_ids, attn, type_ids):
        arrs = {"input_ids": input_ids, "attention_mask": attn, "token_type_ids": type_ids}
        feed = {}
        for name in self.input_names:
            if name in arrs:
                feed[name] = arrs[name]
        # Positional fallback for any oddly-named inputs.
        canon = [input_ids, attn, type_ids]
        ci = 0
        for name in self.input_names:
            if name in feed:
                continue
            feed[name] = canon[ci % len(canon)]
            ci += 1
        return feed

    def score(self, query: str, docs: list[str]) -> list[float]:
        """Return a sigmoid relevance score (0..1) for each (query, doc) pair."""
        if not docs:
            return []
        np = self._np
        encs = [self.tok.encode(query, d) for d in docs]
        maxlen = max((len(e.ids) for e in encs), default=0) or 1
        n = len(encs)
        input_ids = np.zeros((n, maxlen), dtype=np.int64)
        attn = np.zeros((n, maxlen), dtype=np.int64)
        type_ids = np.zeros((n, maxlen), dtype=np.int64)
        for i, e in enumerate(encs):
            L = len(e.ids)
            input_ids[i, :L] = e.ids
            attn[i, :L] = e.attention_mask
            type_ids[i, :L] = e.type_ids
        try:
            out = self.sess.run(None, self._feed(input_ids, attn, type_ids))
        except Exception as e:
            logger.warning(f"reranker inference failed: {e}")
            return [0.0] * n
        logits = np.asarray(out[0]).reshape(-1)
        scores = []
        for i in range(n):
            try:
                scores.append(1.0 / (1.0 + math.exp(-float(logits[i]))))
            except (OverflowError, ValueError):
                scores.append(0.0 if float(logits[i]) < 0 else 1.0)
        return scores


def _download_file(url: str, dest: Path) -> bool:
    """Stream a file to a unique temporary path, then atomically publish it."""
    tmp: Path | None = None
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(prefix=f".{dest.name}.", suffix=".part", dir=dest.parent)
        tmp = Path(tmp_name)
        req = urllib.request.Request(url, headers={"User-Agent": "sieve-cli/13"})
        with urllib.request.urlopen(req, timeout=120) as r, os.fdopen(fd, "wb") as f:
            raw_total = r.headers.get("Content-Length")
            total = int(raw_total) if raw_total else 0
            if total < 0 or total > MAX_MODEL_FILE_BYTES:
                raise ValueError("invalid reranker download size")
            done = 0
            while True:
                chunk = r.read(1 << 16)
                if not chunk:
                    break
                done += len(chunk)
                if done > MAX_MODEL_FILE_BYTES:
                    raise ValueError("reranker download exceeds size limit")
                f.write(chunk)
            f.flush()
            os.fsync(f.fileno())
        if total and done != total:
            logger.warning("short download for %s", dest.name)
            return False
        os.replace(tmp, dest)
        tmp = None
        return True
    except Exception as e:
        logger.warning("reranker download failed: %s", type(e).__name__)
        return False
    finally:
        if tmp is not None:
            tmp.unlink(missing_ok=True)


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _ensure_model() -> Optional[tuple[Path, Path]]:
    """Ensure model.onnx + tokenizer.json are present + valid. Returns paths or None."""
    onnx = MODEL_DIR / "model.onnx"
    tokjson = MODEL_DIR / "tokenizer.json"

    need = []
    if not onnx.exists() or onnx.stat().st_size < MIN_MODEL_BYTES:
        need.append("model.onnx")
    if not tokjson.exists():
        need.append("tokenizer.json")

    if need:
        logger.info(
            "Sieve: downloading the local search reranker model (one-time, ~80MB)..."
        )
        for name in need:
            if not _download_file(MODEL_FILES[name], MODEL_DIR / name):
                return None
        if onnx.stat().st_size < MIN_MODEL_BYTES:
            logger.warning("downloaded model.onnx is too small; rejecting")
            return None
        # (#34) Verify against the maintainer-known pin, not a self-recorded
        # hash: a compromised mirror must not be able to poison the cache.
        if _sha256(onnx) != EXPECTED_SHA256["model.onnx"]:
            logger.warning("downloaded model.onnx does not match the pinned SHA-256; rejecting")
            return None

    # Verify the onnx still matches the pinned hash (detect corruption).
    if _sha256(onnx) != EXPECTED_SHA256["model.onnx"]:
        logger.warning("model.onnx hash mismatch; re-downloading")
        if _download_file(MODEL_FILES["model.onnx"], onnx) and \
                _sha256(onnx) == EXPECTED_SHA256["model.onnx"]:
            pass
        else:
            return None

    if _sha256(tokjson) != EXPECTED_SHA256["tokenizer.json"]:
        logger.warning("tokenizer.json does not match the pinned SHA-256; rejecting")
        return None
    return onnx, tokjson


def _load_reranker() -> Optional[_Reranker]:
    """Synchronous reranker load (ONNX session + tokenizer). Sets the module
    singleton + 'tried' flag. Never raises. Called under the ensure_reranker
    lock so concurrent callers (prewarm + first search) share ONE load."""
    global _reranker, _reranker_tried, _reranker_unavailable_reason
    _reranker_tried = True
    try:
        import onnxruntime  # noqa: F401
        import tokenizers  # noqa: F401
        import numpy  # noqa: F401
    except ImportError as e:
        _reranker_unavailable_reason = (
            f"neural rerank needs the Sieve's [all] extras (uv sync --all-extras) ({e.__class__.__name__}: {e})"
        )
        return None
    try:
        paths = _ensure_model()
        if paths is None:
            _reranker_unavailable_reason = "reranker model download failed (offline?)"
            return None
        onnx_path, tok_path = paths
        from tokenizers import Tokenizer
        try:
            tok = Tokenizer.from_file(str(tok_path))
        except Exception:
            # Fallback: build a BERT WordPiece tokenizer from vocab.txt.
            from tokenizers import BertWordPieceTokenizer
            tok = BertWordPieceTokenizer(str(MODEL_DIR / "vocab.txt"), lowercase=True)
        _reranker = _Reranker(onnx_path, tok)
        logger.info("Sieve: neural reranker ready (ms-marco-MiniLM-L-6-v2, ONNX).")
        return _reranker
    except Exception as e:
        _reranker_unavailable_reason = f"reranker init failed: {e}"
        logger.warning(f"reranker init failed: {e}")
        return None


def get_reranker() -> Optional[_Reranker]:
    """Peek the warm singleton reranker, or None if not yet loaded / unavailable.

    PEEK ONLY â never triggers a load (that's ensure_reranker()'s job, called by
    the prewarm + the search path). Callers (rerank(), search) use this to check
    whether the neural cross-encoder is warm; if None they fall back to
    cross-engine consensus + engine-position order. Never raises.
    """
    return _reranker  # type: ignore[return-value]


async def ensure_reranker(*, download: bool = True) -> Optional[_Reranker]:
    """Race-safe async reranker load. Concurrent callers (the startup prewarm +
    the first search) share ONE load via a lock; the search awaits the in-flight
    prewarm instead of racing it (the old sync get_reranker() would either
    double-load or, if the prewarm had set _reranker_tried mid-load, silently
    return None so the first search lost neural rerank). Returns the warm
    reranker or None (unavailable / model absent and download=False).

    Caches the 'tried' state so a one-time failure doesn't retry every call.
    """
    global _reranker_lock
    if _reranker is not None:
        return _reranker  # type: ignore[return-value]  # warm fast path (no lock)
    if not download and not model_present():
        return None
    if _reranker_lock is None:
        _reranker_lock = asyncio.Lock()
    # Acquire the lock so a concurrent in-flight load (the startup prewarm, or a
    # parallel search) is AWAITED rather than raced: the top-level fast path only
    # short-circuits on a warm reranker, NOT on _reranker_tried (which is set
    # early inside _load_reranker while still holding the lock â so a mid-load
    # caller must wait on the lock, then see the finished result).
    async with _reranker_lock:
        if _reranker is not None:
            return _reranker  # type: ignore[return-value]
        if _reranker_tried:
            return None  # a previous load FINISHED and failed; don't retry this process
        if not download and not model_present():
            return None
        return await asyncio.to_thread(_load_reranker)


def rerank(query: str, results: list) -> Optional[list[tuple]]:
    """Rerank RawResults with the neural cross-encoder. Returns (result, score)
    pairs sorted desc, or None if the reranker is unavailable (caller falls back
    to consensus + engine-position order).

    Scores are min-max normalized across this result set to 0..1. ms-marco
    sigmoid saturates (~1.0 for any clearly-relevant snippet), so raw scores
    cluster tightly and can't discriminate among good results; normalizing
    restores meaningful spread (top=1.0, worst=0.0) for the relevance_score field
    + tier derivation. Ranking ORDER is unchanged (normalization is monotonic).
    """
    rer = get_reranker()
    if rer is None or not results:
        return None
    docs = [f"{r.title} {r.snippet}" for r in results]
    try:
        raw = rer.score(query, docs)
    except Exception as e:
        logger.warning(f"neural rerank failed: {e}")
        return None
    if len(raw) != len(results):
        return None
    mn, mx = min(raw), max(raw)
    if mx > mn:
        scores = [(s - mn) / (mx - mn) for s in raw]
    else:
        scores = [1.0 for _ in raw]  # all equal -> no spread to normalize -> all top
    pairs = list(zip(results, scores))
    pairs.sort(key=lambda rs: (-rs[1], rs[0].position))
    return pairs


def unavailable_reason() -> str:
    return _reranker_unavailable_reason


def model_present() -> bool:
    """True if the reranker model + tokenizer are already cached locally (so
    get_reranker() will NOT trigger a download). Used by startup prewarm to warm
    the ONNX session only when it is free to do so."""
    onnx = MODEL_DIR / "model.onnx"
    tokjson = MODEL_DIR / "tokenizer.json"
    return (onnx.exists() and onnx.stat().st_size >= MIN_MODEL_BYTES
            and tokjson.exists())


async def prewarm_reranker() -> None:
    """Best-effort startup prewarm: if the reranker model is already cached, load
    the ONNX session now (in a worker thread, race-safe via ensure_reranker's
    lock) so the first neural search skips the ~1-2s init and doesn't race this
    load. Does NOT download (skips when the model is absent). Never raises."""
    try:
        await ensure_reranker(download=False)
    except Exception:
        pass
