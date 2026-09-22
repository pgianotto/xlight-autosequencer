"""Summary, export, review, and chord-stats commands."""
from __future__ import annotations

import errno
import json
import os
import socket
import sys
import threading
import webbrowser
from pathlib import Path

import click

from src import export as export_mod
from src.analyzer.result import AnalysisResult
from src.cli import cli
from src.cli.helpers import _format_duration, _print_summary_table, _print_breakdown, _rich_error


def _port_in_use(host: str, port: int) -> bool:
    """Pre-flight check so a taken port fails the same way before uvicorn
    even starts, instead of relying on catching whatever uvicorn's own
    bind-failure behavior happens to be (it exits the process itself on
    a bind error rather than raising Python's usual OSError/EADDRINUSE
    the way Werkzeug's dev server did -- see the Piece 2 note in
    openspec/changes/mcp-tool-server/design.html)."""
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        probe.bind((host, port))
    except OSError as exc:
        return exc.errno == errno.EADDRINUSE
    else:
        return False
    finally:
        probe.close()


class _PerRequestThreadContext:
    """Give every request its own asgiref thread-sensitive context.

    Without this, WsgiToAsgi's synchronous Flask calls funnel through
    asgiref's single shared fallback thread (asgiref.sync.SyncToAsync.
    single_thread_executor is a *class-level*, one-worker ThreadPoolExecutor
    used whenever no ThreadSensitiveContext has been established) --
    meaning literally every Flask request in the whole process serializes
    onto one thread regardless of how many concurrent connections uvicorn
    accepts. Confirmed with a real server + real concurrent sockets: an
    open SSE stream blocked an unrelated /api/v1/library request for the
    SSE stream's entire duration without this wrapper, and didn't with it
    (0.00s vs. fully serialized). This is the actual fix for the Piece 3
    risk in openspec/changes/mcp-tool-server/design.html -- uvicorn alone
    does not solve it; Django's own ASGI handler uses this exact pattern
    for the same reason.
    """

    def __init__(self, inner_app):
        self.inner_app = inner_app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.inner_app(scope, receive, send)
            return
        from asgiref.sync import ThreadSensitiveContext
        async with ThreadSensitiveContext():
            await self.inner_app(scope, receive, send)


def _build_asgi_app(flask_app):
    """Merge the Flask review server with the MCP tool server into one ASGI app.

    MCPServer's Streamable HTTP transport (src/review/mcp_server.py) is a
    Starlette (ASGI) app; the existing review server is Flask (WSGI). Both
    are served together with uvicorn instead of Flask's own dev server --
    MCP at /mcp, everything else falling through to Flask via asgiref's
    WsgiToAsgi (wrapped in _PerRequestThreadContext -- see its docstring).
    See openspec/changes/mcp-tool-server/ for why this was chosen over
    running MCP as a second process/port.

    Deliberately built from a bare starlette.routing.Router (not
    Starlette(routes=[Mount(...), Mount("/", ...)])) -- verified empirically
    that two sibling Mounts don't compose the way "mount MCP at a prefix,
    fall through to Flask for everything else" suggests: a bare "/mcp"
    request (no trailing slash) doesn't redirect to "/mcp/" when a second
    Mount("/", ...) is present, because Starlette's redirect-slash retry
    only fires when *no* route in the list gives a full match, and
    Mount("/", ...) always does. Router's own `default=` fallback (used
    for "nothing else matched", not a competing prefix route) sidesteps
    that entirely and lets /mcp's own built-in redirect-slash handling
    work correctly on its own. See tests/integration/test_mcp_asgi_sse.py.
    """
    from asgiref.wsgi import WsgiToAsgi
    from mcp.server.transport_security import TransportSecuritySettings
    from starlette.routing import Mount, Router

    from src.review.mcp_server import mcp as mcp_server

    mcp_app = mcp_server.streamable_http_app(
        streamable_http_path="/",
        # Single-user, local-network server with no auth today -- same
        # trust model as the existing CORS policy in server.py, which
        # reflects any Origin. Without this, the MCP transport's DNS-
        # rebinding protection rejects every request whose Host header
        # isn't localhost/127.0.0.1, which is exactly how a remote client
        # reaches this container (http://<nas-host>:5173/mcp).
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    )
    return Router(
        routes=[Mount("/mcp", app=mcp_app)],
        default=_PerRequestThreadContext(WsgiToAsgi(flask_app)),
        lifespan=mcp_app.router.lifespan_context,
    )


