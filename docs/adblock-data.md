# Ad and tracker data

Sieve's blocklist is data from The Block List Project's `ads.txt` and
`tracking.txt` at commit
[`ee3bdbaddf49c756cf3340bb2e6a9dee5282d1c6`](https://github.com/blocklistproject/Lists/tree/ee3bdbaddf49c756cf3340bb2e6a9dee5282d1c6).
The pinned repository `LICENSE` file identifies the Unlicense/public-domain
dedication. The upstream `tracking.txt` header separately labels that list
MIT and says the list is maintained by The Block List Project. It does not
name a copyright holder or year. Both statements and the original header are
retained here without resolving their scope or inventing an attribution.
The exact license text and both input snapshots are checked in under
`scripts/data/blocklistproject/`; [adblock-data.json](adblock-data.json)
records their source URLs, retrieval date, byte counts and SHA-256 hashes, plus
the generated file's hash and entry count.

The generator lowercases and deduplicates valid hosts-file domains, then applies
the reviewed suffix exclusions in `scripts/generate_adblock_data.py`. This
produces `sieve/data/adblock_domains.txt`. Reproduce it without network access:

```bash
python scripts/generate_adblock_data.py
python scripts/generate_adblock_data.py --check
```

The first command regenerates the data and its hash manifest from the checked-in
snapshots. The second verifies their hashes and compares the generated output
byte-for-byte with the committed list. Neither command accesses the network.
To stage a different upstream snapshot, fetch its full commit explicitly with
`python scripts/generate_adblock_data.py --fetch <40-character-commit-sha>`;
review the resulting source, terms, and generated-data diffs before accepting
that pin. Runtime blocking only reads the generated file; it never updates the
list or contacts the source project.
