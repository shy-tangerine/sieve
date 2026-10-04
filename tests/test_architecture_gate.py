"""Exact architecture contracts. Regenerate snapshots explicitly for review."""
import json
import subprocess
import sys
from pathlib import Path

from scripts.architecture_report import build_report, compare_snapshot, validate

ROOT = Path(__file__).resolve().parents[1]


def test_architecture_report_check_passes():
    result = subprocess.run([sys.executable, str(ROOT / "scripts/architecture_report.py"), "--check"],
                            capture_output=True, text=True, cwd=ROOT)
    assert result.returncode == 0, result.stdout + result.stderr


def test_primitive_inventory_check_passes():
    result = subprocess.run([sys.executable, str(ROOT / "scripts/primitive_inventory.py"), "--check"],
                            capture_output=True, text=True, cwd=ROOT)
    assert result.returncode == 0, result.stdout + result.stderr


def test_exact_architecture_snapshot():
    baseline = json.loads((ROOT / "scripts/architecture_snapshot.json").read_text())
    errors = compare_snapshot(build_report(), baseline)
    assert not errors, "\n".join(errors) + "\nRegenerate deliberately: python scripts/architecture_report.py --output scripts/architecture_snapshot.json"


def test_transport_edges_and_cycles():
    assert not validate(build_report())


def test_no_private_metadata_in_exports():
    allowed = {("sieve.security", "redact_secret"), ("sieve.public_output", "redact_secrets")}
    violations = [(module, name) for module, names in build_report()["public"].items() for name in names
                  if (module, name) not in allowed and any(p in name.lower() for p in ("upstream", "private", "internal_", "_dev", "secret"))]
    assert not violations
