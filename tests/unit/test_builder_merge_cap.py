"""Unit test for src/story/builder.py Step 4b's consecutive-same-role merge cap.

See openspec/changes/segmentino-label-extraction/ — without this cap, a
total role-classification failure (e.g. every segmentino label lost, or
any other cause producing uniform per-section signals) can collapse most
of a song into one section via the consecutive-same-role merge. This test
deliberately constructs that exact scenario (unlabeled sections + uniform
vocal/energy curves, so every section gets classified with the same role)
and asserts the merge stops at _MAX_MERGED_SECTION_FRACTION of the song
instead of producing one giant section.
"""
from __future__ import annotations

import copy

import pytest

from tests.fixtures.story_fixture import make_hierarchy_dict

from src.story.builder import build_song_story, _MAX_MERGED_SECTION_FRACTION

AUDIO_PATH = "/tmp/fixture_song.mp3"


def _uniform_curve(value: float, n_frames: int = 1500, sample_rate: float = 10.0) -> dict:
    return {"sample_rate": sample_rate, "values": [value] * n_frames}


@pytest.fixture()
def total_label_loss_hierarchy() -> dict:
    """6 equal 25s sections (150s song), no segmentino labels, uniform
    vocal/energy curves -- every section's energy equals the vocal
    median/max, so classify_section_roles' fallback heuristic assigns the
    same role ("chorus", since e >= chorus_threshold when they're all
    equal) to every single one."""
    duration_ms = 150_000
    # 6 raw boundaries + a sentinel end, mirroring story_fixture.py's own
    # FIXTURE_SECTIONS convention -- deliberately NO "label" key on any
    # entry (simulates the segmentino label-loss bug this cap defends
    # against, independent of that bug's specific root cause).
    sections = [
        {"time_ms": t} for t in range(0, duration_ms, 25_000)
    ] + [{"time_ms": duration_ms}]

    hier = make_hierarchy_dict(duration_ms=duration_ms)
    hier["sections"] = sections
    # Uniform, above-VOCAL_THRESHOLD (0.05) vocal energy and uniform
    # full-mix energy everywhere -> vocal_median == vocal_max == e for
    # every section -> chorus_threshold == e -> every vocal section
    # classifies as "chorus" (see section_classifier.py's fallback path).
    hier["energy_curves"] = copy.deepcopy(hier["energy_curves"])
    hier["energy_curves"]["vocals"] = _uniform_curve(0.5)
    hier["energy_curves"]["full_mix"] = _uniform_curve(0.5)
    return hier


def test_merged_section_never_exceeds_the_cap(total_label_loss_hierarchy):
    story = build_song_story(total_label_loss_hierarchy, AUDIO_PATH)
    duration_ms = total_label_loss_hierarchy["duration_ms"]

    assert story["sections"], "expected at least one section"
    longest = max(s["end"] - s["start"] for s in story["sections"])
    assert longest <= _MAX_MERGED_SECTION_FRACTION * duration_ms, (
        f"a merged section spans {longest}ms, more than "
        f"{_MAX_MERGED_SECTION_FRACTION:.0%} of the {duration_ms}ms song — "
        "the consecutive-same-role merge cap did not apply"
    )


def test_does_not_collapse_to_a_single_section(total_label_loss_hierarchy):
    """Sanity check that this scenario really would have collapsed to one
    giant section without the cap -- otherwise the test above is vacuous."""
    story = build_song_story(total_label_loss_hierarchy, AUDIO_PATH)
    assert len(story["sections"]) > 1


def test_every_section_got_the_same_role(total_label_loss_hierarchy):
    """Confirms the fixture actually exercises the intended scenario
    (uniform signals -> uniform role) rather than accidentally varying
    roles for some unrelated reason, which would make the cap moot."""
    story = build_song_story(total_label_loss_hierarchy, AUDIO_PATH)
    roles = {s["role"] for s in story["sections"]}
    assert roles == {"chorus"}, f"expected every section to share one role, got {roles}"