def _serve(flask_app, host: str, port: int, open_url: str | None,
           port_in_use_message: str) -> None:
    """Build the merged ASGI app and serve it with uvicorn (blocking)."""
    if _port_in_use(host, port):
        click.echo(port_in_use_message, err=True)
        sys.exit(5)

    import uvicorn

    asgi_app = _build_asgi_app(flask_app)

    if open_url:
        threading.Timer(0.5, webbrowser.open, args=[open_url]).start()

    uvicorn.run(asgi_app, host=host, port=port, log_level="warning")


# ──────────────────────────────────────────────────────────────────────────────
# summary command
# ──────────────────────────────────────────────────────────────────────────────

@cli.command("summary")
@click.argument("analysis_json", type=click.Path(exists=True, dir_okay=False))
@click.option("--top", "top_n", default=None, type=int, help="Show only top N tracks")
@click.option("--breakdown", "show_breakdown", is_flag=True, default=False,
              help="Show per-criterion score breakdown for each track")
def summary_cmd(analysis_json: str, top_n: int | None, show_breakdown: bool) -> None:
    """Print the scored summary table from an existing analysis JSON."""
    try:
        result = export_mod.read(analysis_json)
    except Exception as exc:
        click.echo(f"ERROR: Cannot read {analysis_json}: {exc}", err=True)
        sys.exit(1)

    duration_str = _format_duration(result.duration_ms)
    click.echo(
        f"Source: {result.filename} ({duration_str}) | BPM: {result.estimated_tempo_bpm} "
        f"| Analyzed: {result.run_timestamp} | {len(result.timing_tracks)} tracks"
    )

    pr = result.phoneme_result
    if pr is not None:
        word_count = len(pr.word_track.marks)
        phoneme_count = len(pr.phoneme_track.marks)
        lang = pr.language
        model = pr.model_name
        click.echo(
            f"Phonemes: {word_count} words | {phoneme_count} phonemes "
            f"| Language: {lang} | Model: {model}"
        )

    ss = result.song_structure
    if ss is not None and ss.segments:
        click.echo(f"\nSong Structure ({len(ss.segments)} segments):")
        for seg in ss.segments:
            start = _format_duration(seg.start_ms)
            end = _format_duration(seg.end_ms)
            duration_s = (seg.end_ms - seg.start_ms) / 1000
            click.echo(f"  {start} – {end}  ({duration_s:.1f}s)  {seg.label}")

    tracks = result.timing_tracks
    if top_n is not None:
        tracks = sorted(tracks, key=lambda t: t.quality_score, reverse=True)[:top_n]

    _print_summary_table(tracks)

    if show_breakdown:
        _print_breakdown(tracks)


# ──────────────────────────────────────────────────────────────────────────────
# export command
# ──────────────────────────────────────────────────────────────────────────────

@cli.command("export")
@click.argument("analysis_json", type=click.Path(exists=True, dir_okay=False))
@click.option("--select", "select_names", default=None, help="Comma-separated track names")
@click.option("--top", "top_n", default=None, type=int, help="Top N tracks by quality score")
@click.option("--output", default=None, help="Output path (default: <input>_selected.json)")
def export_cmd(
    analysis_json: str,
    select_names: str | None,
    top_n: int | None,
    output: str | None,
) -> None:
    """Filter an existing analysis to a subset of tracks and write a new JSON."""
    if select_names is None and top_n is None:
        click.echo("ERROR: Provide --select <names> or --top <N>.", err=True)
        sys.exit(5)

    try:
        result = export_mod.read(analysis_json)
    except Exception as exc:
        click.echo(f"ERROR: Cannot read {analysis_json}: {exc}", err=True)
        sys.exit(1)

    if top_n is not None:
        selected = sorted(
            result.timing_tracks, key=lambda t: t.quality_score, reverse=True
        )[:top_n]
        label = f"top {top_n}"
    else:
        names = {n.strip() for n in select_names.split(",")}
        track_map = {t.name: t for t in result.timing_tracks}
        missing = names - track_map.keys()
        if missing:
            click.echo(
                f"ERROR: Track(s) not found: {', '.join(sorted(missing))}. "
                f"Available: {', '.join(sorted(track_map.keys()))}",
                err=True,
            )
            sys.exit(4)
        selected = [track_map[n] for n in names if n in track_map]
        label = f"selected {len(selected)}"

    in_path = Path(analysis_json)
    out_path = output or str(in_path.parent / (in_path.stem.replace("_analysis", "") + "_selected.json"))

    filtered = AnalysisResult(
        schema_version=result.schema_version,
        source_file=result.source_file,
        filename=result.filename,
        duration_ms=result.duration_ms,
        sample_rate=result.sample_rate,
        estimated_tempo_bpm=result.estimated_tempo_bpm,
        run_timestamp=result.run_timestamp,
        algorithms=[
            a for a in result.algorithms
            if a.name in {t.algorithm_name for t in selected}
        ],
        timing_tracks=selected,
    )

    try:
        export_mod.write(filtered, out_path)
    except OSError as exc:
        click.echo(f"ERROR: Cannot write output: {exc}", err=True)
        sys.exit(3)

    click.echo(
        f"Exporting {label} of {len(result.timing_tracks)} tracks from {analysis_json}"
    )
    _print_summary_table(selected)
    click.echo(f"\nOutput: {out_path}")


