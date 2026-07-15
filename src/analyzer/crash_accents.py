"""Rare whole-house crash/transient detection.

Distinct from ``derive_energy_impacts`` (src/analyzer/derived.py): that
detector averages energy over a 1-second window and is tuned for
section-level energy jumps. It was verified (2026-07-14, against a real
cached hierarchy for Dream On/Aerosmith, 201.9s) to dilute genuine
sub-second percussive transients well below its 1.8x threshold -- two
known audible crashes (50.85s, ~190s) both measured only ~1.3x with that
windowing. This module operates directly on the full-mix onset-strength
envelope (librosa), which has much finer native time resolution than a
1-second average, and is tuned for high precision over recall: by design,
most songs should produce zero marks. See CLAUDE.md -> "Crash/Transient
Detector for Whole-House Accent" for the full background.

Vocal-proximity exclusion is intentionally NOT applied here -- it depends
on WhisperX word timing, which is a generator-side input
(``GenerationConfig.vocal_words``) not available during hierarchy
analysis. See ``src/generator/effect_placer.py::_place_crash_accents``.
"""
from __future__ import annotations

import numpy as np

from src.analyzer.result import TimingMark

_HOP_LENGTH = 512
# Both the percentile floor and the ratio-over-median floor must pass --
# together they held on the one validated song (crash peaks ~5x median,
# in the song's own 94-95th percentile) while still adapting to a given
# song's own dynamic range instead of using an absolute magnitude cutoff.
_PERCENTILE = 95.0
_MIN_RATIO_OVER_MEDIAN = 4.0
# Crashes are rare by design -- never allow two marks closer than this.
_MIN_GAP_MS = 10_000
# Hard cap regardless of how many candidates pass the threshold.
_MAX_MARKS = 5


def detect_crash_accents(audio: np.ndarray, sample_rate: int) -> list[TimingMark]:
    """Return up to `_MAX_MARKS` rare, extreme percussive transients.

    Each candidate frame of the onset-strength envelope must clear both a
    high percentile of the song's own envelope and a minimum ratio over the
    envelope's median, then candidates within `_MIN_GAP_MS` of each other
    are collapsed to their strongest peak.
    """
    import librosa

    if audio.size == 0:
        return []

    onset_env = librosa.onset.onset_strength(y=audio, sr=sample_rate, hop_length=_HOP_LENGTH)
    if onset_env.size == 0:
        return []

    median = float(np.median(onset_env))
    if median <= 0:
        return []
    threshold = float(np.percentile(onset_env, _PERCENTILE))

    candidates: list[tuple[int, float]] = []
    for i, val in enumerate(onset_env):
        if val < threshold or val / median < _MIN_RATIO_OVER_MEDIAN:
            continue
        time_ms = int(round(i * _HOP_LENGTH * 1000 / sample_rate))
        candidates.append((time_ms, float(val)))

    if not candidates:
        return []

    candidates.sort(key=lambda c: c[0])
    clustered: list[list[tuple[int, float]]] = [[candidates[0]]]
    for c in candidates[1:]:
        if c[0] - clustered[-1][-1][0] < _MIN_GAP_MS:
            clustered[-1].append(c)
        else:
            clustered.append([c])
    strongest_per_cluster = [max(cluster, key=lambda c: c[1]) for cluster in clustered]

    strongest_per_cluster.sort(key=lambda c: c[1], reverse=True)
    top = strongest_per_cluster[:_MAX_MARKS]
    top.sort(key=lambda c: c[0])

    return [TimingMark(time_ms=t, confidence=None, label="crash") for t, _ in top]
