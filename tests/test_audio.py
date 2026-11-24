"""Audio marking: spread-spectrum watermark + C2PA for WAV/FLAC/MP3/M4A. Skipped when ffmpeg is not installed."""
import base64
import json
import subprocess

import numpy as np
import pytest
from fastapi.testclient import TestClient
from scipy import signal

from sigil import audio, policy
from sigil.core import AUDIO_DISCLOSURE, PUBLIC_KEY, MarkOptions, Sigil

pytestmark = pytest.mark.skipif(not audio.available(), reason="ffmpeg/ffprobe not installed")

OPTS = dict(provider="Acme AI", system="acme-tts", system_version="1.0")
CID = 0x123456789ABC


def _music(sec=8.0, sr=44100, seed=1):
    """Stereo plucked-note melody with a little noise (tonal content is the hard case for codecs)."""
    rng = np.random.default_rng(seed)
    t = np.arange(int(sec * sr)) / sr
    x = np.zeros_like(t)
    for i, f0 in enumerate(rng.choice([220, 247, 262, 294, 330, 349, 392, 440], size=int(sec * 4))):
        seg = (t >= i * 0.25) & (t < (i + 1) * 0.25)
        tt = t[seg] - i * 0.25
        for h in range(1, 8):
            x[seg] += np.exp(-tt * 6) * np.sin(2 * np.pi * f0 * h * tt) / h
    x += 0.02 * rng.standard_normal(len(t))
    return np.stack([x, np.roll(x, 200)], 1) / np.abs(x).max() * 0.6


def _speech(sec=8.0, sr=24000, seed=2):
    """Mono speech-like signal: glottal pulse train through three formants, syllable envelope, a pause."""
    rng = np.random.default_rng(seed)
    n = int(sec * sr)
    ph = np.cumsum((120 + 20 * np.sin(np.arange(n) / sr * 3)) / sr)
    src = (np.diff(np.floor(ph), prepend=0) > 0).astype(float) + 0.05 * rng.standard_normal(n)
    y = np.zeros(n)
    for fc, bw in [(500, 80), (1500, 120), (2500, 200)]:
        b, a = signal.iirpeak(fc, fc / bw, sr)
        y += signal.lfilter(b, a, src)
    env = signal.lfilter([1], [1, -0.995], (np.sin(np.arange(n) / sr * 2 * np.pi * 4) > -0.2).astype(float))
    env[int(3 * sr):int(3.6 * sr)] = 0
    y *= env
    return (y / np.abs(y).max() * 0.5)[:, None]


def _write(path, pcm, sr):
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "f32le", "-ar", str(sr), "-ac", str(pcm.shape[1]),
                    "-i", "-", str(path)], input=pcm.astype(np.float32).tobytes(), check=True)
    return path


def _ffmpeg(src, dst, *args):
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(src), *args, str(dst)], check=True)
    return dst


def _found(path):
    r = audio.detect_file(path, PUBLIC_KEY)
    return r["found"] and r["content_id"] == f"{CID:012x}"


@pytest.fixture(scope="module")
def marked(tmp_path_factory):
    d = tmp_path_factory.mktemp("audio")
    out = {}
    for name, (pcm, sr) in {"music": (_music(), 44100), "speech": (_speech(), 24000)}.items():
        _write(d / f"{name}_src.wav", pcm, sr)
        m = audio.embed_pcm(pcm, sr, CID, PUBLIC_KEY)
        out[name] = (_write(d / f"{name}_marked.wav", m, sr), pcm, m)
    return d, out


@pytest.mark.parametrize("name", ["music", "speech"])
def test_embed_is_quiet_and_detects(marked, name):
    _, out = marked
    path, pcm, m = out[name]
    snr = 10 * np.log10((pcm ** 2).sum() / ((m - pcm) ** 2).sum())
    assert snr > 18, snr
    assert _found(path)


@pytest.mark.parametrize("name", ["music", "speech"])
def test_unmarked_and_wrong_key_not_found(marked, name):
    d, out = marked
    assert not audio.detect_file(d / f"{name}_src.wav", PUBLIC_KEY)["found"]
    assert not audio.detect_file(out[name][0], b"some-other-key")["found"]


def test_no_false_positives_on_noise_and_music():
    for s in range(12):
        x = (_music(6, 16000, seed=100 + s)[:, 0] if s % 2 else
             np.random.default_rng(s).standard_normal(16000 * 6) * 0.1)
        r = audio.detect_pcm(x, PUBLIC_KEY)
        assert not r["found"] and r["sync_z"] < audio.Z_MIN


ATTACKS = {
    "mp3_128k": ("x.mp3", ["-b:a", "128k"]),
    "mp3_64k": ("x.mp3", ["-b:a", "64k"]),
    "aac_96k": ("x.m4a", ["-c:a", "aac", "-b:a", "96k"]),
    "opus_64k": ("x.opus", ["-b:a", "64k"]),
    "resample_22k": ("x.wav", ["-ar", "22050"]),
    "telephone_8k": ("x.wav", ["-ar", "8000"]),
    "mono": ("x.wav", ["-ac", "1"]),
    "quieter": ("x.wav", ["-af", "volume=0.3"]),
    "lowpass_4k": ("x.wav", ["-af", "lowpass=4000"]),
    "trim": ("x.wav", ["-ss", "1.37", "-t", "5"]),
}


