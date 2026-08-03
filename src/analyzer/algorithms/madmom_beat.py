"""T031: Madmom RNN beat and downbeat tracking algorithms (optional)."""
from __future__ import annotations

import numpy as np

from src.analyzer.algorithms.base import Algorithm
from src.analyzer.result import TimingMark, TimingTrack

_DOWNBEATS_ASARRAY_PATCHED = False


def _patch_downbeats_asarray() -> None:
    """Restore pre-1.24 numpy ``asarray`` semantics inside madmom's downbeats.

    madmom 0.16.1 ``features/downbeats.py`` does
    ``np.argmax(np.asarray(results)[:, 1])`` where ``results`` is a list of
    ``(viterbi_path, log_prob)`` pairs whose path arrays differ in length.
    numpy >= 1.24 refuses to build that ragged array (``ValueError:
    inhomogeneous shape``); older numpy silently produced an object array,
    which madmom relies on. Replace only the ``np`` name in that one module
    with a proxy that delegates everything to numpy except ``asarray``, which
    falls back to ``dtype=object`` on the ragged-array error. Idempotent;
    other modules' numpy is untouched.
    """
    global _DOWNBEATS_ASARRAY_PATCHED
    if _DOWNBEATS_ASARRAY_PATCHED:
        return
    import numpy as _real_np
    from madmom.features import downbeats as _dbm

    class _NumpyAsarrayCompat:
        def __getattr__(self, name):
            return getattr(_real_np, name)

        @staticmethod
        def asarray(a, *args, **kwargs):
            try:
                return _real_np.asarray(a, *args, **kwargs)
            except ValueError:
                return _real_np.asarray(a, dtype=object)

    _dbm.np = _NumpyAsarrayCompat()
    _DOWNBEATS_ASARRAY_PATCHED = True


class MadmomBeatAlgorithm(Algorithm):
    """RNN+DBN beat tracker via madmom."""

    name = "madmom_beats"
    element_type = "beat"
    library = "madmom"
    plugin_key = None
    parameters = {}
    preferred_stem = "drums"
    depends_on = ["stem_separation"]

    def _run(self, audio: np.ndarray, sample_rate: int) -> TimingTrack:
        from madmom.features.beats import RNNBeatProcessor, BeatTrackingProcessor

        proc = BeatTrackingProcessor(fps=100)
        act = RNNBeatProcessor()(audio.astype(np.float32))
        beat_times = proc(act)
        marks = [
            TimingMark(time_ms=int(round(float(t) * 1000)), confidence=None)
            for t in beat_times
        ]
        return TimingTrack(
            name=self.name,
            algorithm_name=self.name,
            element_type=self.element_type,
            marks=marks,
            quality_score=0.0,
        )


class MadmomDownbeatAlgorithm(Algorithm):
    """RNN downbeat tracker via madmom."""

    name = "madmom_downbeats"
    element_type = "bar"
    library = "madmom"
    plugin_key = None
    parameters = {}
    preferred_stem = "drums"
    depends_on = ["stem_separation"]

    def _run(self, audio: np.ndarray, sample_rate: int) -> TimingTrack:
        from madmom.features.downbeats import RNNDownBeatProcessor, DBNDownBeatTrackingProcessor

        _patch_downbeats_asarray()
        proc = DBNDownBeatTrackingProcessor(beats_per_bar=[3, 4], fps=100)
        act = RNNDownBeatProcessor()(audio.astype(np.float32))
        downbeats = proc(act)
        # downbeats is Nx2 array: [time, beat_number]; keep beat_number==1
        # (downbeats). The DBN tested both 3- and 4-beat-per-bar hypotheses
        # to produce this, so the highest beat_number reached within each
        # bar (before it resets to 1 at the next downbeat) is real per-song
        # meter evidence -- stash it as that bar's mark label (e.g. "4")
        # rather than discarding it, so orchestrator.py's meter detection
        # (_detect_time_signature) can use it. Unlike qm_bars and
        # librosa_bars, which both assume a fixed 4 beats/bar by
        # construction, this is the only bar tracker here that actually
        # measures it.
        beat_numbers = [int(row[1]) for row in downbeats]
        downbeat_idxs = [i for i, n in enumerate(beat_numbers) if n == 1]
        marks = []
        for j, i in enumerate(downbeat_idxs):
            bar_end = downbeat_idxs[j + 1] if j + 1 < len(downbeat_idxs) else len(beat_numbers)
            bar_length = max(beat_numbers[i:bar_end])
            marks.append(TimingMark(
                time_ms=int(round(float(downbeats[i][0]) * 1000)), confidence=None,
                label=str(bar_length),
            ))
        return TimingTrack(
            name=self.name,
            algorithm_name=self.name,
            element_type=self.element_type,
            marks=marks,
            quality_score=0.0,
        )
