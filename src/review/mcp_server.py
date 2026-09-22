"""MCP tool server exposing the xLightsAI pipeline to a remote Claude client.

Every tool here is a thin wrapper around the same functions the
``/api/v1/*`` Flask routes call (see ``src/review/api/v1/*.py``) -- no
analysis, generation, or export logic lives in this file. See
``openspec/changes/mcp-tool-server/proposal.md`` and ``design.html`` for
the full design, including why this wraps the ``api/v1`` blueprint (the
live song_id-keyed x-onset dashboard) rather than the legacy hash-keyed
routes in ``src/review/server.py``.

Served merged into the same process as the Flask review server (see
``src/cli/review.py``) at ``/mcp`` via Streamable HTTP -- this module only
builds the ``MCPServer`` instance and registers tools; it does not run
anything on its own.
"""
from __future__ import annotations

import asyncio
import dataclasses
import json
from pathlib import Path
from typing import Any

from mcp.server.mcpserver import Context, MCPServer

mcp = MCPServer(
    name="xonset",
    version="0.1.0",
    instructions=(
        "Tools for driving the xLightsAI / xOnset pipeline: list and import "
        "songs, run analysis, read a song's section structure, browse "
        "themes/effects/variants, generate an xLights sequence, and read "
        "the fixed layout. Every tool operates on the same song library "
        "and session state the x-onset web dashboard uses."
    ),
)


def _jsonable(obj: Any) -> Any:
    """Best-effort JSON-safe conversion for dataclass-heavy return values.

    Recursively converts dataclasses to dicts (``EffectDefinition``,
    ``Theme``, ``EffectVariant``, ...) and falls back to ``str()`` for
    anything else JSON can't natively encode (e.g. an enum member).
    """
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        obj = dataclasses.asdict(obj)
    return json.loads(json.dumps(obj, default=str))


def _tool_error(body: dict, status: int) -> Exception:
    """Turn an api/v1-style ``{"error": {...}}`` response body into an exception.

    4xx (bad input / not found / conflict) -> ValueError; 5xx -> RuntimeError.
    The MCP protocol layer converts any raised exception from a tool
    function into a proper tool-error result for the client.
    """
    err = body.get("error", body)
    message = err.get("message", str(err)) if isinstance(err, dict) else str(err)
    code = err.get("code") if isinstance(err, dict) else None
    full_message = f"{code}: {message}" if code else message
    return RuntimeError(full_message) if status >= 500 else ValueError(full_message)


# ── list_library ─────────────────────────────────────────────────────────────

@mcp.tool()
def list_library() -> dict:
    """List every song in the library, with folders.

    Mirrors ``GET /api/v1/library``. Each song includes its ``song_id``
    (needed by every other per-song tool), ``title``, ``artist``,
    ``status`` (draft / analyzed / themed / source_missing), and
    ``source_exists`` (whether the audio file is still on disk).
    """
    from src.review.storage.library import load_library
    from src.review.api.v1.library import _normalize_folder, _song_with_source_exists

    lib = load_library()
    return {
        "schema_version": lib.get("schema_version", 1),
        "songs": [_song_with_source_exists(s) for s in lib.get("songs", [])],
        "folders": [_normalize_folder(f) for f in lib.get("folders", [])],
    }


# ── import_song ───────────────────────────────────────────────────────────────

@mcp.tool()
def import_song(path: str, folder_id: str | None = None) -> dict:
    """Import a local audio/video file into the library by its absolute path.

    The path must be reachable from inside the server's own container/
    filesystem (not the calling client's) -- mirrors
    ``POST /api/v1/import/by-path``, built for exactly this "no bytes to
    upload, just a path" case. Returns the created (or deduped-existing)
    song record; use its ``song_id`` with ``analyze_song`` next.
    """
    from src.review.api.v1.import_by_path import _import_by_path

    body, status = _import_by_path(path, folder_id)
    if status >= 400:
        raise _tool_error(body, status)
    return body


# ── analyze_song ─────────────────────────────────────────────────────────────

def _extract_failure_message(events: list[dict]) -> str:
    """Find the analysis pipeline's own error message from its event stream.

    Events are heterogeneous ({"overall": {...}} / {"detector": {...}} /
    {"log": {...}}); the failure message lives in the last "overall" event
    with an "error" key (see _analyze_in_background's except block).
    """
    for ev in reversed(events):
        overall = ev.get("overall")
        if isinstance(overall, dict) and overall.get("error"):
            return str(overall["error"])
    return "Analysis failed (no error detail reported)"


