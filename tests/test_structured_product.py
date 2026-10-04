"""Smoke-test: ``sieve fetch --structured`` exposes Product entities with
name, price, and availability through the real extraction pipeline.

The CLI and metadata tests mock the server; this test proves the underlying
extraction seam delivers the three Product fields that the structured-fetch
contract promises.  No HTTP required — the HTML fixture is processed directly
through ``extract_structured_data``.
"""
import json
import sys

from sieve import cli
from sieve.metadata import extract_structured_data, extract_metadata

# ── Fixture: real-world-ish JSON-LD Product markup ────────────────────────
_PRODUCT_JSON_LD = """\
<script type="application/ld+json">
{
  "@context": "https://schema.org",
  "@type": "Product",
  "name": "Widget Pro 3000",
  "description": "A high-quality widget for discerning widget enthusiasts.",
  "image": "https://example.com/widget.jpg",
  "brand": {"@type": "Brand", "name": "WidgetCo"},
  "offers": {
    "@type": "Offer",
    "price": "29.99",
    "priceCurrency": "USD",
    "availability": "https://schema.org/InStock",
    "seller": {"@type": "Organization", "name": "WidgetCo Store"}
  }
}
</script>
"""

# ── Fixture: microdata Product markup ────────────────────────────────────
_PRODUCT_MICRODATA = """\
<div itemscope itemtype="https://schema.org/Product">
  <span itemprop="name">Widget Pro 3000</span>
  <meta itemprop="description" content="A high-quality widget.">
  <div itemprop="offers" itemscope itemtype="https://schema.org/Offer">
    <meta itemprop="price" content="29.99">
    <meta itemprop="priceCurrency" content="USD">
    <link itemprop="availability" href="https://schema.org/InStock">
  </div>
</div>
"""

# ── Fixture: multi-entity JSON-LD (@graph) with Product + Organization ───
_PRODUCT_GRAPH = """\
<script type="application/ld+json">
{
  "@context": "https://schema.org",
  "@graph": [
    {
      "@type": "Product",
      "name": "Widget Pro 3000",
      "offers": {
        "@type": "Offer",
        "price": "29.99",
        "availability": "https://schema.org/InStock"
      }
    },
    {
      "@type": "Organization",
      "name": "WidgetCo",
      "url": "https://example.com"
    }
  ]
}
</script>
"""


def test_jsonld_product_exposes_name_price_availability():
    """JSON-LD Product entity passes name, price, availability through
    extract_structured_data."""
    entities = extract_structured_data(_PRODUCT_JSON_LD)
    assert len(entities) >= 1
    product = next(e for e in entities if e.get("@type") == "Product")
    assert product["name"] == "Widget Pro 3000"
    assert product["offers"]["price"] == "29.99"
    assert "InStock" in product["offers"]["availability"]


def test_microdata_product_exposes_name_price_availability():
    """Microdata Product entity passes name, price, availability through
    extract_structured_data."""
    entities = extract_structured_data(_PRODUCT_MICRODATA)
    assert len(entities) >= 1
    product = next(e for e in entities if e.get("@type") == "Product")
    assert product["name"] == "Widget Pro 3000"
    assert product["offers"]["price"] == "29.99"
    assert "InStock" in product["offers"]["availability"]


def test_graph_product_exposes_name_price_availability():
    """JSON-LD @graph flattening preserves Product name, price, availability."""
    entities = extract_structured_data(_PRODUCT_GRAPH)
    product = next(e for e in entities if e.get("@type") == "Product")
    assert product["name"] == "Widget Pro 3000"
    assert product["offers"]["price"] == "29.99"
    assert "InStock" in product["offers"]["availability"]
    # Organization is a sibling, not merged into Product.
    orgs = [e for e in entities if e.get("@type") == "Organization"]
    assert len(orgs) == 1


def test_product_fields_visible_in_metadata_envelope():
    """extract_metadata surfaces structured_data with Product fields, proving
    the CLI --structured envelope reaches Product consumers."""
    meta = extract_metadata(_PRODUCT_JSON_LD, "https://example.com/product")
    product = next(
        e for e in meta["structured_data"] if e.get("@type") == "Product"
    )
    assert product["name"] == "Widget Pro 3000"
    assert product["offers"]["price"] == "29.99"
    assert "InStock" in product["offers"]["availability"]


def test_structured_fetch_cli_envelope_contains_product_fields(monkeypatch, capsys):
    """End-to-end CLI proof using deterministic HTML extraction.

    The fake transport returns the same kind of response as the production
    server, but computes metadata through ``extract_metadata`` from a local
    fixture.  This keeps the test offline while proving the observable CLI
    envelope contains the Product fields.
    """
    class _StructuredResponse:
        def model_dump(self):
            return {
                "url": "https://example.com/product",
                "status": 200,
                "content": [_PRODUCT_JSON_LD],
                "metadata": extract_metadata(
                    _PRODUCT_JSON_LD, "https://example.com/product"
                ),
            }

    class FakeServer:
        def __init__(self, *, cache_ttl):
            pass

        async def smart_fetch(self, url, **kwargs):
            assert kwargs["extraction_type"] == "structured"
            return _StructuredResponse()

    monkeypatch.setattr("sieve.server.MasterFetchServer", FakeServer)
    monkeypatch.setattr(
        sys,
        "argv",
        ["sieve", "fetch", "--structured", "https://example.com/product"],
    )

    assert cli.main() == 0
    output = json.loads(capsys.readouterr().out)
    product = next(
        entity
        for entity in output["metadata"]["structured_data"]
        if entity.get("@type") == "Product"
    )
    assert product["name"] == "Widget Pro 3000"
    assert product["offers"]["price"] == "29.99"
    assert "InStock" in product["offers"]["availability"]
