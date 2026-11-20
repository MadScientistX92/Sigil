"""Video marking: the v2 image watermark on every frame, re-encoded to H.264 MP4 (C2PA is added by core).

Needs the ``ffmpeg`` and ``ffprobe`` binaries on PATH (``apt install ffmpeg``). Frames stream through pipes,
so memory stays at a few frames regardless of clip length.

Why every frame and why CRF 18: H.264 P-frames are predicted from earlier frames with motion compensation,
and a watermark that stays fixed while the content moves gets smeared away in the residual at normal
bitrates. Sigil's own encode keeps enough bitrate for the mark to survive in every frame. After someone
else re-encodes the file, the mark is most reliably found in their keyframes (I-frames are coded from
scratch, like a JPEG), so the detector reads keyframes first and then evenly spaced frames.
"""
from __future__ import annotations

import io
import json
import shutil
import subprocess
from collections import Counter
from fractions import Fraction
from pathlib import Path
from typing import Callable

import numpy as np
from PIL import Image

from . import watermark

SCHEME = "sigil-video-v1"         # per-frame sigil-dctqim-v2, H.264 CRF 18
CRF = 18
PRESET = "medium"
MAX_SECONDS = 600
KEYFRAMES = 6                     # keyframes checked first when verifying
SPREAD = 6                        # then up to this many evenly spaced frames
AGREE = 2                         # stop early once this many frames agree on one content id
TIMEOUT = 900


class VideoError(ValueError):
    pass


def available() -> bool:
    return bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))


def _need():
    if not available():
        raise VideoError("video support needs ffmpeg and ffprobe on PATH (apt install ffmpeg)")


def is_video(data: bytes) -> bool:
    """Sniff MP4/MOV (ISO BMFF 'ftyp'), WebM/MKV (EBML) and AVI containers."""
    head = data[:16]
    return head[4:8] == b"ftyp" or head[:4] == b"\x1a\x45\xdf\xa3" or (head[:4] == b"RIFF" and head[8:12] == b"AVI ")


def probe(path: Path) -> dict:
    _need()
    r = subprocess.run(["ffprobe", "-v", "error", "-print_format", "json", "-show_streams", "-show_format",
                        str(path)], capture_output=True, timeout=60)
    if r.returncode != 0:
        raise VideoError("could not read video: " + r.stderr.decode(errors="replace")[-300:].strip())
    info = json.loads(r.stdout or b"{}")
    streams = info.get("streams", [])
    v = next((s for s in streams if s.get("codec_type") == "video"), None)
    if v is None:
        raise VideoError("no video stream found")
    fps = Fraction(0)
    for k in ("avg_frame_rate", "r_frame_rate"):
        if fps <= 0 and v.get(k) and not v[k].endswith("/0"):
            fps = Fraction(v[k])
    fps = fps if fps > 0 else Fraction(25)
    dur = float(info.get("format", {}).get("duration") or v.get("duration") or 0)
    w, h = int(v["width"]), int(v["height"])
    rot = 0
    for sd in v.get("side_data_list") or []:
        rot = int(sd.get("rotation", 0) or 0)
    if abs(rot) % 180 == 90:         # ffmpeg auto-rotates on decode
        w, h = h, w
    return {"width": w, "height": h, "fps": fps, "duration": dur,
            "has_audio": any(s.get("codec_type") == "audio" for s in streams)}


def _encode_cmd(src: Path, dst: Path, w: int, h: int, fps: Fraction, audio: str | None) -> list[str]:
    cmd = ["ffmpeg", "-y", "-loglevel", "error", "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{w}x{h}",
           "-framerate", f"{fps.numerator}/{fps.denominator}", "-i", "-"]
    if audio:
        cmd += ["-i", str(src), "-map", "0:v:0", "-map", "1:a:0?", "-c:a", audio]
        if audio == "aac":
            cmd += ["-b:a", "192k"]
    cmd += ["-c:v", "libx264", "-preset", PRESET, "-crf", str(CRF), "-pix_fmt", "yuv420p",
            "-movflags", "+faststart", "-shortest", str(dst)]
    return cmd