@mcp.tool()
async def analyze_song(song_id: str, force: bool = False, ctx: Context = None) -> dict:
    """Run analysis on a library song and commit the result.

    Streams per-stage progress while the pipeline runs (stem separation,
    beat/section detection, lyrics/phoneme alignment, ...) -- this can take
    several minutes for a full song. ``force=True`` re-analyzes even if the
    song is already analyzed; the fresh result is committed with an empty
    assignment carry-forward mapping (there's no user-facing diff-review
    step in a tool call the way the dashboard's Analyze screen has), so a
    forced re-analysis replaces any prior per-section theme customization
    on this song with fresh auto-assigned defaults. A first-time
    (non-force) analysis has no prior customization to lose and persists
    normally.

    Returns the resulting ``sections`` and ``assignments``.
    """
    from src.review.api.v1.analysis import _commit_analyze, _runs, _runs_lock, _start_analyze
    from src.review.storage.assignments import load_session

    start_body, start_status = _start_analyze(song_id, force=force)
    if start_status >= 400:
        raise _tool_error(start_body, start_status)
    run_id = start_body["run_id"]

    # Poll the same in-memory run state the SSE route (/analyze/status)
    # reads, forwarding each new event as MCP progress instead of SSE.
    idx = 0
    run_status = "running"
    while True:
        with _runs_lock:
            state = _runs.get(song_id)
        if state is None:
            raise RuntimeError("Analysis run state disappeared unexpectedly")
        with state.lock:
            events = list(state.events)
            run_status = state.status

        while idx < len(events):
            ev = events[idx]
            idx += 1
            if ctx is None:
                continue
            overall = ev.get("overall")
            detector = ev.get("detector")
            log = ev.get("log")
            if overall is not None:
                await ctx.report_progress(
                    progress=float(overall.get("progress", 0.0)), total=1.0,
                    message=str(overall.get("status", "")),
                )
            elif detector is not None:
                await ctx.info(f"{detector} ({ev.get('library', '?')}): {ev.get('status', '')}")
            elif log is not None:
                await ctx.info(str(log.get("message", "")))

        if run_status != "running":
            break
        await asyncio.sleep(0.2)

    if run_status == "failed":
        raise RuntimeError(_extract_failure_message(events))

    # Force runs stay "pending" until committed (that's the whole point of
    # force -- the pipeline holds the result back so a diff review can
    # happen before it overwrites the session). Non-force runs already
    # auto-persisted to the session inside _analyze_in_background -- do
    # NOT also call _commit_analyze here: its own non-force fallback path
    # recomputes assignment defaults without the hierarchy/story context
    # _analyze_in_background had, which would silently regress the
    # already-good result that's already saved.
    if force:
        commit_body, commit_status = _commit_analyze(song_id, run_id, [])
        if commit_status >= 400:
            raise _tool_error(commit_body, commit_status)
        return commit_body

    session = load_session(song_id) or {}
    return {
        "sections": session.get("sections", []),
        "assignments": session.get("assignments", []),
    }


# ── get_song_story ───────────────────────────────────────────────────────────

