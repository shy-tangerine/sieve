"""Exact site keys reject new debt even when a rule's total stays unchanged."""
from collections import Counter
import json
from scripts import lint_contract


def test_lint_gate_rejects_same_count_replacement(monkeypatch, tmp_path, capsys):
    original = json.dumps(["sieve/example.py", "F821", "Undefined name", "old_name()"])
    replacement = json.dumps(["sieve/example.py", "F821", "Undefined name", "new_name()"])
    baseline = tmp_path / "baseline.json"
    baseline.write_text(json.dumps({original: 1}))
    monkeypatch.setattr(lint_contract, "BASELINE", baseline)
    monkeypatch.setattr(lint_contract, "findings", lambda: Counter({replacement: 1}))
    assert lint_contract.main() == 1
    assert "new_name()" in capsys.readouterr().out


def test_lint_gate_accepts_removed_debt(monkeypatch, tmp_path):
    baseline = tmp_path / "baseline.json"
    baseline.write_text(json.dumps({"removed legacy site": 1}))
    monkeypatch.setattr(lint_contract, "BASELINE", baseline)
    monkeypatch.setattr(lint_contract, "findings", Counter)
    assert lint_contract.main() == 0
