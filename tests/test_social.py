import json
import tracemalloc
from typing import ClassVar

import pytest

from sieve import social
from sieve.social import extract_profile_corpus, extract_profile_items, normalize, platform_for_url

HTML = """
<html><head>
<meta property="og:title" content="A public reel">
<meta property="og:description" content="Caption text">
<meta property="og:image" content="https://cdn.example/image.jpg">
<meta name="author" content="creator">
</head></html>
"""


def test_social_record_is_source_neutral():
    record = normalize("instagram", "https://www.instagram.com/reel/ABC/", HTML)
    assert record["platform"] == "instagram"
    assert record["caption"] == "Caption text"
    assert record["media_urls"] == ["https://cdn.example/image.jpg"]
    assert record["provenance"]["adapter"] == "sieve.social"


def test_social_platform_allowlist():
    assert platform_for_url("https://www.tiktok.com/@x/video/1") == "tiktok"


def test_social_urls_reject_credentials_and_custom_ports():
    with pytest.raises(ValueError, match="credentials"):
        platform_for_url("https://user:pass@" + "www.instagram.com/reel/ABC/")
    with pytest.raises(ValueError, match="custom port"):
        platform_for_url("https://www.tiktok.com:8443/@x/video/1")


def test_social_normalization_does_not_trust_off_platform_canonical():
    html = '<link rel="canonical" href="https://example.com/not-a-social-post">'
    record = normalize("instagram", "https://www.instagram.com/reel/ABC/", html)
    assert record["url"] == "https://www.instagram.com/reel/ABC/"


def test_empty_social_page_is_explicitly_reported():
    record = normalize("tiktok", "https://www.tiktok.com/@x/video/1", "<html></html>")
    assert record["ok"] is False
    assert record["collection_status"] == "empty_or_blocked"


def test_generic_platform_shell_is_not_content():
    record = normalize("instagram", "https://www.instagram.com/reel/ABC/", "<title>Instagram</title>")
    assert record["ok"] is False
    assert record["collection_status"] == "empty_or_blocked"


def test_social_media_urls_reject_active_or_credentialed_metadata():
    html = """
    <meta property="og:title" content="Public reel">
    <meta property="og:image" content="javascript:alert(1)">
    """
    record = normalize("instagram", "https://www.instagram.com/reel/ABC/", html)
    assert record["media_urls"] == []

    html = '<meta property="og:image" content="https://user:pass@' + "cdn.example/image.jpg'>"
    record = normalize("instagram", "https://www.instagram.com/reel/ABC/", html)
    assert record["media_urls"] == []

    html = '<meta property="og:image" content="https://cdn.example/image.jpg?a=1&amp;b=2">'
    record = normalize("instagram", "https://www.instagram.com/reel/ABC/", html)
    assert record["media_urls"] == ["https://cdn.example/image.jpg?a=1&b=2"]


def test_profile_grid_parser_preserves_visible_evidence_and_deduplicates():
    html = '''<a href="https://www.tiktok.com/@x/video/123"><img alt="caption" src="cover.jpg">1.2M</a>
              <a href="https://www.tiktok.com/@x/video/123">duplicate</a>'''
    items = extract_profile_items("tiktok", html)
    assert items == [{"url": "https://www.tiktok.com/@x/video/123",
                      "visible_metric": "1.2M", "caption": "caption", "cover_url": "cover.jpg"}]


def test_profile_grid_parser_resolves_instagram_profile_relative_links():
    html = '<a href="/tykopath/reel/ABC/"><img alt="reel" src="cover.jpg"></a>'
    items = extract_profile_items("instagram", html, base_url="https://www.instagram.com/tykopath/reels/")
    assert items[0]["url"] == "https://www.instagram.com/tykopath/reel/ABC/"


