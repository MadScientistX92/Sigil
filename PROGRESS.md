# Progress log

Engineering log for Sigil. Read this first when resuming work, and update it at the end of every run.

## Shipped
- **v0.1.0 (2026-11-01)**: policy engine (EU, California, India, Korea; 7 rules, versioned ruleset), keyed DWT-DCT
  image watermark (~42 dB PSNR; survives JPEG down to q50, WebP, brightness shifts), C2PA signing and reading
  (c2pa-python), registry + audit log + evidence export, text MinHash fingerprints, visible labels in 8 languages,
  FastAPI service, public `/verify` page, CLI, Dockerfile, 11 passing tests
- **2026-11-05**: pushed to GitHub, Apache-2.0 license added
- **2026-11-10**: API keys stored hashed in the DB (`/v1/keys` create/list/revoke, `sigil keys` CLI, admin
  token bootstrap), per-IP / per-key token-bucket rate limit on `/v1/verify` (429 + Retry-After), monthly usage
  metering per org (`/v1/usage`). Fixed `pip install` (setuptools found two top-level packages, `sigil` and `site`).
  18 passing tests
- **2026-11-13**: JS/TS SDK in `sdk/js` (`@sigil-ai/sdk`, zero deps, fetch-based: Node 18+, Next.js Node/Edge,
  browsers). Covers mark image/text, verify (file or text), rules, record, usage, evidence, keys; typed
  `SigilError` with `retryAfter` on 429. Unit tests (fake fetch) + end-to-end test against a live uvicorn, both
  driven from `pytest` (`tests/test_js_sdk.py`, skipped without node/tsc). `"private": true` so it can't be
  published by accident. 20 passing tests
- **2026-11-13**: ruleset 2026-11-13. California SB 1000 (signed Sept 2026, effective immediately) removed SB 942's 1M monthly-user threshold and replaced the AI detection tool with a disclosure verification tool; `ca.sb942` updated, threshold test flipped, README and site table updated. Sources: Covington (Inside Privacy, 2026-10-06), WilmerHale (2026-10-05). Bill text not read yet; penalty and operative date unchanged pending a check. EU, India, Korea: no rule changes found.
- **2026-11-16**: ruleset 2026-11-16. Read SB 1000's chaptered text (Ch. 861, Stats. 2026, signed 2026-09-30, urgency statute): it also removed the requirement to offer a manifest (visible) disclosure, so `visible_label` is no longer a California control (California images were getting a visible label by default). `ca.sb942` summary now lists the full latent-disclosure contents, the verification-tool requirements (third-party tool allowed) and the licensee notice-and-cure process; penalty and Aug 2 2026 date confirmed. New test `test_ca_no_visible_label_after_sb1000`; README and site updated. Sources: SB 1000 text on Digital Democracy (CalMatters), MoFo (2026-10-01). EU, India, Korea: no rule changes found (EU Art. 50 unchanged by the AI omnibus per secondary sources; Code of Practice final status unconfirmed).
- **2026-11-17**: crop- and rescale-tolerant image watermark `sigil-dctqim-v2` (now used for all new marks).
  Dithered QIM on each 8x8 block's (1,1) DCT coefficient in the pixel domain, bits laid out in a periodic 8x16-block
  tile (64 payload + 64 keyed sync cells). Detector re-finds the 8-px grid from the QIM lattice (no key needed),
  resolves the tile phase from the sync cells, and searches scale 0.5x-2x (coarse local-alignment pass on a central
  window, then a fine pass). Survives crop, 0.5x-2x rescale, crop + rescale + JPEG 70, JPEG 50, WebP, brightness;
  ~42 dB PSNR. v1 kept as a detection fallback so old marks still verify. Not covered: rotation, flips,
  aspect-changing resizes, crops under ~160 px. Verify on a resized image now costs ~0.1-0.4 s. 18 new tests in
  `tests/test_watermark.py`. 39 passing tests
