# Sigil

**One API call makes your AI output compliant with the labeling laws in the EU, California, India and Korea.**

AI-content labeling became law in 2026:

| Jurisdiction | Law | In force | What it asks for |
|---|---|---|---|
| EU | AI Act, Article 50 | 2 Aug 2026 (systems already on the market: **2 Dec 2026**) | Machine-readable marking of AI image, video, audio and text; disclose deepfakes; tell users they're talking to AI |
| California | AI Transparency Act (SB 942 + AB 853) | 2 Aug 2026 | Latent disclosure, optional visible disclosure, free public detection tool (1M+ monthly users) |
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
| GET | `/verify`, `/v/{id}` | public | Hosted detector page, public provenance record |

## Honest limits

- The watermark does not survive cropping, rescaling or a deliberate removal attack. That's why it's paired with signed metadata and the registry. Rescale-robust and audio/video marking are on the roadmap.
- Development builds sign with a self-generated certificate, so C2PA validators report `signingCredential.untrusted`. Production needs a certificate from a C2PA-trusted CA.
- Rules are engineering guidance with sources in `sigil/policy.py`, versioned so audit logs record what was applied. **Not legal advice.** California's pending SB 1000 may change SB 942.

## Run tests

```bash
pytest -q
```

Apache-2.0.
