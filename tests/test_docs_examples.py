"""Offline smoke checks for the actual README CLI examples."""

from __future__ import annotations

import re
import argparse
import shlex
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
README = ROOT / "README.md"
START = "<!-- offline-cli-smoke:start -->"
END = "<!-- offline-cli-smoke:end -->"
RETIRED_EXAMPLES = ("keys add <provider> <key>", "--http", "sieve update -u <version>")


def _commands() -> list[str]:
    text = README.read_text(encoding="utf-8")
    start = text.index(START) + len(START)
    end = text.index(END, start)
    return [line.strip() for line in text[start:end].splitlines() if line.strip().startswith("sieve ")]


def test_readme_quickstart_examples_and_flags_appear_in_offline_help():
    result = subprocess.run(
        [sys.executable, "-m", "sieve", "--help"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert _commands()
    for example in _commands():
        tokens = shlex.split(example)
        assert len(tokens) >= 2 and tokens[0] == "sieve"
        assert " ".join(tokens[:2]) in result.stdout, example
        for flag in (token for token in tokens if token.startswith("--")):
            assert flag in result.stdout, f"{example}: {flag} is absent from `sieve --help`"


def test_retired_cli_examples_are_absent_or_explicitly_negative():
    markdown = [ROOT / "README.md", *ROOT.joinpath("docs").rglob("*.md"), *ROOT.joinpath("plugins").rglob("*.md")]
    for path in markdown:
        lines = path.read_text(encoding="utf-8").splitlines()
        for index, line in enumerate(lines):
            for retired in RETIRED_EXAMPLES:
                if retired not in line:
                    continue
                context = " ".join(lines[max(0, index - 1):index + 2]).lower()
                assert re.search(r"\b(retired|rejected|unsupported|no longer supported)\b", context), (
                    f"{path.relative_to(ROOT)}:{index + 1}: stale alias is not labeled as a negative example")


@pytest.mark.parametrize("example", _commands())
def test_readme_examples_pass_real_parser_without_execution(monkeypatch, example):
    from sieve.server import main

    class ParsedOnly(Exception):
        pass

    parse = argparse.ArgumentParser.parse_args
    parsed = []

    def parse_only(parser, *args, **kwargs):
        parsed.append(parse(parser, *args, **kwargs))
        raise ParsedOnly

    monkeypatch.setattr(sys, "argv", shlex.split(example))
    monkeypatch.setattr(argparse.ArgumentParser, "parse_args", parse_only)
    with pytest.raises(ParsedOnly):
        main()
    assert len(parsed) == 1
