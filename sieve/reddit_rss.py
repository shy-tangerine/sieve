"""Deterministic Reddit RSS/Atom source adapter.

This module is deliberately separate from :mod:`sieve.reddit`: the
existing module handles HTML optimization and parsing for the main fetch path.
The adapter here is a small, source-neutral boundary for Reddit feeds.
"""

from __future__ import annotations

import asyncio
import inspect
import re
from dataclasses import asdict, dataclass
from html import unescape
from html.parser import HTMLParser
from typing import Any, Awaitable, Callable, Mapping, Protocol
from urllib.parse import parse_qsl, quote, urlencode, urljoin, urlparse, urlunparse
from defusedxml.ElementTree import fromstring as _defused_fromstring
from defusedxml.common import EntitiesForbidden

from .security import SecurityError, validate_url
from xml.etree import ElementTree as ET

# defusedxml guards the untrusted-feed parse (bandit B314): Reddit RSS is
# attacker-influenced content, so billion-laughs / entity-expansion and
# external-entity resolution must be rejected rather than processed.
def _parse_xml(xml: str | bytes):
    return _defused_fromstring(xml)


_REDDIT_HOSTS = {"reddit.com", "www.reddit.com", "old.reddit.com", "new.reddit.com"}
_NS = {"atom": "http://www.w3.org/2005/Atom", "media": "http://search.yahoo.com/mrss/"}
_ID_RE = re.compile(r"(?:^|/)(t[13]_[A-Za-z0-9]+)(?:$|/)")
_SUBREDDIT_RE = re.compile(r"/r/([^/]+)", re.IGNORECASE)
_RAW_ENTRY_RE = re.compile(
    r"<(?:[A-Za-z_][\w.-]*:)?(?:entry|item)\b[^>]*>.*?</(?:[A-Za-z_][\w.-]*:)?(?:entry|item)\s*>",
    re.IGNORECASE | re.DOTALL,
)
_MAX_FEED_RESPONSE_BYTES = 10 * 1024 * 1024
_MAX_REDIRECTS = 5


class RSSFetchError(RuntimeError):
    """Raised when a Reddit feed cannot be fetched after retries."""


class _AsyncSession(Protocol):
    def get(self, url: str, **kwargs: Any) -> Any: ...


