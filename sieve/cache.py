"""SQLite-based content cache with TTL support.

Stores fetched content keyed by URL+params hash. Auto-expires entries past TTL.
Uses a shared DB connection pool for efficiency instead of opening a new
connection per operation.
"""
import asyncio
import hashlib
import json
import os
import sqlite3
import stat
import time
from pathlib import Path

import aiosqlite

# Canonical data directory (issue #31): all Sieve state lives under ~/.sieve.
# Cached URLs/content can reveal research history, so the directory is created
# owner-only (0700) and an existing broadly-readable directory is tightened.
_CACHE_DIR = Path.home() / ".sieve" / "cache"
_LEGACY_CACHE_DIR = Path.home() / ".sieve_cache"
_DB_NAME = "cache.db"

DEFAULT_TTL = 3600  # 1 hour
MAX_CACHE_ENTRIES = 10000  # hard cap so a long-lived agent's cache DB can't grow unbounded

# Shared DB path cache — avoids re-running PRAGMA on every operation
_db_initialized: dict[Path, bool] = {}
_db_init_lock = asyncio.Lock()


def _cache_key(url: str, extraction_type: str, css_selector: str | None = None,
               pages: str | None = None, source: str = "live",
               *, main_content_only: bool = True, use_trafilatura: bool = True,
               password: str | None = None) -> str:
    """Deterministic cache key from fetch params.

    ``source`` separates live vs archive.org entries so a page that gets unblocked
    within TTL isn't served a stale archive snapshot (and vice versa).

    Request-affecting options are part of the key (issue #4): extraction flags
    change the returned content, and a PDF password selects which document is
    decrypted — different values must never share an entry. The password itself
    is hashed into the key and never stored or logged.
    """
    pw_part = hashlib.sha256(password.encode()).hexdigest()[:16] if password else ""
    raw = (f"{url}|{extraction_type}|{css_selector or ''}|{pages or ''}|{source or 'live'}"
           f"|{int(bool(main_content_only))}|{int(bool(use_trafilatura))}|{pw_part}")
    return hashlib.sha256(raw.encode()).hexdigest()[:24]


async def _ensure_db(cache_dir: Path | None = None) -> Path:
    """Ensure the DB and table exist. Returns DB path.

    Caches initialization status to avoid redundant PRAGMA calls.
    WAL journal mode for better concurrent read/write performance.
    Busy timeout to handle lock contention gracefully.
    Lock-protected to prevent races during concurrent first-access.

    Issue #31: the directory is created owner-only; a pre-existing
    group/world-readable directory (including the legacy ~/.sieve_cache) is
    tightened to 0700 on first access. Cache rows carry research history.
    """
    d = cache_dir or _CACHE_DIR
    d.mkdir(parents=True, exist_ok=True)
    try:
        mode = stat.S_IMODE(d.stat().st_mode)
        if mode & 0o077:
            os.chmod(d, 0o700)
    except OSError:
        pass
    db_path = d / _DB_NAME

    # Fast path: already initialized, no lock needed
    if _db_initialized.get(db_path):
        return db_path

    async with _db_init_lock:
        # Re-check after acquiring lock (another task may have initialized)
        if _db_initialized.get(db_path):
            return db_path

        async with aiosqlite.connect(db_path) as db:
            # WAL mode: readers don't block writers, writers don't block readers.
            await db.execute("PRAGMA journal_mode=WAL")
            # Wait up to 5s if DB is locked by another connection.
            await db.execute("PRAGMA busy_timeout=5000")
            await db.execute("""
                CREATE TABLE IF NOT EXISTS cache (
                    key TEXT PRIMARY KEY,
                    url TEXT NOT NULL,
                    extraction_type TEXT NOT NULL,
                    content TEXT NOT NULL,
                    status INTEGER NOT NULL,
                    fetched_at REAL NOT NULL,
                    ttl INTEGER NOT NULL DEFAULT 3600
                )
            """)
            await db.execute("CREATE INDEX IF NOT EXISTS idx_fetched_at ON cache(fetched_at)")

            # v3.5.3 schema upgrade: add content_type and total_size_bytes columns.
            # Idempotent — ALTER TABLE raises "duplicate column" if already added.
            # SQLite reuses the same error class as aiosqlite wraps; we catch via sqlite3.
            for ddl in (
                "ALTER TABLE cache ADD COLUMN content_type TEXT NOT NULL DEFAULT ''",
                "ALTER TABLE cache ADD COLUMN total_size_bytes INTEGER NOT NULL DEFAULT 0",
                # v10: envelope round-trips metadata/links/quality_score/toc/page_type/
                # source/archived_at so cache hits restore them (previously lost on hit).
                "ALTER TABLE cache ADD COLUMN envelope TEXT NOT NULL DEFAULT '{}'",
            ):
                try:
                    await db.execute(ddl)
                except sqlite3.OperationalError as exc:
                    if "duplicate column" not in str(exc):
                        raise

            await db.commit()

        _db_initialized[db_path] = True
        return db_path


