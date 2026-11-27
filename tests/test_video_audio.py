"""Video marking, audio part (sigil-video-v2): the soundtrack carries the audio mark too. Skipped without ffmpeg."""
import subprocess

import numpy as np
import pytest

from sigil import audio, video
from sigil.core import PUBLIC_KEY, MarkOptions, Sigil
from test_audio import _music, _write
from test_video import OPTS, _clip, _ffmpeg, _streams
from test_watermark import _photo

pytestmark = pytest.mark.skipif(not video.available(), reason="ffmpeg/ffprobe not installed")


def _clip_with_music(d, name="src.mp4", w=320, h=240, sec=12, fps=10, seed=1):
    """Panning clip with a 12 s music-like soundtrack (8 s clips decode only ~3 in 4 times after a second AAC pass)."""
    wav = _write(d / f"{name}.wav", _music(sec, 44100, seed=seed), 44100)
    n, pan = sec * fps, 1
    base = np.asarray(_photo(w + pan * n + 8, h + n + 8, seed=seed + 2))
    cmd = ["ffmpeg", "-y", "-loglevel", "error", "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{w}x{h}",
           "-framerate", str(fps), "-i", "-", "-i", str(wav), "-map", "0:v", "-map", "1:a", "-c:a", "aac",
           "-b:a", "192k", "-c:v", "libx264", "-crf", "16", "-pix_fmt", "yuv420p", "-shortest", str(d / name)]
    p = subprocess.Popen(cmd, stdin=subprocess.PIPE)
    for i in range(n):
        p.stdin.write(np.ascontiguousarray(base[i:i + h, pan * i:pan * i + w]).tobytes())
    p.stdin.close()
    assert p.wait() == 0
    return (d / name).read_bytes()


@pytest.fixture(scope="module")
def marked(tmp_path_factory):
    d = tmp_path_factory.mktemp("video_audio")
    sg = Sigil(d / "data")
    src = _clip_with_music(d)
    out, receipt = sg.mark_video(src, MarkOptions(**OPTS, visible_label=False))
    (d / "marked.mp4").write_bytes(out)
    return sg, d, out, receipt


def test_audio_track_is_marked(marked):
    sg, d, out, receipt = marked
    a = next(s for s in _streams(d / "marked.mp4") if s["codec_type"] == "audio")
    assert a["codec_name"] == "aac"
    rep = sg.verify_video(out)
    wm = rep["watermark"]
    assert wm["scheme"] == video.SCHEME == "sigil-video-v2"
    assert wm["content_id"] == receipt.content_id and wm["found_in"] == ["frames", "audio"]
    assert wm["audio"]["found"] and wm["audio"]["content_id"] == receipt.content_id
    assert rep["c2pa"]["sigil"]["watermark"] == "sigil-video-v2"


def test_extracted_soundtrack_verifies_as_audio(marked):
    sg, d, _, receipt = marked
    m4a = _ffmpeg(d / "marked.mp4", d / "track.m4a", "-vn", "-c:a", "copy")
    rep = sg.verify_audio(m4a)
    assert rep["watermark"]["found"] and rep["watermark"]["content_id"] == receipt.content_id
    assert rep["record"]["modality"] == "video"


def test_audio_mark_survives_when_frames_do_not(marked):
    """CRF 32 + 0.5x downscale wipes the frame mark (README Honest limits); the soundtrack still carries it."""
    sg, d, _, receipt = marked
    data = _ffmpeg(d / "marked.mp4", d / "crushed.mp4", "-vf", "scale=160:120", "-c:v", "libx264",
                   "-crf", "32", "-c:a", "aac", "-b:a", "96k")
    wm = sg.verify_video(data)["watermark"]
    assert wm["found"] and wm["content_id"] == receipt.content_id and "audio" in wm["found_in"]


def test_muted_copy_still_found_in_frames(marked):
    sg, d, _, receipt = marked
    data = _ffmpeg(d / "marked.mp4", d / "muted.mp4", "-an", "-c:v", "copy")
    wm = sg.verify_video(data)["watermark"]
    assert wm["found_in"] == ["frames"] and wm["content_id"] == receipt.content_id and wm["audio"] is None


def test_short_or_silent_audio_is_copied_unmarked(tmp_path):
    src = tmp_path / "short.mp4"
    _clip(src, n=48)                                        # 2 s tone: below audio.MIN_SECONDS
    info = video.embed_file(src, tmp_path / "o.mp4", 0x123456789ABC, PUBLIC_KEY)
    assert info["audio_marked"] is False
    assert sorted(s["codec_type"] for s in _streams(tmp_path / "o.mp4")) == ["audio", "video"]
    silent = tmp_path / "silent.mp4"
    _write(tmp_path / "silence.wav", np.zeros((44100 * 5, 2)), 44100)
    _ffmpeg(src, silent, "-i", str(tmp_path / "silence.wav"), "-map", "0:v", "-map", "1:a", "-c:v", "copy",
            "-c:a", "aac")
    assert video.embed_file(silent, tmp_path / "o2.mp4", 1, PUBLIC_KEY)["audio_marked"] is False


def test_unmarked_video_with_music_not_detected(tmp_path):
    sg = Sigil(tmp_path / "data")
    rep = sg.verify_video(_clip_with_music(tmp_path, "plain.mp4", sec=6, seed=4))
    wm = rep["watermark"]
    assert not rep["ai_generated"] and not wm["found"] and wm["found_in"] == []
    assert wm["audio"] is not None and not wm["audio"]["found"]


def test_detect_combines_frames_and_audio(monkeypatch, tmp_path):
    fr = {"found": True, "content_id": "aaa", "confidence": 0.5, "scheme": video.SCHEME,
          "frames_checked": 7, "frames_matched": 1}
    au = {"found": True, "content_id": "bbb", "confidence": 0.9, "scheme": audio.SCHEME, "sync_z": 14.0}
    monkeypatch.setattr(video, "probe", lambda p: {"has_audio": True, "duration": 8})
    monkeypatch.setattr(video, "_detect_frames", lambda p, k, i: dict(fr))
    monkeypatch.setattr(video, "_detect_audio", lambda p, k, i: dict(au))
    r = video.detect_file(tmp_path / "x", PUBLIC_KEY)
    assert r["content_id"] == "bbb" and r["found_in"] == ["audio"]     # 1 stray frame loses to a CRC'd audio id
    fr["frames_matched"] = 3
    r = video.detect_file(tmp_path / "x", PUBLIC_KEY)
    assert r["content_id"] == "aaa" and r["found_in"] == ["frames"]    # frames agree: keep them
    fr.update(found=False, content_id=None, confidence=0.0, frames_matched=0)
    r = video.detect_file(tmp_path / "x", PUBLIC_KEY)
    assert r["found"] and r["content_id"] == "bbb" and r["confidence"] == 0.9
