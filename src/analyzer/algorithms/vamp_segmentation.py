"""Segmentino Vamp plugin wrapper: automatic structural segmentation."""
from __future__ import annotations

import numpy as np

from src.analyzer.algorithms.base import Algorithm
from src.analyzer.result import TimingMark, TimingTrack

__all__ = [
    "SegmentinoAlgorithm",
]


class SegmentinoAlgorithm(Algorithm):
    """Segmentino structural segmenter — groups repeated sections."""

    name = "segmentino"
    element_type = "structure"
    library = "vamp"
    plugin_key = "segmentino:segmentino"
    parameters = {}
    preferred_stem = "full_mix"
    depends_on = ["audio_load"]

    # Explicit output selection, matching every other structural/multi-output
    # Vamp wrapper in this codebase (vamp_structure.py's QMSegmenterAlgorithm,
    # vamp_beats.py, vamp_onsets.py, vamp_pitch.py, vamp_harmony.py all pass
    # output=). Segmentino was the one wrapper relying on Vamp's default
    # output, which for a multi-output plugin is not guaranteed to be the
    # labelled one — this silently produced unlabeled boundary marks (no
    # "label" key at all in the exported JSON, since result.py's
    # _mark_to_dict omits None-valued keys), forcing every song through the
    # weaker energy-only classification fallback in section_classifier.py.
    # "segmentation" matches QMSegmenterAlgorithm's own vamp_output value
    # (the sibling C4DM structural segmenter) — confirm this is correct
    # against the real plugin (`vamp.list_outputs_of("segmentino:segmentino")`
    # in the Linux dev container; `vamp` has no native host library outside
    # it) before relying on labels being present. See
    # openspec/changes/segmentino-label-extraction/.
    vamp_output = "segmentation"

    def _run(self, audio: np.ndarray, sample_rate: int) -> TimingTrack:
        import vamp
        outputs = vamp.collect(
            audio, sample_rate, self.plugin_key,
            output=self.vamp_output,
            parameters=self.parameters,
        )
        items = outputs.get("list", [])
        marks = []
        for item in items:
            if isinstance(item, dict):
                ts = item.get("timestamp")
                raw_label = item.get("label")
                raw_duration = item.get("duration")
            else:
                ts = getattr(item, "timestamp", None)
                raw_label = getattr(item, "label", None)
                raw_duration = getattr(item, "duration", None)

            if ts is None:
                continue

            ms = int(round(float(ts) * 1000))
            label = str(raw_label).strip() if raw_label is not None else None
            duration_ms: int | None = None
            if raw_duration is not None:
                try:
                    dur_sec = raw_duration.to_float() if hasattr(raw_duration, "to_float") else float(raw_duration)
                    duration_ms = int(round(dur_sec * 1000))
                except (TypeError, ValueError):
                    pass

            marks.append(TimingMark(
                time_ms=ms,
                confidence=None,
                label=label or None,
                duration_ms=duration_ms,
            ))
        return TimingTrack(
            name=self.name, algorithm_name=self.name,
            element_type=self.element_type, marks=marks, quality_score=0.0,
        )
