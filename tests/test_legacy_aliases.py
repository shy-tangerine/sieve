"""Legacy alias and deprecation coverage.

Covers retired MCP prefixed names (mcp_*, mcp-*), legacy CLI flags
(--http, positional key input), and hidden compatibility shims.  These
tests pin the deprecation boundary so a future removal is safe.
"""
from __future__ import annotations

import io
import json
import os
import sys
from unittest.mock import AsyncMock, patch

import pytest

from sieve.command_router import CapabilityRouter, CANONICAL_CAPABILITIES
from sieve.server import MasterFetchServer, VersionInfoModel


# ── MCP retired aliases ──────────────────────────────────────────────

class TestMCPRetiredAliases:
    """All retired MCP tool names must be rejected by the router."""

    @pytest.mark.parametrize("name", [
        "mcp_version",
        "mcp_smart_fetch",
        "mcp_smart_search",
        "mcp_smart_crawl",
        "mcp_extract",
        "mcp_screenshot",
    ])
    @pytest.mark.asyncio
    async def test_mcp_prefixed_names_rejected(self, name):
        router = CapabilityRouter(type("S", (), {})())
        with pytest.raises(ValueError, match="canonical bare"):
            await router.dispatch(name, {})

    @pytest.mark.parametrize("name", [
        "mcp-version",
        "mcp-smart_fetch",
        "mcp-smart-fetch",
        "smart-fetch",
        "smart-fetch-urls",
    ])
    @pytest.mark.asyncio
    async def test_hyphenated_names_rejected(self, name):
        router = CapabilityRouter(type("S", (), {})())
        with pytest.raises(ValueError, match="Unknown tool"):
            await router.dispatch(name, {})

    @pytest.mark.asyncio
    async def test_retired_media_tools_rejected(self):
        for name in ("mcp-media", "mcp-media-download", "media-download"):
            router = CapabilityRouter(type("S", (), {})())
            with pytest.raises(ValueError):
                await router.dispatch(name, {})


# ── CLI legacy flags ─────────────────────────────────────────────────

class TestCLILegacyFlags:
    """Legacy CLI flags must not be present in the current help surface."""

    def test_no_http_flag_in_help(self, monkeypatch, capsys):
        monkeypatch.setattr(sys, "argv", ["sieve", "--help"])
        from sieve import cli
        cli.main()
        out = capsys.readouterr().out
        assert "--http" not in out

    def test_no_http_flag_in_epilog(self):
        """T1: the epilog must not advertise the retired --http flag.

        The pass-1 audit found the defect survived CI because this surface
        was never checked — only the banner was."""
        from sieve.server import _help_epilog
        assert "--http" not in _help_epilog()

    def test_no_mcp_transport_flag_in_main_help(self, monkeypatch, capsys):
        monkeypatch.setattr(sys, "argv", ["sieve", "--help"])
        from sieve import cli
        cli.main()
        out = capsys.readouterr().out
        # The old `--transport` flag was on the main parser; it now lives
        # under `sieve mcp serve --transport`.
        assert "mcp-transport" not in out.lower()

    def test_serve_http_flag_absent(self, monkeypatch, capsys):
        """The old `sieve serve --http` is retired."""
        monkeypatch.setattr(sys, "argv", ["sieve", "--help"])
        from sieve import cli
        cli.main()
        out = capsys.readouterr().out
        assert "serve --http" not in out


# ── CLI positional key input ─────────────────────────────────────────

