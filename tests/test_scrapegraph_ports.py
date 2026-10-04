"""ScrapeGraphAI ports: script harvest + html2text baseurl + tolerant JSON."""
from sieve.script_harvest import extract_from_script_tags, minify_html, cleanup_html, convert_to_md, harvest_script_json
from sieve.extraction import parse_relaxed_json, normalize_schema, extract_json

def test_script_harvest_next_data():
    html = '<html><head><title>T</title></head><body><script id="__NEXT_DATA__" type="application/json">{"props":{"x":1}}</script></body></html>'
    out = extract_from_script_tags(html)
    assert "__NEXT_DATA__" in out and '"x": 1' in out

def test_script_harvest_ld_json():
    html = '<script type="application/ld+json">{"@type":"Product","name":"A"}</script>'
    out = extract_from_script_tags(html)
    assert "JSON-LD" in out

def test_script_harvest_window():
    # window.* assignments are not emitted verbatim (raw dynamic assignments are
    # dropped); only the parsed JSON payload is surfaced.
    html = '<script>window.__DATA__ = {"a":1};</script>'
    out = extract_from_script_tags(html)
    assert '"a": 1' in out
    assert "window.__DATA__ =" not in out

def test_minify_html():
    assert minify_html("<!-- c --> <div>  a  </div>") == "<div> a </div>"
    assert minify_html("<!-- only a comment -->") == ""


def test_minification_preserves_attribute_and_raw_text_whitespace():
    from lxml import html
    source = '<div title="a  b"><script>{"value":"a  b"}</script><pre> a\n  b </pre></div>'
    result = html.fromstring(minify_html(source))
    assert result.get("title") == "a  b"
    assert result.find("script").text == '{"value":"a  b"}'
    assert result.find("pre").text == " a\n  b "

def test_cleanup_html():
    title, body, links, images, script = cleanup_html('<html><head><title>Hi</title></head><body><a href="/x">a</a><script id="__NEXT_DATA__">{"a":1}</script></body></html>', base_url="https://example.com")
    assert title == "Hi"
    assert links == ["https://example.com/x"]


def test_cleanup_uses_html_elements_and_preserves_script_body_boundary():
    html = '''<html><head><title>Title &amp; text</title></head><body>
    <!-- <a href="/fake">bad</a><img src="/fake.png"> -->
    <script id="__NEXT_DATA__">{"text":"</body><a href='/script'>fake</a>"}</script>
    <a data-label="x>y" href="/real">real</a><img src="/real.png"><p>after script</p>
    </body></html>'''
    title, body, links, images, script = cleanup_html(html, "https://example.com")
    assert title == "Title & text"
    assert links == ["https://example.com/real"]
    assert images == ["https://example.com/real.png"]
    assert "after script" in body
    assert '"text"' in script and "</body>" in script

def test_harvest_list():
    html = '<script id="__NEXT_DATA__">{"a":1}</script>'
    assert len(harvest_script_json(html)) == 1
    assert harvest_script_json("<p>hi</p>") == []

def test_convert_to_md_baseurl():
    html = '<a href="/path">link</a>'
    md = convert_to_md(html, base_url="https://example.com")
    assert "https://example.com/path" in md

def test_convert_to_md_no_baseurl():
    md = convert_to_md("<p>hello</p>")
    assert "hello" in md.lower()

def test_tolerant_json_trailing_comma():
    s = '{"baseSelector": "div", "fields": [{"name": "a", "selector": ".a", "type": "text",},],}'
    d = parse_relaxed_json(s)
    assert d["baseSelector"] == "div"
    assert d["fields"][0]["name"] == "a"

def test_tolerant_json_comments():
    s = '{ // comment\n"baseSelector": "div", /* block */ "fields": [{"name":"a","selector":".a","type":"text"}]\n}'
    d = parse_relaxed_json(s)
    assert d["fields"][0]["name"] == "a"

def test_normalize_schema_aliases():
    s = {"base_selector": "div", "fields": [{"name": "a", "css": ".a", "type": "text"}]}
    n = normalize_schema(s)
    assert n["baseSelector"] == "div"
    assert n["fields"][0]["selector"] == ".a"

def test_extract_with_tolerant_schema():
    html = '<div class="p"><span class="t">hello</span></div>'
    schema = parse_relaxed_json('{"baseSelector": "div.p", "fields": [{"name": "title", "selector": "span.t", "type": "text",},],}')
    schema = normalize_schema(schema)
    items = extract_json(html, schema)
    assert items[0]["title"] == "hello"

def test_fallback_wired():
    # ensure _fallback_extract now returns harvest-augmented markdown when script JSON present
    from sieve.trafilatura_extractor import _fallback_extract
    class FakePage:
        content = '<html><head><title>T</title></head><body><p>hi</p><script id="__NEXT_DATA__">{"x":1}</script></body></html>'
        body = content.encode()
        encoding = "utf-8"
        url = "https://example.com"
    out = _fallback_extract(FakePage(), "markdown", None)
    assert out and "hi" in out[0].lower()
