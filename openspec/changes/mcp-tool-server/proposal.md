# Proposal: MCP tool server

## Why

Every session, a Claude client has to be re-told what this app can do — where
the library lives, what an "analyzed" song looks like, how theming/export
works — because there's no machine-readable interface onto the existing
pipeline, only a browser UI and CLI entry points. This adds a Model Context
Protocol (MCP) server so a remote Claude client (Claude Code, Claude
Desktop, etc. — not necessarily on the same machine as the Synology
container) can drive the real pipeline as typed tools: list the library,
import + analyze a song, read its structure, list themes/effects/variants,
generate a sequence, and read the fixed layout. No analysis, generation, or
export logic is reimplemented — every tool is a thin wrapper calling the
same functions `src/review/api/v1/*.py` already calls.

Two corrections to the original ask, confirmed by reading the code:

1. **The routes to wrap are `/api/v1/songs/<song_id>/...`
   (`src/review/api/v1/*.py`), not the legacy hash-keyed top-level routes**
   in `server.py` (`/upload`, `/analysis`, `/library`, `/export`,
   `/open-from-library`, `/hierarchy-library`, `/open-hierarchy`). Those
   legacy routes back the standalone `xlight-review` CLI tool
   (`src/review/cli.py`, a separate/older code path); the `api/v1` blueprint
   backs the live `x-onset` React dashboard actually deployed on the
   Synology and is where session state (per-section theme assignments,
   overrides, story) actually lives (`src/review/storage/`).
2. **The container actually running on the Synology binds port 5173, not
   5000.** `app/docker-compose.yml` (port 5000, `xlight-review`) is a
   separate local/dev quick-start; the deployed one is `x-onset/compose.yaml`
   + `x-onset/Dockerfile`, running `xlight-analyze review`
   (`src/cli/review.py`), which hardcodes port 5173.

## What changes

- **New dependency**: `mcp>=2.2,<3` (confirmed current API against a fresh
  install — `mcp==2.2.0`; the 1.x `FastMCP`/`fastmcp` module is gone,
  replaced by `from mcp.server.mcpserver import MCPServer`). Pinned to the
  2.x major version in `pyproject.toml`.
- **New module** `src/review/mcp_server.py`: builds an `MCPServer` instance
  and registers the tools below, each calling straight into the existing
  `src/review/api/v1/*` helper functions / `src/review/storage/*` /
  `src/generator`/`src/effects`/`src/themes`/`src/variants` — no new
  analysis/generation/export logic.
- **Merged into the existing Flask process on port 5173** (per your
  decision): `src/review/server.py`'s `create_app()` keeps building the
  Flask app exactly as today; `src/cli/review.py`'s `app.run(...)` calls are
  replaced with a single ASGI launch (`uvicorn`) that serves a Starlette
  app mounting the existing Flask app (wrapped via
  `asgiref.wsgi.WsgiToAsgi`) at `/` and `MCPServer.streamable_http_app()` at
  `/mcp`. One process, one port — `http://<nas-host>:5173/mcp`.
- **New dependencies for the merge**: `uvicorn[standard]`, `asgiref`
  (WSGI→ASGI bridge). Both are small, well-established, and only load at
  process start — no behavior change to any existing Flask route.
- **New CLI-adjacent tests**: `tests/unit/test_mcp_server.py` (tool-level,
  mocking the underlying `api/v1` calls) and one `tests/integration/`
  smoke test that boots the merged ASGI app and calls one tool end-to-end
  against a fixture song.
- **README.md**: a new section on pointing an MCP client at
  `http://<host>:5173/mcp`.

## Tool surface

| Tool | Wraps | Notes |
|---|---|---|
| `list_library` | `src/review/storage/library.load_library()` (mirrors `GET /api/v1/library`) | Songs + folders, with `source_exists` computed the same way `_song_with_source_exists` does. |
| `import_song(path, folder_id=None)` | `src/review/api/v1/import_by_path.py::import_by_path()` internals (`finalize_audio_import`/`finalize_video_import`) | Not in the original list, but required: `analyze_song` needs a `song_id` already in the library, and the only existing "give it a filesystem path" entry point is `/api/v1/import/by-path` (built for exactly this — Tauri's native file picker, not a browser upload). Read-only alternative considered and rejected: silently calling `analyze_song` with a raw path would either reimplement import or bypass the library/session model entirely. |
| `analyze_song(song_id, force=False)` | `src/review/api/v1/analysis.py` analyze-start logic (`run_orchestrator`) | Streams progress via `Context.report_progress` (mcp 2.x's real API), fed by the same `progress_callback` the SSE route already uses. |
| `get_song_story(song_id)` | `src/review/api/v1/sections.py::get_sections()` + the on-disk `_story.json` | Section list plus the fuller per-section character/energy/lighting story where present. |
| `list_themes` | `src/themes/library.py::load_theme_library()` | |
| `list_effects` | `src/effects/library.py::load_effect_library()` | |
| `list_variants` | `src/variants/library.py::load_variant_library()` | |
| `generate_sequence(song_id, genre=None, occasion=None, ...)` | `src/review/api/v1/export.py::start_export()` internals (`src.evaluation.generator_runner.run`) | **Not** `src/generator/plan.py::generate_sequence` — that's the legacy hash-keyed path (`src/review/generate_routes.py`), unused by the live dashboard. This wraps the real one. Blocking call (reuses the existing background-thread + poll pattern, awaited synchronously inside the tool since MCP tool calls are already async). |
| `get_layout_info` | `src/review/api/v1/layout.py::get_committed_layout()` | Reads the fixed `layout/xlights_rgbeffects.xml` + `xlights_networks.xml`. |

## Impact

- Affected code: `src/review/server.py` (unchanged internals, only how it's
  served), `src/cli/review.py` (launch mechanism: `app.run()` →
  `uvicorn.run()` over the merged ASGI app), `pyproject.toml` (new deps +
  optional `mcp` extra), new `src/review/mcp_server.py`, `tests/`,
  `README.md`.
- Regression surface: `src/cli/review.py::review_cmd` is the only caller of
  `app.run(...)` for this server (grepped `src/` and `tests/` — no other
  caller). Existing SSE routes (`/api/v1/songs/<id>/analyze/status`,
  `/api/v1/songs/<id>/export/status`, legacy `/progress`) must keep
  streaming correctly under uvicorn/WsgiToAsgi — this is the actual risk
  in this change and gets explicit test + manual-verification coverage
  (see design doc).
- Historical echo: `src/cli/review.py`'s own comment block documents a real
  prior bug where a *threading* assumption on this exact server (`threaded=True`,
  commit `624214c`) was required because a single-threaded dev server let
  the long-lived SSE analysis stream block new requests (including file
  uploads) for the whole analysis run. The ASGI/uvicorn migration changes
  that concurrency model again — this is exactly the kind of change that
  bug class recurs on, hence explicit SSE-under-uvicorn test coverage
  rather than assuming uvicorn's default worker model reproduces Flask's
  `threaded=True` behavior.
- No changes to `src/analyzer/`, `src/generator/` (effect/theme logic),
  `src/effects/`, `src/themes/` — read-only callers only.