class TestKeysAddSecureInput:
    """`sieve keys add` must never accept the secret as a positional argv
    value.  Secrets come from a hidden getpass prompt (attached terminal),
    an explicit ``--stdin`` pipe, or an explicit ``--key-fd`` descriptor;
    ambiguous input, EOF and empty secrets are rejected.  Contract after
    #198; positional-secret argv is not preserved (see #199)."""

    FAKE = "FAKE-SECRET-NOT-LIVE-0001"

    def _run(self, monkeypatch, argv):
        monkeypatch.setattr(sys, "argv", argv)
        from sieve import cli
        return cli.main()

    @pytest.fixture()
    def captured_add(self, monkeypatch):
        import sieve.byok_config as byok
        calls = []

        def fake_add(provider, key):
            calls.append((provider, key))

        monkeypatch.setattr(byok, "add_key", fake_add)
        return calls

    def test_help_documents_secure_sources_not_positional_secret(self, monkeypatch, capsys):
        with pytest.raises(SystemExit) as exc_info:
            self._run(monkeypatch, ["sieve", "keys", "add", "--help"])
        assert exc_info.value.code == 0
        out = capsys.readouterr().out
        assert "provider" in out.lower()
        assert "<key>" not in out
        assert "--key-fd" in out
        assert "--stdin" in out

    def test_positional_secret_argv_is_rejected(self, monkeypatch, capsys, captured_add):
        rc = self._run(monkeypatch, ["sieve", "keys", "add", "serper", self.FAKE])
        assert rc == 1
        assert captured_add == []  # secret must never reach add_key
        captured = capsys.readouterr()
        assert self.FAKE not in captured.out
        assert self.FAKE not in captured.err
        assert "not allowed" in captured.out

    def test_prompt_reads_secret_without_echo(self, monkeypatch, capsys, captured_add):
        import getpass
        monkeypatch.setattr(getpass, "getpass", lambda prompt="": self.FAKE)
        # stdin is a TTY: any accidental stdin read returns nothing usable
        monkeypatch.setattr(sys, "stdin", io.StringIO(""))
        monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
        rc = self._run(monkeypatch, ["sieve", "keys", "add", "serper"])
        assert rc in (None, 0)
        assert captured_add == [("serper", self.FAKE)]
        assert self.FAKE not in capsys.readouterr().out

    def test_prompt_eof_is_rejected(self, monkeypatch, capsys, captured_add):
        import getpass

        def raise_eof(prompt=""):
            raise EOFError

        monkeypatch.setattr(getpass, "getpass", raise_eof)
        monkeypatch.setattr(sys, "stdin", io.StringIO(""))
        monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
        rc = self._run(monkeypatch, ["sieve", "keys", "add", "serper"])
        assert rc == 1
        assert captured_add == []

    def test_stdin_flag_reads_piped_secret(self, monkeypatch, capsys, captured_add):
        monkeypatch.setattr(sys, "stdin", io.StringIO(self.FAKE + "\n"))
        rc = self._run(monkeypatch, ["sieve", "keys", "add", "serper", "--stdin"])
        assert rc in (None, 0)
        assert captured_add == [("serper", self.FAKE)]
        assert self.FAKE not in capsys.readouterr().out

    def test_key_fd_reads_secret(self, monkeypatch, capsys, captured_add):
        r, w = os.pipe()
        try:
            os.write(w, (self.FAKE + "\n").encode())
            os.close(w)
            rc = self._run(monkeypatch, ["sieve", "keys", "add", "serper", "--key-fd", str(r)])
            assert rc in (None, 0)
            assert captured_add == [("serper", self.FAKE)]
        finally:
            os.close(r)

    def test_stdin_eof_is_rejected(self, monkeypatch, capsys, captured_add):
        monkeypatch.setattr(sys, "stdin", io.StringIO(""))
        rc = self._run(monkeypatch, ["sieve", "keys", "add", "serper", "--stdin"])
        assert rc == 1
        assert captured_add == []
        assert "EOF" in capsys.readouterr().out

    def test_empty_secret_is_rejected(self, monkeypatch, capsys, captured_add):
        monkeypatch.setattr(sys, "stdin", io.StringIO("\n"))
        rc = self._run(monkeypatch, ["sieve", "keys", "add", "serper", "--stdin"])
        assert rc == 1
        assert captured_add == []
        assert "empty" in capsys.readouterr().out.lower()

    def test_ambiguous_stdin_and_fd_is_rejected(self, monkeypatch, capsys, captured_add):
        monkeypatch.setattr(sys, "stdin", io.StringIO(self.FAKE))
        rc = self._run(monkeypatch, ["sieve", "keys", "add", "serper", "--stdin", "--key-fd", "0"])
        assert rc == 1
        assert captured_add == []
        assert "Ambiguous" in capsys.readouterr().out

    def test_non_tty_without_explicit_source_is_rejected(self, monkeypatch, capsys, captured_add):
        monkeypatch.setattr(sys, "stdin", io.StringIO(""))
        rc = self._run(monkeypatch, ["sieve", "keys", "add", "serper"])
        assert rc == 1
        assert captured_add == []
        assert "--stdin" in capsys.readouterr().out


