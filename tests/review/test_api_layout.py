"""Tests for layout endpoints — T052.

The active layout resolves to an uploaded override (POST /api/v1/layout)
when present, else the repo-committed layout/xlights_rgbeffects.xml. See
the module docstring in src/review/api/v1/layout.py for why an override
is stored separately from src.settings.get_layout_path() (the
machine-wide xLights setting export must never silently fall back to).
"""
from __future__ import annotations

import io


_VALID_LAYOUT_XML = b"""<?xml version="1.0" encoding="UTF-8"?>
<xrgb>
  <models>
    <model name="TestArch" DisplayAs="Arches" parm1="50" parm2="1" />
    <model name="TestTree" DisplayAs="Tree" parm1="100" parm2="1" />
  </models>
</xrgb>
"""

_NO_MODELS_XML = b"<?xml version=\"1.0\"?><root><notamodel/></root>"


class TestGetLayout:
    def test_returns_200(self, client):
        resp = client.get("/api/v1/layout")
        assert resp.status_code == 200

    def test_layout_fields_present(self, client):
        data = client.get("/api/v1/layout").get_json()
        assert "layout_id" in data
        assert "display_name" in data
        assert "props" in data
        assert "total_pixels" in data
        assert "xml_path" in data
        assert "source" in data

    def test_props_extracted_from_committed_file(self, client):
        data = client.get("/api/v1/layout").get_json()
        assert len(data["props"]) > 0

    def test_default_source_is_committed(self, client):
        data = client.get("/api/v1/layout").get_json()
        assert data["source"] == "committed"


class TestUploadLayout:
    def test_upload_returns_200(self, client):
        resp = client.post(
            "/api/v1/layout",
            data={"layout": (io.BytesIO(_VALID_LAYOUT_XML), "custom.xml")},
            content_type="multipart/form-data",
        )
        assert resp.status_code == 200

    def test_upload_becomes_the_active_layout(self, client):
        client.post(
            "/api/v1/layout",
            data={"layout": (io.BytesIO(_VALID_LAYOUT_XML), "custom.xml")},
            content_type="multipart/form-data",
        )
        data = client.get("/api/v1/layout").get_json()
        assert data["source"] == "uploaded"
        names = {p["name"] for p in data["props"]}
        assert names == {"TestArch", "TestTree"}

    def test_upload_overrides_a_previous_upload(self, client):
        """Uploading a second layout replaces the first, not appends to it —
        this is what lets a user swap layouts, per the original ask."""
        client.post(
            "/api/v1/layout",
            data={"layout": (io.BytesIO(_VALID_LAYOUT_XML), "first.xml")},
            content_type="multipart/form-data",
        )
        second_xml = _VALID_LAYOUT_XML.replace(b"TestArch", b"DifferentArch")
        client.post(
            "/api/v1/layout",
            data={"layout": (io.BytesIO(second_xml), "second.xml")},
            content_type="multipart/form-data",
        )
        data = client.get("/api/v1/layout").get_json()
        names = {p["name"] for p in data["props"]}
        assert "DifferentArch" in names
        assert "TestArch" not in names

    def test_missing_file_returns_400(self, client):
        resp = client.post("/api/v1/layout", data={}, content_type="multipart/form-data")
        assert resp.status_code == 400
        assert resp.get_json()["error"]["code"] == "missing_file"

    def test_invalid_xml_returns_400(self, client):
        resp = client.post(
            "/api/v1/layout",
            data={"layout": (io.BytesIO(b"not xml at all <<<"), "bad.xml")},
            content_type="multipart/form-data",
        )
        assert resp.status_code == 400
        assert resp.get_json()["error"]["code"] == "invalid_xml"

    def test_xml_with_no_models_returns_400(self, client):
        resp = client.post(
            "/api/v1/layout",
            data={"layout": (io.BytesIO(_NO_MODELS_XML), "empty.xml")},
            content_type="multipart/form-data",
        )
        assert resp.status_code == 400
        assert resp.get_json()["error"]["code"] == "no_models"

    def test_invalid_upload_does_not_replace_existing_active_layout(self, client):
        before = client.get("/api/v1/layout").get_json()
        client.post(
            "/api/v1/layout",
            data={"layout": (io.BytesIO(b"garbage"), "bad.xml")},
            content_type="multipart/form-data",
        )
        after = client.get("/api/v1/layout").get_json()
        assert after == before


class TestDeleteUploadedLayout:
    def test_delete_reverts_to_committed_default(self, client):
        client.post(
            "/api/v1/layout",
            data={"layout": (io.BytesIO(_VALID_LAYOUT_XML), "custom.xml")},
            content_type="multipart/form-data",
        )
        assert client.get("/api/v1/layout").get_json()["source"] == "uploaded"

        resp = client.delete("/api/v1/layout")
        assert resp.status_code == 200

        data = client.get("/api/v1/layout").get_json()
        assert data["source"] == "committed"

    def test_delete_with_no_override_is_a_no_op(self, client):
        before = client.get("/api/v1/layout").get_json()
        resp = client.delete("/api/v1/layout")
        assert resp.status_code == 200
        after = client.get("/api/v1/layout").get_json()
        assert after == before
