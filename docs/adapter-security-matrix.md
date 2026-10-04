# Adapter security matrix

The shared corpus in `tests/test_adapter_security_matrix.py` reaches both actual
`HTTPSession.get` and `_bounded_http_fetch` entry points. It uses real URL/DNS
guards, deterministic DNS answers and fake transports. Unsafe literal IPs,
metadata addresses, IPv6 loopback and public-looking private DNS names are
tested both initially and through a redirect. No private target is opened.

| Adapter | Arbitrary URL / DNS / redirect | Other applicable evidence |
|---|---|---|
| HTTPSession and httpx fallback | Shared corpus; test_fetcher_fallback redirect tests | timeout, proxy override, fallback and streamed byte accounting in test_fetcher_fallback |
| Domain discovery stdlib helper | Shared corpus; test_domain_discovery_security | bounded read, three-hop limit and explicit opener timeout; no per-call proxy input |
| Integration HTTP adapter | test_integrations allowlist/DNS/redirect corpus | streaming body cap, pagination, authentication/redaction, deadline |
| Sitemap harvest transport | test_sitemap_harvest and redirect-policy corpus | byte cap and fail-soft None result; nested sitemap validation in test_sitemap |
| Crawl sitemap transport | test_crawl DNS and private-redirect regressions | bounded stdlib reads use remaining bytes plus one sentinel; shared decompression, parser/item and deadline limits |
| Browser navigation | Browser-specific security/navigation tests | browser navigation policy, proxy and profile containment; HTTP fake corpus does not represent browser behavior |
| Fixed provider endpoints | Endpoint constants; arbitrary-host input is inapplicable | provider-specific deadline, body, authentication and redaction tests remain applicable |
| Parser-only sitemap/reddit paths | Network/DNS/redirect/proxy are inapplicable | bounded input, XML entity/network refusal and parser properties |
| yt-dlp/STT and other subprocess adapters | HTTP transport semantics are inapplicable | argv, executable allowlist, shell refusal, timeout, output cap and safe diagnostics tests |

This table identifies differences rather than claiming identical adapters.
Under a request budget, Primp streams through chunks no larger than 64 KiB.
Bytes are charged before retention, and an over-budget response is rejected
without parsing a partial document. Small accounts read only their remaining
allowance plus one sentinel; larger accounts can incur one bounded native
chunk of overhead. The translator recognizes the same account and avoids a
second debit. Without a request account, the direct low-level HTTP helper
retains its buffered behavior. The supported Primp minimum is 1.2.3, whose
streaming interface is checked in every isolated compatibility environment.
The fixed public serializer cap is separate from request content/work budgets.
Mutation calibration in `scripts/check_security_mutations.py --run` verifies
that representative guard removals are caught by their specific offline tests.
