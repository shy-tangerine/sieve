"""Bounded, resumable batch extraction.

The module deliberately exposes a small async runner so callers can provide an
existing Sieve extractor in tests or in long lived integrations.  Checkpoints
contain only URL completion state and never response bodies or credentials.
"""
from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import math
import os
import tempfile
from pathlib import Path
from typing import Any, Awaitable, Callable, Iterable, TypedDict
from urllib.parse import urlparse
from sieve.security import validate_url
from sieve.public_output import failure_payload, result_diagnostic
from sieve.resource_budget import budgeted_collection, bound_output, current_budget, BudgetExceeded

MAX_INPUT_FILE_BYTES = 2 * 1024 * 1024

Extractor = Callable[[str, dict[str, Any]], Awaitable[dict[str, Any]] | dict[str, Any]]


class BatchRecord(TypedDict, total=False):
    """A bounded result, failure, or resumed checkpoint entry."""
    url: str
    index: int
    ok: bool
    result: Any
    error: str
    category: str
    resumed: bool
    diagnostic: dict[str, Any]
    resource_budget: dict[str, Any]
    skipped: int
    status: str


def read_urls(values: Iterable[str] = (), input_file: str | None = None, *, max_urls: int = 100) -> list[str]:
    """Read and validate a bounded, de-duplicated URL sequence."""
    raw = list(values)
    if input_file:
        path = Path(input_file)
        if not path.is_file():
            raise ValueError(f"input file does not exist: {input_file}")
        if path.stat().st_size > MAX_INPUT_FILE_BYTES:
            raise ValueError("input file exceeds 2 MiB")
        try:
            text = path.read_text(encoding="utf-8")
            loaded = json.loads(text)
            raw.extend(loaded if isinstance(loaded, list) else [loaded])
        except json.JSONDecodeError:
            raw.extend(line.strip() for line in text.splitlines())
    if not raw:
        raise ValueError("at least one URL is required")
    if len(raw) > max_urls:
        raise ValueError(f"URL count exceeds max_urls={max_urls}")
    result: list[str] = []
    seen: set[str] = set()
    for value in raw:
        if not isinstance(value, str) or not value.strip():
            raise ValueError("every URL must be a non-empty string")
        url = value.strip()
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError(f"invalid URL: {url}")
        validate_url(url)
        if url not in seen:
            result.append(url)
            seen.add(url)
    return result


def _read_checkpoint(
    path: Path, config: str, urls: list[str], *, migrate_v1: bool
) -> dict[str, str]:
    if not path.exists():
        return {}
    try:
        if path.stat().st_size > MAX_INPUT_FILE_BYTES:
            raise ValueError("checkpoint exceeds 2 MiB")
        data = json.loads(path.read_text(encoding="utf-8"))
        if data.get("version") == 1:
            if not migrate_v1:
                raise ValueError("v1 checkpoint is unbound; use migrate_v1 or a fresh checkpoint")
            completed = data.get("completed")
            if not isinstance(completed, list) or not all(isinstance(item, str) for item in completed):
                raise ValueError("checkpoint completed entries must be strings")
            legacy = set(completed)
            if not legacy.issubset(urls):
                raise ValueError("v1 checkpoint contains URLs outside the current input")
            return {str(i): "legacy_completed" for i, url in enumerate(urls) if url in legacy}
        if data.get("version") != 2 or not isinstance(data.get("items"), dict):
            raise ValueError("unsupported checkpoint format")
        if data.get("config") != config:
            raise ValueError("checkpoint does not match URLs, schema, timeout, or extractor key")
        items = data["items"]
        indices = {str(i) for i in range(len(urls))}
        if any(key not in indices or
               status not in {"ok", "failed", "legacy_completed"} for key, status in items.items()):
            raise ValueError("invalid checkpoint item status")
        return items
    except (OSError, json.JSONDecodeError, AttributeError, TypeError) as exc:
        raise ValueError(f"corrupt checkpoint: {path}") from exc


