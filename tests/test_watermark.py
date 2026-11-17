"""Geometry robustness of the v2 image watermark (crop, rescale, both, plus re-encoding)."""
import io

import numpy as np
import pytest
from PIL import Image

from sigil import policy, watermark as wm
from sigil.core import PUBLIC_KEY, MarkOptions, Sigil

CID = 0x5161_6C00_BEEF


def _photo(w=768, h=512, seed=0):
    """Photo-like test image: smooth gradients, shapes with edges, texture and noise."""
    rng = np.random.default_rng(seed)
    yy, xx = np.mgrid[0:h, 0:w] / max(h, w)
    a = np.stack([150 + 80 * np.sin(5 * xx + 2 * yy), 120 + 70 * np.cos(4 * yy - 3 * xx),
                  100 + 60 * np.sin(9 * xx * yy)], -1)
    for _ in range(12):
        cx, cy, r = rng.uniform(0, w), rng.uniform(0, h), rng.uniform(20, 120)
        mask = (np.mgrid[0:h, 0:w][1] - cx) ** 2 + (np.mgrid[0:h, 0:w][0] - cy) ** 2 < r * r
        a[mask] = a[mask] * 0.5 + rng.uniform(0, 255, 3) * 0.5
    a += rng.normal(0, 6, a.shape)
    return Image.fromarray(np.clip(a, 0, 255).astype(np.uint8))


def _jpeg(img, q):
    buf = io.BytesIO()
    img.convert("RGB").save(buf, "JPEG", quality=q)
    return Image.open(io.BytesIO(buf.getvalue()))


def _found(img):
    r = wm.detect(img, PUBLIC_KEY)
    return r["found"] and r["content_id"] == wm.id_to_hex(CID)


@pytest.fixture(scope="module")
def marked():
    return wm.embed(_photo(), CID, PUBLIC_KEY)


def test_v2_is_imperceptible(marked):
    a, b = np.asarray(_photo(), float), np.asarray(marked, float)
    assert 10 * np.log10(255 ** 2 / np.mean((a - b) ** 2)) > 40


@pytest.mark.parametrize("q", [50, 75])
def test_v2_survives_jpeg(marked, q):
    assert _found(_jpeg(marked, q))


def test_v2_survives_webp_and_brightness(marked):
    buf = io.BytesIO()
    marked.save(buf, "WEBP", quality=70)
    assert _found(Image.open(io.BytesIO(buf.getvalue())))
    assert _found(Image.fromarray(np.clip(np.asarray(marked, int) + 25, 0, 255).astype(np.uint8)))


@pytest.mark.parametrize("scale,resample", [(0.5, Image.LANCZOS), (0.6, Image.BILINEAR),
                                            (0.75, Image.BICUBIC), (1.33, Image.BICUBIC),
                                            (2.0, Image.LANCZOS)])
def test_v2_survives_rescale(marked, scale, resample):
    w, h = marked.size
    small = marked.resize((round(w * scale), round(h * scale)), resample)
    r = wm.detect(small, PUBLIC_KEY)
    assert r["found"] and r["content_id"] == wm.id_to_hex(CID)
    assert abs(r["scale"] - scale) / scale < 0.03


@pytest.mark.parametrize("box", [(37, 53, 500, 400), (3, 1, 765, 511), (250, 130, 600, 450)])
def test_v2_survives_crop(marked, box):
    assert _found(marked.crop(box))


def test_v2_survives_crop_rescale_and_jpeg(marked):
    cropped = marked.crop((13, 29, 727, 505)).resize((571, 381), Image.BICUBIC)
    assert _found(_jpeg(cropped, 70))


def test_v2_unmarked_images_not_detected():
    rng = np.random.default_rng(7)
    for seed in range(4):
        img = _photo(seed=seed + 10)
        for s in (1.0, rng.uniform(0.5, 0.9), rng.uniform(1.1, 1.9)):
            w, h = img.size
            assert not wm.detect(img.resize((int(w * s), int(h * s))), PUBLIC_KEY)["found"]
    for v in (0, 128, 255):
        assert not wm.detect(Image.new("RGB", (640, 480), (v, v, v)), PUBLIC_KEY)["found"]


def test_v2_wrong_key_not_detected(marked):
    assert not wm.detect(marked, b"other-key")["found"]


def test_v1_marks_still_verify():
    old = wm.embed_v1(_photo(), CID, PUBLIC_KEY)
    r = wm.detect(_jpeg(old, 80), PUBLIC_KEY)
    assert r["found"] and r["scheme"] == wm.SCHEME_V1 and r["content_id"] == wm.id_to_hex(CID)


def test_tiny_image_reports_not_found():
    r = wm.detect(Image.new("RGB", (40, 40)), PUBLIC_KEY)
    assert not r["found"]


def test_verify_finds_record_after_crop_resize_strip(tmp_path):
    sg = Sigil(tmp_path)
    buf = io.BytesIO()
    _photo().save(buf, "PNG")
    out, rec = sg.mark_image(buf.getvalue(), MarkOptions(provider="Acme AI", system="acme-img"))
    assert rec.content_id and Image.open(io.BytesIO(out)).size == (768, 512)
    edited = Image.open(io.BytesIO(out)).convert("RGB").crop((40, 20, 700, 500)).resize((495, 360))
    shared = io.BytesIO()
    edited.save(shared, "JPEG", quality=80)          # re-encode drops the C2PA manifest
    rep = sg.verify_image(shared.getvalue(), policy.Context("image", ("eu",)))
    assert not rep["c2pa"]["present"]
    assert rep["watermark"]["scheme"] == wm.SCHEME
    assert rep["record"]["content_id"] == rec.content_id and rep["ai_generated"]
