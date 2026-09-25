"""Layout endpoints — T053.

GET    /api/v1/layout — return the active layout (uploaded override, if any,
                         else the repo-committed xlights_rgbeffects.xml)
POST   /api/v1/layout — upload a replacement layout XML
DELETE /api/v1/layout — remove the uploaded override, revert to the
                         repo-committed default

The active layout is always exactly one, explicitly-known file — never a
silent fallback to xlights_rgbeffects.xml's location as configured by the
machine-wide xLights settings (src.settings.get_layout_path()) — a past
bug (see the comment in src/review/api/v1/export.py) had exports silently
target whatever layout happened to be configured machine-wide instead of
the intended one. An uploaded override is stored in the persistent state
volume (src.review.storage.paths.uploaded_layout_xml_path()) so it
survives container rebuilds, and takes priority over the repo-committed
layout/xlights_rgbeffects.xml when present.
"""
from __future__ import annotations

import datetime
import hashlib
import xml.etree.ElementTree as ET

from flask import jsonify, request

from . import api_v1
from src.paths import get_committed_layout_xml_path
from src.review.storage.paths import uploaded_layout_xml_path


def _now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _file_mtime_iso(path) -> str:
    """ISO timestamp of a file's last modification — reflects when a git
    checkout/pull last wrote it to disk, i.e. the layout's last refresh."""
    return datetime.datetime.fromtimestamp(
        path.stat().st_mtime, tz=datetime.timezone.utc
    ).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_props(root: ET.Element) -> list[dict]:
    """Extract prop list from xlights_rgbeffects root element."""
    model_elems = root.findall(".//model")
    props = []
    pixel_offset = 0
    for m in model_elems:
        name = m.get("name", "")
        display_as = m.get("DisplayAs", "SingleLine")
        parm1 = int(m.get("parm1", "1") or "1")
        parm2 = int(m.get("parm2", "1") or "1")
        pixel_count = max(parm1 * parm2, 1)
        prop = {
            "name": name,
            "display_type": display_as,
            "pixel_count": pixel_count,
            "pixel_range": [pixel_offset, pixel_offset + pixel_count - 1],
        }
        props.append(prop)
        pixel_offset += pixel_count
    return props


def _parse_layout_file(path, source: str) -> dict | None:
    """Parse one layout XML file into the API-facing layout dict, or None
    if it's missing/unreadable. ``source`` is "uploaded" or "committed"."""
    if not path.exists():
        return None

    xml_bytes = path.read_bytes()
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError:
        return None

    props = _parse_props(root)
    total_pixels = sum(p["pixel_count"] for p in props)
    layout_id = "layout_" + hashlib.sha256(xml_bytes).hexdigest()[:6]
    display_name = root.get("name") or root.findtext("layoutGroup") or path.name

    return {
        "layout_id": layout_id,
        "display_name": display_name,
        "imported_at": _file_mtime_iso(path),
        "props": props,
        "total_pixels": total_pixels,
        "xml_path": str(path),
        "source": source,
    }


_active_layout_cache: dict | None = None


def get_committed_layout() -> dict | None:
    """Return the active layout, or None if neither an uploaded override
    nor the repo-committed default exists.

    Resolution order: an uploaded override
    (src.review.storage.paths.uploaded_layout_xml_path(), set via
    POST /api/v1/layout) takes priority; otherwise the repo-committed
    layout/xlights_rgbeffects.xml. Cached after first parse per process --
    invalidated explicitly by the upload/delete routes below, not by
    time or a server restart alone.
    """
    global _active_layout_cache
    if _active_layout_cache is not None:
        return _active_layout_cache

    uploaded = _parse_layout_file(uploaded_layout_xml_path(), source="uploaded")
    if uploaded is not None:
        _active_layout_cache = uploaded
        return _active_layout_cache

    committed = _parse_layout_file(get_committed_layout_xml_path(), source="committed")
    _active_layout_cache = committed
    return _active_layout_cache


@api_v1.route("/layout", methods=["GET"])
def get_layout():
    layout = get_committed_layout()
    if layout is None:
        return jsonify({"layout": None}), 200
    return jsonify(layout), 200


@api_v1.route("/layout", methods=["POST"])
def upload_layout():
    """Upload a replacement layout XML (multipart form field "layout").

    Validates it parses as XML with at least one <model> element before
    accepting it -- rejects an unrelated/corrupt file rather than silently
    activating something that would break every export.
    """
    global _active_layout_cache

    if "layout" not in request.files:
        return jsonify({"error": {"code": "missing_file",
                                   "message": "No file provided (expected form field 'layout')"}}), 400

    f = request.files["layout"]
    if not f.filename:
        return jsonify({"error": {"code": "missing_file", "message": "No file selected"}}), 400

    xml_bytes = f.read()
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError as exc:
        return jsonify({"error": {"code": "invalid_xml",
                                   "message": f"Not a valid XML file: {exc}"}}), 400

    if not root.findall(".//model"):
        return jsonify({"error": {"code": "no_models",
                                   "message": "This XML has no <model> elements -- doesn't look like an "
                                              "xlights_rgbeffects.xml layout file"}}), 400

    dest = uploaded_layout_xml_path()
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(xml_bytes)

    _active_layout_cache = None  # force re-parse on next GET
    layout = get_committed_layout()
    return jsonify(layout), 200


@api_v1.route("/layout", methods=["DELETE"])
def delete_uploaded_layout():
    """Remove the uploaded override, reverting to the repo-committed default."""
    global _active_layout_cache

    path = uploaded_layout_xml_path()
    if path.exists():
        path.unlink()

    _active_layout_cache = None
    layout = get_committed_layout()
    return jsonify({"layout": layout}), 200
