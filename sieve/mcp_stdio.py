"""stdio MCP surface â reuses MasterFetchServer.build_mcp_server().

No new deps beyond legacy-server extra. Degrades gracefully when mcp absent.
"""
from __future__ import annotations

import sys

MISSING_MSG = "MCP not installed. Run: uv sync --extra legacy-server or '.[all]'"


def _get_server():
    try:
        from sieve.server import MasterFetchServer
    except ImportError:
        print(MISSING_MSG, file=sys.stderr)
        sys.exit(1)
    return MasterFetchServer()


def run_stdio() -> int:
    try:
        import mcp  # noqa: F401
    except ImportError:
        print(MISSING_MSG, file=sys.stderr)
        return 1
    srv = _get_server()
    srv.serve(http=False)
    return 0


def run_http(host: str = "127.0.0.1", port: int = 8765) -> int:
    try:
        import mcp  # noqa: F401
    except ImportError:
        print(MISSING_MSG, file=sys.stderr)
        return 1
    srv = _get_server()
    srv.serve(http=True, host=host, port=port)
    return 0


def handle_mcp_argv(argv: list[str]) -> int | None:
    """Handle the canonical ``sieve mcp serve`` command."""
    if not argv or argv[0] != "mcp":
        return None
    import argparse
    p = argparse.ArgumentParser(prog="sieve mcp", description="MCP server")
    sub = p.add_subparsers(dest="command", required=True)
    serve = sub.add_parser("serve", help="serve MCP over stdio or streamable HTTP")
    serve.add_argument("--transport", choices=["stdio", "http"], default="stdio")
    serve.add_argument("--host", default="127.0.0.1", help="HTTP bind host")
    serve.add_argument("--port", type=int, default=8765, help="HTTP bind port")
    args = p.parse_args(argv[1:])
    if args.transport == "stdio":
        return run_stdio()
    return run_http(host=args.host, port=args.port)


def main() -> int:
    # also works as `python -m sieve.mcp_stdio [--transport ...]`
    import argparse
    p = argparse.ArgumentParser(prog="sieve mcp")
    sub = p.add_subparsers(dest="command", required=True)
    serve = sub.add_parser("serve")
    serve.add_argument("--transport", choices=["stdio", "http"], default="stdio")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8765)
    args = p.parse_args(sys.argv[1:])
    if args.transport == "stdio":
        return run_stdio()
    return run_http(host=args.host, port=args.port)


if __name__ == "__main__":
    raise SystemExit(main())
