# Sigil

**One API call makes your AI output compliant with the labeling laws in the EU, California, India and Korea.**

AI-content labeling became law in 2026:

| Jurisdiction | Law | In force | What it asks for |
|---|---|---|---|
| EU | AI Act, Article 50 | 2 Aug 2026 (systems already on the market: **2 Dec 2026**) | Machine-readable marking of AI image, video, audio and text; disclose deepfakes; tell users they're talking to AI |
| California | AI Transparency Act (SB 942 + AB 853 + SB 1000) | 2 Aug 2026 | Latent disclosure, optional visible disclosure, free disclosure verification tool (no user threshold since SB 1000) |
| India | IT Rules amendment on synthetic content | 20 Feb 2026 | Prominent labels plus permanent metadata or unique IDs on realistic AI media |
| South Korea | AI Basic Act | 22 Jan 2026 (fines after the grace period, Jan 2027) | Advance notice, watermark on AI output, visible label on deepfakes |

The big model labs now mark their own outputs. **Your app still has its own obligations**, and resizing, converting or re-encoding a file usually strips the metadata. Sigil gives every file layered marking that survives that, and keeps the evidence.

## What Sigil does to each file

1. **Signed C2PA manifest** declaring AI origin (IPTC `trainedAlgorithmicMedia`), plus provider, system, version, timestamp and content ID: the "latent disclosure".
2. **Invisible watermark** carrying the content ID. It survives JPEG down to quality 50, WebP and brightness changes, so the file stays traceable after the metadata is stripped.
3. **Visible label** where a rule requires one (deepfakes in the EU, India and Korea), in 8 languages.
4. **Registry + audit log**, so you can hand a compliance evidence export to an enterprise buyer or a regulator.
5. **A hosted public detector** (`/verify`) that anyone can use. It's the free detection tool SB 942 requires.

Text gets a logged fingerprint that survives light edits, plus optional disclosure lines. Statistical text watermarking has to happen at generation time, so Sigil tells you to turn on your model provider's text watermark as the robust layer.

## Quick start

```bash
pip install -e ".[dev]"
uvicorn sigil.api:app --port 8000          # API + public detector at http://localhost:8000/verify

# Mark an image for every jurisdiction you serve
curl -s -H "Authorization: Bearer dev" -F file=@gen.png -F provider="Acme AI" -F system=acme-img \
     -F jurisdictions=eu,ca,in,kr -o marked.png -D - http://localhost:8000/v1/mark/image

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
python -m sigil rules --modality text
```

## API

| Method | Path | Auth | Purpose |
|---|---|---|---|
| POST | `/v1/mark/image` | key | Returns the marked file; receipt in `X-Sigil-Receipt` (base64 JSON) |
| POST | `/v1/mark/text` | key | Logs a fingerprint, optionally appends a localized disclosure |
| POST | `/v1/verify` | public | Provenance + watermark + registry check, compliance report per jurisdiction |
| GET | `/v1/rules` | public | Which controls apply for a modality, jurisdictions, deepfake flag and user count |
| GET | `/v1/evidence` | key | Compliance evidence export (all marks + audit log) |
| POST / GET | `/v1/keys` | key or admin | Create an API key (shown once) / list your org's keys |
| DELETE | `/v1/keys/{id}` | key or admin | Revoke a key |
| GET | `/v1/usage?period=YYYY-MM` | key | Metered usage for your org this month (`mark.image`, `mark.text`, `verify.image`, `verify.text`) |
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

- The watermark does not survive cropping, rescaling or a deliberate removal attack. That's why it's paired with signed metadata and the registry. Rescale-robust and audio/video marking are on the roadmap.
- Rate limits are kept in memory per process: they reset on restart and are not shared across replicas. Fine for one instance; multi-instance hosting needs a shared store (e.g. Redis). Keyless `/v1/verify` is limited per IP, so clients behind one NAT share a bucket and someone rotating IPs can exceed it.
- The JS/TS SDK is not published to npm yet; install it from `sdk/js`. It is a thin client: marking and detection run on the Sigil server, not in the browser or at the edge.
- Usage metering counts successful requests per org per month in SQLite. It is the data billing will need, not billing itself.
- Development builds sign with a self-generated certificate, so C2PA validators report `signingCredential.untrusted`. Production needs a certificate from a C2PA-trusted CA.
- Rules are engineering guidance with sources in `sigil/policy.py`, versioned so audit logs record what was applied. **Not legal advice.** California's SB 1000 (signed Sept 2026) removed SB 942's 1M-user threshold.

## Run tests

```bash
pytest -q          # includes the JS SDK tests when node + typescript are installed (skipped otherwise)
```

Apache-2.0.
