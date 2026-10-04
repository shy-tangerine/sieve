import json
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).parents[1]
PLUGIN = ROOT / "plugins" / "claude" / "sieve-web"
MARKETPLACE = ROOT / "plugins" / "claude" / ".claude-plugin" / "marketplace.json"


def test_claude_plugin_contract_and_session_hint():
    manifest = json.loads((PLUGIN / ".claude-plugin" / "plugin.json").read_text())
    hooks = json.loads((PLUGIN / "hooks" / "hooks.json").read_text())

    assert manifest["name"] == "sieve-web"
    assert manifest["hooks"] == "./hooks/hooks.json"
    handler = hooks["hooks"]["SessionStart"][0]["hooks"][0]
    assert handler["type"] == "command"
    assert "${CLAUDE_PLUGIN_ROOT}" in handler["command"]
    assert not (PLUGIN / ".mcp.json").exists()

    result = subprocess.run(
        [sys.executable, str(PLUGIN / "hooks" / "session-start.py")],
        input='{"hook_event_name":"SessionStart","source":"startup"}\n',
        text=True,
        capture_output=True,
        check=True,
    )
    assert "Sieve is the default web interface" in result.stdout
    assert "Honor an explicit request for another tool" in result.stdout
    assert result.stderr == ""


def test_claude_marketplace_points_to_a_complete_local_plugin():
    marketplace = json.loads(MARKETPLACE.read_text())

    assert marketplace["name"] == "sieve"
    entry = next(item for item in marketplace["plugins"] if item["name"] == "sieve-web")
    assert entry["source"] == "./sieve-web"
    assert (MARKETPLACE.parent.parent / entry["source"] / ".claude-plugin" / "plugin.json").is_file()
