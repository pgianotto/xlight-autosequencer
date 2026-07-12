"""Tests for singing-face and lyric-text placement (effect_placer helpers)."""
from __future__ import annotations

from src.generator.effect_placer import (
    _best_face_definition,
    _place_lyric_text,
    _place_singing_faces,
    _vocal_regions,
)
from src.grouper.layout import Prop


def _prop(name: str, display_as: str = "Custom", *,
          faces: list[str] | None = None, pixels: int = 0) -> Prop:
    return Prop(
        name=name,
        display_as=display_as,
        world_x=0.0, world_y=0.0, world_z=0.0,
        scale_x=1.0, scale_y=1.0,
        parm1=1, parm2=1,
        sub_models=[],
        pixel_count=pixels,
        face_definitions=faces or [],
    )


WORDS = [
    {"label": "HELLO", "start_ms": 1000, "end_ms": 1400},
    {"label": "WORLD", "start_ms": 1600, "end_ms": 2000},
    # > 5s gap — second vocal region
    {"label": "AGAIN", "start_ms": 9000, "end_ms": 9500},
]


class TestVocalRegions:
    def test_merges_close_words_and_splits_on_gap(self):
        assert _vocal_regions(WORDS) == [(1000, 2000), (9000, 9500)]

    def test_empty(self):
        assert _vocal_regions(None) == []
        assert _vocal_regions([]) == []

    def test_unsorted_input(self):
        shuffled = [WORDS[2], WORDS[0], WORDS[1]]
        assert _vocal_regions(shuffled) == [(1000, 2000), (9000, 9500)]


class TestPlaceSingingFaces:
    def test_places_per_region_on_face_props_only(self):
        props = [
            _prop("Singing Face", faces=["SingingFace"]),
            _prop("Arch1", display_as="Arch"),
        ]
        result = _place_singing_faces(props, WORDS)
        assert set(result) == {"Singing Face"}
        placements = result["Singing Face"]
        assert len(placements) == 2
        assert all(p.effect_name == "Faces" for p in placements)
        assert placements[0].parameters["E_CHOICE_Faces_FaceDefinition"] == "SingingFace"
        assert placements[0].parameters["E_CHOICE_Faces_TimingTrack"] == "Phonemes"
        # Frame-aligned to the first vocal region
        assert placements[0].start_ms == 1000
        assert placements[0].end_ms == 2000

    def test_no_words_no_placements(self):
        props = [_prop("Singing Face", faces=["SingingFace"])]
        assert _place_singing_faces(props, None) == {}
        assert _place_singing_faces(props, []) == {}

    def test_no_face_props_no_placements(self):
        assert _place_singing_faces([_prop("Arch1", display_as="Arch")], WORDS) == {}

    def test_face_definition_matched_to_prop_name(self):
        # A model can carry several definitions ("Singing Tree Male" holds
        # both genders' faces) — the one matching the prop name wins.
        prop = _prop("Singing Tree Male", faces=["Female Face", "Male Face"])
        result = _place_singing_faces([prop], WORDS)
        params = result["Singing Tree Male"][0].parameters
        assert params["E_CHOICE_Faces_FaceDefinition"] == "Male Face"

    def test_matrix_with_image_faces_excluded(self):
        # Layout parser leaves face_definitions empty for Matrix-type
        # (image) faces — such props must not receive Faces placements.
        props = [_prop("Big Matrix", display_as="Matrix", pixels=4800)]
        assert _place_singing_faces(props, WORDS) == {}


class TestPlaceLyricText:
    def test_text_on_largest_matrix(self):
        props = [
            _prop("Matrix Small", display_as="Matrix", pixels=512),
            _prop("Matrix Big", display_as="Matrix", pixels=4800),
            _prop("Singing Face", faces=["SingingFace"]),
        ]
        result = _place_lyric_text(props, WORDS)
        assert set(result) == {"Matrix Big"}
        placements = result["Matrix Big"]
        assert len(placements) == 2  # one per vocal region
        assert all(p.effect_name == "Text" for p in placements)
        assert placements[0].parameters["E_CHOICE_Text_LyricTrack"] == "Words"
        assert placements[0].layer == 0

    def test_lyric_named_matrix_preferred(self):
        props = [
            _prop("Matrix Big", display_as="Matrix", pixels=4800),
            _prop("Lyrics Matrix", display_as="Matrix", pixels=512),
        ]
        result = _place_lyric_text(props, WORDS)
        assert set(result) == {"Lyrics Matrix"}

    def test_no_matrix_no_placements(self):
        assert _place_lyric_text([_prop("Arch1", display_as="Arch")], WORDS) == {}

    def test_no_words_no_placements(self):
        props = [_prop("Matrix Big", display_as="Matrix", pixels=4800)]
        assert _place_lyric_text(props, None) == {}
