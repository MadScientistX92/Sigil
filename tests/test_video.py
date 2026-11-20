"""Video marking: per-frame watermark + C2PA for MP4. Skipped when ffmpeg is not installed."""
import json
import subprocess

import numpy as np
import pytest
from fastapi.testclient import TestClient
from PIL import Image

from sigil import policy, video
from sigil.core import PUBLIC_KEY, MarkOptions, Sigil
from test_watermark import _photo

pytestmark = pytest.mark.skipif(not video.available(), reason="ffmpeg/ffprobe not installed")

OPTS = dict(provider="Acme AI", system="acme-vid", system_version="1.0")


def _clip(path, w=480, h=272, n=48, fps=24, audio=True, pan=2):
    """Panning photo-like clip (motion is the hard case for a fixed watermark) with an optional tone."""
    base = np.asarray(_photo(w + pan * n + 8, h + n + 8, seed=3))
    cmd = ["ffmpeg", "-y", "-loglevel", "error", "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{w}x{h}",
           "-framerate", str(fps), "-i", "-"]
    if audio:
        cmd += ["-f", "lavfi", "-i", f"sine=frequency=440:duration={n / fps}", "-c:a", "aac", "-shortest"]
    cmd += ["-c:v", "libx264", "-crf", "16", "-pix_fmt", "yuv420p", str(path)]
    p = subprocess.Popen(cmd, stdin=subprocess.PIPE)
    for i in range(n):
        p.stdin.write(np.ascontiguousarray(base[i:i + h, pan * i:pan * i + w]).tobytes())
    p.stdin.close()
    assert p.wait() == 0
    return path.read_bytes()


def _ffmpeg(src, dst, *args):
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(src), *args, str(dst)], check=True)
    return dst.read_bytes()


def _streams(path):
    r = subprocess.run(["ffprobe", "-v", "error", "-print_format", "json", "-show_streams", str(path)],
                       capture_output=True, check=True)
    return json.loads(r.stdout)["streams"]


@pytest.fixture(scope="module")
def marked(tmp_path_factory):
    d = tmp_path_factory.mktemp("video")
    sg = Sigil(d / "data")
    src = _clip(d / "src.mp4")
    out, receipt = sg.mark_video(src, MarkOptions(**OPTS, visible_label=False))
    (d / "marked.mp4").write_bytes(out)
    return sg, d, out, receipt


def test_mark_video_roundtrip(marked):
    sg, d, out, receipt = marked
    assert out[4:8] == b"ftyp" and receipt.modality == "video"
    assert set(receipt.controls_applied) >= {policy.WATERMARK, policy.IDENTIFIER, policy.C2PA}
    kinds = sorted(s["codec_type"] for s in _streams(d / "marked.mp4"))
    assert kinds == ["audio", "video"]                      # audio kept
    rep = sg.verify_video(out, policy.Context("video", ("eu", "ca")))
    assert rep["ai_generated"] and rep["c2pa"]["present"] and rep["c2pa"]["ai_generated"]
    assert rep["c2pa"]["sigil"]["watermark"] == video.SCHEME
    assert rep["watermark"]["found"] and rep["watermark"]["content_id"] == receipt.content_id
    assert rep["record"]["modality"] == "video"
    assert rep["verdict"].startswith("AI-generated: signed provenance and watermark")
    assert rep["compliance"]["ok"]


def test_every_frame_carries_the_mark(marked):
    _, d, _, receipt = marked
    raw = subprocess.run(["ffmpeg", "-loglevel", "error", "-i", str(d / "marked.mp4"), "-f", "rawvideo",
                          "-pix_fmt", "rgb24", "-"], capture_output=True, check=True).stdout
    frames = np.frombuffer(raw, np.uint8).reshape(-1, 272, 480, 3)
    from sigil import watermark as wm
    hits = [wm.detect(Image.fromarray(f), PUBLIC_KEY)["content_id"] == receipt.content_id for f in frames[::4]]
    assert all(hits), hits


def test_survives_metadata_strip(marked):
    sg, d, _, receipt = marked
    stripped = _ffmpeg(d / "marked.mp4", d / "stripped.mp4", "-map_metadata", "-1", "-c", "copy")
    rep = sg.verify_video(stripped)
    assert not rep["c2pa"]["present"]
    assert rep["watermark"]["content_id"] == receipt.content_id
    assert "provenance metadata was stripped" in rep["verdict"]


