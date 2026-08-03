"""Word/phoneme alignment for singing faces — session-friendly wrapper.

Runs :class:`src.analyzer.phonemes.PhonemeAnalyzer` (WhisperX forced
alignment + cmudict decomposition) and returns plain mark dicts ready to
persist in an X-Onset session (``words`` / ``phonemes`` keys) and to embed
as .xsq timing tracks.

WhisperX may live in the main venv (Windows host) or in the ``.venv-vamp``
sidecar (devcontainer). This module tries an in-process run first and falls
back to a sidecar subprocess — the same pattern as
``src.story.builder._try_free_transcription``.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
from pathlib import Path
from typing import Optional

from src.log import get_logger

log = get_logger("xlight.phoneme_align")

_WORD_RE = re.compile(r"[a-zA-Z0-9']+")


def realign_lyric_lines(lyric_lines: list[dict], words: list[dict]) -> list[dict]:
    """Correct each lyric line's (t_ms, duration_ms) with WhisperX's
    forced-aligned word timestamps instead of the lyrics provider's raw
    line timestamps.

    ``align_words_and_phonemes`` force-aligns exactly the text ``lyric_lines``
    supplies (see ``_lyric_lines_to_text`` — one original line per newline).
    WhisperX doesn't always return timing for every reference word though —
    low-confidence words are dropped from its output entirely (see
    ``PhonemeAnalyzer.analyze``'s ``word_marks`` filter), so ``words`` is
    typically a *subsequence* of the full reference text, not a 1:1 match
    (found 2026-08-03: only 97 of 184 words aligned on one song — an exact
    total-count match, the original approach here, essentially never holds
    in practice and silently discarded every alignment).

    Aligns the flattened reference words against ``words`` by text (via
    :class:`difflib.SequenceMatcher`, which finds the matching blocks of an
    ordered subsequence — exactly what a forced aligner that only *drops*
    words, never reorders or invents them, produces) to recover which
    aligned word belongs to which original line despite the drops. Each
    line whose text matched at least one aligned word gets its
    ``t_ms``/``duration_ms`` set from that line's earliest/latest matched
    word; a line with zero matches keeps its original (provider) timing
    rather than guessing.
    """
    if not lyric_lines or not words:
        return lyric_lines

    def _tokens(text: str) -> list[str]:
        return [t.upper() for t in _WORD_RE.findall(text)]

    orig_tokens: list[str] = []
    orig_line_of: list[int] = []
    for li, line in enumerate(lyric_lines):
        for tok in _tokens(line.get("text", "")):
            orig_tokens.append(tok)
            orig_line_of.append(li)

    aligned_tokens = [w["label"] for w in words]
    if not orig_tokens or not aligned_tokens:
        return lyric_lines

    import difflib
    matcher = difflib.SequenceMatcher(a=orig_tokens, b=aligned_tokens, autojunk=False)

    line_start: dict[int, int] = {}
    line_end: dict[int, int] = {}
    for block in matcher.get_matching_blocks():
        for k in range(block.size):
            li = orig_line_of[block.a + k]
            w = words[block.b + k]
            start_ms, end_ms = w["start_ms"], w["end_ms"]
            if li not in line_start or start_ms < line_start[li]:
                line_start[li] = start_ms
            if li not in line_end or end_ms > line_end[li]:
                line_end[li] = end_ms

    corrected: list[dict] = []
    for li, line in enumerate(lyric_lines):
        if li in line_start:
            corrected.append({
                "t_ms": line_start[li],
                "duration_ms": max(line_end[li] - line_start[li], 1),
                "text": line.get("text", ""),
            })
        else:
            corrected.append(dict(line))
    return corrected

_SUBPROCESS_TIMEOUT_S = 600


def _discover_vocals_stem(audio_path: str) -> Path | None:
    """Find a cached vocals stem next to ``audio_path``, or None if absent.

    Mirrors ``src.story.builder._discover_vocals_stem`` (kept private there).
    """
    audio_p = Path(audio_path)
    for stem_dir in (
        audio_p.parent / "stems",
        audio_p.parent / ".stems",
        audio_p.parent / audio_p.stem / "stems",
        audio_p.parent / audio_p.stem / ".stems",
    ):
        for ext in ("mp3", "wav"):
            candidate = stem_dir / f"vocals.{ext}"
            if candidate.exists():
                return candidate
    return None


def _sidecar_python() -> Path | None:
    """Resolve the .venv-vamp interpreter, or None when no sidecar exists."""
    override = os.environ.get("XLIGHT_VENV_VAMP")
    if override:
        p = Path(override)
        return p if p.exists() else None
    repo_root = Path(__file__).resolve().parents[2]
    for rel in ("bin/python", "Scripts/python.exe"):
        candidate = repo_root / ".venv-vamp" / rel
        if candidate.exists():
            return candidate
    return None


def _lyric_lines_to_text(lyric_lines: list[dict]) -> str:
    """Flatten session lyric lines (``{t_ms, duration_ms, text}``) to a
    timestamped reference text WhisperX alignment can use as per-line
    segment boundary hints (see ``PhonemeAnalyzer._align_with_lyrics``)
    instead of one segment spanning the whole song. Without per-line
    hints, a long instrumental intro before the first sung line can make
    naive whole-song forced alignment anchor early words near the start
    of the song instead of where the singing actually begins (found
    2026-08-03: "It's the Most Wonderful Time of the Year" has a lead-in
    before Andy Williams starts singing; the Timeline's first lyric line
    rendered at t=0 instead of where the vocals audibly enter).

    Format: one ``[<t_ms>]<text>`` per line, e.g. ``[660]It's the most
    wonderful time of the year``. Deliberately not real LRC format (which
    uses ``MM:SS.ff``, not raw milliseconds) — this is a private
    interchange format between this function and
    ``_align_with_lyrics``/``_parse_timed_lyrics``, not meant to be
    confused with the LRC handling in ``synced_lyrics.py``.
    """
    return "\n".join(
        f"[{line['t_ms']}]{line['text']}"
        for line in lyric_lines
        if line.get("text") and line.get("t_ms") is not None
    )


def _run_in_process(
    audio_path: str, lyrics_path: Optional[str],
) -> tuple[list[dict], list[dict], list[str]]:
    from src.analyzer.phonemes import PhonemeAnalyzer

    analyzer = PhonemeAnalyzer(model_name="base", device="cpu", language="en")
    result = analyzer.analyze(audio_path, source_file=audio_path, lyrics_path=lyrics_path)
    warnings = list(getattr(analyzer, "warnings", []) or [])
    if result is None:
        return [], [], warnings
    words = [m.to_dict() for m in result.word_track.marks]
    phonemes = [m.to_dict() for m in result.phoneme_track.marks]
    return words, phonemes, warnings


def _run_in_sidecar(
    sidecar: Path, audio_path: str, lyrics_path: Optional[str],
) -> tuple[list[dict], list[dict], list[str]]:
    repo_root = Path(__file__).resolve().parents[2]
    script = f'''
import json, sys
sys.path.insert(0, {str(repo_root)!r})
try:
    import torch
    _orig_torch_load = torch.load
    def _torch_load_compat(*args, **kwargs):
        kwargs["weights_only"] = False
        return _orig_torch_load(*args, **kwargs)
    torch.load = _torch_load_compat
except Exception:
    pass
from src.analyzer.phonemes import PhonemeAnalyzer
analyzer = PhonemeAnalyzer(model_name="base", device="cpu", language="en")
result = analyzer.analyze({audio_path!r}, source_file={audio_path!r}, lyrics_path={lyrics_path!r})
warnings = list(getattr(analyzer, "warnings", []) or [])
if result is None:
    print(json.dumps({{"words": [], "phonemes": [], "warnings": warnings}}))
else:
    print(json.dumps({{
        "words": [m.to_dict() for m in result.word_track.marks],
        "phonemes": [m.to_dict() for m in result.phoneme_track.marks],
        "warnings": warnings,
    }}))
'''
    proc = subprocess.run(
        [str(sidecar), "-c", script],
        capture_output=True, text=True, timeout=_SUBPROCESS_TIMEOUT_S,
    )
    if proc.returncode != 0:
        log.warning("phoneme sidecar subprocess failed:\n%s", proc.stderr[:800])
        return [], [], []
    payload = json.loads(proc.stdout.strip().split("\n")[-1])
    return payload.get("words", []), payload.get("phonemes", []), payload.get("warnings", [])


def align_words_and_phonemes(
    audio_path: str,
    lyric_lines: Optional[list[dict]] = None,
    lyrics_text: Optional[str] = None,
) -> tuple[list[dict], list[dict], list[str]]:
    """Return ``(words, phonemes, warnings)`` for the song's vocals.

    Each word mark is ``{"label": str, "start_ms": int, "end_ms": int,
    "speaker": int}`` — word labels are uppercased words; ``speaker`` is 0
    (lead) or 1 (featured/backup), from :func:`diarize_words
    <src.analyzer.vocal_diarization.diarize_words>` — always 0 when no
    second voice is confidently detected. Phoneme labels are Papagayo mouth
    shapes (AI/E/O/U/WQ/L/MBP/FV/etc/rest) matching xLights face
    definitions.

    When ``lyric_lines`` (session ``lyrics`` — ``{t_ms, duration_ms,
    text}``, i.e. real LRC timestamps) is provided, WhisperX force-aligns
    that known lyric text. Otherwise, when ``lyrics_text`` (raw plain text —
    e.g. a user-pasted lyrics fallback, or an untimed provider result) is
    provided, WhisperX force-aligns THAT instead. Both produce far more
    accurate word text than free transcription, which only guesses words
    from audio alone (user-confirmed 2026-07-21: free transcription on a
    pasted-but-untimed song produced garbage words). Only when NEITHER is
    available does it fall back to free transcription. Prefers the cached
    demucs vocals stem over the full mix when one exists.

    ``warnings`` includes, notably, the case where lyric text was provided
    but fewer than 50% of its words aligned to the audio — the analyzer
    discards the provided text entirely and falls back to free
    transcription for the WHOLE song in that case (see
    ``PhonemeAnalyzer._align_with_lyrics``), so the returned words can look
    like "made up" text even though real lyrics were supplied. Surface this
    warning to the user rather than silently returning different words than
    what they pasted.

    Never raises: returns ``([], [], [])`` when WhisperX is unavailable in
    both the main venv and the ``.venv-vamp`` sidecar, or when alignment
    fails.
    """
    vocals = _discover_vocals_stem(audio_path)
    align_audio = str(vocals) if vocals is not None else str(audio_path)

    words, phonemes, warnings = _run_alignment(align_audio, lyric_lines, lyrics_text)

    if words and vocals is not None:
        from src.analyzer.vocal_diarization import diarize_words
        words = diarize_words(str(vocals), words)
    else:
        words = [{**w, "speaker": 0} for w in words]

    return words, phonemes, warnings


def _run_alignment(
    align_audio: str,
    lyric_lines: Optional[list[dict]],
    lyrics_text: Optional[str],
) -> tuple[list[dict], list[dict], list[str]]:
    lyrics_path: Optional[str] = None
    tmp_file: Optional[str] = None
    try:
        reference_text: str = ""
        if lyric_lines:
            reference_text = _lyric_lines_to_text(lyric_lines)
        elif lyrics_text:
            reference_text = lyrics_text
        if reference_text.strip():
            fd, tmp_file = tempfile.mkstemp(suffix=".txt", text=True)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(reference_text)
            lyrics_path = tmp_file

        try:
            return _run_in_process(align_audio, lyrics_path)
        except RuntimeError:
            # PhonemeAnalyzer raises RuntimeError when whisperx isn't
            # importable in this venv — try the sidecar interpreter.
            sidecar = _sidecar_python()
            if sidecar is None:
                log.warning(
                    "phoneme alignment skipped: whisperx unavailable and no "
                    ".venv-vamp sidecar found"
                )
                return [], [], []
            return _run_in_sidecar(sidecar, align_audio, lyrics_path)
        except Exception as exc:
            log.warning("phoneme alignment failed: %s", exc, exc_info=True)
            return [], [], []
    finally:
        if tmp_file is not None:
            try:
                os.unlink(tmp_file)
            except OSError:
                pass
