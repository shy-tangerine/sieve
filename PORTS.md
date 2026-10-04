# Upstream provenance

Sieve includes focused adaptations from open-source projects. This file records
where those ideas or implementations came from and how they are licensed. Sieve
does not present the upstream projects as endorsing this work.

## Integrated adaptations

| Sieve component | Upstream source | What was adapted | License |
|---|---|---|---|
| `youtube.py` | [yt-dlp](https://github.com/yt-dlp/yt-dlp) | Bounded subprocess integration; no yt-dlp source is vendored | Unlicense |
| `osint.py` | [Maigret](https://github.com/soxoj/maigret) | Bounded optional subprocess adapter using the upstream `--json ndjson` CLI; no Maigret source or dependency is bundled | MIT |
| `image_search.py` | [Brave Search API](https://api.search.brave.com/app/documentation/images) | Bounded HTTP client for documented image-search endpoint; no Brave source or SDK is bundled | Provider API terms; no upstream code copied |
| `adblock.py` | [The Block List Project](https://github.com/blocklistproject/Lists/tree/ee3bdbaddf49c756cf3340bb2e6a9dee5282d1c6) | Domain data in `sieve/data/adblock_domains.txt` from pinned `ads.txt` and `tracking.txt` snapshots; inputs, terms files, hashes, retrieval date, and offline regeneration are recorded in [docs/adblock-data.md](docs/adblock-data.md) | Unlicense (repository); `tracking.txt` header declares MIT |


OpenOSINT was assessed as an interoperability recipe only. Its current
interactive agent/MCP implementation does not expose a stable versioned
structured output contract, so Sieve does not wrap or depend on it.

The former Crawl4AI, Scrapling, and ScrapeGraphAI modules were reconstructed
under the no-ports policy recorded in issue #451, using Sieve-local contracts
and tests as implementation references. Those tests and the policy do not
independently establish source authorship; this file does not claim an
independent authorship review. The `adblock.py` domain list is third-party
data from The Block List Project and has its own source and terms record above.
The remaining rows describe subprocess or API integrations based on maintainer
provenance records. Operators must review Brave's current API terms,
attribution requirements, and usage rights for returned images before
redistribution. Distribution notices live in
[THIRD-PARTY-NOTICES.md](THIRD-PARTY-NOTICES.md).

## Dependency notices

Python package dependencies keep their own licenses and are not relicensed by
Sieve. See [THIRD-PARTY-NOTICES.md](THIRD-PARTY-NOTICES.md) for the distributed
notice set and `pyproject.toml` for the current dependency list.

When contributing an adaptation, add the upstream repository, observed version
or commit, the affected Sieve files, and the license decision in the change that
introduces it.
