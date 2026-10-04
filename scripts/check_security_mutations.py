"""Opt-in offline guard-removal calibration in disposable source copies."""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
# Each exact replacement removes one guard; ambiguity fails before testing.
SEEDS = {
    "url": ("sieve/security.py", "if _is_forbidden_ip(addr):", "if False:",
            "tests/test_security.py::test_validate_url_blocks_embedded_private_ipv6"),
    "robots": ("sieve/robots.py", "return outcome in (_ALLOWED, _NO_POLICY)", "return True",
               "tests/test_robots_strict_mode.py::test_auth_walled_and_5xx_fail_closed"),
    "redaction": ("sieve/public_output.py", "return bool(_SECRET_KEY.search(normalized))", "return False",
                  "tests/test_public_output.py::test_secret_keys_redacted_at_any_depth"),
    "dns": ("sieve/security.py", "if is_forbidden_ip(ip):", "if False:",
            "tests/test_boundary_properties.py::test_transport_dns_guard_rejects_private_answer"),
    "redirect": ("sieve/domain_discovery.py", "current = validate_url(current)", "current = current",
                 "tests/test_domain_discovery_security.py::TestBoundedHttpFetch::test_redirect_to_private_target_is_refused"),
    "auth": ("sieve/server.py", "return hmac.compare_digest(values[0], expected)", "return True",
             "tests/test_server_security.py::test_bearer_auth_is_case_insensitive_and_exact"),
    "containment": ("sieve/sleeper_bridge.py", "if target != root and root not in target.parents:", "if False:",
                    "tests/test_sleeper_shot.py::test_sleeper_shot_rejects_outside_boundary"),
    "budget": ("sieve/resource_budget.py", "allowed = min(amount, available)", "allowed = amount",
               "tests/test_resource_budget.py::test_aggregate_account_reserves_before_work_and_reports_exhaustion"),
}


def test(root: Path, node: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, "-m", "pytest", "-q", node], cwd=root,
                          capture_output=True, text=True, timeout=120)


def main(args=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", action="store_true", help="Explicitly authorize local disposable mutations")
    parser.add_argument("--seed", choices=tuple(SEEDS), action="append")
    options = parser.parse_args(args)
    if not options.run:
        parser.error("use --run to execute; this is never a default CI mutation job")
    outcomes = []
    for name in options.seed or SEEDS:
        file, before, after, node = SEEDS[name]
        with tempfile.TemporaryDirectory(prefix="sieve-security-mutation-") as directory:
            root = Path(directory)
            for folder in ("sieve", "tests", "scripts"):
                shutil.copytree(ROOT / folder, root / folder,
                                ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "hermes"))
            shutil.copy2(ROOT / "pyproject.toml", root / "pyproject.toml")
            baseline = test(root, node)
            if baseline.returncode != 0:
                raise RuntimeError(f"{name} baseline did not pass: {baseline.stdout}{baseline.stderr}")
            path = root / file
            source = path.read_text()
            if source.count(before) != 1:
                raise ValueError(f"{name} replacement is absent or ambiguous")
            path.write_text(source.replace(before, after, 1))
            mutant = test(root, node)
            killed = mutant.returncode == 1 and "FAILED" in mutant.stdout
            outcomes.append({"seed": name, "test": node, "result": "killed" if killed else "survived-or-invalid",
                             "returncode": mutant.returncode})
    print(json.dumps(outcomes, indent=2))
    return any(item["result"] != "killed" for item in outcomes)


if __name__ == "__main__":
    raise SystemExit(main())
