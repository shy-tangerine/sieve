"""Internal adapter for the optional faster-whisper runtime."""

from __future__ import annotations

from pathlib import Path


class WhisperUnavailableError(RuntimeError):
    """The optional local transcription runtime is not configured."""


# (#120) Model names are an external-process trust boundary: an arbitrary
# model string makes faster-whisper download attacker-chosen (potentially
# large) artifacts. Only operator-known aliases and explicit local paths are
# accepted; anything else fails with a clear validation error.
_ALLOWED_MODELS = frozenset({
    "tiny", "tiny.en", "base", "base.en", "small", "small.en",
    "medium", "medium.en", "large-v2", "large-v3", "distil-large-v2",
    "distil-large-v3", "turbo",
})

_MAX_TRANSCRIPT_CHARS = 1_000_000
_MAX_AUDIO_BYTES = 2 * 1024 * 1024 * 1024  # 2 GiB on-disk audio guard


def _validate_model(model_name: str) -> str:
    if not isinstance(model_name, str) or not model_name.strip():
        raise ValueError("model_name must be a non-empty string")
    if model_name in _ALLOWED_MODELS:
        return model_name
    candidate = Path(model_name)
    # Explicit local path policy: the model directory must already exist on
    # disk (operator-provisioned); remote repo downloads are not allowed.
    if candidate.is_absolute() and candidate.is_dir():
        return model_name
    raise ValueError(
        f"model {model_name!r} is not in the approved allowlist and is not an "
        "existing local model directory"
    )


def run(audio: Path, model_name: str) -> dict:
    """Transcribe one local audio file with faster-whisper."""
    try:
        from faster_whisper import WhisperModel
    except ImportError as exc:
        raise WhisperUnavailableError from exc

    model_name = _validate_model(model_name)

    if audio.is_file() and audio.stat().st_size > _MAX_AUDIO_BYTES:
        raise ValueError("audio file exceeds the 2 GiB transcription guard")

    model = WhisperModel(model_name)
    # Resolve the optional provider method at the adapter seam. This keeps
    # the provider's vocabulary out of Sieve's own transcription interface.
    transcribe_audio = model.transcribe
    segments, info = transcribe_audio(str(audio))
    parts: list[str] = []
    total = 0
    truncated = False
    for segment in segments:
        piece = segment.text.strip()
        parts.append(piece)
        total += len(piece) + 1
        if total >= _MAX_TRANSCRIPT_CHARS:
            truncated = True
            break
    text = " ".join(parts).strip()
    if truncated:
        text = text[:_MAX_TRANSCRIPT_CHARS] + "\n[truncated: transcript budget exceeded]"
    del model
    return {
        "source": "local-whisper",
        "model": model_name,
        "language": info.language,
        "text": text,
        "truncated": truncated,
    }
