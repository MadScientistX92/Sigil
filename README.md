# Sigil

**One API call makes your AI output compliant with the labeling laws in the EU, California, India and Korea.**

AI-content labeling became law in 2026:

| Jurisdiction | Law | In force | What it asks for |
|---|---|---|---|
| EU | AI Act, Article 50 | 2 Aug 2026 (systems already on the market: **2 Dec 2026**) | Machine-readable marking of AI image, video, audio and text; disclose deepfakes; tell users they're talking to AI |
| California | AI Transparency Act (SB 942 + AB 853 + SB 1000) | 2 Aug 2026 | Latent disclosure and a free disclosure verification tool (no user threshold and no manifest/visible disclosure requirement since SB 1000) |
| India | IT Rules amendment on synthetic content | 20 Feb 2026 | Prominent labels plus permanent metadata or unique IDs on realistic AI media |
| South Korea | AI Basic Act | 22 Jan 2026 (fines after the grace period, Jan 2027) | Advance notice, watermark on AI output, visible label on deepfakes |

The big model labs now mark their own outputs. **Your app still has its own obligations**, and resizing, converting or re-encoding a file usually strips the metadata. Sigil gives every file layered marking that survives that, and keeps the evidence.

## What Sigil does to each file

1. **Signed C2PA manifest** declaring AI origin (IPTC `trainedAlgorithmicMedia`), plus provider, system, version, timestamp and content ID: the "latent disclosure".
2. **Invisible watermark** carrying the content ID (~42 dB PSNR). It survives JPEG down to quality 50, WebP, brightness changes, cropping and rescaling (0.5x to 2x, also combined with a crop and re-encode), so the file stays traceable after the metadata is stripped.
3. **Visible label** where a rule requires one (deepfakes in the EU, India and Korea), in 8 languages.
4. **Registry + audit log**, so you can hand a compliance evidence export to an enterprise buyer or a regulator.
5. **A hosted public detector** (`/verify`) that anyone can use. It's the free disclosure verification tool California's SB 942 requires (as amended by SB 1000).

Video gets the same three layers: the watermark goes into every frame, the clip is re-encoded to H.264 MP4 (audio kept) and the MP4 carries the signed C2PA manifest. Video needs `ffmpeg` on the server (the Docker image includes it).

Audio (TTS, voice clones, music) gets a spread-spectrum watermark shaped to the sound itself (~19-26 dB below the signal, nothing added in silence) plus a signed C2PA manifest, in WAV, FLAC, MP3 or M4A. It survives MP3/AAC re-encodes, resampling, mono downmix, volume changes and trimming. Also needs `ffmpeg`.

Text gets a logged fingerprint that survives light edits, plus optional disclosure lines. Statistical text watermarking has to happen at generation time, so Sigil tells you to turn on your model provider's text watermark as the robust layer.

## Quick start

```bash
pip install -e ".[dev]"
uvicorn sigil.api:app --port 8000          # API + public detector at http://localhost:8000/verify

# Mark an image for every jurisdiction you serve
curl -s -H "Authorization: Bearer dev" -F file=@gen.png -F provider="Acme AI" -F system=acme-img \
     -F jurisdictions=eu,ca,in,kr -o marked.png -D - http://localhost:8000/v1/mark/image

# Mark a video (any format ffmpeg reads; you get H.264 MP4 back)
curl -s -H "Authorization: Bearer dev" -F file=@gen.mov -F provider="Acme AI" -F system=acme-vid \
     -o marked.mp4 http://localhost:8000/v1/mark/video

# Mark audio (MP3 in, MP3 out; WAV/FLAC/M4A are kept too, anything else comes back as WAV)
curl -s -H "Authorization: Bearer dev" -F file=@voice.mp3 -F provider="Acme AI" -F system=acme-tts \
     -o marked.mp3 http://localhost:8000/v1/mark/audio

# Check any file (free, no key)
curl -s -F file=@marked.png -F jurisdictions=eu http://localhost:8000/v1/verify | jq .compliance
```

JavaScript / TypeScript (Next.js, Node 18+, Edge, browsers), see [`sdk/js`](sdk/js/README.md):

```ts
import { Sigil } from "@sigil-ai/sdk";
const sigil = new Sigil({ apiKey: process.env.SIGIL_API_KEY, baseUrl: process.env.SIGIL_URL });
const { data, receipt } = await sigil.markImage(png, { provider: "Acme AI", system: "acme-img" });
```

CLI:

```bash
python -m sigil check some-ai-image.png --jurisdictions eu,kr     # exit code 1 if gaps found
python -m sigil mark gen.png out.jpg --provider "Acme AI" --system acme-img --deepfake
python -m sigil mark clip.mov clip.mp4 --provider "Acme AI" --system acme-vid
python -m sigil mark voice.wav voice.mp3 --provider "Acme AI" --system acme-tts
python -m sigil rules --modality text
```

## API

