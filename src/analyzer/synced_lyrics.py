"""Synced-lyrics lookup: fetch, parse, and derive chorus text for boundary refinement.

Uses ``syncedlyrics`` (a token-free, multi-provider LRC aggregator) to feed
``src.story.boundary_refinement``'s Fix 1 (merge short post_chorus tails)
and Fix 2 (relabel/split a bridge whose sung content opens with the chorus
first-line hook). Unlike the retired Genius integration, LRC carries no
``[Chorus]``/``[Verse]`` structural headers — this module does not produce
section labels or boundaries, only word timing and a best-guess chorus text
block derived from line repetition.

Provider allowlist deliberately excludes ``syncedlyrics``'s built-in Genius
scraper: this project does not access genius.com in any form (see
docs/segment-classification-changelog.md, 2026-07-11 entry).
"""
from __future__ import annotations

import re
from typing import Optional

from src.analyzer.phonemes import WordMark
from src.analyzer.result import TimingMark
from src.log import get_logger

log = get_logger("xlight.synced_lyrics")

_ALLOWED_PROVIDERS = ["lrclib", "musixmatch", "netease", "deezer", "megalobiz", "lyricsify"]

_LRC_LINE_RE = re.compile(r"^\[(\d+):(\d+(?:\.\d+)?)\](.*)$")

# syncedlyrics queries multiple providers sequentially with no timeout of
# its own; 60s is generous for a handful of lyrics-lookup HTTP calls while
# still bounding a genuinely unresponsive provider (see fetch_synced_lyrics).
_SEARCH_TIMEOUT_S = 60


def parse_lrc(lrc_text: str) -> list[tuple[int, str]]:
    """Parse LRC-format text into a list of ``(start_ms, line_text)`` tuples.

    Skips metadata tags (e.g. ``[ar:Artist]``, ``[ti:Title]``), timestamp
    tags with empty text, and blank lines. Returned in chronological order.
    """
    lines: list[tuple[int, str]] = []
    for raw_line in lrc_text.splitlines():
        m = _LRC_LINE_RE.match(raw_line.strip())
        if not m:
            continue
        minutes, seconds, text = m.groups()
        text = text.strip()
        if not text:
            continue
        start_ms = int(round((int(minutes) * 60 + float(seconds)) * 1000))
        lines.append((start_ms, text))
    lines.sort(key=lambda pair: pair[0])
    return lines


def lines_to_timing_marks(lines: list[tuple[int, str]], duration_ms: int) -> list[TimingMark]:
    """Expand ``(start_ms, line_text)`` pairs into one ``TimingMark`` per line.

    Used for the lyric timeline track (one labeled, duration-spanning block
    per line), as opposed to ``lines_to_word_marks`` which is per-word for
    boundary-refinement's word-window matching.
    """
    marks: list[TimingMark] = []
    for i, (start_ms, text) in enumerate(lines):
        end_ms = lines[i + 1][0] if i + 1 < len(lines) else duration_ms
        end_ms = max(end_ms, start_ms + 1)
        marks.append(TimingMark(time_ms=start_ms, confidence=None, label=text,
                                 duration_ms=end_ms - start_ms))
    return marks


def lines_to_word_marks(lines: list[tuple[int, str]], duration_ms: int) -> list[WordMark]:
    """Expand ``(start_ms, line_text)`` pairs into per-word ``WordMark``s.

    Every word in a line inherits that line's start timestamp; a word's
    end is the next line's start (or ``duration_ms`` for the last line).
    This is coarser than true per-word alignment, but matches the
    granularity boundary refinement's sliding-window text matching needs —
    it only checks whether a word appears within a window, not its exact
    millisecond position.
    """
    marks: list[WordMark] = []
    for i, (start_ms, text) in enumerate(lines):
        end_ms = lines[i + 1][0] if i + 1 < len(lines) else duration_ms
        end_ms = max(end_ms, start_ms + 1)
        for word in re.sub(r"[^a-zA-Z0-9\s']", " ", text).split():
            marks.append(WordMark(label=word.upper(), start_ms=start_ms, end_ms=end_ms))
    return marks


def _normalize_line(text: str) -> str:
    return re.sub(r"[^a-z0-9\s]", "", text.lower()).strip()


def find_chorus_body(
    lines: list[tuple[int, str]], *, min_repeats: int = 2, block_size: int = 2,
) -> Optional[str]:
    """Find the most-repeated contiguous block of lyric lines.

    LRC carries no ``[Chorus]``/``[Verse]`` headers, so repetition is the
    only available signal: a chorus repeats near-verbatim across the song;
    a verse doesn't. Returns the original-cased text of the earliest
    occurrence of the most-repeated ``block_size``-line window, or ``None``
    if nothing repeats at least ``min_repeats`` times.
    """
    if len(lines) < block_size:
        return None

    occurrences: dict[str, list[int]] = {}
    for i in range(len(lines) - block_size + 1):
        key = " ".join(_normalize_line(lines[j][1]) for j in range(i, i + block_size))
        if not key:
            continue
        occurrences.setdefault(key, []).append(i)

    candidates = [(key, idxs) for key, idxs in occurrences.items() if len(idxs) >= min_repeats]
    if not candidates:
        return None

    # Most repeats wins; ties broken by earliest first occurrence.
    _best_key, best_idxs = min(candidates, key=lambda kv: (-len(kv[1]), kv[1][0]))
    first_idx = best_idxs[0]
    return " ".join(lines[j][1] for j in range(first_idx, first_idx + block_size))


_LRC_METADATA_RE = re.compile(r"^\[(ar|ti):(.*)\]$", re.IGNORECASE)


