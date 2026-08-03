"""Tests for phoneme_align.align_words_and_phonemes' reference-text
selection (lyric_lines vs. lyrics_text vs. free transcription) -- the
actual WhisperX alignment itself is exercised by test_phonemes_lyrics.py
against PhonemeAnalyzer directly."""
from __future__ import annotations

from pathlib import Path

import src.analyzer.phoneme_align as phoneme_align


def _capture_run_in_process(monkeypatch, tmp_path):
    """Monkeypatch _run_in_process to record the lyrics_path it receives
    and, if present, that file's contents at call time -- captured before
    align_words_and_phonemes' finally-block deletes it."""
    captured: dict = {}

    def _fake_run_in_process(audio_path, lyrics_path):
        captured["lyrics_path"] = lyrics_path
        captured["content"] = Path(lyrics_path).read_text(encoding="utf-8") if lyrics_path else None
        return [], [], []

    monkeypatch.setattr(phoneme_align, "_run_in_process", _fake_run_in_process)
    monkeypatch.setattr(phoneme_align, "_discover_vocals_stem", lambda audio_path: None)
    return captured


class TestReferenceTextSelection:
    def test_no_lines_no_text_means_free_transcription(self, monkeypatch, tmp_path):
        captured = _capture_run_in_process(monkeypatch, tmp_path)
        phoneme_align.align_words_and_phonemes("song.mp3")
        assert captured["lyrics_path"] is None

    def test_lyric_lines_alone_forces_alignment(self, monkeypatch, tmp_path):
        captured = _capture_run_in_process(monkeypatch, tmp_path)
        lines = [{"t_ms": 0, "duration_ms": 1000, "text": "first line"},
                 {"t_ms": 1000, "duration_ms": 1000, "text": "second line"}]
        phoneme_align.align_words_and_phonemes("song.mp3", lines)
        assert captured["lyrics_path"] is not None
        # Timestamped format ([t_ms]text) carries each line's approximate
        # start as a per-line segment boundary hint for WhisperX alignment
        # -- see _lyric_lines_to_text.
        assert captured["content"] == "[0]first line\n[1000]second line"

    def test_lyrics_text_alone_forces_alignment(self, monkeypatch, tmp_path):
        # The bug this fixes: a user-pasted lyrics fallback has no timed
        # lyric_lines (plain text produces no per-line timing), so before
        # this fix it fell through to free transcription -- garbage words
        # (user-confirmed, 2026-07-21) despite real lyrics being available.
        captured = _capture_run_in_process(monkeypatch, tmp_path)
        pasted_text = "verse line one\nverse line two\nchorus line here"
        phoneme_align.align_words_and_phonemes("song.mp3", None, pasted_text)
        assert captured["lyrics_path"] is not None
        assert captured["content"] == pasted_text

    def test_empty_lyric_lines_falls_back_to_lyrics_text(self, monkeypatch, tmp_path):
        captured = _capture_run_in_process(monkeypatch, tmp_path)
        phoneme_align.align_words_and_phonemes("song.mp3", [], "pasted text")
        assert captured["content"] == "pasted text"

    def test_lyric_lines_take_priority_over_lyrics_text(self, monkeypatch, tmp_path):
        captured = _capture_run_in_process(monkeypatch, tmp_path)
        lines = [{"t_ms": 0, "duration_ms": 1000, "text": "timed line"}]
        phoneme_align.align_words_and_phonemes("song.mp3", lines, "should not be used")
        assert captured["content"] == "[0]timed line"

    def test_blank_lyrics_text_means_free_transcription(self, monkeypatch, tmp_path):
        captured = _capture_run_in_process(monkeypatch, tmp_path)
        phoneme_align.align_words_and_phonemes("song.mp3", None, "   \n  ")
        assert captured["lyrics_path"] is None


class TestWarningsPropagation:
    """PhonemeAnalyzer's lyrics-mismatch warning must reach the caller
    instead of being silently discarded (user-reported 2026-07-21: pasted
    lyrics replaced with 'made up' words with no explanation)."""

    def test_warnings_from_run_in_process_are_returned(self, monkeypatch):
        monkeypatch.setattr(phoneme_align, "_discover_vocals_stem", lambda audio_path: None)
        monkeypatch.setattr(
            phoneme_align, "_run_in_process",
            lambda audio_path, lyrics_path: (
                [], [], ["Lyrics mismatch — only 30% of words aligned. Falling back to audio-only."]
            ),
        )
        _, _, warnings = phoneme_align.align_words_and_phonemes("song.mp3", None, "pasted text")
        assert warnings == ["Lyrics mismatch — only 30% of words aligned. Falling back to audio-only."]

    def test_no_warnings_returns_empty_list(self, monkeypatch):
        monkeypatch.setattr(phoneme_align, "_discover_vocals_stem", lambda audio_path: None)
        monkeypatch.setattr(
            phoneme_align, "_run_in_process",
            lambda audio_path, lyrics_path: ([], [], []),
        )
        _, _, warnings = phoneme_align.align_words_and_phonemes("song.mp3")
        assert warnings == []