def test_profile_grid_parser_accepts_direct_instagram_item_links():
    html = '<a href="https://www.instagram.com/reel/ABC/"><img alt="reel"></a>'
    items = extract_profile_items("instagram", html)
    assert items[0]["url"] == "https://www.instagram.com/reel/ABC/"


def test_instagram_profile_corpus_extracts_payload_metrics_and_null_private_counts():
    html = '''
    <a href="https://www.instagram.com/reel/AbCdE123/"><img alt="grid caption"></a>
    <script type="application/json">{"items":[{"code":"AbCdE123",
      "caption":{"text":"payload caption"},"play_count":321,
      "like_count":22,"comment_count":4}]}</script>
    '''
    records = extract_profile_corpus("instagram", html, max_posts=10)
    assert records == [{"code": "AbCdE123", "shortcode": "AbCdE123",
        "url": "https://www.instagram.com/reel/AbCdE123/", "caption": "payload caption",
        "views": 321, "metrics": {"views": 321, "likes": 22, "comments": 4,
                                     "shares": None, "saves": None},
        "cover_url": None,
        "provenance": {"adapter": "sieve.social.instagram",
                       "source_url": "https://www.instagram.com/reel/AbCdE123/",
                       "evidence": "rendered_dom_or_captured_xhr"}}]


class _FakeBrowserResponse:
    status = 200
    url = "https://www.instagram.com/reel/ABC/"
    fetcher_used = "stealthy"
    error = ""
    content: ClassVar[list[str]] = [HTML]


