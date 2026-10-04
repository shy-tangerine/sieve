import json
from pathlib import Path

from scripts.install_codex_plugin import install


ROOT = Path(__file__).parents[1]


def test_install_codex_plugin_copies_package_and_preserves_marketplace(tmp_path):
    agents = tmp_path / "agents"
    marketplace = agents / "plugins" / "marketplace.json"
    marketplace.parent.mkdir(parents=True)
    marketplace.write_text(
        json.dumps(
            {
                "name": "personal",
                "interface": {"displayName": "Personal"},
                "plugins": [{"name": "existing", "source": {"source": "local", "path": "./plugins/existing"}}],
            }
        )
    )

    destination, result_path = install(ROOT, agents)

    assert destination == tmp_path / "plugins" / "sieve-web-research"
    assert result_path == marketplace
    assert (destination / ".codex-plugin" / "plugin.json").is_file()
    data = json.loads(marketplace.read_text())
    assert [item["name"] for item in data["plugins"]] == ["existing", "sieve-web-research"]
    assert data["plugins"][-1]["source"]["path"] == "./plugins/sieve-web-research"


def test_install_codex_plugin_dry_run_does_not_write(tmp_path):
    destination, marketplace = install(ROOT, tmp_path / "agents", dry_run=True)

    assert destination == tmp_path / "plugins" / "sieve-web-research"
    assert marketplace == tmp_path / "agents" / "plugins" / "marketplace.json"
    assert not destination.exists()
    assert not marketplace.exists()
