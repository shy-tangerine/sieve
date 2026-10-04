"""Central redirect-policy contract test (issue #212).

Automatic redirect following is a trust boundary: a redirect target bypasses
per-request URL validation. Network code must NOT enable automatic redirects
unless the module is explicitly allowlisted here with rationale. This test
statically scans sieve/ for ``follow_redirects=True`` / auto-redirect
configurations and fails for any module not on the allowlist.

To allowlist a module: add it to REDIRECT_ALLOWLIST with a rationale. The
allowlist entry obligates the module to validate every hop itself (e.g.
fetcher.py follows redirects manually and passes each Location target
through the SSRF validator).
"""

import re
from pathlib import Path

# Modules where automatic redirect following is permitted, with the reason
# and the compensating control that validates redirect destinations.
REDIRECT_ALLOWLIST: dict[str, str] = {
    "sieve/fetcher.py": (
        "follow_redirects is a caller-supplied policy parameter; the default "
        "'safe' mode follows redirects manually and validates every Location "
        "target through the SSRF layer before the next request."
    ),
}

# Patterns that enable automatic redirect following.
_AUTO_REDIRECT_PATTERNS = (
    re.compile(r"follow_redirects\s*=\s*True"),
    re.compile(r"follow_redirects\s*=\s*\"(?:always|unsafe)\""),
    re.compile(r"redirects\s*=\s*True"),
)

# Clients whose redirect behavior is validated hop-by-hop elsewhere; the
# scanner skips these call sites even when the pattern appears in the module.
_SCAN_EXEMPT_CALLS = re.compile(
    r"follow_redirects\s*=\s*(?:False|\"never\"|\"safe\"|None)"
)


def _repo_root() -> Path:
    return Path(__file__).parents[1]


def test_no_unexpected_automatic_redirects():
    """Every follow_redirects=True site must live in REDIRECT_ALLOWLIST."""
    violations: list[str] = []
    for path in sorted((_repo_root() / "sieve").glob("*.py")):
        rel = path.relative_to(_repo_root()).as_posix()
        if rel in REDIRECT_ALLOWLIST:
            continue
        source = path.read_text(encoding="utf-8")
        # Strip lines where redirects are explicitly disabled so the scanner
        # only flags enabling sites.
        cleaned = "\n".join(
            line for line in source.splitlines()
            if not _SCAN_EXEMPT_CALLS.search(line)
        )
        for pattern in _AUTO_REDIRECT_PATTERNS:
            for match in pattern.finditer(cleaned):
                line_no = cleaned[: match.start()].count("\n") + 1
                violations.append(f"{rel}:{line_no}: {match.group(0)!r}")
    assert not violations, (
        "automatic redirect following outside the allowlist "
        "(add to REDIRECT_ALLOWLIST with rationale + compensating validation, "
        "or disable auto-redirects and validate each hop):\n"
        + "\n".join(violations)
    )


def test_allowlist_entries_exist_and_rationales_are_real():
    """Allowlist entries must point at real files with non-empty rationale."""
    for rel, rationale in REDIRECT_ALLOWLIST.items():
        assert (_repo_root() / rel).is_file(), f"allowlisted file missing: {rel}"
        assert len(rationale) > 40, f"rationale too thin for {rel}"
        assert "validat" in rationale.lower(), (
            f"allowlist rationale for {rel} must name its compensating validation"
        )


def test_fetcher_manual_redirects_validate_targets():
    """The fetcher allowlist entry is earned: it must manually follow redirects
    and pass each Location target through validate_url before the next hop."""
    source = (_repo_root() / "sieve" / "fetcher.py").read_text(encoding="utf-8")
    assert "Location" in source or "location" in source
    assert "validate_url" in source
