from lxml import html
from sieve.sanitize import sanitize_html, strip_noise_tags, sanitize_for_ai
from sieve.selectors import generate_css_selector, generate_xpath_selector, selector_hint
from sieve.adblock import should_block_url, should_block_host, AD_DOMAINS
from sieve.autothrottle import parse_retry_after
from email.utils import formatdate
import time

def test_sanitize_strips_script_style_noscript():
    h = "<html><body><script>alert(1)</script><style>.x{}</style><noscript>no</noscript><p>keep</p></body></html>"
    out = sanitize_html(h)
    assert "alert" not in out and "keep" in out

def test_sanitize_hidden_and_zwc():
    h = '<html><body><div style="display:none">hidden</div><p>vis\u200bible</p><!-- c --></body></html>'
    out = sanitize_html(h)
    assert "hidden" not in out and "visible" in out and "c" not in out

def test_sanitize_forbidden_comments():
    h = "<html><body><!-- secret --><p>hi</p></body></html>"
    assert "secret" not in sanitize_html(h)

def test_selector_generation():
    html_str = "<html><body><div id='a'><p>hi</p><p>target</p></div></body></html>"
    hint = selector_hint(html_str, "div p:nth-of-type(2)")
    assert hint["ok"] and hint["css"] and hint["xpath"]
    # direct
    tree = html.fromstring(html_str)
    from lxml.cssselect import CSSSelector
    el = CSSSelector("p:nth-of-type(2)")(tree)[0]
    assert "#a" in generate_css_selector(el)
    assert generate_xpath_selector(el).startswith("//")


def test_generated_selectors_rematch_special_ids_and_full_sibling_paths():
    from lxml.cssselect import CSSSelector
    from sieve.selectors import generate_full_css_selector, generate_full_xpath_selector

    tree = html.fromstring('<html><body><section><p>same</p><p>same</p></section></body></html>')
    target = tree.xpath("//p")[1]
    for identifier in ("plain", "123:a.b", "both'\"quotes", "-1value", "-"):
        target.set("id", identifier)
        assert CSSSelector(generate_css_selector(target))(tree) == [target]
        assert tree.xpath(generate_xpath_selector(target)) == [target]
        assert CSSSelector(generate_full_css_selector(target))(tree) == [target]
        assert tree.xpath(generate_full_xpath_selector(target)) == [target]


def test_selector_hint_bounds_and_no_match():
    import pytest

    assert selector_hint("<p>hello</p>", ".missing") == {"ok": False, "error": "no match"}
    for selector in ("", "p" * 2001, None, "["):
        with pytest.raises(ValueError):
            selector_hint("<p>hello</p>", selector)

def test_adblock():
    assert should_block_url("https://2mdn.net/ads")
    assert should_block_host("sub.2mdn.net")
    assert not should_block_url("https://example.com/page")
    assert len(AD_DOMAINS) > 100_000
    assert should_block_url("https://sub.doubleclick.net/pixel.gif")
    assert not should_block_host("")
    assert not should_block_host("localhost")


def test_adblock_keeps_reviewed_destinations_unblocked():
    # Content/product sites that appear in the upstream lists; excluded by the generator.
    for host in ("www.salesforce.com", "www.webmd.com", "www.semrush.com", "ahrefs.com"):
        assert not should_block_host(host)

def test_retry_after_seconds_and_case_insensitive():
    assert parse_retry_after({"Retry-After": "120"}) == 120
    assert parse_retry_after({"retry-after": "0"}) == 0
    assert parse_retry_after({"RETRY-AFTER": "5"}) == 5
    assert parse_retry_after({}) is None

def test_retry_after_http_date():
    future = formatdate(time.time() + 60, usegmt=True)
    val = parse_retry_after({"Retry-After": future})
    assert val is not None and 0 < val <= 60
    assert parse_retry_after({"Retry-After": "not-a-date"}) is None
