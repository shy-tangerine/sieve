"""Optional local speech-to-text: captions first, faster-whisper fallback.

Requires the ``stt`` extra (``uv sync --extra stt``); the core stays
keyless and heavy-free. No daemon, no network beyond the source fetch,
zero idle VRAM â the model loads per call and is released on return.

Bounds: download/caption steps share ``timeout``; audio longer than
``MAX_AUDIO_SECONDS`` is refused; transcription itself runs to completion
(pick a small model for long audio).
"""

from __future__ import annotations

import math
import os
import re
import subprocess
import tempfile
from pathlib import Path
from urllib.parse import urlparse

from . import youtube
from sieve.public_output import safe_error, safe_diagnostic
from ._whisper import WhisperUnavailableError
from ._whisper import run as _run_whisper

DEFAULT_MODEL = "small"
DEFAULT_TIMEOUT = 300
MAX_TIMEOUT = 600
MAX_AUDIO_SECONDS = 1800
MAX_TRANSCRIPT_BYTES = 8 * 1024 * 1024

# Typed diagnostics accumulated during a transcribe() call (issue #233):
# configuration gaps and best-effort probe failures become observable
# instead of silently degrading behavior. Returned in result["diagnostics"].
_DIAGNOSTICS: list[dict] = []


def _diagnostics_reset() -> None:
    _DIAGNOSTICS.clear()


def _drain_diagnostics() -> list[dict]:
    out = list(_DIAGNOSTICS)
    _DIAGNOSTICS.clear()
    return out


def effective_timeout(user_timeout: int | None, media_seconds: float | None) -> int:
    """Return a bounded timeout, deriving one from duration when unset."""
    if user_timeout is not None:
        if (
            isinstance(user_timeout, bool)
            or not isinstance(user_timeout, (int, float))
            or not math.isfinite(float(user_timeout))
            or user_timeout < 0
        ):
            raise ValueError("timeout must be a finite non-negative number")
        return max(1, min(int(user_timeout), MAX_TIMEOUT))
    if media_seconds is None or media_seconds != media_seconds:
        return DEFAULT_TIMEOUT
    if media_seconds <= 60:
        return 60
    if media_seconds <= 300:
        return 120
    if media_seconds <= 900:
        return 300
    return MAX_TIMEOUT


class STTError(RuntimeError):
    def __init__(self, message: str, *, category: str = "stt") -> None:
        super().__init__(message)
        self.category = category


def _model_name(explicit: str | None) -> str:
    value = (explicit or os.environ.get("SIEVE_STT_MODEL", "") or DEFAULT_MODEL).strip()
    if not re.fullmatch(r"[A-Za-z0-9._-]{1,80}", value):
        raise ValueError("STT model name contains unsupported characters")
    return value


def _validate_url(url: str) -> str:
    value = str(url or "").strip()
    parsed = urlparse(value)
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("only canonical HTTPS media URLs are supported") from exc
    if (
        parsed.scheme != "https"
        or parsed.username
        or parsed.password
        or port not in (None, 443)
        or not parsed.hostname
    ):
        raise ValueError("only canonical HTTPS media URLs are supported")
    return value


_TIMESTAMP = re.compile(r"\d{2,}:\d{2}:\d{2}[,.]\d+.*-->.*")


def _clean_subtitle_text(contents: str) -> str:
    """Strip counters, timestamps, tags, and headers from SRT/VTT text."""
    lines = []
    for line in contents.splitlines():
        stripped = line.strip()
        if not stripped or stripped.isdigit() or stripped == "WEBVTT":
            continue
        if _TIMESTAMP.match(stripped) or stripped.startswith("NOTE"):
            continue
        stripped = re.sub(r"<[^>]+>", "", stripped)
        if stripped:
            lines.append(stripped)
    # ponytail: order-preserving dedupe; adjacent repeats are caption overlap
    seen = set()
    unique = [line for line in lines if not (line in seen or seen.add(line))]
    return "\n".join(unique).strip()


