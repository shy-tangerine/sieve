"""README and plugin capability claims follow public contracts."""

from __future__ import annotations

import ast
import json
import tomllib
from pathlib import Path

import pytest

from scripts import generate_docs_capabilities
from sieve.command_router import CAPABILITY_REGISTRY

ROOT = Path(__file__).resolve().parents[1]
README = ROOT / "README.md"
PLUGIN_READMES = (
    ROOT / "plugins/codex/sieve-web-research/README.md",
    ROOT / "plugins/claude/sieve-web/README.md",
)
PLUGIN_SKILLS = (
    ROOT / "plugins/codex/sieve-web-research/skills/sieve-web-research/SKILL.md",
    ROOT / "plugins/claude/sieve-web/skills/sieve-web/SKILL.md",
)


def _extras() -> set[str]:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    return set(project["project"]["optional-dependencies"])


def _test_reference_exists(reference: str) -> bool:
    path, *names = reference.split("::")
    parent = ast.parse((ROOT / path).read_text(encoding="utf-8"))
    for name in names:
        node = next(
            (item for item in ast.walk(parent)
             if isinstance(item, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)) and item.name == name),
            None,
        )
        if node is None:
            return False
        parent = node
    return True


def test_readme_table_and_definition_manifest_match_public_registry():
    readme = README.read_text(encoding="utf-8")
    assert generate_docs_capabilities.replace_block(
        readme, generate_docs_capabilities.render_block()
    ) == readme, "README capability table is stale; run scripts/generate_docs_capabilities.py --write"
    expected = json.loads(generate_docs_capabilities.render_manifest())
    actual = json.loads((ROOT / "docs/capability-manifest.json").read_text(encoding="utf-8"))
    if actual != expected:
        changed = sorted(set(actual["tools"]) | set(expected["tools"]))
        changed = [name for name in changed if actual["tools"].get(name) != expected["tools"].get(name)]
        pytest.fail(f"public definitions changed for {changed}; review and regenerate the manifest")


def test_registry_required_extras_are_declared():
    available = _extras()
    for name, capability in CAPABILITY_REGISTRY.items():
        assert callable(capability.handler), name
        assert set(capability.extras) <= available, name
        if capability.definition is not None:
            assert capability.definition.get("name") == name


def test_nongenerated_readme_claims_reference_live_contract_tests():
    readme = README.read_text(encoding="utf-8")
    claims = {
        "MCP over local stdio or authenticated streamable HTTP":
            "tests/test_surface_contracts.py::TestCapabilityRouterInvariants::test_canonical_set_matches_public_tools",
        "Explicit update checks, release-note summaries, rollback":
            "tests/test_update_command.py::test_update_rolls_back_when_verification_fails",
        "The update command asks for confirmation":
            "tests/test_update_command.py::test_cli_update_apply_forwards_confirmation",
    }
    for claim, reference in claims.items():
        assert claim in readme and f"<!-- contract-test: {reference} -->" in readme
        assert _test_reference_exists(reference), reference
    for reference in ("tests/test_mcp_http_wire.py::TestBearerWire::test_missing_authorization_is_401",
                      "tests/test_mcp_http_wire.py::test_tool_result_redacts_both_copies_and_bounds_wire"):
        assert f"<!-- contract-test: {reference} -->" in readme
        assert _test_reference_exists(reference)


@pytest.fixture
def core_install_extras() -> frozenset[str]:
    return frozenset()


def test_plugin_screenshot_guidance_matches_required_extra_and_core_fallback(core_install_extras):
    screenshot = CAPABILITY_REGISTRY["screenshot"]
    assert screenshot.extras and set(screenshot.extras) - core_install_extras
    browser_extra = screenshot.extras[0]
    assert browser_extra in _extras()
    for path in PLUGIN_READMES:
        text = " ".join(path.read_text(encoding="utf-8").lower().split())
        assert "when its optional browser extra is installed" in text
        assert f"sieve-cli[{browser_extra}]" in text
        assert "when the extra is absent" in text
        assert "explain the missing capability" in text and "fallback" in text
    for path in PLUGIN_SKILLS:
        text = " ".join(path.read_text(encoding="utf-8").lower().split())
        assert "requires the optional" in text and "if it is unavailable" in text
        assert "fallback" in text