def _write_checkpoint(path: Path, config: str, items: dict[str, str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps({"version": 2, "config": config, "items": items}, separators=(",", ":"))
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


@budgeted_collection
async def run_batch(
    urls: list[str], schema: dict[str, Any], *, extractor: Extractor | None = None,
    concurrency: int = 3, timeout: float = 30, max_pages: int | None = None,
    checkpoint: str | None = None, resume: bool = False,
    emit: Callable[[BatchRecord], None] | None = None,
    retry_failed: bool = False, migrate_v1: bool = False,
    checkpoint_key: str | None = None,
    on_progress: Callable[[BatchRecord], Awaitable[None] | None] | None = None,
) -> list[BatchRecord]:
    """Extract URLs with ordered final results and optional completion progress.

    V2 checkpoints bind input order, schema, timeout and checkpoint_key. Custom
    extractor callers must supply a stable key for checkpoints and change it with adapter
    behavior. V1 migration is explicit because its successes/failures are unknown.
    Cancelling an async batch stops queued work; running synchronous adapter
    threads cannot be interrupted by asyncio cancellation.
    """
    if not 1 <= concurrency <= 32:
        raise ValueError("concurrency must be between 1 and 32")
    if not math.isfinite(timeout) or timeout <= 0 or timeout > 3600:
        raise ValueError("timeout must be between 0 and 3600 seconds")
    if max_pages is not None and (max_pages < 1 or max_pages > len(urls)):
        raise ValueError("max_pages must be between 1 and the URL count")
    if (retry_failed or migrate_v1) and not (resume and checkpoint):
        raise ValueError("retry_failed and migrate_v1 require resume and checkpoint")
    urls = urls[:max_pages] if max_pages is not None else urls
    account = current_budget()
    capacity = min(len(urls), account.remaining("pages")) if account is not None else len(urls)
    cp = Path(checkpoint) if checkpoint else None
    if cp and extractor is not None and not (isinstance(checkpoint_key, str) and checkpoint_key.strip()):
        raise ValueError("custom checkpoint extractors require checkpoint_key")
    identity = {"urls": urls, "schema": schema, "timeout": float(timeout),
                "extractor": checkpoint_key or ("custom" if extractor else "sieve-html")}
    config = hashlib.sha256(json.dumps(identity, sort_keys=True, separators=(",", ":"),
                                     allow_nan=False).encode()).hexdigest()
    completed = _read_checkpoint(cp, config, urls, migrate_v1=migrate_v1) if (cp and resume) else {}
    if cp and migrate_v1:
        await asyncio.to_thread(_write_checkpoint, cp, config, completed)
    planned: list[tuple[int, str]] = []
    resumed_entries: list[tuple[int, str]] = []
    skipped = 0
    skipped_index, first_skipped = 0, ""
    for index, url in enumerate(urls):
        if account is not None and not account.charge("items", 1):
            skipped += len(urls) - index
            if not first_skipped:
                skipped_index, first_skipped = index, url[:2048]
            break
        status = completed.get(str(index))
        entries = resumed_entries if status and not (retry_failed and status == "failed") else planned
        if len(entries) < capacity:
            entries.append((index, url))
        else:
            skipped += 1
            if not first_skipped:
                skipped_index, first_skipped = index, url[:2048]
    if skipped and account is not None:
        account.truncated.add("pages")
    owned_server = None
    if extractor is None:
        from sieve.server import MasterFetchServer
        server = MasterFetchServer(cache_ttl=0)
        owned_server = server
        async def extractor(url: str, request_schema: dict[str, Any]) -> dict[str, Any]:
            return await server.extract(url, schema=request_schema, extraction_type="html", timeout=int(timeout * 1000))
    sem = asyncio.Semaphore(concurrency)
    checkpoint_lock = asyncio.Lock()
    stopped = asyncio.Event()
    records: list[BatchRecord] = []

    async def progress(record: BatchRecord) -> None:
        if on_progress is not None:
            event = {key: value for key, value in record.items() if key != "result"}
            event = bound_output(event, owner=run_batch)
            result = on_progress(event)
            if inspect.isawaitable(result):
                await result

    async def one(index: int, url: str) -> BatchRecord:
        async with sem:
            if stopped.is_set():
                raise asyncio.CancelledError
            try:
                account = current_budget()
                if account is not None and (account.expired or not account.charge("pages", 1)):
                    raise BudgetExceeded("batch resource limit")
                item_timeout = min(timeout, account.time_remaining) if account is not None else timeout
                # Run synchronous adapters off-loop so the per-item timeout
                # remains meaningful for both sync and async integrations.
                async with asyncio.timeout(item_timeout):
                    if inspect.iscoroutinefunction(extractor):
                        value = await extractor(url, schema)
                    else:
                        value = await asyncio.to_thread(extractor, url, schema)
                        if inspect.isawaitable(value):
                            value = await value
                value = bound_output(value, owner=run_batch)
                # content_ok is the Sieve extraction contract; unrelated custom `ok` fields are not interpreted.
                failed = isinstance(value, dict) and value.get("content_ok") is False
                record: BatchRecord = {"url": url, "index": index, "ok": not failed, "result": value}
                if failed:
                    failure = failure_payload(RuntimeError("extraction failed"), fallback_category="extract", route="batch")
                    record["error"] = failure["error"]
                    record["category"] = failure["category"]
                if isinstance(value, dict) and account is not None:
                    value["resource_budget"] = account.report()
                record["diagnostic"] = result_diagnostic(value, route="batch")
            except Exception as exc:
                # Rationale: external extractor failures become safe per-URL records; other URLs continue.
                failure = failure_payload(exc, fallback_category="extract", route="batch")
                record = {"url": url, "index": index, "ok": False,
                          "error": failure["error"], "category": failure["category"],
                          "diagnostic": failure["diagnostic"]}
            try:
                if cp:
                    async with checkpoint_lock:
                        completed[str(index)] = "ok" if record["ok"] else "failed"
                        snapshot = dict(completed)
                        # Serialize atomic replacements so old snapshots cannot clobber new ones.
                        await asyncio.to_thread(_write_checkpoint, cp, config, snapshot)
                await progress(record)
            except BaseException:
                # Rationale: persistence/progress failure stops queued work before releasing its slot.
                stopped.set()
                raise
            return record

    pending = [asyncio.create_task(one(i, url)) for i, url in planned]
    # All work runs inside one() under `sem`, so the gather below is bounded
    # by the validated concurrency limit (1..32).
    try:
        records = await asyncio.gather(*pending)
    finally:
        for task in pending:
            if not task.done():
                task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        if owned_server is not None:
            # MasterFetchServer has no close(); the session teardown entrypoint
            # is _shutdown_close_sessions(), which stops idle-timeout monitors
            # and closes every pooled browser session.
            shutdown = getattr(owned_server, "_shutdown_close_sessions", None)
            if shutdown is not None:
                result = shutdown()
                if inspect.isawaitable(result):
                    await result
    for i, url in resumed_entries:
        status = completed[str(i)]
        record: BatchRecord = {"url": url, "index": i, "ok": status == "ok",
                  "resumed": True, "status": status}
        if status != "ok":
            failure = failure_payload(
                RuntimeError("checkpoint outcome requires review or retry"), route="batch")
            record["error"] = failure["error"]
            record["category"] = failure["category"]
            record["diagnostic"] = failure["diagnostic"]
        records.append(record)
        await progress(record)
    if skipped:
        failure = failure_payload(BudgetExceeded("batch page budget"), route="batch")
        records.append({"url": first_skipped, "index": skipped_index, "ok": False,
                        "skipped": skipped, "error": failure["error"], "category": failure["category"],
                        "diagnostic": failure["diagnostic"]})
    records.sort(key=lambda r: r["index"])
    account = current_budget()
    if account is not None:
        for record in records:
            record["resource_budget"] = account.report()
    if emit:
        for record in records:
            emit(record)
    return records
