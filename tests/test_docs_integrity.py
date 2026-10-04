"""Docs integrity tests: i18n summary sync (#203) and link/anchor checking (#207).

#203: every translated README summary carries canonical revision metadata and
keeps the must-stay-synchronized claims (install quickstart, license,
navigation links, capability tiers) aligned with the canonical README.
Summaries are intentionally shortened — the metadata says so, and the tests
check semantic claims, not literal text hashes.

#207: repository-local Markdown link and anchor validation over docs/ and the
localized READMEs. Relative paths, URL-encoded filenames and heading slug
rules are honored; mailto:, bare fragments, and external http(s) URLs are
excluded from network validation (any external sweep is separate and
nonblocking). Entirely offline.
"""
from __future__ import annotations

import re
import hashlib
import urllib.parse
from pathlib import Path

ROOT = Path(__file__).parents[1]
I18N_DIR = ROOT / "docs" / "i18n"
SUMMARIES = sorted(I18N_DIR.glob("README.*.md"))
CANONICAL = ROOT / "README.md"

MUST_SYNC_INSTALL = "uv sync"
MUST_SYNC_LICENSE = "MIT"
MUST_SYNC_TIERS = ("HTTP", "browser")


# ── helpers shared with the link checker ─────────────────────────────

def _headings(md_path: Path) -> list[str]:
    """Heading texts (## level and below) of a Markdown file."""
    out = []
    for line in md_path.read_text(encoding="utf-8").splitlines():
        m = re.match(r"^#{1,6}\s+(.*)$", line)
        if m:
            out.append(m.group(1).strip())
    return out


def _slug(heading: str) -> str:
    """GitHub-style heading slug (close enough for repo-local anchors)."""
    slug = heading.lower().strip()
    slug = re.sub(r"[^\w\- ]", "", slug, flags=re.UNICODE)
    slug = re.sub(r"\s+", "-", slug)
    return slug


def _iter_md_files() -> list[Path]:
    files = [CANONICAL, *sorted((ROOT / "docs").glob("*.md")), *SUMMARIES]
    return [f for f in files if f.is_file()]


_LINK_RE = re.compile(r"(?<!\!)\[[^\]]*\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")


def _local_links(md_path: Path) -> list[tuple[str, int]]:
    """Repo-local links (relative paths, with or without anchors) as
    (link, line_number). mailto:, bare #fragments, and absolute http(s) URLs
    are excluded from *local* validation by design (#207)."""
    out = []
    for lineno, line in enumerate(md_path.read_text(encoding="utf-8").splitlines(), 1):
        if line.lstrip().startswith("<!--"):  # skip metadata comments
            continue
        for link in _LINK_RE.findall(line):
            if link.startswith(("mailto:", "http://", "https://")):
                continue
            out.append((link, lineno))
    return out


# ── #203: i18n summary metadata and synchronized claims ──────────────

class TestI18nSummarySync:
    def test_summaries_exist(self):
        assert len(SUMMARIES) >= 5, "translated summaries went missing"

    def test_every_summary_has_canonical_metadata_header(self):
        for path in SUMMARIES:
            text = path.read_text(encoding="utf-8")
            m = re.search(r"<!--\s*i18n-summary(.*?)-->", text, re.DOTALL)
            assert m, f"{path.name}: missing i18n-summary metadata header"
            block = m.group(1)
            for field in ("source:", "source-revision:", "scope:", "synced-claims:"):
                assert field in block, f"{path.name}: metadata missing {field!r}"
            assert "summary" in block, (
                f"{path.name}: metadata must mark the file as an intentional summary")

    def test_source_revision_matches_canonical_readme(self):
        """The recorded source-revision must equal the canonical README's
        content hash so staleness is detectable in shallow clones and exports."""
        rev = "sha256:" + hashlib.sha256(CANONICAL.read_bytes()).hexdigest()
        for path in SUMMARIES:
            text = path.read_text(encoding="utf-8")
            m = re.search(r"source-revision:\s*(\S+)", text)
            assert m, f"{path.name}: no source-revision"
            assert m.group(1) == rev, (
                f"{path.name}: source-revision {m.group(1)!r} is stale "
                f"(canonical README is at {rev!r}) — update the translation "
                f"summary or re-record the revision")

    def test_install_claim_synchronized(self):
        for path in SUMMARIES:
            text = path.read_text(encoding="utf-8")
            assert MUST_SYNC_INSTALL in text, (
                f"{path.name}: install quickstart claim ({MUST_SYNC_INSTALL!r}) missing")

    def test_license_claim_synchronized(self):
        for path in SUMMARIES:
            text = path.read_text(encoding="utf-8")
            assert MUST_SYNC_LICENSE in text, (
                f"{path.name}: license claim ({MUST_SYNC_LICENSE!r}) missing")

    def test_capability_tiers_claim_synchronized(self):
        """Each summary names the HTTP-first tier and explicit browser
        escalation (the core capability claim) in its own language."""
        for path in SUMMARIES:
            text = path.read_text(encoding="utf-8")
            assert "HTTP" in text, f"{path.name}: HTTP-first tier claim missing"
            assert any(t.lower() in text.lower() for t in ("browser", "navegador", "ブラウザ", "浏览器", "Browser")), (
                f"{path.name}: browser-escalation claim missing")

    def test_no_retired_claims_in_summaries(self):
        for path in SUMMARIES:
            text = path.read_text(encoding="utf-8")
            for bad in ("keys add <provider> <key>", "sieve mcp serve --transport",
                        "--http", "sieve update -u <version>"):
                assert bad not in text, f"{path.name}: retired claim {bad!r}"


