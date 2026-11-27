"""Video marking: the v2 image watermark on every frame, re-encoded to H.264 MP4 (C2PA is added by core).

Needs the ``ffmpeg`` and ``ffprobe`` binaries on PATH (``apt install ffmpeg``). Frames stream through pipes,
so memory stays at a few frames regardless of clip length.

Why every frame and why CRF 18: H.264 P-frames are predicted from earlier frames with motion compensation,
and a watermark that stays fixed while the content moves gets smeared away in the residual at normal
bitrates. Sigil's own encode keeps enough bitrate for the mark to survive in every frame. After someone
else re-encodes the file, the mark is most reliably found in their keyframes (I-frames are coded from
scratch, like a JPEG), so the detector reads keyframes first and then evenly spaced frames.

Since ``sigil-video-v2`` the audio track (when it has at least ``audio.MIN_SECONDS`` of sound) also gets the
``sigil-audio-ss-v1`` spread-spectrum mark with the same content ID, re-encoded to AAC 192k. The detector reads
both: the audio mark survives re-encodes that wipe the frame mark (heavy CRF, downscales), and the frame mark
covers silent or muted clips. Shorter or silent tracks are copied unmarked (``audio_marked`` is False).
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

from . import audio as audio_mark
from . import watermark

SCHEME = "sigil-video-v2"         # per-frame sigil-dctqim-v2 at H.264 CRF 18 + sigil-audio-ss-v1 on the audio track
AUDIO_BITRATE = "192k"
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


def _encode_cmd(audio_src: Path, dst: Path, w: int, h: int, fps: Fraction, audio: str | None) -> list[str]:
    cmd = ["ffmpeg", "-y", "-loglevel", "error", "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{w}x{h}",
           "-framerate", f"{fps.numerator}/{fps.denominator}", "-i", "-"]
    if audio:
        cmd += ["-i", str(audio_src), "-map", "0:v:0", "-map", "1:a:0?", "-c:a", audio]
        if audio == "aac":
            cmd += ["-b:a", AUDIO_BITRATE]
    cmd += ["-c:v", "libx264", "-preset", PRESET, "-crf", str(CRF), "-pix_fmt", "yuv420p",
            "-movflags", "+faststart", "-shortest", str(dst)]
    return cmd


def _mark_audio_track(src: Path, cid: int, key: bytes) -> Path | None:
    """Watermark the first audio track of ``src`` into a WAV next to it. None when the track is too short
    or silent to carry the audio mark (it is then copied unmarked)."""
    try:
        a = audio_mark.probe(src)
        if a["duration"] and a["duration"] < audio_mark.MIN_SECONDS:
            return None
        sr, ch = a["sample_rate"], min(a["channels"], 2)
        pcm = audio_mark._decode(src, sr, ch)
        if not len(pcm) or not np.any(pcm):
            return None
        marked = audio_mark.embed_pcm(pcm, sr, cid, key)
    except audio_mark.AudioError:
        return None
    wav = src.with_name(src.name + ".marked.wav")
    r = subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "f32le", "-ar", str(sr), "-ac", str(ch),
                        "-i", "-", "-c:a", "pcm_f32le", str(wav)],
                       input=marked.astype(np.float32).tobytes(), capture_output=True, timeout=TIMEOUT)
    if r.returncode != 0:
        raise VideoError("audio track encode failed: " + r.stderr.decode(errors="replace")[-300:].strip())
    return wav


def embed_file(src: Path, dst: Path, cid: int, key: bytes,
               frame_fn: Callable[[Image.Image], Image.Image] | None = None) -> dict:
    """Watermark every frame of ``src`` into an H.264 MP4 at ``dst``, and the audio track with the audio
    mark (re-encoded to AAC). Audio too short to mark is copied when the MP4 container allows it, otherwise
    re-encoded to AAC. ``frame_fn`` runs on each frame before marking (used for the visible label).
    Returns the probe info of the source plus ``frames`` and ``audio_marked``."""
    info = probe(src)
    if info["duration"] > MAX_SECONDS:
        raise VideoError(f"video too long ({info['duration']:.0f}s; max {MAX_SECONDS}s)")
    # yuv420p needs even dimensions: crop (never rescale) one pixel off an odd edge
    w, h = info["width"] // 2 * 2, info["height"] // 2 * 2
    if min(w, h) < watermark.MIN_SIDE:
        raise VideoError(f"video too small to watermark (min side {watermark.MIN_SIDE}px)")
    marked_wav = _mark_audio_track(src, cid, key) if info["has_audio"] else None
    audio_src = marked_wav or src
    last = ""
    for audio in (["aac"] if marked_wav else ["copy", "aac"] if info["has_audio"] else [None]):
        dec = subprocess.Popen(["ffmpeg", "-loglevel", "error", "-i", str(src), "-map", "0:v:0",
                                "-vf", f"crop={w}:{h}:0:0", "-f", "rawvideo", "-pix_fmt", "rgb24",
                                "-fps_mode", "cfr", "-r", f"{info['fps'].numerator}/{info['fps'].denominator}",
                                "-"], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        enc = subprocess.Popen(_encode_cmd(audio_src, dst, w, h, info["fps"], audio),
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
            if marked_wav:
                marked_wav.unlink(missing_ok=True)
            return info | {"frames": n, "width": w, "height": h, "audio_marked": marked_wav is not None}
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


def _detect_audio(path: Path, key: bytes, info: dict) -> dict | None:
    if not info["has_audio"]:
        return None
    try:
        return audio_mark.detect_file(path, key)
    except audio_mark.AudioError:
        return None


def detect_file(path: Path, key: bytes) -> dict:
    """Look for the Sigil watermark in keyframes, then evenly spaced frames, and in the audio track.
    Returns {'found', 'content_id', 'confidence', 'scheme', 'frames_checked', 'frames_matched', 'found_in',
    'audio'}; ``found_in`` lists 'frames' and/or 'audio', ``audio`` is the audio detector's result (or None
    when there is no audio track)."""
    info = probe(path)
    frames = _detect_frames(path, key, info)
    aud = _detect_audio(path, key, info)
    out = frames | {"found_in": ["frames"] if frames["found"] else [], "audio": aud}
    if aud and aud["found"]:
        if not frames["found"]:
            out |= {"found": True, "content_id": aud["content_id"], "confidence": aud["confidence"],
                    "found_in": ["audio"]}
        elif aud["content_id"] == frames["content_id"]:
            out |= {"confidence": max(frames["confidence"], aud["confidence"]), "found_in": ["frames", "audio"]}
        elif frames["frames_matched"] < AGREE:     # one stray frame vs a CRC-checked audio decode
            out |= {"content_id": aud["content_id"], "confidence": aud["confidence"], "found_in": ["audio"]}
    return out


def _detect_frames(path: Path, key: bytes, info: dict) -> dict:
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
