from sieve.batch_extract import BatchRecord
from sieve.sdk import BatchRecord as PublicBatchRecord


def test_sdk_uses_one_batch_record_contract():
    assert PublicBatchRecord is BatchRecord
    assert {"url", "index", "ok", "result", "error", "category", "diagnostic"} <= BatchRecord.__annotations__.keys()
