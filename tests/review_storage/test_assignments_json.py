"""Failing tests for src/review/storage/assignments.py — run before T016."""
import pytest

from src.review.storage import assignments as asgn_storage


SONG_ID = "aabbccddeeff0011"

SECTIONS = [
    {"index": 0, "start_ms": 0, "end_ms": 30000, "kind": "intro", "label": "Intro"},
    {"index": 1, "start_ms": 30000, "end_ms": 90000, "kind": "verse", "label": "Verse 1"},
]

ASSIGNMENTS = [
    {"section_index": 0, "theme_id": "quiet", "overrides": {}},
    {"section_index": 1, "theme_id": "driving", "overrides": {"brightness": 0.8}},
]


def test_round_trip(state_dir):
    asgn_storage.save_session(SONG_ID, SECTIONS, ASSIGNMENTS)
    loaded = asgn_storage.load_session(SONG_ID)
    assert loaded["sections"] == SECTIONS
    assert loaded["assignments"] == ASSIGNMENTS


def test_lengths_must_match(state_dir):
    with pytest.raises(ValueError, match="length"):
        asgn_storage.save_session(SONG_ID, SECTIONS, [ASSIGNMENTS[0]])


def test_missing_returns_none(state_dir):
    result = asgn_storage.load_session("nonexistent0000")
    assert result is None


def test_atomic_write(state_dir):
    asgn_storage.save_session(SONG_ID, SECTIONS, ASSIGNMENTS)
    # Second write should not corrupt the first
    asgn_storage.save_session(SONG_ID, SECTIONS, ASSIGNMENTS)
    loaded = asgn_storage.load_session(SONG_ID)
    assert len(loaded["sections"]) == 2


def test_save_session_preserves_extra_fields(state_dir):
    """Regression: editing assignments must not discard fields the
    analyze-commit path persisted (lyrics, detected_sections, ...) — losing
    them dropped the Lyrics timing track from exported .xsq files."""
    asgn_storage.save_full_session(SONG_ID, {
        "sections": SECTIONS,
        "detected_sections": SECTIONS,
        "assignments": ASSIGNMENTS,
        "ghost_boundaries": [],
        "lyrics": [{"t_ms": 1000, "duration_ms": 2000, "text": "la la la"}],
    })

    updated = [dict(a, theme_id="calm") for a in ASSIGNMENTS]
    asgn_storage.save_session(SONG_ID, SECTIONS, updated)

    loaded = asgn_storage.load_session(SONG_ID)
    assert loaded["assignments"][0]["theme_id"] == "calm"
    assert loaded["lyrics"] == [{"t_ms": 1000, "duration_ms": 2000, "text": "la la la"}]
    assert loaded["detected_sections"] == SECTIONS
