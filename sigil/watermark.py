"""Imperceptible image watermark carrying a 48-bit content ID (+16-bit CRC).

v2 (``sigil-dctqim-v2``, used for new marks) is built to survive cropping and rescaling:

* Each 8x8 pixel block's luma is projected on the (1,1) DCT basis and that coefficient is
  quantized (dithered QIM). Every block sits on a known lattice, which lets the detector
  find the 8-pixel grid again after a crop shifted it (lattice score, no key needed).
* Bits are laid out in a periodic 8x16-block tile: 64 payload cells + 64 keyed sync cells.
  After a crop the detector does not know where the tile starts; it tries every cyclic
  shift and keeps the one where the sync cells match.
* Rescaling is undone by searching the scale factor (0.5x to 2x): a coarse pass scores
  local grid alignment on a central window, then a fine pass refines it.

Not covered: rotation, flips, non-uniform (aspect-changing) resizes, scales below 0.5x or
above 2x, crops leaving less than ~160px per side, and deliberate removal attacks. That is
why Sigil always pairs the watermark with a signed C2PA manifest and a registry record.

v1 (``sigil-dwtdct-v1``, keyed DWT-DCT, no geometry tolerance) is kept for detection only,
so files marked before v2 still verify.
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
SCHEME_V1 = "sigil-dwtdct-v1"
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


def embed_v1(img: Image.Image, cid: int, key: bytes = b"") -> Image.Image:
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


def detect_v1(img: Image.Image, key: bytes = b"") -> dict:
    """v1 reader, kept so files marked before v2 still verify."""
    if min(img.size) < MIN_SIDE:
        return {"found": False, "content_id": None, "confidence": 0.0, "scheme": SCHEME_V1,
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
            "confidence": round(agreement, 3), "scheme": SCHEME_V1}


# ---------------------------------------------------------------- v2: crop / rescale tolerant
SCHEME = "sigil-dctqim-v2"
B = 8                     # block side in pixels
TILE = (8, 16)            # tile of blocks: rows x cols = 128 cells
D2 = 40.0                 # QIM step (~44 dB PSNR); bits sit at D2/4 and 3*D2/4 mod D2
MIN_DETECT = 96           # min side (after undoing the scale) worth searching
SCALES = np.exp(np.linspace(np.log(0.5), np.log(2.0), 57))   # ~2.5% steps
WINDOW = 384              # coarse-search window (restored pixels)
FINE_WINDOW = 1024        # fine search + decode window
SYNC_MIN = 0.45           # normalized sync correlation needed to accept a tile phase


def _basis() -> np.ndarray:
    n = np.arange(B)
    c = np.cos(np.pi * (2 * n + 1) / (2 * B))
    k = np.outer(c, c)
    return (k / np.linalg.norm(k)).astype(np.float32)


K = _basis()


def _tile(cid: int, key: bytes) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Return (tile bits 0/1, payload-cell index per bit, sync mask) for a TILE."""
    n = TILE[0] * TILE[1]
    seed = int.from_bytes(hashlib.sha256(b"sigil-wm-v2" + key).digest()[:8], "big")
    rng = np.random.default_rng(seed)
    order = rng.permutation(n)
    pay_cells, sync_cells = order[:PAYLOAD_BITS], order[PAYLOAD_BITS:]
    sync_bits = rng.integers(0, 2, n - PAYLOAD_BITS)
    flat = np.zeros(n, dtype=np.uint8)
    flat[pay_cells] = _payload(cid) if cid is not None else 0
    flat[sync_cells] = sync_bits
    sync = np.zeros(n)
    sync[sync_cells] = 2.0 * sync_bits - 1.0
    return flat.reshape(TILE), pay_cells, sync.reshape(TILE)


def _luma(img: Image.Image) -> np.ndarray:
    return np.asarray(img.convert("RGB").convert("YCbCr").getchannel(0), dtype=np.float32)


