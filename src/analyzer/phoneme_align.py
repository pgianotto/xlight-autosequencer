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

# WhisperX model size for forced alignment. "small" (244M params) over the
# default "base" (74M) for real accuracy on lyric alignment reliability
# (2026-08-05, user: "I would rather a longer analysis time than sacrifice
# quality") -- meaningfully better than base at a bounded extra CPU cost per
# song; "medium"/"large" exist as further dials if this still isn't enough,
# at a much steeper time cost on this project's weak (Atom) deployment
# hardware.
_WHISPERX_MODEL = "small"


def _tokens(text: str) -> list[str]:
    return [t.upper() for t in _WORD_RE.findall(text)]


def _line_token_counts(lyric_lines: list[dict]) -> dict[int, int]:
    return {li: len(_tokens(line.get("text", ""))) for li, line in enumerate(lyric_lines)}


def _match_lines_to_words(
    lyric_lines: list[dict], words: list[dict],
) -> tuple[dict[int, int], dict[int, int], dict[int, int]]:
    """Return ``(line_start_ms, line_end_ms, line_matched_word_count)`` dicts
    keyed by line index, covering only lines with at least one word in
    ``words`` matched by text.

    Aligns the flattened reference words (from ``lyric_lines``) against
    ``words`` by text via :class:`difflib.SequenceMatcher`, which finds the
    matching blocks of an ordered subsequence — exactly what a forced
    aligner that only *drops* words (never reorders or invents them)
    produces. This recovers which aligned word belongs to which original
    line even when ``words`` is missing entries. ``line_matched_word_count``
    lets a caller judge *how well* a line matched, not just whether it did
    (see ``realign_lyric_lines``, which uses this to pick between two
    candidate word sources per line rather than treating any nonzero match
    as good enough).
    """
    orig_tokens: list[str] = []
    orig_line_of: list[int] = []
    for li, line in enumerate(lyric_lines):
        for tok in _tokens(line.get("text", "")):
            orig_tokens.append(tok)
            orig_line_of.append(li)

    aligned_tokens = [w["label"] for w in words]
    if not orig_tokens or not aligned_tokens:
        return {}, {}, {}

    import difflib
    matcher = difflib.SequenceMatcher(a=orig_tokens, b=aligned_tokens, autojunk=False)

    line_start: dict[int, int] = {}
    line_end: dict[int, int] = {}
    line_matches: dict[int, int] = {}
    for block in matcher.get_matching_blocks():
        for k in range(block.size):
            li = orig_line_of[block.a + k]
            w = words[block.b + k]
            start_ms, end_ms = w["start_ms"], w["end_ms"]
            if li not in line_start or start_ms < line_start[li]:
                line_start[li] = start_ms
            if li not in line_end or end_ms > line_end[li]:
                line_end[li] = end_ms
            line_matches[li] = line_matches.get(li, 0) + 1
    return line_start, line_end, line_matches


# Tolerance below which a correction landing slightly before the previous
# line's accepted end is still trusted (legitimate overlap, e.g. a
# duet/backup-vocal line) rather than rejected as a mismatch. See
# realign_lyric_lines.
_MONOTONICITY_TOLERANCE_MS = 250


