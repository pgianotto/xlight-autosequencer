"""Vocal phoneme analysis: WhisperX transcription + cmudict decomposition."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from src.log import get_logger

log = get_logger("xlight.phonemes")


# ── Data Classes ──────────────────────────────────────────────────────────────

@dataclass
class WordMark:
    """A single word with start/end timing from WhisperX alignment."""

    label: str       # uppercased word text, e.g. "HOLIDAY"
    start_ms: int
    end_ms: int

    def __post_init__(self) -> None:
        self.start_ms = int(self.start_ms)
        self.end_ms = int(self.end_ms)

    def to_dict(self) -> dict:
        return {"label": self.label, "start_ms": self.start_ms, "end_ms": self.end_ms}

    @classmethod
    def from_dict(cls, d: dict) -> "WordMark":
        return cls(label=d["label"], start_ms=d["start_ms"], end_ms=d["end_ms"])


@dataclass
class PhonemeMark:
    """A single Papagayo mouth-shape phoneme with start/end timing."""

    label: str       # one of: AI, E, O, WQ, L, MBP, FV, etc
    start_ms: int
    end_ms: int

    def __post_init__(self) -> None:
        self.start_ms = int(self.start_ms)
        self.end_ms = int(self.end_ms)

    def to_dict(self) -> dict:
        return {"label": self.label, "start_ms": self.start_ms, "end_ms": self.end_ms}

    @classmethod
    def from_dict(cls, d: dict) -> "PhonemeMark":
        return cls(label=d["label"], start_ms=d["start_ms"], end_ms=d["end_ms"])


@dataclass
class WordTrack:
    """Collection of word-level timing marks."""

    name: str
    marks: list[WordMark]
    lyrics_source: str = "auto"   # "auto" | "provided"

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "lyrics_source": self.lyrics_source,
            "marks": [m.to_dict() for m in self.marks],
        }

    @classmethod
    def from_dict(cls, d: dict) -> "WordTrack":
        return cls(
            name=d["name"],
            marks=[WordMark.from_dict(m) for m in d.get("marks", [])],
            lyrics_source=d.get("lyrics_source", "auto"),
        )


@dataclass
class PhonemeTrack:
    """Collection of phoneme-level timing marks."""

    name: str
    marks: list[PhonemeMark]

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "marks": [m.to_dict() for m in self.marks],
        }

    @classmethod
    def from_dict(cls, d: dict) -> "PhonemeTrack":
        return cls(
            name=d["name"],
            marks=[PhonemeMark.from_dict(m) for m in d.get("marks", [])],
        )


@dataclass
class LyricsBlock:
    """Full concatenated lyrics as a single block for EffectLayer 1."""

    text: str
    start_ms: int
    end_ms: int

    def __post_init__(self) -> None:
        self.start_ms = int(self.start_ms)
        self.end_ms = int(self.end_ms)

    def to_dict(self) -> dict:
        return {"text": self.text, "start_ms": self.start_ms, "end_ms": self.end_ms}

    @classmethod
    def from_dict(cls, d: dict) -> "LyricsBlock":
        return cls(text=d["text"], start_ms=d["start_ms"], end_ms=d["end_ms"])


@dataclass
class PhonemeResult:
    """All phoneme analysis output for one audio file."""

    lyrics_block: LyricsBlock
    word_track: WordTrack
    phoneme_track: PhonemeTrack
    source_file: str
    language: str
    model_name: str

    def to_dict(self) -> dict:
        return {
            "lyrics_block": self.lyrics_block.to_dict(),
            "word_track": self.word_track.to_dict(),
            "phoneme_track": self.phoneme_track.to_dict(),
            "source_file": self.source_file,
            "language": self.language,
            "model_name": self.model_name,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "PhonemeResult":
        return cls(
            lyrics_block=LyricsBlock.from_dict(d["lyrics_block"]),
            word_track=WordTrack.from_dict(d["word_track"]),
            phoneme_track=PhonemeTrack.from_dict(d["phoneme_track"]),
            source_file=d.get("source_file", ""),
            language=d.get("language", "en"),
            model_name=d.get("model_name", "base"),
        )


# ── ARPAbet → Papagayo mapping ────────────────────────────────────────────────

# Maps ARPAbet phoneme codes (without stress digits) to Preston Blair
# phoneme set, matching xLights' phoneme_mapping file exactly.
# Reference: github.com/xLightsSequencer/xLights/blob/master/bin/phoneme_mapping
_ARPABET_TO_PAPAGAYO: dict[str, str] = {
    # AI — wide open vowel sounds (odd, at, hut, hide, it)
    "AA": "AI", "AE": "AI", "AH": "AI", "AY": "AI", "IH": "AI",
    # E — mid open vowel sounds (Ed, hurt, ate, eat)
    "EH": "E", "ER": "E", "EY": "E", "IY": "E",
    # O — round vowel sounds (ought, oat, cow)
    "AO": "O", "OW": "O", "AW": "O",
    # U — rounded closed vowel sounds (hood, two)
    "UH": "U", "UW": "U",
    # WQ — pursed lip sounds (we, toy)
    "W": "WQ", "OY": "WQ",
    # L — tongue forward (lee)
    "L": "L",
    # MBP — closed lips (be, me, pee)
    "M": "MBP", "B": "MBP", "P": "MBP",
    # FV — teeth on lip (fee, vee)
    "F": "FV", "V": "FV",
    # etc — CDGKNRSThYZ: all other consonants
    "CH": "etc", "D": "etc", "DH": "etc", "G": "etc", "HH": "etc",
    "JH": "etc", "K": "etc", "N": "etc", "NG": "etc", "R": "etc",
    "S": "etc", "SH": "etc", "T": "etc", "TH": "etc", "Y": "etc",
    "Z": "etc", "ZH": "etc",
    # Silence / xLights bug-fix phonemes
    "SIL": "rest", "SP": "rest", "E21": "E",
}

_VOWEL_LABELS: frozenset[str] = frozenset({"AI", "E", "O", "U"})
_VOWEL_LETTERS: frozenset[str] = frozenset("aeiouAEIOU")


def _strip_stress(arpabet: str) -> str:
    """Remove trailing stress digit from ARPAbet code, e.g. 'AA1' → 'AA'."""
    return arpabet.rstrip("012")


def arpabet_to_papagayo(arpabet: str) -> str:
    """Map a single ARPAbet phoneme (with or without stress digit) to a Papagayo label."""
    return _ARPABET_TO_PAPAGAYO.get(_strip_stress(arpabet.upper()), "etc")


def _unknown_word_fallback(word: str) -> list[str]:
    """Rough letter-by-letter phoneme approximation for words not in cmudict."""
    result = []
    for ch in word.upper():
        if not ch.isalpha():
            continue
        result.append("AI" if ch in _VOWEL_LETTERS else "etc")
    return result or ["etc"]


def word_to_papagayo(word: str, cmu_dict: dict) -> list[str]:
    """
    Convert a word to a list of Papagayo labels using cmudict.

    Falls back to letter-based approximation for unknown words.
    """
    key = word.lower()
    pronunciations = cmu_dict.get(key)
    if pronunciations:
        arpabet_phones = pronunciations[0]
        return [arpabet_to_papagayo(p) for p in arpabet_phones]
    return _unknown_word_fallback(word)


# ── Phoneme timing distribution ───────────────────────────────────────────────

_TRANSITION_MS = 50
_VOWEL_WEIGHT = 1.5
_CONSONANT_WEIGHT = 0.75


def distribute_phoneme_timing(
    papagayo_phonemes: list[str],
    start_ms: int,
    end_ms: int,
) -> list[PhonemeMark]:
    """
    Distribute phoneme durations across [start_ms, end_ms].

    Vowels (AI, E, O, U) get 1.5× weight; consonants get 0.75× weight.
    Consecutive identical labels are merged first.
    """
    duration = end_ms - start_ms
    if not papagayo_phonemes or duration <= 0:
        return []

    # Merge consecutive identical labels (e.g. etc, etc, etc → single etc)
    merged: list[str] = [papagayo_phonemes[0]]
    for p in papagayo_phonemes[1:]:
        if p != merged[-1]:
            merged.append(p)

    weights = [
        _VOWEL_WEIGHT if lbl in _VOWEL_LABELS else _CONSONANT_WEIGHT
        for lbl in merged
    ]
    total_weight = sum(weights) or 1.0

    durations: list[int] = []
    for w in weights:
        d = round((w / total_weight) * duration)
        durations.append(max(1, d))

    # Correct rounding drift
    total = sum(durations)
    if durations and total != duration:
        durations[-1] = max(1, durations[-1] + (duration - total))

    marks: list[PhonemeMark] = []
    t = start_ms
    for lbl, d in zip(merged, durations):
        marks.append(PhonemeMark(label=lbl, start_ms=t, end_ms=t + d))
        t += d

    return marks


# ── Timed lyrics parsing (per-line WhisperX alignment segment hints) ──────────

_TIMED_LINE_RE = re.compile(r"^\[(\d+)\](.*)$")

# Slack applied around each line's provider-supplied timestamp when it
# becomes a WhisperX alignment segment boundary (see _align_with_lyrics) --
# absorbs the provider's own small timing error without reverting to the
# "one segment spans the whole song" behavior this whole mechanism exists
# to avoid.
_SEGMENT_PAD_S = 1.5


def _parse_timed_lyrics(raw: str) -> Optional[list[tuple[int, str]]]:
    """Parse the ``[<t_ms>]<text>`` format written by
    ``phoneme_align._lyric_lines_to_text``.

    Returns ``None`` (not an empty list) when the text doesn't look like
    this format at all — e.g. plain user-pasted lyrics with no timing —
    so callers can distinguish "no timing available, fall back to a
    single whole-song segment" from "timed, but every line was blank".
    """
    lines: list[tuple[int, str]] = []
    for raw_line in raw.splitlines():
        raw_line = raw_line.strip()
        if not raw_line:
            continue
        m = _TIMED_LINE_RE.match(raw_line)
        if not m:
            return None
        t_ms, text = m.groups()
        text = text.strip()
        if text:
            lines.append((int(t_ms), text))
    return lines if lines else None


def _refine_boundary_with_vad(
    estimate: float, raw_value: float, floor: float, ceiling: float,
    candidates: list[float],
) -> float:
    """Snap a padding-based segment boundary ``estimate`` to real VAD
    evidence (a speech onset/offset timestamp) when one exists nearby.

    ``candidates`` is every VAD speech-region start (for a segment's start)
    or end (for a segment's end) across the whole song. Restricted to
    ``[floor, ceiling]`` (the same non-overlapping half used to build
    ``estimate``) so a refinement can never cross into a neighboring
    segment's territory; among those, picks whichever is closest to
    ``raw_value`` (the lyrics provider's own approximate timestamp) rather
    than just the first/last, since a gap can contain more than one VAD
    region (e.g. a brief non-vocal break within the same line's span).
    Returns ``estimate`` unchanged when no candidate qualifies.
    """
    in_range = [c for c in candidates if floor <= c <= ceiling]
    if not in_range:
        return estimate
    return min(in_range, key=lambda c: abs(c - raw_value))


# ── PhonemeAnalyzer ───────────────────────────────────────────────────────────

class PhonemeAnalyzer:
    """
    Analyse vocal phonemes from an audio stem using WhisperX + cmudict.

    Usage::

        analyzer = PhonemeAnalyzer(model_name="base")
        result = analyzer.analyze(vocal_stem_path, source_file)
    """

    def __init__(self, model_name: str = "base", device: str = "cpu", language: str = "en") -> None:
        self.model_name = model_name
        self.device = device
        self.language = language
        self._cmu_dict: dict | None = None

    def _get_cmu_dict(self) -> dict:
        if self._cmu_dict is None:
            import nltk
            nltk.download("cmudict", quiet=True)
            from nltk.corpus import cmudict as _cmudict
            self._cmu_dict = _cmudict.dict()
        return self._cmu_dict

    def analyze(
        self,
        audio_path: str,
        source_file: str,
        lyrics_path: Optional[str] = None,
    ) -> Optional[PhonemeResult]:
        """
        Transcribe and align vocals, decompose into phonemes.

        Returns None if no vocals detected.
        Raises RuntimeError if whisperx is not installed.

        warnings: list of warning strings is attached as analyze.warnings attribute.
        """
        warnings: list[str] = []
        self.warnings = warnings

        try:
            import whisperx
        except ImportError:
            raise RuntimeError(
                "whisperx is required for phoneme analysis. "
                "Install it with: pip install whisperx"
            )

        cmu_dict = self._get_cmu_dict()

        # Load whisperx model — pass language hint to skip auto-detection
        load_lang = self.language if self.language != "auto" else None
        model = whisperx.load_model(
            self.model_name, self.device, compute_type="float32",
            language=load_lang,
        )

        audio = whisperx.load_audio(audio_path)
        duration_s = len(audio) / 16000  # whisperx resamples to 16kHz

        if lyrics_path is not None:
            word_segments, language, lyrics_source = self._align_with_lyrics(
                audio, audio_path, lyrics_path, model, warnings, duration_s
            )
        else:
            word_segments, language, lyrics_source = self._transcribe_and_align(
                audio, audio_path, model, warnings
            )

        if not word_segments:
            warnings.append("No vocals detected in audio. Skipping phoneme analysis.")
            return None

        # Build WordMarks from aligned word segments
        word_marks: list[WordMark] = []
        for ws in word_segments:
            word = ws.get("word", "").strip()
            start = ws.get("start")
            end = ws.get("end")
            if not word or start is None or end is None:
                continue
            word_marks.append(
                WordMark(
                    label=word.upper(),
                    start_ms=int(round(start * 1000)),
                    end_ms=int(round(end * 1000)),
                )
            )

        if not word_marks:
            warnings.append("No vocals detected in audio. Skipping phoneme analysis.")
            return None

        log.info("WhisperX aligned %d words, time range %dms–%dms",
                 len(word_marks), word_marks[0].start_ms, word_marks[-1].end_ms)
        # Log words in the 2:00–2:30 range to debug guitar solo hallucination
        solo_words = [wm for wm in word_marks if 120_000 <= wm.start_ms <= 150_000]
        if solo_words:
            log.warning(
                "Words in 2:00-2:30 range (potential guitar solo): %s",
                [(wm.label, wm.start_ms, wm.end_ms) for wm in solo_words],
            )

        # Decompose words into phonemes
        phoneme_marks: list[PhonemeMark] = []
        for wm in word_marks:
            papagayo = word_to_papagayo(wm.label, cmu_dict)
            phoneme_marks.extend(
                distribute_phoneme_timing(papagayo, wm.start_ms, wm.end_ms)
            )

        lyrics_text = " ".join(wm.label for wm in word_marks)
        lyrics_block = LyricsBlock(
            text=lyrics_text,
            start_ms=word_marks[0].start_ms,
            end_ms=word_marks[-1].end_ms,
        )

        return PhonemeResult(
            lyrics_block=lyrics_block,
            word_track=WordTrack(
                name="whisperx-words",
                marks=word_marks,
                lyrics_source=lyrics_source,
            ),
            phoneme_track=PhonemeTrack(
                name="whisperx-phonemes",
                marks=phoneme_marks,
            ),
            source_file=source_file,
            language=language,
            model_name=self.model_name,
        )

    def _transcribe_and_align(
        self,
        audio,
        audio_path: str,
        model,
        warnings: list[str],
    ) -> tuple[list[dict], str, str]:
        """Transcribe audio and align words. Returns (word_segments, language, lyrics_source)."""
        import whisperx

        result = model.transcribe(audio, batch_size=4)
        language = result.get("language", "en")

        segments = result.get("segments", [])
        if not segments:
            return [], language, "auto"

        align_model, metadata = whisperx.load_align_model(
            language_code=language, device=self.device
        )
        aligned = whisperx.align(segments, align_model, metadata, audio, self.device)
        word_segments = aligned.get("word_segments", [])
        return word_segments, language, "auto"

    def _align_with_lyrics(
        self,
        audio,
        audio_path: str,
        lyrics_path: str,
        model,
        warnings: list[str],
        duration_s: float,
    ) -> tuple[list[dict], str, str]:
        """Align provided lyrics to audio. Falls back to audio-only on mismatch."""
        import whisperx

        try:
            with open(lyrics_path, "r", encoding="utf-8") as fh:
                raw = fh.read()
        except OSError as exc:
            warnings.append(f"Cannot read lyrics file: {exc}. Falling back to audio-only.")
            return self._transcribe_and_align(audio, audio_path, model, warnings)

        # phoneme_align._lyric_lines_to_text emits "[<t_ms>]<text>" lines
        # when it has real per-line timing (session lyric_lines); a plain
        # user-pasted/untimed lyrics_text has no such prefix. When timed,
        # use each line's approximate timestamp as a per-line WhisperX
        # alignment segment boundary instead of one segment spanning the
        # whole song -- without this, a long instrumental intro before the
        # first sung line can make naive whole-song forced alignment
        # anchor early words near the start of the song instead of where
        # the singing actually begins (found 2026-08-03).
        timed_lines = _parse_timed_lyrics(raw)

        words: list[str]
        segments: list[dict]
        if timed_lines is not None:
            # Clamp every raw timestamp to duration_s up front -- a
            # lyrics-provider timestamp past the actual audio length
            # (bad/stale provider data) must not produce a segment
            # boundary outside the audio WhisperX is aligning against.
            raw_starts = [min(t_ms / 1000.0, duration_s) for t_ms, _ in timed_lines]

            # Real acoustic evidence of where singing actually starts/stops,
            # when available -- strictly better than the fixed padding
            # tolerance below, which is just a blind guess around the
            # provider's own approximate timestamp. Computed once for the
            # whole song (Silero VAD is fast) rather than per line.
            from src.analyzer.vad import detect_speech_regions
            vad_regions = detect_speech_regions(audio, sample_rate=16000)
            vad_starts = [s for s, _e in vad_regions] if vad_regions else []
            vad_ends = [e for _s, e in vad_regions] if vad_regions else []

            words = []
            segments = []
            for i, (_t_ms, text) in enumerate(timed_lines):
                line_words = re.sub(r"[^a-zA-Z\s']", " ", text).split()
                if not line_words:
                    continue
                raw_start_s = raw_starts[i]
                raw_end_s = raw_starts[i + 1] if i + 1 < len(raw_starts) else duration_s

                # Non-overlapping boundary: split the gap to the adjacent
                # line's raw timestamp at the midpoint first, THEN apply
                # padding within that half -- padding independently from
                # each line's own timestamp (without this) can push a
                # segment's end past the *next* segment's padded start
                # when two lines are close together, silently overlapping
                # them (caught by test_timed_lyrics_build_per_line_
                # segments_not_one_whole_song_segment). For a large gap
                # (the actual bug this whole mechanism fixes) the pad is
                # the tighter bound instead, giving a small buffer around
                # the line's own timestamp rather than half the gap.
                left_boundary = 0.0 if i == 0 else (raw_starts[i - 1] + raw_start_s) / 2.0
                right_boundary = duration_s if i == len(raw_starts) - 1 else (raw_start_s + raw_end_s) / 2.0

                start_s = max(left_boundary, raw_start_s - _SEGMENT_PAD_S, 0.0)
                end_s = min(right_boundary, raw_end_s + _SEGMENT_PAD_S, duration_s)
                end_s = max(end_s, start_s + 0.05)

                # VAD refinement: if real speech onset/offset evidence
                # exists within this segment's non-overlapping half, snap
                # to whichever one sits closest to the provider's own
                # timestamp instead of the blind padding estimate.
                start_s = _refine_boundary_with_vad(
                    start_s, raw_start_s, left_boundary, right_boundary, vad_starts,
                )
                end_s = _refine_boundary_with_vad(
                    end_s, raw_end_s, left_boundary, right_boundary, vad_ends,
                )
                end_s = max(end_s, start_s + 0.05)

                segments.append({"text": " ".join(line_words), "start": start_s, "end": end_s})
                words.extend(line_words)
        else:
            # Untimed plain text (e.g. user-pasted fallback) -- no per-line
            # hints available, fall back to one segment spanning the audio.
            normalized = re.sub(r"[^a-zA-Z\s']", " ", raw).strip()
            words = normalized.split()
            segments = [{"text": " ".join(words), "start": 0.0, "end": duration_s}] if words else []

        log.info("_align_with_lyrics: %d words from lyrics file (%d segments), duration=%.1fs",
                 len(words), len(segments), duration_s)
        log.debug("_align_with_lyrics: first 20 words: %s", words[:20])
        if not words:
            warnings.append("Lyrics file is empty. Falling back to audio-only.")
            return self._transcribe_and_align(audio, audio_path, model, warnings)

        # Use pre-set language (default "en") — avoids a full transcription
        # pass just to detect language.  Set language="auto" to force detection.
        language = self.language
        if language == "auto":
            log.info("_align_with_lyrics: detecting language via transcription...")
            quick_result = model.transcribe(audio, batch_size=4)
            language = quick_result.get("language", "en")
        log.info("_align_with_lyrics: using language=%s", language)

        align_model, metadata = whisperx.load_align_model(
            language_code=language, device=self.device
        )
        aligned = whisperx.align(segments, align_model, metadata, audio, self.device)
        word_segments = aligned.get("word_segments", [])
        log.info("_align_with_lyrics: WhisperX returned %d word_segments", len(word_segments))

        # Mismatch detection: require >= 50% of provided words aligned
        aligned_count = sum(
            1 for ws in word_segments
            if ws.get("start") is not None and ws.get("end") is not None
        )
        coverage = aligned_count / max(len(words), 1)

        log.info("_align_with_lyrics: coverage=%.1f%% (%d/%d words aligned)",
                 coverage * 100, aligned_count, len(words))
        if coverage < 0.5:
            pct = int(coverage * 100)
            warnings.append(
                f"Lyrics mismatch — only {pct}% of words aligned. Falling back to audio-only."
            )
            return self._transcribe_and_align(audio, audio_path, model, warnings)

        return word_segments, language, "provided"
