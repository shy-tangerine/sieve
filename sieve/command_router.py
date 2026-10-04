"""Canonical capability routing for the MCP surface.

The router is the application seam between a transport and capability methods.
It owns canonical-name validation, argument validation, and capability result
formatting.  Transports only need to provide a server object and pass the
request through this interface.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from sieve.public_output import safe_error, safe_public_json


def _public_dumps(obj) -> str:
    """Final public-output boundary (#251/#252): redact secrets, bound size."""
    return safe_public_json(obj)


def _public_result(obj):
    """Build both result copies from one bounded model traversal."""
    import json
    from mcp.types import TextContent
    text = _public_dumps(obj)
    return [TextContent(type="text", text=text)], json.loads(text)

def _collect(args: dict, options: dict, keys: tuple[str, ...]) -> dict:
    out: dict = {}
    for k in keys:
        v = args.get(k)
        if v is None:
            v = options.get(k)
        if v is not None:
            out[k] = v
    return out


def _validate_public_inputs(tool: str, args: dict, options: dict) -> None:
    """Fail closed using the same schema advertised by the registry."""
    definition = CAPABILITY_REGISTRY[tool].definition
    if definition is None:
        raise ValueError("Capability has no public input schema")
    schema = definition["inputSchema"]
    properties = schema["properties"]
    unknown = sorted(set(args) - properties.keys())
    if unknown:
        raise ValueError(f"Unknown arguments for {tool}: {', '.join(unknown)}")
    unknown_options = sorted(set(options) - properties.get("options", {}).get("properties", {}).keys())
    if unknown_options:
        raise ValueError(f"Unknown options for {tool}: {', '.join(unknown_options)}")


def _proxy_health_pool(server):
    from sieve.proxy_health import ProxyHealth
    if not hasattr(server, "_ph_pool"):
        server._ph_pool = ProxyHealth()
    return server._ph_pool

def _session_store(server):
    from sieve.session_coherence import SessionStore
    if not hasattr(server, "_session_store_inst"):
        server._session_store_inst = SessionStore()
    return server._session_store_inst

def _impersonation_targets() -> list[str]:
    try:
        import primp
        targets = [t for t in dir(getattr(primp, "Impersonation", ())) if t.startswith("chrome")]
        if targets:
            return sorted(targets)
    except Exception:
        pass
    return ["chrome_148", "chrome_147", "chrome_120"]

def _primp_replay_fetch(url: str, *, cookies: str = "", impersonation: str = "chrome_148") -> dict:
    try:
        import primp
    except ImportError:
        return {"ok": False, "status": None, "text": "", "error": "primp not installed"}
    try:
        from sieve.security import validate_url
        # Redirect policy (issue #212): no automatic redirects; every Location
        # hop is validated through the canonical security layer (max 3 hops).
        client = primp.Client(impersonate=impersonation, headers={"Cookie": cookies} if cookies else None, follow_redirects=False)
        current = validate_url(url)
        resp = client.get(current)
        for _hop in range(3):
            if resp.status_code not in (301, 302, 303, 307, 308):
                break
            location = resp.headers.get("location") if hasattr(resp, "headers") else None
            if not location:
                break
            from urllib.parse import urljoin
            current = validate_url(urljoin(current, location))
            resp = client.get(current)
        return {"ok": resp.status_code < 400, "status": resp.status_code, "text": resp.text[:20000]}
    except Exception as e:
        return {"ok": False, "status": None, "text": "", **safe_error(e, fallback_category="network")}

class CapabilityRouter:
    """Dispatch one canonical capability and return its MCP result envelope.

    ``server`` is an application adapter: the router never imports or creates
    a transport.  Callers should use :meth:`dispatch`; the canonical names in
    :data:`CANONICAL_CAPABILITIES` are the complete accepted command set.
    """

    def __init__(self, server):
        self._server = server

    async def dispatch(self, name: str, args: dict):
        return await _dispatch_capability(self._server, name, args)


async def _dispatch_capability(server, name: str, args: dict):
    if name.startswith("mcp_"):
        raise ValueError("Unknown MCP tool: use the canonical bare tool name")
    capability = CAPABILITY_REGISTRY.get(name)
    if capability is None:
        raise ValueError(f"Unknown tool: {name}")
    if not isinstance(args, dict):
        raise ValueError("Tool arguments must be a JSON object")
    options = args.get("options")
    if options is None:
        options = {}
    elif not isinstance(options, dict):
        raise ValueError("'options' must be a JSON object")
    return await capability.handler(server, args, options)


async def _handle_smart_fetch(server, args: dict, options: dict):
    _validate_public_inputs("smart_fetch", args, options)
    url = args.get("url", "")
    urls = args.get("urls")
    if not url and not urls:
        raise ValueError("Either 'url' or 'urls' must be provided")
    kw = _collect(args, options, ("css_selector","max_content_chars","timeout","pages","password","focus","extraction_type","cache_ttl","offset","include_media","include_links","force_fetcher","capture_xhr","capture_pattern","fold_captured"))
    result = await server.smart_fetch(url=url, urls=urls, **kw)
    return _public_result(result)


async def _handle_smart_crawl(server, args: dict, options: dict):
    _validate_public_inputs("smart_crawl", args, options)
    url = args.get("url")
    if not url:
        raise ValueError("'url' must be provided")
    kw = _collect(args, options, ("discover_only","focus","crawl_urls","max_pages","max_depth","path_include","path_exclude","max_content_chars_per","max_total_chars","concurrency","cache_ttl","respect_robots","force_fetcher","timeout","deadline_ms","sitemap","auto_throttle","throttle_min_delay"))
    result = await server.smart_crawl(url=url, **kw)
    return _public_result(result)


async def _handle_screenshot(server, args: dict, options: dict):
    url = args.get("url")
    if not url:
        raise ValueError("'url' must be provided")
    kw = _collect(args, options, ("session_id","full_page","image_type","quality","wait","wait_selector","network_idle","timeout"))
    result = await server.screenshot(url=url, **kw)
    return result


async def _handle_smart_search(server, args: dict, options: dict):
    query = args.get("query")
    if not query:
        raise ValueError("'query' must be provided")
    kw = _collect(args, options, ("max_results","cache_ttl","mode","engines","url","site","exclude_sites","location","language","region","page","freshness","stale_fallback","stale_max_age"))
    result = await server.smart_search(query=query, **kw)
    return _public_result(result)


async def _handle_cache_clear(server, args: dict, options: dict):
    result = await server.cache_clear(all=bool(args.get("all", False)))
    return _public_result(result)


async def _handle_extract(server, args: dict, options: dict):
    from mcp.types import TextContent
    _validate_public_inputs("extract", args, options)
    url = args.get("url")
    schema = args.get("schema")
    if not url or not schema:
        raise ValueError("Both 'url' and 'schema' must be provided")
    kw = _collect(args, options, ("xpath","extraction_type","timeout","force_fetcher","cache_ttl"))
    result = await server.extract(url=url, schema=schema, **kw)
    return [TextContent(type="text", text=_public_dumps(result))], result


async def _handle_detect_block(server, args: dict, options: dict):
    from mcp.types import TextContent
    from sieve.antibot_detector import detect_block
    status = int(args.get("status", 0))
    headers = args.get("headers") or {}
    html = args.get("html") or ""
    result = detect_block(status, headers, html)
    return [TextContent(type="text", text=safe_public_json(result))], result


async def _handle_sleeper_fetch(server, args: dict, options: dict):
    from mcp.types import TextContent
    from sieve.sleeper_bridge import sleeper_fetch, is_available
    if not is_available():
        raise ValueError("Sleeper daemon not reachable on :8790 — start sleeper.service + browser with the extension")
    url = args.get("url", "")
    if not url:
        raise ValueError("'url' must be provided")
    extract = args.get("extract")
    result = sleeper_fetch(url, extract=extract if isinstance(extract, dict) else None, wait_selector=args.get("wait_selector"), tab=args.get("tab"), timeout=45)
    return [TextContent(type="text", text=safe_public_json(result))], result


async def _handle_sleeper_api(server, args: dict, options: dict):
    from mcp.types import TextContent
    from sieve.sleeper_bridge import sleeper_api, is_available
    if not is_available():
        raise ValueError("Sleeper daemon not reachable on :8790 — start sleeper.service + browser with the extension")
    path = args.get("path", "")
    if not path:
        raise ValueError("'path' must be provided")
    result = sleeper_api(path, method=args.get("method","GET"), body=args.get("body"), tab=args.get("tab"), timeout=45)
    return [TextContent(type="text", text=safe_public_json(result))], result


async def _handle_extract_jsonl(server, args: dict, options: dict):
    from mcp.types import TextContent
    url = args.get("url")
    schema = args.get("schema")
    if not url or not schema:
        raise ValueError("'url' and 'schema' must be provided")
    if args.get("validate"):
        from sieve.extraction import validate_schema
        errs = validate_schema(schema)
        if errs:
            raise ValueError("schema invalid: " + "; ".join(errs))
    kw = _collect(args, options, ("xpath","extraction_type","timeout","force_fetcher","cache_ttl"))
    result = await server.extract(url=url, schema=schema, **kw)
    if result.get("error") or not result.get("content_ok"):
        return [TextContent(type="text", text=safe_public_json(result))], result
    items = result.get("items") or []
    lines = []
    for item in items:
        rec = {f["name"]: item.get(f["name"]) for f in schema.get("fields", [])}
        lines.append(safe_public_json(rec, ensure_ascii=False))
    jsonl = "\n".join(lines)
    return [TextContent(type="text", text=jsonl)]


async def _handle_schema_gen(server, args: dict, options: dict):
    from mcp.types import TextContent
    from sieve.schema_gen import generate_schema
    from sieve.byok_config import get_llm_key
    url = args.get("url")
    fields = args.get("fields")
    if not url or not fields:
        raise ValueError("'url' and 'fields' must be provided")
    res = await server.smart_fetch(url=url, extraction_type="html", cache_ttl=0)
    payload = res.model_dump()
    if not payload.get("content_ok") or not payload.get("content"):
        raise ValueError(f"fetch failed for schema gen: {payload.get('summary', 'no content')}")
    html = "\n".join(payload["content"])
    api_key = get_llm_key()
    if not api_key:
        raise ValueError("an LLM key is required for schema generation: "
                         "`sieve keys add llm` or set SIEVE_LLM_KEYS")
    schema = generate_schema(html, fields, model=args.get("model") or "auto", api_key=api_key)
    return [TextContent(type="text", text=_public_dumps(schema))], schema


async def _handle_proxy_health(server, args: dict, options: dict):
    from mcp.types import TextContent
    pool = _proxy_health_pool(server)
    action = args.get("action","status")
    if action == "status":
        return [TextContent(type="text", text=safe_public_json({"scores": pool.scores(),"quarantined": pool.quarantined(),"healthy": pool.healthy()}))], {}
    if action == "register":
        pool.register(args["proxy"])
        return [TextContent(type="text", text=safe_public_json({"ok": True}))], {}
    if action == "report":
        pool.report(args.get("proxy",""), args.get("status_code"), ok=bool(args.get("ok", True)))
        return [TextContent(type="text", text=safe_public_json({"ok": True}))], {}
    if action == "pick":
        return [TextContent(type="text", text=safe_public_json({"proxy": pool.pick()}))], {}
    raise ValueError(f"unknown action: {action}")


async def _handle_session_plan(server, args: dict, options: dict):
    from mcp.types import TextContent
    from sieve.session_coherence import warmup_sequence, human_delay
    action = args.get("action","warmup")
    if action == "warmup":
        url = args.get("url")
        if not url:
            raise ValueError("'url' must be provided for warmup")
        seq = warmup_sequence(url)
        return [TextContent(type="text", text=_public_dumps(seq))], {"plan": seq}
    if action == "human_delay":
        return [TextContent(type="text", text=str(human_delay()))], {}
    store = _session_store(server)
    if action == "profile_list":
        return [TextContent(type="text", text=safe_public_json(store.list()))], {}
    prof = store.get(args.get("profile") or "default")
    if action == "profile_get":
        # Session profiles may contain cookies and credential-bearing proxy
        # URLs. to_dict() defaults to metadata-only output (cookie count,
        # proxy presence); secrets require an explicit include_secrets=True.
        safe_profile = prof.to_dict()
        return [TextContent(type="text", text=_public_dumps(safe_profile))], {}
    if action == "profile_add_cookie":
        prof.add_cookie(args.get("name",""), args.get("value",""), args.get("domain",""))
        return [TextContent(type="text", text=safe_public_json({"ok": True}))], {}
    raise ValueError(f"unknown action: {action}")


async def _handle_datadome_harvest(server, args: dict, options: dict):
    from mcp.types import TextContent
    from sieve.datadome_bridge import harvest_plan, match_impersonation, replay_fetch
    action = args.get("action","plan")
    url = args.get("url","")
    if not url:
        raise ValueError("'url' must be provided")
    if action == "plan":
        plan = harvest_plan(url)
        return [TextContent(type="text", text=_public_dumps(plan))], {"plan": plan}
    if action == "match":
        ua = args.get("user_agent","")
        imp = match_impersonation(ua, _impersonation_targets())
        return [TextContent(type="text", text=safe_public_json({"impersonation": imp}))], {}
    if action == "replay":
        cks = args.get("cookies") or []
        imp = args.get("impersonation") or "chrome_148"
        result = replay_fetch(url, cookies=cks, impersonation=imp, fetch=lambda u, **kw: _primp_replay_fetch(u, **kw))
        return [TextContent(type="text", text=_public_dumps(result))], result
    raise ValueError(f"unknown action: {action}")


async def _handle_profile_identity(server, args: dict, options: dict):
    from mcp.types import TextContent
    from sieve.profile_identity import generate_identity, identities_match, fingerprint_summary, rotate
    action = args.get("action","generate")
    if action == "generate":
        ident = generate_identity(args.get("blueprint"))
        return [TextContent(type="text", text=_public_dumps(ident))], ident
    if action == "check":
        a = args.get("identity_a") or {}
        b = args.get("identity_b") or {}
        return [TextContent(type="text", text=safe_public_json({"match": identities_match(a,b)}))], {}
    if action == "summary":
        a = args.get("identity_a") or {}
        return [TextContent(type="text", text=fingerprint_summary(a))], {}
    if action == "rotate":
        a = args.get("identity_a") or {}
        ident = rotate(a)
        return [TextContent(type="text", text=_public_dumps(ident))], ident
    raise ValueError(f"unknown action: {action}")


async def _handle_sitemap_harvest(server, args: dict, options: dict):
    from mcp.types import TextContent
    from sieve.sitemap_harvest import harvest_urls, harvest_gig_urls
    action = args.get("action","harvest")
    max_urls = int(args.get("max_urls") or 2000)
    if action == "fiverr":
        urls = harvest_gig_urls(max_urls=max_urls)
        return [TextContent(type="text", text=safe_public_json({"count": len(urls),"urls": urls[:50]}))], {"count": len(urls),"urls": urls[:50]}
    url = args.get("url","")
    if not url:
        raise ValueError("'url' must be provided for harvest")
    urls = harvest_urls(url, max_urls=max_urls)
    return [TextContent(type="text", text=safe_public_json({"count": len(urls),"urls": urls[:50]}))], {"count": len(urls),"urls": urls[:50]}


async def _handle_research_ingest(server, args: dict, options: dict):
    from sieve.research_sources import ingest_source
    source = args.get("source")
    if not source:
        raise ValueError("'source' must be provided")
    result = await ingest_source(source, target=args.get("target"), kind=args.get("kind","posts"), subreddit=args.get("subreddit"), query=args.get("query"), ids=args.get("ids"), post_id=args.get("post_id"), limit=args.get("limit",25), after=args.get("after"), before=args.get("before"), sort=args.get("sort","desc"), max_pages=args.get("max_pages",1), timeout=args.get("timeout",15), retries=args.get("retries",2))
    return _public_result(result)


async def _handle_version(server, args: dict, options: dict):
    result = await server.version()
    return _public_result(result)


@dataclass(frozen=True)
class Capability:
    """One canonical handler, its public schema and required installation extras."""
    handler: Callable
    definition: dict | None = None
    extras: tuple[str, ...] = ()


CAPABILITY_REGISTRY = {
    'smart_fetch': Capability(_handle_smart_fetch, {'name': 'smart_fetch',
 'description': 'Fetch any URL or PDF. Auto anti-bot (HTTP -> stealthy). Use after smart_search to get page '
                'content - search gives URLs + snippets, smart_fetch gives the full page. \n'
                '\n'
                'POWER FEATURES (save calls + tokens): \n'
                "- focus='query': extracts only BM25-relevant paragraphs. smart_fetch(url, focus='embedding "
                "dimension') on a 75-page paper returns only paragraphs about embeddings - one call instead "
                'of ten. Post-cache (no re-fetch). Re-pass same focus when paginating. \n'
                "- pages='9' or pages='1-5,9-12': specific PDF pages. PDFs return table_of_contents "
                '[{level,title,page,end_page}] - use page ranges to grab one section. \n'
                "- urls=['url1','url2']: parallel bulk fetch. Use when you need full page content from "
                'multiple specific URLs - one call, not N sequential ones. \n'
                '\n'
                "DECISION GUIDE: Have a URL + a specific question? focus='your question'. Have a PDF + know "
                "which page? pages='9'. Have a PDF + don't know which page? focus='your question' (BM25 "
                'finds it). Content behind click/form/scroll? '
                "actions=[{click:'button'},{fill:{selector:'#q',text:'x'}}]. Need the page's source links? "
                'include_links=true -> response.links.citations. \n'
                '\n'
                'RESPONSE SIGNALS (check before trusting content): \n'
                "- content_ok: True = real content. False = JS shell, login wall, or error - don't trust the "
                'content. \n'
                '- next_action: follow it - tells you the optimal next call (paginate, switch source, follow '
                'links). Empty = done. \n'
                "- page_type: 'list' = page links to the real content (fetch those links or smart_crawl). "
                "'auth_wall'/'paywall' = content behind login/payment (switch sources). \n"
                '- network: present when the page loads its data over XHR (prices, forecasts, tables). '
                'content is then boilerplate and content_ok=False; read network.fragments (the is_primary '
                'one holds the data), or re-fetch with fold_captured=true to get it in content. \n'
                '- is_truncated + next_offset: more content available. Use offset=next_offset to continue, '
                'or re-fetch with focus= to get only relevant parts. \n'
                '- content_age_days + is_stale: for current-state questions, seek newer sources if stale. \n'
                '- quality_score: PDF extraction quality 0-1. Low = garbled/CID corruption. \n'
                '\n'
                'css_selector narrows WHERE to extract (DOM element). focus narrows WHAT to extract '
                '(relevance to query). Use both for maximum precision. DataDome/Akamai/Turnstile '
                "unbypassable -> switch sources, don't retry same URL. cache_ttl=0 forces fresh.",
 'inputSchema': {'type': 'object',
                 'properties': {'url': {'type': 'string', 'description': 'URL to fetch'},
                                'urls': {'type': 'array',
                                         'items': {'type': 'string'},
                                         'description': 'Multiple URLs (parallel; returns per-URL results)'},
                                'extraction_type': {'type': 'string',
                                                    'enum': ['markdown',
                                                             'html',
                                                             'text',
                                                             'article',
                                                             'structured'],
                                                    'description': 'Content format (default markdown). html '
                                                                   '= raw HTML.'},
                                'css_selector': {'type': 'string',
                                                 'description': 'CSS selector to narrow extracted content '
                                                                "(e.g. 'article', '.main'). Token saver."},
                                'max_content_chars': {'type': 'integer',
                                                      'description': 'Max chars of extracted content '
                                                                     '(default 40000, min 500). Lower = less '
                                                                     'context; rest paginated via '
                                                                     'offset/next_offset.'},
                                'timeout': {'type': 'integer',
                                            'description': 'Max request time in ms (default 30000).'},
                                'cache_ttl': {'type': 'integer',
                                              'description': 'Cache seconds (default 3600). 0 = force '
                                                             'fresh.'},
                                'force_fetcher': {'type': 'string',
                                                  'enum': ['http', 'stealthy', 'sleeper'],
                                                  'description': 'Pin to one tier, skip auto-escalation. '
                                                                 "'http' = fast HTTP-only, 'stealthy' = "
                                                                 "pooled headless browser, 'sleeper' = "
                                                                 'operator real-browser daemon. Default = '
                                                                 'auto.'},
                                'offset': {'type': 'integer',
                                           'description': 'Char offset into extracted text to resume a '
                                                          'truncated page. Use next_offset from previous '
                                                          'response.'},
                                'pages': {'type': 'string',
                                          'description': "PDF only: page spec like '1-5' or '1,3,5-7'. Use "
                                                         'table_of_contents page/end_page ranges to pick. '
                                                         'None = all pages.'},
                                'password': {'type': 'string',
                                             'description': 'PDF only: password for an encrypted PDF.'},
                                'focus': {'type': 'string',
                                          'description': 'Query-focused extraction: only BM25-relevant '
                                                         'blocks returned. Context saver on long pages. '
                                                         'Post-cache (no re-fetch). Re-pass same focus when '
                                                         'paginating.'},
                                'options': {'type': 'object',
                                            'description': 'include_links (bool,false: '
                                                           'response.links=citations/navigation/external+primary_source), '
                                                           'include_media (bool,false: up to 20 page image '
                                                           'URLs), capture_xhr (bool,false: capture the '
                                                           "page's XHR responses into response.network - for "
                                                           'pages whose data arrives over XHR; auto-enabled '
                                                           'when an AJAX shell is detected), capture_pattern '
                                                           '(str regex selecting which request URLs to '
                                                           'capture), fold_captured (bool,false: merge the '
                                                           'primary captured fragment into content instead '
                                                           'of leaving it in network), wait (ms,0), '
                                                           'network_idle (bool,SPAs), headless (bool,true), '
                                                           'respect_robots (bool,true: robots.txt checked '
                                                           'before fetching; explicit bypass only), '
                                                           'real_chrome/solve_cloudflare/block_webrtc/hide_canvas/main_content_only/use_trafilatura '
                                                           '(anti-detect tuning, good defaults, rarely '
                                                           'needed).',
                                            'additionalProperties': False,
                                            'properties': {'include_links': {'type': 'boolean'},
                                                           'include_media': {'type': 'boolean'},
                                                           'capture_xhr': {'type': 'boolean'},
                                                           'capture_pattern': {'type': 'string'},
                                                           'fold_captured': {'type': 'boolean'}}}},
                 'additionalProperties': False},
 'annotations': {'readOnlyHint': True, 'idempotentHint': True, 'openWorldHint': True},
 'title': 'Smart Fetch',
 'outputSchema': {'type': 'object', 'additionalProperties': True}}),
    'smart_crawl': Capability(_handle_smart_crawl, {'name': 'smart_crawl',
 'description': 'Deep-crawl a site: best-first same-domain walk, each page as markdown + content_ok + '
                'page_type. List pages -> structured link list. \n'
                '\n'
                'WHEN TO USE: Multi-page docs, API references, or when you need many pages from one domain. '
                'For single pages, use smart_fetch. For multi-source research across different sites, search '
                'broadly then smart_fetch the best results. \n'
                '\n'
                'TWO-PHASE CRAWL (most efficient): sitemap=true (in options) maps all URLs from sitemap.xml '
                'in one fetch -> see the full URL list -> crawl_urls=[urls you need] to fetch only those '
                "pages. Avoids crawling irrelevant pages. sitemap='auto' = use sitemap if present else BFS. "
                'discover_only=true = URL map only (same as sitemap=true but no sitemap fetch). \n'
                '\n'
                "focus='query' makes the crawl prioritize relevant pages AND focus-filters each page's "
                'content - use for large doc sites to save tokens. Caps: max_pages (10), max_depth (2), '
                'max_total_chars (token budget), deadline_ms. Reuses smart_fetch anti-bot + cache.',
 'inputSchema': {'type': 'object',
                 'required': ['url'],
                 'properties': {'url': {'type': 'string',
                                        'description': 'Start URL (crawl stays on this domain)'},
                                'discover_only': {'type': 'boolean',
                                                  'description': 'true = return URL map only, no page '
                                                                 'content. For big sites prefer options '
                                                                 'sitemap=true (one-fetch map).'},
                                'focus': {'type': 'string',
                                          'description': 'Query: prioritize crawling links relevant to this '
                                                         '+ focus-filter each page. Token saver on doc '
                                                         'sites.'},
                                'crawl_urls': {'type': 'array',
                                               'items': {'type': 'string'},
                                               'description': 'Chosen subset of URLs to fetch (second-phase '
                                                              'selective crawl, no re-discovery). Use after '
                                                              'sitemap=true or discover_only=true.'},
                                'options': {'type': 'object',
                                            'description': "sitemap (true|'auto'|false,false: true=map from "
                                                           "sitemap.xml in one fetch; 'auto'=use if present "
                                                           'else BFS), max_pages (1-100,10), max_depth '
                                                           '(0-5,2), path_include (list of path prefixes), '
                                                           'path_exclude (list to skip), '
                                                           'max_content_chars_per (8000), max_total_chars '
                                                           '(token budget), concurrency (1-5,3), cache_ttl '
                                                           '(3600;0=fresh), respect_robots (true: robots.txt '
                                                           'checked before each page fetch; explicit bypass '
                                                           'only), force_fetcher '
                                                           "('http'|'stealthy'|'sleeper'), timeout "
                                                           '(ms,30000), deadline_ms (120000), auto_throttle '
                                                           '(false: adapt per-domain pacing), '
                                                           'throttle_min_delay (2.0 seconds when enabled).',
                                            'additionalProperties': False,
                                            'properties': {'sitemap': {'type': ['boolean', 'string']},
                                                           'max_pages': {'type': 'integer'},
                                                           'max_depth': {'type': 'integer'},
                                                           'path_include': {'type': 'array',
                                                                            'items': {'type': 'string'}},
                                                           'path_exclude': {'type': 'array',
                                                                            'items': {'type': 'string'}},
                                                           'max_content_chars_per': {'type': 'integer'},
                                                           'max_total_chars': {'type': 'integer'},
                                                           'concurrency': {'type': 'integer'},
                                                           'cache_ttl': {'type': 'integer'},
                                                           'respect_robots': {'type': 'boolean'},
                                                           'force_fetcher': {'type': 'string',
                                                                             'enum': ['http',
                                                                                      'stealthy',
                                                                                      'sleeper']},
                                                           'timeout': {'type': 'integer'},
                                                           'deadline_ms': {'type': 'integer'},
                                                           'auto_throttle': {'type': 'boolean'},
                                                           'throttle_min_delay': {'type': 'number'}}}},
                 'additionalProperties': False},
 'annotations': {'readOnlyHint': True, 'idempotentHint': True, 'openWorldHint': True},
 'title': 'Smart Crawl',
 'outputSchema': {'type': 'object', 'additionalProperties': True}}),
    'screenshot': Capability(_handle_screenshot, {'name': 'screenshot',
 'description': 'Screenshot a URL as an image. Multimodal agents only (content as images/canvas/visual '
                'layout). Text agents: use smart_fetch. Stealthy browser auto-managed.',
 'inputSchema': {'type': 'object',
                 'required': ['url'],
                 'properties': {'url': {'type': 'string', 'description': 'URL to screenshot'},
                                'session_id': {'type': 'string',
                                               'description': 'Optional: reuse a specific open browser '
                                                              'session. Omit to auto-manage.'},
                                'options': {'type': 'object',
                                            'description': 'full_page (bool,false), image_type '
                                                           '(png|jpeg,png), quality (0-100,jpeg), wait (ms), '
                                                           'wait_selector (css), network_idle (bool), '
                                                           'timeout (ms,30000).',
                                            'additionalProperties': True}}},
 'annotations': {'readOnlyHint': True, 'idempotentHint': True, 'openWorldHint': True},
 'title': 'Screenshot',
 'outputSchema': {'type': 'object', 'additionalProperties': True}}, ('browser',)),
    'smart_search': Capability(_handle_smart_search, {'name': 'smart_search',
 'description': 'Keyless web search (no API key, no account). 10 general backends in parallel '
                '(ddg,brave,mojeek,yahoo,yandex,startpage,google,qwant + opt-in wikipedia,grokipedia) + '
                'auto-fired specialized JSON-API backends based on query intent: Semantic Scholar (200M+ '
                'papers, AI-ranked, for research/factual queries), GitHub Search (repos sorted by stars, for '
                'code queries), Hacker News (tech community news/discussions, for news/howto queries). All '
                'run in parallel (zero added latency) and merge into the same ranking pipeline. '
                'Neural-reranked + intent-aware multi-query fan-out (detects query intent, sends expanded '
                'query variants to diversity engines for wider recall at zero added latency or request '
                'count) + six-signal ranking (cross-variant consensus + domain reputation + answer-signal '
                'scoring + title relevance + URL relevance) + result diversity (max 2 per domain in top '
                'results). Returns URLs + ranking + snippets, NOT page content. After search, smart_fetch '
                "the 1-2 best results with focus='your question' to get page content. \\n\\nBYOK: if you "
                'have configured search API keys (Serper, Tavily, Exa, Firecrawl, TinyFish) via `sieve keys '
                'add` or env vars (SIEVE_SEARCH_*_KEYS), those providers become the primary search source '
                "with key rotation and automatic fallback to this keyless search. \\n\\nANTI-PATTERN: Don't "
                'search for something you already have a URL for - use smart_fetch with focus= instead. '
                "Don't do one search per sub-fact - one broad search + 1-2 targeted fetches is enough. Don't "
                "fetch every search result. \\n\\nFILTERS (in options): site='domain.com' restricts to one "
                "domain. exclude_sites=['pinterest.com'] removes noise. freshness='day|week|month|year' for "
                'time-sensitive queries. page=0-10 for pagination. location/language/region for geo. '
                '\\n\\nRESULT FIELDS: relevance_score (0-1), fetch_relevance (high/med/low - fetch high '
                'first), engines_consensus (how many independent indexes returned this URL - higher = more '
                'authoritative; cross-variant consensus from multi-query fan-out strengthens this), '
                'source_type (docs|paper|repo|blog|forum|reference|news|other - pick the right source type), '
                'related_queries (follow-up queries from result titles+snippets).',
 'inputSchema': {'type': 'object',
                 'required': ['query'],
                 'properties': {'query': {'type': 'string', 'description': 'Search query'},
                                'options': {'type': 'object',
                                            'description': 'max_results (1-50,6), cache_ttl (300), mode '
                                                           '(auto|neural|find_similar; auto=neural if '
                                                           '[all]+model else consensus; find_similar needs '
                                                           'url=), engines (list, default: '
                                                           'ddg,brave,mojeek,yahoo,yandex,startpage,google,qwant; '
                                                           "add 'wikipedia'/'grokipedia'), site (domain "
                                                           'restrict), exclude_sites (list), location, '
                                                           'language (2-letter), region, page (0-10), '
                                                           'freshness (day|week|month|year), url (for '
                                                           'find_similar), stale_fallback (false; keyless '
                                                           'outages only), stale_max_age (1-86400 seconds,3600).',
                                            'additionalProperties': True}}},
 'annotations': {'readOnlyHint': True, 'idempotentHint': True, 'openWorldHint': True},
 'title': 'Smart Search',
 'outputSchema': {'type': 'object', 'additionalProperties': True}}),
    'cache_clear': Capability(_handle_cache_clear, None),
    'extract': Capability(_handle_extract, {'name': 'extract',
 'description': 'Fetch a URL and extract structured JSON via a declarative schema (no LLM). Ported from '
                'crawl4ai JsonCss/XPath strategies. \\n\\nSCHEMA: {"baseSelector": "div.product", "fields": '
                '[{"name": "title", "selector": "h2.name", "type": "text"}, {"name": "price", "selector": '
                '"span.price", "type": "text"}]}. \\n\\nField types: text | attribute (add "attribute": '
                '"href") | html | regex (add "regex": "\\d+") | nested (add "fields": [...]). \\n\\nUSAGE: '
                'after smart_search/smart_fetch found a structured page (product listings, tables, doc '
                'pages), call mcp_extract with url + schema to get clean JSON instead of markdown. Set '
                "xpath=true for XPath selectors. Respects force_fetcher='http'|'stealthy'|'sleeper'. Returns "
                '{url, status, content_ok, blocked, items, error}.',
 'inputSchema': {'type': 'object',
                 'required': ['url', 'schema'],
                 'properties': {'url': {'type': 'string', 'description': 'URL to fetch + extract'},
                                'schema': {'type': 'object',
                                           'description': 'Declarative extraction schema: {baseSelector, '
                                                          'fields:[{name, selector, type, transform}]}. '
                                                          'Field types: text|attribute|html|regex|nested.'},
                                'xpath': {'type': 'boolean',
                                          'description': 'True = XPath selectors, False = CSS (default)'},
                                'extraction_type': {'type': 'string',
                                                    'enum': ['html', 'text'],
                                                    'description': 'Pass-through to smart_fetch (default '
                                                                   'html)'},
                                'options': {'type': 'object',
                                            'description': 'timeout (ms), force_fetcher '
                                                           "('http'|'stealthy'|'sleeper'), cache_ttl "
                                                           '(0=fresh).',
                                            'additionalProperties': False,
                                            'properties': {'timeout': {'type': 'integer'},
                                                           'force_fetcher': {'type': 'string',
                                                                             'enum': ['http',
                                                                                      'stealthy',
                                                                                      'sleeper']},
                                                           'cache_ttl': {'type': 'integer'}}}},
                 'additionalProperties': False},
 'annotations': {'readOnlyHint': True, 'idempotentHint': True, 'openWorldHint': True},
 'title': 'Extract',
 'outputSchema': {'type': 'object', 'additionalProperties': True}}),
    'detect_block': Capability(_handle_detect_block, {'name': 'detect_block',
 'description': 'Detect whether a fetched response is a bot-block page '
                '(Cloudflare/Akamai/DataDome/PerimeterX/Imperva/Sucuri/Kasada). Pass status + headers + html '
                'from a previous fetch to decide if you should retry with force_fetcher=stealthy. Ported '
                'from crawl4ai antibot_detector.',
 'inputSchema': {'type': 'object',
                 'required': ['status'],
                 'properties': {'status': {'type': 'integer',
                                           'description': 'HTTP status code from the fetch'},
                                'headers': {'type': 'object',
                                            'description': 'Response headers dict (default {})'},
                                'html': {'type': 'string',
                                         'description': "Response body/html (default '')"}}},
 'annotations': {'readOnlyHint': True, 'idempotentHint': True, 'openWorldHint': True},
 'title': 'Detect Block',
 'outputSchema': {'type': 'object', 'additionalProperties': True}}),
    'sleeper_fetch': Capability(_handle_sleeper_fetch, None),
    'sleeper_api': Capability(_handle_sleeper_api, None),
    'extract_jsonl': Capability(_handle_extract_jsonl, {'name': 'extract_jsonl',
 'description': 'Fetch a URL and extract structured JSONL (one JSON object per line) via a declarative '
                'schema. JSONL feeds directly into LLM fine-tuning, pandas lines=True, DuckDB '
                'read_json_auto. Same schema format as mcp_extract. Every field present on every line with '
                'null for missing values.',
 'inputSchema': {'type': 'object',
                 'required': ['url', 'schema'],
                 'properties': {'url': {'type': 'string', 'description': 'URL to fetch'},
                                'schema': {'type': 'object',
                                           'description': 'Extraction schema: {baseSelector, fields: [{name, '
                                                          'selector, type}]} â\x80\x94 type: '
                                                          'text|attribute|html|regex|nested'},
                                'force_fetcher': {'type': 'string',
                                                  'description': 'http|stealthy|real â\x80\x94 override auto '
                                                                 'anti-bot fetcher selection'},
                                'validate': {'type': 'boolean',
                                             'description': 'Validate schema first and raise on errors '
                                                            '(default false)'}}},
 'annotations': {'readOnlyHint': True, 'idempotentHint': True, 'openWorldHint': True},
 'title': 'Extract Jsonl',
 'outputSchema': {'type': 'object', 'additionalProperties': True}}),
    'schema_gen': Capability(_handle_schema_gen, {'name': 'schema_gen',
 'description': 'LLM-once schema generation: one LLM call analyzes a sample page and emits a reusable '
                'extraction schema (baseSelector + CSS selectors per field). Then run '
                'mcp_extract/mcp_extract_jsonl with that schema forever with zero further LLM tokens. Use '
                "when you don't know a site's DOM structure. Requires FreeLLMAPI (:3001) reachable.",
 'inputSchema': {'type': 'object',
                 'required': ['url', 'fields'],
                 'properties': {'url': {'type': 'string', 'description': 'Sample page URL to analyze'},
                                'fields': {'type': 'array',
                                           'items': {'type': 'string'},
                                           'description': "Field names to extract, e.g. ['title', 'price', "
                                                          "'link']"},
                                'model': {'type': 'string', 'description': 'LLM model (default auto)'}}},
 'annotations': {'readOnlyHint': True, 'idempotentHint': True, 'openWorldHint': True},
 'title': 'Schema Gen',
 'outputSchema': {'type': 'object', 'additionalProperties': True}}),
    'proxy_health': Capability(_handle_proxy_health, None),
    'session_plan': Capability(_handle_session_plan, None),
    'datadome_harvest': Capability(_handle_datadome_harvest, None),
    'profile_identity': Capability(_handle_profile_identity, None),
    'sitemap_harvest': Capability(_handle_sitemap_harvest, {'name': 'sitemap_harvest',
 'description': 'Sitemap-first URL harvesting for hard-walled sites (Scout 13): many anti-bot-protected '
                'sites (Fiverr/PerimeterX, DataDome) expose sitemap.xml indexes publicly WITHOUT challenge '
                'â\x80\x94 verified live on fiverr.com. Harvest URL lists (gig/seller URLs) without fighting '
                'the bot wall. Action: harvest|fiverr.',
 'inputSchema': {'type': 'object',
                 'required': ['action'],
                 'properties': {'action': {'type': 'string',
                                           'enum': ['harvest', 'fiverr'],
                                           'description': 'harvest: generic sitemap walk of base_url; '
                                                          'fiverr: fiverr gig URL harvest via '
                                                          'sitemap_gigs[1-7]'},
                                'url': {'type': 'string',
                                        'description': 'Base URL for harvest (e.g. https://www.etsy.com)'},
                                'max_urls': {'type': 'integer',
                                             'description': 'Cap on collected URLs (default 2000)'}}},
 'annotations': {'readOnlyHint': True, 'idempotentHint': True, 'openWorldHint': True},
 'title': 'Sitemap Harvest',
 'outputSchema': {'type': 'object', 'additionalProperties': True}}),
    'research_ingest': Capability(_handle_research_ingest, {'name': 'research_ingest',
 'description': 'Fetch public Reddit research sources into stable source-neutral records. Supports '
                'reddit_rss and arctic_shift. Records carry provenance; this MCP tool returns normalized '
                'records with raw_payload set to None. Raw adapter payload opt-in is available only through '
                'the Python ingest_source API. No downstream application-specific fields are added.',
 'inputSchema': {'type': 'object',
                 'required': ['source'],
                 'properties': {'source': {'type': 'string', 'enum': ['reddit_rss', 'arctic_shift']},
                                'subreddit': {'type': 'string',
                                              'description': 'Optional subreddit name (with or without r/).'},
                                'target': {'type': 'string',
                                           'description': 'RSS target URL/name; use with reddit_rss for '
                                                          'search or thread feeds.'},
                                'query': {'type': 'string', 'description': 'Optional search/feed query.'},
                                'kind': {'type': 'string',
                                         'enum': ['posts', 'comments', 'ids', 'thread'],
                                         'default': 'posts'},
                                'ids': {'type': 'array',
                                        'items': {'type': 'string'},
                                        'description': 'Arctic Shift post/comment IDs for kind=ids.'},
                                'post_id': {'type': 'string',
                                            'description': 'Arctic Shift post ID for kind=thread.'},
                                'limit': {'type': 'integer', 'minimum': 1, 'maximum': 100, 'default': 25},
                                'after': {'type': 'string', 'description': 'Arctic Shift lower time bound.'},
                                'before': {'type': 'string', 'description': 'Arctic Shift upper time bound.'},
                                'sort': {'type': 'string', 'enum': ['asc', 'desc'], 'default': 'desc'},
                                'max_pages': {'type': 'integer', 'minimum': 1, 'maximum': 20, 'default': 1},
                                'timeout': {'type': 'number', 'exclusiveMinimum': 0, 'default': 15},
                                'retries': {'type': 'integer', 'minimum': 0, 'maximum': 5, 'default': 2}}},
 'annotations': {'readOnlyHint': True, 'idempotentHint': True, 'openWorldHint': True},
 'title': 'Research Ingest',
 'outputSchema': {'type': 'object', 'additionalProperties': True}}),
    'version': Capability(_handle_version, None),
}
CANONICAL_CAPABILITIES = frozenset(CAPABILITY_REGISTRY)