def embed(img: Image.Image, cid: int, key: bytes = b"") -> Image.Image:
    if min(img.size) < MIN_SIDE:
        raise ValueError(f"image too small to watermark (min side {MIN_SIDE}px)")
    alpha = img.getchannel("A") if img.mode in ("RGBA", "LA") else None
    y, cb, cr = _split(img)
    nr, nc = y.shape[0] // B, y.shape[1] // B
    H, W = nr * B, nc * B
    bits, _, _ = _tile(cid, key)
    grid = np.tile(bits, (-(-nr // TILE[0]), -(-nc // TILE[1])))[:nr, :nc]
    off = D2 / 4 + grid * (D2 / 2)
    for _ in range(2):   # second pass re-quantizes after uint8 rounding / clipping
        blocks = y[:H, :W].reshape(nr, B, nc, B)
        c = np.einsum("iajb,ab->ij", blocks, K)
        t = D2 * np.round((c - off) / D2) + off
        y[:H, :W] += np.einsum("ij,ab->iajb", t - c, K).reshape(H, W)
        y = np.clip(np.round(y), 0, 255)
    chans = [Image.fromarray(c.astype(np.uint8)) for c in (y, np.round(cb), np.round(cr))]
    out = Image.merge("YCbCr", chans).convert("RGB")
    if alpha is not None:
        out.putalpha(alpha)
    return out


def _response(y: np.ndarray) -> np.ndarray:
    """(1,1)-DCT coefficient of the 8x8 block starting at every pixel."""
    r = cv2.filter2D(y, cv2.CV_32F, K, anchor=(0, 0), borderType=cv2.BORDER_REPLICATE)
    return r[: y.shape[0] - B + 1, : y.shape[1] - B + 1]


def _lattice(r: np.ndarray) -> np.ndarray:
    return np.cos((4 * np.pi / D2) * (r - D2 / 4))   # 1 where a block sits on the QIM lattice


def _grid_score(lat: np.ndarray, patch: int = 0) -> tuple[float, int, int]:
    """Best 8-px grid offset and its contrast (best minus median over the 64 offsets).
    With ``patch`` > 0 the offset is chosen per patch of that many blocks (tolerant of a
    slightly wrong scale) and the contrasts are averaged."""
    n, m = lat.shape[0] // B, lat.shape[1] // B
    if patch:
        p, q = n // patch, m // patch
        if p == 0 or q == 0:
            return -1.0, 0, 0
        t = lat[: p * patch * B, : q * patch * B].reshape(p, patch, B, q, patch, B).mean(axis=(1, 4))
        t = t.transpose(0, 2, 1, 3).reshape(p * q, B * B)
        return float(np.mean(t.max(1) - np.median(t, 1))), 0, 0
    t = lat[: n * B, : m * B].reshape(n, B, m, B).mean(axis=(0, 2))
    oy, ox = np.unravel_index(int(np.argmax(t)), t.shape)
    return float(t.max() - np.median(t)), int(oy), int(ox)


def _restore(y: np.ndarray, s: float, window: int) -> np.ndarray:
    """Undo a resize by factor ``s`` on a central window of ~``window`` restored pixels."""
    h, w = y.shape
    ch, cw = min(h, int(window * s) + B), min(w, int(window * s) + B)
    y0, x0 = (h - ch) // 2, (w - cw) // 2
    crop = y[y0:y0 + ch, x0:x0 + cw]
    if abs(s - 1) < 1e-6:
        return crop
    size = (max(1, round(cw / s)), max(1, round(ch / s)))
    return cv2.resize(crop, size, interpolation=cv2.INTER_CUBIC if s < 1 else cv2.INTER_AREA)


def _decode(r: np.ndarray, oy: int, ox: int, key: bytes) -> dict | None:
    c = r[oy::B, ox::B]
    soft = -np.cos((2 * np.pi / D2) * (c - D2 / 4))     # +1 = bit 1, -1 = bit 0
    tr, tc = TILE
    nr, nc = c.shape[0] // tr * tr, c.shape[1] // tc * tc
    if nr == 0 or nc == 0:
        return None
    cells = soft[:nr, :nc].reshape(nr // tr, tr, nc // tc, tc).sum(axis=(0, 2))
    _, pay_cells, sync = _tile(None, key)
    best = None
    for dy in range(tr):
        for dx in range(tc):
            sh = np.roll(cells, (dy, dx), axis=(0, 1))
            score = float((sh * sync).sum() / max(np.abs(sh[sync != 0]).sum(), 1e-9))
            if best is None or score > best[0]:
                best = (score, sh)
    score, sh = best
    if score < SYNC_MIN:
        return None
    vals = sh.reshape(-1)[pay_cells]
    word = 0
    for v in vals:
        word = (word << 1) | int(v > 0)
    cid, crc = word >> 16, word & 0xFFFF
    if _crc16(cid.to_bytes(6, "big")) != crc:
        return None
    return {"content_id": id_to_hex(cid), "sync": round(score, 3)}


def _try(y: np.ndarray, s: float, window: int, key: bytes) -> tuple[dict | None, float]:
    ry = _restore(y, s, window)
    if min(ry.shape) < MIN_DETECT:
        return None, -1.0
    r = _response(ry)
    contrast, oy, ox = _grid_score(_lattice(r))
    return _decode(r, oy, ox, key), contrast


def detect(img: Image.Image, key: bytes = b"") -> dict:
    """Return {'found', 'content_id', 'confidence' 0..1, 'scheme', 'scale'}.

    Tries v2 at the original scale, then searches scale; falls back to the v1 reader."""
    miss = {"found": False, "content_id": None, "confidence": 0.0, "scheme": SCHEME}
    y = _luma(img)
    if min(y.shape) < MIN_DETECT // 2:
        return {**miss, "note": "image too small"}
    hit, _ = _try(y, 1.0, 1 << 30, key)          # fast path: not resized
    scale = 1.0
    if not hit:
        coarse = []
        for s in SCALES:
            ry = _restore(y, s, WINDOW)
            if min(ry.shape) < MIN_DETECT:
                continue
            coarse.append((_grid_score(_lattice(_response(ry)), patch=8)[0], s))
        coarse.sort(reverse=True)
        side = max(y.shape)
        for _, s0 in coarse[:3]:
            span = min(FINE_WINDOW, side / s0)
            step = 1.5 / span                       # <= ~1.5 px drift across the window
            fine = [(_try(y, s, FINE_WINDOW, key), s)
                    for s in s0 * np.exp(np.arange(-0.015, 0.0151, step))]
            fine = [(c, h, s) for (h, c), s in fine if h]
            if fine:
                _, hit, scale = max(fine, key=lambda f: f[0])
                break
    if hit:
        return {"found": True, "content_id": hit["content_id"], "confidence": hit["sync"],
                "scheme": SCHEME, "scale": round(float(scale), 3)}
    if min(img.size) >= MIN_SIDE:
        old = detect_v1(img, key)
        if old["found"]:
            return old
    return miss
