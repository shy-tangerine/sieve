"""Staged/atomic Codex plugin installation (#116) and regression suite (#157).

The installer must validate source package and marketplace metadata before
any destination mutation, stage the full tree, and swap atomically with
rollback — stale files from a previous version cannot survive, malformed
marketplace JSON cannot leave partial files, and interrupted replacements
restore the previous tree. All fixtures are offline temp dirs.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from scripts.install_codex_plugin import PLUGIN_NAME, _swap_directory, install

ROOT = Path(__file__).parents[1]


@pytest.fixture()
def plugin_source(tmp_path):
    """A minimal valid plugin package (not the repo's real tree)."""
    source = tmp_path / "repo" / "plugins" / "codex" / PLUGIN_NAME
    manifest = source / ".codex-plugin"
    manifest.mkdir(parents=True)
    (manifest / "plugin.json").write_text(json.dumps({"name": PLUGIN_NAME, "version": "2.0.0"}))
    (source / "skills" / "demo").mkdir(parents=True)
    (source / "skills" / "demo" / "SKILL.md").write_text("# demo v2")
    return tmp_path / "repo"


# ── validation before mutation (#116) ────────────────────────────────

class TestValidationBeforeMutation:
    def test_missing_package_raises_without_touching_destination(self, tmp_path, plugin_source):
        agents = tmp_path / "agents"
        dest = tmp_path / "plugins" / PLUGIN_NAME
        dest.mkdir(parents=True)
        (dest / "old.txt").write_text("previous")
        with pytest.raises(FileNotFoundError):
            install(tmp_path / "empty-repo", agents)
        assert (dest / "old.txt").is_file()
        assert not (agents / "plugins" / "marketplace.json").exists()

    def test_malformed_manifest_raises_before_mutation(self, tmp_path, plugin_source):
        manifest = plugin_source / "plugins" / "codex" / PLUGIN_NAME / ".codex-plugin" / "plugin.json"
        manifest.write_text("{not json")
        agents = tmp_path / "agents"
        dest = tmp_path / "plugins" / PLUGIN_NAME
        dest.mkdir(parents=True)
        (dest / "old.txt").write_text("previous")
        with pytest.raises(json.JSONDecodeError):
            install(plugin_source, agents)
        assert (dest / "old.txt").is_file()

    def test_manifest_name_mismatch_raises(self, tmp_path, plugin_source):
        manifest = plugin_source / "plugins" / "codex" / PLUGIN_NAME / ".codex-plugin" / "plugin.json"
        manifest.write_text(json.dumps({"name": "something-else"}))
        with pytest.raises(ValueError, match="name mismatch"):
            install(plugin_source, tmp_path / "agents")

    def test_malformed_marketplace_json_leaves_no_partial_files(self, tmp_path, plugin_source):
        agents = tmp_path / "agents"
        marketplace = agents / "plugins" / "marketplace.json"
        marketplace.parent.mkdir(parents=True)
        marketplace.write_text("{broken json")
        with pytest.raises(json.JSONDecodeError):
            install(plugin_source, agents)
        # Destination tree untouched, no staging/backup residue.
        assert not (tmp_path / "plugins" / PLUGIN_NAME).exists()
        assert marketplace.read_text() == "{broken json"
        plugins_dir = tmp_path / "plugins"
        residue = [p.name for p in plugins_dir.iterdir()] if plugins_dir.exists() else []
        assert not any(name.startswith(f".{PLUGIN_NAME}") for name in residue)

    def test_malformed_marketplace_shape_rejected(self, tmp_path, plugin_source):
        agents = tmp_path / "agents"
        marketplace = agents / "plugins" / "marketplace.json"
        marketplace.parent.mkdir(parents=True)
        marketplace.write_text(json.dumps({"name": "personal", "plugins": ["not-an-object"]}))
        with pytest.raises(ValueError, match="invalid plugins shape"):
            install(plugin_source, agents)

    def test_wrong_marketplace_name_rejected(self, tmp_path, plugin_source):
        agents = tmp_path / "agents"
        marketplace = agents / "plugins" / "marketplace.json"
        marketplace.parent.mkdir(parents=True)
        marketplace.write_text(json.dumps({"name": "shared-workspace", "plugins": []}))
        with pytest.raises(ValueError, match="unexpected name"):
            install(plugin_source, agents)


# ── stale-file removal and replacement (#157) ────────────────────────

class TestReplacementSemantics:
    def test_stale_files_from_previous_version_are_removed(self, tmp_path, plugin_source):
        agents = tmp_path / "agents"
        dest = tmp_path / "plugins" / PLUGIN_NAME
        dest.mkdir(parents=True)
        (dest / "stale-dir").mkdir()
        (dest / "stale-dir" / "junk.py").write_text("# removed upstream")
        (dest / "stale-file.txt").write_text("old")
        (dest / ".sieve-managed").write_text(PLUGIN_NAME + "\n")

        install(plugin_source, agents)

        assert not (dest / "stale-dir").exists()
        assert not (dest / "stale-file.txt").exists()
        assert (dest / ".codex-plugin" / "plugin.json").is_file()
        assert (dest / "skills" / "demo" / "SKILL.md").read_text() == "# demo v2"

    def test_previous_tree_restored_when_marketplace_write_fails(self, tmp_path, plugin_source, monkeypatch):
        """A failure after the destination swap must not lose the old tree."""
        agents = tmp_path / "agents"
        dest = tmp_path / "plugins" / PLUGIN_NAME
        dest.mkdir(parents=True)
        (dest / "sentinel-old.txt").write_text("previous version")
        (dest / ".sieve-managed").write_text(PLUGIN_NAME + "\n")

        import scripts.install_codex_plugin as ici

        def failing_named(*args, **kwargs):
            raise OSError("disk full")

        monkeypatch.setattr(ici.tempfile, "NamedTemporaryFile", failing_named)
        with pytest.raises(OSError):
            install(plugin_source, agents)
        assert (dest / "sentinel-old.txt").is_file()
        assert not (dest / "skills").exists()
        # No staging/backup residue left behind.
        assert not any(p.name.startswith(f".{PLUGIN_NAME}") for p in (tmp_path / "plugins").iterdir())

    def test_symlink_destination_refused(self, tmp_path, plugin_source):
        agents = tmp_path / "agents"
        agents.mkdir(parents=True)
        target = tmp_path / "outside"
        target.mkdir()
        dest = tmp_path / "plugins" / PLUGIN_NAME
        dest.parent.mkdir(parents=True)
        dest.symlink_to(target)
        with pytest.raises(ValueError, match="symlink"):
            install(plugin_source, agents)

    def test_unrelated_marketplace_entries_preserved(self, tmp_path, plugin_source):
        agents = tmp_path / "agents"
        marketplace = agents / "plugins" / "marketplace.json"
        marketplace.parent.mkdir(parents=True)
        marketplace.write_text(json.dumps({
            "name": "personal",
            "plugins": [{"name": "existing", "source": {"source": "local", "path": "./plugins/existing"}}],
        }))
        install(plugin_source, agents)
        data = json.loads(marketplace.read_text())
        names = [item["name"] for item in data["plugins"]]
        assert names == ["existing", PLUGIN_NAME]

    def test_reinstall_replaces_older_copy(self, tmp_path, plugin_source):
        agents = tmp_path / "agents"
        install(plugin_source, agents)
        dest = tmp_path / "plugins" / PLUGIN_NAME
        (dest / "skills" / "demo" / "SKILL.md").write_text("# demo v2.0.1")
        install(plugin_source, agents)
        assert (dest / "skills" / "demo" / "SKILL.md").read_text() == "# demo v2"


# ── swap primitive (#116) ────────────────────────────────────────────

class TestSwapDirectory:
    def test_old_tree_restored_on_failed_swap(self, tmp_path):
        dest = tmp_path / "dest"
        dest.mkdir()
        (dest / "old.txt").write_text("old")
        staging = tmp_path / "staging"
        staging.mkdir()
        (staging / "new.txt").write_text("new")

        import scripts.install_codex_plugin as ici

        real_replace = os.replace

        def failing_replace(a, b):
            # Fail only the staging swap-in; the backup restore must succeed.
            if str(b) == str(dest) and str(a) == str(staging):
                raise OSError("swap failed")
            return real_replace(a, b)

        monkey = pytest.MonkeyPatch()
        try:
            # The swap helper resolves os.replace from its own module's import.
            monkey.setattr(ici.os, "replace", failing_replace)
            with pytest.raises(OSError):
                ici._swap_directory(staging, dest)
        finally:
            monkey.undo()
        assert (dest / "old.txt").is_file()
        assert not (dest / "new.txt").exists()
def test_unmanaged_destination_requires_explicit_overwrite(tmp_path, plugin_source):
    agents = tmp_path / "agents"
    dest = tmp_path / "plugins" / PLUGIN_NAME
    dest.mkdir(parents=True)
    (dest / "user.txt").write_text("keep me")
    for dry_run in (False, True):
        with pytest.raises(ValueError, match="unmanaged"):
            install(plugin_source, agents, dry_run=dry_run)
    assert (dest / "user.txt").read_text() == "keep me"
    assert not (agents / "plugins" / "marketplace.json").exists()
    (dest / ".sieve-managed").write_text("another-product\n")
    with pytest.raises(ValueError, match="unmanaged"):
        install(plugin_source, agents)
    assert (dest / "user.txt").read_text() == "keep me"
    install(plugin_source, agents, overwrite=True)
    assert not (dest / "user.txt").exists()
    assert (dest / ".sieve-managed").read_text() == PLUGIN_NAME + "\n"
    install(plugin_source, agents)


def test_backup_survives_failed_marketplace_promotion_and_restore(tmp_path, monkeypatch):
    dest = tmp_path / "plugin"
    dest.mkdir()
    (dest / "old.txt").write_text("recover me")
    staging = tmp_path / "staging"
    staging.mkdir()
    real_replace = os.replace

    def replace(source, target):
        if str(target) == str(dest) and "backup" in str(source):
            raise OSError("restore failed")
        return real_replace(source, target)

    def promote():
        raise OSError("promotion failed")

    monkeypatch.setattr("scripts.install_codex_plugin.os.replace", replace)
    with pytest.raises(OSError, match="restore failed"):
        _swap_directory(staging, dest, post_swap=promote)
    backups = list(tmp_path.glob(".plugin.backup-*"))
    assert len(backups) == 1
    assert (backups[0] / "old.txt").read_text() == "recover me"
