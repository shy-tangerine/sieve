"""Backup-and-rollback skill install swap (#41).

install_skills stages each skill and verifies it, then swaps via
``_swap_skill_directory``: the previous tree is moved aside and only removed
after the new tree is in place, so an interruption can no longer leave a
destination with no installed skill. Offline temp-dir fixtures only.
"""
from __future__ import annotations

import os
import shutil
import sys

import pytest

from sieve.setup import _swap_skill_directory, install_skills


# ── swap primitive (#41) ─────────────────────────────────────────────

class TestSwapSkillDirectory:
    def test_replaces_existing_tree(self, tmp_path):
        dest = tmp_path / "skill"
        (dest / "nested").mkdir(parents=True)
        (dest / "nested" / "old.md").write_text("old")
        staging = tmp_path / ".skill.tmp"
        (staging / "v2").mkdir(parents=True)
        (staging / "v2" / "new.md").write_text("new")

        _swap_skill_directory(staging, dest)

        assert (dest / "v2" / "new.md").is_file()
        assert not (dest / "nested").exists()
        assert not staging.exists()
        # No backup residue.
        assert not any(p.name.startswith(".skill.backup") for p in tmp_path.iterdir())

    def test_previous_tree_restored_when_swap_in_fails(self, tmp_path, monkeypatch):
        dest = tmp_path / "skill"
        dest.mkdir()
        (dest / "old.md").write_text("old")
        staging = tmp_path / ".skill.tmp"
        staging.mkdir()
        (staging / "new.md").write_text("new")

        real_replace = os.replace

        def failing_replace(a, b):
            if str(b) == str(dest) and str(a) == str(staging):
                raise OSError("swap failed")
            return real_replace(a, b)

        monkeypatch.setattr("sieve.setup.os.replace", failing_replace)
        with pytest.raises(OSError):
            _swap_skill_directory(staging, dest)

        assert (dest / "old.md").is_file()
        assert not (dest / "new.md").exists()

    def test_backup_survives_when_restore_itself_fails(self, tmp_path, monkeypatch):
        """If the rollback also fails, the backup must remain for recovery."""
        dest = tmp_path / "skill"
        dest.mkdir()
        (dest / "old.md").write_text("old")
        staging = tmp_path / ".skill.tmp"
        staging.mkdir()
        (staging / "new.md").write_text("new")

        real_replace = os.replace

        def failing_replace(a, b):
            if str(b) == str(dest):
                raise OSError("swap failed")
            return real_replace(a, b)

        monkeypatch.setattr("sieve.setup.os.replace", failing_replace)
        with pytest.raises(OSError):
            _swap_skill_directory(staging, dest)

        backups = [p for p in tmp_path.iterdir() if p.name.startswith(".skill.backup")]
        assert len(backups) == 1
        assert (backups[0] / "old.md").is_file()

    def test_symlink_destination_is_replaced_not_written_through(self, tmp_path):
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "linked.md").write_text("linked")
        dest = tmp_path / "skill"
        dest.symlink_to(outside)
        staging = tmp_path / ".skill.tmp"
        staging.mkdir()
        (staging / "SKILL.md").write_text("new")

        _swap_skill_directory(staging, dest)

        assert (dest / "SKILL.md").is_file()
        assert not (dest / "linked.md").exists()
        # The symlink target itself was untouched.
        assert (outside / "linked.md").is_file()


# ── end-to-end install_skills with a fake source tree (#41) ──────────