def _fetch_captions(url: str, workdir: Path, timeout: int) -> dict | None:
    """Return parsed captions, or None when the source exposes none."""
    try:
        youtube._run(
            ["--skip-download", "--write-subs", "--write-auto-subs",
             "--sub-langs", "en.*", "--sub-format", "srt/vtt/best",
             "--no-warnings", "--socket-timeout", "20",
             "--retries", "1", "--fragment-retries", "1",
             "--output", str(workdir / "cap.%(ext)s"), url],
            timeout,
        )
    except youtube.YouTubeFetchError as exc:
        # Missing subtitles are a gap, not a failure â the whisper
        # fallback still gets its turn. Anything else propagates.
        missing = str(exc).lower()
        if "sub" not in missing and "data blocks" not in missing:
            raise
        return None
    texts = []
    language = None
    for path in sorted(workdir.glob("cap.*")):
        if path.suffix.lower() not in {".srt", ".vtt"}:
            continue
        text = _clean_subtitle_text(path.read_text(encoding="utf-8", errors="replace"))
        if text:
            texts.append(text)
            if len(path.suffixes) > 1 and not language:
                language = path.suffixes[0].lstrip(".")
    if not texts:
        return None
    return {"source": "captions", "language": language, "text": max(texts, key=len)}


def _audio_seconds(path: Path) -> float | None:
    try:
        proc = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
            capture_output=True, text=True, timeout=30, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    try:
        return float((proc.stdout or "").strip())
    except ValueError:
        return None


def _download_audio(url: str, workdir: Path, timeout: int) -> Path:
    target = workdir / "audio.%(ext)s"
    youtube._run(
        ["-x", "--audio-format", "wav",
         "--no-playlist", "--no-progress", "--no-warnings",
         "--socket-timeout", "20", "--retries", "1", "--fragment-retries", "1",
         "--output", str(target.with_suffix(".%(ext)s")), url],
        timeout,
    )
    # yt-dlp/ffmpeg can leave sidecars (.part, .ytdl) in the work directory;
    # never mistake one of those for the audio payload.
    wavs = sorted(workdir.glob("audio.wav"))
    if not wavs:
        raise STTError("audio extraction produced no file (ffmpeg required)")
    audio = wavs[0]
    duration = _audio_seconds(audio)
    if duration is not None and duration > MAX_AUDIO_SECONDS:
        raise STTError(
            f"audio is {duration:.0f}s; refusing beyond {MAX_AUDIO_SECONDS}s â "
            "transcribe a shorter clip",
            category="input",
        )
    return audio


def _transcribe_with_whisper(audio: Path, model_name: str) -> dict:
    try:
        return _run_whisper(audio, model_name)
    except WhisperUnavailableError as exc:
        raise STTError(
            "local transcription needs the 'stt' extra: uv sync --extra stt",
            category="configuration",
        ) from exc


def _configured_whisper(audio: Path, model_name: str) -> dict | None:
    """Use an explicitly approved OpenAI Whisper CLI, when configured.

    The adapter contract is deliberately narrow: ``whisper INPUT --model
    MODEL --output_format txt --output_dir DIR`` writes one text file. Other
    executables are reported by setup but remain unbound until a compatible
    adapter is added.
    """
    configured = os.environ.get("SIEVE_STT_EXECUTABLE", "")
    if not configured:
        # config.load never raises for a missing file; it records typed
        # diagnostics (issue #233) instead, which we drain and surface as a
        # category="configuration" diagnostic so an unreadable/malformed
        # config is never silently treated as "no whisper configured".
        from . import config as _config
        configured = str(_config.get("stt_executable", "") or "")
        for diag in _config.take_diagnostics():
            _DIAGNOSTICS.append(diag)
    if not configured:
        return None
    if Path(configured).name not in {"whisper", "whisper.exe"}:
        _DIAGNOSTICS.append({"type": "unsupported_executable", "detail": "Configured transcription adapter is unsupported.",
                             "category": "configuration", "diagnostic": safe_diagnostic(category="configuration", route="stt")})
        return None
    path = Path(configured)
    if not path.is_file() or not os.access(path, os.X_OK):
        raise STTError("configured transcription executable is missing or not executable", category="configuration")
    with tempfile.TemporaryDirectory(prefix="sieve-whisper-") as output_dir:
        try:
            subprocess.run(
                [str(path), str(audio), "--model", model_name,
                 "--output_format", "txt", "--output_dir", output_dir],
                check=True, capture_output=True, text=True, timeout=MAX_TIMEOUT,
            )
        except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
            category = "permission" if isinstance(exc, PermissionError) else "timeout" if isinstance(exc, subprocess.TimeoutExpired) else "execution"
            raise STTError("Configured transcription adapter failed.", category=category) from exc
        files = sorted(Path(output_dir).glob("*.txt"))
        if not files:
            raise STTError("configured whisper adapter produced no transcript", category="configuration")
        transcript = files[0]
        if transcript.stat().st_size > MAX_TRANSCRIPT_BYTES:
            raise STTError("configured whisper transcript exceeded the safety limit", category="configuration")
        text = transcript.read_bytes()[:MAX_TRANSCRIPT_BYTES + 1]
        if len(text) > MAX_TRANSCRIPT_BYTES:
            raise STTError("configured whisper transcript exceeded the safety limit", category="configuration")
        return {"source": "configured-whisper", "model": model_name,
                "language": None, "text": text.decode("utf-8", errors="replace").strip()}


