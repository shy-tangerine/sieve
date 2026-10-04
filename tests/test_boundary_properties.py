"""Bounded, reproducible adversarial properties run without network or timers."""
from hypothesis import given, settings, strategies as st
import pytest
from lxml import html

from sieve.dom_budget import DomWorkBudget, measure_tree
from sieve.resource_budget import ResourceBudget
from sieve.selectors import generate_css_selector
from sieve.sitemap import parse_sitemap
from sieve.script_harvest import cleanup_html

BOUNDED = settings(max_examples=60, deadline=None, derandomize=True)


def test_transport_dns_guard_rejects_private_answer(monkeypatch):
    from sieve.security import SecurityError, resolve_and_check
    monkeypatch.setattr("sieve.security.socket.getaddrinfo", lambda *args, **kwargs:
                        [(2, 1, 6, "", ("127.0.0.1", 443))])
    with pytest.raises(SecurityError):
        resolve_and_check("public-looking.example", 443)


@BOUNDED
@given(st.lists(st.integers(min_value=0, max_value=1000), max_size=80), st.integers(0, 500))
def test_resource_reservations_never_exceed_capacity(requests, capacity):
    account = ResourceBudget(limits={"items": capacity})
    accepted = [account.take("items", amount) for amount in requests]
    assert sum(accepted) == min(sum(requests), capacity)
    assert account.remaining("items") >= 0
    assert account.consumed.get("items", 0) <= capacity


@BOUNDED
@given(st.text(max_size=4096))
def test_malformed_html_cleanup_is_bounded(value):
    title, body, links, images, script = cleanup_html(value)
    assert len(links) <= 2000 and len(images) <= 2000
    assert len(script) <= 200100
    assert len(body) <= 6 * len(value) + 100


@BOUNDED
@given(st.binary(max_size=4096))
def test_arbitrary_sitemap_bytes_are_fail_soft(value):
    urls, children = parse_sitemap(value)
    assert isinstance(urls, list) and isinstance(children, list)
    assert len(urls) <= len(value) and len(children) <= len(value)


@BOUNDED
@given(st.integers(1, 30), st.integers(1, 40))
def test_generated_selectors_match_and_tree_work_is_bounded(siblings, capacity):
    root = html.document_fromstring("<div>" + "<p>text</p>" * siblings + "</div>")
    target = root.xpath("//p")[-1]
    assert root.cssselect(generate_css_selector(target)) == [target]
    budget = DomWorkBudget(total_nodes=capacity)
    measure_tree(root, budget=budget)
    assert budget.used <= capacity