class TestInstallSkillsRollback:
    @pytest.fixture()
    def fake_source(self, monkeypatch, tmp_path):
        import sieve.setup as setup_mod

        source = tmp_path / "skills-src" / "sieve"
        (source / "sieve").mkdir(parents=True)
        (source / "sieve" / "SKILL.md").write_text("# skill v2")
        (source / "sieve-setup").mkdir()
        (source / "sieve-setup" / "SKILL.md").write_text("# setup v2")
        monkeypatch.setattr(setup_mod, "_skill_source_dir", lambda: source)
        return source

    def test_reinstall_replaces_previous_version(self, tmp_path, fake_source):
        dest = tmp_path / "agent-skills"
        target = dest / "sieve"
        target.mkdir(parents=True)
        (target / "SKILL.md").write_text("# skill v1")
        (target / "stale.txt").write_text("old junk")
        (target / ".sieve-managed").write_text("sieve\n")

        rc = install_skills(str(dest))

        assert rc == 0
        assert (target / "SKILL.md").read_text() == "# skill v2"
        assert not (target / "stale.txt").exists()
        assert (dest / "sieve-setup" / "SKILL.md").read_text() == "# setup v2"
        # No staging/backup residue anywhere in the destination tree.
        residue = [p.name for p in dest.iterdir() if p.name.startswith(".")]
        assert residue == []

    def test_failed_staging_propagates_and_keeps_swapped_first_skill(self, tmp_path, fake_source, monkeypatch):
        dest = tmp_path / "agent-skills"
        target = dest / "sieve"
        target.mkdir(parents=True)
        (target / "SKILL.md").write_text("# skill v1")
        (target / ".sieve-managed").write_text("sieve\n")

        import sieve.setup as setup_mod

        real_copytree = shutil.copytree

        def failing_copytree(src, dst, **kwargs):
            # Fail only the second skill's staging, after the first swapped.
            if "sieve-setup" in str(src):
                raise OSError("stage failed")
            return real_copytree(src, dst, **kwargs)

        monkeypatch.setattr(setup_mod.shutil, "copytree", failing_copytree)
        with pytest.raises(OSError, match="stage failed"):
            install_skills(str(dest))

        # The already-swapped first skill stays new; the failed skill's
        # previous tree was never touched (staging happens before the swap).
        assert (target / "SKILL.md").read_text() == "# skill v2"
        assert not (dest / "sieve-setup").exists()
        assert not any(p.name.startswith(".") for p in dest.iterdir())

    def test_setup_cli_end_to_end_still_installs(self, tmp_path, monkeypatch):
        """Regression: the packaged `sieve skill install` path still works."""
        import sieve.setup as setup_mod

        source = tmp_path / "skills-src" / "sieve"
        (source / "sieve").mkdir(parents=True)
        (source / "sieve" / "SKILL.md").write_text("# skill")
        monkeypatch.setattr(setup_mod, "_skill_source_dir", lambda: source)
        monkeypatch.setattr(sys, "argv", ["sieve", "skill", "install", "--dir", str(tmp_path / "out")])

        from sieve import cli

        assert cli.main() == 0
        assert (tmp_path / "out" / "sieve" / "SKILL.md").is_file()
def test_unmanaged_skill_is_preserved_until_explicit_overwrite(tmp_path, monkeypatch, capsys):
    import sieve.setup as setup_mod

    source = tmp_path / "source"
    for name in ("sieve", "sieve-setup"):
        (source / name).mkdir(parents=True)
        (source / name / "SKILL.md").write_text("new skill")
    monkeypatch.setattr(setup_mod, "_skill_source_dir", lambda: source)
    dest = tmp_path / "agent-skills"
    target = dest / "sieve-setup"
    target.mkdir(parents=True)
    (target / "SKILL.md").write_text("my skill")
    assert install_skills(str(dest)) == 1
    assert "--overwrite" in capsys.readouterr().err
    assert (target / "SKILL.md").read_text() == "my skill"
    assert not (dest / "sieve").exists()
    (target / ".sieve-managed").write_text("another-skill\n")
    assert install_skills(str(dest)) == 1
    assert (target / "SKILL.md").read_text() == "my skill"
    assert setup_mod.run_skill_command(["install", "--dir", str(dest), "--overwrite"]) == 0
    assert (target / ".sieve-managed").read_text() == "sieve-setup\n"
    assert install_skills(str(dest)) == 0