def _refuse_long_source(url: str, timeout: int) -> float | None:
    """Probe duration once; refuse overlong sources and return metadata."""
    try:
        output = youtube._run(
            ["--skip-download", "--print", "%(duration)s", "--no-warnings",
             "--socket-timeout", "20", "--retries", "1", url],
            min(timeout, 60),
        ).strip().splitlines()
        seconds = float((output or ["nan"])[0])
    except (OSError, subprocess.SubprocessError, ValueError,
            youtube.YouTubeFetchError) as exc:
        # Duration probe is best-effort: yt-dlp absent, network failure, or
        # unparsable duration output all degrade to "duration unknown" so the
        # bounded-transcription path can still run. Narrow exception set
        # instead of bare Exception (issue #233) so programming errors and
        # unexpected transport failures are not masked.
        failure = safe_error(exc, fallback_category="network")
        _DIAGNOSTICS.append({"type": "duration_probe_failed",
                             "category": failure["category"], "detail": failure["error"]})
        return None
    if seconds == seconds and seconds > MAX_AUDIO_SECONDS:
        raise STTError(
            f"source is {seconds:.0f}s; refusing beyond {MAX_AUDIO_SECONDS}s — "
            "transcribe a shorter clip", category="input")
    return seconds if seconds == seconds else None


def transcribe(url: str, *, model: str | None = None,
               timeout: int | None = None) -> dict:
    """Transcribe a media URL: captions first, local model fallback."""
    url = _validate_url(url)
    _diagnostics_reset()
    if timeout is None:
        requested = None
    else:
        from sieve.security import bounded_number
        requested = bounded_number(timeout, name="timeout", minimum=0,
                                   maximum=MAX_TIMEOUT, integer=True)
    probe_timeout = (DEFAULT_TIMEOUT if requested is None
                     else max(1, min(requested, MAX_TIMEOUT)))
    duration = _refuse_long_source(url, probe_timeout)
    effective = effective_timeout(requested, duration)
    model_name = _model_name(model)
    with tempfile.TemporaryDirectory(prefix="sieve_stt_") as tmp:
        workdir = Path(tmp)
        captions = _fetch_captions(url, workdir, effective)
        if captions and captions.get("text"):
            result = {"ok": True, "url": url, **captions}
        else:
            audio = _download_audio(url, workdir, effective)
            configured = _configured_whisper(audio, model_name)
            result = {"ok": True, "url": url,
                      **(configured or _transcribe_with_whisper(audio, model_name))}
    if requested is None and effective != DEFAULT_TIMEOUT:
        result["diagnostics"] = [{"type": "timeout_adjusted",
                                  "timeout": effective,
                                  "duration": duration}]
    drained = _drain_diagnostics()
    if drained:
        result.setdefault("diagnostics", []).extend(drained)
    return result
