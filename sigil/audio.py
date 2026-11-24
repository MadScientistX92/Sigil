"""Audio marking: a keyed spread-spectrum watermark carrying the 48-bit content ID (+16-bit CRC).

Scheme ``sigil-audio-ss-v1``. Works in a 16 kHz mono analysis domain, so any sample rate, channel
layout or codec maps onto the same chips:

* The mark is a periodic chip sequence (±1, 16 kHz) split into 96 slots of 256 chips:
  64 payload slots (one bit each, sign-modulated) and 32 keyed sync slots; 16 ms each, so one
  period is 1.54 s and a typical clip repeats it several times.
* Embedding shapes the chips to the host's own short-time spectrum (STFT, 300 Hz-6 kHz) at
  ``STRENGTH`` relative to the host in each bin, so the mark hides under the content and is
  absent in silence. The 16 kHz watermark is resampled to the file's rate and added to every channel.
* Detection decodes to 16 kHz mono, whitens the spectrum (removes the host's colour), folds the
  signal modulo the period (every repeat adds up coherently, wherever the file was cut), finds the
  period phase by FFT cross-correlation with the sync template, then reads the 64 payload slots and
  checks the CRC (Chase decoding: up to two of the weakest bits may be flipped). No timing
  information is needed, so trimmed clips still decode.

Survives: MP3/AAC re-encodes at normal bitrates (64 kbps and up in tests), resampling (down to 8 kHz),
mono downmix, volume changes, trimming, low-pass at 4 kHz. Not covered: time-stretch, pitch-shift or
speed changes (even 2%), clips shorter than ~4 s of non-silent audio, broadband noise at ~30 dB SNR on
short clips (10 s+ clips survive it), mixing under louder content, and deliberate removal attacks.
Needs ``ffmpeg`` for decoding/encoding.
"""
from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
from math import gcd
from pathlib import Path

import numpy as np
from scipy import signal

from .watermark import PAYLOAD_BITS, _crc16, _payload

SCHEME = "sigil-audio-ss-v1"
FS = 16000
SLOT = 256
N_SYNC = 32
N_SLOTS = PAYLOAD_BITS + N_SYNC
PERIOD = SLOT * N_SLOTS              # 24576 samples = 1.536 s
NFFT, HOP = 512, 256
BAND = (300.0, 6000.0)
STRENGTH = 0.12                      # watermark / host magnitude per STFT bin (~ -18 dB)
MIN_SECONDS = 3.0                    # reliable detection wants ~4 s or more
MAX_SECONDS = 600
Z_MIN = 7.0                          # sync peak significance (in std devs) needed before trusting a decode
CHASE = 8                            # try flipping up to 2 of the 8 least reliable bits (37 CRC checks)
TIMEOUT = 300
# Output formats Sigil writes (all carry C2PA). Inputs in any other format come back as WAV.
FORMATS = {"WAV": ("wav", ["-c:a", "pcm_s16le"]), "FLAC": ("flac", ["-c:a", "flac"]),
           "MP3": ("mp3", ["-c:a", "libmp3lame", "-b:a", "192k"]),
           "M4A": ("ipod", ["-c:a", "aac", "-b:a", "192k"])}
MIME = {"WAV": "audio/wav", "FLAC": "audio/flac", "MP3": "audio/mpeg", "M4A": "audio/mp4"}


class AudioError(ValueError):
    pass


def available() -> bool:
    return bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))


def _need():
    if not available():
        raise AudioError("audio support needs ffmpeg and ffprobe on PATH (apt install ffmpeg)")


def sniff(data: bytes) -> str | None:
    """Return 'WAV' | 'FLAC' | 'MP3' | 'M4A' | 'OGG' for audio containers, else None.
    MP4 files are only treated as audio when the brand says so (M4A); video MP4s are left to video."""
    if data[:3] == b"ID3" and len(data) >= 10:      # ID3v2 tag (MP3, and C2PA-signed FLAC): look past it
        size = 0
        for b in data[6:10]:                           # syncsafe integer, 7 bits per byte
            size = (size << 7) | (b & 0x7F)
        size += 10 + (10 if data[5] & 0x10 else 0)
        inner = data[size:size + 16]
        return "FLAC" if inner[:4] == b"fLaC" else "MP3"
    h = data[:16]
    if h[:4] == b"RIFF" and h[8:12] == b"WAVE":
        return "WAV"
    if h[:4] == b"fLaC":
        return "FLAC"
    if (len(h) > 1 and h[0] == 0xFF and (h[1] & 0xE0) == 0xE0 and (h[1] & 0x06)):
        return "MP3"
    if h[4:8] == b"ftyp" and h[8:11] in (b"M4A", b"M4B"):
        return "M4A"
    if h[:4] == b"OggS":
        return "OGG"
    return None


