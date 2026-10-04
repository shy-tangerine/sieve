from sieve.metadata import extract_metadata, extract_structured_data


def test_structured_data_flattens_graph_and_arrays():
    html = '''<script type="application/ld+json">
    {"@context":"https://schema.org","@graph":[
      {"@type":"Product","name":"Widget","offers":{"price":"12.00"}},
      {"@type":"Organization","name":"Example"}
    ]}</script>'''
    entities = extract_structured_data(html)
    assert [item["@type"] for item in entities] == ["Product", "Organization"]
    assert entities[0]["offers"]["price"] == "12.00"


def test_malformed_json_ld_is_skipped():
    html = '<script type="application/ld+json">{"@type":"Product",}</script>'
    assert extract_structured_data(html) == []
    assert "structured_data" not in extract_metadata(html, "https://example.com")


def test_structured_data_is_attached_to_metadata():
    html = '<script type="application/ld+json">[{"@type":"Product","name":"Widget"}]</script>'
    metadata = extract_metadata(html, "https://example.com")
    assert metadata["structured_data"][0]["name"] == "Widget"


def test_microdata_is_exposed_with_nested_offer_values():
    html = '''<div itemscope itemtype="https://schema.org/Product">
      <span itemprop="name">Widget</span>
      <div itemprop="offers" itemscope itemtype="https://schema.org/Offer">
        <meta itemprop="price" content="12.00">
        <link itemprop="availability" href="https://schema.org/InStock">
      </div>
    </div>'''
    product = extract_structured_data(html)[0]
    assert product["@type"] == "Product"
    assert product["name"] == "Widget"
    assert product["offers"]["price"] == "12.00"
    assert product["offers"]["availability"].endswith("InStock")
