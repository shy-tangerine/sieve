"""Self-healing CLI entry point for sieve.

This module is the console entry point (sieve = sieve.cli:main). It is
deliberately lightweight: NO heavy imports at module level. When sieve.exe
runs, it does `from sieve.cli import main` which imports
`sieve.__init__` (just __version__, no deps) and this module (stdlib
only). The heavy server import happens lazily inside main(), wrapped in a
try/except that auto-recovers from a broken install.

Self-heal flow:
1. User runs `sieve` (any command) after a broken update/dep change
2. `from sieve.server import main` fails (ImportError/ModuleNotFoundError)
3. cli.py catches it, checks if ~/.sieve/repair.py exists
4. If yes: runs it automatically (stops sieve + force-reinstalls)
5. If no: prints a clean one-line error (not a traceback) with the fix command
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from sieve.public_output import failure_payload as safe_error, safe_public_json

PROJECT_ROOT = str(Path(__file__).resolve().parents[1])

# Cap for agent/user-selected JSON files (schemas, pipeline specs, integration
# definitions) read from disk (issue #220): bound the bytes BEFORE parsing so a
# huge local file cannot consume memory before schema/node limits apply.
_MAX_SPEC_FILE_BYTES = 2 * 1024 * 1024


def _read_bounded_text(path_value: str, *, what: str = "file") -> str:
    """Read a local JSON input file with an explicit size cap."""
    path = Path(path_value)
    if not path.is_file():
        raise ValueError(f"{what} does not exist: {path_value}")
    size = path.stat().st_size
    if size > _MAX_SPEC_FILE_BYTES:
        raise ValueError(f"{what} exceeds {_MAX_SPEC_FILE_BYTES // (1024 * 1024)} MiB: {path_value}")
    return path.read_text(encoding="utf-8")


def _print_version() -> None:
    """Show the local Sieve version without loading the legacy server."""
    from sieve import __version__

    print(f"Sieve {__version__}")


def _print_cli_help() -> None:
    """Show the public CLI surface without importing the legacy server."""
    print("Sieve - bounded media and web extraction CLI")
    print()
    print("Usage:")
    print("  sieve youtube search QUERY [--max-results N] [--timeout SECONDS]")
    print("  sieve youtube metadata URL [--timeout SECONDS]")
    print("  sieve youtube download URL OUTPUT_DIR [--format FORMAT] [--timeout SECONDS]")
    print("  sieve social fetch URL [--reader sieve|jina|browser] [--max-posts N] [browser options]")
    print("  sieve social collect PROFILE_URL [browser options]")
    print("  sieve social comments URL [--max-comments N]")
    print("  sieve osint maigret USERNAME --allow-osint [--tags TAGS] [--timeout SECONDS]")
    print("  sieve media download URL [--timeout SECONDS]")
    print("  sieve media transcribe URL [--model NAME] [--timeout SECONDS]  (needs stt extra)")
    print("  sieve search QUERY [--max-results N] [--cached|--refresh]")
    print("  sieve images QUERY [--max-results N] [--safe-search strict|moderate|off] [--timeout SECONDS]")
    print("  images use Brave Search API; follow Brave terms and verify image rights before reuse")
    print("  sieve fetch [--structured] URL [...]")
    print("  sieve crawl URL [--focus QUERY] [--max-pages N]")
    print("  sieve extract URL --schema JSON")
    print("  sieve batch-extract [URL ...] --schema JSON [--input FILE] [--checkpoint FILE]")
    print("  sieve pipeline SPEC.json [--max-nodes N] [--max-items N] [--timeout SECONDS]")
    print("  sieve integration scaffold NAME BASE_URL [--output FILE]")
    print("  sieve integration list|validate FILE|invoke NAME WORKFLOW [--args JSON]")
    print("  sieve screenshot URL [--full-page]")
    print("  sieve cache clear [--clear-all]")
    print("  sieve setup                 configure keys, browser backend, and safety gates")
    print("  sieve skill install         install Sieve skills for local agents")
    print("  sieve update check [--json]   check for a release (read-only)")
    print("  sieve update apply [--yes]    review and explicitly apply a release")
    print()
    print("Browser options:")
    print("  --cdp-url URL       connect to an existing local Chrome/Chromium session")
    print("  --real-chrome       use a real local browser when available")
    print("  --wait-selector CSS wait for a selector before extracting")
    print("  --network-idle      wait for network quiescence")
    print()
    print("Other commands:")
    print("  sieve mcp serve      start the MCP server (stdio by default)")
    print("  sieve --version      show the local Sieve version")
    print("  sieve --help         show this CLI help")
    print()
    print(" sieve social login instagram [--profile-dir PATH]")
    print("All command results are JSON on stdout; diagnostics go to stderr.")


def _run_social_login(argv: list[str]) -> int | None:
    """Bootstrap a Sieve-owned persistent browser profile interactively."""
    if len(argv) < 2 or argv[0:2] != ["social", "login"]:
        return None

    parser = argparse.ArgumentParser(prog="sieve social login")
    parser.add_argument("platform", choices=["instagram"])
    parser.add_argument(
        "--profile-dir",
        default=os.environ.get("SIEVE_BROWSER_PROFILE_DIR")
        or "~/.sieve/profiles/instagram",
        help="persistent Sieve browser profile directory",
    )
    args = parser.parse_args(argv[2:])
    if not sys.stdin.isatty():
        print(safe_public_json({"ok": False, "error": "social login requires an interactive terminal"}))
        return 1

    requested_profile = Path(args.profile_dir).expanduser()
    if requested_profile.is_symlink():
        print(safe_public_json({"ok": False, "error": "profile directory must not be a symlink"}))
        return 1
    profile_dir = requested_profile.resolve()
    profile_dir.mkdir(parents=True, exist_ok=True)
    if os.name != "nt":
        profile_dir.chmod(0o700)

    async def _open_login() -> None:
        import asyncio
        from sieve.browser import StealthyBrowser

        browser = StealthyBrowser(
            headless=False,
            real_chrome=False,
            cdp_url=None,
            profile_dir=str(profile_dir),
            block_ads=False,
        )
        await browser.start()
        page = await browser._context.new_page()
        try:
            await page.goto(
                "https://www.instagram.com/accounts/login/",
                wait_until="domcontentloaded",
            )
            print(
                "Sieve Chromium is open. Complete Instagram login, then return here.",
                file=sys.stderr,
                flush=True,
            )
            await asyncio.to_thread(input, "Press Enter after login is complete: ")
        finally:
            await browser.close()

    try:
        import asyncio
        asyncio.run(_open_login())
    except (KeyboardInterrupt, EOFError):
        print(safe_public_json({"ok": False, "error": "Instagram login cancelled"}))
        return 1
    except Exception as exc:
        print(safe_public_json({"ok": False, "error": f"social login failed: {type(exc).__name__}"}))
        return 1

    print(safe_public_json({"ok": True, "platform": args.platform, "profile_ready": True}))
    return 0


def _run_media_command(argv: list[str]) -> int | None:
    """Handle media commands without importing the legacy server stack."""
    # media download + legacy bare aliases
    if argv and argv[0] == "media":
        parser = argparse.ArgumentParser(prog="sieve media")
        sub = parser.add_subparsers(dest="action")
        dl = sub.add_parser("download")
        dl.add_argument("url")
        dl.add_argument("--timeout", type=int, default=30)
        tr = sub.add_parser("transcribe", help="transcribe media with local Whisper")
        tr.add_argument("url")
        tr.add_argument("--model", default=None)
        tr.add_argument("--timeout", type=int, default=None,
                        help="timeout seconds (default: derive from media duration; max 600)")
        args = parser.parse_args(argv[1:])
        if args.action == "transcribe":
            try:
                from sieve import stt
                result = stt.transcribe(args.url, model=args.model, timeout=args.timeout)
                print(safe_public_json(result, ensure_ascii=False))
                return 0
            except Exception as exc:
                print(safe_public_json({"ok": False, **safe_error(exc)}))
                return 1
        if args.action != "download":
            parser.print_help()
            return 0
        try:
            from sieve.youtube import resolve_urls
            urls = resolve_urls(args.url, args.timeout)
            result = {"ok": True, "source_url": args.url, "via": "yt-dlp", "urls": urls, "count": len(urls)}
            print(safe_public_json(result, ensure_ascii=False))
            return 0
        except Exception as exc:
            print(safe_public_json({"ok": False, **safe_error(exc)}))
            return 1
    if not argv or argv[0] not in {"youtube", "social"}:
        return None
    parser = argparse.ArgumentParser(prog=f"sieve {argv[0]}")
    if argv[0] == "youtube":
        sub = parser.add_subparsers(dest="action")
        search = sub.add_parser("search")
        search.add_argument("query")
        search.add_argument("--max-results", type=int, default=6)
        search.add_argument("--timeout", type=int, default=45)
        metadata = sub.add_parser("metadata")
        metadata.add_argument("url")
        metadata.add_argument("--timeout", type=int, default=45)
        download = sub.add_parser("download")
        download.add_argument("url")
        download.add_argument("output_dir")
        download.add_argument("--format", dest="format_name")
        download.add_argument("--timeout", type=int, default=120)
    else:
        sub = parser.add_subparsers(dest="action")
        fetch = sub.add_parser("fetch")
        fetch.add_argument("url")
        fetch.add_argument("--reader", choices=["sieve", "jina", "browser"], default="sieve")
        fetch.add_argument("--timeout", type=int, default=30)
        fetch.add_argument("--max-media", type=int, default=20)
        fetch.add_argument("--cdp-url")
        fetch.add_argument("--real-chrome", action="store_true")
        fetch.add_argument("--wait", type=int, default=1500)
        fetch.add_argument("--wait-selector")
        fetch.add_argument("--network-idle", action="store_true")
        fetch.add_argument("--browser-backend", choices=["auto", "pool", "sleeper"], default=None)
        fetch.add_argument("--max-posts", type=int, default=0,
                           help="collect up to N profile posts (browser reader)")
        fetch.add_argument("--checkpoint", help="append collected posts as resumable JSONL")
        fetch.add_argument("--max-scrolls", type=int, default=20)
        collect = sub.add_parser("collect")
        collect.add_argument("url")
        collect.add_argument("--creator")
        collect.add_argument("--cdp-url")
        collect.add_argument("--real-chrome", action="store_true")
        collect.add_argument("--scrolls", type=int, default=20)
        collect.add_argument("--scroll-delay", type=int, default=1200)
        collect.add_argument("--max-items", type=int, default=100)
        collect.add_argument("--timeout", type=int, default=60)
        collect.add_argument("--browser-backend", choices=["auto", "pool", "sleeper"], default=None)
        collect.add_argument("--checkpoint", help="append collected posts as resumable JSONL")
        cmts = sub.add_parser("comments")
        cmts.add_argument("url")
        cmts.add_argument("--max-comments", type=int, default=50)
        cmts.add_argument("--reader", choices=["ytdlp", "browser"], default="ytdlp")
        cmts.add_argument("--timeout", type=int, default=90)
    args = parser.parse_args(argv[1:])
    try:
        if argv[0] == "youtube":
            from sieve import youtube
            if args.action == "search":
                result = youtube.search(args.query, args.max_results, args.timeout)
            elif args.action == "metadata":
                result = youtube.metadata(args.url, args.timeout)
            elif args.action == "download":
                result = youtube.download(args.url, args.output_dir, args.timeout, args.format_name)
            else:
                parser.print_help()
                return 0
        else:
            from sieve.social import collect_browser, comments, fetch, fetch_browser
            if args.action == "comments":
                result = comments(args.url, max_comments=args.max_comments, timeout=args.timeout, reader=args.reader)
            elif args.action != "fetch":
                if args.action == "collect":
                    result = collect_browser(args.url, creator=args.creator, cdp_url=args.cdp_url,
                                             real_chrome=args.real_chrome, scrolls=args.scrolls,
                                             scroll_delay=args.scroll_delay, timeout=args.timeout,
                                             max_items=args.max_items, backend=args.browser_backend,
                                             checkpoint=args.checkpoint)
                else:
                    parser.print_help()
                    return 0
            elif args.reader == "browser":
                result = fetch_browser(args.url, cdp_url=args.cdp_url,
                                       real_chrome=args.real_chrome, wait=args.wait,
                                       wait_selector=args.wait_selector,
                                       network_idle=args.network_idle,
                                             timeout=args.timeout, max_media=args.max_media,
                                       backend=args.browser_backend, max_posts=args.max_posts,
                                       checkpoint=args.checkpoint, max_scrolls=args.max_scrolls)
            else:
                result = fetch(args.url, reader=args.reader, timeout=args.timeout, max_media=args.max_media)
        print(safe_public_json(result, ensure_ascii=False))
        return 0
    except Exception as exc:
        # Keep failures on stdout too: downstream callers consume one JSON
        # stream and should not need a second channel to classify a failure.
        print(safe_public_json({"ok": False, **safe_error(exc)}))
        return 1


def _run_images_command(argv: list[str]) -> int | None:
    """Run the bounded BYOK image-search contract without the legacy server."""
    if not argv or argv[0] != "images":
        return None

    class _ImageArgumentParser(argparse.ArgumentParser):
        """Raise into the image command's existing JSON failure seam."""

        def error(self, message: str) -> None:
            raise ValueError(message)

    parser = _ImageArgumentParser(prog="sieve images")
    parser.add_argument("query")
    parser.add_argument("--max-results", type=int, default=6)
    parser.add_argument("--timeout", type=float, default=15)
    parser.add_argument(
        "--safe-search",
        "--safesearch",
        dest="safe_search",
        choices=["strict", "moderate", "off"],
        default="strict",
    )
    query = None
    safe_search = "strict"
    try:
        args = parser.parse_args(argv[1:])
        query = args.query
        safe_search = args.safe_search
        from sieve.image_search import search_images

        result = search_images(
            args.query,
            max_results=args.max_results,
            safe_search=args.safe_search,
            timeout=args.timeout,
        )
        print(safe_public_json(result.to_dict(), ensure_ascii=False))
        return 0 if result.ok else 1
    except Exception as exc:
        print(
            safe_public_json(
                {
                    "ok": False,
                    "query": query,
                    "results": [],
                    "provider": None,
                    "safe_search": safe_search,
                    **safe_error(exc),
                },
                ensure_ascii=False,
            )
        )
        return 1


