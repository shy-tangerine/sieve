from sieve.urlfilters import ContentRelevanceFilter, ContentTypeFilter, DomainFilter, FilterChain, URLPatternFilter


def test_pattern_exclusion_overrides_inclusion():
    policy = URLPatternFilter(include=["/docs/*"], exclude=["/docs/private/*"])
    assert policy.accepts("https://example.test/docs/guide")
    assert not policy.accepts("https://example.test/docs/private/api")
    assert not policy.accepts("https://example.test/blog/post")


def test_domain_filter_rejects_lookalikes_and_blocked_subdomains():
    policy = DomainFilter(allowed=["example.test"], blocked=["private.example.test"])
    assert policy.accepts("https://example.test/a")
    assert policy.accepts("https://docs.example.test/a")
    assert not policy.accepts("https://evil-example.test/a")
    assert not policy.accepts("https://private.example.test/a")


def test_mime_context_overrides_extension():
    policy = ContentTypeFilter(["text/html"])
    assert policy.accepts("https://example.test/report", {"content_type": "text/html; charset=utf-8"})
    assert not policy.accepts("https://example.test/report.html", {"content_type": "application/pdf"})


def test_relevance_and_chain_use_context_without_fetching():
    policy = ContentRelevanceFilter("orchid quantum", threshold=0.1)
    assert policy.accepts("https://example.test/a", {"title": "Orchid quantum", "text": "Research"})
    assert not policy.accepts("https://example.test/b", {"title": "Sports scores", "text": "Weather"})
    chain = FilterChain([ContentTypeFilter(["text/html"]), policy])
    assert chain.accepts("https://example.test/a", content_type="text/html", title="Orchid quantum")
    assert not chain.accepts("https://example.test/a", content_type="application/pdf", title="Orchid quantum")
