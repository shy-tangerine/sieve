"""Issue #249: shared DOM work-budget instrumentation."""

from lxml import html as lxml_html
import pytest

from sieve.dom_budget import DomWorkBudget, measure_tree
from sieve.similar import find_similar
from sieve.links import extract_links
from sieve.metadata import extract_metadata
from sieve.resource_budget import ResourceBudget, budget_scope


HTML = """
<div id="wrap">
  <p class="item">alpha</p><p class="item">beta</p><p class="item">gamma</p>
  <p class="item">delta</p><p class="item">epsilon</p>
</div>
"""


def test_budget_charge_and_exhaustion():
    b = DomWorkBudget(total_nodes=100)
    assert b.charge(60) is True
    assert b.charge(40) is True
    assert b.remaining == 0
    assert b.charge(1) is False          # fails closed once spent
    assert b.charge(0) is False
    assert b.exhausted is True


def test_budget_would_exceed():
    b = DomWorkBudget(total_nodes=10)
    assert b.would_exceed(11) is True
    assert b.would_exceed(5) is False
    b.charge(10)
    assert b.would_exceed(1) is True


def test_measure_tree_counts_nodes_and_text():
    tree = lxml_html.fromstring(HTML)
    metrics = measure_tree(tree)
    assert metrics.node_count > 0
    assert metrics.text_chars >= len("alpha beta gamma delta epsilon") - 4
    assert metrics.max_depth >= 2
    assert metrics.text_ratio > 0


def test_find_similar_budget_bounds_pathological_pages():
    """Thousands of candidate siblings must not serialize unbounded work."""
    # 5000 similar siblings; without the budget each would serialize.
    page = "<div>" + '<p class="item">text</p>' * 5000 + "</div>"
    matches = find_similar(page, "p.item")
    # Correctness preserved for a reasonable set...
    assert len(matches) > 0
    assert all(m["text"] == "text" for m in matches)


def test_find_similar_still_works_small_pages():
    matches = find_similar(HTML, "p.item")
    assert len(matches) == 4  # every sibling except the original


def test_deep_measurement_is_linear_and_never_walks_ancestors():
    class Node:
        text = "x"
        tail = None

        def __init__(self, child=None):
            self.children = () if child is None else (child,)

        def __iter__(self):
            return iter(self.children)

        def getparent(self):
            raise AssertionError("ancestor walks make deep trees quadratic")

    root = Node()
    for _ in range(2500):
        root = Node(root)
    metrics = measure_tree(root)
    assert (metrics.node_count, metrics.text_chars, metrics.max_depth) == (2501, 2501, 2501)


def test_wide_measurement_stops_at_shared_capacity():
    tree = lxml_html.fromstring("<div>" + "<p>x</p>" * 5000 + "</div>")
    budget = DomWorkBudget(total_nodes=20)
    assert measure_tree(tree, budget).node_count == 20
    assert budget.exhausted


def test_consumers_share_exhaustion_before_parser_allocation(monkeypatch):
    page = '<html><title>Example</title><a href="/next">Next</a></html>'
    budget = DomWorkBudget(total_nodes=len(page) + 1)
    assert extract_metadata(page, "https://example.test", work_budget=budget)["title"] == "Example"
    monkeypatch.setattr("sieve.links.lxml_html.fromstring", lambda _: pytest.fail("spent account must refuse parsing"))
    assert extract_links(page, "https://example.test", work_budget=budget)["citations"] == []
    assert budget.exhausted


def test_optional_consumers_report_node_truncation_to_request():
    with budget_scope(ResourceBudget(limits={"nodes": 0})) as account:
        assert find_similar(HTML, "p.item") == []
        assert account.report()["truncated"] == ["nodes"]


def test_shared_small_page_results_match_standalone_consumers():
    page = '<html><title>Example</title><a href="/next">Next</a></html>'
    budget = DomWorkBudget()
    assert extract_metadata(page, "https://example.test", work_budget=budget) == extract_metadata(page, "https://example.test")
    assert extract_links(page, "https://example.test", work_budget=budget) == extract_links(page, "https://example.test")
