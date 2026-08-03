"""Tests for src/analyzer/ctc_align.py — ctc-forced-aligner fallback wrapper.

ctc_forced_aligner is mocked via sys.modules throughout (same pattern used
for silero_vad in test_vad.py) so these tests don't depend on the real
package/model being installed, and run fast regardless.
"""
from __future__ import annotations

import sys
from unittest.mock import MagicMock

import pytest

import src.analyzer.ctc_align as ctc_align_module


@pytest.fixture(autouse=True)
def _reset_cached_model():
    ctc_align_module._model = None
    ctc_align_module._tokenizer = None
    yield
    ctc_align_module._model = None
    ctc_align_module._tokenizer = None


def _install_fake_ctc_forced_aligner(monkeypatch, word_timestamps, raise_on=None):
    fake_module = MagicMock()
    fake_model = MagicMock(dtype="float32", device="cpu")
    fake_tokenizer = MagicMock()
    fake_module.load_alignment_model.return_value = (fake_model, fake_tokenizer)
    fake_module.load_audio.return_value = MagicMock()
    fake_module.generate_emissions.return_value = (MagicMock(), 20.0)
    fake_module.preprocess_text.return_value = (MagicMock(), MagicMock())
    fake_module.get_alignments.return_value = (MagicMock(), MagicMock(), MagicMock())
    fake_module.get_spans.return_value = MagicMock()
    fake_module.postprocess_results.return_value = word_timestamps

    if raise_on:
        getattr(fake_module, raise_on).side_effect = RuntimeError("boom")

    monkeypatch.setitem(sys.modules, "ctc_forced_aligner", fake_module)
    return fake_module


class TestAlignWords:
    def test_returns_word_marks_in_ms(self, monkeypatch):
        _install_fake_ctc_forced_aligner(monkeypatch, [
            {"text": "hello", "start": 0.5, "end": 1.2},
            {"text": "world", "start": 1.2, "end": 1.9},
        ])
        result = ctc_align_module.align_words("song.wav", "hello world")
        assert result == [
            {"label": "HELLO", "start_ms": 500, "end_ms": 1200},
            {"label": "WORLD", "start_ms": 1200, "end_ms": 1900},
        ]

    def test_returns_none_when_package_not_installed(self, monkeypatch):
        monkeypatch.setitem(sys.modules, "ctc_forced_aligner", None)
        assert ctc_align_module.align_words("song.wav", "hello world") is None

    def test_returns_none_on_alignment_failure(self, monkeypatch):
        _install_fake_ctc_forced_aligner(monkeypatch, [], raise_on="generate_emissions")
        assert ctc_align_module.align_words("song.wav", "hello world") is None

    def test_model_loaded_once_and_reused(self, monkeypatch):
        fake = _install_fake_ctc_forced_aligner(monkeypatch, [
            {"text": "hi", "start": 0.0, "end": 0.5},
        ])
        ctc_align_module.align_words("song.wav", "hi")
        ctc_align_module.align_words("song.wav", "hi")
        assert fake.load_alignment_model.call_count == 1
        assert fake.generate_emissions.call_count == 2