@mcp.tool()
def get_song_story(song_id: str) -> dict:
    """Get an analyzed song's section structure.

    Returns the section list (index, label, kind, start_ms, end_ms) from
    the current session -- mirrors ``GET /api/v1/songs/<song_id>/sections``
    -- plus the fuller per-section story (character/energy/lighting
    narrative) from the on-disk ``<song>_story.json`` when present.
    """
    from src.review.api.v1.sections import _load_song
    from src.review.storage.assignments import load_session

    song, _lib = _load_song(song_id)
    if song is None:
        raise ValueError("song_not_found: Song not found")
    if song.get("status") == "draft":
        raise ValueError("not_analyzed: Song has not been analyzed yet")

    session = load_session(song_id)
    if session is None:
        raise ValueError("not_analyzed: No analysis result available")

    result: dict = {
        "sections": session.get("sections", []),
        "ghost_boundaries": session.get("ghost_boundaries", []),
    }

    source_paths = song.get("source_paths") or []
    src = next((Path(p) for p in source_paths if Path(p).exists()), None)
    if src is not None:
        story_path = src.parent / (src.stem + "_story.json")
        if story_path.exists():
            try:
                result["story"] = json.loads(story_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                pass

    return result


# ── list_themes / list_effects / list_variants ─────────────────────────────

@mcp.tool()
def list_themes() -> dict:
    """List every theme (built-in + custom) available for section assignment.

    Mirrors ``GET /api/v1/themes`` -- the same catalog the Theme screen's
    theme picker shows, in the same dashboard-facing shape (theme_id,
    name, description, accent color, swatches, mood/occasion/genre tags).
    """
    from src.review.api.v1.themes import _load_themes

    return {"themes": _load_themes()}


@mcp.tool()
def list_effects() -> dict:
    """List the cataloged xLights effect definitions the generator can place.

    Reads ``src/effects/builtin_effects.json`` plus any
    ``~/.xlight/custom_effects/*.json`` overrides via the same loader the
    generator uses internally (``src.effects.library.load_effect_library``).
    """
    from src.effects.library import load_effect_library

    lib = load_effect_library()
    return {
        "schema_version": lib.schema_version,
        "target_xlights_version": lib.target_xlights_version,
        "effects": [_jsonable(e) for e in lib.effects.values()],
    }


@mcp.tool()
def list_variants() -> dict:
    """List the cataloged effect-parameter variants the generator can place.

    Reads the built-in per-effect variant catalog plus any
    ``~/.xlight/custom_variants/*.json`` overrides via the same loader the
    generator uses internally (``src.variants.library.load_variant_library``).
    """
    from src.effects.library import load_effect_library
    from src.variants.library import load_variant_library

    variant_lib = load_variant_library(effect_library=load_effect_library())
    return {
        "schema_version": variant_lib.schema_version,
        "variants": [_jsonable(v) for v in variant_lib.variants.values()],
    }


# ── generate_sequence ────────────────────────────────────────────────────────

@mcp.tool()
async def generate_sequence(
    song_id: str,
    genre: str | None = None,
    occasion: str | None = None,
    variation_seed: int | None = None,
    reroll: bool = False,
    ctx: Context = None,
) -> dict:
    """Generate an xLights .xsq sequence for a themed song.

    The song must already be fully themed (every section has a confirmed
    theme -- see the Theme screen / ``list_themes``) and the repo's
    committed layout (``layout/xlights_rgbeffects.xml``) must be present.
    Mirrors ``POST /api/v1/songs/<song_id>/export`` (the live export path,
    ``src.evaluation.generator_runner.run`` -- not the older
    ``src/generator/plan.py`` hash-keyed path). Polls until the export
    finishes or fails; returns the output file's server-side path (the
    caller needs filesystem access to it, same as any other MCP tool here
    -- there is no download/byte-transfer step).
    """
    from src.review.api.v1.export import _exports, _exports_lock, _song_exports, _start_export

    body: dict = {}
    if genre is not None:
        body["genre"] = genre
    if occasion is not None:
        body["occasion"] = occasion
    if variation_seed is not None:
        body["variation_seed"] = variation_seed
    if reroll:
        body["reroll"] = True

    start_body, start_status = _start_export(song_id, body)
    if start_status >= 400:
        raise _tool_error(start_body, start_status)
    export_id = start_body["export_id"]

    idx = 0
    while True:
        with _exports_lock:
            state = _exports.get(export_id)
        if state is None:
            raise RuntimeError("Export run state disappeared unexpectedly")
        with state.lock:
            events = list(state.events)
            status = state.status

        while idx < len(events):
            ev = events[idx]
            idx += 1
            if ctx is not None:
                await ctx.report_progress(
                    progress=float(ev.get("progress", 0.0)), total=1.0,
                    message=str(ev.get("stage") or ev.get("detail") or ""),
                )

        if status != "running":
            break
        await asyncio.sleep(0.2)

    if status == "failed":
        error_msg = next((e.get("error") for e in reversed(events) if e.get("error")), None)
        raise RuntimeError(error_msg or "Sequence generation failed")

    return {
        "export_id": export_id,
        "song_id": song_id,
        "output_path": state.output_path,
        "variation_seed": state.variation_seed,
    }


# ── get_layout_info ──────────────────────────────────────────────────────────

@mcp.tool()
def get_layout_info() -> dict:
    """Read the fixed xLights layout (props/groups/networks) generation targets.

    Mirrors the layout ``get_committed_layout()`` used by the Export
    screen and generator -- reads ``layout/xlights_rgbeffects.xml`` (and
    ``xlights_networks.xml`` when present), the single fixed layout this
    repo generates sequences against.
    """
    from src.review.api.v1.layout import get_committed_layout

    layout = get_committed_layout()
    if layout is None:
        raise ValueError(
            "layout_missing: layout/xlights_rgbeffects.xml is missing from the repo"
        )
    return layout