class TestRealignLyricLines:
    """realign_lyric_lines() corrects the Timeline's per-line lyric
    timestamps using WhisperX's forced-aligned word marks instead of the
    lyrics provider's raw (often approximate) line timestamps -- the same
    alignment pass the Words/Phonemes tracks already use (2026-08-02)."""

    def test_empty_inputs_returned_unchanged(self):
        assert phoneme_align.realign_lyric_lines([], []) == []
        lines = [{"t_ms": 0, "duration_ms": 1000, "text": "hello"}]
        assert phoneme_align.realign_lyric_lines(lines, []) == lines
        assert phoneme_align.realign_lyric_lines([], [{"label": "HI", "start_ms": 0, "end_ms": 500}]) == []

    def test_splits_aligned_words_back_into_lines_by_word_count(self):
        lines = [
            {"t_ms": 0, "duration_ms": 20000, "text": "la la placeholder"},
            {"t_ms": 20000, "duration_ms": 5000, "text": "line two here"},
        ]
        # Aligned word timestamps ground truth is very different from the
        # provider's original line timestamps above -- that's the point.
        words = [
            {"label": "LA", "start_ms": 660, "end_ms": 1000},
            {"label": "LA", "start_ms": 1000, "end_ms": 1300},
            {"label": "PLACEHOLDER", "start_ms": 1300, "end_ms": 2500},
            {"label": "LINE", "start_ms": 14190, "end_ms": 14500},
            {"label": "TWO", "start_ms": 14500, "end_ms": 14800},
            {"label": "HERE", "start_ms": 14800, "end_ms": 15200},
        ]
        corrected = phoneme_align.realign_lyric_lines(lines, words)
        assert corrected == [
            {"t_ms": 660, "duration_ms": 2500 - 660, "text": "la la placeholder"},
            {"t_ms": 14190, "duration_ms": 15200 - 14190, "text": "line two here"},
        ]

    def test_word_count_mismatch_still_corrects_matched_lines(self):
        # WhisperX dropped one "LA" during alignment -- total counts
        # disagree, but each line still has matched words and should still
        # be corrected from them (2026-08-03: an exact-count requirement
        # here meant any dropped word silently discarded every alignment).
        lines = [
            {"t_ms": 0, "duration_ms": 5000, "text": "la la placeholder"},
            {"t_ms": 5000, "duration_ms": 5000, "text": "line two here"},
        ]
        words = [
            {"label": "LA", "start_ms": 660, "end_ms": 1000},
            {"label": "PLACEHOLDER", "start_ms": 1000, "end_ms": 2500},
            {"label": "LINE", "start_ms": 14190, "end_ms": 14500},
            {"label": "TWO", "start_ms": 14500, "end_ms": 14800},
            {"label": "HERE", "start_ms": 14800, "end_ms": 15200},
        ]
        corrected = phoneme_align.realign_lyric_lines(lines, words)
        assert corrected == [
            {"t_ms": 660, "duration_ms": 2500 - 660, "text": "la la placeholder"},
            {"t_ms": 14190, "duration_ms": 15200 - 14190, "text": "line two here"},
        ]

    def test_line_with_zero_matched_words_keeps_original_timing(self):
        # A line whose words never got aligned at all (e.g. drowned out by
        # instrumentation) shouldn't be guessed at -- keep its provider
        # timestamp instead of e.g. collapsing to a neighboring line's time.
        lines = [
            {"t_ms": 0, "duration_ms": 5000, "text": "la la placeholder"},
            {"t_ms": 5000, "duration_ms": 5000, "text": "unheard line here"},
        ]
        words = [
            {"label": "LA", "start_ms": 660, "end_ms": 1000},
            {"label": "LA", "start_ms": 1000, "end_ms": 1300},
            {"label": "PLACEHOLDER", "start_ms": 1300, "end_ms": 2500},
        ]
        corrected = phoneme_align.realign_lyric_lines(lines, words)
        assert corrected == [
            {"t_ms": 660, "duration_ms": 2500 - 660, "text": "la la placeholder"},
            {"t_ms": 5000, "duration_ms": 5000, "text": "unheard line here"},
        ]

    def test_line_with_no_matchable_words_kept_as_is(self):
        lines = [{"t_ms": 500, "duration_ms": 100, "text": "..."}]
        words: list[dict] = []
        assert phoneme_align.realign_lyric_lines(lines, words) == lines

    def test_fallback_words_cover_line_whisperx_missed_entirely(self):
        # WhisperX matched line 1 but found nothing at all for line 2 (e.g.
        # drowned out); the ctc-forced-aligner fallback (which never drops
        # words) still covers it.
        lines = [
            {"t_ms": 0, "duration_ms": 5000, "text": "la la placeholder"},
            {"t_ms": 5000, "duration_ms": 5000, "text": "unheard line here"},
        ]
        words = [
            {"label": "LA", "start_ms": 660, "end_ms": 1000},
            {"label": "LA", "start_ms": 1000, "end_ms": 1300},
            {"label": "PLACEHOLDER", "start_ms": 1300, "end_ms": 2500},
        ]
        fallback_words = [
            {"label": "LA", "start_ms": 500, "end_ms": 900},
            {"label": "LA", "start_ms": 900, "end_ms": 1200},
            {"label": "PLACEHOLDER", "start_ms": 1200, "end_ms": 2400},
            {"label": "UNHEARD", "start_ms": 14190, "end_ms": 14500},
            {"label": "LINE", "start_ms": 14500, "end_ms": 14800},
            {"label": "HERE", "start_ms": 14800, "end_ms": 15200},
        ]
        corrected = phoneme_align.realign_lyric_lines(lines, words, fallback_words)
        assert corrected == [
            # Primary (WhisperX) match still wins over the fallback for the
            # line it did cover.
            {"t_ms": 660, "duration_ms": 2500 - 660, "text": "la la placeholder"},
            {"t_ms": 14190, "duration_ms": 15200 - 14190, "text": "unheard line here"},
        ]

    def test_fallback_words_ignored_when_line_still_unmatched(self):
        lines = [{"t_ms": 500, "duration_ms": 100, "text": "silence"}]
        words = [{"label": "OTHER", "start_ms": 0, "end_ms": 100}]
        fallback_words = [{"label": "ALSO-UNRELATED", "start_ms": 0, "end_ms": 100}]
        assert phoneme_align.realign_lyric_lines(lines, words, fallback_words) == lines


