"""Tests for src/generator/video_prep.py — 480p downscale + cache."""
from __future__ import annotations

from pathlib import Path

from src.generator import video_prep


class TestScaledVideoPath:
    def test_appends_480p_suffix(self):
        source = Path("/videos/vid123_video.mp4")
        assert video_prep.scaled_video_path(source) == Path("/videos/vid123_video_480p.mp4")


class TestEnsureScaledVideo:
    def test_skips_reencode_when_cached(self, tmp_path, monkeypatch):
        source = tmp_path / "source.mp4"
        source.write_bytes(b"fake source")
        cached = video_prep.scaled_video_path(source)
        cached.write_bytes(b"already scaled")

        calls = []
        monkeypatch.setattr(video_prep.subprocess, "run", lambda *a, **k: calls.append(a))

        result = video_prep.ensure_scaled_video(source)
        assert result == cached
        assert calls == []

    def test_invokes_ffmpeg_when_not_cached(self, tmp_path, monkeypatch):
        source = tmp_path / "source.mp4"
        source.write_bytes(b"fake source")

        monkeypatch.setattr(video_prep, "_find_ffmpeg", lambda: "ffmpeg")

        calls = []

        def fake_run(cmd, **kwargs):
            calls.append(cmd)
            Path(cmd[-1]).write_bytes(b"scaled output")

        monkeypatch.setattr(video_prep.subprocess, "run", fake_run)

        result = video_prep.ensure_scaled_video(source)
        assert result == video_prep.scaled_video_path(source)
        assert result.exists()
        assert len(calls) == 1
        assert "scale=-2:480" in calls[0]