class _BusyConn:
    """Async CM wrapper that applies PRAGMA busy_timeout on entry."""

    def __init__(self, conn):
        self._conn = conn

    async def __aenter__(self):
        db = await self._conn.__aenter__()
        await db.execute("PRAGMA busy_timeout=5000")
        return db

    async def __aexit__(self, *exc):
        return await self._conn.__aexit__(*exc)


def _connect(db_path: Path) -> _BusyConn:
    """Open a cache DB connection with a busy timeout.

    journal_mode=WAL persists in the DB file, but busy_timeout is
    PER-CONNECTION - without it, concurrent bulk-fetch writers hit
    'database is locked' instantly instead of waiting up to 5s.
    """
    return _BusyConn(aiosqlite.connect(db_path))


# Keys whose values must never be persisted in cache envelopes (issue #254).
_SECRET_ENVELOPE_KEYS = frozenset({
    "password", "api_key", "secret", "token", "authorization", "cookie",
    "cookies", "po_token", "proxy", "proxy_auth", "credentials",
})


def _sanitize_envelope(envelope: dict | None) -> dict:
    """Strip secret-bearing keys from a cache envelope (issue #254).

    The envelope round-trips response metadata; nothing in it needs credential
    material, and a cached row is a persistence boundary. Secret-named keys are
    dropped (recursively) rather than redacted, so no partial value survives.
    """
    if not isinstance(envelope, dict):
        return {}
    clean: dict = {}
    for k, v in envelope.items():
        if str(k).lower() in _SECRET_ENVELOPE_KEYS:
            continue
        if isinstance(v, dict):
            clean[k] = _sanitize_envelope(v)
        elif isinstance(v, list):
            clean[k] = [
                _sanitize_envelope(item) if isinstance(item, dict) else item
                for item in v
            ]
        else:
            clean[k] = v
    return clean


async def get_cached(
    url: str,
    extraction_type: str,
    css_selector: str | None = None,
    ttl: int = DEFAULT_TTL,
    cache_dir: Path | None = None,
    pages: str | None = None,
    source: str = "live",
    *, main_content_only: bool = True, use_trafilatura: bool = True,
    password: str | None = None,
    stale_max_age: int | None = None,
) -> dict | None:
    """Return cached response if fresh, else None.

    Uses the *lesser* of the stored TTL and the caller-requested TTL.
    This prevents serving stale cache when caller wants a fresher window.
    """
    if stale_max_age is not None and not 1 <= stale_max_age <= 86400:
        raise ValueError("stale_max_age must be between 1 and 86400 seconds")
    key = _cache_key(url, extraction_type, css_selector, pages, source,
                     main_content_only=main_content_only,
                     use_trafilatura=use_trafilatura, password=password)
    db_path = await _ensure_db(cache_dir)

    async with _connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        # Use MIN(stored_ttl, requested_ttl) so caller can request fresher data
        now = time.time()
        query = ("SELECT * FROM cache WHERE key = ? AND fetched_at + MIN(ttl, ?) > ?"
                 if stale_max_age is None else
                 "SELECT * FROM cache WHERE key = ? AND fetched_at + ? > ?")
        cursor = await db.execute(
            query,
            (key, ttl if stale_max_age is None else stale_max_age, now),
        )
        row = await cursor.fetchone()
        if row is None:
            return None
        env = row["envelope"] if "envelope" in row.keys() else "{}"
        return {
            "status": row["status"],
            "content": json.loads(row["content"]),
            "url": row["url"],
            "content_type": row["content_type"],
            "total_size_bytes": row["total_size_bytes"],
            "envelope": json.loads(env) if env else {},
            "age_seconds": max(0, now - row["fetched_at"]),
        }


