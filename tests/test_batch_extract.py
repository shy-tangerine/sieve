import asyncio
import json
from pathlib import Path

import pytest

from sieve.batch_extract import read_urls, run_batch


def test_read_urls_file_and_rejects_malformed(tmp_path: Path):
    source = tmp_path / "urls.txt"
    source.write_text("https://a.example\nhttps://a.example\n")
    assert read_urls(input_file=str(source)) == ["https://a.example"]
    with pytest.raises(ValueError):
        read_urls(["ftp://bad.example"])


def test_success_partial_failure_and_checkpoint_resume(tmp_path: Path):
    calls = []

    async def extract(url, schema):
        calls.append(url)
        if "bad" in url:
            raise RuntimeError("blocked")
        return {"items": [{"url": url}]}

    checkpoint = tmp_path / "state.json"
    urls = ["https://a.example", "https://bad.example"]
    records = asyncio.run(run_batch(urls, {}, extractor=extract, checkpoint_key="fixture", checkpoint=str(checkpoint), concurrency=2))
    assert [r["ok"] for r in records] == [True, False]
    assert calls == urls
    again = asyncio.run(run_batch(urls, {}, extractor=extract, checkpoint_key="fixture", checkpoint=str(checkpoint), resume=True))
    assert all(r.get("resumed") for r in again)
    assert [r["ok"] for r in again] == [True, False]
    assert calls == urls
    retried = asyncio.run(run_batch(urls, {}, extractor=extract, checkpoint_key="fixture", checkpoint=str(checkpoint),
                                   resume=True, retry_failed=True))
    assert retried[0]["resumed"] is True
    assert retried[1]["ok"] is False
    assert calls == urls + [urls[1]]


def test_bounds_and_corrupt_checkpoint(tmp_path: Path):
    with pytest.raises(ValueError):
        read_urls([f"https://{i}.example" for i in range(3)], max_urls=2)
    state = tmp_path / "bad.json"
    state.write_text("not-json")
    with pytest.raises(ValueError, match="corrupt checkpoint"):
        asyncio.run(run_batch(["https://a.example"], {}, extractor=lambda *_: {}, checkpoint_key="fixture", checkpoint=str(state), resume=True))
    with pytest.raises(ValueError):
        asyncio.run(run_batch(["https://a.example"], {}, extractor=lambda *_: {}, concurrency=0))


def test_atomic_checkpoint_is_json(tmp_path: Path):
    state = tmp_path / "nested" / "state.json"
    asyncio.run(run_batch(["https://a.example"], {}, extractor=lambda *_: {}, checkpoint_key="fixture", checkpoint=str(state)))
    data = json.loads(state.read_text())
    assert data["version"] == 2
    assert data["items"] == {"0": "ok"}
    assert "a.example" not in state.read_text()


def test_custom_checkpoint_requires_adapter_identity(tmp_path):
    with pytest.raises(ValueError, match="require checkpoint_key"):
        asyncio.run(run_batch(["https://a.example"], {}, extractor=lambda *_: {},
                              checkpoint=str(tmp_path / "state.json")))


@pytest.mark.asyncio
async def test_returned_builtin_failure_is_checkpointed_and_retried(monkeypatch, tmp_path):
    from sieve import server
    calls = []
    class FakeServer:
        def __init__(self, **kwargs):
            pass
        async def extract(self, url, **kwargs):
            calls.append(url)
            return {"content_ok": False, "error": "fetch failed"}
        async def _shutdown_close_sessions(self):
            pass
    monkeypatch.setattr(server, "MasterFetchServer", FakeServer)
    options = {"urls": ["https://a.example"], "schema": {}, "checkpoint": str(tmp_path / "state.json")}
    records = await run_batch(**options)
    assert records[0]["ok"] is False
    assert json.loads((tmp_path / "state.json").read_text())["items"] == {"0": "failed"}
    await run_batch(**options, resume=True, retry_failed=True)
    assert calls == options["urls"] * 2


@pytest.mark.asyncio
async def test_progress_failure_stops_queued_items():
    calls = []
    async def extract(url, schema):
        calls.append(url)
        return {}
    def fail(record):
        raise OSError("output closed")
    with pytest.raises(OSError, match="output closed"):
        await run_batch(["https://a.example/1", "https://a.example/2"], {},
                        extractor=extract, concurrency=1, on_progress=fail)
    assert calls == ["https://a.example/1"]


@pytest.mark.parametrize("override", [
    {"schema": {"new": True}}, {"urls": ["https://b.example"]},
    {"timeout": 10}, {"checkpoint_key": "changed"},
])
def test_checkpoint_identity_rejected_before_extraction(tmp_path, override):
    checkpoint = str(tmp_path / "state.json")
    options = {"urls": ["https://a.example"], "schema": {}, "timeout": 30,
               "extractor": lambda *_: {}, "checkpoint": checkpoint, "checkpoint_key": "fixture"}
    asyncio.run(run_batch(**options))
    options.update(override)
    options["extractor"] = lambda *_: pytest.fail("mismatched checkpoint executed")
    with pytest.raises(ValueError, match="does not match"):
        asyncio.run(run_batch(**options, resume=True))


def test_v1_requires_explicit_migration_and_preserves_unknown_status(tmp_path):
    state = tmp_path / "v1.json"
    old = json.dumps({"version": 1, "completed": ["https://a.example"]})
    state.write_text(old)
    options = {"urls": ["https://a.example"], "schema": {}, "extractor": lambda *_: {},
               "checkpoint": str(state), "checkpoint_key": "fixture", "resume": True}
    with pytest.raises(ValueError, match="v1 checkpoint is unbound"):
        asyncio.run(run_batch(**options))
    assert state.read_text() == old
    records = asyncio.run(run_batch(**options, migrate_v1=True))
    assert records[0]["status"] == "legacy_completed"
    assert records[0]["ok"] is False
    assert json.loads(state.read_text())["version"] == 2


@pytest.mark.asyncio
async def test_progress_arrives_before_completion_and_final_results_stay_ordered():
    release = asyncio.Event()
    progressed = asyncio.Event()
    progress = []
    emitted = []

    async def extract(url, schema):
        if url.endswith("slow"):
            await release.wait()
        return {"value": url}

    def on_progress(record):
        progress.append(record)
        progressed.set()

    task = asyncio.create_task(run_batch(["https://a.example/slow", "https://a.example/fast"], {},
                                        extractor=extract, on_progress=on_progress, emit=emitted.append))
    await asyncio.wait_for(progressed.wait(), 1)
    assert not task.done()
    assert progress[0]["index"] == 1
    assert "result" not in progress[0]
    release.set()
    records = await task
    assert [record["index"] for record in records] == [0, 1]
    assert emitted == records


@pytest.mark.asyncio
async def test_cancel_stops_queued_work_and_closes_owned_server(monkeypatch, tmp_path):
    from sieve import server
    started = asyncio.Event()
    calls = []
    closed = []

    class FakeServer:
        def __init__(self, **kwargs):
            pass

        async def extract(self, url, **kwargs):
            calls.append(url)
            started.set()
            await asyncio.Event().wait()

        async def _shutdown_close_sessions(self):
            closed.append(True)

    monkeypatch.setattr(server, "MasterFetchServer", FakeServer)
    checkpoint = tmp_path / "state.json"
    urls = [f"https://a.example/{i}" for i in range(3)]
    task = asyncio.create_task(run_batch(urls, {}, concurrency=1, checkpoint_key="fixture", checkpoint=str(checkpoint)))
    await asyncio.wait_for(started.wait(), 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert calls == urls[:1]
    assert closed == [True]
    assert not checkpoint.exists()
