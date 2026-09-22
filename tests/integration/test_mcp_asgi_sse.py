"""Integration test for the merged Flask+MCP ASGI app (src/cli/review.py).

Two things this specifically guards against, per
openspec/changes/mcp-tool-server/design.html:

1. The MCP endpoint must actually be reachable at /mcp once merged with
   the existing Flask review server (not just in isolation) -- verifies
   the Router(default=WsgiToAsgi(...)) mounting approach, including the
   /mcp -> /mcp/ redirect-slash behavior that a naive Starlette(routes=[
   Mount("/mcp", ...), Mount("/", ...)]) does NOT reproduce (see the
   design doc's Piece 2 for why).
2. A long-lived SSE stream (analysis progress) must not block a
   concurrent, unrelated request -- this is the exact failure mode
   commit 624214c fixed once already under Flask's own dev server
   (see src/cli/review.py's review_cmd docstring/comments); merging onto
   uvicorn changes the concurrency model again and must not regress it.
"""
from __future__ import annotations

import asyncio
import threading
import time

import pytest
from starlette.testclient import TestClient

from src.cli.review import _build_asgi_app
from src.review.server import create_app


@pytest.fixture()
def asgi_client(tmp_path, monkeypatch):
    monkeypatch.setenv("XLIGHT_STATE_HOME", str(tmp_path))
    monkeypatch.setenv("XLIGHT_STUB_ANALYSIS", "1")

    flask_app = create_app(testing=True)
    asgi_app = _build_asgi_app(flask_app)
    with TestClient(asgi_app) as client:
        yield client


def _mcp_initialize_payload() -> dict:
    return {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "test", "version": "1"},
        },
    }


class TestMergedRouting:
    def test_flask_routes_still_work(self, asgi_client):
        resp = asgi_client.get("/api/v1/library")
        assert resp.status_code == 200
        assert "songs" in resp.json()

    def test_mcp_bare_path_reaches_mcp_server(self, asgi_client):
        """No trailing slash -- must redirect-and-follow to the MCP app,
        not fall through to Flask's catch-all (this is the exact bug the
        naive Starlette(routes=[Mount, Mount]) approach had)."""
        resp = asgi_client.post(
            "/mcp", json=_mcp_initialize_payload(),
            headers={"Accept": "application/json, text/event-stream"},
        )
        assert resp.status_code == 200
        assert "xonset" in resp.text

    def test_mcp_trailing_slash_reaches_mcp_server(self, asgi_client):
        resp = asgi_client.post(
            "/mcp/", json=_mcp_initialize_payload(),
            headers={"Accept": "application/json, text/event-stream"},
        )
        assert resp.status_code == 200
        assert "xonset" in resp.text

    def test_unrelated_flask_path_not_swallowed_by_mcp_mount(self, asgi_client):
        resp = asgi_client.get("/api/v1/themes")
        assert resp.status_code == 200


class TestSSEDoesNotBlockConcurrentRequests:
    def test_open_sse_stream_does_not_block_other_requests(self, asgi_client):
        """Regression guard for the exact bug commit 624214c fixed under
        Flask's own dev server (threaded=True) -- open a long-lived SSE
        stream, then confirm an unrelated request completes promptly
        instead of queuing behind it."""
        # Import a song and start an analysis so /analyze/status has a
        # real long-lived stream to hold open.
        import io
        import math
        import struct
        import wave

        def _wav_bytes(duration_secs: float = 6.0, sample_rate: int = 22050) -> bytes:
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

        song_id = asgi_client.post(
            "/api/v1/import",
            files={"audio": ("test.wav", _wav_bytes(), "audio/wav")},
        ).json()["song"]["song_id"]

        # Artificially slow the stub pipeline so the SSE stream stays open
        # long enough to overlap a concurrent request.
        from src.review.api.v1 import analysis as analysis_module
        original_stub = analysis_module._analyze_stub

        def slow_stub(state, source_path, song_id):
            time.sleep(1.0)
            original_stub(state, source_path, song_id)

        analysis_module._analyze_stub = slow_stub
        try:
            asgi_client.post(f"/api/v1/songs/{song_id}/analyze")

            other_request_done = threading.Event()
            other_status = {}

            def make_other_request():
                start = time.monotonic()
                r = asgi_client.get("/api/v1/library")
                other_status["elapsed"] = time.monotonic() - start
                other_status["code"] = r.status_code
                other_request_done.set()

            # Open the SSE stream in a background thread (TestClient.stream
            # blocks reading the body, so it must not share the main thread
            # with the "other" request below).
            def hold_sse_open():
                with asgi_client.stream(
                    "GET", f"/api/v1/songs/{song_id}/analyze/status"
                ) as resp:
                    for _ in resp.iter_lines():
                        if other_request_done.is_set():
                            break

            sse_thread = threading.Thread(target=hold_sse_open, daemon=True)
            sse_thread.start()
            time.sleep(0.2)  # let the SSE stream actually open first

            other_thread = threading.Thread(target=make_other_request, daemon=True)
            other_thread.start()
            other_thread.join(timeout=5)

            assert other_request_done.is_set(), (
                "concurrent /api/v1/library request never completed -- "
                "it's blocked behind the open SSE stream"
            )
            assert other_status["code"] == 200
            assert other_status["elapsed"] < 2.0, (
                f"concurrent request took {other_status['elapsed']:.2f}s -- "
                "looks like it queued behind the SSE stream instead of "
                "running concurrently"
            )
        finally:
            analysis_module._analyze_stub = original_stub