# ---------- keyed template ----------
def _template(key: bytes):
    """(chips[PERIOD] ±1, is_payload[N_SLOTS], payload_slot_order[64], sync_bits[32] ±1)."""
    seed = int.from_bytes(hashlib.sha256(b"sigil-audio-v1" + key).digest()[:8], "big")
    rng = np.random.default_rng(seed)
    chips = rng.integers(0, 2, PERIOD).astype(np.float64) * 2 - 1
    order = rng.permutation(N_SLOTS)
    pay_slots, sync_slots = order[:PAYLOAD_BITS], order[PAYLOAD_BITS:]
    sync_bits = rng.integers(0, 2, N_SYNC).astype(np.float64) * 2 - 1
    return chips, pay_slots, sync_slots, sync_bits


def _slot_signs(cid: int, key: bytes) -> np.ndarray:
    chips, pay_slots, sync_slots, sync_bits = _template(key)
    signs = np.zeros(N_SLOTS)
    signs[pay_slots] = _payload(cid).astype(np.float64) * 2 - 1
    signs[sync_slots] = sync_bits
    return np.repeat(signs, SLOT) * chips


def _band_mask(freqs: np.ndarray) -> np.ndarray:
    return ((freqs >= BAND[0]) & (freqs <= BAND[1])).astype(np.float64)


def _resample(x: np.ndarray, src: int, dst: int) -> np.ndarray:
    if src == dst:
        return x
    g = gcd(src, dst)
    return signal.resample_poly(x, dst // g, src // g, axis=0)


# ---------- embed / detect on PCM ----------
def embed_pcm(pcm: np.ndarray, sr: int, cid: int, key: bytes = b"") -> np.ndarray:
    """``pcm``: float array (samples, channels) in [-1, 1]. Returns the marked PCM, same shape."""
    pcm = np.asarray(pcm, np.float64)
    if pcm.ndim == 1:
        pcm = pcm[:, None]
    mono16 = _resample(pcm.mean(axis=1), sr, FS)
    n = len(mono16)
    if n < MIN_SECONDS * FS:
        raise AudioError(f"audio too short to watermark (min {MIN_SECONDS:.1f} s)")
    u = np.resize(_slot_signs(cid, key), n)          # periodic, starting at t=0
    f, _, X = signal.stft(mono16, FS, window="hann", nperseg=NFFT, noverlap=NFFT - HOP, boundary="even")
    _, _, U = signal.stft(u, FS, window="hann", nperseg=NFFT, noverlap=NFFT - HOP, boundary="even")
    mag = signal.convolve2d(np.abs(X), np.ones((5, 1)) / 5, mode="same", boundary="symm")
    unit = np.sqrt(np.mean(np.abs(U[(f > BAND[0]) & (f < BAND[1])]) ** 2)) + 1e-12
    W = U / unit * STRENGTH * mag * _band_mask(f)[:, None]
    _, w16 = signal.istft(W, FS, window="hann", nperseg=NFFT, noverlap=NFFT - HOP, boundary=True)
    w = _resample(w16[:n], FS, sr)
    w = np.resize(w, len(pcm)) if len(w) >= len(pcm) else np.pad(w, (0, len(pcm) - len(w)))
    return np.clip(pcm + w[:, None], -1.0, 1.0)


def _whiten(x16: np.ndarray) -> np.ndarray:
    f, _, R = signal.stft(x16, FS, window="hann", nperseg=NFFT, noverlap=NFFT - HOP, boundary="even")
    a = np.abs(R)
    mag = np.maximum(a, signal.convolve2d(a, np.ones((5, 1)) / 5, mode="same", boundary="symm"))
    Rw = R / (mag + 1e-9) * _band_mask(f)[:, None]     # every bin at most unit size: tones can't dominate
    _, y = signal.istft(Rw, FS, window="hann", nperseg=NFFT, noverlap=NFFT - HOP, boundary=True)
    return y[:len(x16)]


def detect_pcm(x16: np.ndarray, key: bytes = b"") -> dict:
    """``x16``: mono float PCM at 16 kHz. Returns {'found', 'content_id', 'confidence', 'scheme', 'sync_z'}."""
    miss = {"found": False, "content_id": None, "confidence": 0.0, "scheme": SCHEME, "sync_z": 0.0}
    x16 = np.asarray(x16, np.float64)
    if len(x16) < PERIOD or not np.any(x16):
        return miss
    y = _whiten(x16)
    k = -(-len(y) // PERIOD)
    fold = np.pad(y, (0, k * PERIOD - len(y))).reshape(k, PERIOD).sum(axis=0)
    chips, pay_slots, sync_slots, sync_bits = _template(key)
    sync_t = np.zeros(N_SLOTS)
    sync_t[sync_slots] = sync_bits
    tmpl = np.repeat(sync_t, SLOT) * chips
    corr = np.fft.irfft(np.fft.rfft(fold) * np.conj(np.fft.rfft(tmpl)), PERIOD)
    med, sd = np.median(corr), corr.std() + 1e-12
    best = None
    for tau in np.argsort(corr)[::-1][:3]:
        z = float((corr[tau] - med) / sd)
        if z < Z_MIN:
            break
        aligned = np.roll(fold, -int(tau)) * chips
        soft = aligned.reshape(N_SLOTS, SLOT).sum(axis=1)[pay_slots]
        hit = _chase(soft)
        if hit is not None:
            cid, flips = hit
            best = {"found": True, "content_id": f"{cid:012x}", "scheme": SCHEME, "sync_z": round(z, 1),
                    "confidence": round(min(1.0, z / 15) * (1 - 0.15 * flips), 3)}
            break
    return best or miss | {"sync_z": round(float((corr.max() - med) / sd), 1)}


def _chase(soft: np.ndarray) -> tuple[int, int] | None:
    """Chase-style decode: the hard decision, then single and double flips of the weakest bits,
    accepting the first word whose CRC-16 matches. Unmarked audio rarely passes the sync gate,
    so the extra CRC tries barely change the false-positive rate."""
    base = (soft > 0).astype(np.int64)
    weak = np.argsort(np.abs(soft))[:CHASE]
    trials = [()] + [(i,) for i in weak] + [(i, j) for a, i in enumerate(weak) for j in weak[a + 1:]]
    for flip in trials:
        bits = base.copy()
        bits[list(flip)] ^= 1
        word = 0
        for b in bits:
            word = (word << 1) | int(b)
        cid, crc = word >> 16, word & 0xFFFF
        if _crc16(cid.to_bytes(6, "big")) == crc:
            return cid, len(flip)
    return None


# ---------- files (ffmpeg) ----------
def probe(path: Path) -> dict:
    _need()
    r = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "a:0", "-show_entries",
                        "stream=sample_rate,channels:format=duration", "-of", "json", str(path)],
                       capture_output=True, timeout=60)
    info = json.loads(r.stdout or b"{}")
    streams = info.get("streams") or []
    if r.returncode != 0:
        raise AudioError("could not read audio: " + r.stderr.decode(errors="replace")[-300:].strip())
    if not streams:
        raise AudioError("no audio stream found")
    s = streams[0]
    return {"sample_rate": int(s.get("sample_rate") or 44100), "channels": int(s.get("channels") or 1),
            "duration": float((info.get("format") or {}).get("duration") or 0)}


