import os
from pathlib import Path


def _state_home() -> Path:
    override = os.environ.get("XLIGHT_STATE_HOME")
    if override:
        return Path(override)
    return Path.home() / ".xlight"


def library_root() -> Path:
    return _state_home() / "library"


def library_json_path() -> Path:
    return library_root() / "library.json"


def song_session_path(song_id: str) -> Path:
    return library_root() / "songs" / song_id / "session.json"


def uploaded_layout_xml_path() -> Path:
    """User-uploaded layout override (POST /api/v1/layout), if any.

    Deliberately separate from src.settings.get_layout_path() -- that's
    the machine-wide xLights layout-path setting the export flow must
    NOT silently fall back to (see the bug-172 comment in
    src/review/api/v1/export.py: falling back to a machine-wide setting
    once caused exports to silently target the wrong layout). This path
    lives in the persistent state volume (survives container rebuilds,
    unlike the repo-committed layout/ dir, which the Dockerfile COPYs
    fresh on every build) and is only ever written by the explicit
    upload endpoint -- so there is still exactly one, explicitly-known
    active layout at any time, just resolved with a clear priority order
    (see get_committed_layout() in src/review/api/v1/layout.py) instead
    of a repo-checkout-only file.
    """
    return _state_home() / "layouts" / "xlights_rgbeffects.xml"