def realign_lyric_lines(
    lyric_lines: list[dict], words: list[dict], fallback_words: Optional[list[dict]] = None,
) -> list[dict]:
    """Correct each lyric line's (t_ms, duration_ms) with forced-aligned
    word timestamps instead of the lyrics provider's raw line timestamps.

    ``align_words_and_phonemes`` force-aligns exactly the text ``lyric_lines``
    supplies (see ``_lyric_lines_to_text`` — one original line per newline).
    WhisperX doesn't always return timing for every reference word though —
    low-confidence words are dropped from its output entirely (see
    ``PhonemeAnalyzer.analyze``'s ``word_marks`` filter), so ``words`` is
    typically a *subsequence* of the full reference text, not a 1:1 match
    (found 2026-08-03: only 97 of 184 words aligned on one song — an exact
    total-count match, the original approach here, essentially never holds
    in practice and silently discarded every alignment).

    Each line's ``t_ms``/``duration_ms`` is set from whichever of ``words``
    or ``fallback_words`` (typically ``ctc_align.align_words`` — a forced
    aligner that never drops words, so it can cover lines WhisperX skipped
    or under-matched, at the cost of being a general-purpose,
    non-singing-adapted model) matched a *larger fraction of that line's own
    words* — not just whichever matched any word at all (found 2026-08-05:
    a line with only 1 of 8 words matched by WhisperX still "won" under the
    old any-match-wins rule even when the fallback matched 7 of 8, because
    the old rule only checked for a completely unmatched line). A line with
    zero matches from both sources keeps its original (provider) timing
    rather than guessing.

    A candidate correction that would place a line before the previous
    line's own accepted end (beyond ``_MONOTONICITY_TOLERANCE_MS``) is
    rejected in favor of the next candidate, or the line's original
    timing if none qualify -- a match text-similar enough to win but
    chronologically implausible next to its neighbors is a mismatch, not
    a real correction (found 2026-09-25, "Magic Mirror": a short,
    weakly-sung line landed ~9s before its predecessor, on top of an
    unrelated earlier line).
    """
    if not lyric_lines or not words:
        return lyric_lines

    token_counts = _line_token_counts(lyric_lines)
    line_start, line_end, line_matches = _match_lines_to_words(lyric_lines, words)
    fb_start, fb_end, fb_matches = (
        _match_lines_to_words(lyric_lines, fallback_words) if fallback_words else ({}, {}, {})
    )

    corrected: list[dict] = []
    prev_end_ms: Optional[int] = None
    for li, line in enumerate(lyric_lines):
        n = token_counts.get(li, 0) or 1
        primary_coverage = line_matches.get(li, 0) / n
        fallback_coverage = fb_matches.get(li, 0) / n
        # Ties (including both zero) favor primary: WhisperX is
        # singing-adapted and already used for the Words/Phonemes tracks, so
        # prefer it whenever the fallback isn't a clear improvement.
        candidates: list[tuple[int, int]] = []
        primary_first = li in line_start and (li not in fb_start or primary_coverage >= fallback_coverage)
        first_source = (line_start, line_end) if primary_first else (fb_start, fb_end)
        second_source = (fb_start, fb_end) if primary_first else (line_start, line_end)
        for starts, ends in (first_source, second_source):
            if li in starts:
                candidates.append((starts[li], ends[li]))

        # A correction that would place this line before a neighbor
        # already placed earlier in the song is a mismatched word, not a
        # legitimate timing fix -- reject it and try the next candidate
        # (found 2026-09-25, "Magic Mirror": a short, weakly-sung line
        # ("Think again...") got a ctc-forced-aligner fallback correction
        # landing ~9s *before* the previous line's own accepted end,
        # colliding with an unrelated earlier line, because neither
        # matcher checks whether its match is chronologically plausible
        # next to already-placed neighbors -- only whether SOME match
        # exists anywhere in the song).
        accepted = next(
            (
                (start, end) for start, end in candidates
                if prev_end_ms is None or start >= prev_end_ms - _MONOTONICITY_TOLERANCE_MS
            ),
            None,
        )

        if accepted is not None:
            start, end = accepted
            corrected.append({
                "t_ms": start,
                "duration_ms": max(end - start, 1),
                "text": line.get("text", ""),
            })
            prev_end_ms = end
        else:
            corrected.append(dict(line))
            prev_end_ms = line.get("t_ms", 0) + line.get("duration_ms", 0)
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

    analyzer = PhonemeAnalyzer(model_name=_WHISPERX_MODEL, device="cpu", language="en")
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
analyzer = PhonemeAnalyzer(model_name={_WHISPERX_MODEL!r}, device="cpu", language="en")
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


def ctc_fallback_words(audio_path: str, lyric_lines: list[dict]) -> Optional[list[dict]]:
    """Force-align ``lyric_lines``' full text against the audio with
    ``ctc_align`` (MMS/Wav2Vec2 CTC), for ``realign_lyric_lines``'
    ``fallback_words`` — a forced aligner that never drops words, unlike
    WhisperX (see ``realign_lyric_lines``), so it can recover timing for
    lines WhisperX matched zero words on. Callers should only bother
    invoking this when such lines actually exist — it's a second full model
    pass over the song, not cheap.

    Returns ``None`` when there's no text to align, no vocals stem is
    available, or ``ctc_align`` itself returns ``None`` (unavailable/failed
    — never raises).
    """
    text = " ".join(
        line.get("text", "") for line in lyric_lines if line.get("text")
    ).strip()
    if not text:
        return None
    vocals = _discover_vocals_stem(audio_path)
    if vocals is None:
        return None
    from src.analyzer.ctc_align import align_words
    return align_words(str(vocals), text)


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
