import json
from pathlib import Path

import pytest

from sieve import youtube


class FakeProcess:
    def __init__(self, stdout, returncode=0):
        self.stdout = stdout
        self.returncode = returncode

    def communicate(self, timeout=None):
        return self.stdout, ""

    def kill(self):
        pass


def test_search_normalizes_tiramisu_fields(monkeypatch):
    seen = {}

    def fake_popen(command, **kwargs):
        seen["command"] = command
        seen["kwargs"] = kwargs
        return FakeProcess(json.dumps({"id": "abc", "title": "Trailer", "uploader": "Studio", "duration": 42}) + "\n")

    monkeypatch.setattr(youtube.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(youtube.shutil, "which", lambda _: "/usr/bin/yt-dlp")
    result = youtube.search("Example game")
    assert result["entries"][0]["webpage_url"] == "https://www.youtube.com/watch?v=abc"
    assert "--no-playlist" in seen["command"]
    assert seen["kwargs"]["shell"] is False


def test_error_classification_matches_upstream_policy():
    assert youtube.classify_error("HTTP 429 too many requests") == "rate_limited"
    assert youtube.classify_error("sign in to confirm you are not a bot") == "authentication_or_block"
    assert youtube.classify_error("network timeout") == "network"


def test_metadata_rejects_non_youtube_urls():
    with pytest.raises(ValueError):
        youtube.metadata("https://example.com/video")


def test_resolve_urls_uses_shared_runner_without_duplicate_runtime_args(monkeypatch):
    seen = {}

    def fake_run(args, timeout):
        seen["args"] = args
        seen["timeout"] = timeout
        return "https://cdn.example/video.mp4\n"

    monkeypatch.setattr(youtube, "_run", fake_run)
    monkeypatch.setattr(youtube.shutil, "which", lambda _: "/usr/bin/yt-dlp")
    monkeypatch.setattr(youtube, "validate_url", lambda value: value)

    assert youtube.resolve_urls("https://www.instagram.com/reel/ABC/", timeout=17) == [
        "https://cdn.example/video.mp4"
    ]
    assert seen == {
        "args": ["--print", "urls", "--no-playlist", "--", "https://www.instagram.com/reel/ABC/"],
        "timeout": 17,
    }


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "https://user:pass@example.com/video",
        "https://example.com:invalid/video",
        "http://127.0.0.1/video",
    ],
)
def test_resolve_urls_rejects_unsafe_media_urls(monkeypatch, url):
    monkeypatch.setattr(youtube.shutil, "which", lambda _: "/usr/bin/yt-dlp")
    with pytest.raises(ValueError):
        youtube.resolve_urls(url)


def test_youtube_rejects_credentials_and_custom_ports():
    with pytest.raises(ValueError):
        youtube.metadata("https://user:pass@" + "www.youtube.com/watch?v=abc")
    with pytest.raises(ValueError):
        youtube.metadata("https://www.youtube.com:8443/watch?v=abc")


def test_po_token_is_forwarded_but_not_returned(monkeypatch):
    """The token travels via a bounded config file, never argv (issue #9)."""
    monkeypatch.setenv("SIEVE_YTDLP_PO_TOKEN", "secret-token")
    monkeypatch.setattr(youtube.shutil, "which", lambda _: "/usr/bin/yt-dlp")
    seen = {}

    def fake_popen(command, **kwargs):
        seen["command"] = list(command)
        idx = list(command).index("--config-location")
        config_path = Path(command[idx + 1])
        seen["config_content"] = config_path.read_text(encoding="utf-8")
        seen["config_mode"] = config_path.stat().st_mode & 0o777
        seen["config_path"] = config_path
        return FakeProcess(json.dumps({"id": "abc", "title": "Video"}) + "\n")

    monkeypatch.setattr(youtube.subprocess, "Popen", fake_popen)
    result = youtube.search("demo", max_results=1)
    command = seen["command"]
    # The raw token must not appear anywhere in argv.
    assert "secret-token" not in " ".join(command)
    assert "youtube:po_token=secret-token" not in command
    # It must be carried by an owner-only config file passed via --config-location.
    assert "--config-location" in command
    assert seen["config_content"] == "--extractor-args youtube:po_token=secret-token\n"
    assert seen["config_mode"] == 0o600
    # The config file is removed after the run.
    assert not seen["config_path"].exists()
    assert "secret-token" not in json.dumps(result)


