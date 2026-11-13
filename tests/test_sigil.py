import io
from datetime import date

import numpy as np
import pytest
from fastapi.testclient import TestClient
from PIL import Image

from sigil import policy
from sigil.core import MarkOptions, Sigil


def _img(fmt="PNG", size=(640, 480)):
    rng = np.random.default_rng(0)
    h, w = size[1], size[0]
    yy, xx = np.mgrid[0:h, 0:w]
    a = np.stack([xx / w * 200 + 30, yy / h * 180 + 40, (xx + yy) / (h + w) * 160 + 60], -1)
    a += rng.normal(0, 10, a.shape)
    buf = io.BytesIO()
    Image.fromarray(np.clip(a, 0, 255).astype(np.uint8)).save(buf, fmt)
    return buf.getvalue()


@pytest.fixture
def sg(tmp_path):
    return Sigil(tmp_path)


OPTS = dict(provider="Acme AI", system="acme-img", system_version="2.1")


def test_rules_eu_text_requires_marking():
    need = policy.required_controls(policy.Context("text", ("eu",)))
    assert policy.TEXT_MARK in need and "eu.art50.2" in need[policy.TEXT_MARK]


def test_ca_no_threshold_after_sb1000():
    small = policy.applicable_rules(policy.Context("image", ("ca",), monthly_users=10))
    big = policy.applicable_rules(policy.Context("image", ("ca",), monthly_users=2_000_000))
    assert small[0].id == "ca.sb942" and big[0].id == "ca.sb942"


def test_grace_status():
    r = next(r for r in policy.RULES if r.id == "eu.art50.2")
    assert "grace period" in policy.rule_status(r, date(2026, 11, 1))
    assert policy.rule_status(r, date(2027, 1, 1)) == "in force"


def test_mark_then_verify_png(sg):
    out, rec = sg.mark_image(_img(), MarkOptions(**OPTS))
    rep = sg.verify_image(out, policy.Context("image", ("eu", "kr")))
    assert rep["ai_generated"] and rep["watermark"]["found"]
    assert rep["c2pa"]["present"] and rep["c2pa"]["ai_generated"]
    assert rep["record"]["provider"] == "Acme AI"
    assert rep["compliance"]["ok"]


def test_watermark_survives_metadata_strip_and_jpeg(sg):
    out, rec = sg.mark_image(_img(), MarkOptions(**OPTS))
    stripped = io.BytesIO()
    Image.open(io.BytesIO(out)).convert("RGB").save(stripped, "JPEG", quality=70)  # drops C2PA
    rep = sg.verify_image(stripped.getvalue(), policy.Context("image", ("eu",)))
    assert not rep["c2pa"]["present"]
    assert rep["watermark"]["content_id"] == rec.content_id
    assert "stripped" in rep["verdict"]


def test_unmarked_image_flags_gaps(sg):
    rep = sg.verify_image(_img(), policy.Context("image", ("eu",)))
    assert not rep["ai_generated"]
    assert not rep["compliance"]["ok"]


def test_deepfake_gets_visible_label(sg):
    out, rec = sg.mark_image(_img(), MarkOptions(**OPTS, jurisdictions=("in",), deepfake=True))
    assert "visible_label" in rec.controls_applied


def test_jpeg_output_keeps_c2pa(sg):
    out, _ = sg.mark_image(_img("JPEG"), MarkOptions(**OPTS))
    rep = sg.verify_image(out)
    assert rep["c2pa"]["present"] and rep["watermark"]["found"]


def test_text_fingerprint_survives_light_edit(sg):
    text = ("Our quarterly report shows revenue grew twelve percent while operating costs "
            "fell, driven by automation in support and a leaner cloud footprint across regions.")
    _, rec = sg.mark_text(text, MarkOptions(**OPTS))
    edited = text.replace("twelve", "nearly twelve") + " Thanks for reading."
    rep = sg.verify_text(edited)
    assert rep["ai_generated"] and rep["record"]["content_id"] == rec.content_id


def test_evidence_export(sg):
    sg.mark_image(_img(), MarkOptions(**OPTS, org="acme"))
    ev = sg.registry.evidence("acme")
    assert ev["totals"]["marked_items"] == 1 and ev["audit"]


def test_api_roundtrip(tmp_path, monkeypatch):
    import sigil.api as api
    monkeypatch.setattr(api, "sigil", Sigil(tmp_path))
    c = TestClient(api.app)
    assert c.post("/v1/mark/image", files={"file": ("a.png", _img())},
                  data={"provider": "x", "system": "y"}).status_code == 401
    r = c.post("/v1/mark/image", headers={"Authorization": "Bearer dev"},
               files={"file": ("a.png", _img())}, data={"provider": "Acme AI", "system": "acme-img"})
    assert r.status_code == 200 and r.headers["X-Sigil-Content-Id"]
    v = c.post("/v1/verify", files={"file": ("m.png", r.content)}, data={"jurisdictions": "eu"}).json()
    assert v["ai_generated"] and v["compliance"]["ok"]
    assert c.get(f"/v/{r.headers['X-Sigil-Content-Id']}").json()["provider"] == "Acme AI"
    assert c.get("/v1/rules?modality=text&jurisdictions=eu,kr").status_code == 200
    assert "Is this AI-generated" in c.get("/verify").text