def _parse_lrc_metadata(lrc_text: str) -> dict[str, str]:
    """Extract ``[ar:]``/``[ti:]`` metadata tags from LRC text, if present.

    Coverage varies by provider — some embed these header tags, others
    don't — so callers must treat an empty result as "nothing to validate
    against", not as a mismatch.
    """
    meta: dict[str, str] = {}
    for raw_line in lrc_text.splitlines():
        m = _LRC_METADATA_RE.match(raw_line.strip())
        if m:
            meta[m.group(1).lower()] = m.group(2).strip()
    return meta


def _significant_tokens(text: str) -> set[str]:
    """Lowercase alphanumeric word tokens, dropping 1-2 letter filler words."""
    tokens = re.findall(r"[a-z0-9]+", text.lower())
    return {t for t in tokens if len(t) > 2 or t.isdigit()}


def _lyrics_match_expected(lrc_text: str, title: str, artist: str) -> bool:
    """Best-effort check that a fetched lyrics result is for the requested song.

    A fuzzy provider search can confidently return the wrong song for the
    same artist (e.g. "Blue Christmas" search landing on "Hound Dog" —
    both Elvis Presley), so artist agreement alone isn't enough; title
    tokens are checked too. Returns True (accept) whenever the provider's
    response doesn't include a metadata tag to check against — there's
    nothing to validate, and providers that omit them shouldn't be
    penalized versus one that includes an accurate tag.
    """
    meta = _parse_lrc_metadata(lrc_text)
    tag_artist = meta.get("ar")
    tag_title = meta.get("ti")

    if tag_artist:
        expected = _significant_tokens(artist)
        got = _significant_tokens(tag_artist)
        if expected and got and expected.isdisjoint(got):
            return False

    if tag_title:
        expected = _significant_tokens(title)
        got = _significant_tokens(tag_title)
        if expected and got and expected.isdisjoint(got):
            return False

    return True


def fetch_synced_lyrics(title: str, artist: str) -> Optional[str]:
    """Search for synced lyrics via ``syncedlyrics``, restricted to non-Genius providers.

    Returns raw LRC (or plain, provider-dependent) text, or ``None`` when no
    match is found, the search fails, the result looks like a mismatch (see
    ``_lyrics_match_expected``), or ``syncedlyrics`` isn't installed.
    """
    try:
        import syncedlyrics
    except ImportError:
        log.warning("syncedlyrics is not installed — skipping synced-lyrics lookup")
        return None

    search_term = f"{title} {artist}".strip()
    if not search_term:
        return None

    # syncedlyrics queries several third-party providers over HTTP and
    # doesn't expose a timeout parameter of its own — a slow/unresponsive
    # provider could otherwise hang analysis indefinitely. Bound it with an
    # external timeout using a *daemon* thread: concurrent.futures'
    # ThreadPoolExecutor registers an atexit hook that joins its worker
    # threads on interpreter shutdown, so it would still block process exit
    # on a genuinely hung call even after future.result(timeout=...)
    # returns. A daemon thread is abandoned outright — it dies with the
    # process instead of blocking it.
    import threading as _threading

    _outcome: dict[str, object] = {}

    def _do_search() -> None:
        try:
            _outcome["result"] = syncedlyrics.search(search_term, providers=list(_ALLOWED_PROVIDERS))
        except Exception as exc:  # noqa: BLE001 — surfaced via _outcome, not raised across threads
            _outcome["exc"] = exc

    search_thread = _threading.Thread(target=_do_search, daemon=True)
    search_thread.start()
    search_thread.join(timeout=_SEARCH_TIMEOUT_S)

    if search_thread.is_alive():
        log.warning(
            "syncedlyrics search timed out after %ds for %r — skipping "
            "(search thread abandoned, may still be running in background)",
            _SEARCH_TIMEOUT_S, search_term,
        )
        return None

    if "exc" in _outcome:
        log.warning("syncedlyrics search failed for %r: %s", search_term, _outcome["exc"])
        return None

    result = _outcome.get("result")

    if result and not _lyrics_match_expected(result, title, artist):
        log.warning(
            "syncedlyrics result for %r looks like a mismatch (expected "
            "title=%r artist=%r) — discarding", search_term, title, artist,
        )
        return None

    return result


def get_boundary_refinement_inputs(
    title: str, artist: str, duration_ms: int,
) -> tuple[list[WordMark], Optional[str], list[TimingMark]]:
    """Fetch synced lyrics and derive ``(forced_words, chorus_body, line_marks)``.

    Returns ``([], None, [])`` when no synced lyrics are found, when
    ``syncedlyrics`` isn't installed, or when the search failed. When a
    provider returns untimed plain text (no LRC tags), ``forced_words`` and
    ``line_marks`` are empty but ``chorus_body`` can still be derived from
    line repetition. ``line_marks`` is one ``TimingMark`` per LRC line, for
    the lyric timeline track — fetched once here rather than a second time
    per call site to avoid a duplicate network lookup.
    """
    lyrics_text = fetch_synced_lyrics(title, artist)
    if not lyrics_text:
        return [], None, []

    lines = parse_lrc(lyrics_text)
    if not lines:
        plain_lines = [(0, ln.strip()) for ln in lyrics_text.splitlines() if ln.strip()]
        return [], find_chorus_body(plain_lines), []

    forced_words = lines_to_word_marks(lines, duration_ms)
    chorus_body = find_chorus_body(lines)
    line_marks = lines_to_timing_marks(lines, duration_ms)
    return forced_words, chorus_body, line_marks
