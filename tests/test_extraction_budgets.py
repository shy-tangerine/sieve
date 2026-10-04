"""Bounded serialization + pruning work tests (#247, #246).

#247: CSS-selector extraction paths must cap selected nodes, per-node
serialized chars and the aggregate before joining, instead of serializing the
whole DOM.

#246: pruning must not serialize every subtree independently; the bottom-up
stats pass produces the same decisions as full-subtree scans.
"""

import pytest
import json
from functools import partial
from types import SimpleNamespace

from sieve import command_router
from sieve.public_output import MAX_PUBLIC_OUTPUT_BYTES, safe_public_json


@pytest.fixture(params=["citations", "scripts", "tables", "records", "similar", "metadata", "pdf"])
def oversized_extraction(request, monkeypatch):
    """Real extraction results, with only PDF decoding and network I/O faked."""
    from sieve.extraction import extract_json
    from sieve.metadata import extract_metadata
    from sieve.pdf_extractor import PdfResult
    from sieve.response_translation import translate_response
    from sieve.script_harvest import harvest_script_json
    from sieve.server import ResponseModel
    from sieve.similar import find_similar
    from sieve.tables import extract_tables
    from sieve.trafilatura_extractor import convert_links_to_citations

    text = '研究 🧪 "quoted" \\ ' * 200
    html = "<html><body>" + "".join(
        f'<div class="record"><span>{i} {text}</span></div>' for i in range(30)
    ) + "</body></html>"
    family = request.param
    if family == "citations":
        converted, references = convert_links_to_citations("[Research](https://example.test/first)\n" + "\n".join(
            f'[{i} {text}](https://example.test/source/{i})' for i in range(30)
        ))
        payload = {"content": [converted, references]}
        assert "⟨1⟩" in converted and "## References" in references
    elif family == "scripts":
        data = json.dumps({"records": [{"name": text}] * 30}, ensure_ascii=False)
        payload = {"content": harvest_script_json(
            f'<script id="__NEXT_DATA__" type="application/json">{data}</script>'
        )}
        assert "__NEXT_DATA__" in payload["content"][0]
    elif family == "tables":
        table = '<table><thead><tr><th>Name</th><th>Index</th></tr></thead>' + "".join(
            f'<tr><td>{text}</td><td>{i}</td></tr>' for i in range(30)
        ) + '</table>'
        payload = {"tables": extract_tables(table)}
        assert payload["tables"][0]["headers"] == ["Name", "Index"]
        assert len(payload["tables"][0]["rows"]) == 30
    elif family == "records":
        payload = {"items": extract_json(html, {
            "baseSelector": ".record",
            "fields": [{"name": "name", "selector": "span", "type": "text"}],
        })}
        assert len(payload["items"]) == 30
    elif family == "similar":
        payload = {"items": find_similar(html, ".record")}
        assert len(payload["items"]) == 29
        assert set(payload["items"][0]) == {"html", "text", "attributes"}
    elif family == "metadata":
        data = json.dumps({"@type": "Article", "name": "Research", "description": text,
                           "parts": [{"text": text}] * 30}, ensure_ascii=False)
        payload = {"metadata": extract_metadata(
            f'<script type="application/ld+json">{data}</script>', "https://example.test"
        )}
        assert payload["metadata"]["title"] == "Research"
        assert payload["metadata"]["structured_data"]
    else:
        from sieve import response_translation

        decoded = PdfResult(content=[text] * 30, content_ok=True,
                            metadata={"title": "Research", "subject": text},
                            table_of_contents=[{"level": 1, "title": text, "page": 1}])
        monkeypatch.setattr(response_translation, "extract_pdf", lambda *a, **kw: decoded)
        page = SimpleNamespace(status=200, url="https://example.test/report.pdf",
                               body=b"%PDF-1.7 fake", encoding="utf-8",
                               headers={"content-type": "application/pdf"})
        result, _ = translate_response(page, "text", None, True, False, "http", 0,
                                       ResponseModel, maximum_bytes=1024)
        assert result.metadata["title"] == "Research"
        assert result.table_of_contents == decoded.table_of_contents
        payload = result
    return family, payload


