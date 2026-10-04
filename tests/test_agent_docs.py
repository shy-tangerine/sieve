from pathlib import Path


ROOT = Path(__file__).parents[1]


def test_agent_guidance_matches_canonical_cli_surface():
    paths = [
        ROOT / "docs" / "agents.md",
        ROOT / "docs" / "mcp.md",
        ROOT / "plugins" / "codex" / "sieve-web-research" / "skills" / "sieve-web-research" / "SKILL.md",
        ROOT / "plugins" / "claude" / "sieve-web" / "skills" / "sieve-web" / "SKILL.md",
        ROOT / "sieve" / "_skills" / "sieve" / "SKILL.md",
        ROOT / "sieve" / "_skills" / "sieve-setup" / "SKILL.md",
    ]
    text = "\n".join(path.read_text() for path in paths)
    assert "sieve mcp serve" in text
    assert "sieve mcp --transport" not in text
    assert "mcp-media" not in text
    assert "--allow-osint" in text
    assert "sieve update check --json" in text
    assert "content_ok" in text



def test_agent_guidance_never_puts_provider_keys_in_argv():
    paths = [
        ROOT / "sieve" / "_skills" / "sieve" / "SKILL.md",
        ROOT / "sieve" / "_skills" / "sieve-setup" / "SKILL.md",
        ROOT / "plugins" / "codex" / "sieve-web-research" / "skills" / "sieve-web-research" / "SKILL.md",
        ROOT / "plugins" / "claude" / "sieve-web" / "skills" / "sieve-web" / "SKILL.md",
    ]
    text = "\n".join(path.read_text() for path in paths)
    assert "keys add <provider> <key>" not in text
    assert "command-line argument" in text