@pytest.mark.parametrize("name", ["music", "speech"])
@pytest.mark.parametrize("attack", list(ATTACKS))
def test_survives(marked, tmp_path, name, attack):
    _, out = marked
    fname, args = ATTACKS[attack]
    assert _found(_ffmpeg(out[name][0], tmp_path / fname, *args)), (name, attack)


def test_survives_noise_on_longer_clip():
    pcm = _music(12, 44100, seed=7)
    x = audio._resample(audio.embed_pcm(pcm, 44100, CID, PUBLIC_KEY).mean(axis=1), 44100, audio.FS)
    x = x + np.random.default_rng(0).standard_normal(len(x)) * np.sqrt(np.mean(x ** 2)) * 0.03   # ~30 dB SNR
    assert audio.detect_pcm(x, PUBLIC_KEY)["content_id"] == f"{CID:012x}"


def test_speed_change_breaks_it(marked, tmp_path):
    """Documented limit (README Honest limits): time-stretch / speed changes are not covered."""
    _, out = marked
    assert not _found(_ffmpeg(out["music"][0], tmp_path / "s.wav", "-af", "atempo=1.02"))


def test_too_short_rejected():
    with pytest.raises(audio.AudioError):
        audio.embed_pcm(_speech(2.0), 24000, CID, PUBLIC_KEY)


def test_sniff():
    assert audio.sniff(b"RIFF\0\0\0\0WAVEfmt ") == "WAV"
    assert audio.sniff(b"fLaC\0\0\0\x22") == "FLAC"
    assert audio.sniff(b"ID3\x04\0\0\0\0\0\0") == "MP3"
    assert audio.sniff(b"\0\0\0\x20ftypM4A \0\0\0\0") == "M4A"
    assert audio.sniff(b"\0\0\0\x20ftypisom\0\0\0\0") is None        # video MP4
    assert audio.sniff(b"\x89PNG\r\n\x1a\n") is None
    assert audio.sniff(b"\xff\xd8\xff\xe0\0\x10JFIF") is None         # JPEG


@pytest.mark.parametrize("fmt", ["WAV", "MP3", "M4A", "FLAC"])
def test_mark_audio_roundtrip(marked, tmp_path, fmt):
    d, out = marked
    sg = Sigil(tmp_path / "data")
    src = (d / "speech_src.wav").read_bytes()
    data, receipt = sg.mark_audio(src, MarkOptions(**OPTS, output_format=fmt, deepfake=True))
    assert audio.sniff(data) == fmt and receipt.modality == "audio"
    assert set(receipt.controls_applied) >= {policy.WATERMARK, policy.IDENTIFIER, policy.C2PA}
    assert receipt.still_required[policy.VISIBLE] == AUDIO_DISCLOSURE
    rep = sg.verify_audio(data, policy.Context("audio", ("eu", "ca", "kr")))
    assert rep["c2pa"]["present"] and rep["c2pa"]["ai_generated"]
    assert rep["watermark"]["found"] and rep["watermark"]["content_id"] == receipt.content_id
    assert rep["record"]["modality"] == "audio"
    # strip the metadata (re-encode): the watermark alone still finds the record
    (tmp_path / "m").write_bytes(data)
    stripped = _ffmpeg(tmp_path / "m", tmp_path / "s.mp3", "-b:a", "128k", "-map_metadata", "-1")
    rep2 = sg.verify_audio(stripped.read_bytes(), policy.Context("audio", ("eu",)))
    assert not rep2["c2pa"]["present"] and rep2["watermark"]["found"]
    assert rep2["record"]["content_id"] == receipt.content_id
    assert "metadata was stripped" in rep2["verdict"]


def test_api_mark_and_verify_audio(marked, tmp_path, monkeypatch):
    from sigil import api
    monkeypatch.setattr(api, "sigil", Sigil(tmp_path / "data"))
    d, _ = marked
    c = TestClient(api.app)
    mp3 = _ffmpeg(d / "speech_src.wav", tmp_path / "in.mp3", "-b:a", "192k").read_bytes()
    r = c.post("/v1/mark/audio", headers={"Authorization": "Bearer dev"},
               files={"file": ("v.mp3", mp3, "audio/mpeg")}, data={"provider": "Acme AI", "system": "acme-tts"})
    assert r.status_code == 200, r.text
    assert r.headers["content-type"] == "audio/mpeg"
    receipt = json.loads(base64.b64decode(r.headers["X-Sigil-Receipt"]))
    v = c.post("/v1/verify", files={"file": ("v.mp3", r.content, "audio/mpeg")}, data={"jurisdictions": "eu"})
    assert v.status_code == 200, v.text
    body = v.json()
    assert body["watermark"]["content_id"] == receipt["content_id"] and body["c2pa"]["present"]
    assert body["compliance"]["ok"]
    usage = c.get("/v1/usage", headers={"Authorization": "Bearer dev"}).json()
    assert "mark.audio" in json.dumps(usage)
    bad = c.post("/v1/mark/audio", headers={"Authorization": "Bearer dev"},
                 files={"file": ("x.wav", b"RIFF\0\0\0\0WAVEjunk", "audio/wav")},
                 data={"provider": "a", "system": "b"})
    assert bad.status_code == 422