class TestCtcFallbackWords:
    def test_no_text_returns_none(self, monkeypatch):
        monkeypatch.setattr(phoneme_align, "_discover_vocals_stem", lambda p: Path("vocals.mp3"))
        assert phoneme_align.ctc_fallback_words("song.mp3", []) is None
        assert phoneme_align.ctc_fallback_words("song.mp3", [{"text": ""}]) is None

    def test_no_vocals_stem_returns_none(self, monkeypatch):
        monkeypatch.setattr(phoneme_align, "_discover_vocals_stem", lambda p: None)
        lines = [{"t_ms": 0, "duration_ms": 1000, "text": "hello"}]
        assert phoneme_align.ctc_fallback_words("song.mp3", lines) is None

    def test_delegates_to_ctc_align_with_joined_text(self, monkeypatch, tmp_path):
        vocals = tmp_path / "vocals.mp3"
        vocals.touch()
        monkeypatch.setattr(phoneme_align, "_discover_vocals_stem", lambda p: vocals)
        captured = {}

        def _fake_align_words(audio_path, text, **kwargs):
            captured["audio_path"] = audio_path
            captured["text"] = text
            return [{"label": "HI", "start_ms": 0, "end_ms": 500}]

        import src.analyzer.ctc_align as ctc_align
        monkeypatch.setattr(ctc_align, "align_words", _fake_align_words)

        lines = [
            {"t_ms": 0, "duration_ms": 1000, "text": "first line"},
            {"t_ms": 1000, "duration_ms": 1000, "text": "second line"},
        ]
        result = phoneme_align.ctc_fallback_words("song.mp3", lines)
        assert captured["audio_path"] == str(vocals)
        assert captured["text"] == "first line second line"
        assert result == [{"label": "HI", "start_ms": 0, "end_ms": 500}]