def _run_with_deadline(coro, deadline_seconds: float):
    """Run a coroutine with a hard overall deadline (issue #469).

    Unlike a per-request timeout, this bounds setup, retries, and shutdown:
    on expiry the coroutine is cancelled and a machine-readable timeout error
    is raised instead of the process idling indefinitely.
    """
    import asyncio

    async def _bounded():
        return await asyncio.wait_for(coro, timeout=deadline_seconds)

    try:
        return asyncio.run(_bounded())
    except asyncio.TimeoutError:
        raise TimeoutError(
            f"fetch did not complete within the {deadline_seconds}s CLI deadline "
            "(per-request timeout plus shutdown allowance)"
        ) from None


def _run_structured_fetch(argv: list[str]) -> int | None:
    """Fetch one or more URLs with Schema.org/JSON-LD data in the envelope.

    ``fetch`` normally delegates to the legacy server parser so its historical
    output remains unchanged.  This opt-in route gives structured-data users a
    discoverable flag while reusing the canonical smart-fetch and metadata
    extraction seams.  Entities are emitted at ``metadata.structured_data``;
    RDFa is intentionally not part of this contract.
    """
    if not argv or argv[0] != "fetch" or "--structured" not in argv[1:]:
        return None
    parser = argparse.ArgumentParser(prog="sieve fetch")
    parser.add_argument("urls", nargs="+", help="URL(s) to fetch")
    parser.add_argument(
        "--structured",
        action="store_true",
        help="include parsed JSON-LD and nested Schema.org microdata",
    )
    parser.add_argument("--timeout", type=int, default=30)
    parser.add_argument(
        "--cache-ttl",
        type=int,
        default=3600,
        help="cache lifetime in seconds (default: 3600)",
    )
    args = parser.parse_args(argv[1:])
    try:
        from sieve.server import MasterFetchServer

        server = MasterFetchServer(cache_ttl=args.cache_ttl)
        fetch_args = {
            "extraction_type": "structured",
            "cache_ttl": args.cache_ttl,
            "timeout": args.timeout * 1000,
        }
        # Overall CLI deadline (#469): per-request timeouts don't cover setup,
        # retries, and shutdown. When the network is denied (e.g. sandbox), the
        # process could idle in the event loop forever. Enforce a hard ceiling
        # on the whole fetch so the CLI always produces bounded output. The
        # shutdown allowance is env-tunable so CI can use a tight bound.
        try:
            allowance = int(os.environ.get("SIEVE_CLI_DEADLINE_ALLOWANCE", "30"))
        except ValueError:
            allowance = 30
        deadline = args.timeout + max(1, allowance)
        if len(args.urls) == 1:
            result = _run_with_deadline(
                server.smart_fetch(args.urls[0], **fetch_args), deadline)
        else:
            result = _run_with_deadline(
                server.smart_fetch(args.urls[0], urls=args.urls, **fetch_args),
                deadline)
        print(safe_public_json(result, ensure_ascii=False))
        return 0
    except Exception as exc:
        # Keep failures machine-readable on stdout, matching the other
        # lightweight CLI routes.
        print(safe_public_json({
            "ok": False,
            **safe_error(exc),
        }, ensure_ascii=False))
        return 1


