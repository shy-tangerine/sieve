# Third-party notices

Sieve includes or adapts small portions of upstream open-source projects. The
project license applies to Sieve-original code; the upstream terms below apply
to the corresponding portions and remain authoritative in each cloned source
repository.

| Component | License | Upstream license |
|---|---|---|
| Former master-fetch/Hound engine patterns | MIT | [master-fetch](https://github.com/dondai44423/master-fetch/blob/master/LICENSE) |
| DDGS-derived search code | MIT | [DDGS](https://github.com/deedy5/ddgs/blob/main/LICENSE.md) |
| Patchright browser dependency | Apache-2.0 | [Patchright](https://github.com/Kaliiiiiiiiii-Vinyzu/patchright-python/blob/main/LICENSE) |
| Playwright API dependency | Apache-2.0 | [Playwright](https://github.com/microsoft/playwright/blob/main/LICENSE) |
| Trafilatura dependency | Apache-2.0 | [Trafilatura](https://github.com/adbar/trafilatura/blob/master/LICENSE) |
| Markdownify dependency | MIT | [Markdownify](https://github.com/matthewwithanm/python-markdownify/blob/master/LICENSE) |
| lxml dependency | BSD-3-Clause | [lxml](https://github.com/lxml/lxml/blob/master/LICENSE.txt) |
| Obscura-derived fingerprint/SSRF concepts (no code copied) | MIT | [Obscura](https://github.com/h4ckf0r0day/obscura) |
| yt-dlp (external binary, invoked as subprocess, not vendored) | Unlicense | [yt-dlp](https://github.com/yt-dlp/yt-dlp) |
| Arctic Shift API (HTTP use only, no code copied) | n/a — API terms govern data | [Arctic Shift](https://github.com/ArthurHeitmann/arctic_shift) |
| `sieve/data/adblock_domains.txt` ad/tracker domain data (`ads.txt` and `tracking.txt`, pinned commit) | Unlicense (repository license); `tracking.txt` header declares MIT | [The Block List Project at pinned commit](https://github.com/blocklistproject/Lists/tree/ee3bdbaddf49c756cf3340bb2e6a9dee5282d1c6); MIT terms: [opensource.org/license/mit](https://opensource.org/license/mit/); exact source snapshots and retrieval record: [docs/adblock-data.md](docs/adblock-data.md) |

The former Crawl4AI-, Scrapling-, and ScrapeGraphAI-derived modules were
reconstructed under the no-ports policy recorded in issue #451, using Sieve
contracts and tests as implementation references. Those tests and the policy
do not independently establish source authorship; this notice does not claim
an independent authorship review. The ad/tracker data comes from The Block
List Project. Its pinned repository license is Unlicense, while the
`tracking.txt` header declares MIT and names The Block List Project as list
maintainer; that header gives no copyright holder or year. The exact input
header is retained, and the standard MIT terms are linked above without
inventing a copyright line or resolving the scope of the two declarations.
The pinned commit, exact source snapshots, license file, hashes, and retrieval
record are in
[docs/adblock-data.md](docs/adblock-data.md).

The standard MIT, BSD-3-Clause, and Apache-2.0 license texts applicable to
distributed dependencies are bundled in `licenses/`. DDGS-specific terms
remain in `NOTICE.ddgs.txt`.

### MIT notice declared by Block List Project `tracking.txt`

The input file identifies The Block List Project as maintainer and declares
MIT, but supplies no copyright year or holder. Its exact header is retained in
the pinned source snapshot. No copyright line is inferred. The permission and
disclaimer terms for the MIT declaration are:

> Permission is hereby granted, free of charge, to any person obtaining a copy
> of this software and associated documentation files (the "Software"), to deal
> in the Software without restriction, including without limitation the rights
> to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
> copies of the Software, and to permit persons to whom the Software is
> furnished to do so, subject to the following conditions:
>
> The above copyright notice and this permission notice shall be included in all
> copies or substantial portions of the Software.
>
> THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
> IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
> FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
> AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
> LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
> OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
> SOFTWARE.

The upstream repository also provides an Unlicense at the pinned commit.
These separate statements are preserved here without a conclusion about their
relative scope.

Before each release, compare this table with the exact upstream snapshot and
retain any required copyright, attribution, or bundled-license files. Arctic
Shift is consumed as an HTTP API and is not copied into Sieve as a library.
