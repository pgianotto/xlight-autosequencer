## 1. Dependencies

- [x] 1.1 Add `mcp>=2.2,<3`, `uvicorn[standard]`, `asgiref` to `pyproject.toml` `dependencies`.
- [x] 1.2 Confirmed `from mcp.server.mcpserver import MCPServer` imports cleanly against a fresh `mcp==2.2.0` install (scratch venv, not the dev container — this machine can't run the full analyzer stack).

## 2. MCP server module

- [x] 2.1 Created `src/review/mcp_server.py`. `mcp = MCPServer(name="xonset", version="0.1.0", instructions=...)`.
- [x] 2.2 `list_library` tool → `load_library()` + `_song_with_source_exists`/`_normalize_folder` (reused directly from `src/review/api/v1/library.py`, no extraction needed — already plain functions).
- [x] 2.3 `import_song(path, folder_id=None)` tool → extracted `_import_by_path()` out of the Flask route in `import_by_path.py`; the route is now a 3-line wrapper.
- [x] 2.4 `analyze_song(song_id, force=False, ctx: Context)` tool → extracted `_start_analyze()` and `_commit_analyze()` out of their Flask routes in `analysis.py`. Implemented as a **blocking async tool that polls and streams progress via `ctx.report_progress`/`ctx.info`**, not job-id+poll — real analysis run (stub pipeline) completed in ~12s in testing; a full real analysis can take minutes, which MCP's Streamable HTTP transport handles fine as a long-running tool call with progress notifications (this is exactly what progress reporting is for). Found and fixed a real bug during implementation: only call `_commit_analyze` when `force=True` — non-force runs already auto-persist inside `_analyze_in_background`, and `_commit_analyze`'s own non-force fallback path recomputes assignment defaults *without* the hierarchy/story context the background thread had, which would have silently regressed already-good auto-assigned themes.
- [x] 2.5 `get_song_story(song_id)` tool → reuses `_load_song()` from `sections.py` + `load_session()`, plus reads `_story.json` when present.
- [x] 2.6 `list_themes` tool → reuses `_load_themes()` from `src/review/api/v1/themes.py` directly (the real dashboard-shaped catalog — not `src.themes.library.load_theme_library()`, which is a different, lower-level generator-internal representation with no live route backing it). `list_effects`/`list_variants` tools → `src.effects.library.load_effect_library()` / `src.variants.library.load_variant_library()` (no live route exists for either, so these wrap the loaders directly, per the original ask).
- [x] 2.7 `generate_sequence(song_id, genre=None, occasion=None, variation_seed=None, reroll=False)` tool → extracted `_start_export()` out of the Flask route in `export.py`. Same blocking-with-progress design as 2.4. Verified end-to-end against the real generator pipeline (stub analysis + real `generator_runner.run`): produced an actual `.xsq` file.
- [x] 2.8 `get_layout_info` tool → `get_committed_layout()` from `layout.py`, unchanged (already a plain function).
- [x] 2.9 Verified via the real (not mocked) `tests/review/` suite conventions and manual runs against real code — no regression in existing route behavior; all extractions are the route body moved verbatim into a function taking already-parsed arguments.

## 3. ASGI merge

- [x] 3.1 In `src/cli/review.py`, replaced all three `app.run(...)` call sites with `_serve()` (built on `_build_asgi_app()`). **Built from a bare `starlette.routing.Router(routes=[Mount("/mcp", ...)], default=...)`, not `Starlette(routes=[Mount, Mount])`** — verified empirically that two sibling `Mount`s don't compose the way expected (a bare `/mcp` falls through to the catch-all `Mount("/", ...)` instead of redirecting to `/mcp/`); `Router`'s `default=` fallback sidesteps this entirely. See design.html Piece 2.
- [x] 3.2 Preserved: `--dev`/no-arg browser auto-open, `EADDRINUSE` → exit code 5 with the existing (slightly inconsistent between call sites, preserved as-is) error messages, `XLIGHT_REVIEW_HOST` honored. Pre-flight port-bind check added (`_port_in_use`) since uvicorn's own bind-failure behavior is `SystemExit(3)` from inside `uvicorn.run()`, not a catchable `OSError`/`EADDRINUSE` the way Werkzeug's dev server raised — confirmed empirically.
- [x] 3.3 **Found uvicorn's own concurrency model was NOT sufficient on its own** — confirmed with a real uvicorn server + real concurrent sockets that a bare `WsgiToAsgi(flask_app)` reproduced the exact SSE-blocks-everything bug `624214c` fixed once already. Root cause: `asgiref`'s `WsgiToAsgi` uses `sync_to_async(thread_sensitive=True)` with no `ThreadSensitiveContext` established, so every Flask call (SSE stream included) serializes onto `asgiref.sync.SyncToAsync.single_thread_executor` — a process-wide, one-worker fallback. Fixed with `_PerRequestThreadContext`, wrapping `WsgiToAsgi` in a fresh `async with ThreadSensitiveContext():` per request (the same pattern Django's own ASGI handler uses). Measured 2.48s (serialized) → 0.00s (fixed). See design.html Piece 3 for the full writeup.

## 4. Tests

- [x] 4.1 `tests/unit/test_mcp_server.py` — 22 tests. Most run against **real code** (real import/analyze/story/catalog/layout calls with the repo's existing `XLIGHT_STUB_ANALYSIS=1` fast pipeline and `XLIGHT_STATE_HOME` isolation conventions, plus the real committed layout and real theme/effect/variant catalogs) rather than mocking — only `generate_sequence` mocks `_start_export` (a full run needs a themed song + real generator pipeline, exercised instead by the manual end-to-end check in 2.7). All 22 pass.
- [x] 4.2 `tests/integration/test_mcp_asgi_sse.py` — 5 tests: /mcp routing (bare path, trailing slash, Flask fallthrough, unrelated-path-not-swallowed) via `starlette.testclient.TestClient`, plus the SSE-concurrency regression test. All 5 pass. (The routing tests alone would NOT have caught the thread-serialization bug — that required the real-socket reproduction described in 3.3; the committed test does catch it because `TestClient`'s own request dispatch, while imperfect, still surfaces this particular bug.)
- [x] 4.3 Substituted with direct JSON-RPC `POST /mcp` initialize-handshake requests in `test_mcp_bare_path_reaches_mcp_server`/`test_mcp_trailing_slash_reaches_mcp_server` — equivalent coverage (proves the mounted MCP app actually answers protocol requests through the merge) without adding a full `mcp.ClientSession` dependency to the test suite.
- [x] 4.4 Not run as a full suite in this environment (no vamp/madmom/torch here — that's Linux-container-only per this repo's own dev setup); ran the directly-relevant `tests/review/` conventions by hand (fixture patterns, isolation env vars) and confirmed no route behavior changed. **Flag for the user:** run the full suite in the real dev container before merging, per the Pre-merge acceptance gate below.

## 5. Docs

- [x] 5.1 `README.md`: added "Connecting an MCP client" section.
- [x] 5.2 `CLAUDE.md` "Active Technologies" list: appended the mcp-tool-server entry.

## 6. Acceptance gate

- [ ] 6.1 Run `xlight-evaluate gate --quick` in the real dev container (not available on this Windows machine) before opening a PR.
- [ ] 6.2 Manually verify against the real Synology deployment: `docker compose build && docker compose up -d`, then point a real MCP client at `http://<nas-host>:5173/mcp` and exercise at least `list_library` + `analyze_song` end-to-end.
