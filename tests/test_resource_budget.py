import asyncio

import pytest

from sieve.resource_budget import ResourceBudget, budget_scope, budgeted, current_budget


@pytest.mark.asyncio
async def test_batch_bounds_tasks_before_fanout_and_reports_omitted_urls():
    from sieve.batch_extract import run_batch
    calls = []
    async def extractor(url, schema):
        calls.append(url)
        return "ok"
    account = ResourceBudget(limits={"pages": 2})
    with budget_scope(account):
        records = await run_batch([f"https://example.com/{i}" for i in range(1000)], {}, extractor=extractor)
    assert len(calls) == 2
    assert len(records) == 3
    assert records[-1]["skipped"] == 998 and records[-1]["category"] == "budget"


@pytest.mark.asyncio
async def test_batch_resume_advances_past_completed_prefix_with_low_capacity(tmp_path):
    import json
    from sieve.batch_extract import run_batch
    urls = [f"https://example.com/{i}" for i in range(5)]
    checkpoint = tmp_path / "checkpoint.json"
    checkpoint.write_text(json.dumps({"version": 1, "completed": urls[:2]}))
    calls = []
    async def extractor(url, schema):
        calls.append(url)
        return "ok"
    with budget_scope(ResourceBudget(limits={"pages": 2})):
        records = await run_batch(urls, {}, extractor=extractor, checkpoint_key="fixture", checkpoint=str(checkpoint),
                                  resume=True, migrate_v1=True)
    assert calls == urls[2:4]
    assert [record["index"] for record in records] == list(range(5))
    assert json.loads(checkpoint.read_text())["items"] == {
        "0": "legacy_completed", "1": "legacy_completed", "2": "ok", "3": "ok"}


@pytest.mark.asyncio
@pytest.mark.parametrize("runner", ["batch", "pipeline"])
async def test_collectors_cancel_children_at_shared_deadline(runner):
    from sieve.batch_extract import run_batch
    from sieve.pipeline import run_pipeline
    cancelled = asyncio.Event()
    async def slow(*args):
        try:
            await asyncio.sleep(10)
        finally:
            cancelled.set()
    account = ResourceBudget(timeout_seconds=0.02)
    with budget_scope(account):
        if runner == "batch":
            records = await run_batch(["https://example.com"], {}, extractor=slow, timeout=1)
        else:
            records = await run_pipeline({"nodes": [{"id": "one", "stage": "fetch", "url": "https://example.com"}]}, fetch=slow, timeout=1)
    assert cancelled.is_set()
    assert records[0]["category"] == "timeout"
    assert "deadline" in account.report()["truncated"]


@pytest.mark.asyncio
async def test_batch_children_share_input_budget_and_preserve_record_order():
    from sieve.batch_extract import run_batch
    from sieve.resource_budget import BudgetExceeded
    accounts = []
    async def extractor(url, schema):
        account = current_budget()
        accounts.append(account)
        if not account.charge("input_bytes", 4):
            raise BudgetExceeded("input exhausted")
        return {"text": "small"}
    account = ResourceBudget(limits={"input_bytes": 7})
    with budget_scope(account):
        records = await run_batch(["https://example.com/1", "https://example.com/2"], {},
                                  extractor=extractor, concurrency=1)
    assert accounts == [account, account]
    assert [record["index"] for record in records] == [0, 1]
    assert records[0]["ok"] and records[1]["category"] == "budget"
    assert account.consumed["input_bytes"] == 7


@pytest.mark.asyncio
async def test_batch_accounts_payload_once_before_jsonl_emission():
    from sieve.batch_extract import run_batch
    from sieve.resource_budget import bound_output
    async def extractor(url, schema):
        return bound_output({"text": "abcde"})
    emitted = []
    account = ResourceBudget(limits={"output_chars": 9})
    with budget_scope(account):
        await run_batch(["https://example.com/1"], {}, extractor=extractor, emit=emitted.append)
    assert emitted[0]["result"]["text"] == "abcde"
    assert account.consumed["output_chars"] == 9


@pytest.mark.asyncio
async def test_pipeline_stages_share_cumulative_output_budget():
    from sieve.pipeline import run_pipeline
    accounts = []
    async def fetch(url):
        accounts.append(current_budget())
        return "abcde"
    spec = {"nodes": [{"id": str(i), "stage": "fetch", "url": "https://example.com"} for i in range(2)]}
    account = ResourceBudget(limits={"output_chars": 8})
    with budget_scope(account):
        records = await run_pipeline(spec, fetch=fetch)
    assert accounts == [account, account]
    assert [record["result"] for record in records] == ["abcde", "abc"]
    assert records[-1]["resource_budget"]["truncated"] == ["output_chars"]


@pytest.mark.parametrize("value", [True, -1, 1.5, "1"])
def test_limits_reject_invalid_values(value):
    with pytest.raises(ValueError):
        ResourceBudget(limits={"nodes": value})


@pytest.mark.parametrize("value", [True, -1, float("inf"), float("nan")])
def test_deadline_rejects_invalid_values(value):
    with pytest.raises(ValueError):
        ResourceBudget(timeout_seconds=value)


def test_aggregate_account_reserves_before_work_and_reports_exhaustion():
    account = ResourceBudget(limits={"nodes": 3, "output_chars": 0})
    assert account.charge("nodes", 2)
    assert not account.charge("nodes", 2)
    assert account.consumed["nodes"] == 3
    assert account.take("output_chars", 10) == 0
    assert account.report()["truncated"] == ["nodes", "output_chars"]


def test_deadline_prevents_any_new_work(monkeypatch):
    account = ResourceBudget(timeout_seconds=1)
    monkeypatch.setattr("sieve.resource_budget.monotonic", lambda: account._started + 2)
    assert not account.charge("pages", 1)
    assert account.report() == {"consumed": {}, "truncated": ["deadline", "pages"]}


@pytest.mark.asyncio
async def test_nested_tasks_share_an_account_and_roots_are_isolated():
    @budgeted
    async def nested():
        account = current_budget()
        await asyncio.sleep(0)
        assert account is current_budget()
        account.charge("items", 1)
        return account

    with budget_scope() as parent:
        assert all(account is parent for account in await asyncio.gather(nested(), nested()))
        assert parent.consumed["items"] == 2
    first, second = await asyncio.gather(nested(), nested())
    assert first is not second
    assert current_budget() is None