# ── server_search import boundary ────────────────────────────────────

class TestServerSearchImportBoundary:
    """Search wrappers share cache policy without importing the transport facade."""

    def test_server_search_does_not_import_transport(self):
        import ast, importlib
        mod = importlib.import_module("sieve.server_search")
        source_file = mod.__file__
        tree = ast.parse(open(source_file).read())
        transport_names = {
            "primp", "httpx", "playwright", "patchright",
            "BrowserSession", "DynamicBrowser", "StealthyBrowser",
        }
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                for alias in (node.names or []):
                    assert alias.name not in transport_names, (
                        f"server_search imports transport symbol {alias.name} "
                        f"from {node.module}"
                    )


# ── canonical surface frozen check ───────────────────────────────────

class TestCanonicalSurfaceFrozen:
    """The canonical capability set must match the expected frozen list.
    If you add a new MCP tool, update this test too."""

    def test_canonical_set_matches_expected(self):
        expected = {
            "cache_clear", "datadome_harvest", "detect_block", "extract",
            "extract_jsonl", "profile_identity", "proxy_health",
            "research_ingest", "schema_gen", "screenshot", "session_plan",
            "sitemap_harvest", "sleeper_api", "sleeper_fetch", "smart_crawl",
            "smart_fetch", "smart_search", "version",
        }
        assert CANONICAL_CAPABILITIES == expected

    def test_public_tools_subset_of_canonical(self):
        from sieve.server import _PUBLIC_MCP_TOOLS
        assert _PUBLIC_MCP_TOOLS <= CANONICAL_CAPABILITIES
        assert "sleeper_fetch" not in _PUBLIC_MCP_TOOLS

    def test_all_canonical_have_dispatch_handler(self):
        from sieve.command_router import CAPABILITY_REGISTRY
        assert set(CAPABILITY_REGISTRY) == CANONICAL_CAPABILITIES
        for name, capability in CAPABILITY_REGISTRY.items():
            assert callable(capability.handler), name
            if capability.definition is not None:
                assert capability.definition["name"] == name


# ── architecture report forbidden cycle ──────────────────────────────

class TestForbiddenImportCycles:
    """The architecture report enforces that server_fetch never imports
    server directly.  This test replicates that check without running
    the full script."""

    def test_server_fetch_has_no_server_import(self):
        import ast
        from pathlib import Path
        fetch_file = Path(__file__).resolve().parents[1] / "sieve" / "server_fetch.py"
        tree = ast.parse(fetch_file.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                assert node.module != "sieve.server", (
                    "server_fetch.py must not import from sieve.server"
                )

    def test_server_search_has_no_server_import(self):
        import ast
        from pathlib import Path
        search_file = Path(__file__).resolve().parents[1] / "sieve" / "server_search.py"
        tree = ast.parse(search_file.read_text())
        assert not any(
            isinstance(node, ast.ImportFrom) and node.module == "sieve.server"
            for node in ast.walk(tree)
        )
    def test_architecture_report_has_no_prohibited_edges(self):
        from scripts.architecture_report import build_report, validate

        assert validate(build_report()) == []

    def test_novel_cycle_is_rejected_by_validate(self):
        """Ensure the validator catches a cycle not in the allowlist."""
        from scripts.architecture_report import validate

        fake_report = {
            "imports": {
                "sieve.foo": ["sieve.bar"],
                "sieve.bar": ["sieve.foo"],
            },
            "public": {},
        }
        errors = validate(fake_report)
        assert any("import cycle" in e for e in errors)

    def test_forbidden_edge_is_rejected(self):
        """Ensure forbidden transport imports are caught."""
        from scripts.architecture_report import validate

        fake_report = {
            "imports": {
                "sieve.server_fetch": ["sieve.server"],
                "sieve.server": [],
            },
            "public": {},
        }
        errors = validate(fake_report)
        assert any("forbidden" in e for e in errors)
