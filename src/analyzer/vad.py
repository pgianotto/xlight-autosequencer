"""Voice activity detection (Silero VAD) — real acoustic vocal-onset evidence.

Used by ``PhonemeAnalyzer._align_with_lyrics`` to refine WhisperX forced-
alignment segment boundaries: the lyrics provider's own per-line timestamps
are only approximate, and a fixed padding tolerance around them is a blind
guess. When VAD confidently detects where speech actually starts/stops near
a line's provider timestamp, that's real evidence and should win over the
guess.

Optional dependency — degrades to ``None`` (no refinement, callers fall back
to their padding-only heuristic) when ``silero-vad`` isn't installed or
detection fails for any reason. Never raises.
"""
from __future__ import annotations

from typing import Optional

from src.log import get_logger

log = get_logger("xlight.vad")

_model = None  # lazily loaded, reused across calls in this process


def _get_model():
    global _model
    if _model is None:
        from silero_vad import load_silero_vad
        _model = load_silero_vad()
    return _model


def detect_speech_regions(
    audio, sample_rate: int = 16000,
) -> Optional[list[tuple[float, float]]]:
    """Return ``[(start_s, end_s), ...]`` speech regions in ``audio``, or
    ``None`` when ``silero-vad`` is unavailable or detection fails.

    ``audio`` is a mono float32 array/tensor at ``sample_rate`` — the exact
    format ``whisperx.load_audio()`` already produces, so callers that
    already loaded audio for WhisperX can pass it straight through with no
    extra I/O or resampling.
    """
    try:
        import torch
        from silero_vad import get_speech_timestamps

        model = _get_model()
        wav = audio if isinstance(audio, torch.Tensor) else torch.from_numpy(audio)
        timestamps = get_speech_timestamps(
            wav, model, sampling_rate=sample_rate, return_seconds=True,
        )
        return [(ts["start"], ts["end"]) for ts in timestamps]
    except ImportError:
        return None
    except Exception as exc:
        log.warning("Silero VAD speech detection failed: %s", exc)
        return None
