"""Imperceptible image watermark: keyed DWT-DCT quantization index modulation.

Payload is a 48-bit content ID plus a 16-bit CRC, repeated across every 8x8 pixel
block and recovered by majority vote. It survives re-encoding (JPEG, WebP) and
mild edits; it does not survive cropping, rescaling or a targeted removal attack.
That is why Sigil always pairs it with a signed C2PA manifest and a registry record.
"""
from __future__ import annotations

import hashlib
import secrets

import cv2
import numpy as np
import pywt
from PIL import Image

PAYLOAD_BITS = 64
ID_BITS = 48
SCHEME = "sigil-dwtdct-v1"
DELTA = 36.0           # quantization step; higher = more robust, less invisible
COEF = (1, 1)          # mid-frequency DCT coefficient inside each 4x4 LL block
MIN_SIDE = 128


def new_content_id() -> int:
    return secrets.randbits(ID_BITS)


def id_to_hex(cid: int) -> str:
    return f"{cid:012x}"


def _crc16(data: bytes) -> int:
    crc = 0xFFFF
    for b in data:
        crc ^= b << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) & 0xFFFF if crc & 0x8000 else (crc << 1) & 0xFFFF
    return crc


def _payload(cid: int) -> np.ndarray:
    raw = cid.to_bytes(6, "big")
    word = (cid << 16) | _crc16(raw)
    return np.array([(word >> (63 - i)) & 1 for i in range(PAYLOAD_BITS)], dtype=np.uint8)


def _bit_map(n_blocks: int, key: bytes) -> np.ndarray:
    seed = int.from_bytes(hashlib.sha256(b"sigil-wm" + key).digest()[:8], "big")
    rng = np.random.default_rng(seed)
    return rng.permutation(n_blocks) % PAYLOAD_BITS


def _split(img: Image.Image):
    ycc = img.convert("RGB").convert("YCbCr")
    y, cb, cr = [np.asarray(c, dtype=np.float64) for c in ycc.split()]
    return y, cb, cr


def _blocks(ll: np.ndarray):
    h, w = ll.shape
    for by in range(h // 4):
        for bx in range(w // 4):
            yield by, bx


def embed(img: Image.Image, cid: int, key: bytes = b"") -> Image.Image:
    if min(img.size) < MIN_SIDE:
        raise ValueError(f"image too small to watermark (min side {MIN_SIDE}px)")
    alpha = img.getchannel("A") if img.mode in ("RGBA", "LA") else None
    y, cb, cr = _split(img)
    h, w = y.shape
    H, W = h - h % 8, w - w % 8
    ll, highs = pywt.dwt2(y[:H, :W], "haar")
    coords = list(_blocks(ll))
    bitmap = _bit_map(len(coords), key)
    bits = _payload(cid)
    for i, (by, bx) in enumerate(coords):
        blk = ll[by * 4:by * 4 + 4, bx * 4:bx * 4 + 4]
        d = cv2.dct(blk)
        c = d[COEF]
        offset = DELTA / 2 if bits[bitmap[i]] else 0.0
        d[COEF] = DELTA * np.round((c - offset) / DELTA) + offset
        ll[by * 4:by * 4 + 4, bx * 4:bx * 4 + 4] = cv2.idct(d)
    y2 = y.copy()
    y2[:H, :W] = pywt.idwt2((ll, highs), "haar")
    chans = [Image.fromarray(np.clip(np.round(c), 0, 255).astype(np.uint8)) for c in (y2, cb, cr)]
    out = Image.merge("YCbCr", chans).convert("RGB")
    if alpha is not None:
        out.putalpha(alpha)
    return out


def detect(img: Image.Image, key: bytes = b"") -> dict:
    """Return {'found': bool, 'content_id': hex|None, 'confidence': 0..1}."""
    if min(img.size) < MIN_SIDE:
        return {"found": False, "content_id": None, "confidence": 0.0, "scheme": SCHEME,
                "note": "image too small"}
    y, _, _ = _split(img)
    h, w = y.shape
    H, W = h - h % 8, w - w % 8
    ll, _ = pywt.dwt2(y[:H, :W], "haar")
    coords = list(_blocks(ll))
    bitmap = _bit_map(len(coords), key)
    votes = np.zeros((PAYLOAD_BITS, 2))
    for i, (by, bx) in enumerate(coords):
        c = cv2.dct(ll[by * 4:by * 4 + 4, bx * 4:bx * 4 + 4])[COEF]
        d0 = abs(c - DELTA * np.round(c / DELTA))
        d1 = abs(c - (DELTA * np.round((c - DELTA / 2) / DELTA) + DELTA / 2))
        votes[bitmap[i], 0 if d0 < d1 else 1] += 1
    bits = (votes[:, 1] > votes[:, 0]).astype(int)
    agreement = float(np.mean(votes.max(axis=1) / np.maximum(votes.sum(axis=1), 1)))
    word = 0
    for b in bits:
        word = (word << 1) | int(b)
    cid, crc = word >> 16, word & 0xFFFF
    ok = _crc16(cid.to_bytes(6, "big")) == crc and agreement > 0.6
    return {"found": bool(ok), "content_id": id_to_hex(cid) if ok else None,
            "confidence": round(agreement, 3), "scheme": SCHEME}
