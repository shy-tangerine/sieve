import os

import pytest

from sieve import social


def test_collect_browser_blocked_by_default():
    os.environ.pop("SIEVE_ENABLE_SOCIAL_COLLECT", None)
    with pytest.raises(RuntimeError, match="SIEVE_ENABLE_SOCIAL_COLLECT=1"):
        social.collect_browser("https://www.instagram.com/handle/reels/")


def test_comments_browser_blocked_by_default():
    os.environ.pop("SIEVE_ENABLE_SOCIAL_COLLECT", None)
    with pytest.raises(RuntimeError, match="SIEVE_ENABLE_SOCIAL_COLLECT=1"):
        social.comments_browser("https://www.instagram.com/reel/ABC123/")


def test_gate_passes_when_enabled(monkeypatch):
    monkeypatch.setenv("SIEVE_ENABLE_SOCIAL_COLLECT", "1")
    social._require_social_collect()
