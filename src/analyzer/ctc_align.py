"""Second-opinion lyric word alignment via ctc-forced-aligner (MMS/Wav2Vec2).

Used by ``phoneme_align.realign_lyric_lines`` as a fallback for lyric lines
that WhisperX's forced alignment couldn't match to any word (see
``phonemes.PhonemeAnalyzer.analyze``: WhisperX drops low-confidence words
from its output entirely rather than returning a null-timed placeholder, so
some lines end up with zero aligned words even when the audio's coverage as
a whole is decent). Unlike WhisperX's alignment, ctc-forced-aligner's CTC
segmentation always places every input word somewhere on the timeline (via a
``<star>`` token for non-speech gaps) — it never drops words — so it can
recover timing for lines WhisperX skipped entirely, at the cost of being a
general-purpose (not singing-adapted) aligner.

Optional dependency — degrades to ``None`` (no fallback, caller keeps
whatever WhisperX/provider timing it already had) when ``ctc_forced_aligner``
isn't installed or alignment fails for any reason. Never raises.
"""
from __future__ import annotations

from typing import Optional

from src.log import get_logger

log = get_logger("xlight.ctc_align")

_model = None
_tokenizer = None


def _get_model(device: str):
    global _model, _tokenizer
    if _model is None:
        import torch
        from ctc_forced_aligner import load_alignment_model

        _model, _tokenizer = load_alignment_model(device, dtype=torch.float32)
    return _model, _tokenizer


def align_words(
    audio_path: str, text: str, language: str = "eng", device: str = "cpu",
) -> Optional[list[dict]]:
    """Force-align every word of ``text`` (space-separated) against the audio
    at ``audio_path``, returning ``[{"label": WORD, "start_ms": int,
    "end_ms": int}, ...]`` in the same order as ``text``'s words, one entry
    per word with no drops — or ``None`` if unavailable/failed.

    ``language`` is an ISO 639-3 code (default "eng"). Romanization is
    always enabled since the default MMS alignment model expects it
    regardless of language.
    """
    try:
        from ctc_forced_aligner import (
            generate_emissions,
            get_alignments,
            get_spans,
            load_audio,
            postprocess_results,
            preprocess_text,
        )

        model, tokenizer = _get_model(device)

        audio_waveform = load_audio(audio_path, model.dtype, model.device)
        emissions, stride = generate_emissions(model, audio_waveform, batch_size=4)

        tokens_starred, text_starred = preprocess_text(
            text, romanize=True, language=language,
        )
        segments, scores, blank_token = get_alignments(emissions, tokens_starred, tokenizer)
        spans = get_spans(tokens_starred, segments, blank_token)
        word_timestamps = postprocess_results(text_starred, spans, stride, scores)

        return [
            {
                "label": w["text"].upper(),
                "start_ms": int(round(w["start"] * 1000)),
                "end_ms": int(round(w["end"] * 1000)),
            }
            for w in word_timestamps
        ]
    except ImportError:
        return None
    except Exception as exc:
        log.warning("ctc-forced-aligner alignment failed: %s", exc)
        return None