def test_po_token_is_redacted_from_failures(monkeypatch):
    monkeypatch.setenv("SIEVE_YTDLP_PO_TOKEN", "secret-token")
    monkeypatch.setattr(youtube.shutil, "which", lambda _: "/usr/bin/yt-dlp")
    monkeypatch.setattr(youtube.subprocess, "Popen", lambda *args, **kwargs: FakeProcess("", returncode=1))
    # The failure helper is exercised directly because fake stderr is empty.
    assert "secret-token" not in youtube._redact_error("token=secret-token")


def test_bounded_communicate_rejects_oversized_output():
    class Stream:
        def __init__(self, value):
            self.value = value

        def read(self, _size):
            value, self.value = self.value, b""
            return value

    class Process:
        stdout = Stream(b"x" * (youtube.MAX_OUTPUT_BYTES + 1))
        stderr = Stream(b"")
        returncode = 0

        def wait(self, timeout=None):
            return 0

    stdout, stderr, overflow = youtube._communicate_bounded(Process(), timeout=1)
    assert len(stdout) == youtube.MAX_OUTPUT_BYTES
    assert stderr == b""
    assert overflow is True


def test_runtime_args_browser_cookies(monkeypatch):
    monkeypatch.delenv("SIEVE_YTDLP_COOKIES_FILE", raising=False)
    monkeypatch.setenv("SIEVE_COOKIES_FROM_BROWSER", "firefox")
    args = youtube._runtime_args()
    assert args == ["--cookies-from-browser", "firefox"]


def test_runtime_args_uses_closed_sieve_profile(monkeypatch, tmp_path):
    monkeypatch.delenv("SIEVE_YTDLP_COOKIES_FILE", raising=False)
    monkeypatch.delenv("SIEVE_COOKIES_FROM_BROWSER", raising=False)
    monkeypatch.setenv("SIEVE_BROWSER_PROFILE_DIR", str(tmp_path))
    assert youtube._runtime_args() == ["--cookies-from-browser", f"chromium:{tmp_path}"]


def test_runtime_args_rejects_live_sieve_profile(monkeypatch, tmp_path):
    monkeypatch.setenv("SIEVE_BROWSER_PROFILE_DIR", str(tmp_path))
    monkeypatch.setenv("SIEVE_BROWSER_PROFILE_ACTIVE", "1")
    with pytest.raises(youtube.YouTubeFetchError, match="still in use"):
        youtube._runtime_args()


def test_runtime_args_file_wins_over_browser(monkeypatch, tmp_path):
    cookie_file = tmp_path / "cookies.txt"
    cookie_file.write_text("# Netscape HTTP Cookie File\n")
    monkeypatch.setenv("SIEVE_YTDLP_COOKIES_FILE", str(cookie_file))
    monkeypatch.setenv("SIEVE_COOKIES_FROM_BROWSER", "firefox")
    args = youtube._runtime_args()
    assert args[:2] == ["--cookies", str(cookie_file)]
    assert "--cookies-from-browser" not in args


def test_oversized_cookie_jar_rejected_before_ytdlp(monkeypatch, tmp_path):
    cookie_file = tmp_path / "cookies.txt"
    cookie_file.write_bytes(b"x" * (youtube.MAX_COOKIE_JAR_BYTES + 1))
    monkeypatch.setenv("SIEVE_YTDLP_COOKIES_FILE", str(cookie_file))
    monkeypatch.delenv("SIEVE_COOKIES_FROM_BROWSER", raising=False)
    with pytest.raises(youtube.YouTubeFetchError) as exc_info:
        youtube._runtime_args()
    assert exc_info.value.category == "configuration"
    assert "413" in str(exc_info.value)


def test_bot_wall_error_names_remedies(monkeypatch):
    class FailedProcess(FakeProcess):
        def __init__(self):
            super().__init__(b"", returncode=1)

        def communicate(self, timeout=None):
            return b"", b"Sign in to confirm you're not a bot"

    monkeypatch.setattr(youtube.subprocess, "Popen", lambda *a, **k: FailedProcess())
    monkeypatch.setattr(youtube.shutil, "which", lambda _: "/usr/bin/yt-dlp")
    monkeypatch.delenv("SIEVE_YTDLP_COOKIES_FILE", raising=False)
    monkeypatch.delenv("SIEVE_COOKIES_FROM_BROWSER", raising=False)
    with pytest.raises(youtube.YouTubeFetchError) as exc_info:
        youtube.metadata("https://www.youtube.com/watch?v=abc")
    assert exc_info.value.category == "authentication_or_block"
    assert "sleeper-fetch" in str(exc_info.value)