@pytest.mark.parametrize("args", [
    ("-c:v", "libx264", "-crf", "23"),                                  # typical platform re-encode
    ("-c:v", "libx264", "-crf", "23", "-g", "12"),                      # more keyframes
    ("-ss", "0.5", "-c:v", "libx264", "-crf", "20"),                    # trimmed start
], ids=["crf23", "crf23-gop12", "trim"])
def test_survives_reencode(marked, args):
    sg, d, _, receipt = marked
    data = _ffmpeg(d / "marked.mp4", d / "re.mp4", *args)
    rep = sg.verify_video(data)
    assert rep["watermark"]["found"] and rep["watermark"]["content_id"] == receipt.content_id


def test_unmarked_video_not_detected(marked, tmp_path):
    sg = marked[0]
    rep = sg.verify_video(_clip(tmp_path / "plain.mp4", n=24, audio=False))
    assert not rep["ai_generated"] and not rep["watermark"]["found"]
    assert rep["watermark"]["frames_checked"] >= 2


def test_visible_label_and_odd_size(tmp_path):
    sg = Sigil(tmp_path / "data")
    src = _clip(tmp_path / "odd.mp4", w=482, h=272, n=12, audio=False)
    # x264 rejects odd sizes for yuv420p, so make an odd-width source with a codec that allows it
    odd = _ffmpeg(tmp_path / "odd.mp4", tmp_path / "odd.mkv", "-vf", "crop=481:271:0:0", "-c:v", "ffv1")
    assert src and video.is_video(odd)
    out, receipt = sg.mark_video(odd, MarkOptions(**OPTS, visible_label=True))
    assert policy.VISIBLE in receipt.controls_applied
    (tmp_path / "o.mp4").write_bytes(out)
    v = next(s for s in _streams(tmp_path / "o.mp4") if s["codec_type"] == "video")
    assert (v["width"], v["height"]) == (480, 270)
    frame = subprocess.run(["ffmpeg", "-loglevel", "error", "-i", str(tmp_path / "o.mp4"), "-frames:v", "1",
                            "-f", "rawvideo", "-pix_fmt", "rgb24", "-"], capture_output=True).stdout
    f = np.frombuffer(frame, np.uint8).reshape(270, 480, 3)
    badge = f[241:263, 8:105].mean(axis=2)
    assert (badge < 70).mean() > 0.4                       # dark label badge, bottom left
    assert sg.verify_video(out)["watermark"]["content_id"] == receipt.content_id


def test_rejects_bad_input(tmp_path):
    sg = Sigil(tmp_path)
    with pytest.raises(ValueError):
        sg.mark_video(b"\x00\x00\x00\x18ftypisom not really a video", MarkOptions(**OPTS))
    small = _clip(tmp_path / "small.mp4", w=120, h=96, n=4, audio=False)
    with pytest.raises(ValueError, match="too small"):
        sg.mark_video(small, MarkOptions(**OPTS))
    assert not video.is_video(b"\x89PNG\r\n\x1a\n" + b"\x00" * 16)


def test_api_mark_and_verify_video(marked, tmp_path, monkeypatch):
    from sigil import api
    from sigil.accounts import RateLimiter
    s = Sigil(tmp_path)
    monkeypatch.setattr(api, "sigil", s)
    monkeypatch.setattr(api, "limiter", RateLimiter())
    c = TestClient(api.app)
    src = (marked[1] / "src.mp4").read_bytes()
    r = c.post("/v1/mark/video", headers={"Authorization": "Bearer dev"},
               files={"file": ("clip.mp4", src, "video/mp4")},
               data={"provider": "Acme AI", "system": "acme-vid", "jurisdictions": "eu,kr"})
    assert r.status_code == 200, r.text
    assert r.headers["content-type"] == "video/mp4"
    cid = r.headers["X-Sigil-Content-Id"]
    v = c.post("/v1/verify", files={"file": ("clip.mp4", r.content, "video/mp4")}, data={"jurisdictions": "eu"})
    assert v.status_code == 200, v.text
    assert v.json()["watermark"]["content_id"] == cid and v.json()["ai_generated"]
    usage = s.accounts.usage("default")
    assert usage["metrics"].get("mark.video") == 1
    assert s.accounts.usage("public")["metrics"].get("verify.video") == 1   # keyless verify meters as public