def _run_osint_command(argv: list[str]) -> int | None:
    if not argv or argv[0] != "osint":
        return None
    parser = argparse.ArgumentParser(prog="sieve osint")
    sub = parser.add_subparsers(dest="adapter")
    maigret = sub.add_parser("maigret")
    maigret.add_argument("username")
    maigret.add_argument("--allow-osint", action="store_true",
                         help="acknowledge an authorized public username check")
    maigret.add_argument("--executable", default="maigret")
    maigret.add_argument("--timeout", type=float, default=60)
    maigret.add_argument("--max-output-bytes", type=int, default=1_000_000)
    maigret.add_argument("--max-sites", type=int, default=500)
    maigret.add_argument("--tags")
    maigret.add_argument("--include-username", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args(argv[1:])
    if args.adapter != "maigret":
        parser.print_help()
        return 0
    try:
        from sieve.osint import run_maigret
        result = run_maigret(args.username, allow_osint=args.allow_osint, executable=args.executable,
                             timeout=args.timeout, max_output_bytes=args.max_output_bytes,
                             max_sites=args.max_sites, tags=args.tags,
                             include_username=args.include_username)
        print(safe_public_json(result, ensure_ascii=False))
        return 0
    except Exception as exc:
        print(safe_public_json({"ok": False, **safe_error(exc)}, ensure_ascii=False))
        return 1


def _run_batch_extract(argv: list[str]) -> int | None:
    """Run bounded JSONL batch extraction without loading the legacy server."""
    if not argv or argv[0] != "batch-extract":
        return None
    parser = argparse.ArgumentParser(prog="sieve batch-extract")
    parser.add_argument("urls", nargs="*")
    parser.add_argument("--input", dest="input_file")
    parser.add_argument("--schema", required=True, help="JSON schema string or @file")
    parser.add_argument("--checkpoint")
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--retry-failed", action="store_true", help="retry failed v2 checkpoint entries")
    parser.add_argument("--migrate-v1", action="store_true", help="explicitly import unknown v1 outcomes")
    parser.add_argument("--progress", action="store_true", help="emit completion summaries on stderr")
    parser.add_argument("--concurrency", type=int, default=3)
    parser.add_argument("--timeout", type=float, default=30)
    parser.add_argument("--max-pages", type=int, default=None)
    args = parser.parse_args(argv[1:])
    try:
        from sieve.batch_extract import read_urls, run_batch
        schema_text = args.schema
        if schema_text.startswith("@"):
            schema_text = _read_bounded_text(schema_text[1:], what="schema file")
        schema = json.loads(schema_text)
        if not isinstance(schema, dict):
            raise ValueError("schema must be a JSON object")
        urls = read_urls(args.urls, args.input_file, max_urls=100)
        import asyncio
        records = asyncio.run(run_batch(urls, schema, concurrency=args.concurrency,
                                         timeout=args.timeout, max_pages=args.max_pages,
                                         checkpoint=args.checkpoint, resume=args.resume,
                                         retry_failed=args.retry_failed, migrate_v1=args.migrate_v1,
                                         on_progress=(lambda record: print(safe_public_json(record), file=sys.stderr))
                                         if args.progress else None))
        for record in records:
            print(safe_public_json(record, ensure_ascii=False))
        return 0 if all(item["ok"] for item in records) else 1
    except Exception as exc:
        # Rationale: the CLI boundary emits a safe failure and exits nonzero.
        print(safe_public_json({"ok": False, **safe_error(exc)}))
        return 1


def _run_pipeline(argv: list[str]) -> int | None:
    """Run a validated, bounded declarative pipeline spec."""
    if not argv or argv[0] != "pipeline":
        return None
    parser = argparse.ArgumentParser(prog="sieve pipeline")
    parser.add_argument("spec", help="JSON pipeline spec file")
    parser.add_argument("--max-nodes", type=int, default=32)
    parser.add_argument("--max-items", type=int, default=1000)
    parser.add_argument("--timeout", type=float, default=300)
    args = parser.parse_args(argv[1:])
    try:
        spec = json.loads(_read_bounded_text(args.spec, what="pipeline spec"))
        from sieve.pipeline import run_pipeline
        from sieve.server import MasterFetchServer
        server = MasterFetchServer(cache_ttl=0)

        async def fetch(url: str):
            result = await server.fetch(url, extraction_type="markdown", timeout=30000)
            return result.model_dump() if hasattr(result, "model_dump") else result

        async def extract(url: str, schema: dict):
            return await server.extract(url, schema=schema, extraction_type="html", timeout=30000)

        async def batch(urls: list[str], schema: dict):
            from sieve.batch_extract import run_batch
            return await run_batch(urls, schema, timeout=min(args.timeout, 3600), max_pages=len(urls))

        schema_generator = None
        from sieve.byok_config import get_llm_key
        _llm_key = get_llm_key()
        if _llm_key:
            from sieve.schema_gen import generate_schema
            def schema_generator(sample_html: str, fields: list[str]):
                return generate_schema(sample_html, fields,
                                       model=os.environ.get("SIEVE_LLM_MODEL", "auto"),
                                       base_url=os.environ.get("SIEVE_LLM_BASE_URL", "http://localhost:3001/v1"),
                                       api_key=get_llm_key(), timeout=args.timeout)

        import asyncio
        records = asyncio.run(run_pipeline(spec, fetch=fetch, extract=extract, batch_extract=batch,
                                            schema_generator=schema_generator,
                                            max_nodes=args.max_nodes, max_items=args.max_items,
                                            timeout=args.timeout))
        print(safe_public_json(records, ensure_ascii=False))
        return 0 if all(item["ok"] for item in records) else 1
    except Exception as exc:
        print(safe_public_json({"ok": False, **safe_error(exc)}, ensure_ascii=False))
        return 1


def _run_integration(argv: list[str]) -> int | None:
    """Manage and explicitly invoke declarative user API integrations."""
    if not argv or argv[0] != "integration":
        return None
    parser = argparse.ArgumentParser(prog="sieve integration")
    sub = parser.add_subparsers(dest="action")
    sub.add_parser("list")
    scaffold = sub.add_parser("scaffold", help="write a reviewable integration template")
    scaffold.add_argument("name")
    scaffold.add_argument("base_url")
    scaffold.add_argument("--output")
    scaffold.add_argument("--directory")
    scaffold.add_argument("--workflow", default="lookup")
    scaffold.add_argument("--method", choices=["GET", "POST", "PUT", "PATCH", "DELETE"], default="GET")
    scaffold.add_argument("--path", default="/")
    scaffold.add_argument("--secret-ref")
    scaffold.add_argument("--auth-type", choices=["bearer", "header", "query"], default="bearer")
    scaffold.add_argument("--routing", choices=["prefer", "fallback", "off"], default="prefer")
    scaffold.add_argument("--description")
    scaffold.add_argument("--schema-url")
    scaffold.add_argument("--schema-version")
    scaffold.add_argument("--license", dest="license_name")
    scaffold.add_argument("--overwrite", action="store_true")
    val = sub.add_parser("validate")
    val.add_argument("file")
    inv = sub.add_parser("invoke")
    inv.add_argument("name")
    inv.add_argument("workflow")
    inv.add_argument("--args", default="{}", help="JSON object of workflow arguments")
    inv.add_argument("--routing", choices=["prefer", "fallback", "off"],
                     help="API routing mode (defaults to the integration file)")
    args = parser.parse_args(argv[1:])
    try:
        from sieve.integrations import (load_integrations, validate_integration,
                                        route_integration, scaffold_integration)
        if args.action == "scaffold":
            path = scaffold_integration(
                args.name, args.base_url, directory=args.directory, output=args.output,
                workflow=args.workflow, method=args.method, path=args.path,
                secret_ref=args.secret_ref, auth_type=args.auth_type, routing=args.routing,
                description=args.description, schema_url=args.schema_url,
                schema_version=args.schema_version, license_name=args.license_name,
                overwrite=args.overwrite,
            )
            print(safe_public_json({"ok": True, "name": args.name, "path": str(path),
                              "review": "validate this file before installing or invoking it"}))
            return 0
        if args.action == "validate":
            spec = json.loads(_read_bounded_text(args.file, what="integration spec"))
            clean = validate_integration(spec)
            print(safe_public_json({"ok": True, "name": clean["name"], "workflows": sorted(clean["workflows"])}))
            return 0
        integrations = load_integrations()
        if args.action == "list":
            print(safe_public_json([{"name": n, "workflows": sorted(s["workflows"])} for n, s in integrations.items()]))
            return 0
        if args.action != "invoke":
            parser.print_help()
            return 0
        call_args = json.loads(args.args)
        if not isinstance(call_args, dict):
            raise ValueError("--args must be a JSON object")
        result = route_integration(integrations[args.name], args.workflow, call_args,
                                   routing=args.routing)
        print(safe_public_json(result, ensure_ascii=False))
        return 0 if result.get("ok") else 1
    except Exception as exc:
        print(safe_public_json({"ok": False, **safe_error(exc)}, ensure_ascii=False))
        return 1


def _run_update(argv: list[str]) -> int | None:
    """Handle update commands before the heavyweight server import."""
    if not argv or argv[0] != "update":
        return None
    parser = argparse.ArgumentParser(prog="sieve update")
    sub = parser.add_subparsers(dest="action")
    check = sub.add_parser("check", help="check for a release without changing files")
    check.add_argument("--json", action="store_true", dest="json_output")
    apply = sub.add_parser("apply", help="apply a checked release")
    apply.add_argument("--yes", action="store_true", help="confirm the update")
    parser.add_argument("--yes", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args(argv[1:])
    from sieve import updater
    if args.action == "check":
        return updater.update_command(check_only=True, json_output=args.json_output)
    return updater.update_command(yes=getattr(args, "yes", False))


def _run_repair() -> int:
    """Run ~/.sieve/repair.py to auto-recover a broken install.

    If repair.py doesn't exist, writes a minimal one inline and runs it.
    Never leaves the user stranded with a traceback.
    """
    repair = os.path.join(os.path.expanduser("~"), ".sieve", "repair.py")
    if not os.path.exists(repair):
        # Write a minimal repair script (same logic as updater._write_repair_script
        # but standalone so we don't need to import the updater module).
        os.makedirs(os.path.dirname(repair), exist_ok=True)
        script = _repair_script()
        try:
            with open(repair, "w") as f:
                f.write(script)
        except Exception:
            # Can't write repair.py - run pip directly as a last resort
            print("  recovering (direct reinstall)...")
            import subprocess
            subprocess.run([sys.executable, "-m", "pip", "install",
                           "-e", os.environ.get("SIEVE_PROJECT_DIR", PROJECT_ROOT),
                           "--quiet", "--disable-pip-version-check"],
                          timeout=120)
            print("  Sieve recovered. Re-run your command.")
            return 0
    import subprocess
    print("  recovering...")
    try:
        result = subprocess.run(
            [sys.executable, repair],
            timeout=120,
            capture_output=False,
        )
        if result.returncode == 0:
            print("  Sieve recovered. Re-run your command.")
            return 0
        print("  Recovery failed. Run manually: "
              f'python "{repair}"')
        return 1
    except Exception as e:
        print(f"  Recovery error: {e}")
        print(f'  Run manually: python "{repair}"')
        return 1


def _repair_script() -> str:
    """Return the standalone repair helper written for a broken install."""
    return f'''import os, sys, subprocess
print("Sieve repair: stopping any running sieve...")
if sys.platform == "win32":
    subprocess.run(["taskkill", "/IM", "sieve.exe", "/F"], capture_output=True)
else:
    subprocess.run(["pkill", "-x", "sieve"], capture_output=True)
print("Sieve repair: reinstalling the local project...")
r = subprocess.run([sys.executable, "-m", "pip", "install", "-e", os.environ.get("SIEVE_PROJECT_DIR", {PROJECT_ROOT!r}),
                    "--quiet", "--disable-pip-version-check"])
if r.returncode != 0:
    print("Sieve repair: reinstall failed (installer exit %d)." % r.returncode)
    print("  Try manually: uv pip install --editable $SIEVE_PROJECT_DIR")
    sys.exit(1)
try:
    from importlib.metadata import version as _v
    print("Sieve " + _v("sieve-cli") + "  repaired")
except Exception:
    print("Sieve repair: reinstalled (version check skipped)")
'''


def main() -> int:
    """Entry point that self-heals on broken imports."""
    argv = sys.argv[1:]
    social_login_result = _run_social_login(argv)
    if social_login_result is not None:
        return social_login_result
    if not argv or argv[0] in {"-h", "--help"}:
        _print_cli_help()
        return 0
    if argv[0] in {"-v", "--version"}:
        _print_version()
        return 0
    if argv[0] == "setup":

        from sieve.setup import run_setup
        return run_setup(argv[1:])

    if argv[0] == "skill":
        from sieve.setup import run_skill_command
        return run_skill_command(argv[1:])

    update_result = _run_update(argv)
    if update_result is not None:
        return update_result

    # MCP stdio surface (new file, graceful degrade when mcp absent)
    try:
        from sieve.mcp_stdio import handle_mcp_argv as _mcp_route
        _mcp_rc = _mcp_route(argv)
        if _mcp_rc is not None:
            return _mcp_rc
    except SystemExit:
        raise
    except Exception:
        pass
    structured_fetch_result = _run_structured_fetch(argv)
    if structured_fetch_result is not None:
        return structured_fetch_result
    media_result = _run_media_command(argv)
    if media_result is not None:
        return media_result
    images_result = _run_images_command(argv)
    if images_result is not None:
        return images_result
    osint_result = _run_osint_command(argv)
    if osint_result is not None:
        return osint_result
    batch_result = _run_batch_extract(argv)
    if batch_result is not None:
        return batch_result
    pipeline_result = _run_pipeline(argv)
    if pipeline_result is not None:
        return pipeline_result
    integration_result = _run_integration(argv)
    if integration_result is not None:
        return integration_result
    try:
        from sieve.server import main as _server_main
        return _server_main() or 0
    except (ImportError, ModuleNotFoundError) as e:
        # Broken install: missing dep, half-failed update, etc.
        # Don't crash with a traceback - auto-recover.
        mod_name = getattr(e, "name", "") or str(e)
        print(f"  Sieve install broken: {mod_name}")
        rc = _run_repair()
        if rc != 0:
            print("  If recovery failed, run: pip install -e $SIEVE_PROJECT_DIR")
            print("  Or: sieve --doctor")
        return rc
    except Exception:
        # Any other import-time crash (not a missing module) - re-raise
        # so real bugs surface, but only after trying repair as a last resort
        # if the error looks install-related.
        raise
