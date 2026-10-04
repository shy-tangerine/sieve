from pathlib import Path
from copy import deepcopy

import pytest

from scripts.architecture_report import ALLOWED_CYCLES, build_report, compare_snapshot, validate


@pytest.mark.parametrize("kind", ["module", "import", "export"])
def test_snapshot_rejects_same_count_replacement(kind):
    baseline = {"schema_version": 1, "imports": {"sieve.a": ["sieve.b"], "sieve.b": []},
                "public": {"sieve.a": ["old"], "sieve.b": []}}
    current = deepcopy(baseline)
    if kind == "module":
        current["imports"]["sieve.c"] = current["imports"].pop("sieve.b")
    elif kind == "import":
        current["imports"]["sieve.a"] = ["sieve.c"]
    else:
        current["public"]["sieve.a"] = ["new"]
    errors = compare_snapshot(current, baseline)
    assert errors and "added=" in errors[0] and "removed=" in errors[0]


def test_snapshot_ignores_order_only():
    baseline = {"schema_version": 1, "imports": {"sieve.a": ["sieve.b", "sieve.c"]},
                "public": {"sieve.a": ["one", "two"]}}
    reordered = {"schema_version": 1, "imports": {"sieve.a": ["sieve.c", "sieve.b"]},
                 "public": {"sieve.a": ["two", "one"]}}
    assert compare_snapshot(reordered, baseline) == []


def test_report_supports_fixture_root(tmp_path):
    package = tmp_path / "sieve"
    package.mkdir()
    (package / "sample.py").write_text("def visible(): pass\n")
    assert build_report(tmp_path)["public"] == {"sieve.sample": ["visible"]}


def test_server_fetch_has_no_transport_import_or_cycle():
    report = build_report(Path(__file__).parents[1])
    assert "sieve.server" not in report["imports"]["sieve.server_fetch"]
    assert not [e for e in validate(report) if "server_fetch" in e]


def test_report_freezes_public_symbols():
    report = build_report(Path(__file__).parents[1])
    assert "sieve.cli" in report["public"]
    assert "main" in report["public"]["sieve.cli"]


def test_validate_rejects_unallowlisted_import_cycle():
    report = {"imports": {"sieve.a": ["sieve.b"], "sieve.b": ["sieve.a"]}}
    assert validate(report) == ["import cycle: sieve.a -> sieve.b -> sieve.a"]


def test_validate_rejects_unallowlisted_self_cycle():
    report = {"imports": {"sieve.a": ["sieve.a"]}}
    assert validate(report) == ["import cycle: sieve.a -> sieve.a"]


def test_validate_cycle_diagnostic_is_stable_for_reordered_graphs():
    first = {
        "imports": {
            "sieve.a": ["sieve.b"],
            "sieve.b": ["sieve.c"],
            "sieve.c": ["sieve.a"],
        }
    }
    reordered = {
        "imports": {
            "sieve.c": ["sieve.a"],
            "sieve.a": ["sieve.b"],
            "sieve.b": ["sieve.c"],
        }
    }

    expected = ["import cycle: sieve.a -> sieve.b -> sieve.c -> sieve.a"]
    assert validate(first) == expected
    assert validate(reordered) == expected


def test_allowed_cycles_have_required_documentation():
    assert ALLOWED_CYCLES
    for cycle in ALLOWED_CYCLES:
        assert cycle.modules
        assert cycle.rationale
        assert cycle.owner
        assert cycle.revisit_condition
        assert cycle.issue == 57
        assert cycle.reviewed_version
        assert cycle.expires_version


def test_cycle_waivers_expire_and_removed_cycles_are_stale():
    from scripts.architecture_report import AllowedCycle
    waiver = AllowedCycle(frozenset({"sieve.a", "sieve.b"}), "temporary seam", "maintainer", "remove seam")
    report = {"imports": {"sieve.a": ["sieve.b"], "sieve.b": ["sieve.a"]}}
    assert validate(report, version="13.2.1", allowed_cycles=[waiver]) == []
    assert any("expired" in error for error in validate(report, version="13.3.0", allowed_cycles=[waiver]))
    report["imports"]["sieve.b"] = []
    assert any("stale" in error for error in validate(report, version="13.2.1", allowed_cycles=[waiver]))
