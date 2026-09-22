"""Tests for src/review/mcp_server.py -- the MCP tool wrappers.

Every tool here is a thin wrapper around existing src/review/api/v1
functions, so these tests call the tool functions directly (not through
the full MCP protocol layer -- that plumbing is exercised separately in
tests/integration/test_mcp_asgi_sse.py) against the same isolated,
stub-pipeline conventions the rest of tests/review/ uses.
"""
from __future__ import annotations

import asyncio
import io
import math
import struct
import wave
from pathlib import Path

import pytest

import src.review.mcp_server as mcp_server


def _make_wav_bytes(duration_secs: float = 6.0, sample_rate: int = 22050) -> bytes:
    """6-second sine WAV that passes import-time validation (>= 5s, non-silent)."""
    n_samples = int(duration_secs * sample_rate)
    samples = [
        int(8000 * math.sin(2 * math.pi * 440 * i / sample_rate))
        for i in range(n_samples)
    ]
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sample_rate)
        w.writeframes(struct.pack(f"<{n_samples}h", *samples))
    return buf.getvalue()


@pytest.fixture()
def isolated_state(tmp_path, monkeypatch):
    """Isolate library/session state and use the fast stub analysis pipeline
    -- same conventions as tests/review/conftest.py's `app` fixture."""
    monkeypatch.setenv("XLIGHT_STATE_HOME", str(tmp_path))
    monkeypatch.setenv("XLIGHT_STUB_ANALYSIS", "1")

    from src.review.api.v1 import analysis as analysis_module
    with analysis_module._runs_lock:
        analysis_module._runs.clear()
    from src.review.api.v1 import export as export_module
    with export_module._exports_lock:
        export_module._exports.clear()
        export_module._song_exports.clear()

    yield tmp_path

    analysis_module._join_active_threads()


@pytest.fixture()
def wav_path(tmp_path) -> Path:
    p = tmp_path / "source" / "test.wav"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(_make_wav_bytes())
    return p


def _imported_song_id(wav_path: Path) -> str:
    result = mcp_server.import_song(str(wav_path))
    return result["song"]["song_id"]


class TestListLibrary:
    def test_empty_library_has_unfiled_folder(self, isolated_state):
        result = mcp_server.list_library()
        assert result["songs"] == []
        assert any(f["folder_id"] == "unfiled" for f in result["folders"])

    def test_lists_imported_song(self, isolated_state, wav_path):
        song_id = _imported_song_id(wav_path)
        result = mcp_server.list_library()
        ids = [s["song_id"] for s in result["songs"]]
        assert song_id in ids

    def test_song_has_source_exists_field(self, isolated_state, wav_path):
        _imported_song_id(wav_path)
        result = mcp_server.list_library()
        assert result["songs"][0]["source_exists"] is True


class TestImportSong:
    def test_returns_song_record(self, isolated_state, wav_path):
        result = mcp_server.import_song(str(wav_path))
        assert result["created"] is True
        assert result["song"]["status"] == "draft"
        assert result["song"]["source_paths"]

    def test_reimport_same_file_dedupes(self, isolated_state, wav_path):
        first = mcp_server.import_song(str(wav_path))
        second = mcp_server.import_song(str(wav_path))
        assert first["song"]["song_id"] == second["song"]["song_id"]

    def test_missing_file_raises_value_error(self, isolated_state, tmp_path):
        with pytest.raises(ValueError, match="file_not_found"):
            mcp_server.import_song(str(tmp_path / "nope.wav"))

    def test_unsupported_extension_raises_value_error(self, isolated_state, tmp_path):
        bogus = tmp_path / "notaudio.txt"
        bogus.write_text("hello")
        with pytest.raises(ValueError, match="unsupported_format"):
            mcp_server.import_song(str(bogus))


class TestAnalyzeSong:
    def test_analyze_returns_sections_and_assignments(self, isolated_state, wav_path):
        song_id = _imported_song_id(wav_path)
        result = asyncio.run(mcp_server.analyze_song(song_id))
        assert result["sections"]
        assert result["assignments"]
        assert len(result["sections"]) == len(result["assignments"])

    def test_analyze_marks_song_analyzed_in_library(self, isolated_state, wav_path):
        song_id = _imported_song_id(wav_path)
        asyncio.run(mcp_server.analyze_song(song_id))
        lib = mcp_server.list_library()
        song = next(s for s in lib["songs"] if s["song_id"] == song_id)
        assert song["status"] == "analyzed"

    def test_analyze_unknown_song_raises_value_error(self, isolated_state):
        with pytest.raises(ValueError, match="song_not_found"):
            asyncio.run(mcp_server.analyze_song("deadbeef00000000"))

    def test_analyze_reports_progress_via_context(self, isolated_state, wav_path):
        song_id = _imported_song_id(wav_path)

        events: list[tuple[float, float | None, str | None]] = []

        class FakeContext:
            async def report_progress(self, progress, total=None, message=None):
                events.append((progress, total, message))

            async def info(self, data, *, logger_name=None):
                events.append((None, None, str(data)))

        asyncio.run(mcp_server.analyze_song(song_id, ctx=FakeContext()))
        assert events, "expected at least one progress/info event"

    def test_force_reanalyze_commits_fresh_result(self, isolated_state, wav_path):
        song_id = _imported_song_id(wav_path)
        asyncio.run(mcp_server.analyze_song(song_id))
        result = asyncio.run(mcp_server.analyze_song(song_id, force=True))
        assert result["sections"]
        assert result["assignments"]