def embed_file(src: Path, dst: Path, cid: int, key: bytes,
               frame_fn: Callable[[Image.Image], Image.Image] | None = None) -> dict:
    """Watermark every frame of ``src`` into an H.264 MP4 at ``dst``. Audio is copied when the MP4
    container allows it, otherwise re-encoded to AAC. ``frame_fn`` runs on each frame before marking
    (used for the visible label). Returns the probe info of the source."""
    info = probe(src)
    if info["duration"] > MAX_SECONDS:
        raise VideoError(f"video too long ({info['duration']:.0f}s; max {MAX_SECONDS}s)")
    # yuv420p needs even dimensions: crop (never rescale) one pixel off an odd edge
    w, h = info["width"] // 2 * 2, info["height"] // 2 * 2
    if min(w, h) < watermark.MIN_SIDE:
        raise VideoError(f"video too small to watermark (min side {watermark.MIN_SIDE}px)")
    last = ""
    for audio in (["copy", "aac"] if info["has_audio"] else [None]):
        dec = subprocess.Popen(["ffmpeg", "-loglevel", "error", "-i", str(src), "-map", "0:v:0",
                                "-vf", f"crop={w}:{h}:0:0", "-f", "rawvideo", "-pix_fmt", "rgb24",
                                "-fps_mode", "cfr", "-r", f"{info['fps'].numerator}/{info['fps'].denominator}",
                                "-"], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        enc = subprocess.Popen(_encode_cmd(src, dst, w, h, info["fps"], audio),
                               stdin=subprocess.PIPE, stderr=subprocess.PIPE)
        n, size = 0, w * h * 3
        try:
            while True:
                buf = dec.stdout.read(size)
                if len(buf) < size:
                    break
                frame = Image.fromarray(np.frombuffer(buf, np.uint8).reshape(h, w, 3))
                if frame_fn:
                    frame = frame_fn(frame).convert("RGB")
                enc.stdin.write(np.asarray(watermark.embed(frame, cid, key), np.uint8).tobytes())
                n += 1
            enc.stdin.close()
        except BrokenPipeError:
            pass
        finally:
            dec.stdout.close()
            dec.wait(TIMEOUT)
            enc.wait(TIMEOUT)
        last = (enc.stderr.read() + dec.stderr.read()).decode(errors="replace")[-300:].strip()
        if enc.returncode == 0 and n:
            return info | {"frames": n, "width": w, "height": h}
    raise VideoError("video encode failed: " + (last or "no frames decoded"))


def _keyframe_times(path: Path) -> list[float]:
    r = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                        "packet=pts_time,flags", "-of", "csv=p=0", str(path)], capture_output=True, timeout=120)
    times = []
    for line in r.stdout.decode().splitlines():
        parts = line.split(",")
        if len(parts) >= 2 and "K" in parts[1]:
            try:
                times.append(float(parts[0]))
            except ValueError:
                pass
    return sorted(set(times))


def _frame_at(path: Path, t: float) -> Image.Image | None:
    r = subprocess.run(["ffmpeg", "-loglevel", "error", "-ss", f"{max(t, 0):.3f}", "-i", str(path),
                        "-frames:v", "1", "-f", "image2pipe", "-c:v", "png", "-"], capture_output=True, timeout=60)
    if r.returncode != 0 or not r.stdout:
        return None
    return Image.open(io.BytesIO(r.stdout)).convert("RGB")


def _pick(xs: list[float], k: int) -> list[float]:
    if len(xs) <= k:
        return xs
    return [xs[int(i)] for i in np.linspace(0, len(xs) - 1, k)]


def detect_file(path: Path, key: bytes) -> dict:
    """Look for the Sigil watermark in keyframes, then evenly spaced frames. Returns
    {'found', 'content_id', 'confidence', 'scheme', 'frames_checked', 'frames_matched'}."""
    info = probe(path)
    dur = info["duration"]
    times = _pick(_keyframe_times(path), KEYFRAMES)
    if dur > 0:
        spread = [dur * (i + 0.5) / SPREAD for i in range(SPREAD)]
        times += [t for t in spread if all(abs(t - u) > 0.2 for u in times)]
    if not times:
        times = [0.0]
    votes, checked = Counter(), 0
    for t in times:
        frame = _frame_at(path, t)
        if frame is None:
            continue
        checked += 1
        r = watermark.detect(frame, key)
        if r["found"]:
            votes[r["content_id"]] += 1
            if votes.most_common(1)[0][1] >= AGREE:
                break
    if not votes:
        return {"found": False, "content_id": None, "confidence": 0.0, "scheme": SCHEME,
                "frames_checked": checked, "frames_matched": 0}
    cid, n = votes.most_common(1)[0]
    return {"found": True, "content_id": cid, "confidence": round(n / max(sum(votes.values()), 1), 3),
            "scheme": SCHEME, "frames_checked": checked, "frames_matched": n}