- **2026-11-20**: video marking `sigil-video-v1` (first slice of Next #4). `sigil/video.py` streams frames through
  ffmpeg pipes, applies the v2 image watermark (and the visible label when a rule needs it) to every frame,
  re-encodes to H.264 MP4 at CRF 18 (audio copied, AAC fallback) and core signs a C2PA manifest into the MP4.
  Detector reads up to 6 keyframes then up to 6 evenly spaced frames and votes on the content id (stops at 2
  agreeing frames). `POST /v1/mark/video`, `/v1/verify` sniffs video uploads, CLI `mark`/`check` take videos,
  metering `mark.video` / `verify.video`, ffmpeg added to the Dockerfile. Survives metadata strip, H.264 CRF 23,
  VP9 ~1 Mbps, trim, moderate crop; patchy at CRF 28 / 0.75x downscale; fails CRF 32 and 0.5x (README Honest
  limits). 10 new tests in `tests/test_video.py` (skipped without ffmpeg). 49 passing tests
- **2026-11-23**: ruleset 2026-11-23. EU `eu.art50.2`: clarified that the Digital Omnibus on AI (Regulation (EU) 2026/1744, in force 2026-07-27) gives the 2026-12-02 grace date only to generative systems placed on the market before 2026-08-02 (later systems: from 2026-08-02); summary now reflects the final Code of Practice (published 2026-06-10, adequate 2026-07-08/09: at least two machine-readable techniques) and the 2027-02-02 detector-interoperability date. Replaced the stale pre-drafting EC FAQ source on all three EU rules with EUR-Lex, the Code page, Paul Weiss (2026-08-04), Reed Smith (2026-07-28) and Gibson Dunn (2026-05-27). Dates, controls and penalties unchanged. Open conflict (not acted on): DLA Piper's timetable table says 2 Dec 2027 for pre-Aug systems while its body text and the other firms say 2 Dec 2026; Omnibus body text not yet read on EUR-Lex. California, India (MeitY 2026-10-08 labelling advisory to social media intermediaries is non-binding, no rule change), Korea: no rule changes found. New outside scope: Pennsylvania Act 99 of 2026 (AI deceptive ads disclosure, signed 2026-10-22, effective ~2026-12-21, advertisers only). 49 passing tests

## Next (in priority order)
1. Hosted public detector (free tier on Fly.io, Render or Hugging Face Spaces) so `/verify` has a public URL
2. Production C2PA signing certificate from a C2PA-trusted CA (dev cert shows as "untrusted")
3. Publish the JS SDK to npm: needs Maddy's npm account and a decision on the package name/scope
   (`@sigil-ai/sdk` is a placeholder); then drop `"private": true` in `sdk/js/package.json`
4. Audio watermark (AudioSeal, MIT). Video follow-ups: `markVideo` in the JS SDK; video on the `/verify` page;
   stronger mark for re-encodes (embed at lower spatial frequency or spread the payload over several frames and
   soft-combine across frames, which needs a per-frame-consistent scheme because QIM soft values don't average);
   background jobs for clips over 25 MB; watermark the audio track once the audio mark exists
5. Billing (Stripe metered, reads the `usage` table); needs Maddy's Stripe account
6. Follow-ups to keys/metering: shared rate-limit store (Redis) for multi-instance hosting; per-plan quotas
7. Watermark follow-ups: rotation / aspect-change tolerance (estimate the tile lattice from its autocorrelation
   instead of a 1-D scale search); skip the slow v1 fallback on very large images once v1 marks age out

Priorities 4-7 wait for design-partner feedback. Build what paying users ask for first.

## Decisions
- One shared public detection key, so any verifier can find Sigil marks. Per-org private keys come with the Growth plan
- Text: fingerprint + disclosure only. Customers turn on their model provider's text watermark for the robust layer
- Open core: CLI, SDK and rules engine are Apache-2.0. The hosted API, branded detector and evidence store are paid
