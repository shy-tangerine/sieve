import sys

sys.path.insert(0, "scripts")
from bench_v2 import crawl_metrics, extraction_score, percentile, run_suite


def test_crawl_metrics_are_bounded_and_count_duplicates():
    result = crawl_metrics({"/": ["/a", "/a"], "/a": ["/b"], "/b": []}, max_pages=2)
    assert result == {"visited": 2, "recall": 0.667, "duplicates": 1, "bounded": True}


def test_extraction_score_handles_missing_and_exact_fields():
    assert extraction_score({"a": 1, "b": 2}, {"a": 1})["accuracy"] == 0.5
    assert extraction_score({}, {})["accuracy"] == 1.0


def test_percentile_empty_and_singleton():
    assert percentile([], 95) is None
    assert percentile([4.0], 95) == 4.0


def test_local_fixture_suites_match_quality_contract():
    assert run_suite("crawl", 1)["metrics"] == {"visited": 4, "recall": 1.0, "duplicates": 2, "bounded": True}
    assert run_suite("extraction", 1)["metrics"] == {"fields": 2, "matched": 2, "accuracy": 1.0}
    metrics = run_suite("search", 1)["metrics"]
    assert metrics["results"] == metrics["valid_urls"] == 2
    assert metrics["validity"] == metrics["relevance_hook"]["score"] == 1.0


def test_local_fixture_output_respects_size_budget():
    metrics = run_suite("retrieval", 1)["metrics"]
    assert metrics["quality_hook"] == {"nonempty": True, "records": 3}
    assert metrics["bytes"] <= 285
    assert metrics["output_tokens_est"] <= 71


def test_crawl_metrics_stay_within_page_and_depth_budget():
    graph = {"/": ["/a", "/b"], "/a": ["/deep"], "/b": [], "/deep": []}
    assert crawl_metrics(graph, max_pages=2)["visited"] == 2
    assert crawl_metrics(graph, max_depth=0)["visited"] == 1


def test_fixture_gate_detects_quality_regression(monkeypatch):
    import bench_v2
    monkeypatch.setitem(bench_v2.FIXTURE, "expected_fields", {"title": "broken", "kind": "research"})
    assert run_suite("extraction", 1)["metrics"]["accuracy"] < 1.0