def _decode(path: Path, sr: int | None = None, channels: int | None = None) -> np.ndarray:
    cmd = ["ffmpeg", "-loglevel", "error", "-i", str(path), "-map", "0:a:0", "-f", "f32le"]
    if sr:
        cmd += ["-ar", str(sr)]
    if channels:
        cmd += ["-ac", str(channels)]
    r = subprocess.run(cmd + ["-"], capture_output=True, timeout=TIMEOUT)
    if r.returncode != 0:
        raise AudioError("could not decode audio: " + r.stderr.decode(errors="replace")[-300:].strip())
    a = np.frombuffer(r.stdout, np.float32)
    return a.reshape(-1, channels) if channels else a


def embed_file(src: Path, dst: Path, cid: int, key: bytes, fmt: str) -> dict:
    """Watermark ``src`` (any format ffmpeg reads) into ``dst`` as ``fmt`` (WAV/FLAC/MP3/M4A)."""
    info = probe(src)
    if info["duration"] > MAX_SECONDS:
        raise AudioError(f"audio too long ({info['duration']:.0f}s; max {MAX_SECONDS}s)")
    sr, ch = info["sample_rate"], min(info["channels"], 2)
    pcm = embed_pcm(_decode(src, sr, ch), sr, cid, key)
    muxer, codec = FORMATS[fmt]
    r = subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "f32le", "-ar", str(sr), "-ac", str(ch),
                        "-i", "-", *codec, "-f", muxer, str(dst)],
                       input=pcm.astype(np.float32).tobytes(), capture_output=True, timeout=TIMEOUT)
    if r.returncode != 0:
        raise AudioError("audio encode failed: " + r.stderr.decode(errors="replace")[-300:].strip())
    return info | {"channels": ch}


def detect_file(path: Path, key: bytes) -> dict:
    _need()
    return detect_pcm(_decode(path, FS, 1)[:, 0].astype(np.float64), key)