| Method | Path | Auth | Purpose |
|---|---|---|---|
| POST | `/v1/mark/image` | key | Returns the marked file; receipt in `X-Sigil-Receipt` (base64 JSON) |
| POST | `/v1/mark/video` | key | Returns a marked H.264 MP4 (every frame watermarked, C2PA signed, audio kept); receipt in `X-Sigil-Receipt` |
| POST | `/v1/mark/audio` | key | Returns marked WAV/FLAC/MP3/M4A (watermark + C2PA; `output_format` to pick); receipt in `X-Sigil-Receipt` |
| POST | `/v1/mark/text` | key | Logs a fingerprint, optionally appends a localized disclosure |
| POST | `/v1/verify` | public | Image, video, audio or text: provenance + watermark + registry check, compliance report per jurisdiction |
| GET | `/v1/rules` | public | Which controls apply for a modality, jurisdictions, deepfake flag and user count |
| GET | `/v1/evidence` | key | Compliance evidence export (all marks + audit log) |
| POST / GET | `/v1/keys` | key or admin | Create an API key (shown once) / list your org's keys |
| DELETE | `/v1/keys/{id}` | key or admin | Revoke a key |
| GET | `/v1/usage?period=YYYY-MM` | key | Metered usage for your org this month (`mark.image`, `mark.video`, `mark.audio`, `mark.text`, `verify.image`, `verify.video`, `verify.audio`, `verify.text`) |
| GET | `/verify`, `/v/{id}` | public | Hosted detector page, public provenance record |

### API keys, rate limits and usage

Keys live in the database, stored as SHA-256 hashes. Create the first one with the CLI or the admin token:

```bash
python -m sigil keys create --org acme --name prod          # prints sk_sigil_... once
SIGIL_ADMIN_TOKEN=... uvicorn sigil.api:app                  # then POST /v1/keys {"org": "acme"} with that token
```

An org key can create, list and revoke keys for its own org only. `SIGIL_API_KEYS` (default `dev:default`) still
works for local bootstrap; set it to an empty string in production.

`/v1/verify` stays free and keyless but is rate limited per client IP (`SIGIL_VERIFY_RATE_ANON`, default 30/min).
Requests with a key get their own bucket (`SIGIL_VERIFY_RATE_KEYED`, default 600/min). Over the limit you get
`429` with `Retry-After`. Behind a proxy, set `SIGIL_TRUST_PROXY=1` so the real client IP from `X-Forwarded-For` is used.

## Honest limits

- The image watermark (`sigil-dctqim-v2`) survives crops and uniform rescales between 0.5x and 2x, but not rotation, flips, aspect-changing (non-uniform) resizes, scales outside 0.5x-2x, crops that leave less than roughly 160 px per side, heavy downscales of already small images (e.g. crop + 0.5x on a 512 px image), or a deliberate removal attack. That's why it's paired with signed metadata and the registry. Detection searches scale, so verifying a resized image takes ~0.1-0.4 s instead of milliseconds. Images marked before v2 (`sigil-dwtdct-v1`) still verify but keep v1's limits (no crop or rescale).
- Audio (`sigil-audio-ss-v1`): survives MP3 at 64 kbps and up, AAC at 96 kbps, Opus at 64 kbps (32 kbps works on speech, not on music), resampling down to 8 kHz, mono downmix, volume changes, a 4 kHz low-pass and trimming (no sync needed). It does not survive time-stretch, pitch-shift or speed changes (even 2% faster breaks it), clips shorter than about 4 s of non-silent audio (marking needs at least 3 s), added broadband noise around 30 dB SNR on short clips (10 s+ clips survived it in tests), being mixed under louder content, or a deliberate removal attack. The mark follows the signal's own loudness, so near-silent passages carry little of it. Robustness was measured on synthetic speech-like and music-like signals, not yet on a real TTS/music corpus, and the ~19-26 dB watermark-to-signal ratio has not had a listening test; treat audibility as unverified until it has. Audio deepfakes still need an audible disclosure (or a label in your player) where a rule asks for a visible label: Sigil does not add one, and the receipt says so. The watermark is not yet applied to the audio track of videos. Same 25 MB / 10 minute upload limit.
- Video (`sigil-video-v1`): the frame watermark survives metadata stripping, a typical platform re-encode (H.264 around CRF 23, VP9 at ~1 Mbps), trimming and moderate crops. It is patchy after heavier compression (CRF 28, HEVC CRF 28) or a 0.75x downscale (found on 720p in tests, not on small 480x272 clips) and is not found after CRF 32 or a 0.5x downscale. Reason: H.264/HEVC predict P-frames from earlier frames, which smears a fixed per-frame mark, so after someone else's re-encode the detector mostly finds it in keyframes and may match only 1 of ~7 sampled frames. Sigil encodes at CRF 18 so every frame of its own output carries the mark, which makes files roughly 3-4x larger than a typical CRF 23 upload. Output is always H.264 MP4 (yuv420p, constant frame rate: variable-frame-rate input is resampled, odd dimensions lose one pixel row/column). Max 10 minutes and 25 MB per API upload; marking runs at roughly real time for 720p. Verifying a video takes a few seconds. C2PA is only read from MP4/MOV, not WebM. Same rotation/flip limits as images.
- Rate limits are kept in memory per process: they reset on restart and are not shared across replicas. Fine for one instance; multi-instance hosting needs a shared store (e.g. Redis). Keyless `/v1/verify` is limited per IP, so clients behind one NAT share a bucket and someone rotating IPs can exceed it.
- The JS/TS SDK is not published to npm yet; install it from `sdk/js`. It is a thin client: marking and detection run on the Sigil server, not in the browser or at the edge.
- Usage metering counts successful requests per org per month in SQLite. It is the data billing will need, not billing itself.
- Development builds sign with a self-generated certificate, so C2PA validators report `signingCredential.untrusted`. Production needs a certificate from a C2PA-trusted CA.
- Rules are engineering guidance with sources in `sigil/policy.py`, versioned so audit logs record what was applied. **Not legal advice.** California's SB 1000 (signed Sept 2026) removed SB 942's 1M-user threshold and its manifest (visible) disclosure requirement.

## Run tests

```bash
pytest -q          # includes the JS SDK tests when node + typescript are installed (skipped otherwise)
```

Apache-2.0.
