"""Pins for 4 crawl4ai ports: citations, tables, pruning, chunking."""
from sieve.trafilatura_extractor import convert_links_to_citations, to_fit_markdown
from sieve.tables import extract_tables
from sieve.pruning import prune_html, filter_content
from sieve.chunking import RegexChunking, IdentityChunking
from sieve.antibot_detector import detect_block


def test_detector_high_confidence_challenge_on_http_200():
    body = '<form id="challenge-form"><input name="__cf_chl_f_tk=" value="x"></form>'
    assert detect_block(200, {}, body)["blocked"] is True


def test_detector_article_mentions_do_not_trigger_block():
    body = "<article><h1>Security review</h1><p>Access Denied is discussed here. " + "Useful reporting. " * 8 + "</p></article>"
    assert detect_block(200, {}, body)["blocked"] is False


def test_detector_medium_signal_requires_error_status():
    body = "<main><h1>Access Denied</h1><p>" + "Please review the policy. " * 4 + "</p></main>"
    assert detect_block(404, {}, body)["blocked"] is True
    assert detect_block(200, {}, body)["blocked"] is False
    assert detect_block(429, {}, "")["blocked"] is True

def test_citations_basic():
    md="[hello](https://example.com) world [hello](https://example.com)"
    conv, refs = convert_links_to_citations(md)
    assert "⟨1⟩" in conv
    assert conv.count("⟨1⟩")==2
    assert "## References" in refs
    assert "https://example.com" in refs

def test_citations_relative():
    md="[x](/path)"
    conv, refs = convert_links_to_citations(md, base_url="https://example.com/base/")
    assert "https://example.com/path" in refs

def test_fit_markdown():
    html="<html><body><article><p>keep this</p></article><nav>remove</nav></body></html>"
    raw, pruned = to_fit_markdown(html)
    assert "keep this" in raw

def test_tables_colspan():
    html='<table><thead><tr><th>A</th><th>B</th></tr></thead><tr><td colspan="2">x</td></tr></table>'
    tables = extract_tables(html)
    assert len(tables)==1
    assert tables[0]["headers"]==["A","B"]
    assert tables[0]["rows"][0]==["x","x"]

def test_tables_rowspan():
    html='<table><thead><tr><th>C</th><th>V</th></tr></thead><tr><td rowspan="2">r</td><td>a</td></tr><tr><td>b</td></tr></table>'
    tables = extract_tables(html)
    assert tables[0]["rows"][0][0]=="r"
    assert tables[0]["rows"][1][0]=="r"

def test_tables_headerless_layout_is_rejected():
    html='<table><tr><td>a</td><td>b</td></tr><tr><td>c</td><td>d</td></tr></table>'
    assert extract_tables(html)==[]

def test_tables_multi_row_header_uses_first_header_row():
    html='<table><thead><tr><th colspan="2">Group</th></tr><tr><th>A</th><th>B</th></tr></thead><tr><td>1</td><td>2</td></tr></table>'
    assert extract_tables(html)[0]["headers"]==["Group", "Group"]

def test_tables_rowspan_colspan_intersection():
    html='<table><thead><tr><th>H1</th><th>H2</th><th>H3</th></tr></thead><tr><td rowspan="2" colspan="2">X</td><td>A</td></tr><tr><td>B</td></tr></table>'
    assert extract_tables(html)[0]["rows"]==[["X", "X", "A"], ["X", "X", "B"]]

def test_tables_caption_and_summary_are_preserved():
    html='<table summary="sum"><caption>Cap</caption><thead><tr><th>A</th></tr></thead><tr><td>x</td></tr></table>'
    assert extract_tables(html)[0] == {
        "headers": ["A"],
        "rows": [["x"]],
        "caption": "Cap",
        "summary": "sum",
        "metadata": {"row_count": 1, "column_count": 1},
    }

def test_tables_malformed_span_is_skipped():
    html='<table><caption>x</caption><tr><th>A</th></tr><tr><td colspan="nope">x</td></tr></table>'
    assert extract_tables(html)==[]


def test_tables_bound_spans_before_expansion():
    from lxml import etree
    from sieve.tables import MAX_COLUMNS, _expand_spans

    enormous = "9" * 5000
    cells = [etree.fromstring(f'<th colspan="{enormous}">x</th>')]
    assert _expand_spans(cells) == ["x"] * MAX_COLUMNS
    html = f'<table><caption>Data</caption><tr><th colspan="{enormous}">H</th></tr>'
    html += f'<tr><td colspan="{enormous}" rowspan="{enormous}">x</td></tr><tr></tr></table>'
    table = extract_tables(html)[0]
    assert table["rows"] == [["x"] * MAX_COLUMNS] * 2


def test_tables_bound_aggregate_grid_before_alignment(monkeypatch):
    import sieve.tables as tables

    monkeypatch.setattr(tables, "MAX_AGGREGATE_CELLS", 6)
    first = '<table><caption>A</caption><tr><th>A</th><th>B</th></tr>'
    first += '<tr><td>a</td><td>b</td></tr>' * 2 + '</table>'
    second = '<table><caption>B</caption><tr><th>C</th></tr>'
    second += '<tr><td>c</td></tr>' * 5 + '</table>'
    result = tables.extract_tables(first + second)
    assert [len(table["rows"]) for table in result] == [2, 2]
    assert sum(len(row) for table in result for row in table["rows"]) == 6

def test_tables_nested_data_table_does_not_pollute_qualifying_outer_table():
    html='''<table summary="outer summary"><caption>Outer</caption>
      <thead><tr><th>Name</th><th>Value</th></tr></thead>
      <tr><td>Alpha</td><td><table summary="inner summary"><caption>Inner</caption>
        <tr><th>Code</th></tr><tr><td>Y</td></tr></table></td></tr>
    </table>'''
    assert extract_tables(html) == [
        {
            "headers": ["Name", "Value"],
            "rows": [["Alpha", ""]],
            "caption": "Outer",
            "summary": "outer summary",
            "metadata": {"row_count": 1, "column_count": 2},
        },
        {
            "headers": ["Code"],
            "rows": [["Y"]],
            "caption": "Inner",
            "summary": "inner summary",
            "metadata": {"row_count": 1, "column_count": 1},
        },
    ]

def test_pruning_removes_nav():
    html="<html><body><p>important content here with enough text to survive pruning threshold</p><nav>nav junk</nav></body></html>"
    out = prune_html(html)
    assert "important content" in out

def test_pruning_filter_content():
    html="<html><body><p>hello world</p></body></html>"
    blocks = filter_content(html)
    assert len(blocks)>=1

def test_chunk_regex():
    c = RegexChunking(patterns=[r"\n\n"])
    assert c.chunk("a\n\nb\n\nc")==["a","b","c"]

def test_chunk_identity():
    c = IdentityChunking()
    assert c.chunk("hello")==["hello"]

def test_chunk_regex_multi_pattern():
    c = RegexChunking(patterns=[r"\n\n", r"\. "])
    chunks = c.chunk("a. b\n\nc")
    assert len(chunks)>=2
