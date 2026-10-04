import sys
import types

import pytest

from sieve import stt

SRT = (
    "1\n00:00:00,000 --> 00:00:01,000\nHello <b>world</b>\n\n"
    "2\n00:00:01,000 --> 00:00:02,000\nHello world\n"
)


def test_rejects_non_https_urls():
    with pytest.raises(ValueError):
        stt.transcribe("http://example.test/video")


def test_rejects_explicit_negative_timeout():
    with pytest.raises(ValueError, match="timeout must be >= 0"):
        stt.transcribe("https://example.test/video", timeout=-1)


def test_adaptive_timeout_uses_none_as_unset():
    assert stt.effective_timeout(None, 30) == 60
    assert stt.effective_timeout(45, 30) == 45


def test_adaptive_timeout_derives_from_duration():
    """None timeout adapts to media length; explicit value wins."""
    assert stt.effective_timeout(None, 10) == 60      # <=60s -> 60
    assert stt.effective_timeout(None, 120) == 120     # <=300s -> 120
    assert stt.effective_timeout(None, 600) == 300     # <=900s -> 300
    assert stt.effective_timeout(None, 1200) == 600    # >900s -> MAX_TIMEOUT
    # explicit value is returned unchanged (clamped to [1, MAX_TIMEOUT])
    assert stt.effective_timeout(90, 10) == 90
    assert stt.effective_timeout(0, 10) == 1           # zero clamps to 1


def test_effective_timeout_rejects_negative():
    with pytest.raises(ValueError, match="non-negative"):
        stt.effective_timeout(-1, 30)


def test_captions_win_without_model(monkeypatch):
    def fake_run(args, timeout):
        template = args[args.index("--output") + 1]
        Path = __import__("pathlib").Path
        Path(template.replace("%(ext)s", "en.srt")).write_text(SRT)
        return ""

    monkeypatch.setattr(stt.youtube, "_run", fake_run)
    result = stt.transcribe("https://example.test/video")
    assert result["ok"] is True
    assert result["source"] == "captions"
    assert result["text"] == "Hello world"


def test_whisper_fallback_without_network(monkeypatch, tmp_path):
    monkeypatch.setattr(stt.youtube, "_run", lambda args, timeout: "")
    audio = tmp_path / "audio.wav"
    audio.write_bytes(b"RIFF" + b"\x00" * 100)
    monkeypatch.setattr(stt, "_download_audio", lambda url, workdir, timeout: audio)
    monkeypatch.setattr(stt, "_audio_seconds", lambda path: 12.0)

    class FakeInfo:
        language = "en"

    class FakeModel:
        def __init__(self, name):
            assert name == "tiny"

        def transcribe(self, path):
            seg = types.SimpleNamespace(text="hi there")
            return [seg], FakeInfo()

    monkeypatch.setitem(sys.modules, "faster_whisper", types.SimpleNamespace(WhisperModel=FakeModel))
    result = stt.transcribe("https://example.test/video", model="tiny")
    assert result["source"] == "local-whisper"
    assert result["text"] == "hi there"
    assert result["language"] == "en"


def test_long_audio_refused(monkeypatch):
    def fake_run(args, timeout):
        template = args[args.index("--output") + 1]
        Path = __import__("pathlib").Path
        Path(template.replace("%(ext)s", "wav")).write_bytes(b"RIFF" + b"\x00" * 44)

    monkeypatch.setattr(stt.youtube, "_run", fake_run)
    monkeypatch.setattr(stt, "_audio_seconds", lambda path: stt.MAX_AUDIO_SECONDS + 1)
    with pytest.raises(stt.STTError) as exc_info:
        stt.transcribe("https://example.test/video", model="tiny")
    assert exc_info.value.category == "input"


def test_missing_subtitles_fall_back_to_whisper(monkeypatch, tmp_path):
    def fake_run(args, timeout):
        raise stt.youtube.YouTubeFetchError("ERROR: Did not get any data blocks")

    monkeypatch.setattr(stt.youtube, "_run", fake_run)
    audio = tmp_path / "audio.wav"
    audio.write_bytes(b"RIFF")
    monkeypatch.setattr(stt, "_download_audio", lambda url, workdir, timeout: audio)
    monkeypatch.setattr(stt, "_audio_seconds", lambda path: 5.0)
    monkeypatch.setattr(stt, "_transcribe_with_whisper", lambda audio, model: {
        "source": "local-whisper", "model": model, "language": "en", "text": "hi"})
    result = stt.transcribe("https://example.test/video", model="tiny")
    assert result["source"] == "local-whisper"


def test_preflight_refuses_long_source(monkeypatch):
    calls = []

    def fake_run(args, timeout):
        calls.append(args)
        return "7200\n"

    monkeypatch.setattr(stt.youtube, "_run", fake_run)
    with pytest.raises(stt.STTError) as exc_info:
        stt.transcribe("https://example.test/video", model="tiny")
    assert exc_info.value.category == "input"
    assert all("--output" not in args for args in calls)


def test_missing_extra_is_configuration_error(monkeypatch, tmp_path):
    monkeypatch.setattr(stt.youtube, "_run", lambda args, timeout: "")
    audio = tmp_path / "audio.wav"
    audio.write_bytes(b"RIFF")
    monkeypatch.setattr(stt, "_download_audio", lambda url, workdir, timeout: audio)
    monkeypatch.setattr(stt, "_audio_seconds", lambda path: 5.0)
    monkeypatch.delitem(sys.modules, "faster_whisper", raising=False)
    monkeypatch.setitem(sys.modules, "faster_whisper", None)
    with pytest.raises(stt.STTError) as exc_info:
        stt.transcribe("https://example.test/video", model="tiny")
    assert exc_info.value.category == "configuration"
    assert "uv sync --extra stt" in str(exc_info.value)
