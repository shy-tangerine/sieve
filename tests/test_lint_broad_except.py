"""Issue #234: broad-except lint policy (scripts/lint_broad_except.py)."""

import ast
import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "lint_broad_except.py"

# Import the script as a module (it's not a package).
import importlib.util

spec = importlib.util.spec_from_file_location("lint_broad_except", SCRIPT)
lint = importlib.util.module_from_spec(spec)
spec.loader.exec_module(lint)


def _check_snippet(tmp_path: Path, code: str) -> list[str]:
    target = tmp_path / "sieve" / "sample.py"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(code)
    return lint.check_file(target, rel="sieve/sample.py")


def test_narrow_except_is_allowed(tmp_path):
    code = "try:\n    pass\nexcept (OSError, ValueError):\n    pass\n"
    assert _check_snippet(tmp_path, code) == []


def test_broad_except_without_rationale_flagged(tmp_path):
    code = "try:\n    pass\nexcept Exception:\n    pass\n"
    violations = _check_snippet(tmp_path, code)
    assert len(violations) == 1
    assert "except Exception" in violations[0]


def test_broad_except_with_rationale_allowed(tmp_path):
    code = (
        "try:\n    pass\nexcept Exception:\n"
        "    # fallback boundary: third-party parser swallows malformed HTML\n"
        "    pass\n"
    )
    assert _check_snippet(tmp_path, code) == []


def test_existing_module_does_not_approve_new_broad_catch(tmp_path):
    code = "try:\n    pass\nexcept Exception:\n    pass\n"
    # Approved modules are matched by repo-relative path; simulate one.
    target = tmp_path / "sieve" / "fetcher.py"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(code)
    violations = lint.check_file(target, rel="sieve/fetcher.py")
    assert len(violations) == 1


def test_string_literal_cannot_supply_rationale(tmp_path):
    assert _check_snippet(tmp_path, 'try:\n    pass\nexcept Exception:\n    print("fallback boundary")\n')


def test_replacement_catch_has_different_baseline_key(tmp_path):
    first = _check_snippet(tmp_path, 'try:\n    first()\nexcept Exception:\n    pass\n')[0]
    second = _check_snippet(tmp_path, 'try:\n    second()\nexcept Exception:\n    pass\n')[0]
    assert lint._violation_key(first) != lint._violation_key(second)


def test_ratchet_gate_passes_on_current_tree():
    """The committed baseline must be current: no NEW violations allowed."""
    result = subprocess.run(
        [sys.executable, str(SCRIPT)], capture_output=True, text=True
    )
    assert result.returncode == 0, result.stdout + result.stderr