class TestGetSongStory:
    def test_raises_before_analysis(self, isolated_state, wav_path):
        song_id = _imported_song_id(wav_path)
        with pytest.raises(ValueError, match="not_analyzed"):
            mcp_server.get_song_story(song_id)

    def test_returns_sections_after_analysis(self, isolated_state, wav_path):
        song_id = _imported_song_id(wav_path)
        asyncio.run(mcp_server.analyze_song(song_id))
        story = mcp_server.get_song_story(song_id)
        assert story["sections"]

    def test_unknown_song_raises_value_error(self, isolated_state):
        with pytest.raises(ValueError, match="song_not_found"):
            mcp_server.get_song_story("deadbeef00000000")


class TestCatalogTools:
    def test_list_themes_nonempty(self, isolated_state):
        result = mcp_server.list_themes()
        assert result["themes"]
        first = result["themes"][0]
        assert {"theme_id", "name", "accent", "swatches"} <= first.keys()

    def test_list_effects_nonempty(self, isolated_state):
        result = mcp_server.list_effects()
        assert result["effects"]
        assert "name" in result["effects"][0]

    def test_list_variants_nonempty(self, isolated_state):
        result = mcp_server.list_variants()
        assert result["variants"]


class TestGetLayoutInfo:
    def test_returns_real_committed_layout(self, isolated_state):
        result = mcp_server.get_layout_info()
        assert result["props"]
        assert result["xml_path"].endswith("xlights_rgbeffects.xml")


class TestGenerateSequence:
    """Mocks the underlying _start_export -- a real end-to-end generation
    needs a fully themed song + real generator pipeline (exercised
    separately in tests/review/test_api_export.py); this only verifies
    generate_sequence's own polling/argument-building/error-propagation
    logic, matching this tool's counterparts in test coverage intent."""

    def test_builds_body_from_arguments(self, isolated_state, monkeypatch):
        captured = {}

        def fake_start_export(song_id, body):
            captured["song_id"] = song_id
            captured["body"] = body
            from src.review.api.v1.export import _ExportState, _exports, _exports_lock, _song_exports
            state = _ExportState("exp_test")
            state.status = "done"
            state.output_path = "/tmp/out.xsq"
            state.variation_seed = 42
            with _exports_lock:
                _exports["exp_test"] = state
                _song_exports[song_id] = "exp_test"
            return {"export_id": "exp_test", "started_at": "now", "variation_seed": 42}, 202

        monkeypatch.setattr("src.review.api.v1.export._start_export", fake_start_export)

        result = asyncio.run(mcp_server.generate_sequence(
            "somesong", genre="rock", occasion="christmas", variation_seed=7,
        ))
        assert captured["song_id"] == "somesong"
        assert captured["body"] == {"genre": "rock", "occasion": "christmas", "variation_seed": 7}
        assert result["output_path"] == "/tmp/out.xsq"

    def test_propagates_start_error(self, isolated_state, monkeypatch):
        def fake_start_export(song_id, body):
            return {"error": {"code": "song_not_found", "message": "Song not found"}}, 404

        monkeypatch.setattr("src.review.api.v1.export._start_export", fake_start_export)

        with pytest.raises(ValueError, match="song_not_found"):
            asyncio.run(mcp_server.generate_sequence("nope"))

    def test_raises_on_generation_failure(self, isolated_state, monkeypatch):
        def fake_start_export(song_id, body):
            from src.review.api.v1.export import _ExportState, _exports, _exports_lock, _song_exports
            state = _ExportState("exp_fail")
            state.status = "failed"
            state.events = [{"stage": "failed", "error": "layout mismatch"}]
            with _exports_lock:
                _exports["exp_fail"] = state
                _song_exports[song_id] = "exp_fail"
            return {"export_id": "exp_fail", "started_at": "now", "variation_seed": 1}, 202

        monkeypatch.setattr("src.review.api.v1.export._start_export", fake_start_export)

        with pytest.raises(RuntimeError, match="layout mismatch"):
            asyncio.run(mcp_server.generate_sequence("somesong"))