# ──────────────────────────────────────────────────────────────────────────────
# review command
# ──────────────────────────────────────────────────────────────────────────────

@cli.command("review")
@click.argument("audio_or_json", required=False, default=None, type=click.Path())
def review_cmd(audio_or_json: str | None) -> None:
    """Launch the track review UI in the default browser.

    AUDIO_OR_JSON can be:
      - A directory: scans for *_hierarchy.json files and shows a scored library
      - A *_hierarchy.json file: opens the timeline directly for that song
      - An audio (.mp3) file: looks up cached analysis via the library
      - Omitted: opens the library home page
    """
    from src.review.server import create_app

    # Docker's -p port mapping only reaches sockets bound to a container's
    # non-loopback interface, so running the review server in a container
    # (e.g. to keep vamp/madmom available for re-analyze) needs 0.0.0.0.
    review_host = os.environ.get("XLIGHT_REVIEW_HOST", "127.0.0.1")

    # Served via uvicorn (see _serve), not Flask's own dev server: the SSE
    # analysis-progress stream (held open for the whole analysis -- many
    # minutes, longer still since the WhisperX/demucs quality changes) must
    # not block other requests, including a new song's file upload.
    # User-reported 2026-08-06: a large import "failed to fetch" -- root
    # cause was the browser's upload request queuing behind an in-flight
    # SSE stream with no free worker thread to accept it, until the browser
    # gave up. Werkzeug's dev server needed threaded=True for this; _serve's
    # merged ASGI app is covered by an explicit SSE-under-uvicorn test
    # instead (tests/integration/test_mcp_asgi_sse.py) rather than assumed
    # equivalent -- see openspec/changes/mcp-tool-server/design.html.

    if audio_or_json is None:
        app = create_app()
        url = "http://127.0.0.1:5173/"
        click.echo(f"Starting review UI at {url}")
        click.echo("Press Ctrl-C to stop.")
        _serve(app, review_host, 5173, url,
               "ERROR: Port 5173 is already in use.\n"
               "Kill the process using that port and try again.")
        return

    given_path = Path(audio_or_json)

    # ── Directory: scan for hierarchy files and open library view ─────────────
    if given_path.is_dir():
        app = create_app(scan_dir=str(given_path.resolve()))
        url = "http://127.0.0.1:5173/library-view"
        click.echo(f"Starting library UI at {url} (scanning {given_path})")
        click.echo("Press Ctrl-C to stop.")
        _serve(app, review_host, 5173, url, "ERROR: Port 5173 is already in use.")
        return

    # ── Audio file path: look up via library ──────────────────────────────────
    if given_path.suffix.lower() != ".json":
        import hashlib
        from src.library import Library
        if not given_path.exists():
            click.echo(f"ERROR: File not found: {audio_or_json}", err=True)
            sys.exit(4)
        h = hashlib.md5()
        with open(given_path, "rb") as fh:
            for chunk in iter(lambda: fh.read(65536), b""):
                h.update(chunk)
        md5 = h.hexdigest()
        entry = Library().find_by_hash(md5)
        if entry is None:
            click.echo(
                f"ERROR: No cached analysis found for {given_path.name}.\n"
                f"Run 'xlight-analyze analyze {audio_or_json}' first.",
                err=True,
            )
            sys.exit(4)
        analysis_json_path = entry.analysis_path
        audio_path_str = entry.source_file
    else:
        # ── JSON path: existing behaviour ─────────────────────────────────────
        analysis_path = given_path
        if not analysis_path.exists():
            click.echo(f"ERROR: Analysis file not found: {audio_or_json}", err=True)
            sys.exit(4)
        try:
            with open(analysis_path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, json.JSONDecodeError) as exc:
            click.echo(f"ERROR: Cannot read {audio_or_json}: {exc}", err=True)
            sys.exit(4)
        audio_path_str = data.get("source_file", "")
        # Resolve relative paths against the analysis JSON's directory
        audio_resolved = Path(audio_path_str)
        if not audio_resolved.is_absolute():
            audio_resolved = (analysis_path.parent / audio_resolved).resolve()
        audio_path_str = str(audio_resolved)
        if not audio_resolved.exists():
            click.echo(
                f"ERROR: Audio file not found: {audio_path_str!r}\n"
                "The analysis JSON's 'source_file' path does not exist on this machine.",
                err=True,
            )
            sys.exit(3)
        analysis_json_path = str(analysis_path.resolve())

    app = create_app(analysis_json_path, audio_path_str)

    url = "http://127.0.0.1:5173/"
    click.echo(f"Starting review UI at {url}")
    click.echo("Press Ctrl-C to stop.")

    _serve(app, review_host, 5173, url,
           "ERROR: Port 5173 is already in use.\n"
           "Kill the process using that port and try again.")


