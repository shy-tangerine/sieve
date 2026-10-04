"""Ad/tracker blocking: domain list + route intercept helper.

The domain data in sieve/data/adblock_domains.txt comes from The Block List
Project (https://github.com/blocklistproject/Lists, Unlicense / public domain),
ads.txt and tracking.txt at a pinned commit. Regenerate it with
scripts/generate_adblock_data.py. The list is loaded on first use, not at import.

Wiring (in the browser fetch path):

    from sieve.adblock import should_block_url

    async def _route_handler(route):
        if should_block_url(route.request.url):
            return await route.abort()
        return await route.continue_()
    await page.route("**/*", _route_handler)

Or for the HTTP tier: skip requests where should_block_host(host) is True.
Do not apply it to top-level page navigations: the list contains real sites.
"""

from __future__ import annotations

from functools import lru_cache
from importlib import resources
from urllib.parse import urlparse

_DATA_FILE = "adblock_domains.txt"


@lru_cache(maxsize=1)
def _load_domains() -> frozenset[str]:
    text = resources.files("sieve").joinpath("data", _DATA_FILE).read_text(encoding="utf-8")
    return frozenset(
        line for line in (raw.strip() for raw in text.splitlines()) if line and not line.startswith("#")
    )


def __getattr__(name: str) -> frozenset[str]:
    if name == "AD_DOMAINS":
        return _load_domains()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def _host(url_or_host: str) -> str:
    if "://" in url_or_host:
        try:
            return (urlparse(url_or_host).hostname or "").lower()
        except Exception:
            return url_or_host.lower()
    return url_or_host.lower().split("/")[0].split(":")[0]


def should_block_host(host: str) -> bool:
    h = _host(host)
    if not h:
        return False
    domains = _load_domains()
    parts = h.split(".")
    return any(".".join(parts[i:]) in domains for i in range(len(parts)))


def should_block_url(url: str) -> bool:
    try:
        host = urlparse(url).hostname or ""
    except Exception:
        return False
    return should_block_host(host)


def is_ad_url(url: str) -> bool:
    return should_block_url(url)
