from scripts import check_public_tree

import pytest


def test_public_tree_rejects_private_development_paths():
    assert check_public_tree.violations([
        "README.md",
        "wiki/research/note.md",
        ".repowise/state.json",
        ".sieve/workflow.json",
        "skills/sieve-development/SKILL.md",
        "docs/agent-development.md",
        "scripts/export_public.py",
        ".github/workflows/upstream-watch.yml",
    ]) == [
        ".github/workflows/upstream-watch.yml",
        ".repowise/state.json",
        ".sieve/workflow.json",
        "docs/agent-development.md",
        "scripts/export_public.py",
        "skills/sieve-development/SKILL.md",
        "wiki/research/note.md",
    ]


@pytest.mark.parametrize("path", [
    ".freebuff/state.json", "scripts/hermes/run.sh", ".config/systemd/sieve.service",
    "skills/local/SKILL.md", "sieve.service",
])
def test_public_tree_rejects_operator_service_and_platform_paths(path):
    assert check_public_tree.violations([path, "sieve/server.py"]) == [path]


def test_artifact_boundary_rejects_untracked_contamination(tmp_path, capsys):
    (tmp_path / "README.md").write_text("Public documentation")
    assert check_public_tree.main(["--root", str(tmp_path)]) == 0
    (tmp_path / "AGENTS.md").write_text("Private agent instructions")
    assert check_public_tree.main(["--root", str(tmp_path)]) == 1
    assert "AGENTS.md" in capsys.readouterr().err


def test_artifact_boundary_rejects_git_metadata(tmp_path):
    (tmp_path / ".git").mkdir()
    assert check_public_tree.main(["--root", str(tmp_path)]) == 1