# ──────────────────────────────────────────────────────────────────────────────
# chord-stats command
# ──────────────────────────────────────────────────────────────────────────────

@cli.command("chord-stats")
@click.argument("analysis_json", type=click.Path(exists=True, dir_okay=False))
def chord_stats_cmd(analysis_json: str) -> None:
    """Show chordino change frequency and label distribution from an analysis JSON."""
    from src.analyzer.result import TimingMark

    try:
        data = json.loads(Path(analysis_json).read_text(encoding="utf-8"))
    except Exception as exc:
        click.echo(f"ERROR: Cannot read {analysis_json}: {exc}", err=True)
        sys.exit(1)

    # Support both hierarchy (schema 2.0.0) and flat analysis formats
    marks: list[TimingMark] = []
    source_name = ""
    duration_ms = 0
    estimated_bpm = 0.0

    from src.analyzer.result import is_hierarchy_schema
    if is_hierarchy_schema(data.get("schema_version")):
        # Hierarchy format — chords are in the top-level "chords" field
        source_name = Path(data.get("source_file", analysis_json)).name
        duration_ms = data.get("duration_ms", 0)
        estimated_bpm = data.get("estimated_bpm", 0.0)
        chords = data.get("chords")
        if chords and isinstance(chords, dict):
            raw_marks = chords.get("marks", [])
            marks = [
                TimingMark(
                    time_ms=m["time_ms"],
                    confidence=m.get("confidence"),
                    label=m.get("label"),
                )
                for m in raw_marks
            ]
    else:
        # Flat AnalysisResult format
        try:
            result = export_mod.read(analysis_json)
        except Exception as exc:
            click.echo(f"ERROR: Cannot parse {analysis_json}: {exc}", err=True)
            sys.exit(1)
        source_name = result.filename
        duration_ms = result.duration_ms
        estimated_bpm = result.estimated_tempo_bpm or 0.0
        tracks = [t for t in result.timing_tracks if t.name == "chordino_chords"]
        if tracks:
            marks = tracks[0].marks

    if not marks:
        click.echo("No chordino chord data found in this analysis.", err=True)
        sys.exit(1)

    click.echo(f"\nSource: {source_name}  ({_format_duration(duration_ms)})")
    click.echo(f"Chordino changes: {len(marks)}")

    if len(marks) >= 2:
        intervals = [
            marks[i + 1].time_ms - marks[i].time_ms
            for i in range(len(marks) - 1)
        ]
        avg_ms = sum(intervals) / len(intervals)
        min_ms = min(intervals)
        max_ms = max(intervals)
        click.echo(f"Avg interval:     {avg_ms:.0f} ms  ({avg_ms / 1000:.2f} s)")
        click.echo(f"Min / Max:        {min_ms} ms / {max_ms} ms")
        if estimated_bpm:
            beat_ms = 60_000 / estimated_bpm
            click.echo(f"Beats per change: {avg_ms / beat_ms:.1f}  (at {estimated_bpm} BPM)")
    elif len(marks) == 1:
        click.echo("Only one chord detected — interval stats not available.")

    labels = [m.label for m in marks if m.label]
    if labels:
        from collections import Counter
        counts = Counter(labels).most_common()
        click.echo(f"\nLabel distribution ({len(labels)} labelled / {len(marks)} total):")
        for label, count in counts:
            pct = count / len(marks) * 100
            bar = "#" * int(pct / 2)
            click.echo(f"  {label:<14}  {count:>4}  ({pct:4.1f}%)  {bar}")
    else:
        click.echo("\nNo labels on marks (chordino output may not include chord names).")
