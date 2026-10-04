import json
import errno
import subprocess
import sys
from pathlib import Path

import pytest

from scripts import report_output


@pytest.mark.parametrize("script", ["architecture_report.py", "bench_v2.py"])
def test_report_cli_replaces_symlink_without_touching_target(tmp_path, script):
    target = tmp_path / "original.json"
    target.write_text("preserve me", encoding="utf-8")
    output = tmp_path / ("bench-v2.jsonl" if script == "bench_v2.py" else "report.json")
    try:
        output.symlink_to(target)
    except OSError as exc:
        if exc.errno in {errno.EPERM, errno.EACCES, errno.ENOSYS, errno.EOPNOTSUPP}:
            pytest.skip("host does not permit symlink creation")
        raise
    root = Path(__file__).resolve().parents[1]
    options = ["--output-dir", str(tmp_path), "--runs", "1"] if script == "bench_v2.py" else ["--output", str(output)]
    result = subprocess.run(
        [sys.executable, str(root / "scripts" / script), *options],
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert target.read_text(encoding="utf-8") == "preserve me"
    assert not output.is_symlink()
    content = output.read_text(encoding="utf-8")
    records = [json.loads(line) for line in content.splitlines()] if script == "bench_v2.py" else [json.loads(content)]
    assert all(records)
    assert not list(tmp_path.glob(".*.tmp"))


@pytest.mark.parametrize("failure_point", ["fsync", "replace"])
def test_report_failure_preserves_old_output_and_cleans_temp(tmp_path, monkeypatch, failure_point):
    output = tmp_path / "report.json"
    output.write_text("old complete report", encoding="utf-8")

    def fail(*args):
        raise OSError("simulated write failure")

    monkeypatch.setattr(report_output.os, failure_point, fail)
    with pytest.raises(OSError, match="simulated"):
        report_output.write_report(output, "new report")
    assert output.read_text(encoding="utf-8") == "old complete report"
    assert list(tmp_path.iterdir()) == [output]


def test_report_encoding_failure_preserves_output(tmp_path):
    output = tmp_path / "report.json"
    output.write_text("old report", encoding="utf-8")
    with pytest.raises(UnicodeEncodeError):
        report_output.write_report(output, "invalid surrogate: \ud800")
    assert output.read_text(encoding="utf-8") == "old report"
    assert list(tmp_path.iterdir()) == [output]