def _assert_useful_extraction(family, payload):
    if family in {"citations", "scripts", "pdf"}:
        assert "研究" in payload["content"][0] or "\\u7814\\u7a76" in payload["content"][0]
        if family == "citations":
            assert "⟨1⟩" in payload["content"][0]
        elif family == "scripts":
            assert "__NEXT_DATA__" in payload["content"][0]
    elif family == "tables":
        table = payload["tables"][0]
        assert table["headers"] == ["Name", "Index"]
        assert "研究" in table["rows"][0][0]
    elif family == "records":
        assert "研究" in payload["items"][0]["name"]
    elif family == "similar":
        assert "record" in payload["items"][0]["html"]
    else:
        assert payload["metadata"]["structured_data"][0]["name"] == "Research"


def _has_truncation(value):
    if isinstance(value, dict):
        return value.get("_truncated") is True or any(_has_truncation(v) for v in value.values())
    if isinstance(value, list):
        return any(_has_truncation(v) for v in value)
    return isinstance(value, str) and value.endswith("...[_truncated]")


@pytest.mark.asyncio
@pytest.mark.parametrize("max_bytes", [19, 4096])
async def test_extraction_families_have_bounded_public_outputs(oversized_extraction, max_bytes, monkeypatch):
    """Exercise MCP text and structured copies through the production router."""
    family, oversized_extraction = oversized_extraction
    class OfflineServer:
        async def smart_fetch(self, **kwargs):
            return oversized_extraction

    complete = safe_public_json(oversized_extraction)
    assert len(complete.encode("utf-8")) > 4096
    assert len(complete.encode("utf-8")) <= MAX_PUBLIC_OUTPUT_BYTES
    original = json.loads(complete)
    assert not _has_truncation(original)
    # Configure the existing serializer's budget, without replacing its traversal.
    monkeypatch.setattr(command_router, "safe_public_json", partial(safe_public_json, max_bytes=max_bytes))
    content, structured = await command_router.CapabilityRouter(OfflineServer()).dispatch(
        "smart_fetch", {"url": "https://example.test"}
    )
    wire = content[0].text
    assert len(wire.encode("utf-8")) <= max_bytes
    assert structured == json.loads(wire)
    assert _has_truncation(structured)
    assert structured != original
    assert json.loads(safe_public_json(oversized_extraction)) == original
    if max_bytes == 4096:
        _assert_useful_extraction(family, structured)
    else:
        assert structured == {"_truncated": True}


@pytest.mark.asyncio
async def test_extraction_families_respect_default_aggregate_public_limit(oversized_extraction):
    """Many individually valid results must share one production byte cap."""
    family, oversized_extraction = oversized_extraction
    payload = {"results": [oversized_extraction] * 64}
    assert len(safe_public_json(oversized_extraction).encode("utf-8")) * 64 > MAX_PUBLIC_OUTPUT_BYTES

    class OfflineServer:
        async def smart_fetch(self, **kwargs):
            return payload

    content, structured = await command_router.CapabilityRouter(OfflineServer()).dispatch(
        "smart_fetch", {"urls": ["https://example.test"] * 64}
    )
    wire = content[0].text
    assert len(wire.encode("utf-8")) <= MAX_PUBLIC_OUTPUT_BYTES
    assert structured == json.loads(wire)
    assert len(structured["results"]) < 64
    assert structured["results"][-1] == {"_truncated": True}
    _assert_useful_extraction(family, structured["results"][0])
    if family == "pdf":
        first = structured["results"][0]
        assert first["metadata"]["title"] == "Research"
        assert first["table_of_contents"][0]["page"] == 1


@pytest.mark.parametrize("max_bytes", [0, 18])
def test_extraction_families_reject_budgets_without_room_for_marker(oversized_extraction, max_bytes):
    _, oversized_extraction = oversized_extraction
    with pytest.raises(ValueError, match="at least 19"):
        safe_public_json(oversized_extraction, max_bytes=max_bytes)


# ── #247: selected-node serialization caps ────────────────────────────


def _page_with_many_nodes(html: str):
    class FakeEl:
        def __init__(self, root):
            self._root = root

    class FakePage:
        url = "https://example.com"
        body = html.encode("utf-8")
        encoding = "utf-8"

        def css(self, selector):
            from lxml import html as lh
            tree = lh.fromstring(html)
            return [FakeEl(el) for el in tree.cssselect(selector)]

    return FakePage()


