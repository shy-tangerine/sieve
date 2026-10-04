"""Static agent-guidance policy tests (#149) and skill copy sync (#21).

#149: canonical and packaged Claude/Codex skills plus agent docs must never
recommend placing secrets in argv (positional provider/key examples), must
not advise dumping raw cookies/headers or authenticated HTML, and must not
recommend unauthenticated remote MCP exposure. Legitimate negative examples
(warnings against the pattern) are handled by requiring the secure
prompt/stdin guidance rather than a blanket word ban.

#21: platform metadata remains separate; the marked shared policy in every
skill must match the canonical section in docs/agents.md after whitespace
normalization.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]

SKILL_FILES = [
    ROOT / "sieve" / "_skills" / "sieve" / "SKILL.md",
    ROOT / "sieve" / "_skills" / "sieve-setup" / "SKILL.md",
    ROOT / "plugins" / "claude" / "sieve-web" / "skills" / "sieve-web" / "SKILL.md",
    ROOT / "plugins" / "codex" / "sieve-web-research" / "skills" / "sieve-web-research" / "SKILL.md",
]

AGENT_DOC_FILES = [
    ROOT / "docs" / "agents.md",
    ROOT / "docs" / "configuration.md",
    ROOT / "docs" / "cli.md",
    ROOT / "sieve" / "_skills" / "sieve-setup" / "SKILL.md",
]

CODE_FENCE_RE = re.compile(r"```[a-zA-Z0-9_-]*\n(.*?)```", re.DOTALL)


def _text(paths) -> str:
    return "\n".join(p.read_text(encoding="utf-8") for p in paths if p.exists())


def _fences(text: str) -> list[str]:
    return CODE_FENCE_RE.findall(text)


def _shared_policy(text):
    start, end = "<!-- sieve-shared-policy:start -->", "<!-- sieve-shared-policy:end -->"
    assert text.count(start) == text.count(end) == 1
    return " ".join(text.split(start)[1].split(end)[0].split())


def _unsafe_dump(fence):
    lowered = fence.lower()
    return any(re.search(pattern, lowered) for pattern in (
        r"document\.(?:cookie|body\.innerhtml)", r"localstorage",
        r"(?:print|console\.log|logger\.(?:debug|info|warning))\([^)]*(?:headers|cookies|authorization|api_key|token|password|secret|html|response\.text)",
        r"prompt\s*=.*(?:api_key|token|password|secret|authenticated_html|response\.text)",
    ))


@pytest.mark.parametrize("snippet", [
    "print(response.headers)", "logger.info(api_key)", "console.log(document.cookie)",
    "print(authenticated_html)", "console.log(document.body.innerHTML)",
    "prompt = instruction + authenticated_html", "prompt = api_key + request",
])
def test_unsafe_secret_and_authenticated_content_examples_are_detected(snippet):
    assert _unsafe_dump(snippet)


def test_safe_secret_input_example_is_allowed():
    assert not _unsafe_dump("sieve keys add llm\nsieve update check --json")


# ── #149: secrets never in argv ──────────────────────────────────────

class TestNoSecretsInArgv:
    def test_no_positional_key_examples_anywhere(self):
        text = _text(SKILL_FILES + AGENT_DOC_FILES)
        # The insecure legacy forms; the secure docs say `keys add <provider>`.
        for bad in ("keys add <provider> <key>", "keys add llm <key>",
                    "keys add serper sk-", "keys add brave <key>"):
            assert bad not in text, f"insecure example found: {bad!r}"

    def test_no_realistic_secret_shaped_examples_in_fences(self):
        for fence in _fences(_text(SKILL_FILES)):
            # Fake prefixes only; realistic-looking provider keys must not
            # appear even as examples.
            for pattern in (r"sk-[A-Za-z0-9]{20,}", r"tvly-[A-Za-z0-9]{20,}",
                            r"Bearer [A-Za-z0-9]{25,}"):
                assert not re.search(pattern, fence), (
                    f"realistic secret shape inside skill code fence: {fence[:120]!r}")

    def test_skills_mention_prompt_or_stdin_for_secrets(self):
        text = _text(SKILL_FILES)
        assert "prompt" in text.lower()
        assert "argv" not in text.lower() or "never" in text.lower()


# ── #149: no raw secret material / unauthenticated remote MCP advice ─

class TestNoUnsafeExfiltrationOrRemoteAdvice:
    def test_no_raw_cookie_or_authenticated_html_dumping_advice(self):
        for path in SKILL_FILES:
            text = path.read_text(encoding="utf-8")
            for fence in _fences(text):
                assert not _unsafe_dump(fence), f"{path.name}: unsafe secret/content dumping in fence"

    def test_no_unauthenticated_remote_mcp_hosting_advice(self):
        for path in SKILL_FILES:
            text = path.read_text(encoding="utf-8").lower()
            # `sieve mcp serve` (local stdio) is canonical; the retired
            # remote-transport aliases are not.
            assert "--transport http" not in text and "mcp --transport" not in text, (
                f"{path.name}: retired remote transport alias present")
            # The remote pilot requires explicit auth; a bare "public http
            # endpoint without authentication" recommendation is forbidden.
            assert "public endpoint without authentication" not in text


# ── #21: shared claims stay synchronized across skill copies ─────────

class TestSkillCopySynchronization:
    def test_shared_sections_match_canonical_agent_documentation(self):
        canonical = _shared_policy((ROOT / "docs" / "agents.md").read_text(encoding="utf-8"))
        for path in SKILL_FILES:
            assert _shared_policy(path.read_text(encoding="utf-8")) == canonical, str(path.relative_to(ROOT))

    def test_all_four_skill_files_exist(self):
        for path in SKILL_FILES:
            assert path.is_file(), f"missing skill file: {path.relative_to(ROOT)}"