class _FakeBrowserServer:
    calls: ClassVar[list] = []
    shutdowns = 0

    async def stealthy_fetch(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return _FakeBrowserResponse()

    async def _shutdown_close_sessions(self):
        type(self).shutdowns += 1


def test_browser_fetch_forwards_authenticated_session_options_and_closes(monkeypatch):
    monkeypatch.setenv("SIEVE_BROWSER_BACKEND", "pool")
    _FakeBrowserServer.calls = []
    _FakeBrowserServer.shutdowns = 0
    monkeypatch.setattr("sieve.server.MasterFetchServer", _FakeBrowserServer)

    record = social.fetch_browser(
        "https://www.instagram.com/reel/ABC/",
        cdp_url="http://127.0.0.1:9222",
        real_chrome=True,
        wait=250,
        wait_selector="article",
        network_idle=True,
        timeout=12,
    )

    args, kwargs = _FakeBrowserServer.calls[0]
    assert args[0] == "https://www.instagram.com/reel/ABC/"
    assert kwargs["cdp_url"] == "http://127.0.0.1:9222"
    assert kwargs["real_chrome"] is True
    assert kwargs["wait"] == 250
    assert kwargs["wait_selector"] == "article"
    assert kwargs["network_idle"] is True
    assert kwargs["timeout"] == 12_000
    assert record["ok"] is True
    assert record["provenance"]["browser_status"] == 200
    assert _FakeBrowserServer.shutdowns == 1


def test_browser_collection_forwards_scroll_action_and_closes(monkeypatch):
    monkeypatch.setenv("SIEVE_BROWSER_BACKEND", "pool")
    monkeypatch.setenv("SIEVE_ENABLE_SOCIAL_COLLECT", "1")
    _FakeBrowserServer.calls = []
    _FakeBrowserServer.shutdowns = 0
    _FakeBrowserResponse.content = [
        '<a href="https://www.instagram.com/reel/ABC/"><img alt="reel"></a>'
    ]
    monkeypatch.setattr("sieve.server.MasterFetchServer", _FakeBrowserServer)

    result = social.collect_browser(
        "https://www.instagram.com/creator/reels/",
        creator="creator",
        cdp_url="http://127.0.0.1:9222",
        scrolls=3,
        scroll_delay=400,
        timeout=9,
        max_items=1,
    )

    _, kwargs = _FakeBrowserServer.calls[0]
    assert kwargs["cdp_url"] == "http://127.0.0.1:9222"
    assert kwargs["timeout"] == 9_000
    assert kwargs["page_action"] is not None
    assert result["ok"] is True
    assert result["items"][0]["url"].endswith("/reel/ABC/")
    assert _FakeBrowserServer.shutdowns == 1


def test_browser_collection_checkpoint_resume_parses_codes_without_duplicates(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("SIEVE_BROWSER_BACKEND", "pool")
    monkeypatch.setenv("SIEVE_ENABLE_SOCIAL_COLLECT", "1")

    class Response:
        status = 200
        url = "https://www.instagram.com/creator/reels/"
        fetcher_used = "fixture"
        error = ""
        content = [
            '<a href="https://www.instagram.com/reel/AbCdE123/"><img alt="first"></a>',
            '<a href="https://www.instagram.com/reel/DeFgH456/"><img alt="second"></a>',
        ]
        network = {"fragments": []}

    class Server:
        async def stealthy_fetch(self, *args, **kwargs):
            return Response()

        async def _shutdown_close_sessions(self):
            return None

    checkpoint = tmp_path / "instagram.jsonl"
    checkpoint.write_text(
        json.dumps({"code": "AbCdE123", "url": "https://www.instagram.com/reel/AbCdE123/"})
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr("sieve.server.MasterFetchServer", Server)

    social.collect_browser(
        "https://www.instagram.com/creator/reels/",
        creator="creator",
        max_items=2,
        scrolls=1,
        checkpoint=str(checkpoint),
    )
    social.collect_browser(
        "https://www.instagram.com/creator/reels/",
        creator="creator",
        max_items=2,
        scrolls=1,
        checkpoint=str(checkpoint),
    )

    records = [json.loads(line) for line in checkpoint.read_text(encoding="utf-8").splitlines()]
    assert [record["code"] for record in records] == ["AbCdE123", "DeFgH456"]

def test_instagram_collection_skips_retained_transcripts(monkeypatch, tmp_path):
    monkeypatch.setenv("SIEVE_BROWSER_BACKEND", "pool")
    monkeypatch.setenv("SIEVE_ENABLE_SOCIAL_COLLECT", "1")
    monkeypatch.setenv("IG_TRANSCRIPTS_DIR", str(tmp_path))
    (tmp_path / "AbCdE123.json").write_text("{}", encoding="utf-8")

    class Response:
        status = 200
        url = "https://www.instagram.com/creator/reels/"
        fetcher_used = "fixture"
        error = ""
        content = ['<script type="application/json">{"items":[{"code":"AbCdE123"},{"code":"DeFgH456"}]}</script>']
        network = {"fragments": []}

    class Server:
        async def stealthy_fetch(self, *args, **kwargs):
            return Response()
        async def _shutdown_close_sessions(self):
            return None

    monkeypatch.setattr("sieve.server.MasterFetchServer", Server)
    result = social.collect_browser("https://www.instagram.com/creator/reels/", max_items=2, scrolls=1)
    assert [item["code"] for item in result["items"]] == ["DeFgH456"]
    assert result["provenance"]["skipped_transcripts"] == 1


@pytest.mark.parametrize("code", ["*", "?", "[]", "normal-code"])
def test_transcript_exists_treats_code_as_literal(monkeypatch, tmp_path, code):
    monkeypatch.setenv("IG_TRANSCRIPTS_DIR", str(tmp_path))
    (tmp_path / f"{code}.json").write_text("{}", encoding="utf-8")
    (tmp_path / "unrelated.json").write_text("{}", encoding="utf-8")

    assert social._transcript_exists(code) is True


@pytest.mark.parametrize("code", ["*", "?", "[]"])
def test_transcript_exists_does_not_expand_glob_metacharacters(monkeypatch, tmp_path, code):
    monkeypatch.setenv("IG_TRANSCRIPTS_DIR", str(tmp_path))
    (tmp_path / "unrelated.json").write_text("{}", encoding="utf-8")

    assert social._transcript_exists(code) is False


def test_sleeper_collection_skips_retained_transcripts(monkeypatch, tmp_path):
    monkeypatch.setenv("SIEVE_BROWSER_BACKEND", "sleeper")
    monkeypatch.setenv("SIEVE_ENABLE_SOCIAL_COLLECT", "1")
    monkeypatch.setenv("IG_TRANSCRIPTS_DIR", str(tmp_path))
    (tmp_path / "AbCdE123.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(
        social,
        "_sleeper_html",
        lambda *args, **kwargs: (
            '<script type="application/json">'
            '{"items":[{"code":"AbCdE123"},{"code":"DeFgH456"}]}'
            "</script>"
        ),
    )

    result = social.collect_browser(
        "https://www.instagram.com/creator/reels/", max_items=2, scrolls=1
    )

    assert [item["code"] for item in result["items"]] == ["DeFgH456"]
    assert result["provenance"]["skipped_transcripts"] == 1


def test_sleeper_collection_checkpoint_resume_deduplicates(monkeypatch, tmp_path):
    monkeypatch.setenv("SIEVE_BROWSER_BACKEND", "sleeper")
    monkeypatch.setenv("SIEVE_ENABLE_SOCIAL_COLLECT", "1")
    monkeypatch.setattr(
        social,
        "_sleeper_html",
        lambda *args, **kwargs: (
            '<a href="https://www.instagram.com/reel/AbCdE123/"><img alt="first"></a>'
            '<a href="https://www.instagram.com/reel/DeFgH456/"><img alt="second"></a>'
        ),
    )
    checkpoint = tmp_path / "instagram.jsonl"

    first = social.collect_browser(
        "https://www.instagram.com/creator/reels/",
        max_items=2,
        scrolls=1,
        checkpoint=str(checkpoint),
    )
    second = social.collect_browser(
        "https://www.instagram.com/creator/reels/",
        max_items=2,
        scrolls=1,
        checkpoint=str(checkpoint),
    )

    records = [json.loads(line) for line in checkpoint.read_text(encoding="utf-8").splitlines()]
    assert [record["code"] for record in records] == ["AbCdE123", "DeFgH456"]
    assert first["provenance"]["checkpoint"] == str(checkpoint)
    assert second["provenance"]["checkpoint"] == str(checkpoint)


def test_sleeper_checkpoint_deduplicates_shortcode_and_url_fallbacks(tmp_path):
    checkpoint = tmp_path / "records.jsonl"
    checkpoint.write_text(
        json.dumps({"shortcode": "already"}) + "\n"
        + json.dumps({"url": "https://example.test/new"}) + "\n",
        encoding="utf-8",
    )

    social._checkpoint_items(
        [
            {"code": "already", "url": "https://example.test/already"},
            {"shortcode": "new-shortcode"},
            {"shortcode": "new-shortcode"},
            {"url": "https://example.test/new"},
            {"url": "https://example.test/fresh"},
        ],
        str(checkpoint),
    )

    records = [json.loads(line) for line in checkpoint.read_text(encoding="utf-8").splitlines()]
    assert records == [
        {"shortcode": "already"},
        {"url": "https://example.test/new"},
        {"shortcode": "new-shortcode"},
        {"url": "https://example.test/fresh"},
    ]


@pytest.mark.parametrize("backend", ["pool", "sleeper"])
def test_collection_checkpoint_recovers_utf8_tail_and_matches_all_aliases(
    monkeypatch, tmp_path, backend
):
    monkeypatch.setenv("SIEVE_BROWSER_BACKEND", backend)
    monkeypatch.setenv("SIEVE_ENABLE_SOCIAL_COLLECT", "1")
    checkpoint = tmp_path / "records.jsonl"
    checkpoint.write_bytes(
        json.dumps({"shortcode": "AbCdE123"}).encode() + b"\n" + b'{"broken": "\xff\n'
    )

    html = '<a href="https://www.instagram.com/reel/AbCdE123/"><img alt="same"></a>'
    if backend == "sleeper":
        monkeypatch.setattr(social, "_sleeper_html", lambda *a, **kw: html)
    else:
        class Response:
            status = 200
            url = "https://www.instagram.com/creator/reels/"
            fetcher_used = "fixture"
            error = ""
            content = [html]
            network = {"fragments": []}

        class Server:
            async def stealthy_fetch(self, *args, **kwargs):
                return Response()

            async def _shutdown_close_sessions(self):
                return None

        monkeypatch.setattr("sieve.server.MasterFetchServer", Server)

    social.collect_browser(
        "https://www.instagram.com/creator/reels/",
        max_items=1,
        scrolls=1,
        checkpoint=str(checkpoint),
    )

    records = [
        json.loads(line.decode("utf-8"))
        for line in checkpoint.read_bytes().splitlines()
        if b"\xff" not in line
    ]
    assert records == [{"shortcode": "AbCdE123"}]


def test_checkpoint_recovery_reads_bounded_tail_and_streams_lines(tmp_path):
    checkpoint = tmp_path / "large.jsonl"
    checkpoint.write_bytes(
        json.dumps({"platform": "instagram", "code": "old"}).encode()
        + b"\n"
        + (b"x" * social.MAX_CHECKPOINT_READ_BYTES)
        + b"\n"
    )

    social._checkpoint_items(
        [{"platform": "instagram", "code": "new"}], str(checkpoint)
    )

    records = []
    for line in checkpoint.read_text().splitlines():
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            pass
    assert any(record["code"] == "new" for record in records)


def test_checkpoint_scanner_bounds_unterminated_oversized_line(tmp_path, monkeypatch):
    checkpoint = tmp_path / "unterminated.jsonl"
    monkeypatch.setattr(social, "MAX_CHECKPOINT_LINE_BYTES", 1024)
    checkpoint.write_bytes(b"x" * (8 * 1024 * 1024))

    tracemalloc.start()
    try:
        social._checkpoint_items([{"platform": "instagram", "code": "new"}], str(checkpoint))
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    assert peak < 512 * 1024
    assert checkpoint.read_bytes().endswith(
        b'{"platform": "instagram", "code": "new"}\n'
    )


def test_checkpoint_numeric_aliases_are_scoped_by_platform(tmp_path):
    checkpoint = tmp_path / "platforms.jsonl"
    social._checkpoint_items(
        [
            {"platform": "instagram", "code": "12345", "url": "https://www.instagram.com/reel/12345/"},
            {"platform": "tiktok", "code": "12345", "url": "https://www.tiktok.com/@x/video/12345"},
        ],
        str(checkpoint),
    )

    assert [json.loads(line)["platform"] for line in checkpoint.read_text().splitlines()] == [
        "instagram", "tiktok"
    ]


def test_instagram_collection_rejects_unbounded_pacing(monkeypatch):
    monkeypatch.setenv("SIEVE_ENABLE_SOCIAL_COLLECT", "1")
    with pytest.raises(ValueError, match="scroll_delay"):
        social.collect_browser("https://www.instagram.com/creator/reels/", scroll_delay=10_001)


def test_tiktok_collection_not_affected_by_transcript_filter(monkeypatch, tmp_path):
    """Transcript filtering must only apply to Instagram, not TikTok."""
    monkeypatch.setenv("SIEVE_BROWSER_BACKEND", "sleeper")
    monkeypatch.setenv("SIEVE_ENABLE_SOCIAL_COLLECT", "1")
    monkeypatch.setenv("IG_TRANSCRIPTS_DIR", str(tmp_path))
    # Create a fake transcript file that would match an Instagram code
    (tmp_path / "12345.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(
        social, "_sleeper_html",
        lambda *a, **kw: '<a href="https://www.tiktok.com/@x/video/12345">vid</a>',
    )
    result = social.collect_browser(
        "https://www.tiktok.com/@x", max_items=5, scrolls=0
    )
    # TikTok items are NOT filtered by IG_TRANSCRIPTS_DIR
    assert len(result["items"]) == 1
    assert result["provenance"]["skipped_transcripts"] == 0


def test_browser_social_fetch_rejects_off_platform_final_url(monkeypatch):
    monkeypatch.setenv("SIEVE_BROWSER_BACKEND", "pool")
    _FakeBrowserServer.calls = []
    _FakeBrowserServer.shutdowns = 0
    _FakeBrowserResponse.url = "https://example.com/redirected"
    monkeypatch.setattr("sieve.server.MasterFetchServer", _FakeBrowserServer)

    with pytest.raises(RuntimeError, match="permitted social host"):
        social.fetch_browser("https://www.instagram.com/reel/ABC/")
    assert _FakeBrowserServer.shutdowns == 1
    _FakeBrowserResponse.url = "https://www.instagram.com/reel/ABC/"


def test_direct_social_response_is_bounded(monkeypatch):
    class Response:
        status_code = 200
        encoding = "utf-8"

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def iter_bytes(self):
            yield b"x" * (social.MAX_RESPONSE_BYTES + 1)

    monkeypatch.setattr(social.httpx, "stream", lambda *args, **kwargs: Response())
    try:
        social.fetch("https://www.instagram.com/reel/ABC/")
    except RuntimeError as exc:
        assert "safety limit" in str(exc)
    else:
        raise AssertionError("oversized response was accepted")


def test_direct_social_redirects_stay_on_the_platform(monkeypatch):
    class Response:
        def __init__(self, status_code, location=None, body=b"<title>Post</title>"):
            self.status_code = status_code
            self.headers = {"location": location} if location else {}
            self.encoding = "utf-8"
            self.body = body

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def iter_bytes(self):
            yield self.body

    responses = iter([
        Response(302, "http://127.0.0.1:8080/private"),
    ])
    monkeypatch.setattr(social.httpx, "stream", lambda *args, **kwargs: next(responses))
    with pytest.raises(RuntimeError, match="permitted host"):
        social._bounded_get_text("https://www.instagram.com/reel/ABC/", timeout=5, platform="instagram")

    responses = iter([
        Response(302, "/reel/XYZ/"),
        Response(200),
    ])
    monkeypatch.setattr(social.httpx, "stream", lambda *args, **kwargs: next(responses))
    assert social._bounded_get_text(
        "https://www.instagram.com/reel/ABC/", timeout=5, platform="instagram"
    ) == "<title>Post</title>"


def test_social_comments_normalizes(monkeypatch, tmp_path):
    info = tmp_path / "post.info.json"
    info.write_text(
        '{"comments": [{"author": "fan", "text": "what game?", "like_count": 5},'
        ' {"author": {"name": "op"}, "text": "reply", "like_count": 0}]}'
    )
    monkeypatch.setattr(social, "_ytdlp_run", lambda args, timeout: "")
    monkeypatch.setattr(social.shutil, "which", lambda _: "/usr/bin/yt-dlp")
    monkeypatch.setattr(social.Path, "glob", lambda self, pat: [info] if "info" in pat else [])
    record = social.comments("https://www.instagram.com/p/ABC/", max_comments=10)
    assert record["ok"] and record["count"] == 2
    assert record["comments"][0] == {"author": "fan", "text": "what game?", "likes": 5, "timestamp": None}
    assert record["comments"][1]["author"] == "op"


def test_parse_browser_comments():
    text = "Caption here\n\n3,974 likes\n\nReply\n\nView all 25 replies\n\nThis game was $1\n\n1,640 likes\n\nMore posts from x"
    items = social.parse_browser_comments(text, 10)
    texts = [i["text"] for i in items]
    assert "Caption here" in texts and "This game was $1" in texts
    assert not any("Reply" in t or "likes" in t for t in texts)
