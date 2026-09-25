import os
import json
import tempfile
import pytest

from src.review.server import create_app


@pytest.fixture()
def app(tmp_path, monkeypatch):
    """Flask test-client app wired to a fresh temp library dir."""
    monkeypatch.setenv("XLIGHT_STATE_HOME", str(tmp_path))
    # Use the fast stub analysis pipeline in tests so they don't time out
    # waiting for the full vamp/demucs/madmom pipeline.
    monkeypatch.setenv("XLIGHT_STUB_ANALYSIS", "1")

    # Clear module-level analysis run state. `_runs` is a dict that
    # accumulates across test invocations because the analysis module
    # isn't reloaded between tests. Without this, tests that import the
    # same WAV bytes (→ same song_id) see stale state from prior runs
    # and /analyze responses can omit run_id when the prior run is
    # still cached.
    from src.review.api.v1 import analysis as _analysis_module
    with _analysis_module._runs_lock:
        _analysis_module._runs.clear()

    # Clear the layout module's cache too. It was already a module-level
    # global before the upload feature existed, but harmlessly so — every
    # test read the same repo-committed file regardless of XLIGHT_STATE_HOME.
    # Now that an uploaded override lives under XLIGHT_STATE_HOME (which
    # *does* vary per test via tmp_path above), a stale cached value from a
    # previous test's upload would leak into this one without this reset.
    from src.review.api.v1 import layout as _layout_module
    _layout_module._active_layout_cache = None

    application = create_app(testing=True)
    application.config["TESTING"] = True
    yield application

    # Block until any background analysis thread this test spawned finishes,
    # before monkeypatch reverts XLIGHT_STATE_HOME below. Otherwise a thread
    # still running past the end of this test picks up the NEXT test's env
    # var (it's read live, not captured at spawn) and writes into that
    # test's temp dir instead of this one's.
    _analysis_module._join_active_threads()


@pytest.fixture()
def client(app):
    return app.test_client()


@pytest.fixture()
def sample_song():
    """Minimal Song dict matching data-model.md."""
    return {
        "song_id": "aabbccddeeff0011",
        "title": "Highway Star",
        "artist": "Deep Purple",
        "duration_ms": 370_000,
        "bpm": 148.0,
        "key": "E minor",
        "time_signature": [4, 4],
        "status": "draft",
        "source_paths": ["/tmp/highway.mp3"],
        "folder_id": "unfiled",
        "imported_at": "2026-04-21T00:00:00Z",
        "last_opened_at": None,
    }