class _TextCollector(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        self.parts.append(data)


def _plain_text(value: str | None) -> str:
    if not value:
        return ""
    parser = _TextCollector()
    parser.feed(unescape(value))
    parser.close()
    return " ".join(" ".join(parser.parts).split())


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _child(element: ET.Element, name: str) -> ET.Element | None:
    for child in element:
        if _local_name(child.tag) == name:
            return child
    return None


def _child_text(element: ET.Element, name: str) -> str:
    child = _child(element, name)
    return "" if child is None else "".join(child.itertext()).strip()


def _entry_link(entry: ET.Element) -> str:
    links = [child for child in entry if _local_name(child.tag) == "link"]
    for link in links:
        if link.attrib.get("rel", "alternate") == "alternate" and link.attrib.get("href"):
            return link.attrib["href"]
    for link in links:
        if link.attrib.get("href"):
            return link.attrib["href"]
        text = "".join(link.itertext()).strip()
        if text:
            return text
    return ""


def _entry_content(entry: ET.Element) -> str:
    for name in ("content", "summary", "description"):
        child = _child(entry, name)
        if child is not None:
            return "".join(child.itertext()).strip()
    return ""


def _raw_entry_payloads(xml: str | bytes) -> list[str]:
    """Return entry/item slices from the input, preserving their XML verbatim."""
    source = xml.decode("utf-8") if isinstance(xml, bytes) else xml
    return [match.group(0) for match in _RAW_ENTRY_RE.finditer(source)]


def _source_kind(entry_id: str, feed_url: str) -> str:
    if entry_id.startswith("t1_"):
        return "comment"
    if entry_id.startswith("t3_"):
        return "submission"
    return "comment" if "/comments/" in urlparse(feed_url).path and entry_id else "entry"


def _feed_type(feed_url: str) -> str:
    path = urlparse(feed_url).path.rstrip("/")
    if re.search(r"/comments/[^/]+(?:\.rss)?$", path, re.IGNORECASE):
        return "thread"
    if path.endswith(".rss") and "/search" in path:
        return "search"
    if "/search.rss" in path:
        return "search"
    return "subreddit"


@dataclass(frozen=True, slots=True)
class RedditRSSRecord:
    """A normalized Reddit feed entry with stable provenance."""

    title: str
    url: str
    source: str
    source_id: str
    kind: str
    published_at: str
    author: str
    summary: str
    content: str
    subreddit: str
    provenance: Mapping[str, str]
    raw_payload: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def feed_url(target: str, *, kind: str | None = None, query: str | None = None) -> str:
    """Build a canonical Reddit RSS URL for a subreddit, search, or thread."""
    target = target.strip()
    parsed = urlparse(target if "://" in target else f"https://www.reddit.com/{target.lstrip('/')}")
    if parsed.hostname and parsed.hostname.lower() in _REDDIT_HOSTS:
        path = parsed.path.rstrip("/")
        if path.endswith(".rss"):
            return urlunparse(parsed._replace(scheme="https", query=parsed.query))
        if not path.startswith(("/r/", "/user/", "/u/", "/comments/")):
            path = f"/r/{path.lstrip('/')}"
    else:
        path = "/r/" + quote(target.strip("/"), safe="")
    if kind == "thread" or "/comments/" in path:
        thread_match = re.search(r"(/comments/[^/]+)", path, re.IGNORECASE)
        path = thread_match.group(1) if thread_match else path
        path = path if path.endswith(".rss") else f"{path}.rss"
    elif kind == "search" or query is not None or "/search" in path:
        path = path.replace("/search", "/search.rss") if "/search" in path else f"{path}/search.rss"
        params = dict(parse_qsl(parsed.query, keep_blank_values=True))
        if query is not None:
            params["q"] = query
        return urlunparse(("https", "www.reddit.com", path, "", urlencode(params), ""))
    else:
        path = f"{path}.rss"
    return urlunparse(("https", "www.reddit.com", path, "", parsed.query, ""))


def parse_reddit_rss(xml: str | bytes, *, feed_url: str, limit: int = 25) -> list[RedditRSSRecord]:
    """Parse Reddit RSS/Atom XML into deterministic, source-neutral records."""
    if isinstance(limit, bool) or not isinstance(limit, int) or not 0 <= limit <= 500:
        raise ValueError("limit must be an integer between 0 and 500")
    if limit == 0:
        return []
    try:
        root = _parse_xml(xml)
    except ET.ParseError as exc:
        raise RSSFetchError(f"invalid Reddit RSS XML: {exc}") from exc
    except EntitiesForbidden as exc:
        raise RSSFetchError("invalid Reddit RSS XML: entity expansion is not allowed") from exc

    feed_type = _feed_type(feed_url)
    subreddit_match = _SUBREDDIT_RE.search(urlparse(feed_url).path)
    feed_subreddit = subreddit_match.group(1) if subreddit_match else ""
    records: list[RedditRSSRecord] = []
    entries = [element for element in root.iter() if _local_name(element.tag) in ("entry", "item")]
    raw_entries = _raw_entry_payloads(xml)
    for index, entry in enumerate(entries[:limit]):
        entry_id = _child_text(entry, "id") or _child_text(entry, "guid")
        source_id = (_ID_RE.search(entry_id) or _ID_RE.search(_entry_link(entry)))
        stable_id = source_id.group(1) if source_id else entry_id
        link = _entry_link(entry)
        try:
            parsed_link = urlparse(link)
            if parsed_link.scheme not in {"http", "https"} or not parsed_link.hostname or parsed_link.username or parsed_link.password:
                link = ""
        except ValueError:
            link = ""
        subreddit = feed_subreddit
        for category in entry:
            if _local_name(category.tag) == "category":
                term = category.attrib.get("term", "")
                match = re.search(r"(?:/r/|^)([^/]+)$", term)
                if match and term.lower().startswith(("/r/", "reddit.com/r/")):
                    subreddit = match.group(1)
                    break
        title = _plain_text(_child_text(entry, "title"))
        raw_content = _entry_content(entry)
        text = _plain_text(raw_content)
        author_element = _child(entry, "author")
        author = _child_text(author_element, "name") if author_element is not None else ""
        published_at = (_child_text(entry, "published") or _child_text(entry, "updated")
                        or _child_text(entry, "pubDate"))
        records.append(RedditRSSRecord(
            title=title,
            url=link,
            source="reddit",
            source_id=stable_id,
            kind=_source_kind(stable_id, feed_url),
            published_at=published_at,
            author=author.removeprefix("/u/").removeprefix("u/"),
            summary=text,
            content=text,
            subreddit=subreddit,
            provenance={
                "source": "reddit",
                "feed_url": feed_url,
                "feed_type": feed_type,
                "entry_id": entry_id,
            },
            raw_payload=(raw_entries[index] if index < len(raw_entries)
                         else ET.tostring(entry, encoding="unicode")),
        ))
    return records


async def _maybe_await(value: Any) -> Any:
    return await value if inspect.isawaitable(value) else value


def _validated_reddit_redirect(current_url: str, location: str | None) -> str:
    """Resolve and validate one Reddit redirect before making the next request."""
    if not location:
        raise RSSFetchError("Reddit RSS redirect is missing a Location header")
    target = urljoin(current_url, location)
    parsed = urlparse(target)
    if parsed.hostname is None or parsed.hostname.lower() not in _REDDIT_HOSTS:
        raise RSSFetchError("Reddit RSS redirect targets an unsupported host")
    try:
        return validate_url(target)
    except SecurityError as exc:
        raise RSSFetchError("Reddit RSS redirect failed destination validation") from exc


async def fetch_reddit_rss(
    url: str,
    *,
    session: _AsyncSession | None = None,
    limit: int = 25,
    timeout: float = 10.0,
    retries: int = 2,
    retry_delay: float = 0.25,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> list[RedditRSSRecord]:
    """Fetch and parse a Reddit feed with bounded timeout and retries.

    ``retries`` is the number of retries after the initial request. Only
    transient failures (exceptions, 429, and 5xx) are retried.
    """
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not 0 < float(timeout) <= 120:
        raise ValueError("timeout must be a number between 0 and 120")
    if isinstance(retries, bool) or not isinstance(retries, int) or not 0 <= retries <= 10:
        raise ValueError("retries must be an integer between 0 and 10")
    if isinstance(retry_delay, bool) or not isinstance(retry_delay, (int, float)) or not 0 <= float(retry_delay) <= 30:
        raise ValueError("retry_delay must be a number between 0 and 30")
    parsed = urlparse(url)
    if parsed.hostname is None or parsed.hostname.lower() not in _REDDIT_HOSTS:
        raise ValueError("url must use a supported reddit.com host")
    if not parsed.path.endswith(".rss"):
        raise ValueError("url must point to a Reddit RSS feed (.rss)")

    own_session = session is None
    if own_session:
        import httpx
        session = httpx.AsyncClient(timeout=timeout, follow_redirects=False)
    if session is None:
        raise RSSFetchError("Reddit RSS session initialization failed")
    try:
        last_error: Exception | None = None
        for attempt in range(retries + 1):
            try:
                if own_session:
                    request_url = url
                    redirects = 0
                    while True:
                        validate_url(request_url)
                        async with session.stream("GET", request_url, timeout=timeout, headers={"Accept": "application/atom+xml,application/rss+xml"}) as response:
                            status = int(getattr(response, "status_code", 0))
                            if status in {301, 302, 303, 307, 308}:
                                redirects += 1
                                if redirects > _MAX_REDIRECTS:
                                    raise RSSFetchError("Reddit RSS exceeded 5 redirects")
                                request_url = _validated_reddit_redirect(
                                    request_url, response.headers.get("location")
                                )
                                continue
                            chunks = bytearray()
                            async for chunk in response.aiter_bytes():
                                chunks.extend(chunk)
                                if len(chunks) > _MAX_FEED_RESPONSE_BYTES:
                                    raise RSSFetchError("Reddit RSS response exceeds 10 MiB")
                            body = bytes(chunks)
                        break
                else:
                    response = await _maybe_await(session.get(url, timeout=timeout, headers={"Accept": "application/atom+xml,application/rss+xml"}))
                    body = getattr(response, "content", None)
                    if body is None:
                        body = getattr(response, "text", "")
                    status = int(getattr(response, "status_code", 0))
                if status == 429 or status >= 500:
                    raise RSSFetchError(f"Reddit RSS returned HTTP {status}")
                if status >= 400:
                    raise RSSFetchError(f"Reddit RSS returned HTTP {status}")
                if isinstance(body, str):
                    if len(body.encode("utf-8")) > _MAX_FEED_RESPONSE_BYTES:
                        raise RSSFetchError("Reddit RSS response exceeds 10 MiB")
                elif len(body) > _MAX_FEED_RESPONSE_BYTES:
                    raise RSSFetchError("Reddit RSS response exceeds 10 MiB")
                return parse_reddit_rss(body, feed_url=url, limit=limit)
            except (RSSFetchError, OSError, TimeoutError) as exc:
                last_error = exc
                if isinstance(exc, RSSFetchError) and "HTTP 429" not in str(exc) and not re.search(r"HTTP 5\d\d", str(exc)):
                    break
                if attempt >= retries:
                    break
                await sleep(retry_delay * (2**attempt))
        raise RSSFetchError(f"failed to fetch Reddit RSS after {retries + 1} attempts") from last_error
    finally:
        if own_session:
            await session.aclose()


__all__ = ["RSSFetchError", "RedditRSSRecord", "feed_url", "fetch_reddit_rss", "parse_reddit_rss"]