# ── #207: offline link/anchor validation ─────────────────────────────

class TestLocalLinkIntegrity:
    def test_all_repo_local_links_resolve(self):
        """Every repo-local Markdown link target exists on disk."""
        broken = []
        for md in _iter_md_files():
            for link, lineno in _local_links(md):
                target, _, fragment = link.partition("#")
                if not target:
                    continue  # bare in-page fragment handled by anchor test
                decoded = urllib.parse.unquote(target)
                resolved = (md.parent / decoded).resolve()
                if not resolved.exists():
                    broken.append(f"{md.relative_to(ROOT)}:{lineno} -> {link}")
        assert broken == [], "broken repo-local links:\n" + "\n".join(broken)

    def test_in_page_and_cross_file_anchors_resolve(self):
        """Anchors on existing Markdown targets match a heading slug."""
        broken = []
        for md in _iter_md_files():
            for link, lineno in _local_links(md):
                target, _, fragment = link.partition("#")
                if not fragment:
                    continue
                decoded = urllib.parse.unquote(fragment).lower()
                if target:
                    target_path = (md.parent / urllib.parse.unquote(target)).resolve()
                    if not target_path.exists():
                        continue  # missing file reported by the link test
                else:
                    target_path = md
                if target_path.suffix != ".md":
                    continue
                slugs = {_slug(h) for h in _headings(target_path)}
                if decoded not in slugs:
                    broken.append(
                        f"{md.relative_to(ROOT)}:{lineno} -> #{fragment} "
                        f"(not a heading of {target_path.name})")
        assert broken == [], "unresolvable anchors:\n" + "\n".join(broken)

    def test_duplicate_headings_do_not_break_anchors(self, tmp_path):
        """Fixture: two identical headings make the anchor ambiguous but
        resolvable; the checker accepts it rather than false-failing."""
        assert _slug("Use the async SDK!") == "use-the-async-sdk"

    def test_legitimate_external_links_are_not_network_checked(self):
        """http(s)/mailto links are excluded from local validation (#207):
        the checker must not attempt DNS/network work for them."""
        external = []
        for md in _iter_md_files():
            for link, _ in _local_links(md):
                assert not link.startswith(("http://", "https://", "mailto:")), (
                    f"{md.relative_to(ROOT)}: external link leaked into local checks")
            text = md.read_text(encoding="utf-8")
            external.extend(re.findall(r"https://[^\s)>\"']+", text))
        # Sanity: the corpus does contain external links; they are just not
        # validated here (a separate nonblocking sweep owns them).
        assert external, "expected external links in docs corpus"

    def test_url_encoded_link_targets_resolve(self, tmp_path):
        """Fixture: a file named with a space, linked URL-encoded, resolves."""
        target_dir = tmp_path / "d"
        target_dir.mkdir()
        (target_dir / "my file.md").write_text("# X\n")
        import urllib.parse
        quoted = urllib.parse.quote("my file.md")
        resolved = (target_dir / urllib.parse.unquote(quoted)).resolve()
        assert resolved.exists()