async def set_cached(
    url: str,
    extraction_type: str,
    content: list[str],
    status: int,
    css_selector: str | None = None,
    ttl: int = DEFAULT_TTL,
    cache_dir: Path | None = None,
    content_type: str = "",
    total_size_bytes: int = 0,
    pages: str | None = None,
    source: str = "live",
    envelope: dict | None = None,
    *, main_content_only: bool = True, use_trafilatura: bool = True,
    password: str | None = None,
) -> None:
    """Store a response in cache.

    v3.5.3+: content_type and total_size_bytes round-trip through cache so
    agents preserve MIME info on hits instead of always seeing empty/0.
    v10: ``envelope`` round-trips metadata/links/quality_score/toc/page_type/
    source/archived_at so cache hits restore the full research-grade response
    (previously these fields were silently dropped on cache hits).

    Issue #254: the envelope is sanitized of secret-bearing keys before it
    touches disk; issue #4: request-affecting options join the cache key (the
    PDF password is hashed, never stored).
    """
    key = _cache_key(url, extraction_type, css_selector, pages, source,
                     main_content_only=main_content_only,
                     use_trafilatura=use_trafilatura, password=password)
    db_path = await _ensure_db(cache_dir)
    env_json = json.dumps(_sanitize_envelope(envelope)) if envelope else "{}"

    async with _connect(db_path) as db:
        await db.execute(
            """INSERT OR REPLACE INTO cache
               (key, url, extraction_type, content, status, fetched_at, ttl,
                content_type, total_size_bytes, envelope)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (key, url, extraction_type, json.dumps(content), status,
             time.time(), ttl, content_type, total_size_bytes, env_json),
        )
        # Bound the cache: if over MAX_CACHE_ENTRIES, evict the oldest rows by
        # fetched_at down to 90% of the cap. Cheaper than per-insert single
        # evictions and amortizes the cost (only runs when the cap is exceeded).
        count_cursor = await db.execute("SELECT COUNT(*) FROM cache")
        (count,) = await count_cursor.fetchone()
        if count > MAX_CACHE_ENTRIES:
            excess = count - int(MAX_CACHE_ENTRIES * 0.9)
            await db.execute(
                "DELETE FROM cache WHERE key IN "
                "(SELECT key FROM cache ORDER BY fetched_at ASC LIMIT ?)",
                (excess,),
            )
        await db.commit()


async def clear_cache(cache_dir: Path | None = None) -> int:
    """Clear all expired entries. Returns count of purged rows."""
    db_path = await _ensure_db(cache_dir)
    async with _connect(db_path) as db:
        cursor = await db.execute(
            "DELETE FROM cache WHERE fetched_at + ttl <= ?", (time.time(),)
        )
        await db.commit()
        return cursor.rowcount


async def clear_all_cache(cache_dir: Path | None = None) -> int:
    """Nuke the entire cache. Returns count of purged rows."""
    db_path = await _ensure_db(cache_dir)
    async with _connect(db_path) as db:
        cursor = await db.execute("DELETE FROM cache")
        await db.commit()
        return cursor.rowcount
