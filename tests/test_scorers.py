from sieve.scorers import CompositeScorer, KeywordRelevanceScorer, ScoringStats
from sieve.scorers import PathDepthScorer, score_urls
import pytest


def test_composite_scores_each_child_once():
    child = KeywordRelevanceScorer(["docs"])
    composite = CompositeScorer([child])

    score, reason = composite.score("https://example.test/docs")

    assert score == 1.0
    assert "KeywordRelevanceScorer" in reason
    assert child.stats._urls_scored == 1


def test_scoring_stats_track_first_and_extreme_values():
    stats = ScoringStats()
    stats.update(0.4)
    stats.update(-0.2)
    stats.update(0.9)

    assert stats.get_average() == 0.3666666666666667
    assert stats.get_min() == -0.2
    assert stats.get_max() == 0.9


def test_score_urls_preserves_ties_and_ordered_reasons_without_io(monkeypatch):
    import socket

    def forbidden(*args, **kwargs):
        raise AssertionError("scoring must not open a socket")

    monkeypatch.setattr(socket, "socket", forbidden)
    urls = ["https://example.test/other", "https://example.test/docs/b", "https://example.test/docs/a"]
    results = score_urls(urls, [KeywordRelevanceScorer(["docs"]), PathDepthScorer(optimal_depth=2)])
    assert [item.url for item in results] == [urls[1], urls[2], urls[0]]
    assert results[0].reasons == [
        "KeywordRelevanceScorer: keywords=['docs'] (score=1.000)",
        "PathDepthScorer: path-depth (score=1.000)",
    ]
    assert CompositeScorer([KeywordRelevanceScorer(["docs"])]).score(urls[1])[1] == (
        "KeywordRelevanceScorer=1.000 (keywords=['docs'])"
    )


@pytest.mark.parametrize("extra", [["x"] * 256, ["x" * 129], [None], [""]])
def test_per_call_keyword_configuration_obeys_constructor_limits(extra):
    with pytest.raises(ValueError):
        KeywordRelevanceScorer(["docs"]).score("https://example.test/docs", {"keywords": extra})