def test_extract_html_content_caps_selected_nodes():
    from sieve.extractor import extract_html_content, _MAX_SELECTED_NODES

    html = "<html><body>" + "".join(
        f'<div class="item"><p>paragraph {i}</p></div>' for i in range(300)
    ) + "</body></html>"
    out = extract_html_content(_page_with_many_nodes(html), css_selector=".item")
    # Capped: not all 300 nodes serialized.
    assert out.count("paragraph") <= _MAX_SELECTED_NODES


def test_extract_html_content_caps_aggregate_chars():
    from sieve.extractor import extract_html_content, _MAX_AGGREGATE_SERIALIZED_CHARS

    big = "<p>" + "x" * 20_000 + "</p>"
    html = "<html><body>" + big * 60 + "</body></html>"  # ~1.2 MB matched
    out = extract_html_content(_page_with_many_nodes(html), css_selector="p")
    assert len(out) <= _MAX_AGGREGATE_SERIALIZED_CHARS + len("\n") * 100 + 1000


def test_trafilatura_selector_caps_aggregate(monkeypatch):
    from sieve import trafilatura_extractor as te

    html = "<html><body>" + "".join(
        f'<div class="item"><p>{ "word " * 2000 }</p></div>' for _ in range(60)
    ) + "</body></html>"
    page = _page_with_many_nodes(html)
    # Force the selector path regardless of trafilatura internals.
    monkeypatch.setattr(te, "_extract_type", lambda h, u, t: h)
    out = te.extract_with_trafilatura(page, extraction_type="html", css_selector=".item")
    joined = "".join(out)
    assert len(joined) <= te._MAX_AGGREGATE_SERIALIZED_CHARS + te._MAX_NODE_SERIALIZED_CHARS + 1000


# ── #246: pruning single-pass equivalence ────────────────────────────


def test_pruning_matches_reference_semantics():
    """Pruning keeps high-value content and drops low-value chrome, matching
    the previous per-subtree-serialization behavior's decisions."""
    from sieve.pruning import prune_html

    html = (
        "<html><body>"
        "<nav>Home About Contact Us</nav>"
        "<article><h1>Real Article</h1>"
        "<p>This paragraph carries substantial article content and should survive pruning decisions.</p>"
        "<p>A second paragraph of real prose to give the article block enough text density to score well.</p>"
        "</article>"
        "<footer>copyright all rights reserved links</footer>"
        "</body></html>"
    )
    out = prune_html(html)
    assert "substantial article content" in out
    assert "Home About" not in out
    assert "copyright" not in out


def test_pruning_stats_are_memoized():
    """Repeated scoring of one node reuses cached subtree stats."""
    from lxml import html as lh
    from sieve import pruning

    el = lh.fromstring("<div><p>hello world</p></div>").xpath("//div")[0]
    s1 = pruning._subtree_stats(el)
    s2 = pruning._subtree_stats(el)
    assert s1 is s2


def test_pruning_min_word_threshold_counts_words_not_chars():
    """min_word_threshold is a word budget (per the parameter name); the
    single-pass stats must preserve that semantic."""
    from sieve.pruning import prune_html

    html = (
        "<html><body>"
        "<p>too short</p>"
        f"<p>{'meaningful ' * 40}</p>"
        "</body></html>"
    )
    out = prune_html(html, min_word_threshold=10)
    assert "too short" not in out
    assert "meaningful" in out


def test_pruning_handles_deep_nesting():
    """Deep nesting must not blow the stack or produce pathological work.

    (Deeply-nested text-only divs are pruned by the scoring heuristic in both
    the old per-subtree and new single-pass implementations; this pins that
    the single pass survives the input without RecursionError.)
    """
    from sieve.pruning import prune_html

    depth = 400
    html = "<div>" * depth + "<p>deep content lives here</p>" + "</div>" * depth
    out = prune_html(html)
    assert isinstance(out, str)


def test_pruning_handles_malformed_and_empty_input():
    from sieve.pruning import prune_html

    assert prune_html("") == ""
    assert prune_html(None) == ""  # type: ignore[arg-type]
    assert prune_html("not html at all <<<>>>") is not None
    with pytest.raises(ValueError):
        prune_html("<p>x</p>", threshold=7)
