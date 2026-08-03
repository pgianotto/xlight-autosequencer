"""Tests for meter (time signature) detection: orchestrator._label_beats'
pre-beat extrapolation and orchestrator._detect_time_signature.
"""
from __future__ import annotations

from src.analyzer.orchestrator import _detect_time_signature, _label_beats
from src.analyzer.result import TimingMark, TimingTrack


def _track(algorithm_name: str, times_ms: list[int]) -> TimingTrack:
    return TimingTrack(
        name=algorithm_name, algorithm_name=algorithm_name, element_type="bar",
        marks=[TimingMark(time_ms=t, confidence=None) for t in times_ms],
        quality_score=0.0,
    )


class TestLabelBeatsMeterAwarePickup:
    def test_pickup_beats_use_first_bars_own_count_not_hardcoded_four(self):
        # A 3/4 song: bars every 1500ms (3 beats @ 500ms), one pickup beat
        # before the first bar. Old code always labelled a lone pickup beat
        # "4" (hardcoded % 4); it should be "3" here since the bar it leads
        # into only has 3 beats.
        bars = _track("madmom_downbeats", [500, 2000, 3500])
        beats = _track("beats", [0, 500, 1000, 1500, 2000, 2500, 3000, 3500, 4000, 4500])
        _label_beats(beats, bars)
        pickup = next(m for m in beats.marks if m.time_ms == 0)
        assert pickup.label == "3"

    def test_pickup_beats_still_four_for_4_4_song(self):
        bars = _track("madmom_downbeats", [500, 2500])
        beats = _track("beats", [0, 500, 1000, 1500, 2000, 2500])
        _label_beats(beats, bars)
        pickup = next(m for m in beats.marks if m.time_ms == 0)
        assert pickup.label == "4"


class TestDetectTimeSignature:
    def test_detected_true_for_madmom_source(self):
        # 4 bars of 3/4 (500ms beats, 1500ms bars)
        bar_times = [0, 1500, 3000, 4500]
        beat_times = [i * 500 for i in range(12)]
        bars = _track("madmom_downbeats", bar_times)
        beats = _track("beats", beat_times)
        result = _detect_time_signature(bars, beats)
        assert result == {
            "beats_per_bar": 3,
            "confidence": 1.0,
            "detected": True,
            "source": "madmom_downbeats",
        }

    def test_detected_false_for_algorithms_that_assume_meter(self):
        # qm_bars/librosa_bars structurally always produce 4 beats/bar --
        # the count still computes to 4, but "detected" must say this is an
        # assumption, not a measurement.
        bar_times = [0, 2000, 4000]
        beat_times = [i * 500 for i in range(9)]
        bars = _track("qm_bars", bar_times)
        beats = _track("beats", beat_times)
        result = _detect_time_signature(bars, beats)
        assert result["beats_per_bar"] == 4
        assert result["detected"] is False
        assert result["source"] == "qm_bars"

    def test_confidence_reflects_disagreement_across_bars(self):
        # 3 bars: two with 4 beats, one with only 3 (e.g. one bar mistracked).
        bar_times = [0, 1500, 3500, 5500]
        beat_times = [0, 500, 1000, 1500, 2000, 2500, 3000, 3500, 4000, 4500, 5000]
        bars = _track("madmom_downbeats", bar_times)
        beats = _track("beats", beat_times)
        result = _detect_time_signature(bars, beats)
        assert result["beats_per_bar"] == 4
        assert result["confidence"] == round(2 / 3, 4)

    def test_none_when_bars_or_beats_missing(self):
        bars = _track("madmom_downbeats", [0, 1000])
        empty_beats = TimingTrack(
            name="beats", algorithm_name="beats", element_type="beat",
            marks=[], quality_score=0.0,
        )
        assert _detect_time_signature(None, empty_beats) is None
        assert _detect_time_signature(bars, None) is None
        assert _detect_time_signature(bars, empty_beats) is None

    def test_none_with_fewer_than_two_bars(self):
        bars = _track("madmom_downbeats", [0])
        beats = _track("beats", [0, 500, 1000])
        assert _detect_time_signature(bars, beats) is None
