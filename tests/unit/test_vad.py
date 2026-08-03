"""Tests for src/analyzer/vad.py — Silero VAD wrapper.

silero_vad is mocked via sys.modules throughout (same pattern used for
whisperx elsewhere) so these tests don't depend on the real package/model
being installed, and run fast regardless.
"""
from __future__ import annotations

import sys
from unittest.mock import MagicMock

import numpy as np
import pytest

import src.analyzer.vad as vad_module


@pytest.fixture(autouse=True)
def _reset_cached_model():
    """detect_speech_regions caches the loaded model at module scope --
    reset between tests so mocks don't leak across them."""
    vad_module._model = None
    yield
    vad_module._model = None


def _install_fake_silero_vad(monkeypatch, timestamps, raise_on_load=False, raise_on_detect=False):
    fake_module = MagicMock()
    if raise_on_load:
        fake_module.load_silero_vad.side_effect = RuntimeError("model load failed")
    else:
        fake_module.load_silero_vad.return_value = MagicMock(name="model")
    if raise_on_detect:
        fake_module.get_speech_timestamps.side_effect = RuntimeError("inference failed")
    else:
        fake_module.get_speech_timestamps.return_value = timestamps
    monkeypatch.setitem(sys.modules, "silero_vad", fake_module)
    return fake_module


class TestDetectSpeechRegions:
    def test_returns_start_end_pairs(self, monkeypatch):
        _install_fake_silero_vad(
            monkeypatch,
            [{"start": 3.5, "end": 6.2}, {"start": 8.0, "end": 9.1}],
        )
        audio = np.zeros(16000, dtype=np.float32)
        regions = vad_module.detect_speech_regions(audio, sample_rate=16000)
        assert regions == [(3.5, 6.2), (8.0, 9.1)]

    def test_empty_timestamps_returns_empty_list_not_none(self, monkeypatch):
        _install_fake_silero_vad(monkeypatch, [])
        audio = np.zeros(16000, dtype=np.float32)
        assert vad_module.detect_speech_regions(audio) == []

    def test_returns_none_when_package_not_installed(self, monkeypatch):
        monkeypatch.setitem(sys.modules, "silero_vad", None)
        audio = np.zeros(16000, dtype=np.float32)
        assert vad_module.detect_speech_regions(audio) is None

    def test_returns_none_on_model_load_failure(self, monkeypatch):
        _install_fake_silero_vad(monkeypatch, [], raise_on_load=True)
        audio = np.zeros(16000, dtype=np.float32)
        assert vad_module.detect_speech_regions(audio) is None

    def test_returns_none_on_detection_failure(self, monkeypatch):
        _install_fake_silero_vad(monkeypatch, [], raise_on_detect=True)
        audio = np.zeros(16000, dtype=np.float32)
        assert vad_module.detect_speech_regions(audio) is None

    def test_model_loaded_once_and_reused(self, monkeypatch):
        fake = _install_fake_silero_vad(monkeypatch, [{"start": 1.0, "end": 2.0}])
        audio = np.zeros(16000, dtype=np.float32)
        vad_module.detect_speech_regions(audio)
        vad_module.detect_speech_regions(audio)
        assert fake.load_silero_vad.call_count == 1
        assert fake.get_speech_timestamps.call_count == 2
