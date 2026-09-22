## 1. Dependencies

- [ ] 1.1 Add `mcp>=2.2,<3`, `uvicorn[standard]`, `asgiref` to `pyproject.toml` `dependencies` (or a new `mcp` extra if the user prefers it optional — confirm).
- [ ] 1.2 `pip install -e ".[all]"` (or the relevant extra) locally and confirm `from mcp.server.mcpserver import MCPServer` imports cleanly.

## 2. MCP server module

- [ ] 2.1 Create `src/review/mcp_server.py`. Build `mcp = MCPServer(name="xonset", version=...)`.
- [ ] 2.2 `list_library` tool → `src/review/storage/library.py::load_library()` + `_song_with_source_exists`-equivalent enrichment (reuse or extract the existing helper from `src/review/api/v1/library.py` rather than duplicating it).
- [ ] 2.3 `import_song(path, folder_id=None)` tool → extract the body of `import_by_path()` (`src/review/api/v1/import_by_path.py`) into a plain function both the Flask route and this tool call, rather than duplicating the extension/size-limit branching.
- [ ] 2.4 `analyze_song(song_id, force=False, ctx: Context)` tool → extract the orchestrator-invocation logic from `src/review/api/v1/analysis.py`'s analyze-start route into a callable both the route and the tool use; forward `run_orchestrator`'s `progress_callback` into `ctx.report_progress(idx, total, name)`. Return the finished summary (or run_id + poll instructions if the tool call would otherwise block past a reasonable timeout — decide based on typical analysis duration; flag if it's long enough to need async job semantics rather than a blocking tool call).
- [ ] 2.5 `get_song_story(song_id)` tool → reuse `get_sections()`'s section-loading logic from `src/review/api/v1/sections.py`, plus read `_story.json` for the richer per-section fields when present.
- [ ] 2.6 `list_themes`, `list_effects`, `list_variants` tools → `load_theme_library()`, `load_effect_library()`, `load_variant_library()` respectively. Return built-in + custom entries as the existing loaders already merge them.
- [ ] 2.7 `generate_sequence(song_id, genre=None, occasion=None, variation_seed=None, reroll=False)` tool → extract `start_export()`'s job-building logic (song/layout/session validation + `run_generator` call) from `src/review/api/v1/export.py` into a shared function. Decide sync-blocking vs job-id+poll the same way as 2.4 — export can take minutes.
- [ ] 2.8 `get_layout_info` tool → `get_committed_layout()` from `src/review/api/v1/layout.py`.
- [ ] 2.9 Every extracted "shared function" from 2.3/2.4/2.7 must leave the original Flask route calling it unchanged in behavior — run the existing `tests/review/` suite for those routes after each extraction to confirm no regression before wiring the MCP tool on top.

## 3. ASGI merge

- [ ] 3.1 In `src/cli/review.py`, replace all three `app.run(host=review_host, port=5173, ...)` call sites with a shared helper that builds the `Starlette(routes=[Mount("/mcp", ...), Mount("/", WsgiToAsgi(flask_app))])` app and calls `uvicorn.run(...)`.
- [ ] 3.2 Preserve existing CLI behavior: `--dev`/no-arg browser auto-open (`threading.Timer(0.5, webbrowser.open, ...)`), `EADDRINUSE` → exit code 5 with the existing error message, `XLIGHT_REVIEW_HOST` env var honored.
- [ ] 3.3 Confirm Werkzeug's `threaded=True` argument has no uvicorn equivalent needed as a passthrough — uvicorn's default worker model already handles concurrent requests; document the reasoning inline as a comment referencing commit `624214c` so a future reader understands why this isn't a silent regression.

## 4. Tests

- [ ] 4.1 `tests/unit/test_mcp_server.py` — one test per tool, mocking the underlying `api/v1`/library/story functions (matching this repo's existing pattern of not hitting real disk/analysis in unit tests).
- [ ] 4.2 `tests/integration/test_mcp_asgi_sse.py` — boot the merged ASGI app (e.g. via `httpx.AsyncClient` against the Starlette app directly, no real socket needed), open `/api/v1/songs/<id>/analyze/status` (or a stubbed long-lived SSE route) and, while it's open, issue `GET /api/v1/library` concurrently; assert the second request completes without waiting for the first to close.
- [ ] 4.3 One end-to-end MCP smoke test: connect an in-process MCP client session to the mounted app, call `list_library`, assert a well-formed tool result.
- [ ] 4.4 Run the full existing `tests/review/` suite unmodified — must stay green (proves the Flask side is untouched).

## 5. Docs

- [ ] 5.1 `README.md`: new section "Connecting an MCP client" — `http://<nas-host>:5173/mcp`, Streamable HTTP transport, example Claude Code / `claude mcp add` config.
- [ ] 5.2 `CLAUDE.md` "Active Technologies" list: append the mcp-tool-server entry per its existing per-feature convention (see the many `NNN-feature-name` lines already there).

## 6. Acceptance gate

- [ ] 6.1 Run `xlight-evaluate gate --quick` locally before opening a PR.
- [ ] 6.2 Manually verify against the real Synology deployment: `docker compose build && docker compose up -d`, then point a real MCP client at `http://<nas-host>:5173/mcp` and exercise at least `list_library` + `analyze_song` end-to-end.
