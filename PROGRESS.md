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

## Next (in priority order)
1. Hosted public detector (free tier on Fly.io, Render or Hugging Face Spaces) so `/verify` has a public URL
2. Production C2PA signing certificate from a C2PA-trusted CA (dev cert shows as "untrusted")
3. JS/TS SDK (one-line integration for Next.js apps); publishing to npm needs Maddy's npm account
4. Rescale- and crop-tolerant watermark (sync template)
5. Video (per-frame mark + C2PA for MP4), then audio watermark (AudioSeal, MIT)
6. Billing (Stripe metered, reads the `usage` table); needs Maddy's Stripe account
7. Follow-ups to keys/metering: shared rate-limit store (Redis) for multi-instance hosting; per-plan quotas

Priorities 4-7 wait for design-partner feedback. Build what paying users ask for first.

## Decisions
- One shared public detection key, so any verifier can find Sigil marks. Per-org private keys come with the Growth plan
- Text: fingerprint + disclosure only. Customers turn on their model provider's text watermark for the robust layer
- Open core: CLI, SDK and rules engine are Apache-2.0. The hosted API, branded detector and evidence store are paid
